import re
import json
import time
import http.client
import http.cookiejar
import urllib.parse
import urllib.request
import urllib.error
import ssl
import socket
import threading

from aqt import QAction, QKeySequence, mw
from aqt.utils import tooltip

try:
    from aqt.operations.note import update_note
except ImportError:
    update_note = None

from aqt.qt import qtmajor
if qtmajor == 6:
    from PyQt6.QtWidgets import (
        QApplication, QWidget, QDialog, QGridLayout, QPushButton,
        QVBoxLayout, QDialogButtonBox, QProgressDialog, QMessageBox
    )
    from PyQt6.QtGui import QPixmap, QIcon, QImage
    from PyQt6.QtCore import QSize, QByteArray, QBuffer, QIODevice, Qt
    BTN_OK = QDialogButtonBox.StandardButton.Ok
    BTN_CANCEL = QDialogButtonBox.StandardButton.Cancel
    IO_WRITE_ONLY = QIODevice.OpenModeFlag.WriteOnly
    APP_MODAL = Qt.WindowModality.ApplicationModal
elif qtmajor == 5:
    from PyQt5.QtWidgets import (
        QApplication, QWidget, QDialog, QGridLayout, QPushButton,
        QVBoxLayout, QDialogButtonBox, QProgressDialog, QMessageBox
    )
    from PyQt5.QtGui import QPixmap, QIcon, QImage
    from PyQt5.QtCore import QSize, QByteArray, QBuffer, QIODevice, Qt
    BTN_OK = QDialogButtonBox.Ok
    BTN_CANCEL = QDialogButtonBox.Cancel
    IO_WRITE_ONLY = QIODevice.WriteOnly
    APP_MODAL = Qt.ApplicationModal
else:
    raise ImportError("Could not find valid Qt major version, expected (5, 6) found ({})".format(qtmajor))

DEFAULT_QUERY = "capybara"

# --- Networking (standard library only) ---

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

HTTP_TIMEOUT_SECONDS = 15

NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    socket.timeout,
    ssl.SSLError,
    OSError,
)


def dialog_exec(dialog):
    """Executes a QDialog safely across PyQt5 (.exec_()) and PyQt6 (.exec())."""
    if qtmajor == 6:
        return dialog.exec()
    return dialog.exec_()


def http_get_bytes(url, timeout):
    """Fetch raw bytes from a URL using only standard library modules.
    Catches timeouts, disconnects, HTTP errors, and handles SSL chain retries."""
    if not url:
        return None
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    resp = None
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                print("[Image Search] WARNING: cert verification failed for {}, retrying without verification...".format(url))
                insecure_context = ssl.create_default_context()
                insecure_context.check_hostname = False
                insecure_context.verify_mode = ssl.CERT_NONE
                resp = urllib.request.urlopen(req, timeout=timeout, context=insecure_context)
            else:
                raise
        except ssl.SSLError:
            print("[Image Search] WARNING: SSL error for {}, retrying without verification...".format(url))
            insecure_context = ssl.create_default_context()
            insecure_context.check_hostname = False
            insecure_context.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=timeout, context=insecure_context)

        return resp.read()
    except NETWORK_ERRORS as e:
        print("[Image Search] Network failure fetching {}: {}".format(url, e))
        return None
    except Exception as e:
        print("[Image Search] Unexpected error fetching {}: {}".format(url, e))
        return None
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def build_ddg_opener():
    cookie_jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def ddg_get_bytes(opener, url, timeout, extra_headers=None):
    headers = dict(HTTP_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    resp = None
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.read()
    except NETWORK_ERRORS as e:
        raise RuntimeError("DuckDuckGo request failed for {}: {}".format(url, e))
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


# --- DuckDuckGo image search ---

def fetch_vqd_token(opener, query):
    print("[Image Search] Step 1: requesting DuckDuckGo page for vqd token...")
    token_url = "https://duckduckgo.com/?{}".format(
        urllib.parse.urlencode({"q": query})
    )
    raw_bytes = ddg_get_bytes(opener, token_url, HTTP_TIMEOUT_SECONDS)
    html = raw_bytes.decode("utf-8", errors="ignore")

    match = re.search(r'vqd=([\'"]?)([\d-]+)\1', html)
    if match:
        vqd = match.group(2)
    else:
        match = re.search(r'vqd=([\w-]+)', html)
        if not match:
            raise RuntimeError("Failed to retrieve DuckDuckGo search token (vqd).")
        vqd = match.group(1)

    print("[Image Search]     got vqd token: {}".format(vqd))
    return vqd


def fetch_image_results(opener, query, vqd, max_results, region_code):
    print("[Image Search] Step 2: fetching image results from DuckDuckGo...")
    api_params = {
        "l": region_code,
        "o": "json",
        "q": query,
        "vqd": vqd,
        "f": ",,,",
        "p": "1",
    }
    print("api_params::", api_params)
    api_url = "https://duckduckgo.com/i.js?{}".format(
        urllib.parse.urlencode(api_params)
    )
    referer = "https://duckduckgo.com/?{}".format(
        urllib.parse.urlencode({"q": query})
    )
    extra_headers = {
        "Referer": referer,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    raw_bytes = ddg_get_bytes(opener, api_url, HTTP_TIMEOUT_SECONDS, extra_headers)
    payload = json.loads(raw_bytes.decode("utf-8"))

    raw_results = payload.get("results", [])
    print("[Image Search]     received {} raw result(s)".format(len(raw_results)))

    results = []
    for item in raw_results[:max_results]:
        results.append({
            "title": item.get("title"),
            "image": item.get("image"),
            "thumbnail": item.get("thumbnail"),
            "url": item.get("url"),
            "width": item.get("width"),
            "height": item.get("height"),
        })
    return results


def search_duckduckgo_images(query, region_code, max_results=8):
    opener = build_ddg_opener()
    vqd = fetch_vqd_token(opener, query)
    return fetch_image_results(opener, query, vqd, max_results, region_code)


def now_ms():
    return time.perf_counter() * 1000


def get_config():
    config = mw.addonManager.getConfig(__name__) or {}
    return {
        "query_field": config.get("query_field", "Front"),
        "insert_field": config.get("insert_field", "Back"),
        "query_regex": config.get("query_regex", ""),
        "region_code": config.get("region_code", "wt-wt"),
    }


def apply_query_regex(query, pattern):
    if not pattern:
        return query
    try:
        match = re.search(pattern, query)
    except re.error as e:
        tooltip("Image Search: invalid regex in config ({}). Using unprocessed query.".format(e))
        return query
    if not match:
        return query
    return match.group(1) if match.groups() else match.group(0)


# --- Thread & GUI Unblocking Runner ---

def run_with_progress(task_fn, label_text, parent=None):
    """Runs task_fn in a background thread while keeping Qt event loop alive.
    Returns (result, cancelled_bool)."""
    container = {"result": None, "error": None, "done": False}

    def worker():
        try:
            container["result"] = task_fn()
        except Exception as e:
            container["error"] = e
        finally:
            container["done"] = True

    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()

    progress = QProgressDialog(label_text, "Cancel", 0, 0, parent or mw)
    progress.setWindowTitle("Image Search")
    progress.setWindowModality(APP_MODAL)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.setMinimumDuration(0)
    progress.show()

    user_cancelled = False
    while not container["done"]:
        QApplication.processEvents()
        if progress.wasCanceled():
            user_cancelled = True
            break
        time.sleep(0.02)

    progress.reset()
    progress.hide()

    if user_cancelled:
        return None, True

    if container["error"]:
        raise container["error"]

    return container["result"], False


def make_search():
    try:
        return _make_search()
    except Exception as e:
        print("[ERROR] Unexpected failure in make_search: {}".format(e))
        tooltip("Image Search failed unexpectedly: {}".format(e))
        return []


def _make_search():
    print("Entered: make_search")

    if getattr(mw, "state", None) != "review" or not mw.reviewer or not mw.reviewer.card:
        QMessageBox.warning(
            mw,
            "Image Search",
            "You must be currently reviewing a card to search for images."
        )
        return []

    config = get_config()
    query_field = config["query_field"]
    insert_field = config["insert_field"]
    region_code = config["region_code"]

    query = DEFAULT_QUERY
    note = None
    if mw.reviewer and mw.reviewer.card:
        card = mw.reviewer.card
        note = card.note()
        if query_field in note.keys():
            query = note[query_field]
        else:
            tooltip("Image Search: query field '{}' not found on this note type. Using default query.".format(query_field))

    regexed_query = apply_query_regex(query, config["query_regex"])

    def search_and_fetch():
        print("Querying {}".format(regexed_query))
        results = search_duckduckgo_images(regexed_query, region_code, max_results=8)
        if not results:
            return [], [], []

        pairs = [(r.get("thumbnail"), r.get("image")) for r in results]
        thumb_urls = [p[0] for p in pairs]
        image_bytes_list = [None] * len(thumb_urls)

        def fetch_thumb_worker(idx, url):
            image_bytes_list[idx] = fetch_image_bytes(url)

        threads = []
        for i, u in enumerate(thumb_urls):
            t = threading.Thread(target=fetch_thumb_worker, args=(i, u))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        return results, pairs, image_bytes_list

    t_start = now_ms()
    try:
        search_data, cancelled = run_with_progress(
            search_and_fetch,
            "Searching images...",
            mw
        )
    except Exception as e:
        tooltip("Image Search: image search failed ({}).".format(e))
        return []

    if cancelled:
        return []

    t_end = now_ms()
    print("Search & thumbnails elapsed ms: {:.0f}".format(t_end - t_start))

    if not search_data or not search_data[0]:
        tooltip("Image Search: no results found for '{}'.".format(regexed_query))
        return []

    results, pairs, image_bytes_list = search_data

    # UI Construction on Main Thread
    dialog = QDialog(mw)
    dialog.setWindowTitle("Select Images")
    dialog.resize(650, 380)

    layout = QVBoxLayout(dialog)
    grid = QGridLayout()
    layout.addLayout(grid)

    buttons = []
    failed_thumbs = 0
    for img_bytes, (thumb_url, full_url) in zip(image_bytes_list, pairs):
        if not img_bytes:
            failed_thumbs += 1
            continue

        pixmap = QPixmap()
        if not pixmap.loadFromData(img_bytes) or pixmap.isNull():
            failed_thumbs += 1
            continue

        btn = QPushButton()
        btn.setIcon(QIcon(pixmap))
        btn.setIconSize(QSize(130, 130))
        btn.setCheckable(True)
        btn.setStyleSheet("QPushButton:checked { border: 3px solid #4CAF50; background: #C8E6C9; }")
        btn.setProperty("full_url", full_url)

        grid.addWidget(btn, len(buttons) // 4, len(buttons) % 4)
        buttons.append(btn)

    if failed_thumbs:
        tooltip("Image Search: {} thumbnail(s) failed to load.".format(failed_thumbs))

    if not buttons:
        tooltip("Image Search: no thumbnails could be loaded.")
        return []

    button_box = QDialogButtonBox(BTN_OK | BTN_CANCEL)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    if not dialog_exec(dialog):
        return []

    selected_urls = [b.property("full_url") for b in buttons if b.isChecked()]

    if selected_urls and note is not None:
        insert_images(note, insert_field, selected_urls)

    return selected_urls


def save_note(note, count):
    if update_note is not None:
        update_note(parent=mw, note=note).success(
            lambda _: tooltip("Image Search: inserted {} image(s).".format(count))
        ).failure(
            lambda e: tooltip("Image Search: failed to save the note ({}).".format(e))
        ).run_in_background()
    else:
        try:
            mw.checkpoint("Image Search")
            note.flush()
            mw.reset()
            tooltip("Image Search: inserted {} image(s).".format(count))
        except Exception as e:
            tooltip("Image Search: failed to save the note ({}).".format(e))


def insert_images(note, insert_field, selected_urls):
    if insert_field not in note.keys():
        tooltip("Image Search: insert field '{}' not found on this note type. Images were not inserted.".format(insert_field))
        return

    print("Downloading {} full resolution images...".format(len(selected_urls)))

    def download_all_images():
        converted = [None] * len(selected_urls)

        def download_convert_worker(idx, url):
            converted[idx] = download_and_convert_to_jpg(url, idx)

        threads = []
        for i, url in enumerate(selected_urls):
            t = threading.Thread(target=download_convert_worker, args=(i, url))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        return converted

    t_downloading_start = now_ms()
    try:
        converted_images, cancelled = run_with_progress(
            download_all_images,
            "Downloading {} selected image(s)...".format(len(selected_urls)),
            mw
        )
    except Exception as e:
        tooltip("Image Search: failed to download images ({}).".format(e))
        return

    if cancelled or not converted_images:
        return

    print("Done downloading in {:.0f}ms".format(now_ms() - t_downloading_start))

    valid_img_tags = []
    failed_count = 0
    for item in converted_images:
        if not item:
            failed_count += 1
            continue
        filename, data = item
        if not filename or not data:
            failed_count += 1
            continue
        try:
            media_manager = mw.col.media
            has_write_data_func = (getattr(media_manager, "write_data", None)) is not None
            if has_write_data_func:
                actual_fname = media_manager.write_data(filename, data)
            else:
                actual_fname = media_manager.writeData(filename, data)

            valid_img_tags.append('<img src="{}">'.format(actual_fname))
        except Exception as e:
            print("[ERROR] Failed to save media file {}: {}".format(filename, e))
            failed_count += 1

    if failed_count:
        tooltip("Image Search: {} image(s) failed to download or save.".format(failed_count))

    if not valid_img_tags:
        tooltip("Image Search: none of the selected images could be saved.")
        return

    imgs_html = "<br>".join(valid_img_tags)
    if note[insert_field].strip():
        note[insert_field] += "<br>" + imgs_html
    else:
        note[insert_field] = imgs_html

    count = len(valid_img_tags)
    save_note(note, count)

    print("Updated note")


def download_and_convert_to_jpg(url, idx):
    """Downloads full res image, converts to JPG at 75% quality. Thread-safe."""
    try:
        data = http_get_bytes(url, timeout=10)
        if not data:
            return None, None
        img = QImage.fromData(data)
        if not img.isNull():
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(IO_WRITE_ONLY)

            img.save(buf, "JPG", 75)
            buf.close()
            filename = "imgsearch_{}_{}.jpg".format(int(time.time() * 1000), idx)
            return filename, bytes(ba.data())
    except Exception as e:
        print("[ERROR] Failed to process full res image {}: {}".format(url, e))
    return None, None


def fetch_image_bytes(url):
    return http_get_bytes(url, timeout=6)


action = QAction("Search Images...", mw)
action.triggered.connect(make_search)
action.setShortcut(QKeySequence("Ctrl+H"))
mw.form.menuTools.addAction(action)