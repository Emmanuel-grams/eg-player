"""Per-user (HKCU, no admin) "Open with EG Player" registration for video files."""
from __future__ import annotations

import ctypes
import os
import sys
import winreg
from pathlib import Path

from . import paths

PROG_ID = "EGPlayer.Media"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    script = paths.source_root() / "run.pyw"
    return f'"{exe}" "{script}" "%1"'


def icon_path() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}",0'
    ico = paths.source_root() / "assets" / "egplayer.ico"
    return str(ico) if ico.exists() else ""


def _set(root, sub, name, value):
    with winreg.CreateKeyEx(root, sub, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def register() -> int:
    cmd = launch_command()
    base = r"Software\Classes"
    _set(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}", "", "EG Player media file")
    _set(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}", "FriendlyTypeName", "EG Player media file")
    ico = icon_path()
    if ico:
        _set(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}\DefaultIcon", "", ico)
    _set(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}\shell\open", "", "Play with EG Player")
    _set(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}\shell\open\command", "", cmd)
    app_key = rf"{base}\Applications\EGPlayer.exe"
    _set(winreg.HKEY_CURRENT_USER, app_key, "FriendlyAppName", "EG Player")
    _set(winreg.HKEY_CURRENT_USER, rf"{app_key}\shell\open\command", "", cmd)
    n = 0
    for ext in sorted(paths.VIDEO_EXTS | paths.AUDIO_EXTS):
        _set(winreg.HKEY_CURRENT_USER, rf"{base}\.{ext}\OpenWithProgids", PROG_ID, "")
        _set(winreg.HKEY_CURRENT_USER, rf"{app_key}\SupportedTypes", f".{ext}", "")
        n += 1
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:
        pass
    return n


def unregister() -> None:
    base = r"Software\Classes"

    def _delete_tree(root, sub):
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_ALL_ACCESS) as k:
                while True:
                    try:
                        child = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    _delete_tree(root, rf"{sub}\{child}")
            winreg.DeleteKey(root, sub)
        except OSError:
            pass

    _delete_tree(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}")
    _delete_tree(winreg.HKEY_CURRENT_USER, rf"{base}\Applications\EGPlayer.exe")
    for ext in paths.VIDEO_EXTS | paths.AUDIO_EXTS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{base}\.{ext}\OpenWithProgids", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, PROG_ID)
        except OSError:
            pass
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass
