"""Inbox the Blender addon uses to hand a mesh to this app."""

import ctypes
import json
import os
import time
from pathlib import Path

from app.paths import install_root

ROOT = install_root()
HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ScreenshotUtility"
INBOX = HOME / "inbox"
EXPORTS = HOME / "exports"
APP = HOME / "app.json"


def publish():
    _write(APP, {"pid": os.getpid(), "root": str(ROOT)})
    _sweep_exports()


def unpublish():
    current = _read(APP)
    if not current or current.get("pid") != os.getpid():
        return
    current["pid"] = 0
    _write(APP, current)


def take_request():
    """Claim the oldest inbox file. None when the inbox is empty."""
    INBOX.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in INBOX.glob("*.json") if path.is_file())
    if not files:
        return None
    processing = INBOX / "processing"
    processing.mkdir(parents=True, exist_ok=True)
    dest = processing / files[0].name
    try:
        files[0].replace(dest)
    except OSError:
        return None
    try:
        return json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        dest.unlink(missing_ok=True)


def _sweep_exports():
    if not EXPORTS.is_dir():
        return
    cutoff = time.time() - 2 * 24 * 3600
    for path in EXPORTS.glob("*.fbx"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True
