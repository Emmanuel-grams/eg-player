"""Seek bar and transport controls (frame stepping, speed in %, volume, zoom)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QDoubleSpinBox, QHBoxLayout, QLabel, QMenu, QSizePolicy, QSlider,
                               QToolButton, QVBoxLayout, QWidget)

from . import theme
from .project import fmt_timecode

SPEED_PRESETS = [5, 10, 25, 33.33, 50, 75, 90, 100, 110, 125, 150, 175, 200, 300, 400, 800, 1600]


class SeekBar(QWidget):
    seekRequested = Signal(float, bool)   # time, finished(drag released)
    stepRequested = Signal(int)
    scrubStarted = Signal()
    scrubEnded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        self.duration = 0.0
        self.pos = 0.0
        self.fps = 30.0
        self.mark_in: float | None = None
        self.mark_out: float | None = None
        self.ticks: list[float] = []
        self._hover_x: float | None = None
        self._dragging = False
        self.font_ = QFont("Consolas")
        self.font_.setPixelSize(11)

    def set_state(self, pos=None, duration=None):
        if duration is not None:
            self.duration = max(0.0, duration)
        if pos is not None:
            self.pos = pos
        self.update()

    def _bar(self) -> QRectF:
        return QRectF(8, self.height() / 2 - 3, self.width() - 16, 6)

    def _x_to_t(self, x: float) -> float:
        b = self._bar()
        if self.duration <= 0 or b.width() <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - b.left()) / b.width())) * self.duration

    def _t_to_x(self, t: float) -> float:
        b = self._bar()
        if self.duration <= 0:
            return b.left()
        return b.left() + b.width() * max(0.0, min(1.0, t / self.duration))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        b = self._bar()
        path = QPainterPath()
        path.addRoundedRect(b, 3, 3)
        p.fillPath(path, QColor("#34343a"))
        if self.duration > 0:
            if self.mark_in is not None or self.mark_out is not None:
                a = self._t_to_x(self.mark_in or 0.0)
                z = self._t_to_x(self.mark_out if self.mark_out is not None else self.duration)
                p.fillRect(QRectF(a, b.top() - 3, max(1.0, z - a), b.height() + 6), QColor(61, 139, 255, 70))
                p.setPen(QPen(QColor(theme.ACCENT_2), 2))
                if self.mark_in is not None:
                    p.drawLine(QPointF(a, b.top() - 6), QPointF(a, b.bottom() + 6))
                if self.mark_out is not None:
                    p.drawLine(QPointF(z, b.top() - 6), QPointF(z, b.bottom() + 6))
            x = self._t_to_x(self.pos)
            played = QPainterPath()
            played.addRoundedRect(QRectF(b.left(), b.top(), x - b.left(), b.height()), 3, 3)
            p.fillPath(played, QColor(theme.ACCENT))
            if self.ticks:
                p.setPen(QPen(QColor(255, 255, 255, 110), 1))
                for t in self.ticks:
                    tx = self._t_to_x(t)
                    p.drawLine(QPointF(tx, b.top() - 2), QPointF(tx, b.bottom() + 2))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#f4f4f6"))
            p.drawEllipse(QPointF(x, b.center().y()), 7, 7)
            if self._hover_x is not None:
                t = self._x_to_t(self._hover_x)
                txt = fmt_timecode(t, self.fps)
                p.setFont(self.font_)
                fm = QFontMetricsF(self.font_)
                tw = fm.horizontalAdvance(txt) + 10
                hx = max(0.0, min(self.width() - tw, self._hover_x - tw / 2))
                r = QRectF(hx, 0, tw, 13)
                p.setBrush(QColor(10, 10, 12, 220))
                p.drawRoundedRect(r, 3, 3)
                p.setPen(QColor("#e0e0e4"))
                p.drawText(r, Qt.AlignCenter, txt)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration > 0:
            self._dragging = True
            self.scrubStarted.emit()
            self.seekRequested.emit(self._x_to_t(e.position().x()), False)

    def mouseMoveEvent(self, e):
        self._hover_x = e.position().x()
        if self._dragging:
            t = self._x_to_t(e.position().x())
            self.pos = t
            self.seekRequested.emit(t, False)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self.seekRequested.emit(self._x_to_t(e.position().x()), True)
            self.scrubEnded.emit()

    def leaveEvent(self, e):
        self._hover_x = None
        self.update()

    def wheelEvent(self, e):
        steps = e.angleDelta().y()
        if steps:
            self.stepRequested.emit(1 if steps < 0 else -1)


class SpeedBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(1.0, 10000.0)
        self.setDecimals(2)
        self.setSingleStep(5.0)
        self.setSuffix(" %")
        self.setValue(100.0)
        self.setKeyboardTracking(False)
        self.setFixedWidth(92)
        self.setToolTip("Playback speed in percent (1 % – 10000 %).\n"
                        "Type any value, scroll, or use [ ] (±10 %) and Ctrl+[ ] (±1 %). \\ resets.")
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def textFromValue(self, v: float) -> str:
        return f"{v:g}"

    def keyPressEvent(self, e):
        super().keyPressEvent(e)
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.clearFocus()
            w = self.window()
            if hasattr(w, "viewer"):
                w.viewer.setFocus()


class TransportBar(QWidget):
    playToggled = Signal()
    stepRequested = Signal(int)
    goStart = Signal()
    goEnd = Signal()
    speedChanged = Signal(float)       # factor (1.0 = 100 %)
    volumeChanged = Signal(float)
    muteToggled = Signal()
    markIn = Signal()
    markOut = Signal()
    loopToggled = Signal(bool)
    zoomIn = Signal()
    zoomOut = Signal()
    zoomFit = Signal()
    zoom100 = Signal()
    pixelPeepToggled = Signal(bool)
    snapshot = Signal()
    fullscreen = Signal()
    infoToggled = Signal(bool)
    timeFormatToggled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.seek = SeekBar(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 6)
        lay.setSpacing(2)
        lay.addWidget(self.seek)
        row = QHBoxLayout()
        row.setSpacing(2)
        lay.addLayout(row)

        B = theme.icon_button
        self.btn_start = B("prev", "Go to start (Home)")
        self.btn_back = B("step_back", "Previous frame (←)   ·   Shift+← = 1 s back")
        self.btn_play = B("play", "Play / Pause (Space, K)", 18)
        self.btn_fwd = B("step_fwd", "Next frame (→)   ·   Shift+→ = 1 s forward")
        self.btn_end = B("next", "Go to end (End)")
        for b in (self.btn_start, self.btn_back, self.btn_play, self.btn_fwd, self.btn_end):
            row.addWidget(b)
        self.btn_start.clicked.connect(self.goStart)
        self.btn_back.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.btn_play.clicked.connect(self.playToggled)
        self.btn_fwd.clicked.connect(lambda: self.stepRequested.emit(1))
        self.btn_end.clicked.connect(self.goEnd)
        self.btn_back.setAutoRepeat(True)
        self.btn_fwd.setAutoRepeat(True)
        self.btn_back.setAutoRepeatInterval(60)
        self.btn_fwd.setAutoRepeatInterval(60)

        row.addSpacing(8)
        self.lbl_time = QLabel("00:00:00:00 / 00:00:00:00")
        self.lbl_time.setProperty("timecode", True)
        self.lbl_time.setToolTip("Timecode HH:MM:SS:FF — click to toggle frame numbers")
        self.lbl_time.setCursor(Qt.PointingHandCursor)
        self.lbl_time.mousePressEvent = lambda e: self.timeFormatToggled.emit()
        row.addWidget(self.lbl_time)
        row.addSpacing(10)

        self.btn_in = QToolButton(text="[ In")
        self.btn_in.setToolTip("Mark In (I)  ·  Alt+I clears")
        self.btn_out = QToolButton(text="Out ]")
        self.btn_out.setToolTip("Mark Out (O)  ·  Alt+O clears")
        self.btn_loop = B("loop", "Loop between In and Out (Ctrl+L)", checkable=True)
        for b in (self.btn_in, self.btn_out):
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        row.addWidget(self.btn_loop)
        self.btn_in.clicked.connect(self.markIn)
        self.btn_out.clicked.connect(self.markOut)
        self.btn_loop.toggled.connect(self.loopToggled)

        row.addStretch(1)

        lbl = QLabel("Speed")
        lbl.setProperty("dim", True)
        row.addWidget(lbl)
        self.speed = SpeedBox()
        self.speed.valueChanged.connect(lambda v: self.speedChanged.emit(v / 100.0))
        row.addWidget(self.speed)
        self.btn_speed_menu = QToolButton(text="▾")
        self.btn_speed_menu.setFocusPolicy(Qt.NoFocus)
        self.btn_speed_menu.setToolTip("Speed presets")
        self.btn_speed_menu.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self.btn_speed_menu)
        for pct in SPEED_PRESETS:
            a = m.addAction(f"{pct:g} %")
            a.triggered.connect(lambda _=False, v=pct: self.speed.setValue(v))
        self.btn_speed_menu.setMenu(m)
        row.addWidget(self.btn_speed_menu)
        row.addSpacing(10)

        self.btn_mute = B("volume", "Mute (M)")
        self.btn_mute.clicked.connect(self.muteToggled)
        row.addWidget(self.btn_mute)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 200)
        self.vol.setValue(100)
        self.vol.setFixedWidth(90)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setToolTip("Volume (↑/↓ in player mode)")
        self.vol.valueChanged.connect(lambda v: self.volumeChanged.emit(float(v)))
        row.addWidget(self.vol)
        row.addSpacing(10)

        self.btn_zoom_out = B("zoom_out", "Zoom out (-)  ·  mouse wheel over the video")
        self.lbl_zoom = QLabel("Fit")
        self.lbl_zoom.setProperty("dim", True)
        self.lbl_zoom.setMinimumWidth(44)
        self.lbl_zoom.setAlignment(Qt.AlignCenter)
        self.btn_zoom_in = B("zoom_in", "Zoom in (+)  ·  mouse wheel over the video")
        self.btn_fit = B("fit", "Fit to window (0 or middle-click)")
        self.btn_100 = QToolButton(text="1:1")
        self.btn_100.setFocusPolicy(Qt.NoFocus)
        self.btn_100.setToolTip("Actual pixels — 1 video pixel = 1 screen pixel (1)")
        self.btn_peep = B("grid", "Pixel Peep (P): nearest-neighbour zoom, pixel grid and RGB read-out",
                          checkable=True)
        for w in (self.btn_zoom_out, self.lbl_zoom, self.btn_zoom_in, self.btn_fit, self.btn_100, self.btn_peep):
            row.addWidget(w)
        self.btn_zoom_out.clicked.connect(self.zoomOut)
        self.btn_zoom_in.clicked.connect(self.zoomIn)
        self.btn_fit.clicked.connect(self.zoomFit)
        self.btn_100.clicked.connect(self.zoom100)
        self.btn_peep.toggled.connect(self.pixelPeepToggled)
        row.addSpacing(8)

        self.btn_snap = B("camera", "Save current frame as lossless PNG (Ctrl+Shift+S)")
        self.btn_info = B("info", "Media info (Ctrl+I)", checkable=True)
        self.btn_full = B("fullscreen", "Fullscreen (F / double-click)")
        for b in (self.btn_snap, self.btn_info, self.btn_full):
            row.addWidget(b)
        self.btn_snap.clicked.connect(self.snapshot)
        self.btn_info.toggled.connect(self.infoToggled)
        self.btn_full.clicked.connect(self.fullscreen)

    def set_playing(self, playing: bool):
        theme.set_button_icon(self.btn_play, "pause" if playing else "play")

    def set_muted(self, muted: bool):
        theme.set_button_icon(self.btn_mute, "mute" if muted else "volume")

    def set_speed_pct(self, pct: float):
        if abs(self.speed.value() - pct) > 1e-6:
            self.speed.blockSignals(True)
            self.speed.setValue(pct)
            self.speed.blockSignals(False)

    def set_volume(self, v: float):
        if self.vol.value() != int(round(v)):
            self.vol.blockSignals(True)
            self.vol.setValue(int(round(v)))
            self.vol.blockSignals(False)
