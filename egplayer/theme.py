"""Dark, Resolve-inspired look: Fusion palette, stylesheet and icon-font helpers."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QToolButton

BG = "#151517"
PANEL = "#1d1d20"
PANEL_2 = "#242428"
BORDER = "#2f2f34"
TEXT = "#d8d8dc"
TEXT_DIM = "#8b8b93"
ACCENT = "#ff7a3d"
ACCENT_2 = "#3d8bff"
PLAYHEAD = "#ff4d3d"
VIDEO_CLIP = "#3b5f9a"
AUDIO_CLIP = "#2f7d56"

# Segoe Fluent Icons (Win 11) / Segoe MDL2 Assets (Win 10) share these code points.
ICONS = {
    "play": "", "pause": "", "prev": "", "next": "",
    "step_back": "", "step_fwd": "", "volume": "", "mute": "",
    "fullscreen": "", "unfullscreen": "", "zoom_in": "", "zoom_out": "",
    "camera": "", "cut": "", "delete": "", "undo": "", "redo": "",
    "export": "", "save": "", "open": "", "folder": "", "loop": "",
    "fit": "", "info": "", "add": "", "edit": "", "settings": "",
    "video": "", "grid": "", "pixel": "", "append": "", "insert": "",
    "speed": "", "mark_in": "", "mark_out": "", "playlist": "",
}

_icon_family: list[str] | None = None


def icon_families() -> list[str]:
    global _icon_family
    if _icon_family is None:
        fams = set(QFontDatabase.families())
        _icon_family = [f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams]
    return _icon_family


def icon_font(size: int = 12) -> QFont:
    f = QFont()
    fams = icon_families()
    if fams:
        f.setFamilies(fams)
    f.setPixelSize(size)
    return f


def icon_button(name: str, tooltip: str = "", size: int = 15, checkable: bool = False,
                text_fallback: str = "") -> QToolButton:
    b = QToolButton()
    if icon_families():
        b.setText(ICONS.get(name, "?"))
        b.setFont(icon_font(size))
    else:
        b.setText(text_fallback or name)
    b.setToolTip(tooltip)
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    b.setFocusPolicy(b.focusPolicy().NoFocus)
    b.setProperty("iconbtn", True)
    return b


def set_button_icon(b: QToolButton, name: str) -> None:
    if icon_families():
        b.setText(ICONS.get(name, "?"))


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.Window, c(BG))
    pal.setColor(QPalette.WindowText, c(TEXT))
    pal.setColor(QPalette.Base, c("#111113"))
    pal.setColor(QPalette.AlternateBase, c(PANEL))
    pal.setColor(QPalette.ToolTipBase, c(PANEL_2))
    pal.setColor(QPalette.ToolTipText, c(TEXT))
    pal.setColor(QPalette.Text, c(TEXT))
    pal.setColor(QPalette.Button, c(PANEL_2))
    pal.setColor(QPalette.ButtonText, c(TEXT))
    pal.setColor(QPalette.BrightText, c("#ffffff"))
    pal.setColor(QPalette.Highlight, c(ACCENT))
    pal.setColor(QPalette.HighlightedText, c("#111111"))
    pal.setColor(QPalette.Link, c(ACCENT_2))
    pal.setColor(QPalette.PlaceholderText, c(TEXT_DIM))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c("#5a5a60"))
    app.setPalette(pal)
    f = app.font()
    f.setFamily("Segoe UI")
    f.setPointSizeF(9.0)
    app.setFont(f)
    app.setStyleSheet(STYLESHEET)


STYLESHEET = f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenuBar {{ background: {BG}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {PANEL_2}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 26px 5px 22px; border-radius: 3px; }}
QMenu::item:selected {{ background: {ACCENT}; color: #111; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QToolButton {{ border: none; border-radius: 4px; padding: 4px 6px; background: transparent; }}
QToolButton:hover {{ background: {PANEL_2}; }}
QToolButton:pressed {{ background: #34343a; }}
QToolButton:checked {{ background: #3a2a22; color: {ACCENT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 4px; padding: 5px 14px; }}
QPushButton:hover {{ border-color: #4a4a52; }}
QPushButton:pressed {{ background: #2e2e34; }}
QPushButton:default, QPushButton[accent="true"] {{ background: {ACCENT}; color: #141414; border: 1px solid {ACCENT}; font-weight: 600; }}
QPushButton:disabled {{ color: #5a5a60; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: #111113; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 6px;
    selection-background-color: {ACCENT}; selection-color: #111;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{ width: 14px; }}
QSlider::groove:horizontal {{ height: 4px; background: #34343a; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #e6e6ea; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 14px; padding: 8px 6px 6px 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_DIM}; }}
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}
QScrollBar:horizontal {{ height: 10px; background: transparent; }}
QScrollBar:vertical {{ width: 10px; background: transparent; }}
QScrollBar::handle {{ background: #3a3a40; border-radius: 4px; min-width: 24px; min-height: 24px; }}
QScrollBar::handle:hover {{ background: #4a4a52; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QListWidget {{ background: #111113; border: none; outline: none; }}
QListWidget::item {{ border-radius: 4px; padding: 4px; color: {TEXT}; }}
QListWidget::item:selected {{ background: #3a2a22; border: 1px solid {ACCENT}; }}
QListWidget::item:hover {{ background: {PANEL_2}; }}
QProgressBar {{ background: #111113; border: 1px solid {BORDER}; border-radius: 4px; text-align: center; height: 18px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QStatusBar {{ background: {BG}; color: {TEXT_DIM}; border-top: 1px solid {BORDER}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #55555c; border-radius: 3px; background: #111113; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QLabel[panelTitle="true"] {{ color: {TEXT_DIM}; font-weight: 600; letter-spacing: 1px; padding: 6px 8px; }}
QFrame[panel="true"] {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel[timecode="true"] {{ font-family: Consolas, 'Cascadia Mono', monospace; font-size: 13px; color: #f0f0f0; }}
QLabel[dim="true"] {{ color: {TEXT_DIM}; }}
"""
