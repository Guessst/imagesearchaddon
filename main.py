"""
Low-latency Google Images picker (PyQt6).

How it stays fast:

 1. The hidden browser is warmed up at startup (DNS / TLS / cookies) while you
    are still typing your first query.
 2. After a query, the results page is POLLED every 40 ms and the search
    finishes the moment 8 usable thumbnails exist, instead of waiting for
    loadFinished + a fixed sleep. The heavy page is then replaced by a tiny
    one so Google's scripts stop running in the background.
 3. Thumbnails that are plain URLs are downloaded with QNetworkAccessManager
    (async, HTTP/2, one multiplexed connection, no threads).
 4. The dialog opens immediately and cards fill in as images arrive.
 5. A strict request interceptor drops stylesheets, fonts, media, pings,
    beacons, favicons, prefetch and analytics/ads hosts.

Per-stage timings are printed so you can compare versions.
"""
import base64
import os
import signal
import sys
import threading
import time
import urllib.parse
from functools import partial
from typing import Optional

from PyQt6.QtCore import (
    QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, QUrl, pyqtSignal
)
from PyQt6.QtGui import QImageReader, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QVBoxLayout, QWidget
)

# Force PyQt to respect standard terminal Ctrl+C interrupts
signal.signal(signal.SIGINT, signal.SIG_DFL)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NUM_IMAGES = 8
MIN_SIDE_PX = 80                 # drops "related search" chips / icons
GOOGLE_ORIGIN = "https://www.google.com"
WARMUP_PATH = "/robots.txt"      # tiny, script-free page used to keep things warm
WARMUP_TIMEOUT_MS = 6000
NAV_POLL_MS = 40
NAV_TIMEOUT_MS = 8000
NAV_GRACE_AFTER_LOAD_MS = 400
IMG_TIMEOUT_MS = 4000
PROMPT = "\nEnter search query (or type 'quit' to exit): "


# ---------------------------------------------------------------------------
# JavaScript polled while the results page is loading
# ---------------------------------------------------------------------------
POLL_JS = r"""
(function () {
  var WANT = __WANT__, MIN = __MIN__, out = [], seen = {};
  var imgs = document.images;
  for (var i = 0; i < imgs.length && out.length < WANT; i++) {
    var img = imgs[i];
    var src = img.currentSrc || img.src || (img.dataset && img.dataset.src) || "";
    if (!src || seen[src]) continue;
    if (src.indexOf("data:image") === 0) {
      if (src.length < 1500 || img.naturalWidth < MIN || img.naturalHeight < MIN) continue;
    } else if (!/^https:\/\/encrypted-tbn\d\.gstatic\.com\//.test(src)) {
      continue;
    }
    seen[src] = 1;
    out.push(src);
  }
  return out;
})()
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _min_side(data: bytes) -> int:
    """Smaller image side read from the header only (no full decode)."""
    ba = QByteArray(data)          # must outlive the buffer
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    size = QImageReader(buf).size()
    return min(size.width(), size.height()) if size.isValid() else 0


def _decode_data_uri(src: str) -> Optional[bytes]:
    try:
        b64 = src.split(",", 1)[1]
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. Background thread for stdin input (serialised: one prompt per query)
# ---------------------------------------------------------------------------
class StdinListener(QObject):
    query_submitted = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._prompt_evt = threading.Event()
        self._prompt_evt.set()

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()

    def prompt(self):
        """Called by the controller when it is ready for the next query."""
        self._prompt_evt.set()

    def _listen(self):
        while True:
            try:
                self._prompt_evt.wait()
                self._prompt_evt.clear()
                print(PROMPT, end="", flush=True)
                line = sys.stdin.readline()
                if not line:
                    break
                query = line.strip()
                if query.lower() in ("quit", "exit", "q"):
                    print("\nExiting application...")
                    os._exit(0)
                if query:
                    self.query_submitted.emit(query)
                else:
                    self._prompt_evt.set()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting application...")
                os._exit(0)


# ---------------------------------------------------------------------------
# 2. Resource interceptor
# ---------------------------------------------------------------------------
_RT = QWebEngineUrlRequestInfo.ResourceType


def _rtypes(*names):
    return frozenset(getattr(_RT, n) for n in names if hasattr(_RT, n))


BLOCKED_TYPES = _rtypes(
    "ResourceTypeStylesheet", "ResourceTypeFontResource", "ResourceTypeMedia",
    "ResourceTypeObject", "ResourceTypePluginResource", "ResourceTypePing",
    "ResourceTypeCspReport", "ResourceTypePrefetch", "ResourceTypeFavicon",
    "ResourceTypeServiceWorker", "ResourceTypeSharedWorker",
)
BLOCKED_HOST_SUFFIXES = (
    "doubleclick.net", "googlesyndication.com", "google-analytics.com",
    "googletagmanager.com", "play.google.com", "ogs.google.com",
    "apis.google.com",
)


class SpeedOptimizerInterceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info: QWebEngineUrlRequestInfo):
        if info.resourceType() in BLOCKED_TYPES:
            info.block(True)
        elif info.requestUrl().host().endswith(BLOCKED_HOST_SUFFIXES):
            info.block(True)


# ---------------------------------------------------------------------------
# 3. Persistent hidden browser with DOM polling
# ---------------------------------------------------------------------------
class SilentWebEnginePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        pass  # mute console logs and CSP warnings


class GoogleImageScraper(QObject):
    # list of (src, bytes_or_None). bytes is set for inline data: URIs;
    # None means "download this URL". Empty list means failure.
    results_ready = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.profile = QWebEngineProfile.defaultProfile()
        self.interceptor = SpeedOptimizerInterceptor()
        self.profile.setUrlRequestInterceptor(self.interceptor)

        self.browser = QWebEngineView()
        self.page = SilentWebEnginePage(self.profile, self.browser)
        self.browser.setPage(self.page)
        self._trim_settings()
        self.page.loadFinished.connect(self._on_load_finished)

        self.query = ""
        self.num_images = NUM_IMAGES

        self._phase = "warmup"       # warmup | idle | nav | reset
        self._pending = False
        self._nav_loaded = False
        self._nav_deadline = 0.0
        self._grace_deadline: Optional[float] = None
        self._poll_inflight = False
        self._poll_js = ""

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(NAV_POLL_MS)
        self._poll_timer.timeout.connect(self._poll)

        self._warm_timer = QTimer(self)
        self._warm_timer.setSingleShot(True)
        self._warm_timer.timeout.connect(self._become_idle)

        self._load_warm_page("warmup")

    # -- setup ---------------------------------------------------------------
    def _trim_settings(self):
        s = self.page.settings()
        for name, value in (
            ("PluginsEnabled", False), ("WebGLEnabled", False),
            ("Accelerated2dCanvasEnabled", False), ("AutoLoadIconsForPage", False),
            ("PdfViewerEnabled", False), ("ShowScrollBars", False),
            ("JavascriptCanOpenWindows", False),
        ):
            attr = getattr(QWebEngineSettings.WebAttribute, name, None)
            if attr is not None:
                s.setAttribute(attr, value)

    def _load_warm_page(self, phase: str):
        self._phase = phase
        self._warm_timer.start(WARMUP_TIMEOUT_MS)
        self.page.load(QUrl(GOOGLE_ORIGIN + WARMUP_PATH))

    # -- public API ----------------------------------------------------------
    def search(self, query: str, num_images: int = NUM_IMAGES):
        if self._phase == "nav":
            return  # a search is already running
        self.query = query
        self.num_images = num_images
        if self._phase in ("warmup", "reset"):
            self._pending = True     # runs as soon as the warm page is ready
            return
        self._start_nav()

    # -- state machine -------------------------------------------------------
    def _become_idle(self):
        if self._phase not in ("warmup", "reset"):
            return
        self._warm_timer.stop()
        self._phase = "idle"
        if self._pending:
            self._pending = False
            self._start_nav()

    def _on_load_finished(self, ok: bool):
        if self._phase in ("warmup", "reset"):
            if ok and self.page.url().path() != WARMUP_PATH:
                return  # stale event from an aborted navigation
            self._become_idle()
        elif self._phase == "nav":
            self._nav_loaded = True

    def _finish(self, cands: list):
        self._poll_timer.stop()
        # get the heavy Google page out of the browser again
        self._load_warm_page("reset")
        self.results_ready.emit(cands)

    # -- polling -------------------------------------------------------------
    def _start_nav(self):
        self._phase = "nav"
        self._nav_loaded = False
        self._grace_deadline = None
        self._poll_inflight = False
        self._nav_deadline = time.monotonic() + NAV_TIMEOUT_MS / 1000.0
        self._poll_js = (POLL_JS
                         .replace("__WANT__", str(self.num_images))
                         .replace("__MIN__", str(MIN_SIDE_PX)))
        url = GOOGLE_ORIGIN + "/search?tbm=isch&q=" + urllib.parse.quote_plus(self.query)
        self.page.load(QUrl(url))
        self._poll_timer.start()

    def _poll(self):
        if self._phase != "nav" or self._poll_inflight:
            return
        self._poll_inflight = True
        self.page.runJavaScript(self._poll_js, self._on_poll_result)

    def _on_poll_result(self, result):
        self._poll_inflight = False
        if self._phase != "nav":
            return
        cands = self._build_candidates(result if isinstance(result, list) else [])
        if len(cands) >= self.num_images:
            self._finish(cands)
            return

        now = time.monotonic()
        if self._nav_loaded and self._grace_deadline is None:
            self._grace_deadline = now + NAV_GRACE_AFTER_LOAD_MS / 1000.0
        expired = now > self._nav_deadline or (
            self._grace_deadline is not None and now > self._grace_deadline)
        if expired:
            if not cands:
                print("\n[Error] Failed to load Google search page.")
            self._finish(cands)

    # -- candidate extraction ------------------------------------------------
    def _build_candidates(self, items: list) -> list:
        """Decode inline images (cheap), size-filter them, top up with URLs."""
        want = self.num_images
        inline, urls = [], []
        for src in items:
            if not isinstance(src, str):
                continue
            if src.startswith("data:image"):
                if len(inline) >= want:
                    continue
                data = _decode_data_uri(src)
                if data and _min_side(data) >= MIN_SIDE_PX:
                    inline.append((src, data))
            elif src.startswith("http"):
                urls.append((src, None))
        out = inline[:want]
        if len(out) < want:
            out += urls[: want - len(out)]
        return out


# ---------------------------------------------------------------------------
# 4. Async thumbnail downloader (one HTTP/2 connection, no threads)
# ---------------------------------------------------------------------------
class ImageLoader(QObject):
    def __init__(self):
        super().__init__()
        self.nam = QNetworkAccessManager(self)
        self._inflight = set()

    def fetch(self, url: str, callback):
        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(IMG_TIMEOUT_MS)
        req.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, True)
        reply = self.nam.get(req)
        self._inflight.add(reply)

        def _done():
            self._inflight.discard(reply)
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll()) if ok else None
            reply.deleteLater()
            callback(data or None)

        reply.finished.connect(_done)

    def abort_all(self):
        for reply in list(self._inflight):
            reply.abort()


# ---------------------------------------------------------------------------
# 5. Card-like item widget
# ---------------------------------------------------------------------------
class ImageCard(QFrame):
    def __init__(self, src: str, image_bytes: Optional[bytes], parent=None):
        super().__init__(parent)
        self.src = src
        self.data: Optional[bytes] = None
        self._selected = False
        self.setObjectName("card")   # so the stylesheet doesn't hit the QLabel

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.image_label = QLabel("Loading...")
        self.image_label.setFixedSize(140, 140)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.image_label)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_style()
        if image_bytes is not None:
            self.set_image_bytes(image_bytes)

    def set_image_bytes(self, image_bytes: Optional[bytes]):
        pix = QPixmap()
        if image_bytes:
            pix.loadFromData(image_bytes)
        if not pix.isNull():
            self.data = image_bytes
            self.image_label.setPixmap(pix.scaled(
                130, 130, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.image_label.setText("No Preview")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._selected = not self._selected
            self.update_style()
        super().mousePressEvent(event)

    def update_style(self):
        if self._selected:
            self.setStyleSheet("#card { background-color: #d4edda; "
                               "border: 2px solid #28a745; border-radius: 8px; }")
        else:
            self.setStyleSheet("#card { background-color: #f8f9fa; "
                               "border: 1px solid #ced4da; border-radius: 8px; }")

    def is_selected(self) -> bool:
        return self._selected


# ---------------------------------------------------------------------------
# 6. Selection popup dialog (opens immediately, fills in progressively)
# ---------------------------------------------------------------------------
class ImagePickerDialog(QDialog):
    all_loaded = pyqtSignal()

    def __init__(self, query: str, candidates: list, loader: ImageLoader, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Select Images for: '{query}'")
        self.resize(650, 480)
        self._closed = False
        self._pending = 0
        self.finished.connect(self._mark_closed)

        main_layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        grid = QGridLayout(container)
        grid.setSpacing(12)

        self.cards = []
        cols = 4
        for i, (src, img_bytes) in enumerate(candidates):
            card = ImageCard(src, img_bytes)
            self.cards.append(card)
            grid.addWidget(card, i // cols, i % cols)
            if img_bytes is None:
                self._pending += 1
                loader.fetch(src, partial(self._on_downloaded, card))

        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        btn_box = QHBoxLayout()
        confirm_btn = QPushButton("Done / Confirm Selection")
        confirm_btn.setStyleSheet("padding: 8px 16px; font-weight: bold;")
        confirm_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_box.addStretch()
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(confirm_btn)
        main_layout.addLayout(btn_box)

        if self._pending == 0:  # everything was inline: already populated
            QTimer.singleShot(0, self._emit_all_loaded)

    def _mark_closed(self, _result=0):
        self._closed = True

    def _emit_all_loaded(self):
        if not self._closed:
            self.all_loaded.emit()

    def _on_downloaded(self, card: ImageCard, data: Optional[bytes]):
        card.set_image_bytes(data)
        self._pending -= 1
        if self._pending == 0:
            self._emit_all_loaded()

    def get_selected_images(self) -> list:
        return [c.src for c in self.cards if c.is_selected()]

    def get_selected_bytes(self) -> list:
        """Raw image bytes of the selection (handy for Anki's media folder)."""
        return [c.data for c in self.cards if c.is_selected() and c.data]


# ---------------------------------------------------------------------------
# 7. Main coordinator
# ---------------------------------------------------------------------------
class Controller(QObject):
    def __init__(self):
        super().__init__()
        self.scraper = GoogleImageScraper()
        self.scraper.results_ready.connect(self.process_and_display)
        self.loader = ImageLoader()

        self.listener = StdinListener()
        self.listener.query_submitted.connect(self.on_query)
        self.listener.start()

        self.dialog: Optional[ImagePickerDialog] = None
        self.current_query = ""
        self.t_start = 0.0
        self.t_shown = 0.0

    def on_query(self, query: str):
        self.current_query = query
        self.t_start = time.perf_counter()
        print(f"\n[Scraper] Searching for '{query}'...")
        self.scraper.search(query, num_images=NUM_IMAGES)

    def process_and_display(self, candidates: list):
        if not candidates:
            print("\n[Scraper] No images retrieved.")
            self.listener.prompt()
            return

        t_scraped = time.perf_counter()
        dialog = ImagePickerDialog(self.current_query, candidates, self.loader)
        dialog.accepted.connect(self._on_accepted)
        dialog.rejected.connect(self._on_rejected)
        dialog.finished.connect(self._on_closed)
        dialog.all_loaded.connect(self._on_all_loaded)
        self.dialog = dialog

        dialog.open()
        dialog.raise_()
        dialog.activateWindow()
        self.t_shown = time.perf_counter()

        print(f"[Timing] scrape: {(t_scraped - self.t_start) * 1000:.0f} ms")
        print(f"[Timing] dialog built + shown: "
              f"{(self.t_shown - t_scraped) * 1000:.0f} ms")

    def _on_all_loaded(self):
        now = time.perf_counter()
        print(f"[Timing] last thumbnail visible: "
              f"{(now - self.t_shown) * 1000:.0f} ms after dialog shown")
        print(f"[Scraper] Total elapsed time to process images: "
              f"{now - self.t_start:.2f} seconds.")

    def _on_accepted(self):
        selected = self.dialog.get_selected_images()
        print(f"\n>>> Confirmed {len(selected)} image(s) for '{self.current_query}':")
        for i, src in enumerate(selected, 1):
            preview = src[:70] + "..." if len(src) > 70 else src
            print(f"    {i}. {preview}")

    def _on_rejected(self):
        print(f"\n>>> Selection cancelled for '{self.current_query}'.")

    def _on_closed(self, _result=0):
        self.loader.abort_all()
        self.listener.prompt()


def _configure_chromium_env():
    """Must run BEFORE QApplication is created.

    The hidden browser never draws anything, so the GPU process is useless
    here. Disabling it removes the "Failed to create GLES3 context" errors at
    the source; --log-level=3 (fatal only) silences whatever Chromium still
    prints, and the Qt logging rule mutes Qt's own WebEngine context messages.
    """
    extra = "--disable-gpu --disable-gpu-compositing --disable-logging --log-level=3"
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{current} {extra}".strip()

    rules = os.environ.get("QT_LOGGING_RULES", "")
    os.environ["QT_LOGGING_RULES"] = ";".join(
        r for r in (rules, "qt.webenginecontext.*=false") if r)


if __name__ == "__main__":
    _configure_chromium_env()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Empty timer lets the interpreter run periodically so Ctrl+C is caught
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(500)

    controller = Controller()
    app.exec()