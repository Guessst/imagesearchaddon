import sys
import os
import re
import time
import concurrent.futures
from urllib.parse import quote_plus

dep_dir_nam = "vendor"
sys.path.append(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        dep_dir_nam
    )
)

import requests
from ddgs import DDGS
from aqt import mw
from aqt import gui_hooks
from aqt.utils import tooltip
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QGridLayout, QPushButton, QVBoxLayout, QDialogButtonBox
from PyQt6.QtGui import QPixmap, QIcon, QImage
from PyQt6.QtCore import QSize, QByteArray, QBuffer, QIODevice

SESSION = requests.Session()

DEFAULT_QUERY = "capybara"


def now_ms():
    return time.perf_counter() * 1000


def custom_shortcuts(state: str, shortcuts: list[tuple[str, callable]]):
    if state == "review":
        shortcuts.append(("Ctrl+H", make_search))

gui_hooks.state_shortcuts_will_change.append(custom_shortcuts)


def get_config():
    config = mw.addonManager.getConfig(__name__) or {}
    return {
        "query_field": config.get("query_field", "Front"),
        "insert_field": config.get("insert_field", "Back"),
        "query_regex": config.get("query_regex", ""),
    }


def apply_query_regex(query: str, pattern: str) -> str:
    """Pre-process the query with a user-supplied regex, if one is configured.

    If the regex has a capture group, the first group is used. Otherwise the
    whole match is used. If the pattern is empty, invalid, or doesn't match,
    the original query is returned unchanged.
    """
    if not pattern:
        return query
    try:
        match = re.search(pattern, query)
    except re.error as e:
        tooltip(f"Image Search: invalid regex in config ({e}). Using unprocessed query.")
        return query
    if not match:
        return query
    return match.group(1) if match.groups() else match.group(0)


def make_search():
    """Entry point. Wrapped so an unexpected error never hangs Anki - it's
    reported via a tooltip instead."""
    try:
        return _make_search()
    except Exception as e:
        print(f"[ERROR] Unexpected failure in make_search: {e}")
        tooltip(f"Image Search failed unexpectedly: {e}")
        return []

def _make_search():
    print("Entered: make_search")
    config = get_config()
    query_field = config["query_field"]
    insert_field = config["insert_field"]

    query=DEFAULT_QUERY
    note = None
    if mw.reviewer and mw.reviewer.card:
        card = mw.reviewer.card
        note = card.note()
        if query_field in note.keys():
            query = note[query_field]
        else:
            tooltip(f"Image Search: query field '{query_field}' not found on this note type. Using default query.")

    regexed_query = apply_query_regex(query, config["query_regex"])

    t_start = now_ms()
    try:
        results = list(DDGS().images(regexed_query, max_results=8))
    except Exception as e:
        tooltip(f"Image Search: image search failed ({e}).")
        return []
    t_end = now_ms()
    print(f"Elapsed ms: {(t_end - t_start):.0f}")

    if not results:
        tooltip(f"Image Search: no results found for '{encoded_query}'.")
        return []

    pairs = [
        (
            r.get("thumbnail"),
            r.get("image"),
        )
        for r in results
    ]

    t_fetch_start = now_ms()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        thumb_urls = [p[0] for p in pairs]
        image_bytes_list = list(executor.map(fetch_image_bytes, thumb_urls))
    print(f"All thumbnails downloaded in {(now_ms() - t_fetch_start):.0f}ms")

    dialog = QDialog()
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
        tooltip(f"Image Search: {failed_thumbs} thumbnail(s) failed to load.")

    if not buttons:
        tooltip("Image Search: no thumbnails could be loaded.")
        return []

    button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    if not dialog.exec():
        return []

    selected_urls = [b.property("full_url") for b in buttons if b.isChecked()]

    if selected_urls and note is not None:
        insert_images(note, insert_field, selected_urls)

    return selected_urls


def insert_images(note, insert_field, selected_urls):
    if insert_field not in note.keys():
        tooltip(f"Image Search: insert field '{insert_field}' not found on this note type. Images were not inserted.")
        return

    print(f"Downloading {len(selected_urls)} full resolution images...")
    t_downloading_start = now_ms()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected_urls)) as executor:
            futures = [executor.submit(download_and_convert_to_jpg, url, i) for i, url in enumerate(selected_urls)]
            converted_images = [f.result() for f in futures]
    except Exception as e:
        tooltip(f"Image Search: failed to download images ({e}).")
        return
    print(f"Done downloading in {(now_ms() - t_downloading_start):.0f}ms")

    valid_img_tags = []
    failed_count = 0
    for filename, data in converted_images:
        if not filename or not data:
            failed_count += 1
            continue
        try:
            actual_fname = mw.col.media.write_data(filename, data)
            valid_img_tags.append(f'<img src="{actual_fname}">')
        except Exception as e:
            print(f"[ERROR] Failed to save media file {filename}: {e}")
            failed_count += 1

    if failed_count:
        tooltip(f"Image Search: {failed_count} image(s) failed to download or save.")

    if not valid_img_tags:
        tooltip("Image Search: none of the selected images could be saved.")
        return

    imgs_html = "<br>".join(valid_img_tags)
    if note[insert_field].strip():
        note[insert_field] += "<br>" + imgs_html
    else:
        note[insert_field] = imgs_html

    try:
        mw.col.update_note(note)
        if mw.reviewer:
            mw.reviewer.show()
    except Exception as e:
        tooltip(f"Image Search: failed to save the note ({e}).")
        return

    print("Updated note")
    tooltip(f"Image Search: inserted {len(valid_img_tags)} image(s).")


def download_and_convert_to_jpg(url: str, idx: int):
    """Downloads full res image, converts to JPG at 75% quality. Safe for background threads."""
    try:
        resp = SESSION.get(url, timeout=10)
        if resp.status_code == 200:
            img = QImage.fromData(resp.content)
            if not img.isNull():
                ba = QByteArray()
                buf = QBuffer(ba)
                buf.open(QIODevice.OpenModeFlag.WriteOnly)

                # Convert to JPG with 75% quality
                img.save(buf, "JPG", 75)

                # Generate unique filename for Anki's media folder
                filename = f"imgsearch_{int(time.time() * 1000)}_{idx}.jpg"
                return filename, ba.data()
    except Exception as e:
        print(f"[ERROR] Failed to fetch full res {url}: {e}")
    return None, None


def fetch_image_bytes(url: str):
    try:
        resp = SESSION.get(url, timeout=6)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        print(f"[ERROR] Error fetching {url}: {e}")
    return None