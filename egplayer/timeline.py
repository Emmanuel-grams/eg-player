"""Single-track (V1 + linked A1) magnetic timeline with trimming, moving, razor and waveforms."""
from __future__ import annotations

import math

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QScrollBar, QToolButton, QVBoxLayout,
                               QWidget)

from . import theme
from .media import WAVE_RATE, MediaManager
from .project import Timeline, fmt_timecode

MIME_MEDIA = "application/x-eg-media"


class TimelineView(QWidget):
    seekRequested = Signal(float)
    selectionChanged = Signal(object)
    edited = Signal(str)
    liveEdit = Signal()
    mediaDropped = Signal(list, float)
    contextAction = Signal(str, object)
    viewChanged = Signal()

    RULER_H = 26
    V_H = 62
    A_H = 46
    HEAD_W = 46
    PAD = 6
    EDGE = 6

    def __init__(self, timeline: Timeline, media: MediaManager, parent=None):
        super().__init__(parent)
        self.tl = timeline
        self.media = media
        self.thumbs: dict[str, QPixmap] = {}
        self.fps = 30.0
        self.pps = 40.0
        self.scroll = 0.0
        self.playhead = 0.0
        self.selected: int | None = None
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumHeight(self.RULER_H + self.V_H + self.A_H + 3 * self.PAD + 6)
        self._mode = None
        self._press_x = 0.0
        self._drag_uid = None
        self._orig = None
        self._insert_idx: int | None = None
        self._drop_t: float | None = None
        self._hover_edge = None
        self.f_small = QFont("Segoe UI")
        self.f_small.setPixelSize(11)
        self.f_mono = QFont("Consolas")
        self.f_mono.setPixelSize(10)
        self.f_name = QFont("Segoe UI")
        self.f_name.setPixelSize(12)
        self.f_name.setWeight(QFont.DemiBold)

    # ------------------------------------------------------------------ coordinates
    def t2x(self, t: float) -> float:
        return self.HEAD_W + (t - self.scroll) * self.pps

    def x2t(self, x: float) -> float:
        return self.scroll + (x - self.HEAD_W) / self.pps

    def visible_secs(self) -> float:
        return max(0.1, (self.width() - self.HEAD_W) / self.pps)

    def v_rect_y(self) -> float:
        return self.RULER_H + self.PAD

    def a_rect_y(self) -> float:
        return self.RULER_H + self.PAD + self.V_H + self.PAD

    def zoom(self, factor: float, anchor_x: float | None = None):
        if anchor_x is None:
            anchor_x = self.t2x(self.playhead)
            if not (self.HEAD_W <= anchor_x <= self.width()):
                anchor_x = self.HEAD_W + (self.width() - self.HEAD_W) / 2
        t_anchor = self.x2t(anchor_x)
        self.pps = max(0.02, min(4000.0, self.pps * factor))
        self.scroll = max(0.0, t_anchor - (anchor_x - self.HEAD_W) / self.pps)
        self._clamp_scroll()
        self.update()
        self.viewChanged.emit()

    def zoom_fit(self):
        d = self.tl.duration
        if d <= 0:
            self.pps, self.scroll = 40.0, 0.0
        else:
            self.pps = max(0.02, (self.width() - self.HEAD_W - 30) / d)
            self.scroll = 0.0
        self.update()
        self.viewChanged.emit()

    def _clamp_scroll(self):
        max_scroll = max(0.0, self.tl.duration + self.visible_secs() * 0.5 - self.visible_secs())
        self.scroll = max(0.0, min(self.scroll, max_scroll))

    def set_scroll(self, s: float):
        self.scroll = max(0.0, s)
        self.update()

    def set_playhead(self, t: float, follow: bool = True):
        self.playhead = max(0.0, t)
        if follow:
            x = self.t2x(self.playhead)
            if x > self.width() - 20 or x < self.HEAD_W:
                self.scroll = max(0.0, self.playhead - self.visible_secs() * 0.1)
                self.viewChanged.emit()
        self.update()

    # ------------------------------------------------------------------ hit testing
    def _clip_spans(self):
        out, t = [], 0.0
        for c in self.tl.clips:
            out.append((c, t, t + c.duration))
            t += c.duration
        return out

    def _hit(self, pos: QPointF):
        y = pos.y()
        if y < self.RULER_H:
            return ("ruler", None)
        in_lane = self.v_rect_y() <= y <= self.a_rect_y() + self.A_H
        if not in_lane:
            return ("empty", None)
        x = pos.x()
        spans = self._clip_spans()
        for i, (c, a, b) in enumerate(spans):
            xa, xb = self.t2x(a), self.t2x(b)
            if xa - self.EDGE <= x <= xb + self.EDGE:
                if abs(x - xa) <= self.EDGE and (x >= xa or i == 0):
                    return ("trim_l", c)
                if abs(x - xb) <= self.EDGE and (x <= xb or i == len(spans) - 1):
                    return ("trim_r", c)
                if xa <= x <= xb:
                    return ("clip", c)
        return ("empty", None)

    def _snap_time(self, t: float, px: float = 7.0) -> float:
        best, bd = t, px / self.pps
        for p in self.tl.edit_points():
            if abs(p - t) < bd:
                best, bd = p, abs(p - t)
        return best

    def _insert_index_at(self, t: float) -> int:
        pts = self.tl.edit_points()
        return min(range(len(pts)), key=lambda k: abs(pts[k] - t)) if pts else 0

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#141416"))
        vy, ay = self.v_rect_y(), self.a_rect_y()
        # lanes
        p.fillRect(QRectF(self.HEAD_W, vy, W, self.V_H), QColor("#1b1b1e"))
        p.fillRect(QRectF(self.HEAD_W, ay, W, self.A_H), QColor("#1b1b1e"))
        self._paint_ruler(p, W)
        p.save()
        p.setClipRect(QRectF(self.HEAD_W, self.RULER_H, W - self.HEAD_W, H - self.RULER_H))
        spans = self._clip_spans()
        for c, a, b in spans:
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < self.HEAD_W - 2 or x0 > W + 2:
                continue
            self._paint_clip(p, c, x0, x1, vy, ay)
        if not spans:
            p.setPen(QColor(theme.TEXT_DIM))
            p.setFont(self.f_small)
            p.drawText(QRectF(self.HEAD_W, vy, W - self.HEAD_W, self.V_H + self.A_H + self.PAD),
                       Qt.AlignCenter,
                       "Empty timeline — drag clips here from the Media Pool, or open a video and press F12 "
                       "(Append In→Out)")
        # insertion marker (move / drop)
        mark_t = None
        if self._mode == "move" and self._insert_idx is not None:
            pts = self.tl.edit_points()
            if 0 <= self._insert_idx < len(pts):
                mark_t = pts[self._insert_idx]
        elif self._drop_t is not None:
            mark_t = self._drop_t
        if mark_t is not None:
            x = self.t2x(mark_t)
            p.setPen(QPen(QColor(theme.ACCENT_2), 3))
            p.drawLine(QPointF(x, vy - 2), QPointF(x, ay + self.A_H + 2))
        # playhead
        x = self.t2x(self.playhead)
        p.setPen(QPen(QColor(theme.PLAYHEAD), 1.5))
        p.drawLine(QPointF(x, self.RULER_H - 2), QPointF(x, H))
        p.restore()
        if x >= self.HEAD_W - 1:
            head = QPolygonF([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x + 6, 12),
                              QPointF(x, self.RULER_H - 6), QPointF(x - 6, 12)])
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.PLAYHEAD))
            p.drawPolygon(head)
        # track headers
        p.fillRect(QRectF(0, 0, self.HEAD_W, H), QColor("#19191c"))
        p.setPen(QColor("#2c2c31"))
        p.drawLine(QPointF(self.HEAD_W - 0.5, 0), QPointF(self.HEAD_W - 0.5, H))
        p.setFont(self.f_name)
        p.setPen(QColor("#9ab4e6"))
        p.drawText(QRectF(0, vy, self.HEAD_W, self.V_H), Qt.AlignCenter, "V1")
        p.setPen(QColor("#8fd3ad"))
        p.drawText(QRectF(0, ay, self.HEAD_W, self.A_H), Qt.AlignCenter, "A1")
        p.end()

    def _paint_ruler(self, p: QPainter, W: int):
        p.fillRect(QRectF(0, 0, W, self.RULER_H), QColor("#1d1d21"))
        p.setPen(QColor("#2c2c31"))
        p.drawLine(QPointF(0, self.RULER_H - 0.5), QPointF(W, self.RULER_H - 0.5))
        fps = self.fps or 30.0
        cands = [1 / fps, 2 / fps, 5 / fps, 10 / fps, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900,
                 1800, 3600, 7200]
        step = next((c for c in cands if c * self.pps >= 90), cands[-1])
        minor = step / 5 if step * self.pps / 5 >= 8 else step / 2
        t0 = math.floor(self.scroll / minor) * minor
        t1 = self.x2t(W)
        p.setFont(self.f_mono)
        n = 0
        t = t0
        while t <= t1 and n < 5000:
            x = self.t2x(t)
            is_major = abs(t / step - round(t / step)) < 1e-6
            if x >= self.HEAD_W:
                if is_major:
                    p.setPen(QColor("#6a6a72"))
                    p.drawLine(QPointF(x, self.RULER_H - 12), QPointF(x, self.RULER_H - 1))
                    p.setPen(QColor("#a8a8b0"))
                    p.drawText(QPointF(x + 4, 12), fmt_timecode(t, fps))
                else:
                    p.setPen(QColor("#45454c"))
                    p.drawLine(QPointF(x, self.RULER_H - 6), QPointF(x, self.RULER_H - 1))
            t = round((t + minor) / minor) * minor
            n += 1

    def _paint_clip(self, p: QPainter, c, x0: float, x1: float, vy: float, ay: float):
        sel = c.uid == self.selected
        info = self.media.get(c.path)
        w = max(2.0, x1 - x0)
        vr = QRectF(x0 + 0.5, vy, w - 1, self.V_H)
        ar = QRectF(x0 + 0.5, ay, w - 1, self.A_H)
        has_v = info.has_video if info else True
        has_a = info.has_audio if info else True
        vcol = QColor(theme.VIDEO_CLIP) if has_v else QColor("#2a2a30")
        acol = QColor(theme.AUDIO_CLIP) if has_a else QColor("#2a2a30")
        if sel:
            vcol = vcol.lighter(125)
            acol = acol.lighter(125)
        for r, col in ((vr, vcol), (ar, acol)):
            path = QPainterPath()
            path.addRoundedRect(r, 4, 4)
            p.fillPath(path, col)
        # thumbnail
        pix = self.thumbs.get(c.path)
        if pix is not None and has_v and w > 30:
            th = self.V_H - 4
            tw = th * pix.width() / max(1, pix.height())
            p.save()
            p.setClipRect(vr.adjusted(2, 2, -2, -2))
            target = QRectF(x0 + 2, vy + 2, tw, th)
            p.setOpacity(0.85)
            p.drawPixmap(target, pix, QRectF(pix.rect()))
            p.setOpacity(1.0)
            # tail thumbnail if wide
            if w > tw * 2.6:
                p.drawPixmap(QRectF(x1 - tw - 2, vy + 2, tw, th), pix, QRectF(pix.rect()))
            p.restore()
        # labels
        p.save()
        p.setClipRect(vr.adjusted(3, 0, -3, 0))
        p.setFont(self.f_name)
        txt_x = x0 + 6
        bg = QRectF(txt_x - 2, vy + 3, min(w - 8, QFontMetricsF(self.f_name).horizontalAdvance(c.name) + 8), 16)
        p.fillRect(bg, QColor(0, 0, 0, 120))
        p.setPen(QColor("#f2f2f5"))
        p.drawText(QPointF(txt_x + 2, vy + 15), c.name)
        badges = []
        if abs(c.speed - 1) > 1e-6:
            badges.append(f"{c.speed * 100:g}%")
        if c.has_color():
            badges.append("CC")
        if abs(c.volume - 1) > 1e-6:
            badges.append(f"vol {c.volume * 100:.0f}%")
        if badges:
            p.setFont(self.f_small)
            s = "  ".join(badges)
            bw = QFontMetricsF(self.f_small).horizontalAdvance(s) + 10
            br = QRectF(x0 + 4, vy + self.V_H - 18, bw, 15)
            p.fillRect(br, QColor(255, 122, 61, 200))
            p.setPen(QColor("#151515"))
            p.drawText(br, Qt.AlignCenter, s)
        p.restore()
        # waveform
        peaks = self.media.wave(c.path) if has_a else b""
        if peaks:
            self._paint_wave(p, c, peaks, x0, x1, ar)
        # fades
        for fade, at_start in ((c.fade_in, True), (c.fade_out, False)):
            if fade > 0:
                fw = min(w, fade * self.pps)
                if at_start:
                    tri = QPolygonF([QPointF(x0, vy), QPointF(x0 + fw, vy), QPointF(x0, vy + self.V_H)])
                else:
                    tri = QPolygonF([QPointF(x1, vy), QPointF(x1 - fw, vy), QPointF(x1, vy + self.V_H)])
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 110))
                p.drawPolygon(tri)
        # outline
        p.setBrush(Qt.NoBrush)
        pen = QPen(QColor(theme.ACCENT) if sel else QColor(0, 0, 0, 160), 2 if sel else 1)
        p.setPen(pen)
        p.drawRoundedRect(vr, 4, 4)
        p.drawRoundedRect(ar, 4, 4)
        if self._hover_edge and self._hover_edge[1] is c:
            ex = x0 if self._hover_edge[0] == "trim_l" else x1
            p.setPen(QPen(QColor(theme.ACCENT), 3))
            p.drawLine(QPointF(ex, vy), QPointF(ex, ay + self.A_H))

    def _paint_wave(self, p: QPainter, c, peaks: bytes, x0: float, x1: float, ar: QRectF):
        n = len(peaks)
        cy = ar.center().y()
        half = (ar.height() - 8) / 2
        gain = min(2.0, c.volume)
        lines = []
        px_start = int(max(x0, self.HEAD_W))
        px_end = int(min(x1, self.width()))
        src_per_px = c.speed / self.pps
        for px in range(px_start, px_end):
            src = c.src_in + (px - x0) * src_per_px
            i0 = int(src * WAVE_RATE)
            i1 = max(i0 + 1, int((src + src_per_px) * WAVE_RATE))
            if i0 >= n:
                break
            seg = peaks[max(0, i0):min(n, i1)]
            if not seg:
                continue
            h = min(half, max(seg) / 255.0 * half * gain)
            if h >= 0.5:
                lines.append(QLineF(px + 0.5, cy - h, px + 0.5, cy + h))
        if lines:
            p.setPen(QPen(QColor(200, 255, 220, 170), 1))
            p.drawLines(lines)

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        pos = e.position()
        kind, clip = self._hit(pos)
        self._press_x = pos.x()
        if e.button() == Qt.RightButton:
            if clip is not None:
                self._select(clip.uid)
                self._context_menu(e.globalPosition().toPoint(), clip)
            return
        if e.button() != Qt.LeftButton:
            return
        if kind in ("ruler", "empty"):
            self._mode = "scrub"
            if kind == "empty":
                self._select(None)
            self._scrub_to(pos.x())
        elif kind in ("trim_l", "trim_r"):
            self._mode = kind
            self._drag_uid = clip.uid
            self._orig = (clip.src_in, clip.src_out)
            self._select(clip.uid)
        elif kind == "clip":
            self._mode = "press_clip"
            self._drag_uid = clip.uid
            self._select(clip.uid)

    def _scrub_to(self, x: float):
        t = max(0.0, min(self.tl.duration, self.x2t(x)))
        t = self._snap_time(t)
        self.playhead = t
        self.update()
        self.seekRequested.emit(t)

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._mode is None:
            kind, clip = self._hit(pos)
            if kind in ("trim_l", "trim_r"):
                self.setCursor(Qt.SizeHorCursor)
                self._hover_edge = (kind, clip)
            else:
                self.unsetCursor()
                self._hover_edge = None
            self.update()
            return
        if self._mode == "scrub":
            self._scrub_to(pos.x())
            return
        clip = self.tl.by_uid(self._drag_uid)
        if clip is None:
            return
        dt = (pos.x() - self._press_x) / self.pps
        fps = (self.media.get(clip.path).fps if self.media.get(clip.path) else 0) or self.fps or 30.0
        min_src = 1.0 / fps
        info = self.media.get(clip.path)
        src_dur = info.duration if info and info.duration else clip.src_out
        if self._mode == "trim_l":
            new_in = self._orig[0] + dt * clip.speed
            new_in = round(new_in * fps) / fps
            clip.src_in = max(0.0, min(new_in, clip.src_out - min_src))
            self.liveEdit.emit()
            self.update()
        elif self._mode == "trim_r":
            new_out = self._orig[1] + dt * clip.speed
            new_out = round(new_out * fps) / fps
            clip.src_out = min(src_dur, max(new_out, clip.src_in + min_src))
            self.liveEdit.emit()
            self.update()
        elif self._mode in ("press_clip", "move"):
            if self._mode == "press_clip" and abs(pos.x() - self._press_x) > 5:
                self._mode = "move"
                self.setCursor(Qt.ClosedHandCursor)
            if self._mode == "move":
                self._insert_idx = self._insert_index_at(self.x2t(pos.x()))
                self.update()

    def mouseReleaseEvent(self, e):
        mode = self._mode
        self._mode = None
        self.unsetCursor()
        if mode in ("trim_l", "trim_r"):
            c = self.tl.by_uid(self._drag_uid)
            if c is not None and self._orig != (c.src_in, c.src_out):
                self.edited.emit("Trim clip")
        elif mode == "move":
            idx = self._insert_idx
            self._insert_idx = None
            cur = self.tl.index_of(self._drag_uid)
            if idx is not None and cur >= 0 and idx not in (cur, cur + 1):
                self.tl.move(self._drag_uid, idx)
                self.edited.emit("Move clip")
        elif mode == "press_clip":
            # plain click on a clip also parks the playhead there
            self._scrub_to(e.position().x())
        self._drag_uid = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        kind, clip = self._hit(e.position())
        if clip is not None:
            self.contextAction.emit("open_source", clip.uid)

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        dx = e.angleDelta().x()
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(1.2 ** (dy / 120.0), e.position().x())
        else:
            delta = dx if dx else dy
            self.scroll = max(0.0, self.scroll - delta / 120.0 * self.visible_secs() * 0.12)
            self._clamp_scroll()
            self.update()
            self.viewChanged.emit()
        e.accept()

    def leaveEvent(self, e):
        self._hover_edge = None
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.viewChanged.emit()

    def _select(self, uid):
        if uid != self.selected:
            self.selected = uid
            self.selectionChanged.emit(uid)
        self.update()

    def select(self, uid):
        self._select(uid)

    def _context_menu(self, gpos, clip):
        m = QMenu(self)
        acts = [("split", "Split at playhead\tB"), ("duplicate", "Duplicate\tCtrl+D"), None,
                ("speed_50", "Speed 50 %"), ("speed_100", "Speed 100 %"), ("speed_200", "Speed 200 %"), None,
                ("reset", "Reset clip effects"), ("open_source", "Open source in viewer"), None,
                ("delete", "Delete (ripple)\tDel")]
        for a in acts:
            if a is None:
                m.addSeparator()
                continue
            act = m.addAction(a[1])
            act.triggered.connect(lambda _=False, n=a[0], u=clip.uid: self.contextAction.emit(n, u))
        m.exec(gpos)

    # ------------------------------------------------------------------ drag & drop from pool/explorer
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        t = self.x2t(e.position().x())
        pts = self.tl.edit_points()
        self._drop_t = pts[self._insert_index_at(t)] if pts else 0.0
        self.update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_t = None
        self.update()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        t = self._drop_t if self._drop_t is not None else self.tl.duration
        self._drop_t = None
        self.update()
        if files:
            self.mediaDropped.emit(files, t)
            e.acceptProposedAction()


class TimelinePanel(QWidget):
    """Toolbar + timeline view + horizontal scrollbar."""

    actionRequested = Signal(str)

    def __init__(self, timeline: Timeline, media: MediaManager, parent=None):
        super().__init__(parent)
        self.view = TimelineView(timeline, media, self)
        self.sb = QScrollBar(Qt.Horizontal, self)
        self.sb.setFocusPolicy(Qt.NoFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        bar.setSpacing(2)
        title = QLabel("TIMELINE")
        title.setProperty("panelTitle", True)
        bar.addWidget(title)
        B = theme.icon_button
        self.btn_undo = B("undo", "Undo (Ctrl+Z)")
        self.btn_redo = B("redo", "Redo (Ctrl+Y)")
        self.btn_split = B("cut", "Split clip at playhead — razor (B / Ctrl+B)")
        self.btn_delete = B("delete", "Delete selected clip, ripple (Del)")
        self.btn_append = QToolButton(text="Append In→Out")
        self.btn_append.setToolTip("Append the viewer's In→Out range to the end of the timeline (F12)")
        self.btn_insert = QToolButton(text="Insert at playhead")
        self.btn_insert.setToolTip("Insert the viewer's In→Out range at the timeline playhead (F9)")
        self.btn_view = QToolButton(text="Play Timeline")
        self.btn_view.setCheckable(True)
        self.btn_view.setToolTip("Show the edited timeline in the viewer (Tab toggles Source / Timeline)")
        for b in (self.btn_append, self.btn_insert, self.btn_view):
            b.setFocusPolicy(Qt.NoFocus)
        for w in (self.btn_undo, self.btn_redo, self.btn_split, self.btn_delete, self.btn_append,
                  self.btn_insert, self.btn_view):
            bar.addWidget(w)
        bar.addStretch(1)
        self.lbl_info = QLabel("")
        self.lbl_info.setProperty("dim", True)
        bar.addWidget(self.lbl_info)
        bar.addSpacing(8)
        self.btn_zoom_out = B("zoom_out", "Zoom timeline out (Ctrl+wheel)")
        self.btn_zoom_in = B("zoom_in", "Zoom timeline in (Ctrl+wheel)")
        self.btn_fit = B("fit", "Fit timeline (Shift+Z)")
        self.btn_export = QToolButton(text="  Export…  ")
        self.btn_export.setFocusPolicy(Qt.NoFocus)
        self.btn_export.setToolTip("Render the timeline to a file (Ctrl+M)")
        self.btn_export.setStyleSheet(f"QToolButton {{ background: {theme.ACCENT}; color: #141414; "
                                      f"font-weight: 600; padding: 4px 10px; }}")
        for w in (self.btn_zoom_out, self.btn_zoom_in, self.btn_fit):
            bar.addWidget(w)
        bar.addSpacing(6)
        bar.addWidget(self.btn_export)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.sb)
        for b, name in ((self.btn_undo, "undo"), (self.btn_redo, "redo"), (self.btn_split, "split"),
                        (self.btn_delete, "delete"), (self.btn_append, "append"), (self.btn_insert, "insert"),
                        (self.btn_export, "export")):
            b.clicked.connect(lambda _=False, n=name: self.actionRequested.emit(n))
        self.btn_view.clicked.connect(lambda: self.actionRequested.emit("toggle_view"))
        self.btn_zoom_in.clicked.connect(lambda: self.view.zoom(1.5))
        self.btn_zoom_out.clicked.connect(lambda: self.view.zoom(1 / 1.5))
        self.btn_fit.clicked.connect(self.view.zoom_fit)
        self.view.viewChanged.connect(self._sync_sb)
        self.sb.valueChanged.connect(self._on_sb)
        self._sb_lock = False

    def _sync_sb(self):
        v = self.view
        total = v.tl.duration + v.visible_secs() * 0.5
        mx = max(0.0, total - v.visible_secs())
        self._sb_lock = True
        self.sb.setRange(0, int(mx * 1000))
        self.sb.setPageStep(int(v.visible_secs() * 1000))
        self.sb.setValue(int(v.scroll * 1000))
        self._sb_lock = False

    def _on_sb(self, val):
        if not self._sb_lock:
            self.view.set_scroll(val / 1000.0)

    def refresh(self):
        self._sync_sb()
        self.view.update()
