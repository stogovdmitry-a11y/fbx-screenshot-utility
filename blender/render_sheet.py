"""Render turnaround passes for an FBX. Blender 4.5, EEVEE."""

import json
import math
import sys
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sheetlib

DATA_PASSES = {"normal", "roughness", "metallic", "ao", "special"}
FRONTS = {
    "-Y": (0.0, -1.0, 0.0),
    "+Y": (0.0, 1.0, 0.0),
    "-X": (-1.0, 0.0, 0.0),
    "+X": (1.0, 0.0, 0.0),
}

def srgb_to_linear(value):
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def reset_tree(mat):
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    return tree


def tex_node(tree, image, x, y):
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.location = (x, y)
    return node


def float_from_image(tree, image, x, y):
    node = tex_node(tree, image, x, y)
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = (x + 280, y)
    tree.links.new(node.outputs["Color"], separate.inputs["Color"])
    return separate.outputs["Red"]


def set_surface_mode(mat, has_alpha):
    if has_alpha:
        mat.blend_method = "CLIP"
        mat.alpha_threshold = 0.5
        mat.surface_render_method = "DITHERED"
        mat.use_backface_culling = True
        mat.use_backface_culling_shadow = False
    else:
        mat.blend_method = "OPAQUE"
        mat.surface_render_method = "DITHERED"
        mat.use_backface_culling = False
        mat.use_backface_culling_shadow = False


def clip_alpha(tree, images):
    """Hard cutout. Opacity map wins, otherwise the base color alpha."""
    source = None
    if images.get("opacity") is not None:
        source = float_from_image(tree, images["opacity"], -900, -520)
    elif images.get("alpha_from_basecolor") and images.get("basecolor") is not None:
        node = tex_node(tree, images["basecolor"], -900, -520)
        source = node.outputs["Alpha"]
    if source is None:
        return None
    clip = tree.nodes.new("ShaderNodeMath")
    clip.operation = "GREATER_THAN"
    clip.location = (-560, -520)
    clip.inputs[1].default_value = 0.5
    tree.links.new(source, clip.inputs[0])
    return clip.outputs["Value"]


def albedo_socket(tree, images, x=-780, y=220):
    """Base color texture only. AO is a separate row and never tints this."""
    base = images.get("basecolor")
    if base is None:
        rgb = tree.nodes.new("ShaderNodeRGB")
        rgb.location = (x, y)
        rgb.outputs[0].default_value = (0.75, 0.75, 0.75, 1.0)
        return rgb.outputs["Color"]
    return tex_node(tree, base, x, y).outputs["Color"]


def normal_color_socket(tree, image, invert_g):
    node = tex_node(tree, image, -780, 40)
    color = node.outputs["Color"]
    if not invert_g:
        return color
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = (-480, 40)
    tree.links.new(color, separate.inputs["Color"])
    invert = tree.nodes.new("ShaderNodeMath")
    invert.operation = "MULTIPLY"
    invert.location = (-260, -40)
    invert.inputs[1].default_value = -1.0
    tree.links.new(separate.outputs["Green"], invert.inputs[0])
    combine = tree.nodes.new("ShaderNodeCombineColor")
    combine.location = (-40, 40)
    tree.links.new(separate.outputs["Red"], combine.inputs["Red"])
    tree.links.new(invert.outputs["Value"], combine.inputs["Green"])
    tree.links.new(separate.outputs["Blue"], combine.inputs["Blue"])
    return combine.outputs["Color"]


def link_normal_map(tree, target, image, invert_g):
    if image is None:
        return
    color = normal_color_socket(tree, image, invert_g)
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.space = "TANGENT"
    normal_map.location = (-80, -280)
    tree.links.new(color, normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], target.inputs["Normal"])


def output_node(tree, shader_socket, alpha_socket):
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    out.location = (420, 0)
    if alpha_socket is None:
        tree.links.new(shader_socket, out.inputs["Surface"])
        return
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (80, -180)
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.location = (220, 0)
    tree.links.new(alpha_socket, mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(shader_socket, mix.inputs[2])
    tree.links.new(mix.outputs[0], out.inputs["Surface"])


def build_basecolor(mat, images):
    """Albedo as a flat color. No lights, so the row matches the texture."""
    tree = reset_tree(mat)
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.location = (40, 80)
    emit.inputs["Strength"].default_value = 1.0
    tree.links.new(albedo_socket(tree, images), emit.inputs["Color"])
    alpha = clip_alpha(tree, images)
    output_node(tree, emit.outputs["Emission"], alpha)
    set_surface_mode(mat, alpha is not None)


def link_owlcat_masks(tree, bsdf, images):
    """R roughness, G emissive, B metallic, A translucency."""
    masks = images["masks"]
    node = tex_node(tree, masks, -1100, -40)
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = (-780, -40)
    tree.links.new(node.outputs["Color"], separate.inputs["Color"])
    tree.links.new(separate.outputs["Red"], bsdf.inputs["Roughness"])
    tree.links.new(separate.outputs["Blue"], bsdf.inputs["Metallic"])
    if images.get("emission"):
        # G is only how bright the glow is. The color is chosen in the app.
        channels = images.get("emission_color") or (255, 255, 255)
        red, green, blue = (srgb_to_linear(float(channel) / 255.0) for channel in channels[:3])
        bsdf.inputs["Emission Color"].default_value = (red, green, blue, 1.0)
        glow = tree.nodes.new("ShaderNodeMath")
        glow.operation = "MULTIPLY"
        glow.location = (-520, 80)
        glow.inputs[1].default_value = float(images.get("emission_strength", 1.0))
        tree.links.new(separate.outputs["Green"], glow.inputs[0])
        tree.links.new(glow.outputs["Value"], bsdf.inputs["Emission Strength"])
    else:
        bsdf.inputs["Emission Strength"].default_value = 0.0
    if masks.depth >= 32:
        wrap = tree.nodes.new("ShaderNodeMath")
        wrap.operation = "MULTIPLY"
        wrap.location = (-520, -180)
        wrap.inputs[1].default_value = 0.25
        tree.links.new(node.outputs["Alpha"], wrap.inputs[0])
        tree.links.new(wrap.outputs["Value"], bsdf.inputs["Subsurface Weight"])
    else:
        bsdf.inputs["Subsurface Weight"].default_value = 0.0
    bsdf.inputs["Specular IOR Level"].default_value = 0.5


def link_smoothness(tree, bsdf, image):
    """White on the map is smooth. Roughness in the shader is one minus that."""
    invert = tree.nodes.new("ShaderNodeMath")
    invert.operation = "SUBTRACT"
    invert.location = (-460, -80)
    invert.inputs[0].default_value = 1.0
    tree.links.new(float_from_image(tree, image, -980, -80), invert.inputs[1])
    tree.links.new(invert.outputs["Value"], bsdf.inputs["Roughness"])
    bsdf.inputs["Specular IOR Level"].default_value = 0.5


def build_lit(mat, images, invert_g):
    tree = reset_tree(mat)
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (40, 80)
    tree.links.new(albedo_socket(tree, images), bsdf.inputs["Base Color"])
    if images.get("pipeline") == "owlcat" and images.get("masks") is not None:
        link_owlcat_masks(tree, bsdf, images)
    else:
        gloss = images.get("roughness")
        metallic = images.get("metallic")
        smooth = images.get("surface_model") != "roughness"
        if gloss is not None and smooth:
            link_smoothness(tree, bsdf, gloss)
        elif gloss is not None:
            tree.links.new(float_from_image(tree, gloss, -780, -80), bsdf.inputs["Roughness"])
            bsdf.inputs["Specular IOR Level"].default_value = 0.4
        else:
            bsdf.inputs["Roughness"].default_value = 0.45 if smooth else 0.55
            bsdf.inputs["Specular IOR Level"].default_value = 0.5
        if metallic is not None:
            tree.links.new(float_from_image(tree, metallic, -780, -300), bsdf.inputs["Metallic"])
        else:
            bsdf.inputs["Metallic"].default_value = 0.0
    link_normal_map(tree, bsdf, images.get("normal"), invert_g)
    alpha = clip_alpha(tree, images)
    output_node(tree, bsdf.outputs["BSDF"], alpha)
    set_surface_mode(mat, alpha is not None)


def build_data(mat, pass_name, images, invert_g):
    tree = reset_tree(mat)
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.location = (40, 80)
    emit.inputs["Strength"].default_value = 1.0
    if pass_name == "normal" and images.get("normal") is not None:
        tree.links.new(normal_color_socket(tree, images["normal"], invert_g), emit.inputs["Color"])
    elif pass_name == "normal":
        emit.inputs["Color"].default_value = (0.5, 0.5, 1.0, 1.0)
    else:
        image = images.get(pass_name)
        if image is not None:
            node = tex_node(tree, image, -520, 80)
            tree.links.new(node.outputs["Color"], emit.inputs["Color"])
        else:
            emit.inputs["Color"].default_value = (0.5, 0.5, 0.5, 1.0)
    alpha = clip_alpha(tree, images)
    output_node(tree, emit.outputs["Emission"], alpha)
    set_surface_mode(mat, alpha is not None)


def setup_world(scene, background):
    world = bpy.data.worlds.new("SheetWorld")
    scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    channels = background or (60, 60, 60)
    red, green, blue = (srgb_to_linear(channel / 255.0) for channel in channels)
    camera_bg = tree.nodes.new("ShaderNodeBackground")
    camera_bg.name = "CameraBG"
    camera_bg.inputs["Color"].default_value = (red, green, blue, 1.0)
    camera_bg.inputs["Strength"].default_value = 1.0
    # Dark for non-camera rays, otherwise the gray world flattens the lighting.
    env_bg = tree.nodes.new("ShaderNodeBackground")
    env_bg.inputs["Color"].default_value = (0.16, 0.165, 0.17, 1.0)
    env_bg.inputs["Strength"].default_value = 1.0
    light_path = tree.nodes.new("ShaderNodeLightPath")
    mix = tree.nodes.new("ShaderNodeMixShader")
    out = tree.nodes.new("ShaderNodeOutputWorld")
    tree.links.new(light_path.outputs["Is Camera Ray"], mix.inputs[0])
    tree.links.new(env_bg.outputs["Background"], mix.inputs[1])
    tree.links.new(camera_bg.outputs["Background"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], out.inputs["Surface"])


def use_eevee(scene):
    """4.5 calls the engine EEVEE Next. 5.0 put the old name back."""
    if bpy.app.version >= (5, 0, 0):
        scene.render.engine = "BLENDER_EEVEE"
    else:
        scene.render.engine = "BLENDER_EEVEE_NEXT"


_cycles_ready = False


def _cycles_gpu():
    """OptiX, then CUDA, then the other GPU backends. None keeps Cycles on the CPU."""
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for device_type in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
        try:
            prefs.compute_device_type = device_type
        except (TypeError, ValueError):
            continue
        if prefs.compute_device_type != device_type:
            continue
        prefs.get_devices()
        found = False
        for device in prefs.devices:
            device.use = device.type == device_type
            found = found or device.use
        if found:
            return device_type
    return None


def use_cycles(scene):
    """Real path tracing for the lit row. Later frames reuse the same device and samples."""
    global _cycles_ready
    scene.render.engine = "CYCLES"
    scene.render.use_persistent_data = True
    if _cycles_ready:
        return
    cycles = scene.cycles
    device_type = _cycles_gpu()
    cycles.device = "GPU" if device_type else "CPU"
    cycles.samples = 128
    cycles.use_adaptive_sampling = True
    cycles.adaptive_threshold = 0.01
    cycles.use_denoising = True
    if hasattr(cycles, "denoising_use_gpu"):
        cycles.denoising_use_gpu = cycles.device == "GPU"
    for layer in scene.view_layers:
        layer.cycles.use_denoising = True
    _cycles_ready = True
    where = device_type or "CPU"
    print(f"CYCLES {where} 128", flush=True)


def prepare_engine(scene, lit, raytracing, cycles):
    """Data rows stay on EEVEE. Cycles is only worth it once the pass is actually lit."""
    if lit and cycles:
        use_cycles(scene)
        return
    use_eevee(scene)
    scene.eevee.use_raytracing = lit and raytracing
    set_gtao(scene, lit and raytracing)
    scene.eevee.use_shadows = lit


def set_gtao(scene, enabled, distance=None):
    """Crevice darkening on the lit pass.

    use_gtao only feeds the AO render pass. On the beauty image, EEVEE gets
    this from Fast GI, and only while raytracing is on. Its default mode is
    global illumination, which bounces light back into the gaps.
    """
    eevee = scene.eevee
    if hasattr(eevee, "use_gtao"):
        eevee.use_gtao = enabled
    if hasattr(eevee, "use_fast_gi"):
        eevee.use_fast_gi = enabled
        if enabled and hasattr(eevee, "fast_gi_method"):
            eevee.fast_gi_method = "AMBIENT_OCCLUSION_ONLY"
    if distance is None:
        return
    if hasattr(eevee, "gtao_distance"):
        eevee.gtao_distance = distance
    if hasattr(eevee, "gtao_quality"):
        eevee.gtao_quality = 1.0
    if hasattr(eevee, "fast_gi_distance"):
        eevee.fast_gi_distance = distance
        eevee.fast_gi_quality = 0.8
        eevee.fast_gi_ray_count = 4
        eevee.fast_gi_step_count = 16
        eevee.fast_gi_resolution = "2"
        eevee.fast_gi_thickness_near = max(0.001, distance * 0.08)
        eevee.fast_gi_bias = 0.02
    # Rougher than this takes the AO path. Glossier surfaces still screen-trace.
    if hasattr(eevee, "ray_tracing_options"):
        eevee.ray_tracing_options.trace_max_roughness = 0.3
    for layer in scene.view_layers:
        settings = getattr(layer, "eevee", None)
        if settings is not None and hasattr(settings, "ambient_occlusion_distance"):
            settings.ambient_occlusion_distance = distance


def setup_render(scene, frame_size, extent):
    use_eevee(scene)
    scene.render.resolution_x = frame_size
    scene.render.resolution_y = frame_size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.use_sequencer = False
    scene.render.use_compositing = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.display_settings.display_device = "sRGB"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.eevee.use_shadows = True
    scene.eevee.use_raytracing = True
    scene.eevee.ray_tracing_method = "SCREEN"
    scene.eevee.ray_tracing_options.resolution_scale = "1"
    scene.eevee.ray_tracing_options.screen_trace_quality = 0.5
    scene.eevee.ray_tracing_options.screen_trace_thickness = max(0.002, max(extent.x, extent.y, extent.z) * 0.015)
    scene.eevee.ray_tracing_options.use_denoise = True
    scene.eevee.shadow_ray_count = 4
    scene.eevee.shadow_step_count = 12
    if hasattr(scene.eevee, "shadow_pool_size"):
        scene.eevee.shadow_pool_size = "1024"
    # Long enough to reach between limbs, not just pores.
    set_gtao(scene, True, max(0.005, max(extent.x, extent.y, extent.z) * 0.28))


def setup_bloom(scene, strength):
    """A small halo around emission. Flat rows leave compositing off."""
    scene.use_nodes = True
    scene.render.use_compositing = False
    # The Emit socket only exists once the pass is on, and only on a node made after that.
    for layer in scene.view_layers:
        layer.use_pass_emit = True
    tree = scene.node_tree
    tree.nodes.clear()
    layers = tree.nodes.new("CompositorNodeRLayers")
    layers.location = (-720, 0)
    glare = tree.nodes.new("CompositorNodeGlare")
    glare.glare_type = "BLOOM"
    glare.quality = "HIGH"
    glare.location = (-440, -200)
    glare.inputs["Threshold"].default_value = 0.15
    glare.inputs["Strength"].default_value = max(0.0, float(strength))
    glare.inputs["Size"].default_value = 0.35
    glare.inputs["Smoothness"].default_value = 0.2
    tree.links.new(layers.outputs["Emit"], glare.inputs["Image"])
    add = tree.nodes.new("CompositorNodeMixRGB")
    add.blend_type = "ADD"
    add.use_clamp = False
    add.location = (-140, 40)
    add.inputs["Fac"].default_value = 1.0
    tree.links.new(layers.outputs["Image"], add.inputs[1])
    tree.links.new(glare.outputs["Glare"], add.inputs[2])
    # The sheet keeps a pixel only where alpha is set, so the halo has to carry its own.
    glow = tree.nodes.new("CompositorNodeRGBToBW")
    glow.location = (-140, -220)
    tree.links.new(glare.outputs["Glare"], glow.inputs["Image"])
    cover = tree.nodes.new("CompositorNodeMath")
    cover.operation = "MAXIMUM"
    cover.location = (80, -180)
    tree.links.new(layers.outputs["Alpha"], cover.inputs[0])
    tree.links.new(glow.outputs["Val"], cover.inputs[1])
    shaped = tree.nodes.new("CompositorNodeSetAlpha")
    shaped.location = (280, 40)
    tree.links.new(add.outputs["Image"], shaped.inputs["Image"])
    tree.links.new(cover.outputs["Value"], shaped.inputs["Alpha"])
    out = tree.nodes.new("CompositorNodeComposite")
    out.location = (520, 40)
    tree.links.new(shaped.outputs["Image"], out.inputs["Image"])


def look_at(obj, target):
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def add_contact_shadow(center, extent, minimum):
    """A soft dark disc under the feet. Only the lit row shows it."""
    span_x = max(extent.x, 0.001)
    span_y = max(extent.y, 0.001)
    hx = span_x * 0.46
    hy = span_y * 0.46
    z = minimum.z - max(span_x, span_y) * 0.004
    mesh = bpy.data.meshes.new("ContactShadow")
    mesh.from_pydata(
        [(-hx, -hy, 0.0), (hx, -hy, 0.0), (hx, hy, 0.0), (-hx, hy, 0.0)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()
    obj = bpy.data.objects.new("ContactShadow", mesh)
    obj.location = (center.x, center.y, z)
    bpy.context.scene.collection.objects.link(obj)
    if hasattr(obj, "visible_shadow"):
        obj.visible_shadow = False
    mat = bpy.data.materials.new("ContactShadow")
    mat.use_nodes = True
    mat.blend_method = "BLEND"
    mat.surface_render_method = "BLENDED"
    mat.use_backface_culling = True
    tree = reset_tree(mat)
    coord = tree.nodes.new("ShaderNodeTexCoord")
    coord.location = (-900, 0)
    center_at = tree.nodes.new("ShaderNodeVectorMath")
    center_at.operation = "SUBTRACT"
    center_at.location = (-700, 0)
    center_at.inputs[1].default_value = (0.5, 0.5, 0.5)
    tree.links.new(coord.outputs["Generated"], center_at.inputs[0])
    length = tree.nodes.new("ShaderNodeVectorMath")
    length.operation = "LENGTH"
    length.location = (-500, 0)
    tree.links.new(center_at.outputs["Vector"], length.inputs[0])
    # 0 at the middle, 1 before the quad edge, so the disc has no square rim.
    reach = tree.nodes.new("ShaderNodeMath")
    reach.operation = "DIVIDE"
    reach.location = (-320, 0)
    reach.inputs[1].default_value = 0.36
    tree.links.new(length.outputs["Value"], reach.inputs[0])
    invert = tree.nodes.new("ShaderNodeMath")
    invert.operation = "SUBTRACT"
    invert.location = (-140, 80)
    invert.inputs[0].default_value = 1.0
    tree.links.new(reach.outputs["Value"], invert.inputs[1])
    clamped = tree.nodes.new("ShaderNodeMath")
    clamped.operation = "MAXIMUM"
    clamped.location = (40, 80)
    clamped.inputs[1].default_value = 0.0
    tree.links.new(invert.outputs["Value"], clamped.inputs[0])
    falloff = tree.nodes.new("ShaderNodeMath")
    falloff.operation = "POWER"
    falloff.location = (220, 80)
    falloff.inputs[1].default_value = 1.8
    tree.links.new(clamped.outputs["Value"], falloff.inputs[0])
    strength = tree.nodes.new("ShaderNodeMath")
    strength.operation = "MULTIPLY"
    strength.location = (400, 80)
    strength.inputs[1].default_value = 0.38
    tree.links.new(falloff.outputs["Value"], strength.inputs[0])
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (400, -80)
    dark = tree.nodes.new("ShaderNodeEmission")
    dark.location = (400, -220)
    dark.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    dark.inputs["Strength"].default_value = 1.0
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.location = (640, 0)
    tree.links.new(strength.outputs["Value"], mix.inputs["Fac"])
    tree.links.new(transparent.outputs["BSDF"], mix.inputs[1])
    tree.links.new(dark.outputs["Emission"], mix.inputs[2])
    output_node(tree, mix.outputs["Shader"], None)
    obj.data.materials.append(mat)
    obj.hide_render = True
    return obj


def add_area(name):
    light_data = bpy.data.lights.new(name, "AREA")
    light_data.shape = "DISK"
    obj = bpy.data.objects.new(name, light_data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def orbit_direction(front, degrees):
    base = FRONTS[front]
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    return Vector((base[0] * cosine - base[1] * sine, base[0] * sine + base[1] * cosine, 0.0)).normalized()


def fit_distance(extent, camera, distance_scale):
    radius = 0.5 * max(extent.x, extent.y, extent.z) * 1.16
    fov = camera.data.angle
    return (radius / math.tan(fov * 0.5)) * distance_scale


def elevated_direction(direction, horizontal, height_frac, _extent):
    """Height is a pitch. 1.0 looks down at 45°, whatever the model's proportions."""
    view = direction * horizontal + Vector((0.0, 0.0, horizontal * height_frac))
    if view.length < 1e-8:
        return direction
    return view.normalized()


def framing_distance(camera, extent, view_dir):
    """How far back the camera must sit so the whole bounds stay inside the frame."""
    view_dir = view_dir.normalized()
    world_up = Vector((0.0, 0.0, 1.0))
    if abs(view_dir.dot(world_up)) > 0.99:
        world_up = Vector((0.0, 1.0, 0.0))
    right = world_up.cross(view_dir).normalized()
    up = view_dir.cross(right).normalized()
    half_x = math.tan(camera.data.angle_x * 0.5)
    half_y = math.tan(camera.data.angle_y * 0.5)
    needed = 0.001
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            for sz in (-0.5, 0.5):
                offset = Vector((extent.x * sx, extent.y * sy, extent.z * sz))
                depth = offset.dot(view_dir)
                needed = max(
                    needed,
                    abs(offset.dot(right)) / half_x + depth,
                    abs(offset.dot(up)) / half_y + depth,
                )
    return needed * 1.16


def orbit_radius(camera, extent, front, frames, start, step, height_frac, distance_scale):
    """One distance for the whole turnaround, far enough for the worst angle.

    Height changes the aim. Distance still multiplies how far back the camera sits,
    so the whole bounds stay in frame instead of only the level-view sphere.
    """
    horizontal = fit_distance(extent, camera, distance_scale)
    worst = 0.001
    for index in range(max(1, frames)):
        direction = orbit_direction(front, start + index * step)
        view_dir = elevated_direction(direction, horizontal, height_frac, extent)
        worst = max(worst, framing_distance(camera, extent, view_dir))
    return worst * distance_scale, horizontal


def place_rig(camera, lights, center, extent, distance, direction, height_frac, horizontal):
    view_dir = elevated_direction(direction, horizontal, height_frac, extent)
    camera.location = center + view_dir * distance
    look_at(camera, center)
    forward = (center - camera.location).normalized()
    right = forward.cross(Vector((0.0, 0.0, 1.0)))
    if right.length < 1e-6:
        right = Vector((1.0, 0.0, 0.0))
    else:
        right.normalize()
    up = right.cross(forward).normalized()

    key, wrap, rim = lights
    span = max(extent.x, extent.y, extent.z, 0.001)
    # Key in front, a little above and to the side. Big enough to stay soft, small enough to shadow.
    key.location = center - forward * distance * 0.62 + up * span * 0.55 - right * span * 0.75
    wrap.location = center - forward * distance * 0.4 - up * span * 0.7
    rim.location = center + forward * distance * 0.4 + up * span * 0.9
    for obj in lights:
        look_at(obj, center)

    falloff = distance * distance
    # Energies track the disk area, so a smaller key stays the same brightness
    # and can actually cast a limb shadow.
    key.data.energy = 2.6 * falloff
    wrap.data.energy = 0.55 * falloff
    rim.data.energy = 0.8 * falloff
    key.data.color = (1.0, 0.97, 0.94)
    wrap.data.color = (1.0, 0.98, 0.96)
    key.data.size = span * 1.15
    key.data.use_shadow = True
    wrap.data.size = span * 2.2
    wrap.data.use_shadow = True
    rim.data.size = span * 1.05
    rim.data.use_shadow = True
    camera.data.clip_start = max(distance / 200.0, 0.001)
    camera.data.clip_end = distance * 50.0


def resolve_materials(job, snapshots):
    resolved = {}
    invert_g = bool(job.get("invert_normal_g"))
    assigned = job.get("materials") or {}
    for mat in sheetlib.materials_in_order():
        user = assigned.get(mat.name) or {}
        snap = snapshots.get(mat.name) or {}
        resolved[mat.name] = {
            "basecolor": sheetlib.resolve_image(user.get("basecolor") or "", snap.get("basecolor"), "sRGB"),
            "normal": sheetlib.resolve_image(user.get("normal") or "", snap.get("normal"), "Non-Color"),
            "roughness": sheetlib.resolve_image(user.get("roughness") or "", snap.get("roughness"), "Non-Color"),
            "metallic": sheetlib.resolve_image(user.get("metallic") or "", snap.get("metallic"), "Non-Color"),
            "ao": sheetlib.resolve_image(user.get("ao") or "", None, "Non-Color"),
            "opacity": sheetlib.resolve_image(user.get("opacity") or "", None, "Non-Color"),
            "masks": sheetlib.resolve_image(user.get("masks") or "", None, "Non-Color"),
            "special": sheetlib.resolve_image(user.get("special") or "", None, "Non-Color"),
            "alpha_from_basecolor": bool(user.get("alpha_from_basecolor")),
            "invert_g": invert_g,
            "surface_model": "roughness" if job.get("surface_model") == "roughness" else "smoothness",
            "pipeline": "owlcat" if job.get("character", job.get("owlcat", True)) else "generic",
            "emission_color": job.get("emission") or [255, 255, 255],
            "emission_strength": float(job.get("emission_strength", 1.0)),
            "emission": bool(job.get("emission_enabled")),
        }
    return resolved


def apply_pass(pass_name, resolved):
    for mat in sheetlib.materials_in_order():
        images = resolved.get(mat.name)
        if images is None:
            continue
        if pass_name == "basecolor":
            build_basecolor(mat, images)
        elif pass_name in DATA_PASSES:
            build_data(mat, pass_name, images, images["invert_g"])
        else:
            build_lit(mat, images, images["invert_g"])


def render_wire(
    scene,
    camera,
    lights,
    meshes,
    resolved,
    pass_name,
    center,
    extent,
    distance,
    horizontal,
    front,
    frames,
    start_degrees,
    step_degrees,
    height,
    frame_size,
    thickness_px,
    color,
    out_dir,
    done,
    total,
    raytracing=True,
    cycles=False,
    exposure=0.15,
    bloom=1.0,
    ground=None,
):
    """Edges sit on the shaded mesh. The mesh itself hides whatever is behind it."""
    apply_pass(pass_name, resolved)
    if pass_name in DATA_PASSES:
        view_name = "Raw"
    elif pass_name == "material":
        view_name = "Khronos PBR Neutral"
    else:
        view_name = "Standard"
    try:
        scene.view_settings.view_transform = view_name
    except TypeError:
        scene.view_settings.view_transform = "Standard"
    lit = pass_name == "material"
    if ground is not None:
        ground.hide_render = not lit
    scene.view_settings.exposure = exposure if lit else 0.0
    scene.render.use_compositing = lit and bloom > 0
    prepare_engine(scene, lit, raytracing, cycles)
    scene.eevee.taa_render_samples = 32 if lit else 8

    world_per_pixel = (2.0 * distance * math.tan(camera.data.angle * 0.5)) / max(frame_size, 1)
    world_thickness = world_per_pixel * max(1, thickness_px)
    red, green, blue = (srgb_to_linear(channel) for channel in color)
    material = bpy.data.materials.new("SheetWire")
    tree = reset_tree(material)
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (red, green, blue, 1.0)
    emit.inputs["Strength"].default_value = 1.0
    output_node(tree, emit.outputs["Emission"], None)
    set_surface_mode(material, False)

    created = []
    for obj in meshes:
        wire = obj.copy()
        wire.data = obj.data.copy()
        wire.hide_render = False
        scene.collection.objects.link(wire)
        modifier = wire.modifiers.new("SheetWire", "WIREFRAME")
        scale = wire.matrix_world.to_scale()
        largest = max(abs(scale.x), abs(scale.y), abs(scale.z), 1e-6)
        modifier.thickness = world_thickness / largest
        modifier.use_replace = True
        modifier.use_boundary = True
        modifier.use_even_offset = True
        modifier.offset = 1.0
        wire.data.materials.clear()
        wire.data.materials.append(material)
        if hasattr(wire, "visible_shadow"):
            wire.visible_shadow = False
        created.append(wire)

    try:
        for index in range(frames):
            angle = start_degrees + index * step_degrees
            place_rig(
                camera,
                lights,
                center,
                extent,
                distance,
                orbit_direction(front, angle),
                height,
                horizontal,
            )
            scene.render.filepath = str(out_dir / f"wire_{index:02d}.png")
            bpy.ops.render.render(write_still=True)
            done += 1
            print(f"PROGRESS {done} {total} wire {index}", flush=True)
    finally:
        for wire in created:
            data = wire.data
            bpy.data.objects.remove(wire, do_unlink=True)
            if data.users == 0:
                bpy.data.meshes.remove(data)


def main():
    job_path = sys.argv[sys.argv.index("--") + 1]
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    sheetlib.clear_scene()
    sheetlib.import_fbx(job["fbx"])
    sheetlib.ensure_materials()
    meshes = sheetlib.mesh_objects()
    minimum, maximum = sheetlib.world_bounds(meshes)
    center = (minimum + maximum) * 0.5
    extent = maximum - minimum
    snapshots = {mat.name: sheetlib.snapshot_material(mat) for mat in sheetlib.materials_in_order()}
    resolved = resolve_materials(job, snapshots)

    scene = bpy.context.scene
    setup_render(scene, int(job["frame_size"]), extent)
    bloom = float(job.get("bloom", 1.0)) if job.get("emission_enabled") else 0.0
    setup_bloom(scene, bloom)
    setup_world(scene, job.get("background"))

    camera_data = bpy.data.cameras.new("SheetCamera")
    camera_data.lens = 100
    camera_data.sensor_fit = "VERTICAL"
    camera = bpy.data.objects.new("SheetCamera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    lights = (add_area("Key"), add_area("Wrap"), add_area("Rim"))
    ground = add_contact_shadow(center, extent, minimum)

    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stats.json").write_text(
        json.dumps({"triangles": sheetlib.triangle_count(meshes)}),
        encoding="utf-8",
    )
    passes = list(job["passes"])
    frames = int(job["frames"])
    wire_frames = frames if job.get("wire") else 0
    total = max(1, len(passes) * frames + wire_frames)
    done = 0
    front = job.get("front") or "-Y"
    if front not in FRONTS:
        raise RuntimeError(f"Unknown front side: {front}")
    height = float(job.get("height", 0.0))
    distance, horizontal = orbit_radius(
        camera,
        extent,
        front,
        frames,
        float(job["start_degrees"]),
        float(job["step_degrees"]),
        height,
        float(job.get("distance_scale", 1.0)),
    )

    raytracing = bool(job.get("raytracing", True))
    cycles = bool(job.get("cycles", False))
    for pass_name in passes:
        apply_pass(pass_name, resolved)
        if pass_name in DATA_PASSES:
            view_name = "Raw"
        elif pass_name == "material":
            view_name = "Khronos PBR Neutral"
        else:
            view_name = "Standard"
        try:
            scene.view_settings.view_transform = view_name
        except TypeError:
            scene.view_settings.view_transform = "Standard"
        lit = pass_name == "material"
        ground.hide_render = not lit
        # Other rows stay at the texture's real brightness. Material was reading too dark.
        scene.view_settings.exposure = float(job.get("exposure", 0.15)) if lit else 0.0
        scene.render.use_compositing = lit and bloom > 0
        prepare_engine(scene, lit, raytracing, cycles)
        scene.eevee.taa_render_samples = 32 if lit else 16
        for index in range(frames):
            angle = float(job["start_degrees"]) + index * float(job["step_degrees"])
            place_rig(
                camera,
                lights,
                center,
                extent,
                distance,
                orbit_direction(front, angle),
                height,
                horizontal,
            )
            scene.render.filepath = str(out_dir / f"{pass_name}_{index:02d}.png")
            bpy.ops.render.render(write_still=True)
            done += 1
            print(f"PROGRESS {done} {total} {pass_name} {index}", flush=True)
    wire = job.get("wire")
    if wire:
        render_wire(
            scene,
            camera,
            lights,
            meshes,
            resolved,
            wire.get("pass") or "material",
            center,
            extent,
            distance,
            horizontal,
            front,
            frames,
            float(job["start_degrees"]),
            float(job["step_degrees"]),
            float(job.get("height", 0.0)),
            int(job["frame_size"]),
            int(wire.get("thickness") or 1),
            wire.get("color") or [0.07, 0.07, 0.07],
            out_dir,
            done,
            total,
            raytracing,
            cycles,
            float(job.get("exposure", 0.15)),
            bloom,
            ground,
        )
    print("RESULT ok", flush=True)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.exit(1)
