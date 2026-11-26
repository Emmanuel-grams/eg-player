"""'Check for updates' dialog: shows the latest version, downloads and runs the installer."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)

from . import updater
from .version import DEFAULT_UPDATE_SOURCE, __version__


# Access denied / virus detected / blocked by policy: what antivirus and Windows security return when
# they stop a new, unsigned program from running.
BLOCKED_ERRORS = {5, 225, 1260, 4551, 4556}
BLOCKED_TEXT = ("Your antivirus or Windows security blocked the installer. It's a new program that "
                "isn't code-signed, so some antivirus products (Avast's CyberCapture, for example) hold it "
                "back to analyse it first. Allow it in your antivirus, or wait until it has finished "
                "checking, then click Download and install again. You can also click Show installer and "
                "run it yourself.")


def update_source(qs) -> str:
    return str(qs.value("update/source", DEFAULT_UPDATE_SOURCE) or DEFAULT_UPDATE_SOURCE)


class UpdateDialog(QDialog):
    available = Signal(object)      # UpdateInfo when a newer version exists, None when up to date
    _launched = Signal(object, str)  # Popen | None, error

    def __init__(self, qs, prepare_install, set_updating, finish_install, parent=None):
        """prepare_install() -> bool runs before downloading (e.g. save prompts).
        set_updating(bool) tells the app to close without prompts while setup runs (its Restart
        Manager closes the app before replacing files). finish_install() closes the app."""
        super().__init__(parent)
        self.qs = qs
        self.prepare_install = prepare_install
        self.set_updating = set_updating
        self.finish_install = finish_install
        self._proc = None
        self._installer: Path | None = None
        self._poll = QTimer(self, interval=1000, timeout=self._poll_installer)
        self._launched.connect(self._on_launched)
        self.info: updater.UpdateInfo | None = None
        self.setWindowTitle("EG Player updates")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        where = updater.installed_dir()
        kind = "installed" if where else "portable copy" if getattr(sys, "frozen", False) else "running from source"
        head = QLabel(f"<span style='font-size:15px; font-weight:600'>EG Player {__version__}</span>"
                      f"&nbsp;&nbsp;<span style='color:#8b8b93'>{kind}</span>")
        lay.addWidget(head)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(160)
        self.notes.setVisible(False)
        lay.addWidget(self.notes)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        grp = QGroupBox("Update source")
        gl = QVBoxLayout(grp)
        row = QHBoxLayout()
        self.src = QLineEdit(update_source(qs))
        self.src.setPlaceholderText("owner/repo on GitHub, https://…/latest.json, or a folder")
        browse = QPushButton("Folder…")
        browse.setAutoDefault(False)
        row.addWidget(self.src, 1)
        row.addWidget(browse)
        gl.addLayout(row)
        hint = QLabel("Where new versions are published: a GitHub repository (owner/repo — attach "
                      "EGPlayer-Setup-&lt;version&gt;.exe to a Release), a link to a latest.json manifest, "
                      "or a local / network folder that holds the installers (release.ps1 writes one).")
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        gl.addWidget(hint)
        self.auto = QCheckBox("Check for updates automatically when EG Player starts")
        self.auto.setChecked(bool(qs.value("update/auto", True, type=bool)))
        gl.addWidget(self.auto)
        lay.addWidget(grp)

        btns = QHBoxLayout()
        self.btn_check = QPushButton("Check now")
        self.btn_show = QPushButton("Show installer")
        self.btn_show.setToolTip("Open the downloaded installer's folder, so you can run it yourself")
        self.btn_show.setVisible(False)
        self.btn_close = QPushButton("Close")
        self.btn_install = QPushButton("Download and install")
        self.btn_install.setProperty("accent", True)
        self.btn_install.setEnabled(False)
        for b in (self.btn_check, self.btn_show, self.btn_close, self.btn_install):
            b.setAutoDefault(False)
        btns.addWidget(self.btn_check)
        btns.addWidget(self.btn_show)
        btns.addStretch(1)
        btns.addWidget(self.btn_close)
        btns.addWidget(self.btn_install)
        lay.addLayout(btns)

        browse.clicked.connect(self._browse)
        self.btn_check.clicked.connect(self.check)
        self.btn_show.clicked.connect(self._show_installer)
        self.btn_close.clicked.connect(self._close)
        self.btn_install.clicked.connect(self._install)
        self.auto.toggled.connect(lambda on: self.qs.setValue("update/auto", on))
        self.src.editingFinished.connect(self._save_source)

        self._checker = updater.CheckWorker(self)
        self._checker.finished.connect(self._on_checked)
        self._downloader = updater.DownloadWorker(self)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.finished.connect(self._on_downloaded)
        self._downloading = False
        QTimer.singleShot(0, self.check)

    # ------------------------------------------------------------------
    def _save_source(self):
        # Only a source the user changed is stored, so a new default in a later version still applies.
        text = self.src.text().strip()
        if not text or text == DEFAULT_UPDATE_SOURCE:
            self.qs.remove("update/source")
        else:
            self.qs.setValue("update/source", text)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Folder with EG Player installers", self.src.text().strip())
        if d:
            self.src.setText(d.replace("/", "\\"))
            self._save_source()
            self.check()

    def _set_status(self, text: str, color: str = ""):
        self.status.setStyleSheet(f"color: {color};" if color else "")
        self.status.setText(text)

    def check(self):
        if self._downloading:
            return
        self._save_source()
        self.btn_install.setEnabled(False)
        self.btn_check.setEnabled(False)
        self.notes.setVisible(False)
        self._set_status("Checking for updates…")
        self._checker.start(self.src.text().strip())

    def _on_checked(self, info, err):
        self.btn_check.setEnabled(True)
        if err:
            self._set_status(err, "#e07070")
            return
        self.qs.setValue("update/last", time.time())
        self.info = info
        if info.is_newer:
            date = f" — released {info.date}" if info.date else ""
            self._set_status(f"<b>Version {info.version} is available</b>{date}. You have {__version__}.<br>"
                             f"<span style='color:#8b8b93'>From {info.source}</span>")
            if info.notes.strip():
                self.notes.setPlainText(info.notes.strip())
                self.notes.setVisible(True)
            self.btn_install.setEnabled(True)
            self.btn_install.setDefault(True)
            self.available.emit(info)
        else:
            self._set_status(f"You're up to date — {__version__} is the latest version "
                             f"(found {info.version} at {info.source}).", "#7fc99a")
            self.available.emit(None)

    def _install(self):
        if self.info is None or not self.prepare_install():
            return
        self._downloading = True
        self.btn_install.setEnabled(False)
        self.btn_check.setEnabled(False)
        self.btn_close.setText("Cancel")
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self._set_status(f"Downloading EG Player {self.info.version}…")
        self._downloader.start(self.info)

    def _on_progress(self, done, total):
        if total:
            self.bar.setValue(int(done / total * 1000))
            self.bar.setFormat(f"{done / 2**20:,.1f} of {total / 2**20:,.1f} MB")
        else:
            self.bar.setFormat(f"{done / 2**20:,.1f} MB")

    def _on_downloaded(self, path, err):
        self._downloading = False
        self.btn_close.setText("Close")
        if err:
            self.bar.setVisible(False)
            self.btn_install.setEnabled(True)
            self.btn_check.setEnabled(True)
            self._set_status(err, "#e07070")
            return
        self.bar.setRange(0, 0)  # busy: Windows scans a fresh installer before it may run
        self._set_status("Starting the installer… (Windows may take a few seconds to scan it)")
        self.btn_close.setEnabled(False)
        self.btn_show.setVisible(False)
        self._installer = Path(path)
        self._downloading = True
        threading.Thread(target=self._launch, args=(Path(path),), daemon=True).start()

    def _launch(self, path: Path):
        try:
            self._launched.emit(updater.launch_installer(path), "")
        except OSError as e:
            if getattr(e, "winerror", None) in BLOCKED_ERRORS:
                self._launched.emit(None, BLOCKED_TEXT)
            else:
                self._launched.emit(None, f"Could not start the installer: {e}")

    def _show_installer(self):
        if self._installer and self._installer.exists():
            subprocess.Popen(f'explorer /select,"{self._installer}"')

    def _on_launched(self, proc, err):
        if err:
            self._downloading = False
            self.btn_close.setEnabled(True)
            self.bar.setVisible(False)
            self.bar.setRange(0, 1000)
            self.btn_install.setEnabled(True)
            self.btn_check.setEnabled(True)
            self.btn_show.setVisible(True)
            self._set_status(err, "#e07070")
            return
        self._proc = proc
        self.set_updating(True)
        self._set_status(f"Installing EG Player {self.info.version}… The app closes and restarts by itself.")
        self._poll.start()

    def _poll_installer(self):
        code = self._proc.poll() if self._proc is not None else 0
        if code is None:
            return  # still installing; setup will close this app before it copies files
        self._poll.stop()
        self._downloading = False
        if code == 0:
            self.finish_install()  # setup finished and already started the new version
            return
        self.set_updating(False)
        self.btn_close.setEnabled(True)
        self.btn_check.setEnabled(True)
        self.btn_install.setEnabled(True)
        self.bar.setVisible(False)
        self.bar.setRange(0, 1000)
        self.btn_show.setVisible(True)
        self._set_status(f"The installer stopped (exit code {code}) and nothing was changed. If your "
                         "antivirus reported the installer, allow it there and try again, or click Show "
                         "installer and run it yourself.", "#e07070")

    def _close(self):
        if self._downloading:
            self._downloader.cancel()
            return
        self._save_source()
        self.accept()

    def reject(self):
        self._close()
