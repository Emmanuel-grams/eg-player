"""Main window: player + Resolve-style mini editor (media pool, inspector, timeline, deliver)."""
from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QDialog, QFileDialog, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QSplitter, QToolButton, QVBoxLayout,
                               QWidget)

import time

from . import fileassoc, paths, theme, updater
from .export_dialog import ExportDialog
from .update_dialog import UpdateDialog, update_source
from .media import MediaInfo, MediaManager
from .mpvwidget import MpvWidget
from .panels import Inspector, MediaPool
from .project import Clip, History, Timeline, fmt_timecode, frame_of, load_project, save_project
from .timeline import TimelinePanel
from .transport import SPEED_PRESETS, TransportBar

SHORTCUTS = """\
PLAYBACK
  Space / K            Play / pause
  ← / →                Previous / next frame (hold to repeat)
  Shift+← / →          Back / forward 1 second
  Ctrl+← / →           Back / forward 5 seconds
  Right-drag on video  Jog frame by frame
  Mouse wheel on bar   Step frames
  J / L                Shuttle reverse / forward (press again: 2×, 4×, 8×)
  Home / End           Start / end
  Page Up / Down       Previous / next video in folder
  [ / ]                Speed −10 % / +10 %
  Ctrl+[ / Ctrl+]      Speed −1 % / +1 %
  \\                    Speed back to 100 %
  M                    Mute      ↑ / ↓  volume (player) or edit points (timeline)
  I / O                Mark In / Out      Alt+I / Alt+O clear
  Ctrl+L               Loop In→Out

DETAIL VIEWING
  Mouse wheel          Zoom at cursor (Ctrl = fine)
  Left-drag            Pan when zoomed      Middle-click  Fit
  + / -                Zoom in / out
  0 / 1 / 2 / 4 / 8    Fit / 100 % / 200 % / 400 % / 800 %
  P                    Pixel Peep: nearest-neighbour, pixel grid, RGB read-out
  Ctrl+I               Media info overlay
  Ctrl+Shift+S         Save the current frame as a lossless PNG
  F / double-click     Fullscreen      Esc  leave fullscreen

EDITOR (Ctrl+E)
  Tab                  Viewer shows Source / Timeline
  F12 / F9             Append / insert In→Out to timeline
  B or Ctrl+B          Split clip at playhead (razor)
  Delete               Delete selected clip (ripple)
  Ctrl+D               Duplicate clip
  Ctrl+Z / Ctrl+Y      Undo / redo
  Drag clip / edges    Move clip / trim (ripple)
  Ctrl+wheel           Zoom timeline      Shift+Z  fit timeline
  Ctrl+M               Export timeline    Ctrl+Shift+E  export source In→Out
  Ctrl+S               Save project
"""


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class MainWindow(QMainWindow):
    def __init__(self, files: list[str] | None = None):
        super().__init__()
        self.qs = QSettings(paths.APP_ID, paths.APP_ID)
        self.setWindowTitle(paths.APP_NAME)
        self.resize(1440, 900)
        self.setAcceptDrops(True)

        self.media = MediaManager(self)
        self.tl = Timeline()
        self.history = History(self.tl)
        self.project_path: str | None = None
        self.dirty = False

        self.mode = "source"
        self.source_path = ""
        self.source_resume: tuple[str, float] | None = None
        self.marks: dict[str, list] = {}
        self.cur_seg = -1
        self.global_speed = 1.0
        self.shuttle = 1.0
        self.reverse = False
        self.base_volume = float(self.qs.value("volume", 100.0, type=float))
        self.show_frames = bool(self.qs.value("show_frames", False, type=bool))
        self.loop_on = False
        self.info_on = False
        self._resume_after_scrub = False
        self._reverse_from = 0.0
        self._fs_restore = None
        self._edl_path = paths.temp_dir() / "timeline.edl"

        # ---------------------------------------------------------------- widgets
        self.viewer = MpvWidget(hwdec=self.qs.value("hwdec", "auto-safe"),
                                quality=self.qs.value("quality", "high"))
        self.viewer.click_to_pause = bool(self.qs.value("click_to_pause", True, type=bool))
        self.transport = TransportBar()
        self.pool = MediaPool()
        self.inspector = Inspector()
        self.tlpanel = TimelinePanel(self.tl, self.media)
        self.tlv = self.tlpanel.view
        self.pool.setMinimumWidth(190)
        self.inspector.setMinimumWidth(270)

        viewer_box = QWidget()
        vb = QVBoxLayout(viewer_box)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(0)
        vb.addWidget(self.viewer, 1)
        vb.addWidget(self.transport)
        self.top_split = QSplitter(Qt.Horizontal)
        self.top_split.addWidget(self.pool)
        self.top_split.addWidget(viewer_box)
        self.top_split.addWidget(self.inspector)
        self.top_split.setStretchFactor(0, 0)
        self.top_split.setStretchFactor(1, 1)
        self.top_split.setStretchFactor(2, 0)
        self.top_split.setSizes([300, 900, 300])
        self.top_split.setCollapsible(1, False)
        self.main_split = QSplitter(Qt.Vertical)
        self.main_split.addWidget(self.top_split)
        self.main_split.addWidget(self.tlpanel)
        self.main_split.setStretchFactor(0, 1)
        self.main_split.setStretchFactor(1, 0)
        self.main_split.setSizes([620, 260])
        self.main_split.setCollapsible(0, False)
        self.setCentralWidget(self.main_split)

        self.lbl_media = QLabel("")
        self.lbl_media.setProperty("dim", True)
        self.statusBar().addPermanentWidget(self.lbl_media)
        self.btn_update = QToolButton()
        self.btn_update.setFocusPolicy(Qt.NoFocus)
        self.btn_update.setCursor(Qt.PointingHandCursor)
        self._set_update_badge(None)
        self.btn_update.clicked.connect(self.check_updates)
        self.statusBar().addPermanentWidget(self.btn_update)
        self.statusBar().setSizeGripEnabled(False)
        self._update_info = None
        self._update_dialog = None
        self._updating = False
        self._auto_checker = updater.CheckWorker(self)
        self._auto_checker.finished.connect(self._on_auto_check)

        self._commit_timer = QTimer(self, singleShot=True, interval=500,
                                    timeout=lambda: self._commit("Adjust clip", reload=False))
        self._reload_timer = QTimer(self, singleShot=True, interval=60, timeout=self._reload_timeline)
        self._fs_timer = QTimer(self, singleShot=True, interval=2500, timeout=self._fs_autohide)
        self._info_timer = QTimer(self, interval=500, timeout=self._refresh_info)

        self._build_actions()
        self._connect()
        self._restore_ui()
        self._apply_audio()
        self.transport.set_volume(self.base_volume)
        self._update_title_badge()

        for f in files or []:
            if os.path.isfile(f) and f.lower().endswith(".egproj"):
                self.open_project(f)
                break
        media_files = [f for f in (files or []) if os.path.isfile(f) and not f.lower().endswith(".egproj")]
        if media_files:
            for f in media_files[1:]:
                self.import_media([f])
            self.open_file(media_files[0])
        self.viewer.setFocus()
        QTimer.singleShot(4000, self._auto_update_check)

    # ================================================================ setup
    def _act(self, text, slot, shortcut=None, checkable=False, menu: QMenu | None = None, tip=None):
        a = QAction(text, self)
        if shortcut:
            seqs = shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]
            a.setShortcuts([QKeySequence(s) for s in seqs])
            a.setShortcutContext(Qt.WindowShortcut)
        a.setCheckable(checkable)
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        self.addAction(a)
        if menu is not None:
            menu.addAction(a)
        return a

    def _build_actions(self):
        mb = self.menuBar()
        # ---- File
        m = mb.addMenu("&File")
        self._act("&Open video…", self.open_dialog, "Ctrl+O", menu=m)
        self.recent_menu = m.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        self._act("&Import media to pool…", self.import_dialog, "Ctrl+Shift+I", menu=m)
        m.addSeparator()
        self._act("&New project", self.new_project, "Ctrl+N", menu=m)
        self._act("Open &project…", self.open_project_dialog, "Ctrl+Shift+O", menu=m)
        self._act("&Save project", self.save_project, "Ctrl+S", menu=m)
        self._act("Save project &as…", lambda: self.save_project(ask=True), menu=m)
        m.addSeparator()
        self._act("&Export timeline…", self.export_timeline, "Ctrl+M", menu=m)
        self._act("Export source In→&Out…", self.export_source, "Ctrl+Shift+E", menu=m)
        self._act("Save &frame as PNG", self.snapshot, "Ctrl+Shift+S", menu=m)
        m.addSeparator()
        self._act("E&xit", self.close, "Alt+F4", menu=m)

        # ---- Edit
        m = mb.addMenu("&Edit")
        self._act("&Undo", self.undo, "Ctrl+Z", menu=m)
        self._act("&Redo", self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"], menu=m)
        m.addSeparator()
        self._act("&Split at playhead", self.split, ["B", "Ctrl+B"], menu=m)
        self._act("&Delete clip (ripple)", self.delete_clip, ["Del", "Backspace"], menu=m)
        self._act("D&uplicate clip", self.duplicate_clip, "Ctrl+D", menu=m)
        m.addSeparator()
        self._act("&Append In→Out to timeline", self.append_source, "F12", menu=m)
        self._act("&Insert In→Out at playhead", self.insert_source, "F9", menu=m)
        m.addSeparator()
        self._act("&Fit timeline", self.tlv.zoom_fit, "Shift+Z", menu=m)
        self._act("Zoom timeline in", lambda: self.tlv.zoom(1.5), "Ctrl+=", menu=m)
        self._act("Zoom timeline out", lambda: self.tlv.zoom(1 / 1.5), "Ctrl+-", menu=m)

        # ---- Playback
        m = mb.addMenu("&Playback")
        self._act("&Play / Pause", self.toggle_play, ["Space", "K"], menu=m)
        self._act("&Next frame", lambda: self.step(1), "Right", menu=m)
        self._act("Pre&vious frame", lambda: self.step(-1), "Left", menu=m)
        self._act("Forward 1 s", lambda: self.jump(1.0), "Shift+Right", menu=m)
        self._act("Back 1 s", lambda: self.jump(-1.0), "Shift+Left", menu=m)
        self._act("Forward 5 s", lambda: self.jump(5.0), "Ctrl+Right", menu=m)
        self._act("Back 5 s", lambda: self.jump(-5.0), "Ctrl+Left", menu=m)
        self._act("Shuttle forward", self.shuttle_forward, "L", menu=m)
        self._act("Shuttle reverse", self.shuttle_reverse, "J", menu=m)
        self._act("Go to start", self.go_start, "Home", menu=m)
        self._act("Go to end", self.go_end, "End", menu=m)
        self._act("Up (volume / previous edit)", lambda: self.up_down(-1), "Up")
        self._act("Down (volume / next edit)", lambda: self.up_down(1), "Down")
        m.addSeparator()
        sm = m.addMenu("&Speed")
        self._act("Faster (+10 %)", lambda: self.nudge_speed(10), "]", menu=sm)
        self._act("Slower (−10 %)", lambda: self.nudge_speed(-10), "[", menu=sm)
        self._act("Faster (+1 %)", lambda: self.nudge_speed(1), "Ctrl+]", menu=sm)
        self._act("Slower (−1 %)", lambda: self.nudge_speed(-1), "Ctrl+[", menu=sm)
        self._act("Reset to 100 %", lambda: self.transport.speed.setValue(100), "\\", menu=sm)
        sm.addSeparator()
        for pct in SPEED_PRESETS:
            a = sm.addAction(f"{pct:g} %")
            a.triggered.connect(lambda _=False, v=pct: self.transport.speed.setValue(v))
        self._act("Custom speed…", self._focus_speed, "Ctrl+Shift+P", menu=sm)
        self.act_pitch = self._act("Keep audio pitch at any speed", self._toggle_pitch, checkable=True, menu=m)
        self.act_pitch.setChecked(True)
        m.addSeparator()
        self._act("Mark &In", self.mark_in, "I", menu=m)
        self._act("Mark &Out", self.mark_out, "O", menu=m)
        self._act("Clear In", lambda: self._set_mark(0, None), "Alt+I", menu=m)
        self._act("Clear Out", lambda: self._set_mark(1, None), "Alt+O", menu=m)
        self.act_loop = self._act("&Loop In→Out", self._toggle_loop_action, "Ctrl+L", checkable=True, menu=m)
        m.addSeparator()
        self._act("Previous video in folder", lambda: self.folder_step(-1), "PgUp", menu=m)
        self._act("Next video in folder", lambda: self.folder_step(1), "PgDown", menu=m)
        m.addSeparator()
        self._act("Cycle audio track", lambda: self.viewer.cmd("cycle", "aid"), "Ctrl+Shift+A", menu=m)
        self._act("Cycle subtitles", lambda: self.viewer.cmd("cycle", "sid"), "Ctrl+Shift+U", menu=m)
        self._act("&Mute", lambda: self.viewer.set_mute(not self.viewer.muted), "M", menu=m)

        # ---- View
        m = mb.addMenu("&View")
        self.act_editor = self._act("&Editor (media pool, inspector, timeline)", self.toggle_editor, "Ctrl+E",
                                    checkable=True, menu=m)
        self._act("Viewer: Source / &Timeline", self.toggle_view_mode, "Tab", menu=m)
        self._act("&Fullscreen", self.toggle_fullscreen, ["F", "F11", "Alt+Return"], menu=m)
        self._act("Leave fullscreen", self.leave_fullscreen, "Esc")
        m.addSeparator()
        self._act("Zoom in", lambda: self.viewer.zoom_by(1.25), ["+", "="], menu=m)
        self._act("Zoom out", lambda: self.viewer.zoom_by(0.8), "-", menu=m)
        self._act("Fit to window", self.viewer.zoom_fit, "0", menu=m)
        for key, pct in (("1", 100), ("2", 200), ("4", 400), ("8", 800)):
            self._act(f"Zoom {pct} %", lambda _=False, p=pct: self.viewer.zoom_percent_to(p), key, menu=m)
        self.act_peep = self._act("&Pixel Peep (nearest-neighbour + grid + RGB)", self._toggle_peep_action, "P",
                                  checkable=True, menu=m)
        self.act_grid = self._act("Show pixel grid in Pixel Peep", self._toggle_grid, checkable=True, menu=m)
        self.act_grid.setChecked(True)
        self.act_info = self._act("Media &info overlay", self._toggle_info_action, "Ctrl+I", checkable=True, menu=m)
        m.addSeparator()
        qm = m.addMenu("Rendering &quality")
        g = QActionGroup(self)
        for key, label in (("high", "High (EWA Lanczos sharp — best detail)"), ("balanced", "Balanced (Spline36)"),
                           ("fast", "Fast (bilinear — weak GPUs)")):
            a = qm.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.viewer.quality == key)
            a.triggered.connect(lambda _=False, k=key: self._set_quality(k))
            g.addAction(a)
        hm = m.addMenu("&Hardware decoding")
        g2 = QActionGroup(self)
        cur = self.qs.value("hwdec", "auto-safe")
        for key, label in (("auto-safe", "Auto (GPU, recommended)"), ("auto-copy-safe", "Auto copy-back"),
                           ("no", "Off (software decoding)")):
            a = hm.addAction(label)
            a.setCheckable(True)
            a.setChecked(cur == key)
            a.triggered.connect(lambda _=False, k=key: self._set_hwdec(k))
            g2.addAction(a)
        self.act_click = self._act("Click video to play/pause", self._toggle_click, checkable=True, menu=m)
        self.act_click.setChecked(self.viewer.click_to_pause)
        self.act_frames = self._act("Show frame numbers", self.toggle_time_format, checkable=True, menu=m)
        self.act_frames.setChecked(self.show_frames)

        # ---- Tools / Help
        m = mb.addMenu("&Tools")
        if updater.installed_dir() is None:  # the installer already registers "Open with"
            self._act("Add \"Open with EG Player\" to Explorer", self.register_assoc, menu=m)
            self._act("Remove Explorer integration", self.unregister_assoc, menu=m)
            m.addSeparator()
        self._act("Open snapshot folder", lambda: os.startfile(self._snapshot_dir()), menu=m)
        self._act("Clear thumbnail / waveform cache", self._clear_cache, menu=m)
        m = mb.addMenu("&Help")
        self._act("&Keyboard shortcuts", self.show_shortcuts, "F1", menu=m)
        self._act("Check for &updates…", self.check_updates, menu=m)
        m.addSeparator()
        self._act("&About EG Player", self.show_about, menu=m)

    def _connect(self):
        v, t = self.viewer, self.transport
        v.timeChanged.connect(self._on_time)
        v.durationChanged.connect(lambda d: self._on_time(v.time_pos))
        v.pauseChanged.connect(self._on_pause)
        v.muteChanged.connect(t.set_muted)
        v.fileLoaded.connect(self._on_file_loaded)
        v.fileEnded.connect(self._on_file_ended)
        v.videoParamsChanged.connect(self._on_vparams)
        v.viewChanged.connect(self._on_view_changed)
        v.doubleClicked.connect(self.toggle_fullscreen)
        v.clicked.connect(self.toggle_play)
        v.contextMenuRequested.connect(self._viewer_menu)
        v.filesDropped.connect(self._on_files_dropped)
        v.jogRequested.connect(self.step)
        v.mouseActivity.connect(self._on_mouse_activity)

        t.playToggled.connect(self.toggle_play)
        t.stepRequested.connect(self.step)
        t.goStart.connect(self.go_start)
        t.goEnd.connect(self.go_end)
        t.speedChanged.connect(self._on_speed)
        t.volumeChanged.connect(self._on_volume)
        t.muteToggled.connect(lambda: v.set_mute(not v.muted))
        t.markIn.connect(self.mark_in)
        t.markOut.connect(self.mark_out)
        t.loopToggled.connect(self._set_loop)
        t.zoomIn.connect(lambda: v.zoom_by(1.25))
        t.zoomOut.connect(lambda: v.zoom_by(0.8))
        t.zoomFit.connect(v.zoom_fit)
        t.zoom100.connect(lambda: v.zoom_percent_to(100))
        t.pixelPeepToggled.connect(self._set_peep)
        t.snapshot.connect(self.snapshot)
        t.fullscreen.connect(self.toggle_fullscreen)
        t.infoToggled.connect(self._set_info)
        t.timeFormatToggled.connect(self.toggle_time_format)
        t.seek.seekRequested.connect(self._on_seekbar)
        t.seek.stepRequested.connect(self.step)
        t.seek.scrubStarted.connect(self._scrub_start)
        t.seek.scrubEnded.connect(self._scrub_end)

        self.media.probed.connect(self._on_probed)
        self.media.thumbReady.connect(self._on_thumb)
        self.media.waveReady.connect(lambda p, w: self.tlpanel.refresh())

        self.pool.openRequested.connect(self.open_file)
        self.pool.appendRequested.connect(self.append_whole)
        self.pool.importRequested.connect(self.import_dialog)
        self.pool.filesDropped.connect(self.import_media)

        self.tlv.seekRequested.connect(self._on_timeline_seek)
        self.tlv.selectionChanged.connect(self._on_selection)
        self.tlv.edited.connect(lambda label: self._commit(label))
        self.tlv.liveEdit.connect(self._on_live_edit)
        self.tlv.mediaDropped.connect(self._on_timeline_drop)
        self.tlv.contextAction.connect(self._on_clip_action)
        self.tlpanel.actionRequested.connect(self._on_tl_action)

        self.inspector.changed.connect(self._on_inspector)
        self.inspector.resetRequested.connect(self._reset_clip)

    def _restore_ui(self):
        geo = self.qs.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        st = self.qs.value("split_main")
        if st is not None:
            self.main_split.restoreState(st)
        st = self.qs.value("split_top")
        if st is not None:
            self.top_split.restoreState(st)
        editor = bool(self.qs.value("editor", False, type=bool))
        self.act_editor.setChecked(editor)
        self._set_editor_visible(editor)

    # ================================================================ file handling
    def open_dialog(self):
        start = self.qs.value("last_dir", str(Path.home() / "Videos"))
        files, _ = QFileDialog.getOpenFileNames(self, "Open video", start, paths.media_file_filter())
        if files:
            self.qs.setValue("last_dir", os.path.dirname(files[0]))
            for f in files[1:]:
                self.import_media([f])
            self.open_file(files[0])

    def import_dialog(self):
        start = self.qs.value("last_dir", str(Path.home() / "Videos"))
        files, _ = QFileDialog.getOpenFileNames(self, "Import media", start, paths.media_file_filter())
        if files:
            self.qs.setValue("last_dir", os.path.dirname(files[0]))
            self.import_media(files)

    def import_media(self, files: list[str]):
        for f in self._expand(files):
            if self.pool.add(f):
                self.media.request(f)

    def _expand(self, files: list[str]) -> list[str]:
        out = []
        for f in files:
            if os.path.isdir(f):
                for name in sorted(os.listdir(f), key=_natural_key):
                    p = os.path.join(f, name)
                    if os.path.isfile(p) and paths.is_media_file(p):
                        out.append(p)
            elif os.path.isfile(f):
                out.append(f)
        return out

    def open_file(self, path: str, start: float | None = None):
        if not path or not os.path.exists(path):
            self.viewer.overlay.osd("File not found")
            return
        if path.lower().endswith(".egproj"):
            self.open_project(path)
            return
        self.import_media([path])
        self.mode = "source"
        self.reverse = False
        self.shuttle = 1.0
        self.source_path = path
        self.cur_seg = -1
        self.tlpanel.btn_view.setChecked(False)
        self.viewer.set_prop("play-direction", "forward")
        self.viewer.load(path, start=start, pause=False)
        self._apply_segment(-1)
        self._sync_marks()
        self._update_title_badge()
        self._add_recent(path)
        self.setWindowTitle(f"{Path(path).name} — {paths.APP_NAME}")
        self.transport.seek.ticks = []

    def _on_files_dropped(self, files: list[str]):
        files = self._expand(files)
        if not files:
            return
        proj = [f for f in files if f.lower().endswith(".egproj")]
        if proj:
            self.open_project(proj[0])
            return
        for f in files[1:]:
            self.import_media([f])
        self.open_file(files[0])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self._on_files_dropped([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def folder_step(self, d: int):
        if not self.source_path:
            return
        folder = os.path.dirname(self.source_path)
        try:
            files = sorted((f for f in os.listdir(folder) if paths.is_media_file(f)), key=_natural_key)
        except OSError:
            return
        names = [f.lower() for f in files]
        cur = os.path.basename(self.source_path).lower()
        if cur not in names:
            return
        i = names.index(cur) + d
        if 0 <= i < len(files):
            self.open_file(os.path.join(folder, files[i]))
        else:
            self.viewer.overlay.osd("No more videos in this folder")

    def _add_recent(self, path):
        rec = [p for p in (self.qs.value("recent", []) or []) if p != path]
        if isinstance(rec, str):
            rec = [rec]
        rec.insert(0, path)
        self.qs.setValue("recent", rec[:15])

    def _fill_recent(self):
        self.recent_menu.clear()
        rec = self.qs.value("recent", []) or []
        if isinstance(rec, str):
            rec = [rec]
        for p in rec:
            a = self.recent_menu.addAction(p)
            a.setEnabled(os.path.exists(p))
            a.triggered.connect(lambda _=False, f=p: self.open_file(f))
        if not rec:
            self.recent_menu.addAction("(empty)").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear list", lambda: self.qs.setValue("recent", []))

    # ================================================================ mpv state → UI
    def fps(self) -> float:
        if self.mode == "timeline":
            return self.tlv.fps or 30.0
        return self.viewer.frame_rate()

    def _display_pos_dur(self) -> tuple[float, float]:
        v = self.viewer
        if self.mode == "timeline":
            pos, _ = self.tl.edl_to_timeline(v.time_pos)
            return pos, self.tl.duration
        return v.time_pos, v.duration

    def _on_time(self, t: float):
        if self.mode == "timeline":
            pos, seg = self.tl.edl_to_timeline(t)
            if seg != self.cur_seg:
                self._apply_segment(seg)
            self.tlv.set_playhead(pos, follow=not self.viewer.paused)
            dur = self.tl.duration
        else:
            pos, dur = t, self.viewer.duration
        self.transport.seek.fps = self.fps()
        self.transport.seek.set_state(pos, dur)
        self._update_time_label(pos, dur)

    def _update_time_label(self, pos, dur):
        fps = self.fps()
        txt = f"{fmt_timecode(pos, fps)} / {fmt_timecode(dur, fps)}"
        if self.show_frames:
            txt += f"    F {frame_of(pos, fps):,} / {frame_of(dur, fps):,}"
        self.transport.lbl_time.setText(txt)

    def _on_pause(self, paused: bool):
        self.transport.set_playing(not paused and self.viewer.has_file)
        if paused:
            self.viewer.overlay.update()

    def _on_file_loaded(self, path: str):
        v = self.viewer
        self.cur_seg = -1
        self.transport.set_playing(not v.paused)
        if self.mode == "source":
            chapters = v.prop("chapter-list", []) or []
            self.transport.seek.ticks = [float(c.get("time", 0)) for c in chapters if c.get("time")]
            self._apply_segment(-1)
            self._set_loop(self.loop_on)
        else:
            self.transport.seek.ticks = self.tl.edit_points()[1:-1]
        self._on_time(v.time_pos)

    def _on_file_ended(self, reason: int, msg: str):
        if msg:
            self.viewer.overlay.osd("Cannot play this file", 3)
            self.statusBar().showMessage(msg, 10000)

    def _on_vparams(self):
        v = self.viewer
        vp = v.vparams
        parts = []
        if vp.get("w"):
            parts.append(f"{vp.get('w')}×{vp.get('h')}")
            if vp.get("pixelformat"):
                parts.append(str(vp.get("pixelformat")))
        if v.fps:
            parts.append(f"{v.fps:.3f} fps")
        if v.hwdec_current and v.hwdec_current != "no":
            parts.append(f"GPU decode: {v.hwdec_current}")
        elif v.has_video:
            parts.append("CPU decode")
        self.lbl_media.setText("   ·   ".join(parts))
        self._on_view_changed()
        if self.mode == "source":
            self._update_time_label(v.time_pos, v.duration)

    def _on_view_changed(self):
        v = self.viewer
        self.transport.lbl_zoom.setText("Fit" if v.is_fit() else f"{v.zoom_percent():.0f}%")

    def _update_title_badge(self):
        if self.mode == "timeline":
            self.viewer.overlay.title = f"TIMELINE  ·  {len(self.tl.clips)} clips"
        elif self.source_path and self.act_editor.isChecked():
            self.viewer.overlay.title = f"SOURCE  ·  {Path(self.source_path).name}"
        else:
            self.viewer.overlay.title = ""
        self.viewer.overlay.update()
        self.tlpanel.btn_view.setChecked(self.mode == "timeline")

    # ================================================================ transport
    def toggle_play(self):
        v = self.viewer
        if not v.has_file:
            if self.mode == "timeline" and self.tl.clips:
                self._reload_timeline()
            elif not self.source_path:
                self.open_dialog()
            return
        if self.reverse or self.shuttle != 1.0:
            self._reset_shuttle()
            v.set_paused(True)
            return
        v.toggle_pause()

    def step(self, n: int):
        self._reset_shuttle()
        v = self.viewer
        if v.duration and n > 0 and v.time_pos >= v.duration - 1e-3 and self.mode == "source":
            return
        v.frame_step(n)
        fps = self.fps()
        pos, _ = self._display_pos_dur()
        self.viewer.overlay.osd(f"Frame {frame_of(pos, fps) + n:,}", 0.6)

    def jump(self, dt: float):
        pos, dur = self._display_pos_dur()
        self._seek_display(max(0.0, min(dur, pos + dt)))

    def go_start(self):
        self._seek_display(0.0)

    def go_end(self):
        _, dur = self._display_pos_dur()
        self._seek_display(dur)

    def up_down(self, d: int):
        fw = QApplication.focusWidget()
        if isinstance(fw, QAbstractSpinBox):  # let number boxes keep their arrow keys
            fw.stepBy(-d)
            return
        if self.mode == "timeline":
            pos, _ = self._display_pos_dur()
            pts = self.tl.edit_points()
            if d > 0:
                nxt = [p for p in pts if p > pos + 1e-3]
                if nxt:
                    self._seek_display(nxt[0])
            else:
                prv = [p for p in pts if p < pos - 1e-3]
                if prv:
                    self._seek_display(prv[-1])
        else:
            self.transport.vol.setValue(int(self.base_volume - d * 5))

    def _seek_display(self, t: float, exact: bool = True):
        if self.mode == "timeline":
            self.tlv.set_playhead(t)
            edl_t, floor = self.tl.timeline_to_edl_seek(t)
            self.viewer.seek(edl_t, exact, floor)
        else:
            self.viewer.seek(t, exact)

    def _on_seekbar(self, t: float, final: bool):
        self._seek_display(t)

    def _scrub_start(self):
        self._resume_after_scrub = not self.viewer.paused
        if self._resume_after_scrub:
            self.viewer.set_paused(True)

    def _scrub_end(self):
        if self._resume_after_scrub:
            self.viewer.set_paused(False)
        self._resume_after_scrub = False

    def _on_speed(self, factor: float):
        self.global_speed = factor
        self._apply_speed()
        self.viewer.overlay.osd(f"Speed {factor * 100:g} %", 0.8)

    def nudge_speed(self, pct: float):
        self.transport.speed.setValue(max(1.0, min(10000.0, self.transport.speed.value() + pct)))

    def _focus_speed(self):
        self.transport.speed.setFocus()
        self.transport.speed.selectAll()

    def _toggle_pitch(self):
        self.viewer.set_prop("audio-pitch-correction", "yes" if self.act_pitch.isChecked() else "no")

    def _apply_speed(self):
        s = self.global_speed * self.shuttle
        if self.mode == "timeline" and 0 <= self.cur_seg < len(self.tl.clips):
            s *= self.tl.clips[self.cur_seg].speed
        self.viewer.set_speed(s)

    def shuttle_forward(self):
        v = self.viewer
        if self.reverse:
            self.reverse = False
            v.set_prop("play-direction", "forward")
            self.shuttle = 1.0
        elif not v.paused:
            self.shuttle = min(8.0, self.shuttle * 2)
        else:
            self.shuttle = 1.0
        self._apply_speed()
        v.set_paused(False)
        self.viewer.overlay.osd(f"▶ {self.shuttle:g}×", 0.7)

    def shuttle_reverse(self):
        v = self.viewer
        if not v.has_file:
            return
        if not self.reverse:
            self.reverse = True
            self.shuttle = 1.0
            v.set_prop("play-direction", "backward")
            self._reverse_from = v.time_pos
            QTimer.singleShot(2500, self._reverse_watchdog)
        else:
            self.shuttle = min(8.0, self.shuttle * 2)
        self._apply_speed()
        v.set_paused(False)
        self.viewer.overlay.osd(f"◀ {self.shuttle:g}×", 0.7)

    def _reverse_watchdog(self):
        # very long-GOP files (e.g. some 4K HEVC) can't be decoded backwards within mpv's buffer
        if self.reverse and not self.viewer.paused and abs(self.viewer.time_pos - self._reverse_from) < 1e-3:
            self._reset_shuttle()
            self.viewer.set_paused(True)
            self.viewer.overlay.osd("Reverse play not possible for this file — hold ← to step back", 2.5)

    def _reset_shuttle(self):
        if self.reverse:
            self.reverse = False
            self.viewer.set_prop("play-direction", "forward")
        if self.shuttle != 1.0:
            self.shuttle = 1.0
            self._apply_speed()

    def _on_volume(self, v: float):
        self.base_volume = v
        self.qs.setValue("volume", v)
        self._apply_audio()

    def _apply_audio(self):
        vol = self.base_volume
        if self.mode == "timeline" and 0 <= self.cur_seg < len(self.tl.clips):
            vol *= self.tl.clips[self.cur_seg].volume
        self.viewer.set_volume(min(200.0, vol))

    def _apply_segment(self, seg: int):
        """Apply the per-clip speed / volume / colour of timeline clip `seg` (or neutral for -1)."""
        self.cur_seg = seg
        c = self.tl.clips[seg] if self.mode == "timeline" and 0 <= seg < len(self.tl.clips) else None
        if c is not None:
            self.viewer.set_equalizer(c.brightness, c.contrast, c.saturation, c.gamma, c.hue)
        else:
            self.viewer.set_equalizer()
        self._apply_speed()
        self._apply_audio()

    # ================================================================ marks / loop
    def _mark_key(self) -> str:
        return "__timeline__" if self.mode == "timeline" else (self.source_path or "")

    def mark_in(self):
        pos, _ = self._display_pos_dur()
        self._set_mark(0, pos)

    def mark_out(self):
        pos, _ = self._display_pos_dur()
        self._set_mark(1, pos)

    def _set_mark(self, which: int, value):
        key = self._mark_key()
        if not key:
            return
        mk = self.marks.setdefault(key, [None, None])
        mk[which] = value
        if mk[0] is not None and mk[1] is not None and mk[1] < mk[0]:
            mk[0], mk[1] = mk[1], mk[0]
        self._sync_marks()
        fps = self.fps()
        if value is not None:
            self.viewer.overlay.osd(f"{'In' if which == 0 else 'Out'}  {fmt_timecode(value, fps)}", 0.9)

    def _sync_marks(self):
        mk = self.marks.get(self._mark_key(), [None, None])
        self.transport.seek.mark_in, self.transport.seek.mark_out = mk
        self.transport.seek.update()
        self._set_loop(self.loop_on)

    def _set_loop(self, on: bool):
        self.loop_on = on
        for w in (self.transport.btn_loop, self.act_loop):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        if not on:
            self.viewer.set_ab_loop(None, None)
            return
        a, b = self.marks.get(self._mark_key(), [None, None])
        _, dur = self._display_pos_dur()
        a = a or 0.0
        b = b if b is not None else dur
        if self.mode == "timeline":
            a, b = self.tl.timeline_to_edl(a), self.tl.timeline_to_edl(b)
        self.viewer.set_ab_loop(a, b)

    def _toggle_loop_action(self):
        self._set_loop(self.act_loop.isChecked())

    # ================================================================ view
    def _set_peep(self, on: bool):
        self.viewer.set_pixel_peep(on)
        for w in (self.transport.btn_peep, self.act_peep):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        if on:
            self.viewer.overlay.osd("Pixel Peep on — pause to read RGB values", 1.4)

    def _toggle_peep_action(self):
        self._set_peep(self.act_peep.isChecked())

    def _toggle_grid(self):
        self.viewer.overlay.show_grid = self.act_grid.isChecked()
        self.viewer.overlay.update()

    def _set_info(self, on: bool):
        self.info_on = on
        for w in (self.transport.btn_info, self.act_info):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        if on:
            self._refresh_info()
            self._info_timer.start()
        else:
            self._info_timer.stop()
            self.viewer.overlay.set_info("")

    def _toggle_info_action(self):
        self._set_info(self.act_info.isChecked())

    def _refresh_info(self):
        v = self.viewer
        if not v.has_file:
            self.viewer.overlay.set_info("No media loaded")
            return
        g = v.prop
        vp = v.vparams
        lines = [f"File       {Path(self.source_path).name if self.mode == 'source' else 'Timeline (EDL)'}",
                 f"Container  {g('file-format', '?')}"]
        if vp.get("w"):
            lines += [
                f"Video      {g('video-codec', '?')}",
                f"Size       {vp.get('w')}×{vp.get('h')}   display {vp.get('dw')}×{vp.get('dh')}",
                f"Format     {vp.get('pixelformat', '?')}  ({vp.get('hw-pixelformat') or 'sw'})",
                f"Color      {vp.get('colormatrix', '?')} / {vp.get('primaries', '?')} / {vp.get('gamma', '?')}"
                f" / {vp.get('colorlevels', '?')}",
                f"Frame rate {v.fps:.3f} fps   (estimated {float(g('estimated-vf-fps', 0) or 0):.3f})",
                f"Decoder    {v.hwdec_current or 'software'}",
                f"Bitrate    {float(g('video-bitrate', 0) or 0) / 1e6:.2f} Mb/s",
                f"Dropped    {g('frame-drop-count', 0)} frames (decoder {g('decoder-frame-drop-count', 0)})",
                f"Zoom       {v.zoom_percent():.1f} %   scaler {g('scale', '?')}",
            ]
        ap = g("audio-params") or {}
        if ap:
            lines.append(f"Audio      {g('audio-codec-name', '?')}  {ap.get('samplerate', '?')} Hz  "
                         f"{ap.get('channel-count', '?')} ch  {ap.get('format', '')}")
        fps = self.fps()
        pos, dur = self._display_pos_dur()
        lines.append(f"Position   {fmt_timecode(pos, fps)}   frame {frame_of(pos, fps):,}")
        lines.append(f"Speed      {v.speed * 100:g} %")
        self.viewer.overlay.set_info("\n".join(lines))

    def toggle_time_format(self):
        self.show_frames = not self.show_frames
        self.qs.setValue("show_frames", self.show_frames)
        self.act_frames.blockSignals(True)
        self.act_frames.setChecked(self.show_frames)
        self.act_frames.blockSignals(False)
        pos, dur = self._display_pos_dur()
        self._update_time_label(pos, dur)

    def _set_quality(self, q: str):
        self.viewer.set_quality(q)
        self.qs.setValue("quality", q)

    def _set_hwdec(self, mode: str):
        self.viewer.set_hwdec(mode)
        self.qs.setValue("hwdec", mode)

    def _toggle_click(self):
        self.viewer.click_to_pause = self.act_click.isChecked()
        self.qs.setValue("click_to_pause", self.viewer.click_to_pause)

    def _viewer_menu(self, gpos):
        m = QMenu(self)
        m.addAction("Open video…", self.open_dialog)
        m.addAction("Pause" if not self.viewer.paused else "Play", self.toggle_play)
        m.addSeparator()
        z = m.addMenu("Zoom")
        z.addAction("Fit", self.viewer.zoom_fit)
        for pct in (25, 50, 100, 200, 400, 800, 1600, 3200):
            z.addAction(f"{pct} %", lambda p=pct: self.viewer.zoom_percent_to(p))
        m.addAction(self.act_peep)
        m.addAction(self.act_info)
        m.addAction("Save frame as PNG", self.snapshot)
        m.addSeparator()
        if self.source_path and self.mode == "source":
            m.addAction("Append In→Out to timeline", self.append_source)
        m.addAction(self.act_editor)
        m.addAction("Fullscreen", self.toggle_fullscreen)
        m.exec(gpos)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()
            return
        self._fs_restore = (self.act_editor.isChecked(), self.isMaximized())
        self._set_editor_visible(False)
        self.menuBar().hide()
        self.statusBar().hide()
        self.showFullScreen()
        self._fs_timer.start()

    def leave_fullscreen(self):
        if not self.isFullScreen():
            return
        self._fs_timer.stop()
        self.transport.show()
        self.viewer.unsetCursor()
        self.menuBar().show()
        self.statusBar().show()
        editor, maxed = self._fs_restore or (False, False)
        self.showMaximized() if maxed else self.showNormal()
        self._set_editor_visible(editor)

    def _fs_autohide(self):
        if self.isFullScreen() and not self.transport.underMouse():
            self.transport.hide()
            self.viewer.setCursor(Qt.BlankCursor)

    def _on_mouse_activity(self):
        if self.isFullScreen():
            if self.transport.isHidden():
                self.transport.show()
                self.viewer.unsetCursor()
            self._fs_timer.start()

    def toggle_editor(self):
        self._set_editor_visible(self.act_editor.isChecked())
        self.qs.setValue("editor", self.act_editor.isChecked())

    def _set_editor_visible(self, on: bool):
        self.pool.setVisible(on)
        self.inspector.setVisible(on)
        self.tlpanel.setVisible(on)
        self.act_editor.blockSignals(True)
        self.act_editor.setChecked(on)
        self.act_editor.blockSignals(False)
        if on:
            self.tlpanel.refresh()
        self._update_title_badge()

    def snapshot(self):
        v = self.viewer
        if not v.has_video:
            v.overlay.osd("Nothing to capture")
            return
        pos, _ = self._display_pos_dur()
        fps = self.fps()
        stem = Path(self.source_path).stem if self.mode == "source" and self.source_path else "timeline"
        name = f"{stem}_{fmt_timecode(pos, fps).replace(':', '-')}.png"
        out = Path(self._snapshot_dir()) / name
        v.screenshot(str(out))
        v.overlay.osd("Frame saved (lossless PNG)", 1.5)
        self.statusBar().showMessage(f"Saved {out}", 8000)

    def _snapshot_dir(self) -> str:
        d = Path(self.qs.value("snapshot_dir", str(Path.home() / "Pictures" / "EG Player")))
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    # ================================================================ media manager callbacks
    def _on_probed(self, path: str, info: MediaInfo):
        self.pool.update_info(path, info)
        if self.tl.clips and info.fps and self.tl.clips[0].path == path:
            self.tlv.fps = info.fps
        self.tlpanel.refresh()

    def _on_thumb(self, path: str, thumb: str):
        self.pool.set_thumb(path, thumb)
        pm = QPixmap(thumb)
        if not pm.isNull():
            self.tlv.thumbs[path] = pm
            self.tlpanel.refresh()

    # ================================================================ editor: building the timeline
    def _info(self, path: str) -> MediaInfo:
        return self.media.probe_sync(path)

    def _source_range(self) -> tuple[str, float, float] | None:
        path = self.source_path
        if not path:
            return None
        info = self._info(path)
        if not info.ok:
            QMessageBox.warning(self, "Cannot add", f"{Path(path).name}: {info.error or 'unsupported'}")
            return None
        a, b = self.marks.get(path, [None, None])
        a = a or 0.0
        dur = info.duration or self.viewer.duration
        b = b if b is not None else dur
        if b - a <= 1e-3:
            return None
        return path, a, b

    def append_source(self):
        r = self._source_range()
        if r is None:
            return
        self.tl.clips.append(Clip(*r))
        self._commit("Append clip")
        self.tlv.zoom_fit() if len(self.tl.clips) == 1 else None
        self.viewer.overlay.osd("Appended to timeline", 0.9)
        if not self.act_editor.isChecked():
            self.act_editor.setChecked(True)
            self.toggle_editor()

    def insert_source(self):
        r = self._source_range()
        if r is None:
            return
        self.tl.insert_at_time(self.tlv.playhead, Clip(*r))
        self._commit("Insert clip")
        self.viewer.overlay.osd("Inserted at playhead", 0.9)

    def append_whole(self, files: list[str]):
        added = 0
        for f in files:
            info = self._info(f)
            if info.ok and info.duration > 0:
                self.tl.clips.append(Clip(f, 0.0, info.duration))
                added += 1
        if added:
            self._commit("Append clips")
            if len(self.tl.clips) == added:
                self.tlv.zoom_fit()

    def _on_timeline_drop(self, files: list[str], t: float):
        files = self._expand(files)
        self.import_media(files)
        pts = self.tl.edit_points()
        idx = min(range(len(pts)), key=lambda k: abs(pts[k] - t)) if self.tl.clips else 0
        added = 0
        for f in files:
            info = self._info(f)
            if info.ok and info.duration > 0:
                a, b = (self.marks.get(f, [None, None]) if f == self.source_path else (None, None))
                self.tl.insert(idx, Clip(f, a or 0.0, b if b is not None else info.duration))
                idx += 1
                added += 1
        if added:
            first = len(self.tl.clips) == added
            self._commit("Add clips")
            if first:
                self.tlv.zoom_fit()

    def split(self):
        uid = self.tl.split_at(self.tlv.playhead)
        if uid is None:
            self.viewer.overlay.osd("Nothing to split here", 0.8)
            return
        self._commit("Split clip")

    def delete_clip(self):
        uid = self.tlv.selected
        if uid is None:
            return
        i = self.tl.remove(uid)
        self.tlv.select(self.tl.clips[min(i, len(self.tl.clips) - 1)].uid if self.tl.clips else None)
        self._commit("Delete clip")

    def duplicate_clip(self):
        if self.tlv.selected is None:
            return
        uid = self.tl.duplicate(self.tlv.selected)
        if uid:
            self.tlv.select(uid)
            self._commit("Duplicate clip")

    def undo(self):
        label = self.history.undo()
        if label:
            self._after_history(f"Undo {label}")

    def redo(self):
        label = self.history.redo()
        if label:
            self._after_history(f"Redo {label}")

    def _after_history(self, msg):
        if self.tl.by_uid(self.tlv.selected) is None:
            self.tlv.select(None)
        self._set_dirty(True)
        self._refresh_editor()
        self.viewer.overlay.osd(msg, 0.9)
        if self.mode == "timeline":
            self._reload_timer.start()

    def _commit(self, label: str, reload: bool = True):
        was_empty = not self.history._current
        self.history.commit(label)
        if was_empty and self.tl.clips:
            QTimer.singleShot(50, self.tlv.zoom_fit)
        self._set_dirty(True)
        if self.tl.clips:
            info = self.media.get(self.tl.clips[0].path)
            if info and info.fps:
                self.tlv.fps = info.fps
        self._refresh_editor()
        if self.mode == "timeline":
            if reload:
                self._reload_timer.start()
            else:
                self._apply_segment(self.cur_seg)

    def _refresh_editor(self):
        self.tlpanel.refresh()
        clip = self.tl.by_uid(self.tlv.selected)
        self.inspector.show_clip(clip, self._clip_fps(clip))
        n = len(self.tl.clips)
        self.tlpanel.lbl_info.setText(f"{n} clip{'s' if n != 1 else ''}  ·  "
                                      f"{fmt_timecode(self.tl.duration, self.tlv.fps)}" if n else "")
        if self.mode == "timeline":
            self.transport.seek.ticks = self.tl.edit_points()[1:-1]
        self._update_title_badge()

    def _clip_fps(self, clip: Clip | None) -> float:
        if clip is None:
            return self.tlv.fps
        info = self.media.get(clip.path)
        return info.fps if info and info.fps else self.tlv.fps

    def _on_live_edit(self):
        clip = self.tl.by_uid(self.tlv.selected)
        self.inspector.show_clip(clip, self._clip_fps(clip))

    def _on_selection(self, uid):
        clip = self.tl.by_uid(uid)
        self.inspector.show_clip(clip, self._clip_fps(clip))

    def _on_inspector(self, uid: int, key: str, value: float):
        clip = self.tl.by_uid(uid)
        if clip is None:
            return
        setattr(clip, key, value)
        self.tlpanel.refresh()
        if key == "speed":
            self.inspector.show_clip(clip, self._clip_fps(clip))
        if self.mode == "timeline" and self.tl.index_of(uid) == self.cur_seg:
            self._apply_segment(self.cur_seg)
        self._set_dirty(True)
        self._commit_timer.start()

    def _reset_clip(self, uid: int):
        clip = self.tl.by_uid(uid)
        if clip is None:
            return
        for k in ("brightness", "contrast", "saturation", "gamma", "hue", "fade_in", "fade_out"):
            setattr(clip, k, 0.0)
        clip.speed = 1.0
        clip.volume = 1.0
        self._commit("Reset clip", reload=False)

    def _on_clip_action(self, name: str, uid):
        clip = self.tl.by_uid(uid)
        if clip is None:
            return
        if name == "split":
            self.split()
        elif name == "delete":
            self.tlv.select(uid)
            self.delete_clip()
        elif name == "duplicate":
            self.tlv.select(uid)
            self.duplicate_clip()
        elif name.startswith("speed_"):
            clip.speed = int(name.split("_")[1]) / 100.0
            self._commit("Change speed", reload=False)
        elif name == "reset":
            self._reset_clip(uid)
        elif name == "open_source":
            self.marks[clip.path] = [clip.src_in, clip.src_out]
            # match frame: open the source at the frame under the playhead if it's inside this clip
            i, local = self.tl.clip_at(self.tlv.playhead)
            start = clip.src_in + local * clip.speed if i == self.tl.index_of(uid) else clip.src_in
            self.open_file(clip.path, start=start)
            self.viewer.set_paused(True)

    def _on_tl_action(self, name: str):
        {"undo": self.undo, "redo": self.redo, "split": self.split, "delete": self.delete_clip,
         "append": self.append_source, "insert": self.insert_source, "export": self.export_timeline,
         "toggle_view": self.toggle_view_mode}[name]()

    def _on_timeline_seek(self, t: float):
        if self.mode != "timeline" and self.tl.clips:
            self._enter_timeline_mode(t)
            return
        self._seek_display(t)

    # ================================================================ source / timeline viewer modes
    def toggle_view_mode(self):
        if self.mode == "timeline":
            self._enter_source_mode()
        else:
            self._enter_timeline_mode(self.tlv.playhead)

    def _enter_timeline_mode(self, t: float | None = None):
        if not self.tl.clips:
            self.viewer.overlay.osd("The timeline is empty", 1.0)
            self.tlpanel.btn_view.setChecked(False)
            return
        if self.mode == "source" and self.source_path:
            self.source_resume = (self.source_path, self.viewer.time_pos)
        self._reset_shuttle()
        self.mode = "timeline"
        if t is not None:
            self.tlv.playhead = t
        self._reload_timeline(pause=True)
        self._sync_marks()
        self._update_title_badge()
        self.setWindowTitle(f"Timeline — {paths.APP_NAME}")

    def _enter_source_mode(self):
        self.mode = "source"
        self.cur_seg = -1
        if self.source_resume and os.path.exists(self.source_resume[0]):
            path, pos = self.source_resume
            self.source_path = path
            self.viewer.load(path, start=pos, pause=True)
            self.setWindowTitle(f"{Path(path).name} — {paths.APP_NAME}")
        elif self.source_path:
            self.viewer.load(self.source_path, pause=True)
        else:
            self.viewer.stop()
        self._apply_segment(-1)
        self._sync_marks()
        self._update_title_badge()

    def _reload_timeline(self, pause: bool | None = None):
        if self.mode != "timeline":
            return
        if not self.tl.clips:
            self.viewer.stop()
            self.transport.seek.set_state(0, 0)
            self._update_time_label(0, 0)
            return
        self.tl.write_edl(self._edl_path)
        t = min(self.tlv.playhead, max(0.0, self.tl.duration - 1e-3))
        edl_t = self.tl.timeline_to_edl(t)
        if pause is None:
            pause = self.viewer.paused
        self.cur_seg = -1
        self.viewer.load(str(self._edl_path), start=edl_t, pause=pause, keep_view=True)
        self.transport.seek.ticks = self.tl.edit_points()[1:-1]

    # ================================================================ projects
    def new_project(self):
        if not self._maybe_save():
            return
        self.tl.clips = []
        self.history.reset()
        self.project_path = None
        self._set_dirty(False)
        self.tlv.select(None)
        if self.mode == "timeline":
            self._enter_source_mode()
        self._refresh_editor()

    def open_project_dialog(self):
        if not self._maybe_save():
            return
        start = self.qs.value("project_dir", str(Path.home() / "Videos"))
        path, _ = QFileDialog.getOpenFileName(self, "Open project", start, "EG Player project (*.egproj)")
        if path:
            self.open_project(path)

    def open_project(self, path: str):
        try:
            clips, media, settings = load_project(path)
        except Exception as e:
            QMessageBox.warning(self, "Open project", f"Could not open project:\n{e}")
            return
        self.qs.setValue("project_dir", os.path.dirname(path))
        self.import_media([m for m in media if os.path.exists(m)])
        self.import_media([c.path for c in clips if os.path.exists(c.path)])
        for c in clips:
            self.media.probe_sync(c.path) if os.path.exists(c.path) else None
        self.tl.clips = clips
        self.history.reset()
        self.project_path = path
        self._set_dirty(False)
        if not self.act_editor.isChecked():
            self.act_editor.setChecked(True)
            self.toggle_editor()
        self._refresh_editor()
        self.tlv.zoom_fit()
        missing = [c.path for c in clips if not os.path.exists(c.path)]
        if missing:
            QMessageBox.warning(self, "Open project", "Some media files are missing:\n" + "\n".join(missing[:8]))
        self.statusBar().showMessage(f"Opened project {path}", 5000)

    def save_project(self, ask: bool = False) -> bool:
        path = self.project_path
        if ask or not path:
            start = self.project_path or os.path.join(self.qs.value("project_dir", str(Path.home() / "Videos")),
                                                      "Untitled.egproj")
            path, _ = QFileDialog.getSaveFileName(self, "Save project", start, "EG Player project (*.egproj)")
            if not path:
                return False
        try:
            save_project(path, self.tl, self.pool.paths(), {"fps": self.tlv.fps})
        except OSError as e:
            QMessageBox.warning(self, "Save project", str(e))
            return False
        self.project_path = path
        self.qs.setValue("project_dir", os.path.dirname(path))
        self._set_dirty(False)
        self.statusBar().showMessage(f"Saved {path}", 5000)
        return True

    def _set_dirty(self, d: bool):
        self.dirty = d

    def _maybe_save(self) -> bool:
        if not self.dirty or not self.tl.clips:
            return True
        r = QMessageBox.question(self, paths.APP_NAME, "Save changes to the timeline project?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self.save_project()
        return True

    # ================================================================ export
    def export_timeline(self):
        if not self.tl.clips:
            QMessageBox.information(self, "Export", "The timeline is empty. Add clips first (Ctrl+E opens the "
                                                    "editor; F12 appends the viewer's In→Out range).")
            return
        for c in self.tl.clips:
            self.media.probe_sync(c.path)
        base = Path(self.project_path).stem if self.project_path else "Timeline"
        d = self.qs.value("export/dir", str(Path.home() / "Videos"))
        self._run_export(copy.deepcopy(self.tl.clips), os.path.join(d, f"{base}.mkv"), "Timeline")

    def export_source(self):
        r = self._source_range()
        if r is None:
            QMessageBox.information(self, "Export", "Open a video first, then mark In (I) and Out (O).")
            return
        path, a, b = r
        d = self.qs.value("export/dir", os.path.dirname(path))
        self._run_export([Clip(path, a, b)], os.path.join(d, f"{Path(path).stem}_cut.mkv"),
                         f"{Path(path).name}  {fmt_timecode(a, self.fps())} → {fmt_timecode(b, self.fps())}")

    def _run_export(self, clips, default_out, title):
        was_paused = self.viewer.paused
        self.viewer.set_paused(True)
        dlg = ExportDialog(clips, self.media, default_out, self.qs, title, self)
        dlg.exec()
        if not was_paused:
            self.viewer.set_paused(False)

    # ================================================================ updates
    def _set_update_badge(self, info):
        b = self.btn_update
        if info is not None and info.is_newer:
            b.setText(f"  ↑  Update to {info.version}  ")
            b.setToolTip(f"EG Player {info.version} is available (you have {paths.APP_VERSION}). Click to install.")
            b.setStyleSheet(f"QToolButton {{ background: {theme.ACCENT}; color: #141414; font-weight: 600; "
                            f"border-radius: 4px; padding: 1px 6px; }}")
        else:
            b.setText(f"v{paths.APP_VERSION}  ·  Check for updates")
            b.setToolTip("Check for a newer version of EG Player")
            b.setStyleSheet(f"QToolButton {{ color: {theme.TEXT_DIM}; padding: 1px 6px; }}"
                            f"QToolButton:hover {{ color: {theme.TEXT}; }}")

    def check_updates(self):
        if self._update_dialog is not None:
            self._update_dialog.raise_()
            return
        dlg = UpdateDialog(self.qs, self._prepare_update, self._set_updating, self._finish_update, self)
        dlg.available.connect(self._on_update_found)
        self._update_dialog = dlg
        dlg.finished.connect(lambda _=0: setattr(self, "_update_dialog", None))
        dlg.show()

    def _auto_update_check(self):
        # one quiet background check per start (a folder read or a single GitHub API call)
        if bool(self.qs.value("update/auto", True, type=bool)):
            self._auto_checker.start(update_source(self.qs))

    def _on_auto_check(self, info, err):
        if err or info is None:
            return  # stay quiet on startup; the dialog shows errors when asked
        self.qs.setValue("update/last", time.time())
        self._on_update_found(info if info.is_newer else None)
        if info.is_newer:
            self.statusBar().showMessage(f"EG Player {info.version} is available — click “Update” "
                                         f"at the bottom right to install it.", 15000)

    def _on_update_found(self, info):
        self._update_info = info
        self._set_update_badge(info)

    def _prepare_update(self) -> bool:
        if not self._maybe_save():
            return False
        self.dirty = False
        return True

    def _set_updating(self, on: bool):
        self._updating = on  # setup's Restart Manager may now close us without any save prompt
        if on:
            self.viewer.set_paused(True)

    def _finish_update(self):
        self._updating = True
        self.close()

    # ================================================================ misc
    def register_assoc(self):
        try:
            n = fileassoc.register()
        except OSError as e:
            QMessageBox.warning(self, paths.APP_NAME, f"Could not register: {e}")
            return
        QMessageBox.information(self, paths.APP_NAME,
                                f"EG Player now appears under “Open with” for {n} media file types.\n\n"
                                "To make it the default player: right-click a video → Open with → "
                                "Choose another app → EG Player → Always.")

    def unregister_assoc(self):
        fileassoc.unregister()
        QMessageBox.information(self, paths.APP_NAME, "Explorer integration removed.")

    def _clear_cache(self):
        n = 0
        for f in paths.cache_dir().glob("*"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        self.statusBar().showMessage(f"Removed {n} cached files", 4000)

    def show_shortcuts(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard & mouse")
        lay = QVBoxLayout(dlg)
        t = QPlainTextEdit(SHORTCUTS)
        t.setReadOnly(True)
        t.setStyleSheet("font-family: Consolas; font-size: 12px;")
        t.setMinimumSize(QSize(640, 620))
        lay.addWidget(t)
        dlg.exec()

    def show_about(self):
        mpv_ver = self.viewer.prop("mpv-version", "mpv")
        ff_ver = self.viewer.prop("ffmpeg-version", "?")
        QMessageBox.about(self, f"About {paths.APP_NAME}",
                          f"<h3>{paths.APP_NAME} {paths.APP_VERSION}</h3>"
                          "<p>Fast, lightweight video player with frame-accurate scrubbing, "
                          "any-percentage speed control, pixel-level inspection and a mini editor.</p>"
                          f"<p>Playback: {mpv_ver} (libmpv) · FFmpeg {ff_ver}<br>"
                          "UI: Qt for Python (PySide6)</p>"
                          "<p>libmpv and FFmpeg are licensed under the GPL / LGPL.</p>")

    def closeEvent(self, e):
        if not self._updating and not self._maybe_save():
            e.ignore()
            return
        if self.isFullScreen():
            self.leave_fullscreen()
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("split_main", self.main_split.saveState())
        self.qs.setValue("split_top", self.top_split.saveState())
        self.qs.setValue("editor", self.act_editor.isChecked())
        self.media.shutdown()
        self.viewer.shutdown()
        super().closeEvent(e)
