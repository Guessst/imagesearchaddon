import sys
import os
import time
import concurrent.futures

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
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QGridLayout, QPushButton, QVBoxLayout, QDialogButtonBox
from PyQt6.QtGui import QPixmap, QIcon, QImage
from PyQt6.QtCore import QSize, QByteArray, QBuffer, QIODevice

SESSION = requests.Session()

def now_ms():
    return time.perf_counter() * 1000

def custom_shortcuts(state: str, shortcuts: list[tuple[str, callable]]):
    if state == "review":
        shortcuts.append(("Ctrl+H", make_search))

gui_hooks.state_shortcuts_will_change.append(custom_shortcuts)

def make_search(query="capybara"):
    print("Entered: make_search")

    note = None
    if mw.reviewer and mw.reviewer.card:
        card = mw.reviewer.card
        note = card.note()
        if len(note.fields) > 0:
            query = note.fields[0]
    
    t_start = now_ms()
    results = list(DDGS().images(query, max_results=8))
    t_end = now_ms()
    print(f"Elapsed ms: {(t_end - t_start):.0f}")
    
    pairs = [
        (
            r.get("thumbnail"),
            r.get("image"),
        )
        for r in results
    ]

    dialog = QDialog()
    dialog.setWindowTitle("Select Images")
    dialog.resize(650, 380)

    layout = QVBoxLayout(dialog)
    grid = QGridLayout()
    layout.addLayout(grid)

    t_fetch_start = now_ms()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        thumb_urls = [p[0] for p in pairs]
        image_bytes_list = list(executor.map(fetch_image_bytes, thumb_urls))
    print(f"All thumbnails downloaded in {(now_ms() - t_fetch_start):.0f}ms")

    buttons = []
    for i, (img_bytes, (thumb_url, full_url)) in enumerate(zip(image_bytes_list, pairs)):
        if not img_bytes:
            continue

        pixmap = QPixmap()
        pixmap.loadFromData(img_bytes)

        btn = QPushButton()
        btn.setIcon(QIcon(pixmap))
        btn.setIconSize(QSize(130, 130))
        btn.setCheckable(True)
        btn.setStyleSheet("QPushButton:checked { border: 3px solid #4CAF50; background: #C8E6C9; }")
        btn.setProperty("full_url", full_url)

        grid.addWidget(btn, i // 4, i % 4)
        buttons.append(btn)

    button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    if dialog.exec():
        selected_urls = [b.property("full_url") for b in buttons if b.isChecked()]
        
        # Process selected images if we have an active Anki note with at least 2 fields
        if selected_urls and note and len(note.fields) > 1:
            print(f"Downloading {len(selected_urls)} full resolution images...")
            t_processing_start = now_ms()
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected_urls)) as executor:
                futures = [executor.submit(download_and_convert_to_jpg, url, i) for i, url in enumerate(selected_urls)]
                converted_images = [f.result() for f in futures]
            
            valid_img_tags = []
            for filename, data in converted_images:
                if filename and data:
                    # Save image bytes directly to Anki's media folder
                    actual_fname = mw.col.media.write_data(filename, data)
                    valid_img_tags.append(f'<img src="{actual_fname}">')

            print(f"Done processing in {(now_ms() - t_processing_start):.0f}")
            if valid_img_tags:
                imgs_html = "<br>".join(valid_img_tags)
                
                # Prepend a linebreak if field[1] already contains text
                if note.fields[1].strip():
                    note.fields[1] += "<br>" + imgs_html
                else:
                    note.fields[1] = imgs_html
                
                # Save changes to the Anki database and refresh the current view
                mw.col.update_note(note)
                if mw.reviewer:
                    mw.reviewer.show()

                print("Done processing.")

        return selected_urls
    return []

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

def main():
    app = QApplication(sys.argv)
    while True:
        try:
            query = input("Type your query ('quit' or 'exit' to quit): ").strip()
            if query.lower() in ['quit', 'exit', 'q']:
                print("Exiting...")
                app.exit(0)
                os._exit(0)
            
            make_search(query)

        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            app.exit(0)
            os._exit(0)

if __name__ == "__main__":
    main()