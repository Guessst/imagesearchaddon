import sys
import os
import time

dep_dir_nam= "vendor"
sys.path.append(
    os.path.join(
        os.path.dirname( os.path.abspath(__file__) ),
        dep_dir_nam
    )
)

import requests
from ddgs import DDGS
from aqt import mw
from aqt import gui_hooks
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QGridLayout, QPushButton, QVBoxLayout, QDialogButtonBox
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import QSize

SESSION = requests.Session()

def now_ms():
    return time.perf_counter() * 1000

def custom_shortcuts(state: str, shortcuts: list[tuple[str, callable]]):
    if state == "review":
        shortcuts.append(("Ctrl+H", make_search))

gui_hooks.state_shortcuts_will_change.append(custom_shortcuts)

def make_search(query="capybara"):
    print("Entered: make_search")

    if mw.reviewer and mw.reviewer.card:
        card = mw.reviewer.card
        note = card.note()
        print("note.fields:", note.fields)
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

    buttons = []
    for i, (thumb_url, full_url) in enumerate(pairs):
        pixmap = make_thumbnail(thumb_url)
        if not pixmap:
            continue

        btn = QPushButton()
        btn.setIcon(QIcon(pixmap))
        btn.setIconSize(QSize(130, 130))
        btn.setCheckable(True)
        btn.setStyleSheet("QPushButton:checked { border: 3px solid #4CAF50; background: #C8E6C9; }")
        btn.setProperty("full_url", full_url)

        grid.addWidget(btn, i // 4, i % 4)  # 2 rows x 4 columns
        buttons.append(btn)

    # OK / Cancel confirmation
    button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    if dialog.exec():
        # Return full image URLs of checked thumbnails
        return [b.property("full_url") for b in buttons if b.isChecked()]
    return []

def make_thumbnail(url: str):
    t_start = now_ms()
    try:
        resp = SESSION.get(url, timeout=6)
        if resp.status_code == 200:
            t_end = now_ms()
            elapsed = t_end - t_start
            print(f"Elapsed {elapsed}ms")
            
            # Load raw image bytes directly into a QPixmap
            pixmap = QPixmap()
            pixmap.loadFromData(resp.content)
            return pixmap
        else:
            print(f"Unexpected Status code: {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] Error: {e}")
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