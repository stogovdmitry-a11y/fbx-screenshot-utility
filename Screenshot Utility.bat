@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem venv's pythonw.exe is a GUI stub that restarts the console python.exe,
rem and that console is the extra window. Launch the real pythonw instead.
set "BASE_HOME="
for /f "usebackq tokens=1,* delims==" %%A in (".venv\pyvenv.cfg") do (
    set "KEY=%%A"
    set "VAL=%%B"
    if /I "!KEY: =!"=="home" (
        for /f "tokens=* delims= " %%V in ("!VAL!") do set "BASE_HOME=%%V"
    )
)
if not defined BASE_HOME (
    echo Could not find home= in .venv\pyvenv.cfg
    pause
    exit /b 1
)

set "__PYVENV_LAUNCHER__=%~dp0.venv\Scripts\pythonw.exe"
start "" "!BASE_HOME!\pythonw.exe" -m app
