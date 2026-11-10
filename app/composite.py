"""Stitch rendered frames into one labeled sheet."""

from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf"):
        path = Path(r"C:\Windows\Fonts") / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _flatten(image, background):
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, background)
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


def _text_box(text, font):
    return ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), text, font=font)


def _footer_lines(name, task, frame_w):
    lines = []
    if task.strip():
        lines.append((task.strip(), _font(max(34, frame_w // 11)), (235, 235, 235)))
    if name.strip():
        lines.append((name.strip(), _font(max(18, frame_w // 18)), (205, 205, 205)))
    return lines


def _sheet_rows(passes, labels, grid):
    names = dict(PASS_LABELS)
    if labels:
        names.update(labels)
    rows = [(pass_name, names.get(pass_name, pass_name), False) for pass_name in passes]
    if grid:
        key = grid["pass"]
        rows.append((key, f"{names.get(key, key)} Wire", True))
    return rows


def measure_sheet(frame_w, frame_count, rows, name="", task=""):
    """Pixel size of the finished sheet when every frame is frame_w square."""
    frame_w = max(1, int(frame_w))
    footer = _footer_lines(name, task, frame_w)
    line_gap = max(78, frame_w // 9) if len(footer) > 1 else 0
    text_h = sum(_text_box(text, font)[3] - _text_box(text, font)[1] for text, font, _color in footer)
    pad_x = max(48, frame_w // 14)
    pad_top = max(72, frame_w // 9)
    pad_bottom = max(48, frame_w // 14)
    footer_h = 28 + text_h + line_gap + pad_bottom
    font = _font(max(22, frame_w // 13))
    widest = max(_text_box(label, font)[2] for _key, label, _grid in rows)
    label_w = widest + 36
    sheet_w = pad_x + label_w + frame_count * frame_w + pad_x
    sheet_h = pad_top + len(rows) * frame_w + footer_h
    return sheet_w, sheet_h


def frame_size_for_long_side(target, frame_count, passes, grid=None, name="", task="", labels=None):
    """Largest square frame whose sheet's long side is still within target."""
    rows = _sheet_rows(passes, labels, grid)
    if not rows or frame_count < 1:
        return max(1, int(target))
    target = max(1, int(target))
    best = 1
    low, high = 1, target
    while low <= high:
        mid = (low + high) // 2
        width, height = measure_sheet(mid, frame_count, rows, name, task)
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


def build_sheet(frame_dir, passes, frame_count, output_path, name="", task="", grid=None, labels=None, long_side=None, background=None):
    frame_dir = Path(frame_dir)
    background = tuple(background or SHEET_BG)
    rows = _sheet_rows(passes, labels, grid)
    if not rows:
        raise ValueError("Нет строк для листа")
    with Image.open(frame_dir / f"{rows[0][0]}_00.png") as first:
        frame_w, frame_h = first.size
    footer = _footer_lines(name, task, frame_w)
    line_gap = max(78, frame_w // 9) if len(footer) > 1 else 0
    text_h = sum(_text_box(text, font)[3] - _text_box(text, font)[1] for text, font, _color in footer)
    pad_x = max(48, frame_w // 14)
    pad_top = max(72, frame_w // 9)
    pad_bottom = max(48, frame_w // 14)
    footer_h = 28 + text_h + line_gap + pad_bottom
    font = _font(max(22, frame_w // 13))
    widest = max(_text_box(label, font)[2] for _key, label, _grid in rows)
    label_w = widest + 36
    sheet_w = pad_x + label_w + frame_count * frame_w + pad_x
    grid_top = pad_top
    sheet = Image.new("RGB", (sheet_w, grid_top + len(rows) * frame_h + footer_h), background)
    draw = ImageDraw.Draw(sheet)

    for row, (pass_name, label, with_grid) in enumerate(rows):
        bbox = draw.textbbox((0, 0), label, font=font)
        text_h = bbox[3] - bbox[1]
        x = pad_x - bbox[0]
        y = grid_top + row * frame_h + (frame_h - text_h) / 2 - bbox[1]
        draw.text((x, y), label, fill=(235, 235, 235), font=font)
        for col in range(frame_count):
            filename = f"wire_{col:02d}.png" if with_grid else f"{pass_name}_{col:02d}.png"
            with Image.open(frame_dir / filename) as frame:
                sheet.paste(_flatten(frame, background), (pad_x + label_w + col * frame_w, grid_top + row * frame_h))

    y = grid_top + len(rows) * frame_h + 28
    name_y = None
    for index, (text, footer_font, color) in enumerate(footer):
        bbox = draw.textbbox((0, 0), text, font=footer_font)
        text_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        draw.text(((sheet_w - text_w) / 2 - bbox[0], y - bbox[1]), text, fill=color, font=footer_font)
        if index == len(footer) - 1:
            name_y = y
            name_h = line_h
        y += line_h + (line_gap if index == 0 and len(footer) > 1 else 0)

    stamp = date.today().strftime("%d.%m.%Y")
    date_font = _font(max(22, frame_w // 15))
    date_box = draw.textbbox((0, 0), stamp, font=date_font)
    date_w = date_box[2] - date_box[0]
    date_h = date_box[3] - date_box[1]
    if name_y is None:
        date_top = sheet.height - pad_bottom - date_h
    else:
        date_top = name_y + (name_h - date_h) / 2
    draw.text(
        (sheet_w - pad_x - date_w - date_box[0], date_top - date_box[1]),
        stamp,
        fill=(176, 176, 176),
        font=date_font,
    )

    sheet = _fit_long_side(sheet, long_side)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)
    return output_path
