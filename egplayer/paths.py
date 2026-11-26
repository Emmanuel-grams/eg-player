"""Locations of bundled binaries (libmpv, ffmpeg) and the per-user cache."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .version import __version__ as APP_VERSION  # noqa: E402

APP_NAME = "EG Player"
APP_ID = "EGPlayer"


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bin_dir() -> Path:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "bin")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "bin")
    candidates.append(source_root() / "bin")
    for c in candidates:
        if (c / "libmpv-2.dll").exists():
            return c
    return candidates[-1]


def setup_dll_search_path() -> None:
    """Make libmpv-2.dll and the FFmpeg DLLs findable before `import mpv`."""
    b = str(bin_dir())
    os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(b)
        except OSError:
            pass


def ffmpeg_exe() -> str:
    p = bin_dir() / "ffmpeg.exe"
    return str(p) if p.exists() else "ffmpeg"


def ffprobe_exe() -> str:
    p = bin_dir() / "ffprobe.exe"
    return str(p) if p.exists() else "ffprobe"


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / APP_ID / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_dir() -> Path:
    base = os.environ.get("TEMP") or os.environ.get("TMP") or str(cache_dir())
    d = Path(base) / APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


VIDEO_EXTS = {
    "mp4", "m4v", "mkv", "mk3d", "webm", "mov", "qt", "avi", "wmv", "asf", "flv", "f4v",
    "mpg", "mpeg", "mpe", "m1v", "m2v", "mpv", "ts", "m2ts", "mts", "m2t", "tp", "trp",
    "vob", "evo", "3gp", "3g2", "ogv", "ogm", "ogg", "mxf", "rm", "rmvb", "divx", "xvid",
    "dv", "dvr-ms", "wtv", "y4m", "yuv", "nut", "ivf", "h264", "264", "h265", "265", "hevc",
    "avc", "av1", "obu", "vc1", "mjpeg", "mjpg", "amv", "mod", "tod", "drc", "dav", "nsv",
    "bik", "smk", "roq", "gif", "apng", "swf", "mcf", "hdmov", "rec", "mp4v", "braw",
}
AUDIO_EXTS = {
    "mp3", "flac", "wav", "m4a", "aac", "opus", "oga", "wma", "alac", "aiff", "aif",
    "ape", "wv", "ac3", "eac3", "dts", "mka", "mpc", "tta", "caf", "amr",
}


def is_media_file(path: str | os.PathLike) -> bool:
    ext = Path(path).suffix.lower().lstrip(".")
    return ext in VIDEO_EXTS or ext in AUDIO_EXTS


def media_file_filter() -> str:
    vids = " ".join(f"*.{e}" for e in sorted(VIDEO_EXTS))
    auds = " ".join(f"*.{e}" for e in sorted(AUDIO_EXTS))
    return f"Video files ({vids});;Audio files ({auds});;All files (*.*)"
