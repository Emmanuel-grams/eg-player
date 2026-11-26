"""Write build/version_info.txt (Windows version resource for EGPlayer.exe) from egplayer/version.py."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "egplayer", "version.py"), encoding="utf-8").read()
ver = re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)
nums = [int(x) for x in re.findall(r"\d+", ver)][:4]
nums += [0] * (4 - len(nums))
t = tuple(nums)
text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'EG Player'),
      StringStruct('FileDescription', 'EG Player'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'EGPlayer'),
      StringStruct('OriginalFilename', 'EGPlayer.exe'),
      StringStruct('ProductName', 'EG Player'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
out = os.path.join(ROOT, "build", "version_info.txt")
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8").write(text)
print(ver)
sys.exit(0)
