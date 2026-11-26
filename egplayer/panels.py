"""Media Pool and clip Inspector panels."""
from __future__ import annotations

import os

from PySide6.QtCore import QMimeData, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QScrollArea, QSlider, QSpinBox, QToolButton, QVBoxLayout,
                               QWidget)

from . import theme
from .media import MediaInfo
from .project import COLOR_KEYS, Clip, fmt_seconds, fmt_timecode
from .timeline import MIME_MEDIA

THUMB = QSize(160, 90)


def _placeholder(glyph: str) -> QPixmap:
    pm = QPixmap(THUMB)
    pm.fill(QColor("#232327"))
    p = QPainter(pm)
    p.setPen(QColor("#55555d"))
    p.setFont(theme.icon_font(30) if theme.icon_families() else QFont("Segoe UI", 20))
    p.drawText(pm.rect(), Qt.AlignCenter, glyph)
    p.end()
    return pm


class MediaPoolList(QListWidget):
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(THUMB)
        self.setGridSize(QSize(THUMB.width() + 16, THUMB.height() + 46))
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setFocusPolicy(Qt.ClickFocus)

    def mimeTypes(self):
        return ["text/uri-list", MIME_MEDIA]

    def mimeData(self, items):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(it.data(Qt.UserRole)) for it in items])
        md.setData(MIME_MEDIA, b"1")
        return md

    def supportedDropActions(self):
        return Qt.CopyAction

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not e.mimeData().hasFormat(MIME_MEDIA):
            e.acceptProposedAction()
        elif e.source() is self:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() and not e.mimeData().hasFormat(MIME_MEDIA):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        if e.mimeData().hasFormat(MIME_MEDIA):
            e.ignore()
            return
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.filesDropped.emit(files)
            e.acceptProposedAction()


class MediaPool(QWidget):
    openRequested = Signal(str)
    appendRequested = Signal(list)
    importRequested = Signal()
    filesDropped = Signal(list)
    removed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        t = QLabel("MEDIA POOL")
        t.setProperty("panelTitle", True)
        bar.addWidget(t)
        bar.addStretch(1)
        self.btn_import = theme.icon_button("add", "Import media… (Ctrl+Shift+I)")
        bar.addWidget(self.btn_import)
        lay.addLayout(bar)
        self.list = MediaPoolList(self)
        lay.addWidget(self.list, 1)
        self.btn_import.clicked.connect(self.importRequested)
        self.list.itemDoubleClicked.connect(lambda it: self.openRequested.emit(it.data(Qt.UserRole)))
        self.list.filesDropped.connect(self.filesDropped)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self._items: dict[str, QListWidgetItem] = {}
        self._ph_video = _placeholder(theme.ICONS["video"] if theme.icon_families() else "▶")
        self._ph_audio = _placeholder("" if theme.icon_families() else "♪")
        self.hint = QLabel("Drop videos here\nor click + to import", self.list)
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setProperty("dim", True)
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.hint.setGeometry(self.list.rect())

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def paths(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def add(self, path: str) -> bool:
        k = self._key(path)
        if k in self._items:
            return False
        it = QListWidgetItem(QIcon(self._ph_video), os.path.basename(path))
        it.setData(Qt.UserRole, path)
        it.setToolTip(path)
        it.setSizeHint(QSize(THUMB.width() + 12, THUMB.height() + 42))
        it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.list.addItem(it)
        self._items[k] = it
        self.hint.setVisible(False)
        return True

    def update_info(self, path: str, info: MediaInfo):
        it = self._items.get(self._key(path))
        if it is None:
            return
        if not info.ok:
            it.setText(f"{info.name}\n⚠ {info.error[:40]}")
            it.setForeground(QColor("#e07070"))
            return
        meta = fmt_seconds(info.duration)
        if info.has_video:
            meta += f" · {info.width}×{info.height}"
        else:
            it.setIcon(QIcon(self._ph_audio))
        it.setText(f"{info.name}\n{meta}")
        tip = [info.path, f"Duration {fmt_seconds(info.duration)}"]
        if info.has_video:
            tip.append(f"Video {info.vcodec} {info.width}×{info.height} @ {info.fps:.3f} fps, {info.pix_fmt}")
        if info.has_audio:
            tip.append(f"Audio {info.acodec} {info.sample_rate} Hz, {info.channels} ch")
        it.setToolTip("\n".join(tip))

    def set_thumb(self, path: str, thumb: str):
        it = self._items.get(self._key(path))
        if it is None:
            return
        pm = QPixmap(thumb)
        if pm.isNull():
            return
        canvas = QPixmap(THUMB)
        canvas.fill(QColor("#0d0d0f"))
        p = QPainter(canvas)
        sc = pm.scaled(THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap((THUMB.width() - sc.width()) // 2, (THUMB.height() - sc.height()) // 2, sc)
        p.end()
        it.setIcon(QIcon(canvas))

    def selected_paths(self) -> list[str]:
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    def remove(self, paths: list[str]):
        for pth in paths:
            it = self._items.pop(self._key(pth), None)
            if it is not None:
                self.list.takeItem(self.list.row(it))
        self.hint.setVisible(self.list.count() == 0)

    def clear(self):
        self.list.clear()
        self._items.clear()
        self.hint.setVisible(True)

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        m = QMenu(self)
        if it is not None:
            sel = self.selected_paths() or [it.data(Qt.UserRole)]
            m.addAction("Open in viewer", lambda: self.openRequested.emit(it.data(Qt.UserRole)))
            m.addAction("Append to timeline", lambda: self.appendRequested.emit(sel))
            m.addSeparator()
            m.addAction("Show in Explorer", lambda: os.startfile(os.path.dirname(it.data(Qt.UserRole))))
            m.addAction("Remove from pool", lambda: (self.remove(sel), self.removed.emit(sel)))
            m.addSeparator()
        m.addAction("Import media…", self.importRequested)
        m.exec(self.list.mapToGlobal(pos))


class _ColorRow(QWidget):
    changed = Signal(str, float)

    def __init__(self, key: str, label: str, parent=None):
        super().__init__(parent)
        self.key = key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setFixedWidth(70)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-100, 100)
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.spin = QSpinBox()
        self.spin.setRange(-100, 100)
        self.spin.setFixedWidth(58)
        self.spin.setKeyboardTracking(False)
        rst = QToolButton(text="↺")
        rst.setToolTip("Reset")
        rst.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(lab)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        lay.addWidget(rst)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        rst.clicked.connect(lambda: self.spin.setValue(0))
        self.slider.mouseDoubleClickEvent = lambda e: self.spin.setValue(0)

    def _from_slider(self, v):
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self.changed.emit(self.key, float(v))

    def _from_spin(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self.changed.emit(self.key, float(v))

    def set_value(self, v: float):
        for w in (self.slider, self.spin):
            w.blockSignals(True)
            w.setValue(int(round(v)))
            w.blockSignals(False)


class Inspector(QWidget):
    """Edits the selected timeline clip: speed, volume, fades and colour correction."""

    changed = Signal(int, str, float)   # uid, field, value
    resetRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.uid: int | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        t = QLabel("INSPECTOR")
        t.setProperty("panelTitle", True)
        outer.addWidget(t)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(10, 4, 10, 10)
        lay.setSpacing(8)

        self.empty = QLabel("Select a clip on the timeline to adjust\nspeed, volume, fades and colour.")
        self.empty.setProperty("dim", True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)

        self.box = QWidget()
        bl = QVBoxLayout(self.box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        self.lbl_name = QLabel()
        self.lbl_name.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.lbl_name.setWordWrap(True)
        self.lbl_range = QLabel()
        self.lbl_range.setProperty("dim", True)
        self.lbl_range.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(self.lbl_name)
        bl.addWidget(self.lbl_range)

        g1 = QGroupBox("Clip")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignRight)
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(1.0, 10000.0)
        self.sp_speed.setDecimals(2)
        self.sp_speed.setSingleStep(5)
        self.sp_speed.setSuffix(" %")
        self.sp_speed.setKeyboardTracking(False)
        self.sp_speed.setToolTip("Clip speed — any percentage. Changes the clip's length on the timeline.")
        self.sp_vol = QSpinBox()
        self.sp_vol.setRange(0, 400)
        self.sp_vol.setSuffix(" %")
        self.sp_vol.setKeyboardTracking(False)
        self.sp_fin = QDoubleSpinBox()
        self.sp_fin.setRange(0, 60)
        self.sp_fin.setDecimals(2)
        self.sp_fin.setSingleStep(0.25)
        self.sp_fin.setSuffix(" s")
        self.sp_fin.setKeyboardTracking(False)
        self.sp_fout = QDoubleSpinBox()
        self.sp_fout.setRange(0, 60)
        self.sp_fout.setDecimals(2)
        self.sp_fout.setSingleStep(0.25)
        self.sp_fout.setSuffix(" s")
        self.sp_fout.setKeyboardTracking(False)
        f1.addRow("Speed", self.sp_speed)
        f1.addRow("Volume", self.sp_vol)
        f1.addRow("Fade in", self.sp_fin)
        f1.addRow("Fade out", self.sp_fout)
        note = QLabel("Fades are rendered on export (to/from black + audio).")
        note.setProperty("dim", True)
        note.setWordWrap(True)
        f1.addRow(note)
        bl.addWidget(g1)

        g2 = QGroupBox("Color")
        gl = QVBoxLayout(g2)
        self.rows: dict[str, _ColorRow] = {}
        for key, label in zip(COLOR_KEYS, ("Brightness", "Contrast", "Saturation", "Gamma", "Hue")):
            r = _ColorRow(key, label)
            r.changed.connect(self._emit)
            gl.addWidget(r)
            self.rows[key] = r
        bl.addWidget(g2)

        self.btn_reset = QPushButton("Reset all clip effects")
        bl.addWidget(self.btn_reset)
        bl.addStretch(1)
        lay.addWidget(self.box)
        lay.addStretch(1)
        self.box.setVisible(False)

        self.sp_speed.valueChanged.connect(lambda v: self._emit("speed", v / 100.0))
        self.sp_vol.valueChanged.connect(lambda v: self._emit("volume", v / 100.0))
        self.sp_fin.valueChanged.connect(lambda v: self._emit("fade_in", v))
        self.sp_fout.valueChanged.connect(lambda v: self._emit("fade_out", v))
        self.btn_reset.clicked.connect(lambda: self.uid is not None and self.resetRequested.emit(self.uid))

    def _emit(self, key, value):
        if self.uid is not None:
            self.changed.emit(self.uid, key, float(value))

    def show_clip(self, clip: Clip | None, fps: float = 30.0):
        self.uid = clip.uid if clip else None
        self.empty.setVisible(clip is None)
        self.box.setVisible(clip is not None)
        if clip is None:
            return
        self.lbl_name.setText(clip.name)
        self.lbl_range.setText(f"Source  {fmt_timecode(clip.src_in, fps)} → {fmt_timecode(clip.src_out, fps)}\n"
                               f"Length  {fmt_timecode(clip.duration, fps)} on timeline")
        for w, v in ((self.sp_speed, clip.speed * 100), (self.sp_vol, clip.volume * 100),
                     (self.sp_fin, clip.fade_in), (self.sp_fout, clip.fade_out)):
            w.blockSignals(True)
            w.setValue(int(round(v)) if isinstance(w, QSpinBox) else v)
            w.blockSignals(False)
        for k, r in self.rows.items():
            r.set_value(getattr(clip, k))
