"""Application bootstrap."""
from __future__ import annotations

import ctypes
import locale
import os
import sys
import traceback

from . import paths

paths.setup_dll_search_path()

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap,  # noqa: E402
                           QPolygonF, QSurfaceFormat)
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from . import theme  # noqa: E402


def draw_icon(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    r = QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor("#ff9a4d"))
    g.setColorAt(1, QColor("#e8432f"))
    path = QPainterPath()
    path.addRoundedRect(r, s * 0.22, s * 0.22)
    p.fillPath(path, g)
    # film perforations
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 70))
    for i in range(4):
        y = r.top() + s * (0.16 + i * 0.2)
        p.drawRoundedRect(QRectF(r.left() + s * 0.07, y, s * 0.08, s * 0.1), s * 0.02, s * 0.02)
        p.drawRoundedRect(QRectF(r.right() - s * 0.15, y, s * 0.08, s * 0.1), s * 0.02, s * 0.02)
    tri = QPolygonF([QPointF(s * 0.38, s * 0.28), QPointF(s * 0.74, s * 0.5), QPointF(s * 0.38, s * 0.72)])
    p.setBrush(QColor("#ffffff"))
    p.drawPolygon(tri)
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(draw_icon(sz))
    return icon


def _excepthook(exc_type, exc, tb):
    msg = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        log = paths.cache_dir().parent / "error.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass
    sys.__stderr__ and sys.__stderr__.write(msg)
    app = QApplication.instance()
    if app is not None:
        QMessageBox.critical(None, paths.APP_NAME, f"Unexpected error:\n\n{exc}\n\nDetails were written to error.log")


def main() -> int:
    sys.excepthook = _excepthook
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EGPlayer.EGPlayer")
    except Exception:
        pass
    QCoreApplication.setAttribute(Qt.AA_UseDesktopOpenGL)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    fmt = QSurfaceFormat()
    fmt.setSwapInterval(1)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(sys.argv)
    app.setApplicationName(paths.APP_NAME)
    app.setOrganizationName(paths.APP_ID)
    app.setApplicationVersion(paths.APP_VERSION)
    app.setWindowIcon(app_icon())
    theme.apply_theme(app)
    locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv requirement

    if not (paths.bin_dir() / "libmpv-2.dll").exists():
        QMessageBox.critical(None, paths.APP_NAME,
                             f"libmpv-2.dll was not found in:\n{paths.bin_dir()}\n\nRun setup.ps1 first.")
        return 1

    from .mainwindow import MainWindow

    files = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("-")]
    w = MainWindow(files)
    w.show()
    return app.exec()
