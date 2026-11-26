"""Deliver page: choose a preset, resolution and frame rate, then render with live progress."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QToolButton, QVBoxLayout)

from .exporter import PRESETS, RenderJob, RenderSettings, build_command, preset_by_key, resolve_output_format, validate
from .media import MediaManager
from .project import Clip, fmt_timecode

RESOLUTIONS = [("Source (first clip)", 0, 0), ("3840 × 2160 (4K UHD)", 3840, 2160), ("2560 × 1440", 2560, 1440),
               ("1920 × 1080 (Full HD)", 1920, 1080), ("1280 × 720 (HD)", 1280, 720), ("1080 × 1920 (vertical)", 1080, 1920),
               ("1080 × 1080 (square)", 1080, 1080), ("854 × 480", 854, 480)]
FRAME_RATES = [("Source (first clip)", 0.0), ("23.976", 24000 / 1001), ("24", 24.0), ("25", 25.0),
               ("29.97", 30000 / 1001), ("30", 30.0), ("50", 50.0), ("59.94", 60000 / 1001), ("60", 60.0),
               ("120", 120.0)]


class ExportDialog(QDialog):
    def __init__(self, clips: list[Clip], media: MediaManager, default_path: str, settings, title: str,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Export — {title}")
        self.setMinimumWidth(620)
        self.clips = clips
        self.media = media
        self.qs = settings
        self.job: RenderJob | None = None
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        dur = sum(c.duration for c in clips)
        head = QLabel(f"<b>{title}</b> &nbsp;·&nbsp; {len(clips)} clip{'s' if len(clips) != 1 else ''} "
                      f"&nbsp;·&nbsp; {fmt_timecode(dur, 30)}")
        lay.addWidget(head)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.preset = QComboBox()
        for p in PRESETS:
            self.preset.addItem(p.label, p.key)
        last = self.qs.value("export/preset", "ffv1")
        idx = max(0, self.preset.findData(last))
        self.preset.setCurrentIndex(idx)
        form.addRow("Preset", self.preset)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setProperty("dim", True)
        form.addRow("", self.note)
        self.res = QComboBox()
        for label, w, h in RESOLUTIONS:
            self.res.addItem(label, (w, h))
        self.fps = QComboBox()
        for label, f in FRAME_RATES:
            self.fps.addItem(label, f)
        form.addRow("Resolution", self.res)
        form.addRow("Frame rate", self.fps)
        self.audio = QCheckBox("Include audio")
        self.audio.setChecked(True)
        form.addRow("", self.audio)
        row = QHBoxLayout()
        self.out = QLineEdit(default_path)
        browse = QPushButton("Browse…")
        row.addWidget(self.out, 1)
        row.addWidget(browse)
        form.addRow("Output file", row)
        self.resolved = QLabel()
        self.resolved.setProperty("dim", True)
        form.addRow("", self.resolved)
        lay.addLayout(form)

        self.cmd_toggle = QToolButton(text="▸ FFmpeg command")
        self.cmd_toggle.setCheckable(True)
        lay.addWidget(self.cmd_toggle, 0, Qt.AlignLeft)
        self.cmd = QPlainTextEdit()
        self.cmd.setReadOnly(True)
        self.cmd.setMaximumHeight(130)
        self.cmd.setVisible(False)
        self.cmd.setStyleSheet("font-family: Consolas; font-size: 11px;")
        lay.addWidget(self.cmd)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("Ready")
        lay.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.btn_open = QPushButton("Show in folder")
        self.btn_open.setVisible(False)
        btns.addWidget(self.btn_open)
        btns.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_render = QPushButton("Render")
        self.btn_render.setDefault(True)
        self.btn_render.setProperty("accent", True)
        btns.addWidget(self.btn_close)
        btns.addWidget(self.btn_render)
        lay.addLayout(btns)
        for b in (browse, self.btn_open, self.btn_close):
            b.setAutoDefault(False)

        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.res.currentIndexChanged.connect(self._refresh)
        self.fps.currentIndexChanged.connect(self._refresh)
        self.audio.toggled.connect(self._refresh)
        self.out.textChanged.connect(self._refresh)
        browse.clicked.connect(self._browse)
        self.cmd_toggle.toggled.connect(self._toggle_cmd)
        self.btn_render.clicked.connect(self._render)
        self.btn_close.clicked.connect(self._close)
        self.btn_open.clicked.connect(self._show_in_folder)
        self._preset_changed()

    # ------------------------------------------------------------------
    def _current(self) -> RenderSettings:
        pr = preset_by_key(self.preset.currentData())
        w, h = self.res.currentData()
        return RenderSettings(preset=pr, output=self.out.text().strip(), width=w, height=h,
                              fps=float(self.fps.currentData()), audio=self.audio.isChecked())

    def _preset_changed(self):
        pr = preset_by_key(self.preset.currentData())
        self.note.setText(("✔ Lossless.  " if pr.lossless else "") + pr.note)
        out = self.out.text().strip()
        if out:
            self.out.setText(str(Path(out).with_suffix("." + pr.ext)))
        for w in (self.res, self.fps):
            w.setEnabled(not pr.copy)
        self._refresh()

    def _refresh(self):
        st = self._current()
        if not st.preset.copy:
            W, H, fps, rate, layout, has_audio = resolve_output_format(self.clips, self.media, st)
            a = f", audio {rate} Hz {layout}" if has_audio else ", no audio"
            self.resolved.setText(f"Output: {W}×{H} @ {fps:.3f} fps{a}")
        else:
            self.resolved.setText("Output: streams copied as-is")
        if self.cmd.isVisible():
            self._fill_cmd()

    def _fill_cmd(self):
        st = self._current()
        if not st.output:
            self.cmd.setPlainText("(choose an output file)")
            return
        args, _, _ = build_command(self.clips, self.media, st)
        self.cmd.setPlainText(subprocess.list2cmdline(args))

    def _toggle_cmd(self, on):
        self.cmd_toggle.setText(("▾" if on else "▸") + " FFmpeg command")
        self.cmd.setVisible(on)
        if on:
            self._fill_cmd()
        self.adjustSize()

    def _browse(self):
        pr = preset_by_key(self.preset.currentData())
        start = self.out.text().strip() or str(Path.home() / "Videos" / f"export.{pr.ext}")
        path, _ = QFileDialog.getSaveFileName(self, "Export to", start,
                                              f"{pr.ext.upper()} file (*.{pr.ext});;All files (*.*)")
        if path:
            self.out.setText(path)

    def _set_busy(self, busy: bool):
        for w in (self.preset, self.res, self.fps, self.audio, self.out):
            w.setEnabled(not busy)
        if not busy:
            pr = preset_by_key(self.preset.currentData())
            self.res.setEnabled(not pr.copy)
            self.fps.setEnabled(not pr.copy)
        self.btn_render.setEnabled(not busy)
        self.btn_close.setText("Cancel render" if busy else "Close")

    def _render(self):
        st = self._current()
        if not st.output:
            self._browse()
            st = self._current()
            if not st.output:
                return
        err, warn = validate(self.clips, self.media, st)
        if err:
            QMessageBox.warning(self, "Cannot export", err)
            return
        if warn and QMessageBox.question(self, "Export", warn + "\n\nContinue?") != QMessageBox.Yes:
            return
        out = Path(st.output)
        if any(os.path.abspath(c.path).lower() == str(out.resolve()).lower() for c in self.clips):
            QMessageBox.warning(self, "Cannot export", "The output file is one of the source clips.")
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        self.qs.setValue("export/preset", st.preset.key)
        self.qs.setValue("export/dir", str(out.parent))
        args, total, cleanup = build_command(self.clips, self.media, st)
        self.job = RenderJob(args, total, st.output, cleanup, self)
        self.job.progress.connect(self._on_progress)
        self.job.finished.connect(self._on_finished)
        self._set_busy(True)
        self.btn_open.setVisible(False)
        self.status.setText("Rendering…")
        self.bar.setValue(0)
        self.bar.setFormat("0 %")
        self.job.start()

    def _on_progress(self, frac, eta):
        self.bar.setValue(int(frac * 1000))
        self.bar.setFormat(f"{frac * 100:.1f} %")
        if eta:
            self.status.setText(eta)

    def _on_finished(self, ok, msg):
        self._set_busy(False)
        self.job = None
        self.status.setText(msg)
        self.bar.setFormat("Finished" if ok else "Stopped")
        if ok:
            self.bar.setValue(1000)
            self.btn_open.setVisible(True)

    def _show_in_folder(self):
        out = self.out.text().strip()
        if out and os.path.exists(out):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(out)])

    def _close(self):
        if self.job is not None:
            self.job.cancel()
            return
        self.accept()

    def closeEvent(self, e):
        if self.job is not None:
            if QMessageBox.question(self, "Export", "Cancel the running render?") != QMessageBox.Yes:
                e.ignore()
                return
            self.job.cancel()
        super().closeEvent(e)

    def reject(self):
        if self.job is not None:
            self._close()
            return
        super().reject()
