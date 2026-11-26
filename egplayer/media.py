"""Media probing (ffprobe), thumbnails and audio waveform peaks, computed off the UI thread."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import paths

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WAVE_RATE = 100  # peak buckets per second of audio


def _run(args: list[str], timeout: float | None = None, binary: bool = False):
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, text=not binary,
                          encoding=None if binary else "utf-8", errors=None if binary else "replace")


def _fraction(s: str | None) -> float:
    if not s or s in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            b = float(b)
            return float(a) / b if b else 0.0
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class MediaInfo:
    path: str
    name: str = ""
    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    sar: float = 1.0
    rotation: int = 0
    vcodec: str = ""
    pix_fmt: str = ""
    color_space: str = ""
    color_range: str = ""
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0
    size_bytes: int = 0
    container: str = ""
    has_video: bool = False
    has_audio: bool = False
    error: str = ""
    thumb: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error and (self.has_video or self.has_audio)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return d


def probe(path: str) -> MediaInfo:
    info = MediaInfo(path=path, name=Path(path).name)
    try:
        info.size_bytes = os.path.getsize(path)
    except OSError:
        pass
    try:
        r = _run([paths.ffprobe_exe(), "-v", "error", "-print_format", "json",
                  "-show_format", "-show_streams", path], timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        info.error = f"ffprobe failed: {e}"
        return info
    if r.returncode != 0:
        info.error = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "Unsupported file"
        return info
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        info.error = "Could not read media information"
        return info
    fmt = data.get("format", {})
    info.container = fmt.get("format_long_name") or fmt.get("format_name", "")
    info.duration = _fraction(fmt.get("duration"))
    try:
        info.bitrate = int(fmt.get("bit_rate") or 0)
    except ValueError:
        pass
    for st in data.get("streams", []):
        ctype = st.get("codec_type")
        disp = st.get("disposition", {}) or {}
        if ctype == "video" and not info.has_video and not disp.get("attached_pic"):
            info.has_video = True
            info.vcodec = st.get("codec_name", "")
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            info.pix_fmt = st.get("pix_fmt", "")
            info.color_space = st.get("color_space", "") or ""
            info.color_range = st.get("color_range", "") or ("pc" if info.pix_fmt.startswith("yuvj") else "")
            fps = _fraction(st.get("avg_frame_rate"))
            rfps = _fraction(st.get("r_frame_rate"))
            if fps <= 0 or fps > 1000:
                fps = rfps
            if rfps and fps and abs(rfps - fps) / rfps < 0.01:
                fps = rfps  # prefer the exact rational rate (e.g. 24000/1001)
            info.fps = fps if 0 < fps <= 1000 else 0.0
            sar = st.get("sample_aspect_ratio")
            if sar and sar not in ("0:1", "1:1", "N/A") and ":" in sar:
                a, b = sar.split(":", 1)
                try:
                    info.sar = float(a) / float(b) if float(b) else 1.0
                except ValueError:
                    pass
            for sd in st.get("side_data_list", []) or []:
                if "rotation" in sd:
                    try:
                        info.rotation = int(round(float(sd["rotation"]))) % 360
                    except (TypeError, ValueError):
                        pass
            if not info.duration:
                info.duration = _fraction(st.get("duration"))
        elif ctype == "audio" and not info.has_audio:
            info.has_audio = True
            info.acodec = st.get("codec_name", "")
            info.sample_rate = int(st.get("sample_rate") or 0)
            info.channels = int(st.get("channels") or 0)
            if not info.duration:
                info.duration = _fraction(st.get("duration"))
    if not info.has_video and not info.has_audio:
        info.error = "No playable video or audio streams"
    if info.has_video and info.fps <= 0:
        info.fps = 30.0
    return info


def _cache_key(path: str, extra: str = "") -> str:
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{extra}"
    except OSError:
        sig = f"{path}|{extra}"
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:20]


def make_thumbnail(info: MediaInfo, width: int = 192) -> str:
    if not info.has_video:
        return ""
    out = paths.cache_dir() / f"{_cache_key(info.path, f'thumb{width}')}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    t = min(max(info.duration * 0.1, 0.0), 10.0) if info.duration else 0.0
    for ss in (t, 0.0):
        try:
            _run([paths.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{ss:.3f}",
                  "-i", info.path, "-frames:v", "1", "-vf", f"scale={width}:-2:flags=bicubic",
                  "-q:v", "3", str(out)], timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.exists() and out.stat().st_size > 0:
            return str(out)
    return ""


def make_waveform(info: MediaInfo) -> bytes:
    """Return peak amplitudes (0..255), WAVE_RATE values per second of source audio."""
    if not info.has_audio or info.duration <= 0:
        return b""
    out = paths.cache_dir() / f"{_cache_key(info.path, 'wave2')}.peaks"
    if out.exists() and out.stat().st_size > 0:
        return out.read_bytes()
    rate = 8000 if info.duration < 1800 else 2000
    per = rate // WAVE_RATE
    try:
        proc = subprocess.Popen([paths.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", info.path,
                                 "-vn", "-sn", "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except OSError:
        return b""
    peaks = bytearray()
    chunk_bytes = per * 2 * 4096
    leftover = b""
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        buf = leftover + buf
        usable = len(buf) - (len(buf) % (per * 2))
        leftover = buf[usable:]
        a = array("h")
        a.frombytes(buf[:usable])
        for i in range(0, len(a), per):
            seg = a[i:i + per]
            m = max(max(seg), -min(seg))
            peaks.append(min(255, m >> 7))
    proc.wait()
    # normalise so quiet recordings still show a readable shape
    if peaks:
        top = max(peaks) or 1
        if top < 200:
            scale = 230.0 / top
            peaks = bytearray(min(255, int(p * scale)) for p in peaks)
        try:
            out.write_bytes(bytes(peaks))
        except OSError:
            pass
    return bytes(peaks)


class MediaManager(QObject):
    """Caches MediaInfo per path and produces thumbnails/waveforms in a thread pool."""

    probed = Signal(str, object)        # path, MediaInfo
    thumbReady = Signal(str, str)       # path, thumb file
    waveReady = Signal(str, bytes)      # path, peaks

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="media")
        self._lock = threading.Lock()
        self.infos: dict[str, MediaInfo] = {}
        self.waves: dict[str, bytes] = {}
        self._pending: set[str] = set()

    def get(self, path: str) -> MediaInfo | None:
        return self.infos.get(os.path.normcase(os.path.abspath(path)))

    def probe_sync(self, path: str) -> MediaInfo:
        key = os.path.normcase(os.path.abspath(path))
        if key in self.infos:
            return self.infos[key]
        info = probe(path)
        self.infos[key] = info
        return info

    def request(self, path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        self._pool.submit(self._work, path, key)

    def _work(self, path: str, key: str) -> None:
        info = self.infos.get(key) or probe(path)
        self.infos[key] = info
        self.probed.emit(path, info)
        if not info.ok:
            return
        thumb = make_thumbnail(info)
        if thumb:
            info.thumb = thumb
            self.thumbReady.emit(path, thumb)
        peaks = make_waveform(info)
        if peaks:
            self.waves[key] = peaks
            self.waveReady.emit(path, peaks)

    def wave(self, path: str) -> bytes:
        return self.waves.get(os.path.normcase(os.path.abspath(path)), b"")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
