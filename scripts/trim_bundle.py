"""Drop Qt and Pillow pieces the utility never loads."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "dist" / "Screenshot Utility" / "_internal"
PYSIDE = ROOT / "PySide6"

DLLS = (
    "opengl32sw.dll",
    "Qt6Quick.dll",
    "Qt6Qml.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Pdf.dll",
    "Qt6OpenGL.dll",
    "Qt6VirtualKeyboard.dll",
    "Qt6Svg.dll",
)
PLUGINS = (
    "plugins/imageformats/qicns.dll",
    "plugins/imageformats/qwbmp.dll",
    "plugins/imageformats/qpdf.dll",
    "plugins/imageformats/qsvg.dll",
    "plugins/iconengines",
    "plugins/tls",
    "translations",
)


def main():
    if not PYSIDE.is_dir():
        raise SystemExit(f"Build folder not found: {PYSIDE}")
    for name in DLLS:
        _remove(PYSIDE / name)
    for name in PLUGINS:
        _remove(PYSIDE / name)
    for path in (ROOT / "PIL").glob("_avif*.pyd"):
        _remove(path)


def _remove(path):
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    print("removed", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
