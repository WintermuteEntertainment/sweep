"""Where Part B may look: drives, known leftover places, and what to avoid."""
from __future__ import annotations

import os
import platform
import tempfile
import uuid
from pathlib import Path

_DOWNLOADS = "374DE290-123F-4565-9164-39C4925E467B"
_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"


def norm(path: str) -> str:
    """A form of a path that can be compared with another."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def is_inside(path: str, folder: str) -> bool:
    path, folder = norm(path), norm(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _windows_known_folder(folder_id: str) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD),
                        ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        parsed = uuid.UUID(folder_id)
        guid = GUID(parsed.time_low, parsed.time_mid, parsed.time_hi_version,
                    (ctypes.c_ubyte * 8)(*parsed.bytes[8:]))
        shell = ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        pointer = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                      ctypes.byref(pointer)) != 0:
            return None
        try:
            return ctypes.wstring_at(pointer.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.ole32.CoTaskMemFree(pointer)
    except Exception:  # any failure here just means "use the fallback"
        return None


def fixed_drives() -> list[str]:
    """Local, non-removable drives. On Linux and macOS: the root folder."""
    if platform.system() != "Windows":
        return ["/"]
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        mask = kernel.GetLogicalDrives()
        drives = []
        for index in range(26):
            if mask >> index & 1:
                root = f"{chr(65 + index)}:\\"
                if kernel.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    drives.append(root)
        return drives
    except Exception:
        return []


def known_places() -> list[tuple[str, str]]:
    """(label, path) for places where leftovers commonly collect. Only
    places that exist on this machine are returned."""
    home = Path.home()
    system = platform.system()
    found: list[tuple[str, str]] = [("temp folder", tempfile.gettempdir())]
    if system == "Windows":
        downloads = _windows_known_folder(_DOWNLOADS) or str(home / "Downloads")
        desktop = _windows_known_folder(_DESKTOP) or str(home / "Desktop")
        roaming = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
        found += [
            ("Downloads", downloads),
            ("Desktop", desktop),
            ("editor file history", os.path.join(roaming, "Code", "User", "History")),
            ("editor backups", os.path.join(roaming, "Code", "Backups")),
            ("shell history", os.path.join(roaming, "Microsoft", "Windows",
                                           "PowerShell", "PSReadLine")),
            ("AI assistant folder", os.path.join(roaming, "Claude")),
        ]
        for drive in fixed_drives():
            found.append(("Recycle Bin", os.path.join(drive, "$Recycle.Bin")))
    else:
        found += [
            ("Downloads", str(home / "Downloads")),
            ("Desktop", str(home / "Desktop")),
            ("shell history", str(home / ".bash_history")),
            ("shell history", str(home / ".zsh_history")),
        ]
        if system == "Darwin":
            support = home / "Library" / "Application Support"
            found += [
                ("editor file history", str(support / "Code" / "User" / "History")),
                ("Trash", str(home / ".Trash")),
            ]
        else:
            found += [
                ("editor file history", str(home / ".config" / "Code" / "User" / "History")),
                ("Trash", str(home / ".local" / "share" / "Trash")),
            ]
    for name in (".claude", ".codex", ".cursor", ".gemini", ".aider", ".continue"):
        found.append(("AI assistant folder", str(home / name)))
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for label, path in found:
        if os.path.exists(path) and norm(path) not in seen:
            seen.add(norm(path))
            result.append((label, path))
    return result


def system_excludes() -> list[str]:
    """Operating system folders that are never searched."""
    if platform.system() == "Windows":
        root = os.environ.get("SystemRoot") or os.environ.get("windir")
        return [root] if root else []
    return ["/proc", "/sys", "/dev", "/run"]
