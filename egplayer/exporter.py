"""Turn a list of clips into an FFmpeg render (lossless, edit-ready or delivery presets)."""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

from . import paths
from .media import MediaInfo, MediaManager
from .project import Clip


@dataclass
class Preset:
    key: str
    label: str
    ext: str
    vargs: list[str]
    aargs: list[str]
    pix: str | None = None          # forced pixel format, None = keep the source's
    lossless: bool = False
    copy: bool = False
    extra: list[str] = field(default_factory=list)
    note: str = ""


PRESETS: list[Preset] = [
    Preset("ffv1", "Lossless — FFV1 + FLAC (.mkv)", "mkv",
           ["-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1"], ["-c:a", "flac"],
           lossless=True,
           note="Mathematically lossless intra-frame archive codec. Every decoded pixel is kept bit-exact. "
                "Large files; plays in EG Player, VLC, mpv, Resolve."),
    Preset("h264ll", "Lossless — H.264 (qp 0) + FLAC (.mkv)", "mkv",
           ["-c:v", "libx264", "-qp", "0", "-preset", "slow"], ["-c:a", "flac"], lossless=True,
           note="Lossless H.264: bit-exact pixels, much smaller than FFV1. Some hardware players can't "
                "decode lossless H.264."),
    Preset("copy", "Lossless — stream copy, no re-encode (.mkv)", "mkv", [], [], lossless=True, copy=True,
           note="Copies the compressed streams untouched: zero quality loss and very fast. Cuts snap to the "
                "nearest earlier keyframe, clip effects (speed, colour, fades, volume) are ignored, and all "
                "clips must share the same codec and resolution."),
    Preset("prores", "Edit-ready — ProRes 422 HQ + PCM (.mov)", "mov",
           ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"], ["-c:a", "pcm_s24le"],
           pix="yuv422p10le", note="Visually lossless 10-bit intermediate for DaVinci Resolve / Premiere / FCP."),
    Preset("prores4444", "Edit-ready — ProRes 4444 XQ + PCM (.mov)", "mov",
           ["-c:v", "prores_ks", "-profile:v", "5", "-vendor", "apl0"], ["-c:a", "pcm_s24le"],
           pix="yuv444p10le", note="Highest-quality ProRes, full 4:4:4 chroma. Very large files."),
    Preset("h264hq", "High quality — H.264 CRF 14 + AAC 320k (.mp4)", "mp4",
           ["-c:v", "libx264", "-crf", "14", "-preset", "slow"], ["-c:a", "aac", "-b:a", "320k"],
           pix="yuv420p", extra=["-movflags", "+faststart"],
           note="Near-transparent quality that plays everywhere."),
    Preset("h265hq", "High quality — H.265 10-bit CRF 16 + AAC 320k (.mp4)", "mp4",
           ["-c:v", "libx265", "-crf", "16", "-preset", "medium", "-tag:v", "hvc1", "-x265-params", "log-level=error"],
           ["-c:a", "aac", "-b:a", "320k"], pix="yuv420p10le", extra=["-movflags", "+faststart"],
           note="Near-transparent quality at roughly half the size of H.264."),
    Preset("av1", "Efficient — AV1 10-bit (SVT) CRF 24 + AAC (.mp4)", "mp4",
           ["-c:v", "libsvtav1", "-crf", "24", "-preset", "6"], ["-c:a", "aac", "-b:a", "256k"],
           pix="yuv420p10le", extra=["-movflags", "+faststart"], note="Smallest files at high quality."),
    Preset("h264web", "Share — H.264 CRF 20 + AAC 192k (.mp4)", "mp4",
           ["-c:v", "libx264", "-crf", "20", "-preset", "medium"], ["-c:a", "aac", "-b:a", "192k"],
           pix="yuv420p", extra=["-movflags", "+faststart"], note="Good quality, small files for sharing."),
]


def preset_by_key(key: str) -> Preset:
    return next((p for p in PRESETS if p.key == key), PRESETS[0])


def _f(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") if "." in f"{x:.6f}" else f"{x}"


def _atempo_chain(speed: float) -> list[str]:
    out = []
    s = speed
    while s < 0.5:
        out.append("atempo=0.5")
        s /= 0.5
    while s > 100.0:
        out.append("atempo=100")
        s /= 100.0
    if abs(s - 1.0) > 1e-9:
        out.append(f"atempo={s:.6f}")
    return out


_MATRIX = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470", "bt2020nc": "bt2020",
           "bt2020c": "bt2020", "smpte240m": "smpte240m", "fcc": "fcc"}


def _color_filters(c: Clip, info: MediaInfo | None = None) -> list[str]:
    """Reproduce mpv's video equalizer (exactly what the viewer previews) with FFmpeg filters.

    mpv rotates hue / scales saturation on chroma, then per RGB channel applies contrast as a gain,
    brightness as a bias and finally out = in^(1/gamma) with gamma = 8^(g/100).
    """
    out = []
    if c.hue or c.saturation:
        out.append(f"hue=h={-c.hue * 1.8:.4f}:s={(c.saturation + 100) / 100.0:.4f}")
    pix = info.pix_fmt if info is not None else ""
    is_yuv = pix.startswith("yuv")
    if c.contrast or c.brightness:
        # gain/bias on RGB == gain+bias on Y and gain on UV; done in YUV so out-of-gamut values
        # survive exactly like they do in mpv's float pipeline (only the code range is clipped)
        C = (c.contrast + 100) / 100.0
        B = c.brightness / 100.0
        bits = 16 if "16" in pix else 12 if "12" in pix else 10 if "10" in pix else 8
        top = (1 << bits) - 1
        R = "(maxval-minval)"
        y = f"clip(minval+{C:.6f}*(val-minval)+{B:.6f}*{R}\\,0\\,{top})"
        uv = f"clip((minval+maxval)/2+{C:.6f}*(val-(minval+maxval)/2)\\,0\\,{top})"
        out.append(f"lutyuv=y={y}:u={uv}:v={uv}")
    if c.gamma:
        # mpv clamps to [0,1] RGB, then applies out = in^(1/gamma) per channel
        IG = 1.0 / (8.0 ** (c.gamma / 100.0))
        cs = (info.color_space if info else "") or ""
        matrix = _MATRIX.get(cs) or ("bt709" if info is None or info.height >= 720 else "smpte170m")
        rng = "full" if info is not None and info.color_range == "pc" else "limited"
        back = pix if is_yuv else "yuv444p10le"
        e = f"clip(maxval*pow(val/maxval\\,{IG:.6f})\\,0\\,maxval)"
        flags = "accurate_rnd+full_chroma_int+bitexact"
        out.append(f"scale=in_color_matrix={matrix}:in_range={rng}:flags={flags},format=gbrp16le")
        out.append(f"lutrgb=r={e}:g={e}:b={e}")
        out.append(f"scale=out_color_matrix={matrix}:out_range={rng}:flags={flags},format={back}")
    return out


@dataclass
class RenderSettings:
    preset: Preset
    output: str
    width: int = 0       # 0 = source
    height: int = 0
    fps: float = 0.0     # 0 = source
    audio: bool = True


def _display_size(info: MediaInfo | None) -> tuple[int, int]:
    if info is None or not info.has_video:
        return 0, 0
    w, h = info.width, info.height
    if info.sar and abs(info.sar - 1.0) > 1e-3:
        w = int(round(w * info.sar))
    if info.rotation in (90, 270):
        w, h = h, w
    return w, h


def resolve_output_format(clips: list[Clip], media: MediaManager, st: RenderSettings):
    first = next((media.get(c.path) for c in clips if media.get(c.path) and media.get(c.path).has_video), None)
    W, H = st.width, st.height
    if not W or not H:
        W, H = _display_size(first) if first else (1920, 1080)
        W, H = W or 1920, H or 1080
    W -= W % 2
    H -= H % 2
    fps = st.fps or (first.fps if first and first.fps else 30.0)
    rates = {media.get(c.path).sample_rate for c in clips if media.get(c.path) and media.get(c.path).has_audio}
    rate = rates.pop() if len(rates) == 1 else 48000
    rate = rate or 48000
    chans = max([media.get(c.path).channels for c in clips
                 if media.get(c.path) and media.get(c.path).has_audio] or [2])
    layout = "5.1" if chans >= 6 else "stereo"
    has_audio = st.audio and any(media.get(c.path) and media.get(c.path).has_audio for c in clips)
    return W, H, fps, rate, layout, has_audio


def build_command(clips: list[Clip], media: MediaManager, st: RenderSettings) -> tuple[list[str], float, str]:
    """Return (ffmpeg args, expected output duration, temp file to delete or '')."""
    pr = st.preset
    ff = paths.ffmpeg_exe()
    if pr.copy:
        return _build_copy(clips, media, st)
    W, H, fps, rate, layout, has_audio = resolve_output_format(clips, media, st)
    args = [ff, "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    vparts, aparts, labels = [], [], []
    total = 0.0
    for i, c in enumerate(clips):
        info = media.get(c.path)
        args += ["-ss", _f(c.src_in), "-t", _f(c.src_len), "-i", c.path]
        dur = c.duration
        total += dur
        # ---- video
        if info and info.has_video:
            ch = ["setpts=PTS-STARTPTS"]
            if abs(c.speed - 1.0) > 1e-9:
                ch.append(f"setpts=PTS/{c.speed:.6f}")
            if info.sar and abs(info.sar - 1.0) > 1e-3:
                ch.append("scale=trunc(iw*sar/2)*2:ih:flags=lanczos,setsar=1")
            ch += _color_filters(c, info)
            dw, dh = _display_size(info)
            if (dw, dh) != (W, H):
                ch.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos")
                ch.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
            ch.append("setsar=1")
            if abs(c.speed - 1.0) > 1e-9 or not info.fps or abs(info.fps - fps) > 1e-3:
                ch.append(f"fps={fps:.6f}")
            if c.fade_in > 0:
                ch.append(f"fade=t=in:st=0:d={min(c.fade_in, dur):.4f}")
            if c.fade_out > 0:
                fo = min(c.fade_out, dur)
                ch.append(f"fade=t=out:st={max(0.0, dur - fo):.4f}:d={fo:.4f}")
            if pr.pix:
                ch.append(f"format={pr.pix}")
            vparts.append(f"[{i}:v:0]{','.join(ch)}[v{i}]")
        else:
            fmt = f",format={pr.pix}" if pr.pix else ""
            vparts.append(f"color=c=black:s={W}x{H}:r={fps:.6f}:d={dur:.6f},setsar=1{fmt}[v{i}]")
        # ---- audio
        if has_audio:
            if info and info.has_audio:
                ach = ["asetpts=PTS-STARTPTS"] + _atempo_chain(c.speed)
                if abs(c.volume - 1.0) > 1e-9:
                    ach.append(f"volume={c.volume:.4f}")
                if c.fade_in > 0:
                    ach.append(f"afade=t=in:st=0:d={min(c.fade_in, dur):.4f}")
                if c.fade_out > 0:
                    fo = min(c.fade_out, dur)
                    ach.append(f"afade=t=out:st={max(0.0, dur - fo):.4f}:d={fo:.4f}")
                ach.append(f"aresample={rate}")
                ach.append(f"aformat=sample_rates={rate}:channel_layouts={layout}")
                # pad/trim so audio length matches the video exactly
                ach.append(f"apad=whole_dur={dur:.6f},atrim=0:{dur:.6f}")
                aparts.append(f"[{i}:a:0]{','.join(ach)}[a{i}]")
            else:
                aparts.append(f"aevalsrc=0:c={layout}:s={rate}:d={dur:.6f}[a{i}]")
            labels.append(f"[v{i}][a{i}]")
        else:
            labels.append(f"[v{i}]")
    n = len(clips)
    concat = f"{''.join(labels)}concat=n={n}:v=1:a={1 if has_audio else 0}[vout]{'[aout]' if has_audio else ''}"
    graph = ";".join(vparts + aparts + [concat])
    args += ["-filter_complex", graph, "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]"]
    args += pr.vargs
    if has_audio:
        args += pr.aargs
    args += pr.extra
    args += ["-r", f"{fps:.6f}" if abs(fps - round(fps)) > 1e-6 else str(int(round(fps)))]
    args.append(st.output)
    return args, total, ""


def _build_copy(clips: list[Clip], media: MediaManager, st: RenderSettings):
    lst = paths.temp_dir() / f"concat_{int(time.time() * 1000)}.txt"
    lines = ["ffconcat version 1.0"]
    total = 0.0
    for c in clips:
        p = os.path.abspath(c.path).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{p}'")
        lines.append(f"inpoint {c.src_in:.6f}")
        lines.append(f"outpoint {c.src_out:.6f}")
        total += c.src_len
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = [paths.ffmpeg_exe(), "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
            "-nostats", "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0?"]
    if st.audio:
        args += ["-map", "0:a?"]
    args += ["-c", "copy", "-avoid_negative_ts", "make_zero", st.output]
    return args, total, str(lst)


def validate(clips: list[Clip], media: MediaManager, st: RenderSettings) -> tuple[str, str]:
    """Return (error, warning)."""
    if not clips:
        return "The timeline is empty.", ""
    missing = [c.path for c in clips if not os.path.exists(c.path)]
    if missing:
        return f"Missing source file:\n{missing[0]}", ""
    if st.preset.copy:
        sig = set()
        for c in clips:
            i = media.get(c.path)
            if i:
                sig.add((i.vcodec, i.width, i.height, i.acodec))
        if len(sig) > 1:
            return ("Stream copy needs every clip to share the same codec and resolution.\n"
                    "Choose a lossless re-encode preset (FFV1 or H.264 qp 0) instead."), ""
        if any(not c.is_plain() for c in clips):
            return "", "Stream copy ignores speed, colour, volume and fade settings on clips."
    return "", ""


class RenderJob(QObject):
    progress = Signal(float, str)
    finished = Signal(bool, str)

    def __init__(self, args: list[str], total: float, output: str, cleanup: str = "", parent=None):
        super().__init__(parent)
        self.args = args
        self.total = max(total, 1e-3)
        self.output = output
        self.cleanup = cleanup
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self._on_out)
        self.proc.readyReadStandardError.connect(self._on_err)
        self.proc.finished.connect(self._on_done)
        self.proc.errorOccurred.connect(self._on_error)
        self._err = []
        self._buf = b""
        self._t0 = 0.0
        self._cancelled = False

    def start(self):
        self._t0 = time.monotonic()
        self.proc.start(self.args[0], self.args[1:])

    def cancel(self):
        self._cancelled = True
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()

    def _on_out(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for ln in lines:
            ln = ln.strip().decode("ascii", "ignore")
            if ln.startswith("out_time_us=") or ln.startswith("out_time_ms="):
                try:
                    us = int(ln.split("=", 1)[1])
                except ValueError:
                    continue
                frac = max(0.0, min(1.0, us / 1e6 / self.total))
                el = time.monotonic() - self._t0
                eta = ""
                if frac > 0.01:
                    rem = el * (1 - frac) / frac
                    eta = f"{int(rem // 60)}:{int(rem % 60):02d} left"
                speed = (us / 1e6) / el if el > 0 else 0
                self.progress.emit(frac, f"{eta}   ·   {speed:.2f}× realtime" if eta else "")

    def _on_err(self):
        txt = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        self._err.append(txt)

    def _finish_cleanup(self):
        if self.cleanup:
            try:
                os.remove(self.cleanup)
            except OSError:
                pass

    def _on_error(self, err):
        if err == QProcess.FailedToStart:
            self._finish_cleanup()
            self.finished.emit(False, "Could not start FFmpeg. Is bin\\ffmpeg.exe present?")

    def _on_done(self, code, status):
        self._finish_cleanup()
        if self._cancelled:
            try:
                if os.path.exists(self.output):
                    os.remove(self.output)
            except OSError:
                pass
            self.finished.emit(False, "Render cancelled.")
            return
        if code == 0 and os.path.exists(self.output):
            el = time.monotonic() - self._t0
            size = os.path.getsize(self.output) / (1024 * 1024)
            self.progress.emit(1.0, "")
            self.finished.emit(True, f"Done in {el:.1f} s — {size:,.1f} MB")
        else:
            msg = "".join(self._err).strip().splitlines()
            self.finished.emit(False, "FFmpeg failed:\n" + "\n".join(msg[-8:]) if msg else "FFmpeg failed.")
