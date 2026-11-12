"""Desktop window: FBX in, turnaround sheet out. Blender stays in the background."""

import ctypes
import json
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QImageReader,
    QIcon,
    QKeySequence,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from PIL import Image, UnidentifiedImageError

from app import VERSION
from app.bridge import publish, take_request, unpublish
from app.composite import PASS_LABELS, build_sheet, frame_size_for_long_side
from app.paths import resource_root
from app.runner import DEFAULT_BLENDER, BlenderError, inspect_fbx, render_sheet

CHANNELS = (
    ("basecolor", "Base Color"),
    ("normal", "Normal"),
    ("roughness", "Roughness"),
    ("metallic", "Metallic"),
    ("ao", "AO"),
    ("opacity", "Opacity"),
)
OWL_CHANNELS = (
    ("basecolor", "Base Color"),
    ("masks", "Masks"),
    ("normal", "Normal"),
    ("special", "Special Masks"),
)
ALL_CHANNELS = OWL_CHANNELS + (
    ("roughness", "Roughness"),
    ("metallic", "Metallic"),
    ("ao", "AO"),
    ("opacity", "Opacity"),
)
PASSES = (
    ("material", "Material"),
    ("basecolor", "Base Color"),
    ("normal", "Normal"),
    ("roughness", "Roughness"),
    ("metallic", "Metallic"),
    ("ao", "AO"),
    ("special", "Special Masks"),
)
DEFAULT_PASSES = {"basecolor", "normal", "roughness", "material"}
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.tga *.tif *.tiff *.exr *.bmp)"
# Character mask: R roughness (bright is rough), G emissive, B metallic, A translucency.
MASK_CHANNEL_NAMES = ("Roughness", "Emissive", "Metallic", "Translucency")


def split_mask_file(path):
    """Write each mask channel next to the source. Alpha is included only when the file has one."""
    source = Path(path)
    with Image.open(source) as image:
        image.load()
        has_alpha = "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)
        flat = image.convert("RGBA" if has_alpha else "RGB")
        bands = [band.copy() for band in flat.split()]
    written = []
    names = MASK_CHANNEL_NAMES if has_alpha else MASK_CHANNEL_NAMES[:3]
    for band, name in zip(bands, names):
        rgb = Image.merge("RGB", (band, band, band))
        dest = source.with_name(f"{source.stem}_{name}{source.suffix}")
        rgb.save(dest)
        written.append(dest)
    return written


def apply_dark(app):
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.Base, QColor(32, 32, 32))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(48, 48, 48))
    palette.setColor(QPalette.ColorRole.Text, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.Button, QColor(58, 58, 58))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(70, 112, 158))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(32, 32, 32))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))
    # setColor(role, color) paints every group, including Disabled, so a switched-off
    # checkbox stayed the same white as a live one.
    dim = QPalette.ColorGroup.Disabled
    palette.setColor(dim, QPalette.ColorRole.WindowText, QColor(110, 110, 110))
    palette.setColor(dim, QPalette.ColorRole.Text, QColor(110, 110, 110))
    palette.setColor(dim, QPalette.ColorRole.ButtonText, QColor(110, 110, 110))
    palette.setColor(dim, QPalette.ColorRole.Button, QColor(48, 48, 48))
    app.setPalette(palette)


class DropLine(QLineEdit):
    file_dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText("not set")
        self.setClearButtonEnabled(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.setText(path)
            self.file_dropped.emit(path)
            event.acceptProposedAction()


class JobThread(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    failed = Signal(str)
    succeeded = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.proc_slot = []
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.proc_slot:
            self.proc_slot[0].kill()

    def run(self):
        try:
            result = self._fn(self)
        except Exception as exc:
            if not self.cancelled:
                self.failed.emit(str(exc))
            return
        if not self.cancelled:
            self.succeeded.emit(result)


def _pixmap_fitted(path, width, height=0):
    """Decode only as large as the view. Qt otherwise refuses sheets over 256 MB."""
    reader = QImageReader(str(path))
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0 and width > 0:
        if height > 0:
            scale = min(width / size.width(), height / size.height())
            width = max(1, round(size.width() * scale))
            height = max(1, round(size.height() * scale))
            reader.setScaledSize(QSize(width, height))
        elif size.width() > width:
            height = max(1, round(size.height() * width / size.width()))
            reader.setScaledSize(QSize(width, height))
    image = reader.read()
    if image.isNull():
        return QPixmap(), reader.errorString()
    return QPixmap.fromImage(image), ""


class SheetWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sheet")
        self.resize(1200, 860)
        self._path = ""
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumSize(0, 0)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label.setStyleSheet("background: #2a2a2a;")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

    def show_sheet(self, path):
        self._path = path
        self.setWindowTitle(Path(path).name)
        self.show()
        self.raise_()
        self.activateWindow()
        self._fit()
        QTimer.singleShot(0, self._fit)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        if not self._path or not Path(self._path).is_file():
            return
        view = self.scroll.viewport()
        width = view.width()
        height = view.height()
        if width <= 50 or height <= 50:
            rect = self.contentsRect()
            width, height = rect.width(), rect.height()
        pixmap, error = _pixmap_fitted(self._path, max(1, width), max(1, height))
        if pixmap.isNull():
            self.label.setText(f"Saved, but the preview failed to open:\n{error}")
            return
        self.label.setPixmap(pixmap)
        self.label.setMinimumSize(0, 0)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Screenshot Utility {VERSION}")
        self.resize(880, 820)
        self.setAcceptDrops(True)
        self._settings = QSettings("ScreenshotUtility", "ScreenshotUtility")
        self._thread = None
        self._slots = {}
        self._output_auto = True
        self._preview_path = ""
        self._sheet_window = None
        self._loaded_fbx = ""
        self._triangles = None
        self._loading_slots = False
        self._render_after_load = False
        self._render_when_free = False
        self._build()
        self._restore()
        publish()
        self._inbox_timer = QTimer(self)
        self._inbox_timer.timeout.connect(self._take_inbox)
        self._inbox_timer.start(500)

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_materials())
        splitter.addWidget(self._build_settings())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self._splitter = splitter
        self._settings_halved = False
        layout.addWidget(splitter, 2)

        self.preview = QLabel("The sheet will show up here")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(0, 0)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview.setMinimumHeight(220)
        self.preview.setStyleSheet("background: #2a2a2a;")
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setWidget(self.preview)
        layout.addWidget(preview_scroll, 3)
        self.preview_scroll = preview_scroll

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(88)
        self.log.setPlaceholderText("Blender log")
        layout.addWidget(self.log)

        blender_row = QHBoxLayout()
        blender_row.addWidget(QLabel("Blender"))
        self.blender_edit = QLineEdit()
        self.blender_edit.setPlaceholderText(str(DEFAULT_BLENDER))
        blender_row.addWidget(self.blender_edit, 1)
        browse_blender = QPushButton("Browse")
        browse_blender.clicked.connect(self._browse_blender)
        blender_row.addWidget(browse_blender)
        layout.addLayout(blender_row)

        foot = QHBoxLayout()
        version = QLabel(VERSION)
        version.setStyleSheet("color: #8a8a8a;")
        foot.addWidget(version)
        repo = QPushButton("GitHub")
        repo.setToolTip("Open the GitHub repository")
        repo.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/stogovdmitry-a11y/fbx-screenshot-utility")
            )
        )
        foot.addWidget(repo)
        foot.addStretch(1)
        layout.addLayout(foot)

    def _build_materials(self):
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)

        captions = QFormLayout()
        captions.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.caption_task = QLineEdit()
        self.caption_task.setPlaceholderText("Printed on the sheet as typed")
        captions.addRow("Task", self.caption_task)
        self.caption_project = QLineEdit()
        self.caption_project.setPlaceholderText("Printed on the sheet as typed")
        captions.addRow("Project", self.caption_project)
        self.caption_name = QLineEdit()
        self.caption_name.setPlaceholderText("Printed on the sheet as typed")
        captions.addRow("Artist", self.caption_name)
        column.addLayout(captions)

        row = QHBoxLayout()
        self.fbx_edit = QLineEdit()
        self.fbx_edit.setPlaceholderText("Drop an FBX here or browse")
        self.fbx_edit.returnPressed.connect(lambda: self.load_fbx(follow_fbx=True))
        row.addWidget(self.fbx_edit, 1)
        self.open_btn = QPushButton("Open FBX")
        self.open_btn.clicked.connect(self._browse_fbx)
        row.addWidget(self.open_btn)
        column.addLayout(row)

        hint = QLabel("An empty map keeps whatever is already in the FBX. Slots stay as in the file, head and hair separate.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a8a8a8;")
        column.addWidget(hint)

        self.reset_btn = QPushButton("Reset textures")
        self.reset_btn.setEnabled(False)
        self.reset_btn.clicked.connect(self._reset_textures)
        column.addWidget(self.reset_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self.split_btn = QPushButton("Split Masks Texture")
        self.split_btn.setEnabled(False)
        self.split_btn.setToolTip(
            "Splits the Masks map into separate textures next to the file. "
            "R Roughness, G Emissive, B Metallic, and A Translucency when the file has alpha."
        )
        self.split_btn.clicked.connect(self._split_masks)

        self.slot_host = QWidget()
        self.slot_layout = QVBoxLayout(self.slot_host)
        self.slot_layout.setContentsMargins(0, 0, 0, 0)
        self.slot_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.slot_host)
        column.addWidget(scroll, 1)
        return panel

    def _build_settings(self):
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.frames = QSpinBox()
        self.frames.setRange(1, 36)
        self.frames.setValue(6)
        self.frames.setToolTip("Cameras are spaced evenly around the model. Two frames are front and side.")
        form.addRow("Frames", self.frames)

        self.start = QDoubleSpinBox()
        self.start.setRange(-360, 360)
        self.start.setDecimals(1)
        self.start.setValue(0)
        self.start.setSuffix("°")
        self.start.setToolTip("0° looks from the chosen side. Orbit is around Z.")
        form.addRow("Start", self.start)

        self.distance = QDoubleSpinBox()
        self.distance.setRange(0.4, 3)
        self.distance.setSingleStep(0.05)
        self.distance.setDecimals(2)
        self.distance.setValue(1.0)
        self.distance.setToolTip("Multiplier on the distance fitted to the model bounds.")
        form.addRow("Distance", self.distance)

        self.height = QDoubleSpinBox()
        self.height.setRange(-2, 2)
        self.height.setSingleStep(0.05)
        self.height.setDecimals(2)
        self.height.setValue(0)
        self.height.setToolTip(
            "Camera pitch. 1.0 looks down at 45°, 2.0 is about 63°. The camera backs up so the model stays in frame."
        )
        form.addRow("Height", self.height)

        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(-2, 3)
        self.exposure.setSingleStep(0.05)
        self.exposure.setDecimals(2)
        self.exposure.setValue(0.15)
        self.exposure.setToolTip(
            "Stops on the lit material row. 0.15 is the usual brightness. Texture rows stay as they are."
        )
        form.addRow("Exposure", self.exposure)

        self.frame_size = QComboBox()
        for size in (384, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192):
            self.frame_size.addItem(str(size), size)
        self.frame_size.setCurrentIndex(2)
        self.frame_size.setToolTip("Long side of the finished sheet, not of one frame.")
        form.addRow("Long side", self.frame_size)

        self.gap = QSpinBox()
        self.gap.setRange(0, 400)
        self.gap.setSingleStep(4)
        self.gap.setValue(0)
        self.gap.setSuffix(" px")
        self.gap.setToolTip("Space between frames, in frame pixels. It scales with the sheet.")
        form.addRow("Gap", self.gap)

        self.front = QComboBox()
        for value, label in (("-Y", "−Y"), ("+Y", "+Y"), ("-X", "−X"), ("+X", "+X")):
            self.front.addItem(label, value)
        self.front.setToolTip("Which side of the model is the front. Up is Z, as after importing the FBX into Blender.")
        form.addRow("Front", self.front)
        column.addWidget(form_host)

        flags = QVBoxLayout()
        self.invert_g = QCheckBox("Invert normal G (DirectX)")
        flags.addWidget(self.invert_g)

        self.raytracing = QCheckBox("Raytracing")
        self.raytracing.setChecked(True)
        self.raytracing.setToolTip(
            "Screen-trace reflections on the Material row, and the contact AO that comes with it. Shadows stay either way."
        )
        flags.addWidget(self.raytracing)

        self.cycles = QCheckBox("Cycles")
        self.cycles.setToolTip(
            "Path-trace the Material row on the GPU. Real shadows and GI. The other rows stay on EEVEE."
        )
        self.cycles.toggled.connect(self._toggle_cycles)
        flags.addWidget(self.cycles)

        self.character = QCheckBox("Character Pipeline")
        self.character.setToolTip(
            "Masks: R roughness, G emissive, B metallic, A translucency. "
            "Opacity is in the Base Color alpha, and that checkbox is off by default."
        )
        self.character.toggled.connect(self._apply_pipeline)
        flags.addWidget(self.character)

        self._bg_color = QColor("#3c3c3c")
        self.bg_color_btn = QPushButton()
        self.bg_color_btn.setFixedWidth(88)
        self.bg_color_btn.clicked.connect(self._pick_bg_color)
        bg_row = QHBoxLayout()
        bg_row.addWidget(QLabel("Background"))
        bg_row.addWidget(self.bg_color_btn)
        bg_row.addStretch(1)
        flags.addLayout(bg_row)
        self._paint_bg_button()

        self._emit_color = QColor("#ffffff")
        self.emit_color_btn = QPushButton()
        self.emit_color_btn.setFixedSize(36, 22)
        self.emit_color_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.emit_color_btn.setToolTip("Color of the glow. The mask's green channel is only the brightness.")
        self.emit_color_btn.clicked.connect(self._pick_emit_color)
        self.emit_strength = QDoubleSpinBox()
        self.emit_strength.setRange(0, 50)
        self.emit_strength.setSingleStep(0.25)
        self.emit_strength.setDecimals(2)
        self.emit_strength.setValue(1)
        self.emit_strength.setToolTip("Multiplier on the mask's green channel.")
        self._fit_number(self.emit_strength)
        emit_row = QHBoxLayout()
        emit_row.setSpacing(14)
        self.emission_enabled = QCheckBox("Emission")
        self.emission_enabled.setToolTip("Uses the mask's green channel as brightness. Off by default.")
        self.emission_enabled.toggled.connect(self._toggle_emission)
        emit_row.addWidget(self.emission_enabled)
        emit_row.addWidget(self.emit_color_btn)
        emit_row.addWidget(self.emit_strength)
        self.bloom = QDoubleSpinBox()
        self.bloom.setRange(0, 8)
        self.bloom.setSingleStep(0.1)
        self.bloom.setDecimals(2)
        self.bloom.setValue(1)
        self.bloom.setToolTip("Halo around the emission on the Material row. 0 turns it off.")
        self._fit_number(self.bloom)
        bloom_label = QLabel("Bloom")
        bloom_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        emit_row.addWidget(bloom_label)
        emit_row.addWidget(self.bloom)
        emit_row.addStretch(1)
        self._paint_emit_button()
        self._toggle_emission(False)

        self.surface_model = QComboBox()
        self.surface_model.addItem("Smoothness", "smoothness")
        self.surface_model.addItem("Roughness", "roughness")
        self.surface_model.setToolTip("Smoothness: bright is smooth. Roughness: bright is rough.")
        self.surface_model.currentIndexChanged.connect(self._apply_surface_model)
        surface_row = QHBoxLayout()
        self._surface_caption = QLabel("Highlight")
        surface_row.addWidget(self._surface_caption)
        surface_row.addWidget(self.surface_model, 1)
        flags.addLayout(surface_row)
        flags.addStretch(1)

        passes = QGroupBox("Rows")
        pass_list = QVBoxLayout(passes)
        self.pass_boxes = {}
        for key, label in PASSES:
            box = QCheckBox(label)
            box.setChecked(key in DEFAULT_PASSES)
            self.pass_boxes[key] = box
            pass_list.addWidget(box)

        grid_box = QGroupBox("Wireframe row")
        grid_box.setCheckable(True)
        grid_box.setChecked(False)
        grid_form = QFormLayout(grid_box)
        self.grid_enabled = grid_box
        self.grid_pass = QComboBox()
        for key, label in PASSES:
            self.grid_pass.addItem(label, key)
        material_index = self.grid_pass.findData("material")
        if material_index >= 0:
            self.grid_pass.setCurrentIndex(material_index)
        grid_form.addRow("Type", self.grid_pass)
        self.grid_thickness = QSpinBox()
        self.grid_thickness.setRange(1, 32)
        self.grid_thickness.setValue(2)
        self.grid_thickness.setSuffix(" px")
        grid_form.addRow("Thickness", self.grid_thickness)
        self._wire_color = QColor("#111111")
        self.wire_color_btn = QPushButton()
        self.wire_color_btn.setFixedWidth(72)
        self.wire_color_btn.clicked.connect(self._pick_wire_color)
        grid_form.addRow("Color", self.wire_color_btn)
        self._paint_wire_button()
        grid_box.toggled.connect(self._set_grid_enabled)
        self._set_grid_enabled(False)

        side = QHBoxLayout()
        side.setSpacing(16)
        side.addLayout(flags)
        side.addWidget(passes, 0, Qt.AlignmentFlag.AlignTop)
        side.addWidget(grid_box, 0, Qt.AlignmentFlag.AlignTop)
        side.addStretch(1)
        column.addLayout(side)
        column.addLayout(emit_row)

        self.date_in_name = QCheckBox("Date in the file name")
        self.date_in_name.toggled.connect(self._refresh_output_name)
        self.number_if_exists = QCheckBox("Number the file if it already exists")
        self.separate_window = QCheckBox("Open the sheet in a separate window")
        self.separate_window.toggled.connect(self._toggle_separate_preview)
        self.triangles_enabled = QCheckBox("Triangle count")
        self.triangles_enabled.setToolTip("Prints Triangles Count just above the dark footer.")

        checks = QVBoxLayout()
        checks.addWidget(self.date_in_name)
        checks.addWidget(self.number_if_exists)
        checks.addWidget(self.separate_window)
        checks.addWidget(self.triangles_enabled)
        checks.addSpacing(10)
        logo_row = QHBoxLayout()
        self.logo_enabled = QCheckBox("Logo")
        self.logo_enabled.toggled.connect(self._toggle_logo)
        logo_row.addWidget(self.logo_enabled)
        self.logo_edit = QLineEdit()
        self.logo_edit.setPlaceholderText("None")
        self.logo_edit.setClearButtonEnabled(True)
        logo_row.addWidget(self.logo_edit, 1)
        self.logo_browse = QPushButton("Browse")
        self.logo_browse.clicked.connect(self._browse_logo)
        logo_row.addWidget(self.logo_browse)
        checks.addLayout(logo_row)
        self._toggle_logo(False)
        checks.addStretch(1)

        text_box = QGroupBox("Text")
        text_form = QFormLayout(text_box)
        text_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.font_labels = self._font_size(40, "Row names on the left.")
        self.font_caption = self._font_size(28, "Task, Project and Artist in the bottom bar.")
        self.font_date = self._font_size(32, "Date in the top right corner.")
        text_form.addRow("Labels", self.font_labels)
        text_form.addRow("Caption", self.font_caption)
        text_form.addRow("Date", self.font_date)

        options = QHBoxLayout()
        options.addLayout(checks, 1)
        options.addWidget(text_box, 1)
        column.addLayout(options)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output Directory"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Where to save the PNG")
        self.output_edit.textEdited.connect(self._output_edited)
        output_row.addWidget(self.output_edit, 1)
        browse_out = QPushButton("Browse")
        browse_out.clicked.connect(self._browse_output)
        output_row.addWidget(browse_out)
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(self._open_output_folder)
        output_row.addWidget(open_folder)
        column.addLayout(output_row)

        buttons = QHBoxLayout()
        self.render_btn = QPushButton("Build sheet (F12)")
        self.render_btn.setShortcut(QKeySequence(Qt.Key.Key_F12))
        self.render_btn.setEnabled(False)
        self.render_btn.clicked.connect(self.render)
        self.render_btn.setStyleSheet("padding: 8px 18px; font-weight: 600;")
        buttons.addWidget(self.render_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch(1)
        column.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        column.addWidget(self.progress)
        self.status = QLabel("Open an FBX")
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        column.addStretch(1)
        panel.setMinimumWidth(420)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        return panel

    def _restore(self):
        blender = self._settings.value("blender", str(DEFAULT_BLENDER))
        self.blender_edit.setText(str(blender))
        self.frames.setValue(int(self._settings.value("frames", 6)))
        self.start.setValue(float(self._settings.value("start", 0)))
        self.distance.setValue(float(self._settings.value("distance", 1)))
        self.height.setValue(float(self._settings.value("height", 0)))
        self.exposure.setValue(float(self._settings.value("exposure", 0.15)))
        self.gap.setValue(int(self._settings.value("gap", 0)))
        saved_size = int(self._settings.value("frame_size", 768))
        index = self.frame_size.findData(saved_size)
        if index >= 0:
            self.frame_size.setCurrentIndex(index)
        front = self._settings.value("front", "-Y")
        front_index = self.front.findData(front)
        if front_index >= 0:
            self.front.setCurrentIndex(front_index)
        self.invert_g.setChecked(self._settings.value("invert_g", False) in (True, "true", "1"))
        self.raytracing.setChecked(self._settings.value("raytracing", True) in (True, "true", "1"))
        self.cycles.setChecked(self._settings.value("cycles", False) in (True, "true", "1"))
        self._toggle_cycles(self.cycles.isChecked())
        saved_pipeline = self._settings.value("character", self._settings.value("owlcat", True))
        self.character.setChecked(saved_pipeline in (True, "true", "1"))
        saved_bg = self._settings.value("bg_color", "#3c3c3c") or "#3c3c3c"
        self._bg_color = QColor(str(saved_bg))
        if not self._bg_color.isValid():
            self._bg_color = QColor("#3c3c3c")
        self._paint_bg_button()
        saved_emit = self._settings.value("emission_color", "#ffffff") or "#ffffff"
        self._emit_color = QColor(str(saved_emit))
        if not self._emit_color.isValid():
            self._emit_color = QColor("#ffffff")
        self._paint_emit_button()
        self.emit_strength.setValue(float(self._settings.value("emission_strength", 1)))
        self.bloom.setValue(float(self._settings.value("bloom", 1)))
        self.emission_enabled.setChecked(self._settings.value("emission_enabled", False) in (True, "true", "1"))
        surface = self._settings.value("surface_model", "smoothness") or "smoothness"
        if surface == "specular":
            surface = "smoothness"
        surface_index = self.surface_model.findData(surface)
        if surface_index >= 0:
            self.surface_model.setCurrentIndex(surface_index)
        self._apply_surface_model()
        saved_passes = self._settings.value("passes", list(DEFAULT_PASSES))
        if isinstance(saved_passes, str):
            saved_passes = saved_passes.split(",") if saved_passes else []
        if saved_passes:
            for key, box in self.pass_boxes.items():
                box.setChecked(key in saved_passes)
        self.caption_name.setText(self._settings.value("caption_name", "") or "")
        self.caption_task.setText(self._settings.value("caption_task", "") or "")
        self.caption_project.setText(self._settings.value("caption_project", "") or "")
        self.date_in_name.setChecked(self._settings.value("date_in_name", False) in (True, "true", "1"))
        self.number_if_exists.setChecked(self._settings.value("number_if_exists", False) in (True, "true", "1"))
        self.separate_window.setChecked(self._settings.value("separate_window", False) in (True, "true", "1"))
        self.triangles_enabled.setChecked(self._settings.value("triangles_enabled", False) in (True, "true", "1"))
        self.logo_edit.setText(self._settings.value("logo_path", "") or "")
        self.logo_enabled.setChecked(self._settings.value("logo_enabled", False) in (True, "true", "1"))
        self.grid_enabled.setChecked(self._settings.value("grid_enabled", False) in (True, "true", "1"))
        grid_pass = self._settings.value("grid_pass", "material") or "material"
        grid_index = self.grid_pass.findData(grid_pass)
        if grid_index >= 0:
            self.grid_pass.setCurrentIndex(grid_index)
        self.grid_thickness.setValue(int(self._settings.value("grid_thickness", 2)))
        saved_color = self._settings.value("wire_color", "#111111") or "#111111"
        self._wire_color = QColor(str(saved_color))
        if not self._wire_color.isValid():
            self._wire_color = QColor("#111111")
        self._paint_wire_button()
        self._output_auto = self._settings.value("output_auto", True) in (True, "true", "1")
        saved_output = self._settings.value("output_path", "") or ""
        if saved_output:
            self.output_edit.setText(str(saved_output))
        self.font_labels.setValue(int(self._settings.value("font_labels", 40)))
        saved_caption = self._settings.value("font_caption", None)
        if saved_caption in (None, ""):
            saved_caption = self._settings.value("font_name", 28)
        self.font_caption.setValue(int(saved_caption))
        self.font_date.setValue(int(self._settings.value("font_date", 32)))
        last_fbx = self._settings.value("last_fbx", "") or ""
        if self._take_inbox():
            return
        if last_fbx and Path(last_fbx).is_file():
            self.fbx_edit.setText(last_fbx)
            self.load_fbx()

    def _save_settings(self):
        self._settings.setValue("blender", self.blender_edit.text().strip())
        self._settings.setValue("frames", self.frames.value())
        self._settings.setValue("start", self.start.value())
        self._settings.setValue("distance", self.distance.value())
        self._settings.setValue("height", self.height.value())
        self._settings.setValue("exposure", self.exposure.value())
        self._settings.setValue("gap", self.gap.value())
        self._settings.setValue("frame_size", self.frame_size.currentData())
        self._settings.setValue("front", self.front.currentData())
        self._settings.setValue("invert_g", self.invert_g.isChecked())
        self._settings.setValue("raytracing", self.raytracing.isChecked())
        self._settings.setValue("cycles", self.cycles.isChecked())
        self._settings.setValue("character", self.character.isChecked())
        self._settings.setValue("bg_color", self._bg_color.name())
        self._settings.setValue("emission_color", self._emit_color.name())
        self._settings.setValue("emission_strength", self.emit_strength.value())
        self._settings.setValue("emission_enabled", self.emission_enabled.isChecked())
        self._settings.setValue("bloom", self.bloom.value())
        self._settings.setValue("surface_model", self.surface_model.currentData())
        self._settings.setValue("passes", [key for key, box in self.pass_boxes.items() if box.isChecked()])
        self._settings.setValue("caption_name", self.caption_name.text())
        self._settings.setValue("caption_task", self.caption_task.text())
        self._settings.setValue("caption_project", self.caption_project.text())
        self._settings.setValue("date_in_name", self.date_in_name.isChecked())
        self._settings.setValue("number_if_exists", self.number_if_exists.isChecked())
        self._settings.setValue("separate_window", self.separate_window.isChecked())
        self._settings.setValue("triangles_enabled", self.triangles_enabled.isChecked())
        self._settings.setValue("logo_path", self.logo_edit.text().strip())
        self._settings.setValue("logo_enabled", self.logo_enabled.isChecked())
        self._settings.setValue("grid_enabled", self.grid_enabled.isChecked())
        self._settings.setValue("grid_pass", self.grid_pass.currentData())
        self._settings.setValue("grid_thickness", self.grid_thickness.value())
        self._settings.setValue("wire_color", self._wire_color.name())
        self._settings.setValue("output_path", self.output_edit.text().strip())
        self._settings.setValue("output_auto", self._output_auto)
        self._settings.setValue("font_labels", self.font_labels.value())
        self._settings.setValue("font_caption", self.font_caption.value())
        self._settings.setValue("font_date", self.font_date.value())
        if self._loaded_fbx:
            self._settings.setValue("last_fbx", self._loaded_fbx)
        self._save_textures()

    def closeEvent(self, event):
        unpublish()
        self._save_settings()
        if self._sheet_window is not None:
            self._sheet_window.close()
        if self._thread is not None and self._thread.isRunning():
            self._thread.cancel()
            self._thread.wait(3000)
        super().closeEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".fbx"):
                self.fbx_edit.setText(path)
                self.load_fbx(follow_fbx=True)
                event.acceptProposedAction()
                return
        event.ignore()

    def _browse_fbx(self):
        path, _ = QFileDialog.getOpenFileName(self, "FBX", self._dir_hint(), "FBX (*.fbx)")
        if path:
            self.fbx_edit.setText(path)
            self.load_fbx(follow_fbx=True)

    def _browse_blender(self):
        path, _ = QFileDialog.getOpenFileName(self, "blender.exe", self.blender_edit.text(), "blender.exe (blender.exe)")
        if path:
            self.blender_edit.setText(path)

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "Sheet", self.output_edit.text() or str(self._output_dir()), "PNG (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            self._output_auto = False
            self._remember_output_dir(path)
            self.output_edit.setText(self._dated_output(path))

    def _texture_size(self, path):
        reader = QImageReader(str(path))
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            return size.width(), size.height()
        try:
            with Image.open(path) as image:
                return image.size
        except (OSError, UnidentifiedImageError):
            return None

    def _sheet_sizes(self, materials):
        found = {}
        for maps in materials.values():
            for key in ("basecolor", "normal", "roughness", "metallic", "ao", "special"):
                path = (maps.get(key) or "").strip()
                if not path or not Path(path).is_file():
                    continue
                wh = self._texture_size(path)
                if not wh:
                    continue
                found.setdefault(key, [])
                if wh not in found[key]:
                    found[key].append(wh)
        return {key: "  ".join(f"{w}×{h}" for w, h in pairs) for key, pairs in found.items()}

    def _triangle_count(self, folder):
        stats = Path(folder) / "stats.json"
        if stats.is_file():
            try:
                value = json.loads(stats.read_text(encoding="utf-8")).get("triangles")
            except (OSError, json.JSONDecodeError, TypeError):
                value = None
            if isinstance(value, int):
                return value
        return self._triangles

    def _toggle_logo(self, enabled):
        self.logo_edit.setEnabled(enabled)
        self.logo_browse.setEnabled(enabled)

    def _browse_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Logo", self.logo_edit.text() or self._texture_dir(), IMAGE_FILTER)
        if path:
            self.logo_edit.setText(path)
            self._remember_texture_dir(path)

    def _output_edited(self):
        self._output_auto = not bool(self.output_edit.text().strip())

    def _open_output_folder(self):
        output = self.output_edit.text().strip()
        folder = Path(output).parent if output else Path(self._output_dir())
        if not folder.is_dir():
            QMessageBox.warning(self, "Screenshot Utility", "That folder isn't there yet")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _dated_output(self, path):
        target = Path(path)
        stem = re.sub(r"_\d{2}\.\d{2}\.\d{4}(?:_\d+)?$", "", target.stem)
        if self.number_if_exists.isChecked() and not self.date_in_name.isChecked():
            stem = re.sub(r"_\d+$", "", stem)
        if self.date_in_name.isChecked():
            stem = f"{stem}_{date.today().strftime('%d.%m.%Y')}"
        suffix = target.suffix if target.suffix else ".png"
        target = target.with_name(stem + suffix)
        if self.number_if_exists.isChecked():
            target = self._free_numbered(target)
        return str(target)

    def _free_numbered(self, target):
        if not target.exists():
            return target
        number = 2
        while True:
            candidate = target.with_name(f"{target.stem}_{number}{target.suffix}")
            if not candidate.exists():
                return candidate
            number += 1

    def _refresh_output_name(self):
        current = self.output_edit.text().strip()
        if current:
            self.output_edit.setText(self._dated_output(current))

    def _output_dir(self):
        saved = self._settings.value("output_dir", "")
        if saved and Path(str(saved)).is_dir():
            return Path(str(saved))
        fbx = self.fbx_edit.text().strip()
        if fbx:
            return Path(fbx).parent
        return Path(self._dir_hint())

    def _remember_output_dir(self, path):
        folder = Path(path).parent
        if folder.is_dir():
            self._settings.setValue("output_dir", str(folder))

    def _auto_output(self, fbx):
        return self._dated_output(self._output_dir() / (Path(fbx).stem + "_sheet.png"))

    def _dir_hint(self):
        fbx = self.fbx_edit.text().strip()
        if fbx:
            return str(Path(fbx).parent)
        saved = self._settings.value("fbx_dir", "")
        if saved and Path(str(saved)).is_dir():
            return str(saved)
        return str(Path.home())

    def _texture_dir(self):
        saved = self._settings.value("texture_dir", "")
        if saved and Path(str(saved)).is_dir():
            return str(saved)
        return self._dir_hint()

    def _remember_texture_dir(self, path):
        folder = Path(path).parent
        if folder.is_dir():
            self._settings.setValue("texture_dir", str(folder))

    def _blender_path(self):
        text = self.blender_edit.text().strip()
        return text or str(DEFAULT_BLENDER)

    def _set_busy(self, busy):
        self.open_btn.setEnabled(not busy)
        self.render_btn.setEnabled(not busy and bool(self._slots))
        self.reset_btn.setEnabled(not busy and bool(self._slots))
        self.split_btn.setEnabled(not busy and bool(self._slots))
        self.cancel_btn.setEnabled(busy)
        if not busy:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)

    def _cancel(self):
        if self._thread is not None:
            self._thread.cancel()
            self.status.setText("Stopping…")

    def _log_line(self, line):
        self.log.appendPlainText(line)

    def _watch_line(self, thread, line):
        if line.startswith("PROGRESS"):
            parts = line.split()
            if len(parts) >= 4:
                thread.progress.emit(int(parts[1]), int(parts[2]), parts[3])
            return
        interesting = (
            line.startswith("RESULT")
            or line.startswith("CYCLES")
            or line.startswith("Traceback")
            or "Error" in line
            or "Exception" in line
            or line.strip().startswith("File ")
        )
        if interesting:
            thread.log.emit(line)

    def _start(self, fn, on_ok):
        if self._thread is not None and self._thread.isRunning():
            return
        self._save_settings()
        self._set_busy(True)
        thread = JobThread(fn)
        thread.log.connect(self._log_line)
        thread.progress.connect(self._on_progress)
        thread.failed.connect(self._fail)
        thread.succeeded.connect(on_ok)
        thread.finished.connect(self._on_job_finished)
        self._thread = thread
        thread.start()

    def _on_job_finished(self):
        if self.sender() is not self._thread:
            return
        cancelled = self._thread.cancelled
        self._set_busy(False)
        if cancelled:
            self._render_after_load = False
            self._render_when_free = False
            return
        if self._render_when_free:
            self._render_when_free = False
            self.render()

    def _take_inbox(self):
        if self._render_after_load or self._render_when_free:
            return False
        if self._thread is not None and self._thread.isRunning():
            return False
        request = take_request()
        if not request:
            return False
        fbx = str(request.get("fbx") or "")
        if not Path(fbx).is_file():
            self.status.setText("Blender sent a file, but it is already gone")
            return False
        name = str(request.get("name") or "").strip()
        if name and not self.caption_name.text().strip():
            self.caption_name.setText(name)
        self._render_after_load = bool(request.get("render", True))
        self.fbx_edit.setText(fbx)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.status.setText("From Blender…")
        self.load_fbx(follow_fbx=True)
        return True

    def _on_progress(self, done, total, pass_name):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(f"{PASS_LABELS.get(pass_name, pass_name)}   {done}/{total}")

    def _fail(self, text):
        if self.sender() is not self._thread:
            return
        self._render_after_load = False
        self._render_when_free = False
        self.log.appendPlainText(text)
        lines = [line for line in text.strip().splitlines() if line.strip()]
        QMessageBox.warning(self, "Screenshot Utility", "\n".join(lines[-12:]) or "Failed")

    def load_fbx(self, follow_fbx=False):
        fbx = self.fbx_edit.text().strip()
        if not fbx:
            return
        if not Path(fbx).is_file():
            QMessageBox.warning(self, "Screenshot Utility", "FBX not found")
            return
        folder = Path(fbx).resolve().parent
        self._settings.setValue("fbx_dir", str(folder))
        self._settings.setValue("last_fbx", str(Path(fbx).resolve()))
        # The save folder follows the FBX only when the user picks a new file.
        # A restart reloads the last FBX and keeps the folder from last time.
        if follow_fbx:
            self._settings.setValue("output_dir", str(folder))
            current = self.output_edit.text().strip()
            if self._output_auto or not current:
                name = Path(fbx).stem + "_sheet.png"
            else:
                name = Path(current).name
            self.output_edit.setText(self._dated_output(folder / name))
        elif not self.output_edit.text().strip():
            self.output_edit.setText(self._auto_output(fbx))
        self._save_textures()
        saved = self._textures_for(Path(fbx).resolve())
        blender = self._blender_path()
        self.status.setText("Reading FBX…")
        self.progress.setRange(0, 0)

        def work(thread):
            temp = Path(tempfile.mkdtemp(prefix="screenshot-utility-"))
            json_path = temp / "inspect.json"
            try:
                inspect_fbx(
                    blender,
                    fbx,
                    json_path,
                    on_line=lambda line: self._watch_line(thread, line),
                    proc_slot=thread.proc_slot,
                )
                return json.loads(json_path.read_text(encoding="utf-8")), saved
            except BlenderError:
                raise
            finally:
                shutil.rmtree(temp, ignore_errors=True)

        self._start(work, self._show_slots)

    def _show_slots(self, payload):
        if self.sender() is not self._thread:
            return
        pending = self._render_after_load
        self._render_after_load = False
        self._render_when_free = False
        if payload is None:
            self.status.setText("Cancelled")
            return
        info, saved = payload
        while self.slot_layout.count():
            item = self.slot_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.split_btn:
                widget.deleteLater()
        self._loading_slots = True
        self._slots = {}
        for mat in info.get("materials") or []:
            self._add_slot(mat, saved.get(mat["name"]) if isinstance(saved, dict) else None)
        self._loaded_fbx = str(Path(self.fbx_edit.text().strip()).resolve())
        triangles = info.get("triangles")
        self._triangles = int(triangles) if isinstance(triangles, int) else None
        self._loading_slots = False
        self._save_textures()
        self.slot_layout.addWidget(self.split_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.split_btn.setEnabled(bool(self._slots))
        self.slot_layout.addStretch(1)
        size = info.get("size") or [0, 0, 0]
        self.status.setText(
            f"Meshes: {info.get('mesh_count', 0)}, slots: {len(self._slots)}. "
            f"Size {size[0]:.3f} × {size[1]:.3f} × {size[2]:.3f}"
        )
        self.render_btn.setEnabled(bool(self._slots))
        if not self._slots:
            QMessageBox.information(self, "Screenshot Utility", "The FBX has no meshes")
            return
        self._render_when_free = pending

    def _active_channels(self):
        return OWL_CHANNELS if self.character.isChecked() else CHANNELS

    def _pass_enabled(self, key):
        owl = self.character.isChecked()
        if key == "special":
            return owl
        if key in {"roughness", "metallic", "ao"}:
            return not owl
        return True

    def _apply_pipeline(self, *_args):
        if not hasattr(self, "pass_boxes"):
            return
        owl = self.character.isChecked()
        active = {key for key, _label in self._active_channels()}
        for slot in self._slots.values():
            form = slot.get("form")
            if form is None:
                continue
            for key, host in slot.get("hosts", {}).items():
                form.setRowVisible(host, key in active)
        for key, box in self.pass_boxes.items():
            box.setVisible(self._pass_enabled(key))
        self.surface_model.setVisible(not owl)
        self._surface_caption.setVisible(not owl)
        self._toggle_emission(self.emission_enabled.isChecked())
        self._refill_grid_passes()

    def _refill_grid_passes(self):
        if not hasattr(self, "grid_pass"):
            return
        current = self.grid_pass.currentData()
        self.grid_pass.blockSignals(True)
        self.grid_pass.clear()
        for key, label in PASSES:
            if not self._pass_enabled(key):
                continue
            shown = self._channel_label(key) if key == "roughness" else label
            self.grid_pass.addItem(shown, key)
        index = self.grid_pass.findData(current)
        if index < 0:
            index = self.grid_pass.findData("material")
        if index >= 0:
            self.grid_pass.setCurrentIndex(index)
        self.grid_pass.blockSignals(False)

    def _channel_label(self, key):
        if key == "roughness" and self.surface_model.currentData() != "roughness":
            return "Smoothness"
        return dict(ALL_CHANNELS)[key]

    def _pass_labels(self):
        labels = dict(PASS_LABELS)
        labels["roughness"] = self._channel_label("roughness")
        return labels

    def _apply_surface_model(self, *_args):
        text = self._channel_label("roughness")
        if "roughness" in getattr(self, "pass_boxes", {}):
            self.pass_boxes["roughness"].setText(text)
        if hasattr(self, "grid_pass"):
            index = self.grid_pass.findData("roughness")
            if index >= 0:
                self.grid_pass.setItemText(index, text)
            self._refill_grid_passes()
        for slot in self._slots.values():
            label = slot.get("roughness_label")
            if label is not None:
                label.setText(text)

    def _add_slot(self, mat, previous):
        group = QGroupBox(mat["name"])
        form = QFormLayout(group)
        fields = {}
        hosts = {}
        roughness_label = None
        previous = previous or {}
        for key, _label in ALL_CHANNELS:
            line = DropLine()
            line.file_dropped.connect(self._remember_texture_dir)
            line.textChanged.connect(self._save_textures)
            saved_path = previous.get(key) or ""
            if saved_path:
                line.setText(saved_path)
            elif mat.get(f"has_{key}"):
                line.setPlaceholderText("from FBX")
            browse = QPushButton("…")
            browse.setFixedWidth(32)
            browse.clicked.connect(lambda _checked=False, target=line: self._browse_image(target))
            row = QHBoxLayout()
            row.addWidget(line, 1)
            row.addWidget(browse)
            host = QWidget()
            host.setLayout(row)
            title = QLabel(self._channel_label(key))
            form.addRow(title, host)
            fields[key] = line
            hosts[key] = host
            if key == "roughness":
                roughness_label = title
        alpha = QCheckBox("Alpha from Base Color")
        alpha.setToolTip("Opacity comes from the Base Color alpha. Usually left off for the Character Pipeline.")
        if "alpha_from_basecolor" in previous:
            alpha.setChecked(bool(previous["alpha_from_basecolor"]))
        elif not self.character.isChecked():
            alpha.setChecked(bool(mat.get("has_alpha")))
        alpha.toggled.connect(self._save_textures)
        form.addRow("", alpha)
        self.slot_layout.addWidget(group)
        self._slots[mat["name"]] = {
            "fields": fields,
            "alpha": alpha,
            "has_alpha": bool(mat.get("has_alpha")),
            "roughness_label": roughness_label,
            "form": form,
            "hosts": hosts,
        }
        self._apply_pipeline()

    def _fbx_key(self, path):
        return str(Path(path).resolve())

    def _texture_store(self):
        raw = self._settings.value("texture_sets", "") or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _textures_for(self, path):
        saved = self._texture_store().get(self._fbx_key(path))
        return saved if isinstance(saved, dict) else {}

    def _save_textures(self, *_args):
        if self._loading_slots or not self._loaded_fbx or not self._slots:
            return
        store = self._texture_store()
        store[self._loaded_fbx] = self._collect_materials()
        self._settings.setValue("texture_sets", json.dumps(store, ensure_ascii=False))

    def _reset_textures(self):
        self._loading_slots = True
        try:
            for slot in self._slots.values():
                for line in slot["fields"].values():
                    line.clear()
                slot["alpha"].setChecked(False if self.character.isChecked() else bool(slot.get("has_alpha")))
        finally:
            self._loading_slots = False
        self._save_textures()

    def _browse_image(self, line):
        path, _ = QFileDialog.getOpenFileName(self, "Texture", self._texture_dir(), IMAGE_FILTER)
        if path:
            line.setText(path)
            self._remember_texture_dir(path)

    def _split_masks(self):
        paths = []
        seen = set()
        for slot in self._slots.values():
            line = slot["fields"].get("masks")
            if line is None:
                continue
            text = line.text().strip()
            if not text:
                continue
            key = str(Path(text)).lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(Path(text))
        if not paths:
            QMessageBox.information(self, "Screenshot Utility", "Set a Masks texture first.")
            return
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            QMessageBox.warning(self, "Screenshot Utility", "These Masks textures are missing:\n" + "\n".join(missing))
            return
        written = []
        try:
            for path in paths:
                written.extend(split_mask_file(path))
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            QMessageBox.warning(self, "Screenshot Utility", f"Could not split {path.name}:\n{exc}")
            return
        names = "\n".join(path.name for path in written)
        self.status.setText(f"Split {len(written)} textures")
        QMessageBox.information(self, "Screenshot Utility", "Wrote:\n" + names)

    def _collect_materials(self):
        collected = {}
        for name, slot in self._slots.items():
            entry = {key: line.text().strip() for key, line in slot["fields"].items()}
            entry["alpha_from_basecolor"] = slot["alpha"].isChecked()
            collected[name] = entry
        return collected

    def _font_size(self, default, tip):
        box = QSpinBox()
        box.setRange(8, 200)
        box.setValue(default)
        box.setSuffix(" px")
        box.setToolTip(tip + " This is the size next to a 512px frame. It scales with the frames.")
        return box

    def _sheet_fonts(self):
        return {
            "labels": self.font_labels.value(),
            "caption": self.font_caption.value(),
            "date": self.font_date.value(),
        }

    def _set_grid_enabled(self, enabled):
        self.grid_pass.setEnabled(enabled)
        self.grid_thickness.setEnabled(enabled)
        self.wire_color_btn.setEnabled(enabled)

    def _fit_number(self, spin):
        text = spin.textFromValue(spin.maximum())
        spin.setFixedWidth(spin.fontMetrics().horizontalAdvance(text) + 32)
        spin.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _toggle_cycles(self, enabled):
        self.raytracing.setEnabled(not enabled)

    def _toggle_emission(self, enabled):
        live = bool(enabled) and self.character.isChecked()
        self.emission_enabled.setEnabled(self.character.isChecked())
        self.emit_color_btn.setEnabled(live)
        self.emit_strength.setEnabled(live)
        self.bloom.setEnabled(live)

    def _swatch_style(self, color):
        ink = "#111" if color.lightness() > 140 else "#eee"
        # A squeezed button otherwise grows scrollbars beside the hex text.
        return (
            "QPushButton {"
            f"background: {color.name()}; color: {ink};"
            "border: 1px solid #5a5a5a; padding: 1px 4px;"
            "}"
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar:horizontal { height: 0px; }"
        )

    def _paint_wire_button(self):
        color = self._wire_color.name()
        self.wire_color_btn.setStyleSheet(self._swatch_style(self._wire_color))
        self.wire_color_btn.setText(color)

    def _pick_wire_color(self):
        chosen = QColorDialog.getColor(self._wire_color, self, "Wireframe color")
        if chosen.isValid():
            self._wire_color = chosen
            self._paint_wire_button()

    def _paint_emit_button(self):
        color = self._emit_color.name()
        self.emit_color_btn.setStyleSheet(self._swatch_style(self._emit_color))
        self.emit_color_btn.setText("")
        self.emit_color_btn.setToolTip(f"{color}. The mask's green channel is only the brightness.")

    def _pick_emit_color(self):
        chosen = QColorDialog.getColor(self._emit_color, self, "Emission color")
        if chosen.isValid():
            self._emit_color = chosen
            self._paint_emit_button()

    def _paint_bg_button(self):
        color = self._bg_color.name()
        self.bg_color_btn.setStyleSheet(self._swatch_style(self._bg_color))
        self.bg_color_btn.setText(color)

    def _pick_bg_color(self):
        chosen = QColorDialog.getColor(self._bg_color, self, "Background color")
        if chosen.isValid():
            self._bg_color = chosen
            self._paint_bg_button()

    def _grid_row(self):
        if not self.grid_enabled.isChecked():
            return None
        color = self._wire_color
        return {
            "pass": self.grid_pass.currentData(),
            "thickness": self.grid_thickness.value(),
            "color": [color.redF(), color.greenF(), color.blueF()],
        }

    def _selected_passes(self):
        keys = [key for key, box in self.pass_boxes.items() if self._pass_enabled(key) and box.isChecked()]
        if "material" in keys:
            keys.remove("material")
            keys.insert(0, "material")
        return keys

    def _step_degrees(self):
        frames = max(1, self.frames.value())
        if frames == 2:
            return 90.0
        return 360.0 / frames

    def render(self):
        fbx = self.fbx_edit.text().strip()
        output = self.output_edit.text().strip()
        if output and (self.date_in_name.isChecked() or self.number_if_exists.isChecked()):
            output = self._dated_output(output)
            self.output_edit.setText(output)
        passes = self._selected_passes()
        grid = self._grid_row()
        render_passes = list(passes)
        if grid and grid["pass"] not in render_passes:
            render_passes.append(grid["pass"])
        if not Path(fbx).is_file():
            QMessageBox.warning(self, "Screenshot Utility", "FBX not found")
            return
        if not render_passes:
            QMessageBox.warning(self, "Screenshot Utility", "Turn on at least one row")
            return
        if not output:
            QMessageBox.warning(self, "Screenshot Utility", "Choose where to save the PNG")
            return
        logo = self.logo_edit.text().strip() if self.logo_enabled.isChecked() else ""
        if logo and not Path(logo).is_file():
            QMessageBox.warning(self, "Screenshot Utility", "Logo file not found")
            return
        self._remember_output_dir(output)
        if not Path(self._blender_path()).is_file():
            QMessageBox.warning(self, "Screenshot Utility", "Can't find blender.exe. Set the Blender path at the bottom of the window.")
            return
        materials = self._collect_materials()
        missing = []
        for name, maps in materials.items():
            for key, label in self._active_channels():
                path = maps[key]
                if path and not Path(path).is_file():
                    missing.append(f"{name} / {label}")
        if missing:
            QMessageBox.warning(self, "Screenshot Utility", "These textures are missing:\n" + "\n".join(missing))
            return

        self.status.setText("Building the sheet…")
        self.progress.setRange(0, 0)
        self.log.clear()
        blender = self._blender_path()
        caption_name = self.caption_name.text()
        caption_task = self.caption_task.text()
        caption_project = self.caption_project.text()
        labels = self._pass_labels()
        sizes = self._sheet_sizes(materials)
        sheet_side = int(self.frame_size.currentData())
        show_triangles = self.triangles_enabled.isChecked()
        gap = self.gap.value()
        fonts = self._sheet_fonts()
        render_frame = frame_size_for_long_side(
            sheet_side,
            self.frames.value(),
            passes,
            grid,
            caption_name,
            caption_task,
            labels,
            gap,
            fonts,
            caption_project,
            0 if show_triangles else None,
            sizes,
        )
        job_body = {
            "fbx": fbx,
            "frames": self.frames.value(),
            "step_degrees": self._step_degrees(),
            "start_degrees": self.start.value(),
            "distance_scale": self.distance.value(),
            "height": self.height.value(),
            "exposure": self.exposure.value(),
            "frame_size": render_frame,
            "sheet_long_side": sheet_side,
            "front": self.front.currentData(),
            "invert_normal_g": self.invert_g.isChecked(),
            "raytracing": self.raytracing.isChecked(),
            "cycles": self.cycles.isChecked(),
            "character": self.character.isChecked(),
            "background": [self._bg_color.red(), self._bg_color.green(), self._bg_color.blue()],
            "emission": [self._emit_color.red(), self._emit_color.green(), self._emit_color.blue()],
            "emission_enabled": self.emission_enabled.isChecked(),
            "emission_strength": self.emit_strength.value(),
            "bloom": self.bloom.value(),
            "surface_model": self.surface_model.currentData(),
            "pass_labels": labels,
            "passes": render_passes,
            "wire": grid,
            "materials": materials,
        }

        def work(thread):
            temp = Path(tempfile.mkdtemp(prefix="screenshot-utility-"))
            job = dict(job_body)
            job["output_dir"] = str(temp)
            job_path = temp / "job.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                render_sheet(
                    blender,
                    job_path,
                    on_line=lambda line: self._watch_line(thread, line),
                    proc_slot=thread.proc_slot,
                )
                if thread.cancelled:
                    return None
                return str(build_sheet(
                    temp,
                    passes,
                    job["frames"],
                    output,
                    caption_name,
                    caption_task,
                    grid,
                    job.get("pass_labels"),
                    job.get("sheet_long_side"),
                    job.get("background"),
                    gap,
                    fonts,
                    caption_project,
                    logo,
                    self._triangle_count(temp) if show_triangles else None,
                    sizes,
                ))
            finally:
                shutil.rmtree(temp, ignore_errors=True)

        self._start(work, self._show_sheet)

    def _show_sheet(self, path):
        if not path:
            self.status.setText("Cancelled")
            return
        self._preview_path = path
        if self.separate_window.isChecked():
            if self._sheet_window is None:
                self._sheet_window = SheetWindow()
            self._sheet_window.show_sheet(path)
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Sheet is open in a separate window")
        else:
            self._update_preview()
        self.status.setText(path)

    def _toggle_separate_preview(self, separate):
        self.preview_scroll.setVisible(not separate)
        if separate:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("The sheet will open in a separate window")
        elif self._preview_path:
            self._update_preview()

    def _update_preview(self):
        if self.separate_window.isChecked():
            return
        if not self._preview_path or not Path(self._preview_path).is_file():
            return
        view = self.preview_scroll.viewport()
        pixmap, error = _pixmap_fitted(
            self._preview_path,
            max(1, view.width() - 8),
            max(1, view.height() - 8),
        )
        if pixmap.isNull():
            self.preview.setText(f"Saved, but the preview failed to open:\n{error}")
            return
        self.preview.setPixmap(pixmap)
        self.preview.setMinimumSize(0, 0)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._settings_halved:
            self._settings_halved = True
            QTimer.singleShot(0, self._halve_settings)

    def _halve_settings(self):
        sizes = self._splitter.sizes()
        total = sum(sizes)
        if total <= 0 or sizes[1] <= 0:
            self._settings_halved = False
            return
        right_floor = 420
        left = min(400, max(280, total - right_floor))
        self._splitter.setSizes([left, total - left])
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_preview()


def _set_windows_app_id():
    """Taskbar otherwise labels the window with the host exe: pythonw."""
    if sys.platform != "win32":
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScreenshotUtility.App")


def main():
    _set_windows_app_id()
    QImageReader.setAllocationLimit(0)
    app = QApplication([])
    app.setApplicationName("Screenshot Utility")
    app.setApplicationDisplayName("Screenshot Utility")
    icon = QIcon(str(resource_root() / "icons" / "app.png"))
    app.setWindowIcon(icon)
    apply_dark(app)
    window = MainWindow()
    window.show()
    app.exec()
