"""Import an FBX and write material-slot info as JSON. Blender 4.5."""

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sheetlib


def main():
    fbx_path, out_path = sys.argv[sys.argv.index("--") + 1 :]
    sheetlib.clear_scene()
    sheetlib.import_fbx(fbx_path)
    sheetlib.ensure_materials()
    meshes = sheetlib.mesh_objects()
    minimum, maximum = sheetlib.world_bounds(meshes)
    size = maximum - minimum
    materials = []
    for mat in sheetlib.materials_in_order():
        snap = sheetlib.snapshot_material(mat)
        materials.append(
            {
                "name": mat.name,
                "has_basecolor": snap["basecolor"] is not None,
                "has_normal": snap["normal"] is not None,
                "has_roughness": snap["roughness"] is not None,
                "has_metallic": snap["metallic"] is not None,
                "has_alpha": snap["alpha"],
            }
        )
    payload = {
        "materials": materials,
        "mesh_count": len(meshes),
        "size": [round(size.x, 4), round(size.y, 4), round(size.z, 4)],
    }
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT ok", flush=True)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.exit(1)
