"""Self-update: find a newer EG Player installer, download and verify it, then run it.

An update source can be
  * a GitHub repository        "owner/repo", "github:owner/repo" or "https://github.com/owner/repo"
                               (uses the latest Release; attach EGPlayer-Setup-<ver>.exe to it)
  * a manifest URL             "https://example.com/egplayer/latest.json"
  * a folder (local / network) "D:\\EG Player releases" or "\\\\server\\share\\egplayer"
                               (reads latest.json there, or picks the newest EGPlayer-Setup-*.exe)

latest.json: {"version": "1.2.0", "installer": "EGPlayer-Setup-1.2.0.exe", "sha256": "...",
              "notes": "What's new", "date": "2026-11-26", "size": 123456}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import paths
from .version import __version__

INSTALLER_RE = re.compile(r"^EGPlayer-Setup-(\d+(?:\.\d+){0,3})\.exe$", re.I)
HEADERS = {"User-Agent": "EGPlayer-Updater"}
TIMEOUT = 20


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    return tuple(nums + [0] * (4 - len(nums)))


@dataclass
class UpdateInfo:
    version: str
    location: str           # URL or file path of the installer
    sha256: str = ""
    notes: str = ""
    date: str = ""
    size: int = 0
    source: str = ""

    @property
    def is_newer(self) -> bool:
        return parse_version(self.version) > parse_version(__version__)

    @property
    def is_url(self) -> bool:
        return self.location.lower().startswith(("http://", "https://"))


# ---------------------------------------------------------------------------------------- checking
def classify(source: str) -> tuple[str, str]:
    s = (source or "").strip().strip('"')
    if not s:
        raise UpdateError("No update source is set. Open Help → Check for updates → Update settings.")
    m = re.match(r"^(?:github:|https?://(?:www\.)?github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", s, re.I)
    if m:
        return "github", f"{m.group(1)}/{m.group(2)}"
    if re.match(r"^[\w.-]+/[\w.-]+$", s) and not os.path.exists(s):
        return "github", s
    if s.lower().startswith(("http://", "https://")):
        return "url", s
    return "folder", s


def _fetch(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise UpdateError(f"The server answered {e.code} for {url}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise UpdateError(f"Could not connect ({reason}). Check your internet connection.") from e


def _from_manifest(data: dict, base: str, source: str) -> UpdateInfo:
    ver = str(data.get("version", "")).strip()
    inst = str(data.get("installer", "")).strip()
    if not ver or not inst:
        raise UpdateError("latest.json is missing 'version' or 'installer'.")
    if base.lower().startswith(("http://", "https://")):
        loc = urllib.parse.urljoin(base, inst)
    else:
        loc = inst if os.path.isabs(inst) else str(Path(base) / inst)
    return UpdateInfo(ver, loc, str(data.get("sha256", "")).lower(), str(data.get("notes", "")),
                      str(data.get("date", "")), int(data.get("size") or 0), source)


def _check_github(repo: str) -> UpdateInfo:
    try:
        rel = json.loads(_fetch(f"https://api.github.com/repos/{repo}/releases/latest",
                                "application/vnd.github+json"))
    except UpdateError as e:
        if "404" in str(e):
            raise UpdateError(f"No published release was found for GitHub repository {repo}.") from e
        if "403" in str(e) or "429" in str(e):
            raise UpdateError("GitHub's limit for update checks from this network was reached. "
                              "Try again in an hour.") from e
        raise
    assets = rel.get("assets") or []
    inst = next((a for a in assets if INSTALLER_RE.match(a.get("name", ""))), None)
    if inst is None:
        raise UpdateError(f"The latest release of {repo} has no EGPlayer-Setup-<version>.exe attached.")
    version = (rel.get("tag_name") or "").lstrip("vV") or INSTALLER_RE.match(inst["name"]).group(1)
    sha = ""
    digest = inst.get("digest") or ""
    if digest.lower().startswith("sha256:"):
        sha = digest.split(":", 1)[1].lower()
    man = next((a for a in assets if a.get("name") == "latest.json"), None)
    if man is not None and not sha:
        try:
            sha = str(json.loads(_fetch(man["browser_download_url"])).get("sha256", "")).lower()
        except (UpdateError, ValueError):
            pass
    return UpdateInfo(version, inst["browser_download_url"], sha, rel.get("body") or "",
                      (rel.get("published_at") or "")[:10], int(inst.get("size") or 0), f"GitHub · {repo}")


def _check_url(url: str) -> UpdateInfo:
    if url.endswith("/"):
        url += "latest.json"
    try:
        data = json.loads(_fetch(url, "application/json"))
    except ValueError as e:
        raise UpdateError("The update manifest is not valid JSON.") from e
    return _from_manifest(data, url, url)


def _check_folder(folder: str) -> UpdateInfo:
    p = Path(folder)
    if p.is_file() and p.suffix.lower() == ".json":
        p, manifest = p.parent, p
    else:
        manifest = p / "latest.json"
    if not p.is_dir():
        raise UpdateError(f"The update folder was not found:\n{p}")
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"Could not read {manifest}: {e}") from e
        return _from_manifest(data, str(p), str(p))
    found = [(parse_version(m.group(1)), m.group(1), f) for f in p.iterdir()
             if (m := INSTALLER_RE.match(f.name))]
    if not found:
        raise UpdateError(f"No EGPlayer-Setup-<version>.exe found in\n{p}")
    _, ver, f = max(found)
    return UpdateInfo(ver, str(f), size=f.stat().st_size, source=str(p))


def check(source: str) -> UpdateInfo:
    kind, value = classify(source)
    if kind == "github":
        return _check_github(value)
    if kind == "url":
        return _check_url(value)
    return _check_folder(value)


# ---------------------------------------------------------------------------------------- download
def download(info: UpdateInfo, progress=None, cancel: threading.Event | None = None) -> Path:
    dest_dir = paths.temp_dir() / "updates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.urlparse(info.location).path).name if info.is_url else Path(info.location).name
    if not INSTALLER_RE.match(name):
        name = f"EGPlayer-Setup-{info.version}.exe"
    dest = dest_dir / name
    part = dest.with_suffix(".part")
    for old in dest_dir.glob("EGPlayer-Setup-*"):  # previous downloads are no longer needed
        if old.name != dest.name:
            try:
                old.unlink()
            except OSError:
                pass
    h = hashlib.sha256()
    done = 0
    try:
        if info.is_url:
            req = urllib.request.Request(info.location, headers=HEADERS)
            src = urllib.request.urlopen(req, timeout=TIMEOUT)
            total = int(src.headers.get("Content-Length") or info.size or 0)
        else:
            if not os.path.isfile(info.location):
                raise UpdateError(f"Installer not found:\n{info.location}")
            src = open(info.location, "rb")
            total = os.path.getsize(info.location)
        with src, open(part, "wb") as out:
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("Download cancelled.")
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Download failed: {getattr(e, 'reason', e)}") from e
    if info.sha256 and h.hexdigest().lower() != info.sha256.lower():
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded installer failed its SHA-256 check and was discarded.")
    if done < 1024 * 1024:
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded installer is unexpectedly small; aborting.")
    dest.unlink(missing_ok=True)
    part.replace(dest)
    return dest


# ---------------------------------------------------------------------------------------- install
def installed_dir() -> Path | None:
    """Folder of an installed (Inno Setup) copy, or None for source / portable runs."""
    if not getattr(sys, "frozen", False):
        return None
    d = Path(sys.executable).resolve().parent
    return d if any(d.glob("unins*.exe")) else None


def install_scope() -> str:
    d = installed_dir()
    if d is not None:
        for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            pf = os.environ.get(env)
            if pf and str(d).lower().startswith(pf.lower().rstrip("\\") + "\\"):
                return "/ALLUSERS"
    return "/CURRENTUSER"


def launch_installer(installer: Path) -> subprocess.Popen:
    """Run the installer with a progress window only. Its Restart Manager closes this app, it updates the
    files and starts the new version. The returned process exits when setup has finished."""
    args = [str(installer), "/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            install_scope()]
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(args, close_fds=True, creationflags=flags, cwd=str(installer.parent))


# ---------------------------------------------------------------------------------------- Qt workers
class CheckWorker(QObject):
    finished = Signal(object, str)        # UpdateInfo | None, error message

    def start(self, source: str):
        threading.Thread(target=self._run, args=(source,), daemon=True).start()

    def _run(self, source):
        try:
            self.finished.emit(check(source), "")
        except UpdateError as e:
            self.finished.emit(None, str(e))
        except Exception as e:  # never let a bad server response crash the app
            self.finished.emit(None, f"Update check failed: {e}")


class DownloadWorker(QObject):
    progress = Signal(object, object)     # bytes done, bytes total
    finished = Signal(str, str)           # installer path, error message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()

    def start(self, info: UpdateInfo):
        self.cancel_event.clear()
        threading.Thread(target=self._run, args=(info,), daemon=True).start()

    def cancel(self):
        self.cancel_event.set()

    def _run(self, info):
        try:
            path = download(info, lambda d, t: self.progress.emit(d, t), self.cancel_event)
            self.finished.emit(str(path), "")
        except UpdateError as e:
            self.finished.emit("", str(e))
        except Exception as e:
            self.finished.emit("", f"Download failed: {e}")
