"""Render the app icon (same drawing as at runtime) into assets/egplayer.ico (PNG-compressed ICO)."""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

app = QGuiApplication(sys.argv)
from egplayer.app import draw_icon  # noqa: E402

sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
blobs = []
for s in sizes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    draw_icon(s).toImage().save(buf, "PNG")
    blobs.append(bytes(buf.data()))

out = os.path.join(ROOT, "assets", "egplayer.ico")
os.makedirs(os.path.dirname(out), exist_ok=True)
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for s, data in zip(sizes, blobs):
    entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
with open(out, "wb") as f:
    f.write(header + entries + b"".join(blobs))
draw_icon(256).save(os.path.join(ROOT, "assets", "egplayer.png"))
print("wrote", out)
