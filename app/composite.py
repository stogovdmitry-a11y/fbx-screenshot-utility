"""Stitch rendered frames into one labeled sheet."""

from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.paths import resource_root

BUNDLED_FONT = resource_root() / "fonts" / "ExonRegular.otf"

SHEET_BG = (60, 60, 60)

PASS_LABELS = {
    "basecolor": "Base Color",
    "normal": "Normal",
    "roughness": "Roughness",
    "metallic": "Metallic",
    "ao": "AO",
    "special": "Special Masks",
    "material": "Material",
}


def _font(size, path=None):
    size = max(1, int(size))
    chosen = path if path and Path(path).is_file() else BUNDLED_FONT
    if chosen.is_file():
        try:
            return ImageFont.truetype(str(chosen), size)
        except OSError:
            pass
    for name in ("segoeui.ttf", "arial.ttf"):
        fallback = Path(r"C:\Windows\Fonts") / name
        if fallback.is_file():
            return ImageFont.truetype(str(fallback), size)
    return ImageFont.load_default()


def _type_scale(frame_w, fonts, key, automatic):
    """User sizes are for a 512px frame, then they grow and shrink with the frame."""
    chosen = (fonts or {}).get(key)
    if not chosen:
        return automatic
    return max(8, round(int(chosen) * max(frame_w, 1) / 512))


def _face():
    return BUNDLED_FONT


def _flatten(image, background):
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, background)
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


def _text_box(text, font):
    return ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), text, font=font)


def _caption_entries(name, task, project):
    values = {"Task": task, "Project": project, "Artist": name}
    entries = []
    for label, value in values.items():
        text = (value or "").strip()
        if text:
            entries.append((f"{label}:", text))
    return entries


def _caption_style(frame_w, fonts):
    """One size for Task, Project and Artist. Row height comes from the font, not the glyphs."""
    chosen = None
    if fonts:
        chosen = fonts.get("caption") or fonts.get("name") or fonts.get("task")
    size = _type_scale(frame_w, {"caption": chosen} if chosen else None, "caption", max(18, frame_w // 18))
    font = _font(size, _face())
    ink = font.getbbox("Agy", anchor="ls")
    ink_top = ink[1]
    ink_h = ink[3] - ink_top
    pad_x = max(14, round(size * 0.55))
    pad_y = max(2, round(size * 0.1))
    row_h = ink_h + pad_y * 2
    gap = max(6, round(size * 0.32))
    margin = max(16, frame_w // 26)
    return {
        "font": font,
        "baseline": pad_y - ink_top,
        "row_h": row_h,
        "pad_x": pad_x,
        "gap": gap,
        "margin": margin,
    }


def _darken(color):
    """A step darker than the sheet, so the caption bar still reads on a dark background."""
    darkened = tuple(int(channel * 0.62) for channel in color[:3])
    if sum(color[i] - darkened[i] for i in range(3)) < 28:
        darkened = tuple(max(0, channel - 16) for channel in color[:3])
    return darkened


def _footer_block(frame_w, name, task, fonts=None, project="", triangles=None):
    """Height of the gap under the frames plus the dark caption bar."""
    entries = _caption_entries(name, task, project)
    style = _caption_style(frame_w, fonts)
    gap_before = max(18, frame_w // 22)
    if triangles is not None:
        pad = max(10, frame_w // 36)
        gap_before = max(gap_before, style["row_h"] + pad * 2)
    rows_h = 0
    if entries:
        rows_h = len(entries) * style["row_h"] + (len(entries) - 1) * style["gap"]
    bar_h = style["margin"] + rows_h + style["margin"]
    return gap_before, bar_h


def _corner_inset(frame_w):
    return max(36, frame_w // 14)


def _draw_caption_table(sheet, draw, bar_top, left, name, task, project, frame_w, fonts):
    entries = _caption_entries(name, task, project)
    if not entries:
        return sheet
    style = _caption_style(frame_w, fonts)
    font = style["font"]
    label_w = max(font.getlength(label) for label, _value in entries)
    value_x = left + label_w + style["pad_x"]
    text = (242, 242, 242)
    labels = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    label_draw = ImageDraw.Draw(labels)
    y = bar_top + style["margin"]
    for label, value in entries:
        baseline = y + style["baseline"]
        label_draw.text((left, baseline), label, font=font, fill=(242, 242, 242, 178), anchor="ls")
        draw.text((value_x, baseline), value, font=font, fill=text, anchor="ls")
        y += style["row_h"] + style["gap"]
    return Image.alpha_composite(sheet.convert("RGBA"), labels).convert("RGB")


def _draw_date(sheet, frame_w, fonts):
    stamp = date.today().strftime("%d.%m.%Y")
    date_font = _font(_type_scale(frame_w, fonts, "date", max(22, frame_w // 15)), _face())
    inset = _corner_inset(frame_w)
    overlay = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).text(
        (sheet.width - inset, inset),
        stamp,
        font=date_font,
        fill=(176, 176, 176, 128),
        anchor="rt",
    )
    return Image.alpha_composite(sheet.convert("RGBA"), overlay).convert("RGB")


def _paste_logo(sheet, logo_path, frame_w, bar_h):
    """Bottom-right badge, fitted inside the dark footer. Source pixel size does not matter."""
    path = Path(logo_path) if logo_path else None
    if path is None or not str(path).strip() or not path.is_file() or bar_h < 8:
        return sheet
    with Image.open(path) as image:
        logo = image.convert("RGBA")
    bounds = logo.getchannel("A").getbbox()
    if bounds is None:
        return sheet
    logo = logo.crop(bounds)
    pad_y = max(6, round(bar_h * 0.1))
    pad_x = _corner_inset(frame_w)
    target_h = max(1, bar_h - pad_y * 2)
    max_w = max(target_h, sheet.width // 3)
    scale = target_h / logo.height
    if logo.width * scale > max_w:
        scale = max_w / logo.width
    logo = logo.resize(
        (max(1, round(logo.width * scale)), max(1, round(logo.height * scale))),
        Image.Resampling.LANCZOS,
    )
    bar_top = sheet.height - bar_h
    x = max(0, sheet.width - pad_x - logo.width)
    y = bar_top + (bar_h - logo.height) // 2
    base = sheet.convert("RGBA")
    base.alpha_composite(logo, (x, y))
    return base.convert("RGB")


def _draw_triangles(sheet, bar_top, frame_w, fonts, count):
    if count is None:
        return sheet
    style = _caption_style(frame_w, fonts)
    font = style["font"]
    label = "Triangles Count: "
    number = f"{int(count):,}".replace(",", " ")
    label_w = font.getlength(label)
    total_w = label_w + font.getlength(number)
    x = (sheet.width - total_w) / 2
    pad = max(10, frame_w // 36)
    descent = max(0, font.getbbox("Ag", anchor="ls")[3])
    baseline = bar_top - pad - descent
    overlay = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.text((x, baseline), label, font=font, fill=(242, 242, 242, 178), anchor="ls")
    draw.text((x + label_w, baseline), number, font=font, fill=(242, 242, 242, 255), anchor="ls")
    return Image.alpha_composite(sheet.convert("RGBA"), overlay).convert("RGB")


def _sheet_rows(passes, labels, grid):
    names = dict(PASS_LABELS)
    if labels:
        names.update(labels)
    ordered = list(passes)
    if "material" in ordered:
        ordered.remove("material")
        ordered.insert(0, "material")
    rows = [(pass_name, names.get(pass_name, pass_name), False) for pass_name in ordered]
    if grid:
        key = grid["pass"]
        rows.append((key, f"{names.get(key, key)} Wire", True))
    return rows


def _size_font(frame_w, fonts):
    label_size = _type_scale(frame_w, fonts, "labels", max(22, frame_w // 13))
    return _font(max(8, round(label_size * 0.62)), _face())


def measure_sheet(frame_w, frame_count, rows, name="", task="", gap=0, fonts=None, project="", triangles=None, sizes=None):
    """Pixel size of the finished sheet when every frame is frame_w square."""
    frame_w = max(1, int(frame_w))
    gap = max(0, int(gap))
    gap_before, bar_h = _footer_block(frame_w, name, task, fonts, project, triangles)
    pad_x = max(48, frame_w // 14)
    pad_top = max(72, frame_w // 9)
    footer_h = gap_before + bar_h
    font = _font(_type_scale(frame_w, fonts, "labels", max(22, frame_w // 13)), _face())
    size_font = _size_font(frame_w, fonts)
    widest = 0
    for key, label, _grid in rows:
        widest = max(widest, _text_box(label, font)[2])
        size_text = (sizes or {}).get(key)
        if size_text:
            widest = max(widest, _text_box(size_text, size_font)[2])
    label_w = widest + 36
    gaps_x = gap * max(0, frame_count - 1)
    gaps_y = gap * max(0, len(rows) - 1)
    sheet_w = pad_x + label_w + frame_count * frame_w + gaps_x + pad_x
    sheet_h = pad_top + len(rows) * frame_w + gaps_y + footer_h
    return sheet_w, sheet_h


def frame_size_for_long_side(target, frame_count, passes, grid=None, name="", task="", labels=None, gap=0, fonts=None, project="", triangles=None, sizes=None):
    """Largest square frame whose sheet's long side is still within target."""
    rows = _sheet_rows(passes, labels, grid)
    if not rows or frame_count < 1:
        return max(1, int(target))
    target = max(1, int(target))
    best = 1
    low, high = 1, target
    while low <= high:
        mid = (low + high) // 2
        width, height = measure_sheet(mid, frame_count, rows, name, task, gap, fonts, project, triangles, sizes)
        if max(width, height) <= target:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _fit_long_side(sheet, target):
    if not target:
        return sheet
    target = int(target)
    width, height = sheet.size
    long_side = max(width, height)
    if long_side == target or long_side < 1:
        return sheet
    if width >= height:
        size = (target, max(1, round(height * target / width)))
    else:
        size = (max(1, round(width * target / height)), target)
    return sheet.resize(size, Image.Resampling.LANCZOS)


def build_sheet(
    frame_dir,
    passes,
    frame_count,
    output_path,
    name="",
    task="",
    grid=None,
    labels=None,
    long_side=None,
    background=None,
    gap=0,
    fonts=None,
    project="",
    logo=None,
    triangles=None,
    sizes=None,
):
    frame_dir = Path(frame_dir)
    background = tuple(background or SHEET_BG)
    rows = _sheet_rows(passes, labels, grid)
    if not rows:
        raise ValueError("No rows to put on the sheet")
    with Image.open(frame_dir / f"{rows[0][0]}_00.png") as first:
        frame_w, frame_h = first.size
    gap = max(0, int(gap))
    gap_before, bar_h = _footer_block(frame_w, name, task, fonts, project, triangles)
    pad_x = max(48, frame_w // 14)
    pad_top = max(72, frame_w // 9)
    footer_h = gap_before + bar_h
    font = _font(_type_scale(frame_w, fonts, "labels", max(22, frame_w // 13)), _face())
    size_font = _size_font(frame_w, fonts)
    widest = 0
    for key, label, _grid in rows:
        widest = max(widest, _text_box(label, font)[2])
        size_text = (sizes or {}).get(key)
        if size_text:
            widest = max(widest, _text_box(size_text, size_font)[2])
    label_w = widest + 36
    gaps_x = gap * max(0, frame_count - 1)
    grid_h = len(rows) * frame_h + gap * max(0, len(rows) - 1)
    sheet_w = pad_x + label_w + frame_count * frame_w + gaps_x + pad_x
    grid_top = pad_top
    sheet = Image.new("RGB", (sheet_w, grid_top + grid_h + footer_h), background)
    draw = ImageDraw.Draw(sheet)

    for row, (pass_name, label, with_grid) in enumerate(rows):
        size_text = None if with_grid else (sizes or {}).get(pass_name)
        bbox = draw.textbbox((0, 0), label, font=font)
        text_h = bbox[3] - bbox[1]
        x = pad_x - bbox[0]
        row_y = grid_top + row * (frame_h + gap)
        if size_text:
            size_box = draw.textbbox((0, 0), size_text, font=size_font)
            size_h = size_box[3] - size_box[1]
            line_gap = max(10, frame_w // 28)
            block_h = text_h + line_gap + size_h
            y = row_y + (frame_h - block_h) / 2 - bbox[1]
            draw.text((x, y), label, fill=(235, 235, 235), font=font)
            draw.text(
                (pad_x - size_box[0], y + text_h + line_gap - size_box[1]),
                size_text,
                fill=(176, 176, 176),
                font=size_font,
            )
        else:
            y = row_y + (frame_h - text_h) / 2 - bbox[1]
            draw.text((x, y), label, fill=(235, 235, 235), font=font)
        for col in range(frame_count):
            filename = f"wire_{col:02d}.png" if with_grid else f"{pass_name}_{col:02d}.png"
            with Image.open(frame_dir / filename) as frame:
                sheet.paste(
                    _flatten(frame, background),
                    (pad_x + label_w + col * (frame_w + gap), row_y),
                )

    bar_top = grid_top + grid_h + gap_before
    bar = _darken(background)
    draw.rectangle((0, bar_top, sheet_w, sheet.height), fill=bar)
    sheet = _draw_caption_table(sheet, draw, bar_top, pad_x, name, task, project, frame_w, fonts)
    sheet = _draw_triangles(sheet, bar_top, frame_w, fonts, triangles)
    sheet = _draw_date(sheet, frame_w, fonts)
    sheet = _paste_logo(sheet, logo, frame_w, bar_h)

    sheet = _fit_long_side(sheet, long_side)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)
    return output_path
