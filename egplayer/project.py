"""Editing model: clips on a magnetic (gap-free) timeline, undo history, EDL generation."""
from __future__ import annotations

import copy
import itertools
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

_uid = itertools.count(1)

COLOR_KEYS = ("brightness", "contrast", "saturation", "gamma", "hue")


def fmt_timecode(t: float, fps: float, show_frames: bool = True) -> str:
    if t is None or not math.isfinite(t):
        t = 0.0
    neg = t < 0
    t = abs(t)
    fps = fps if fps and fps > 0 else 30.0
    secs = int(math.floor(t + 1e-6))
    ff = int(round((t - secs) * fps))
    base = int(round(fps))
    if ff >= base:
        secs += 1
        ff = 0
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    sign = "-" if neg else ""
    if show_frames:
        return f"{sign}{h:02d}:{m:02d}:{s:02d}:{ff:02d}"
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def fmt_seconds(t: float) -> str:
    t = max(0.0, t or 0.0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if h >= 1:
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"
    return f"{int(m)}:{s:05.2f}"


def frame_of(t: float, fps: float) -> int:
    return int(round((t or 0.0) * (fps or 30.0)))


@dataclass
class Clip:
    path: str
    src_in: float
    src_out: float
    speed: float = 1.0          # 1.0 = 100 %
    volume: float = 1.0         # 1.0 = 100 %
    brightness: float = 0.0     # -100 .. 100 (mpv units)
    contrast: float = 0.0
    saturation: float = 0.0
    gamma: float = 0.0
    hue: float = 0.0
    fade_in: float = 0.0        # seconds (timeline time)
    fade_out: float = 0.0
    uid: int = field(default_factory=lambda: next(_uid))

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def src_len(self) -> float:
        return max(0.0, self.src_out - self.src_in)

    @property
    def duration(self) -> float:
        return self.src_len / max(self.speed, 1e-6)

    def has_color(self) -> bool:
        return any(abs(getattr(self, k)) > 1e-9 for k in COLOR_KEYS)

    def is_plain(self) -> bool:
        return (abs(self.speed - 1) < 1e-9 and abs(self.volume - 1) < 1e-9 and not self.has_color()
                and self.fade_in <= 0 and self.fade_out <= 0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("uid", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "uid"}
        return cls(**known)


class Timeline:
    def __init__(self):
        self.clips: list[Clip] = []

    # ----- geometry -------------------------------------------------------------------------
    def starts(self) -> list[float]:
        out, t = [], 0.0
        for c in self.clips:
            out.append(t)
            t += c.duration
        return out

    @property
    def duration(self) -> float:
        return sum(c.duration for c in self.clips)

    def index_of(self, uid: int | None) -> int:
        for i, c in enumerate(self.clips):
            if c.uid == uid:
                return i
        return -1

    def by_uid(self, uid: int | None) -> Clip | None:
        i = self.index_of(uid)
        return self.clips[i] if i >= 0 else None

    def clip_at(self, t: float) -> tuple[int, float]:
        """Index of the clip under timeline time t and the local (timeline) offset into it."""
        if not self.clips:
            return -1, 0.0
        acc = 0.0
        for i, c in enumerate(self.clips):
            d = c.duration
            if t < acc + d - 1e-9 or i == len(self.clips) - 1:
                return i, min(max(t - acc, 0.0), d)
            acc += d
        return len(self.clips) - 1, self.clips[-1].duration

    def edit_points(self) -> list[float]:
        pts, t = [0.0], 0.0
        for c in self.clips:
            t += c.duration
            pts.append(t)
        return pts

    # ----- EDL mapping (EDL plays every clip at 1x, in source seconds) ------------------------
    # Each EDL segment is cut EDL_SHAVE short of the clip's out point. That never drops a frame
    # (frames are >= 1 ms apart), but it keeps mpv from skipping the next clip's first frame when
    # a segment runs to the exact end of its file.
    EDL_SHAVE = 0.0005

    @classmethod
    def edl_len(cls, c: Clip) -> float:
        return c.src_len - cls.EDL_SHAVE if c.src_len > 4 * cls.EDL_SHAVE else c.src_len

    def edl_offsets(self) -> list[float]:
        out, t = [], 0.0
        for c in self.clips:
            out.append(round(t, 6))
            t = round(t, 6) + round(self.edl_len(c), 6)
        return out

    def edl_to_timeline(self, e: float) -> tuple[float, int]:
        offs = self.edl_offsets()
        tstarts = self.starts()
        for i in range(len(self.clips) - 1, -1, -1):
            if e >= offs[i] - 1e-6 or i == 0:
                c = self.clips[i]
                local = min(max(0.0, e - offs[i]), c.src_len)
                return tstarts[i] + local / c.speed, i
        return 0.0, -1

    def timeline_to_edl_seek(self, t: float) -> tuple[float, float]:
        """EDL time for timeline time t, plus the start of that clip's segment (a seek floor)."""
        i, local = self.clip_at(t)
        if i < 0:
            return 0.0, 0.0
        off = self.edl_offsets()[i]
        return off + min(local * self.clips[i].speed, self.edl_len(self.clips[i])), off

    def timeline_to_edl(self, t: float) -> float:
        return self.timeline_to_edl_seek(t)[0]

    def write_edl(self, path: Path) -> None:
        lines = ["# mpv EDL v0"]
        for c in self.clips:
            p = os.path.abspath(c.path)
            n = len(p.encode("utf-8"))
            lines.append(f"%{n}%{p},{c.src_in:.6f},{self.edl_len(c):.6f}")
        # mpv only recognises the header with a bare LF, so never let Windows translate newlines
        path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    # ----- editing ---------------------------------------------------------------------------
    def split_at(self, t: float, min_len: float = 1e-3) -> int | None:
        i, local = self.clip_at(t)
        if i < 0:
            return None
        c = self.clips[i]
        if local < min_len or c.duration - local < min_len:
            return None
        cut = c.src_in + local * c.speed
        right = copy.deepcopy(c)
        right.uid = next(_uid)
        right.src_in = cut
        right.fade_in = 0.0
        c.src_out = cut
        c.fade_out = 0.0
        self.clips.insert(i + 1, right)
        return right.uid

    def insert(self, index: int, clip: Clip) -> None:
        index = max(0, min(index, len(self.clips)))
        self.clips.insert(index, clip)

    def insert_at_time(self, t: float, clip: Clip) -> None:
        if not self.clips or t >= self.duration - 1e-6:
            self.clips.append(clip)
            return
        pts = self.edit_points()
        for idx, p in enumerate(pts):
            if abs(p - t) < 1e-4:
                self.clips.insert(idx, clip)
                return
        self.split_at(t)
        pts = self.edit_points()
        idx = min(range(len(pts)), key=lambda k: abs(pts[k] - t))
        self.clips.insert(idx, clip)

    def remove(self, uid: int) -> int:
        i = self.index_of(uid)
        if i >= 0:
            del self.clips[i]
        return i

    def move(self, uid: int, new_index: int) -> None:
        i = self.index_of(uid)
        if i < 0:
            return
        c = self.clips.pop(i)
        if new_index > i:
            new_index -= 1
        self.clips.insert(max(0, min(new_index, len(self.clips))), c)

    def duplicate(self, uid: int) -> int | None:
        i = self.index_of(uid)
        if i < 0:
            return None
        c = copy.deepcopy(self.clips[i])
        c.uid = next(_uid)
        self.clips.insert(i + 1, c)
        return c.uid

    # ----- persistence -----------------------------------------------------------------------
    def snapshot(self) -> list[dict]:
        return [dict(c.to_dict(), uid=c.uid) for c in self.clips]

    def restore(self, snap: list[dict]) -> None:
        self.clips = []
        for d in snap:
            c = Clip.from_dict(d)
            if "uid" in d:
                c.uid = d["uid"]
            self.clips.append(c)


class History:
    """Snapshot-based undo/redo."""

    def __init__(self, timeline: Timeline, limit: int = 200):
        self.tl = timeline
        self.limit = limit
        self.undo_stack: list[tuple[str, list[dict]]] = []
        self.redo_stack: list[tuple[str, list[dict]]] = []
        self._current = timeline.snapshot()

    def commit(self, label: str) -> None:
        snap = self.tl.snapshot()
        if snap == self._current:
            return
        self.undo_stack.append((label, self._current))
        if len(self.undo_stack) > self.limit:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self._current = snap

    def reset(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._current = self.tl.snapshot()

    def undo(self) -> str | None:
        if not self.undo_stack:
            return None
        label, snap = self.undo_stack.pop()
        self.redo_stack.append((label, self._current))
        self._current = snap
        self.tl.restore(copy.deepcopy(snap))
        return label

    def redo(self) -> str | None:
        if not self.redo_stack:
            return None
        label, snap = self.redo_stack.pop()
        self.undo_stack.append((label, self._current))
        self._current = snap
        self.tl.restore(copy.deepcopy(snap))
        return label


PROJECT_VERSION = 1


def save_project(path: str, timeline: Timeline, media: list[str], settings: dict) -> None:
    data = {
        "app": "EG Player",
        "version": PROJECT_VERSION,
        "media": media,
        "timeline": [c.to_dict() for c in timeline.clips],
        "settings": settings,
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_project(path: str) -> tuple[list[Clip], list[str], dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    clips = [Clip.from_dict(d) for d in data.get("timeline", [])]
    return clips, list(data.get("media", [])), dict(data.get("settings", {}))
