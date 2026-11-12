@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Create the venv first. See the README.
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --noupx --name "Screenshot Utility" --icon "icons\app.ico" --paths . --add-data "fonts;fonts" --add-data "blender;blender" --add-data "icons\app.png;icons" app\__main__.py
".venv\Scripts\python.exe" scripts\trim_bundle.py
echo.
echo Folder: dist\Screenshot Utility\
