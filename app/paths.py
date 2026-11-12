"""Where the app lives in a checkout, and inside a packaged folder."""

import sys
from pathlib import Path


def install_root():
    """Folder the user runs. The repo, or the folder that contains the exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root():
    """Fonts and Blender scripts shipped next to the code."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", install_root()))
    return install_root()
