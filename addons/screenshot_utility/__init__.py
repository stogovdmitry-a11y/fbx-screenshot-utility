"""Send the open meshes to Screenshot Utility and build the sheet there."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import bpy
from bpy.props import StringProperty

bl_info = {
    "name": "Screenshot Utility",
    "author": "Dmitry Stogov",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "Top bar",
    "description": "Send the current meshes to Screenshot Utility and render a turnaround sheet",
    "category": "Render",
}

HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ScreenshotUtility"
INBOX = HOME / "inbox"
EXPORTS = HOME / "exports"
APP = HOME / "app.json"

_MODES = {
    "EDIT_MESH": "EDIT",
    "EDIT_CURVE": "EDIT",
    "EDIT_SURFACE": "EDIT",
    "EDIT_TEXT": "EDIT",
    "EDIT_ARMATURE": "EDIT",
    "EDIT_METABALL": "EDIT",
    "EDIT_LATTICE": "EDIT",
    "POSE": "POSE",
    "SCULPT": "SCULPT",
    "PAINT_WEIGHT": "WEIGHT_PAINT",
    "PAINT_VERTEX": "VERTEX_PAINT",
    "PAINT_TEXTURE": "TEXTURE_PAINT",
    "PARTICLE": "PARTICLE_EDIT",
}


class ScreenshotUtilityPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    utility_dir: StringProperty(
        name="Utility folder",
        description="Folder with Screenshot Utility.bat. Needed only if the app has never been opened on this machine",
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, _context):
        self.layout.prop(self, "utility_dir")


class SCREENSHOTUTILITY_OT_send(bpy.types.Operator):
    bl_idname = "screenshot_utility.send"
    bl_label = "Build Screen List"
    bl_description = "Export the meshes and render a sheet in Screenshot Utility"
    bl_options = {"REGISTER"}

    def execute(self, context):
        meshes = _meshes(context)
        if not meshes:
            if context.selected_objects:
                self.report({"WARNING"}, "The selection has no visible meshes")
            else:
                self.report({"WARNING"}, "The scene has no visible meshes")
            return {"CANCELLED"}

        EXPORTS.mkdir(parents=True, exist_ok=True)
        INBOX.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fbx = EXPORTS / f"{_safe(_label(meshes))}_{stamp}.fbx"
        try:
            _export(context, meshes, fbx)
        except Exception as exc:
            self.report({"ERROR"}, f"FBX export failed: {exc}")
            return {"CANCELLED"}

        request = INBOX / f"{stamp}.json"
        payload = {"fbx": str(fbx), "render": True, "name": _label(meshes)}
        tmp = request.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(request)

        launched, problem = _ensure_utility()
        if problem:
            self.report({"WARNING"}, problem)
            return {"CANCELLED"}
        if launched:
            self.report({"INFO"}, "Screenshot Utility is opening, the sheet will start")
        else:
            self.report({"INFO"}, "Sent to Screenshot Utility")
        return {"FINISHED"}


def _meshes(context):
    picked = []
    for obj in context.selected_objects:
        for item in (obj, *obj.children_recursive):
            if item.type == "MESH" and item.visible_get() and item not in picked:
                picked.append(item)
    if context.selected_objects:
        return picked
    return [
        obj
        for obj in context.view_layer.objects
        if obj.type == "MESH" and obj.visible_get() and not obj.hide_render
    ]


def _label(meshes):
    if bpy.data.filepath:
        return Path(bpy.data.filepath).stem
    if len(meshes) == 1:
        return meshes[0].name
    return "sheet"


def _safe(name):
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in name).strip("._")
    return cleaned or "sheet"


def _export(context, meshes, filepath):
    if not hasattr(bpy.ops.export_scene, "fbx"):
        bpy.ops.preferences.addon_enable(module="io_scene_fbx")
    previous_mode = context.mode
    view_layer = context.view_layer
    previous = [(obj, obj.select_get()) for obj in view_layer.objects]
    active = view_layer.objects.active
    if previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        for obj in view_layer.objects:
            obj.select_set(False)
        chosen = []
        for obj in meshes:
            try:
                obj.select_set(True)
            except RuntimeError:
                continue
            chosen.append(obj)
        if not chosen:
            raise RuntimeError("none of the meshes could be selected")
        view_layer.objects.active = chosen[0]
        result = bpy.ops.export_scene.fbx(
            filepath=str(filepath),
            use_selection=True,
            object_types={"MESH"},
            use_mesh_modifiers=True,
            use_mesh_modifiers_render=True,
            mesh_smooth_type="FACE",
            use_tspace=True,
            path_mode="COPY",
            embed_textures=True,
            bake_anim=False,
            axis_forward="-Z",
            axis_up="Y",
        )
    finally:
        for obj, selected in previous:
            try:
                obj.select_set(selected)
            except RuntimeError:
                pass
        view_layer.objects.active = active
        restore = _MODES.get(previous_mode)
        if restore:
            try:
                bpy.ops.object.mode_set(mode=restore)
            except RuntimeError:
                pass
    if result != {"FINISHED"}:
        raise RuntimeError(str(set(result)))


def _ensure_utility():
    info = _read_json(APP)
    if info and _pid_alive(info.get("pid")):
        return False, ""
    folder = _utility_folder(info)
    if folder is None:
        return False, "Open Screenshot Utility once, or set its folder in the addon preferences"
    bat = folder / "Screenshot Utility.bat"
    if not bat.is_file():
        return False, f"Can't find Screenshot Utility.bat in {folder}"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(folder), creationflags=flags)
    return True, ""


def _utility_folder(info):
    prefs = bpy.context.preferences.addons.get(__name__)
    chosen = ""
    if prefs is not None:
        chosen = prefs.preferences.utility_dir.strip()
    if chosen and Path(chosen).is_dir():
        return Path(chosen)
    root = str((info or {}).get("root") or "")
    if root and Path(root).is_dir():
        return Path(root)
    return None


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        handle = ctypes_open(int(pid))
        return handle
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def ctypes_open(pid):
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _draw_topbar(self, context):
    # Same slot GoB uses: the right half of the top bar, not the left menus.
    if context.region.alignment != "RIGHT":
        return
    self.layout.operator("screenshot_utility.send", text="Build Screen List", icon="DESKTOP")


def _install_topbar_button():
    header = bpy.types.TOPBAR_HT_upper_bar
    header.remove(_draw_topbar)
    funcs = header._dyn_ui_initialize()
    after = None
    for index, func in enumerate(funcs):
        if getattr(func, "__name__", "") == "draw_goz_buttons":
            after = index
            break
    if after is None:
        header.prepend(_draw_topbar)
        return
    header._dyn_owner_apply(_draw_topbar)
    funcs.insert(after + 1, _draw_topbar)


def _remove_topbar_button():
    bpy.types.TOPBAR_HT_upper_bar.remove(_draw_topbar)


def _redraw_viewports():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def register():
    bpy.utils.register_class(ScreenshotUtilityPreferences)
    bpy.utils.register_class(SCREENSHOTUTILITY_OT_send)
    _install_topbar_button()
    _redraw_viewports()


def unregister():
    _remove_topbar_button()
    _redraw_viewports()
    bpy.utils.unregister_class(SCREENSHOTUTILITY_OT_send)
    bpy.utils.unregister_class(ScreenshotUtilityPreferences)
