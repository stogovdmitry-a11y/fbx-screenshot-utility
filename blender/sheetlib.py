"""Shared bits for the headless Blender scripts. Runs inside Blender 4.5."""

import math
from mathutils import Vector

import bpy


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_fbx(path):
    if not hasattr(bpy.ops.import_scene, "fbx"):
        bpy.ops.preferences.addon_enable(module="io_scene_fbx")
    result = bpy.ops.import_scene.fbx(
        filepath=str(path),
        use_custom_normals=True,
        use_image_search=False,
    )
    if result != {"FINISHED"}:
        raise RuntimeError(f"FBX import failed: {result}")


def mesh_objects():
    return [obj for obj in bpy.data.objects if obj.type == "MESH" and not obj.hide_render]


def triangle_count(meshes):
    """Tessellated triangles. A quad counts as two."""
    total = 0
    for obj in meshes:
        mesh = obj.data
        if mesh is None:
            continue
        for poly in mesh.polygons:
            corners = len(poly.vertices)
            if corners >= 3:
                total += corners - 2
    return total


def ensure_materials():
    """Give every mesh a named slot so the UI has something to assign to."""
    for obj in mesh_objects():
        if len(obj.material_slots) == 0:
            mat = bpy.data.materials.new(f"{obj.name}_Material")
            obj.data.materials.append(mat)
        for index, slot in enumerate(obj.material_slots):
            if slot.material is None:
                slot.material = bpy.data.materials.new(f"{obj.name}_{index}")


def materials_in_order():
    seen = set()
    ordered = []
    for obj in mesh_objects():
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            ordered.append(mat)
    return ordered


def _image_from_socket(socket):
    if socket is None or not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node.type == "TEX_IMAGE":
        return node.image
    if node.type == "NORMAL_MAP":
        color = node.inputs.get("Color")
        if color is not None and color.is_linked:
            src = color.links[0].from_node
            if src.type == "TEX_IMAGE":
                return src.image
    return None


def find_principled(mat):
    if not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def snapshot_material(mat):
    principled = find_principled(mat)
    if principled is None:
        return {"basecolor": None, "normal": None, "roughness": None, "metallic": None, "alpha": False}

    alpha = principled.inputs.get("Alpha")
    return {
        "basecolor": _image_from_socket(principled.inputs.get("Base Color")),
        "normal": _image_from_socket(principled.inputs.get("Normal")),
        "roughness": _image_from_socket(principled.inputs.get("Roughness")),
        "metallic": _image_from_socket(principled.inputs.get("Metallic")),
        "alpha": bool(alpha is not None and alpha.is_linked),
    }


def world_bounds(objects):
    minimum = Vector((math.inf, math.inf, math.inf))
    maximum = Vector((-math.inf, -math.inf, -math.inf))
    any_point = False
    for obj in objects:
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            minimum.x = min(minimum.x, world.x)
            minimum.y = min(minimum.y, world.y)
            minimum.z = min(minimum.z, world.z)
            maximum.x = max(maximum.x, world.x)
            maximum.y = max(maximum.y, world.y)
            maximum.z = max(maximum.z, world.z)
            any_point = True
    if not any_point:
        raise RuntimeError("The FBX has no meshes")
    return minimum, maximum


def load_image(path, colorspace):
    image = bpy.data.images.load(str(path), check_existing=False)
    image.colorspace_settings.name = colorspace
    image.alpha_mode = "STRAIGHT"
    return image


def resolve_image(user_path, existing, colorspace):
    if user_path:
        return load_image(user_path, colorspace)
    if existing is not None:
        try:
            existing.colorspace_settings.name = colorspace
        except (AttributeError, TypeError):
            pass
        return existing
    return None
