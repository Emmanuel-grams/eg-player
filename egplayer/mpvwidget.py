"""libmpv rendered into a QOpenGLWidget, with zoom/pan, pixel inspection and a HUD overlay."""
from __future__ import annotations

import ctypes
import math
import time

import mpv
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QWidget

# ------------------------------------------------------------------------------------------------
# OpenGL function loader for mpv (WGL first, then the GL 1.1 exports of opengl32.dll)
_opengl32 = ctypes.WinDLL("opengl32")
_wglGetProcAddress = _opengl32.wglGetProcAddress
_wglGetProcAddress.argtypes = [ctypes.c_char_p]
_wglGetProcAddress.restype = ctypes.c_void_p
_kernel32 = ctypes.WinDLL("kernel32")
_GetProcAddress = _kernel32.GetProcAddress
_GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
_GetProcAddress.restype = ctypes.c_void_p
_BAD = {1, 2, 3, ctypes.c_void_p(-1).value}


def _get_proc_address(_ctx, name):
    addr = _wglGetProcAddress(name)
    if not addr or addr in _BAD:
        addr = _GetProcAddress(_opengl32._handle, name)
    return addr or 0


QUALITY = {
    # 'high' keeps detail: sharp EWA upscaling, gamma-correct downscaling, no debanding/denoise.
    "high": {"scale": "ewa_lanczossharp", "cscale": "spline36", "dscale": "mitchell",
             "correct-downscaling": "yes", "linear-downscaling": "yes", "sigmoid-upscaling": "yes"},
    "balanced": {"scale": "spline36", "cscale": "spline36", "dscale": "mitchell",
                 "correct-downscaling": "yes", "linear-downscaling": "no", "sigmoid-upscaling": "no"},
    "fast": {"scale": "bilinear", "cscale": "bilinear", "dscale": "bilinear",
             "correct-downscaling": "no", "linear-downscaling": "no", "sigmoid-upscaling": "no"},
}

MIN_ZOOM_PCT = 5.0
MAX_ZOOM_PCT = 6400.0


class MpvWidget(QOpenGLWidget):
    # internal cross-thread signals
    _sigRender = Signal()
    _sigProp = Signal(str, object)
    _sigEvent = Signal(int, object)

    timeChanged = Signal(float)
    durationChanged = Signal(float)
    pauseChanged = Signal(bool)
    speedChanged = Signal(float)
    volumeChanged = Signal(float)
    muteChanged = Signal(bool)
    eofChanged = Signal(bool)
    fileLoaded = Signal(str)
    fileEnded = Signal(int, str)
    videoParamsChanged = Signal()
    seekFinished = Signal()
    viewChanged = Signal()
    doubleClicked = Signal()
    clicked = Signal()
    contextMenuRequested = Signal(QPoint)
    filesDropped = Signal(list)
    jogRequested = Signal(int)
    mouseActivity = Signal()

    def __init__(self, parent=None, hwdec: str = "auto-safe", quality: str = "high"):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 180)
        self.ctx = None
        self._pending_load = None
        self._closed = False
        self.last_error = ""
        self.quality = quality if quality in QUALITY else "high"
        opts = dict(
            vo="libmpv",
            hwdec=hwdec,
            keep_open="yes",
            idle="yes",
            osc="no",
            osd_level="0",
            input_default_bindings="no",
            input_vo_keyboard="no",
            hr_seek="yes",
            hr_seek_framedrop="no",
            cache="yes",
            demuxer_max_bytes="512MiB",
            demuxer_max_back_bytes="256MiB",
            deband="no",
            dither_depth="auto",
            screenshot_format="png",
            screenshot_png_compression="6",
            screenshot_high_bit_depth="yes",
            screenshot_tag_colorspace="yes",
            audio_pitch_correction="yes",
            volume_max="200",
            image_display_duration="inf",
            sub_auto="fuzzy",
        )
        for k, v in QUALITY[self.quality].items():
            opts[k.replace("-", "_")] = v
        self.m = mpv.MPV(log_handler=self._on_log, loglevel="warn", **opts)
        self._proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc_address)

        # state mirrored from mpv
        self.time_pos = 0.0
        self.duration = 0.0
        self.paused = True
        self.speed = 1.0
        self.volume = 100.0
        self.muted = False
        self.eof = False
        self.fps = 0.0
        self.path = ""
        self.vparams: dict = {}
        self.voparams: dict = {}
        self.hwdec_current = ""

        # view state
        self.zoom_log2 = 0.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.pixel_peep = False
        self._scaler_override = None
        self.click_to_pause = True

        # seeking (coalesced so scrubbing always lands on real, exact frames)
        self._seek_busy = False
        self._seek_pending: tuple[float, bool] | None = None
        self._seek_guard = QTimer(self, singleShot=True, interval=350, timeout=self._seek_unstick)

        # mouse
        self._press_btn = None
        self._press_pos = QPointF()
        self._last_pos = QPointF()
        self._moved = False
        self._jog_accum = 0.0
        self._click_timer = QTimer(self, singleShot=True, timeout=self.clicked.emit)
        self.hover_pos: QPointF | None = None

        # frame cache for pixel read-out
        self._frame = None  # (time, w, h, stride, bytes)

        self._sigRender.connect(self._on_render_request, Qt.QueuedConnection)
        self._sigProp.connect(self._on_prop, Qt.QueuedConnection)
        self._sigEvent.connect(self._on_event, Qt.QueuedConnection)
        for name in ("time-pos", "duration", "pause", "speed", "volume", "mute", "eof-reached",
                     "video-params", "video-out-params", "container-fps", "estimated-vf-fps",
                     "path", "hwdec-current"):
            self.m.observe_property(name, self._observer)
        self.m.register_event_callback(self._mpv_event)

        self.overlay = ViewerOverlay(self)

    # ------------------------------------------------------------------ mpv thread callbacks
    def _observer(self, name, value):
        if not self._closed:
            self._sigProp.emit(name, value)

    def _mpv_event(self, event):
        if self._closed:
            return
        eid = event.event_id.value
        if eid == mpv.MpvEventID.END_FILE:
            d = event.data
            self._sigEvent.emit(eid, (d.reason, d.error))
        elif eid in (mpv.MpvEventID.FILE_LOADED, mpv.MpvEventID.PLAYBACK_RESTART,
                     mpv.MpvEventID.VIDEO_RECONFIG):
            self._sigEvent.emit(eid, None)

    def _on_log(self, level, prefix, text):
        if level in ("error", "fatal"):
            self.last_error = f"{prefix}: {text.strip()}"

    # ------------------------------------------------------------------ GUI-thread dispatch
    def _on_prop(self, name, value):
        if name == "time-pos":
            if value is not None:
                self.time_pos = float(value)
                self.timeChanged.emit(self.time_pos)
                if self.paused and self.pixel_peep:
                    self.overlay.update()
        elif name == "duration":
            self.duration = float(value or 0.0)
            self.durationChanged.emit(self.duration)
        elif name == "pause":
            self.paused = bool(value)
            self.pauseChanged.emit(self.paused)
        elif name == "speed":
            self.speed = float(value or 1.0)
            self.speedChanged.emit(self.speed)
        elif name == "volume":
            self.volume = float(value or 0.0)
            self.volumeChanged.emit(self.volume)
        elif name == "mute":
            self.muted = bool(value)
            self.muteChanged.emit(self.muted)
        elif name == "eof-reached":
            self.eof = bool(value)
            self.eofChanged.emit(self.eof)
        elif name in ("container-fps", "estimated-vf-fps"):
            if name == "container-fps" and value:
                self.fps = float(value)
            elif name == "estimated-vf-fps" and value and not self.fps:
                self.fps = float(value)
            self.videoParamsChanged.emit()
        elif name == "video-params":
            self.vparams = dict(value) if value else {}
            self._frame = None
            self.videoParamsChanged.emit()
            self._apply_view()
        elif name == "video-out-params":
            self.voparams = dict(value) if value else {}
            self.videoParamsChanged.emit()
            self._apply_view()
        elif name == "path":
            if value:  # ignore the transient None between files; load()/stop() manage the rest
                self.path = value
        elif name == "hwdec-current":
            self.hwdec_current = value or ""
            self.videoParamsChanged.emit()

    def _on_event(self, eid, data):
        if eid == mpv.MpvEventID.PLAYBACK_RESTART:
            self._frame = None
            self._seek_busy = False
            self._seek_guard.stop()
            if self._seek_pending is not None:
                self._issue_seek()
            else:
                self.seekFinished.emit()
            self.overlay.update()
        elif eid == mpv.MpvEventID.FILE_LOADED:
            self._seek_busy = False
            self._frame = None
            self.fileLoaded.emit(self.path)
        elif eid == mpv.MpvEventID.END_FILE:
            reason, err = data
            msg = ""
            if reason == mpv.MpvEventEndFile.ERROR:
                msg = self.last_error or "Playback error"
            self.fileEnded.emit(reason, msg)
        elif eid == mpv.MpvEventID.VIDEO_RECONFIG:
            self._apply_view()

    # ------------------------------------------------------------------ OpenGL
    def initializeGL(self):
        self.ctx = mpv.MpvRenderContext(self.m, "opengl",
                                        opengl_init_params={"get_proc_address": self._proc_fn})
        self.ctx.update_cb = self._sigRender.emit
        if self._pending_load is not None:
            # a file requested before the GL context existed: vo=libmpv needs the context first
            args = self._pending_load
            self._pending_load = None
            QTimer.singleShot(0, lambda: self.load(*args))

    def _on_render_request(self):
        if self.ctx is not None and not self._closed:
            if self.ctx.update():
                self.update()

    def paintGL(self):
        if self.ctx is None or self._closed:
            return
        w, h = self._phys_size()
        self.ctx.render(flip_y=True, block_for_target_time=False,
                        opengl_fbo={"w": w, "h": h, "fbo": self.defaultFramebufferObject()})

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.overlay.setGeometry(self.rect())
        self._apply_view(clamp_only=True)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.makeCurrent()
            if self.ctx is not None:
                self.ctx.free()
                self.ctx = None
            self.doneCurrent()
        except Exception:
            pass
        try:
            self.m.terminate()
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers
    def _phys_size(self) -> tuple[int, int]:
        dpr = self.devicePixelRatioF()
        return max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))

    def prop(self, name, default=None):
        try:
            v = self.m._get_property(name)
            return default if v is None else v
        except Exception:
            return default

    def set_prop(self, name, value):
        try:
            self.m._set_property(name, value)
            return True
        except Exception:
            return False

    def cmd(self, *args):
        try:
            return self.m.command(*args)
        except Exception as e:
            self.last_error = str(e)
            return None

    @property
    def has_video(self) -> bool:
        return bool(self.vparams.get("w"))

    @property
    def has_file(self) -> bool:
        return bool(self.path)

    def frame_rate(self) -> float:
        return self.fps if self.fps and self.fps > 0 else 30.0

    # ------------------------------------------------------------------ playback API
    def load(self, path: str, start: float | None = None, pause: bool | None = None,
             keep_view: bool = False):
        if self.ctx is None:
            self._pending_load = (path, start, pause, keep_view)
            self.path = path
            return True
        if not keep_view:
            self.zoom_log2, self.pan_x, self.pan_y = 0.0, 0.0, 0.0
        self.fps = 0.0
        self._frame = None
        self.last_error = ""
        opts = {}
        if start is not None and start > 0:
            opts["start"] = f"{start:.6f}"
        if pause is not None:
            opts["pause"] = "yes" if pause else "no"
        try:
            self.m.loadfile(path, "replace", **opts)
        except Exception as e:
            self.last_error = str(e)
            return False
        self.path = path
        self._apply_view()
        return True

    def stop(self):
        self.cmd("stop")
        self.path = ""
        self.duration = 0.0
        self.time_pos = 0.0
        self.vparams = {}
        self.voparams = {}
        self.durationChanged.emit(0.0)
        self.timeChanged.emit(0.0)

    def set_paused(self, p: bool):
        if not p and self.eof and self.duration and self.time_pos >= self.duration - 0.05:
            self.seek(0.0)
        self.set_prop("pause", "yes" if p else "no")

    def toggle_pause(self):
        self.set_paused(not self.paused)

    def seek(self, t: float, exact: bool = True, floor: float = 0.0):
        """Seek to t. `floor` is the lowest time the precision bias may reach (a timeline clip start)."""
        if not self.has_file:
            return
        dur = self.duration or 0.0
        t = max(0.0, min(t, dur)) if dur else max(0.0, t)
        self._seek_pending = (t, exact, floor)
        if not self._seek_busy:
            self._issue_seek()

    def _issue_seek(self):
        if self._seek_pending is None:
            return
        t, exact, floor = self._seek_pending
        self._seek_pending = None
        self._seek_busy = True
        self._seek_guard.start()
        if exact:
            # mpv shows the first frame with pts >= target; bias back by 0.5 ms (< any frame interval)
            # so a target rounded up by float formatting never skips the intended frame. Never bias
            # across a clip boundary in timeline mode (floor), mpv's EDL seeking misbehaves there.
            t = max(0.0, floor, t - 0.0005) if t >= floor else max(0.0, t - 0.0005)
        if self.cmd("seek", f"{t:.6f}", "absolute+exact" if exact else "absolute+keyframes") is None \
                and self.last_error:
            self._seek_busy = False

    def _seek_unstick(self):
        self._seek_busy = False
        if self._seek_pending is not None:
            self._issue_seek()

    def seek_relative(self, dt: float):
        self.seek(self.time_pos + dt)

    def frame_step(self, n: int = 1):
        if not self.has_file:
            return
        if n == 1:
            self.cmd("frame-step")
        elif n == -1:
            self.cmd("frame-back-step")
        else:
            self.set_prop("pause", "yes")
            self.seek(self.time_pos + n / self.frame_rate())

    def set_speed(self, s: float):
        self.set_prop("speed", f"{max(0.01, min(100.0, s)):.6f}")

    def set_volume(self, v: float):
        self.set_prop("volume", f"{max(0.0, min(200.0, v)):.2f}")

    def set_mute(self, m: bool):
        self.set_prop("mute", "yes" if m else "no")

    def set_ab_loop(self, a: float | None, b: float | None):
        self.set_prop("ab-loop-a", "no" if a is None else f"{a:.6f}")
        self.set_prop("ab-loop-b", "no" if b is None else f"{b:.6f}")

    def set_equalizer(self, brightness=0, contrast=0, saturation=0, gamma=0, hue=0):
        for k, v in (("brightness", brightness), ("contrast", contrast), ("saturation", saturation),
                     ("gamma", gamma), ("hue", hue)):
            self.set_prop(k, str(int(round(max(-100, min(100, v))))))

    def set_quality(self, q: str):
        if q not in QUALITY:
            return
        self.quality = q
        for k, v in QUALITY[q].items():
            self.set_prop(k, v)
        self._scaler_override = None
        self._update_scaler()

    def set_hwdec(self, mode: str):
        self.set_prop("hwdec", mode)

    def screenshot(self, path: str, include_subs: bool = False) -> bool:
        return self.cmd("screenshot-to-file", path, "subtitles" if include_subs else "video") is not None \
            or not self.last_error

    # ------------------------------------------------------------------ zoom / pan
    def _display_dims(self) -> tuple[float, float, int, int]:
        """Display size (aspect-corrected, rotated) and pixel counts along display x/y."""
        vo = self.voparams or self.vparams
        w = int(vo.get("w") or 0)
        h = int(vo.get("h") or 0)
        dw = float(vo.get("dw") or w)
        dh = float(vo.get("dh") or h)
        rot = int(vo.get("rotate") or 0) % 360
        if rot in (90, 270):
            dw, dh, w, h = dh, dw, h, w
        return dw, dh, w, h

    def rotation(self) -> int:
        vo = self.voparams or self.vparams
        return int(vo.get("rotate") or 0) % 360

    def fit_scale(self) -> float:
        dw, dh, _, _ = self._display_dims()
        if dw <= 0 or dh <= 0:
            return 1.0
        W, H = self._phys_size()
        return min(W / dw, H / dh)

    def video_rect(self) -> QRectF:
        """Displayed video rectangle in physical pixels."""
        dw, dh, _, _ = self._display_dims()
        if dw <= 0:
            return QRectF()
        s = self.fit_scale() * (2.0 ** self.zoom_log2)
        Dw, Dh = dw * s, dh * s
        W, H = self._phys_size()
        cx = W / 2 + self.pan_x * Dw
        cy = H / 2 + self.pan_y * Dh
        return QRectF(cx - Dw / 2, cy - Dh / 2, Dw, Dh)

    def pixel_scale(self) -> float:
        """Physical screen pixels per source pixel (horizontal)."""
        dw, dh, nx, ny = self._display_dims()
        if nx <= 0:
            return 1.0
        return self.video_rect().width() / nx

    def zoom_percent(self) -> float:
        return self.pixel_scale() * 100.0

    def is_fit(self) -> bool:
        return abs(self.zoom_log2) < 1e-6 and abs(self.pan_x) < 1e-6 and abs(self.pan_y) < 1e-6

    def _apply_view(self, clamp_only: bool = False):
        if self._closed:
            return
        self._clamp_pan()
        self.set_prop("video-zoom", f"{self.zoom_log2:.6f}")
        self.set_prop("video-pan-x", f"{self.pan_x:.6f}")
        self.set_prop("video-pan-y", f"{self.pan_y:.6f}")
        self._update_scaler()
        self.overlay.update()
        self.viewChanged.emit()

    def _clamp_pan(self):
        r = self.video_rect()
        if r.isEmpty():
            return
        W, H = self._phys_size()
        Dw, Dh = r.width(), r.height()
        mx = 0.0 if Dw <= W else 0.5 - W / (2 * Dw)
        my = 0.0 if Dh <= H else 0.5 - H / (2 * Dh)
        self.pan_x = max(-mx, min(mx, self.pan_x))
        self.pan_y = max(-my, min(my, self.pan_y))

    def _update_scaler(self):
        want = "nearest" if (self.pixel_peep and self.pixel_scale() >= 3.0) else None
        if want != self._scaler_override:
            self._scaler_override = want
            self.set_prop("scale", want or QUALITY[self.quality]["scale"])

    def zoom_to(self, log2: float, anchor: QPointF | None = None):
        fit = self.fit_scale()
        _, _, nx, _ = self._display_dims()
        dw = self._display_dims()[0]
        if nx > 0 and dw > 0:
            # clamp by on-screen pixel size
            min_l = math.log2(max(1e-6, (MIN_ZOOM_PCT / 100.0) * nx / dw / fit))
            max_l = math.log2(max(1e-6, (MAX_ZOOM_PCT / 100.0) * nx / dw / fit))
            log2 = max(min(min_l, 0.0), min(max_l, log2))
        old = self.video_rect()
        W, H = self._phys_size()
        if anchor is None:
            anchor = QPointF(W / 2, H / 2)
        if old.isEmpty():
            self.zoom_log2 = log2
            self._apply_view()
            return
        ux = (anchor.x() - old.center().x()) / old.width()
        uy = (anchor.y() - old.center().y()) / old.height()
        self.zoom_log2 = log2
        new = self.video_rect()
        cx = anchor.x() - ux * new.width()
        cy = anchor.y() - uy * new.height()
        self.pan_x = (cx - W / 2) / new.width()
        self.pan_y = (cy - H / 2) / new.height()
        self._apply_view()

    def zoom_by(self, factor: float, anchor: QPointF | None = None):
        self.zoom_to(self.zoom_log2 + math.log2(factor), anchor)

    def zoom_percent_to(self, pct: float, anchor: QPointF | None = None):
        dw, _, nx, _ = self._display_dims()
        if nx <= 0 or dw <= 0:
            return
        s = (pct / 100.0) * nx / dw
        self.zoom_to(math.log2(s / self.fit_scale()), anchor)

    def zoom_fit(self):
        self.zoom_log2, self.pan_x, self.pan_y = 0.0, 0.0, 0.0
        self._apply_view()

    def set_pixel_peep(self, on: bool):
        self.pixel_peep = on
        self._frame = None
        self._update_scaler()
        self.overlay.update()

    # ------------------------------------------------------------------ pixel inspection
    def source_pixel_at(self, pos_logical: QPointF) -> tuple[int, int] | None:
        r = self.video_rect()
        if r.isEmpty():
            return None
        dpr = self.devicePixelRatioF()
        px, py = pos_logical.x() * dpr, pos_logical.y() * dpr
        dx = (px - r.left()) / r.width()
        dy = (py - r.top()) / r.height()
        if not (0 <= dx < 1 and 0 <= dy < 1):
            return None
        vp = self.vparams
        sw, sh = int(vp.get("w") or 0), int(vp.get("h") or 0)
        rot = self.rotation()
        if rot == 90:
            sx, sy = dy, 1 - dx
        elif rot == 180:
            sx, sy = 1 - dx, 1 - dy
        elif rot == 270:
            sx, sy = 1 - dy, dx
        else:
            sx, sy = dx, dy
        return min(sw - 1, int(sx * sw)), min(sh - 1, int(sy * sh))

    def sample_rgb(self, pos_logical: QPointF) -> tuple[int, int, int] | None:
        """RGB (8-bit) of the decoded source frame under the cursor. Grabs the frame when paused."""
        if not self.paused or not self.has_video:
            return None
        sp = self.source_pixel_at(pos_logical)
        if sp is None:
            return None
        fr = self._frame
        if fr is None or abs(fr[0] - self.time_pos) > 1e-6:
            try:
                res = self.m.command("screenshot-raw", "video")
            except Exception:
                return None
            if not res or res.get("format") != "bgr0":
                return None
            fr = (self.time_pos, int(res["w"]), int(res["h"]), int(res["stride"]), res["data"])
            self._frame = fr
        _, w, h, stride, data = fr
        x, y = sp
        vp = self.vparams
        sw, sh = int(vp.get("w") or 0), int(vp.get("h") or 0)
        if (w, h) != (sw, sh) and sw and sh:
            # screenshot is in display orientation / size: map through the display rect instead
            r = self.video_rect()
            dpr = self.devicePixelRatioF()
            dx = (pos_logical.x() * dpr - r.left()) / r.width()
            dy = (pos_logical.y() * dpr - r.top()) / r.height()
            x, y = min(w - 1, int(dx * w)), min(h - 1, int(dy * h))
        if not (0 <= x < w and 0 <= y < h):
            return None
        o = y * stride + x * 4
        if o + 2 >= len(data):
            return None
        return data[o + 2], data[o + 1], data[o]

    # ------------------------------------------------------------------ mouse / drag & drop
    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        if not self.has_video:
            return
        dpr = self.devicePixelRatioF()
        base = 1.06 if e.modifiers() & Qt.ControlModifier else 1.25
        self.zoom_by(base ** steps, QPointF(e.position().x() * dpr, e.position().y() * dpr))
        e.accept()

    def mousePressEvent(self, e):
        self._press_btn = e.button()
        self._press_pos = e.position()
        self._last_pos = e.position()
        self._moved = False
        self._jog_accum = 0.0
        if e.button() == Qt.MiddleButton:
            self.zoom_fit()
        e.accept()

    def mouseMoveEvent(self, e):
        self.mouseActivity.emit()
        pos = e.position()
        self.hover_pos = pos
        if self._press_btn is not None:
            d = pos - self._last_pos
            if (pos - self._press_pos).manhattanLength() > 4:
                self._moved = True
            if self._press_btn == Qt.LeftButton and self._moved:
                r = self.video_rect()
                if not r.isEmpty():
                    dpr = self.devicePixelRatioF()
                    self.pan_x += d.x() * dpr / r.width()
                    self.pan_y += d.y() * dpr / r.height()
                    self._apply_view()
                    self.setCursor(Qt.ClosedHandCursor)
            elif self._press_btn == Qt.RightButton and self._moved:
                self._jog_accum += d.x()
                step_px = 6.0
                while abs(self._jog_accum) >= step_px:
                    n = 1 if self._jog_accum > 0 else -1
                    self._jog_accum -= n * step_px
                    self.jogRequested.emit(n)
                self.setCursor(Qt.SizeHorCursor)
        self._last_pos = pos
        self.overlay.update()

    def mouseReleaseEvent(self, e):
        btn = self._press_btn
        self._press_btn = None
        self.unsetCursor()
        if btn == Qt.LeftButton and not self._moved and self.click_to_pause:
            self._click_timer.start(QApplication.doubleClickInterval())
        elif btn == Qt.RightButton and not self._moved:
            self.contextMenuRequested.emit(e.globalPosition().toPoint())
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._click_timer.stop()
            self.doubleClicked.emit()
        e.accept()

    def leaveEvent(self, e):
        self.hover_pos = None
        self.overlay.update()
        super().leaveEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.filesDropped.emit(files)
            e.acceptProposedAction()


class ViewerOverlay(QWidget):
    """Transparent HUD drawn above the video: OSD messages, zoom badge, pixel grid and read-out."""

    def __init__(self, viewer: MpvWidget):
        super().__init__(viewer)
        self.v = viewer
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.title = ""
        self.info_text = ""
        self.show_grid = True
        self._osd = ""
        self._osd_until = 0.0
        self._badge_until = 0.0
        self._osd_timer = QTimer(self, singleShot=True, timeout=self.update)
        self.mono = QFont("Consolas")
        self.mono.setPixelSize(12)
        self.ui = QFont("Segoe UI")
        self.ui.setPixelSize(13)
        self.big = QFont("Segoe UI")
        self.big.setPixelSize(22)
        self.big.setWeight(QFont.DemiBold)
        viewer.viewChanged.connect(self._on_view)

    def _on_view(self):
        self._badge_until = time.monotonic() + 1.6
        self._osd_timer.start(1700)

    def osd(self, text: str, seconds: float = 1.2):
        self._osd = text
        self._osd_until = time.monotonic() + seconds
        self._osd_timer.start(int(seconds * 1000) + 60)
        self.update()

    def set_info(self, text: str):
        self.info_text = text
        self.update()

    def _panel(self, p: QPainter, rect: QRectF, alpha=170):
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        p.fillPath(path, QColor(10, 10, 12, alpha))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        v = self.v
        dpr = v.devicePixelRatioF()
        now = time.monotonic()
        W, H = self.width(), self.height()

        # --- pixel grid -------------------------------------------------------------------------
        if v.pixel_peep and self.show_grid and v.has_video:
            ps = v.pixel_scale()
            if ps >= 12:
                r = v.video_rect()
                _, _, nx, ny = v._display_dims()
                step_x = r.width() / nx
                step_y = r.height() / ny
                p.setRenderHint(QPainter.Antialiasing, False)
                p.setPen(QPen(QColor(0, 0, 0, 90), 1))
                x0 = max(0, int((0 - r.left()) / step_x))
                x1 = min(nx, int((W * dpr - r.left()) / step_x) + 1)
                y0 = max(0, int((0 - r.top()) / step_y))
                y1 = min(ny, int((H * dpr - r.top()) / step_y) + 1)
                top = max(0.0, r.top() / dpr)
                bot = min(H, r.bottom() / dpr)
                left = max(0.0, r.left() / dpr)
                right = min(W, r.right() / dpr)
                for i in range(x0, x1 + 1):
                    x = (r.left() + i * step_x) / dpr
                    p.drawLine(QPointF(x, top), QPointF(x, bot))
                for j in range(y0, y1 + 1):
                    y = (r.top() + j * step_y) / dpr
                    p.drawLine(QPointF(left, y), QPointF(right, y))
                p.setRenderHint(QPainter.Antialiasing, True)

        # --- title badge ------------------------------------------------------------------------
        if self.title:
            p.setFont(self.ui)
            fm = QFontMetricsF(self.ui)
            tw = min(fm.horizontalAdvance(self.title) + 18, W - 20)
            rect = QRectF(10, 10, tw, 24)
            self._panel(p, rect, 140)
            p.setPen(QColor(230, 230, 235))
            p.drawText(rect.adjusted(9, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(self.title, Qt.ElideMiddle, tw - 16))

        # --- zoom / speed badge -----------------------------------------------------------------
        badges = []
        if v.has_video and (not v.is_fit() or now < self._badge_until):
            badges.append(f"{v.zoom_percent():.0f}%")
        if abs(v.speed - 1.0) > 1e-6:
            badges.append(f"{v.speed * 100:g}% speed")
        if v.pixel_peep:
            badges.append("PIXEL PEEP")
        if badges:
            p.setFont(self.ui)
            fm = QFontMetricsF(self.ui)
            txt = "   ".join(badges)
            tw = fm.horizontalAdvance(txt) + 20
            rect = QRectF(W - tw - 10, 10, tw, 24)
            self._panel(p, rect, 140)
            p.setPen(QColor(255, 170, 120))
            p.drawText(rect, Qt.AlignCenter, txt)

        # --- pixel read-out ---------------------------------------------------------------------
        hp = v.hover_pos
        if hp is not None and v.has_video and (v.pixel_peep or v.zoom_percent() > 100.5):
            sp = v.source_pixel_at(hp)
            if sp is not None:
                txt = f"X {sp[0]:>5}   Y {sp[1]:>5}"
                rgb = v.sample_rgb(hp) if v.pixel_peep else None
                if rgb is not None:
                    txt += f"   R {rgb[0]:>3}  G {rgb[1]:>3}  B {rgb[2]:>3}   #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
                elif v.pixel_peep and not v.paused:
                    txt += "   (pause to read RGB)"
                p.setFont(self.mono)
                fm = QFontMetricsF(self.mono)
                tw = fm.horizontalAdvance(txt) + 18
                sw = 22 if rgb is not None else 0
                rect = QRectF(10, H - 34, tw + sw, 24)
                self._panel(p, rect, 170)
                if rgb is not None:
                    p.fillRect(QRectF(rect.left() + 6, rect.top() + 5, 14, 14), QColor(*rgb))
                    p.setPen(QPen(QColor(255, 255, 255, 120), 1))
                    p.drawRect(QRectF(rect.left() + 6, rect.top() + 5, 14, 14))
                p.setPen(QColor(220, 220, 225))
                p.drawText(rect.adjusted(9 + sw, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)

        # --- info panel -------------------------------------------------------------------------
        if self.info_text:
            p.setFont(self.mono)
            fm = QFontMetricsF(self.mono)
            lines = self.info_text.splitlines()
            tw = max(fm.horizontalAdvance(l) for l in lines) + 24
            th = fm.height() * len(lines) + 18
            rect = QRectF(10, 44, tw, th)
            self._panel(p, rect, 190)
            p.setPen(QColor(220, 220, 225))
            y = rect.top() + 9 + fm.ascent()
            for l in lines:
                p.drawText(QPointF(rect.left() + 12, y), l)
                y += fm.height()

        # --- transient OSD ----------------------------------------------------------------------
        if self._osd and now < self._osd_until:
            p.setFont(self.big)
            fm = QFontMetricsF(self.big)
            tw = fm.horizontalAdvance(self._osd) + 36
            rect = QRectF((W - tw) / 2, H * 0.12, tw, 44)
            self._panel(p, rect, 160)
            p.setPen(QColor(245, 245, 248))
            p.drawText(rect, Qt.AlignCenter, self._osd)
        p.end()
