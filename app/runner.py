"""Run Blender 4.5 in the background and collect its log."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLENDER_DIR = ROOT / "blender"
DEFAULT_BLENDER = Path(r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe")
INSPECT_SCRIPT = BLENDER_DIR / "inspect_fbx.py"
RENDER_SCRIPT = BLENDER_DIR / "render_sheet.py"


class BlenderError(RuntimeError):
    pass


def run_blender(blender, script, args, on_line=None, proc_slot=None):
    if not Path(blender).is_file():
        raise BlenderError(f"Blender не найден: {blender}")
    command = [
        str(blender),
        "--background",
        "--factory-startup",
        "--python",
        str(script),
        "--",
        *[str(arg) for arg in args],
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc_slot is not None:
        proc_slot.clear()
        proc_slot.append(proc)
    lines = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        lines.append(line)
        if on_line is not None:
            on_line(line)
    code = proc.wait()
    if proc_slot is not None:
        proc_slot.clear()
    if code != 0:
        tail = "\n".join(lines[-40:])
        raise BlenderError(tail or f"Blender завершился с кодом {code}")
    return lines


def inspect_fbx(blender, fbx_path, json_path, on_line=None, proc_slot=None):
    return run_blender(blender, INSPECT_SCRIPT, [fbx_path, json_path], on_line, proc_slot)


def render_sheet(blender, job_path, on_line=None, proc_slot=None):
    return run_blender(blender, RENDER_SCRIPT, [job_path], on_line, proc_slot)
