"""Headless proof: fake FBX, two slots, three passes, one sheet."""

import json
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.composite import build_sheet
from app.runner import DEFAULT_BLENDER, inspect_fbx, render_sheet, run_blender

FIXTURE = r"""
import math
import bpy
import sys
from pathlib import Path

out = Path(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.12, segments=48, ring_count=24, location=(0, 0, 0.12))
head = bpy.context.active_object
head.name = "Head"
head_mat = bpy.data.materials.new("Head")
head.data.materials.append(head_mat)
bpy.ops.mesh.primitive_plane_add(size=0.16, location=(0, -0.06, 0.22))
hair = bpy.context.active_object
hair.rotation_euler[0] = math.pi / 2
hair.name = "Hair"
hair_mat = bpy.data.materials.new("Hair")
hair.data.materials.append(hair_mat)
bpy.ops.export_scene.fbx(filepath=str(out), use_mesh_modifiers=True)
"""


def make_textures(folder):
    folder = Path(folder)
    head = Image.new("RGB", (256, 256), (196, 154, 132))
    draw = ImageDraw.Draw(head)
    draw.rectangle((108, 40, 148, 210), fill=(120, 70, 60))
    head.save(folder / "head_base.png")

    rough = Image.new("RGB", (256, 256), (90, 90, 90))
    ImageDraw.Draw(rough).rectangle((0, 0, 128, 256), fill=(210, 210, 210))
    rough.save(folder / "head_rough.png")

    normal = Image.new("RGB", (256, 256), (128, 128, 255))
    ImageDraw.Draw(normal).rectangle((0, 0, 128, 256), fill=(80, 180, 255))
    normal.save(folder / "head_normal.png")

    hair = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(hair).ellipse((48, 36, 208, 220), fill=(196, 164, 92, 255))
    hair.save(folder / "hair_base.png")
    return {
        "Head": {
            "basecolor": str(folder / "head_base.png"),
            "normal": str(folder / "head_normal.png"),
            "roughness": str(folder / "head_rough.png"),
            "metallic": "",
            "ao": "",
            "opacity": "",
            "alpha_from_basecolor": False,
        },
        "Hair": {
            "basecolor": str(folder / "hair_base.png"),
            "normal": "",
            "roughness": "",
            "metallic": "",
            "ao": "",
            "opacity": "",
            "alpha_from_basecolor": True,
        },
    }


def main():
    out_dir = Path(tempfile.mkdtemp(prefix="screenshot-utility-smoke-"))
    fbx = out_dir / "fixture.fbx"
    script = out_dir / "make_fixture.py"
    script.write_text(FIXTURE, encoding="utf-8")
    run_blender(DEFAULT_BLENDER, script, [fbx])
    info_path = out_dir / "inspect.json"
    inspect_fbx(DEFAULT_BLENDER, fbx, info_path)
    info = json.loads(info_path.read_text(encoding="utf-8"))
    print("INSPECT", json.dumps(info, ensure_ascii=False))
    passes = ["basecolor", "normal", "material"]
    frames = 3
    job = {
        "fbx": str(fbx),
        "output_dir": str(out_dir),
        "frames": frames,
        "step_degrees": 120,
        "start_degrees": 0,
        "distance_scale": 1.0,
        "height": 0.0,
        "frame_size": 320,
        "front": "-Y",
        "invert_normal_g": False,
        "passes": passes,
        "materials": make_textures(out_dir),
    }
    job_path = out_dir / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    render_sheet(DEFAULT_BLENDER, job_path, on_line=print)
    sheet = build_sheet(out_dir, passes, frames, out_dir / "sheet.png")
    print("SHEET", sheet)


if __name__ == "__main__":
    main()
