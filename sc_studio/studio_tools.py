"""Authoring/publishing dialogs, kept separate from the core editor widget."""

import copy
import json
from dataclasses import asdict
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap, QColor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QHBoxLayout,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QCheckBox,
    QLineEdit,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QSlider,
    QScrollArea,
    QMenu,
)
from .workspace import native_picker
from . import authoring, easing
from .publishing import PublishSettings, publish, repack_atlases
from .widgets import ColorSwatch


def swatch(form, label, value):
    """A colour row in an authoring dialog, matching the inspector's swatch."""
    field = ColorSwatch(value)
    form.addRow(label, field)
    return field


def dialog(parent, title):
    d = QDialog(parent)
    d.setWindowTitle(title)
    d.resize(480, 320)
    layout = QVBoxLayout(d)
    form = QFormLayout()
    layout.addLayout(form)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(d.accept)
    buttons.rejected.connect(d.reject)
    layout.addWidget(buttons)
    return d, form


def number(form, label, value, low=0, high=65535, decimals=None):
    field = QSpinBox() if decimals is None else QDoubleSpinBox()
    if decimals is not None:
        field.setDecimals(decimals)
    field.setRange(low, high)
    field.setValue(value)
    form.addRow(label, field)
    return field


def combo(form, label, values, current=None):
    field = QComboBox()
    field.addItems(list(values))
    if current is not None:
        field.setCurrentText(str(current))
    form.addRow(label, field)
    return field


def checkbox(form, label, value=False):
    field = QCheckBox()
    field.setChecked(value)
    form.addRow(label, field)
    return field


def selected_editor(window, kind=None):
    ed = window.current()
    if ed is None:
        return None
    if not ed.resolve_drafts():
        return None
    if kind and (
        ed.selected not in ed.doc.index or ed.doc.index[ed.selected][0] != kind
    ):
        QMessageBox.information(
            window, "SC Studio", f"Select a {kind.rstrip('s')} first."
        )
        return None
    return ed


def edit(window, kind, fn):
    ed = selected_editor(window, kind)
    if ed:
        ed.mutate(lambda: fn(ed), full_validate=True)


class PublishDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Publish SC")
        self.resize(560, 650)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        from PyQt6.QtWidgets import QWidget

        content = QWidget()
        self.form = QFormLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        try:
            self.settings = PublishSettings(
                **json.loads(window.settings.value("publish_settings", "{}"))
            )
        except (ValueError, TypeError):
            self.settings = PublishSettings()
        self.fields = {}
        self.path = QLineEdit(str(window.settings.value("publish_output", "")))
        row = QHBoxLayout()
        row.addWidget(self.path)
        button = QPushButton("Browse…")
        row.addWidget(button)
        button.clicked.connect(self.pick_output)
        self.form.addRow("Output SC file", row)
        self.sources = QListWidget()
        for i in range(window.editors.count()):
            ed = window.editors.widget(i)
            item = QListWidgetItem(
                ed.doc.path.name if ed.doc.path else f"Untitled {i + 1}"
            )
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if ed is window.current()
                else Qt.CheckState.Unchecked
            )
            self.sources.addItem(item)
        self.sources.setMaximumHeight(90)
        self.form.addRow("Source documents", self.sources)
        self.selection = checkbox(self.form, "Publish selected resources only")
        self.merge = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.merge)
        b = QPushButton("Browse…")
        row.addWidget(b)
        b.clicked.connect(self.pick_merge)
        self.form.addRow("Merge into existing SC", row)
        self.fields["version"] = combo(
            self.form, "SC2 version", ("5", "6"), self.settings.version
        )
        self.fields["encoding"] = combo(
            self.form,
            "Texture encoding",
            ("RGBA8", "RGBA4", "RGB565", "KTX"),
            self.settings.encoding,
        )
        for key, label in [
            ("linked_only", "Linked symbols only"),
            ("repack", "Repack atlas regions"),
            ("lowres", "Generate low-resolution textures"),
            ("external", "External KTX files"),
            ("compress_external", "Compress external KTX"),
            ("write_text", "Write text contents"),
            ("low_precision", "Quantize matrices when in range"),
            ("short_frames", "Short unlabeled frames (v5)"),
            ("replace_exports", "Replace matching destination exports"),
        ]:
            self.fields[key] = checkbox(self.form, label, getattr(self.settings, key))
        for key, label, lo, hi in [
            ("max_width", "Maximum atlas width", 16, 8192),
            ("max_height", "Maximum atlas height", 16, 8192),
            ("padding", "Atlas padding", 0, 16),
            ("compression_level", "Zstandard compression level", 1, 22),
        ]:
            self.fields[key] = number(
                self.form, label, getattr(self.settings, key), lo, hi
            )
        self.fields["texture_scale"] = number(
            self.form, "Texture scale", self.settings.texture_scale, 0.01, 1, 2
        )
        tip = QLabel(
            "Publishing creates a separate output. Geometry stays at its original size.\nKTX here is uncompressed RGBA8. SC1, SCTX and ASTC encoding are not available."
        )
        tip.setWordWrap(True)
        outer.addWidget(tip)
        row = QHBoxLayout()
        load = QPushButton("Load preset…")
        save = QPushButton("Save preset…")
        load.clicked.connect(self.load_preset)
        save.clicked.connect(self.save_preset)
        row.addWidget(load)
        row.addWidget(save)
        outer.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def values(self):
        values = {}
        for key, field in self.fields.items():
            values[key] = (
                field.isChecked()
                if isinstance(field, QCheckBox)
                else field.currentText()
                if isinstance(field, QComboBox)
                else field.value()
            )
        values["version"] = int(values["version"])
        settings = PublishSettings(**values)
        settings.validate()
        return settings

    def pick_output(self):
        path, _ = native_picker(
            self, "Publish output", self.path.text(), "SC files (*.sc)", save=True
        )
        if path:
            self.path.setText(path)

    def pick_merge(self):
        path, _ = native_picker(self, "Destination SC", "", "SC files (*.sc)")
        if path:
            self.merge.setText(path)

    def load_preset(self):
        path, _ = native_picker(self, "Load publish preset", "", "JSON (*.json)")
        if not path:
            return
        try:
            settings = PublishSettings.load(path)
            for key, field in self.fields.items():
                v = getattr(settings, key)
                if isinstance(field, QCheckBox):
                    field.setChecked(v)
                elif isinstance(field, QComboBox):
                    field.setCurrentText(str(v))
                else:
                    field.setValue(v)
        except Exception as e:
            QMessageBox.critical(self, "Preset", str(e))

    def save_preset(self):
        path, _ = native_picker(
            self, "Save publish preset", "", "JSON (*.json)", save=True
        )
        if path:
            try:
                self.values().save(path)
            except Exception as e:
                QMessageBox.critical(self, "Preset", str(e))

    def confirm(self):
        try:
            settings = self.values()
            if not self.path.text().strip():
                raise ValueError("Choose an output filename")
            editors = [
                self.window.editors.widget(
                    self.sources.item(i).data(Qt.ItemDataRole.UserRole)
                )
                for i in range(self.sources.count())
                if self.sources.item(i).checkState() == Qt.CheckState.Checked
            ]
            if not editors:
                raise ValueError("Choose at least one source document")
            if any(e.drafts for e in editors):
                raise ValueError("Apply source inspector drafts before publishing")
            target = Path(self.path.text()).expanduser().resolve()
            if any(e.doc.path and target == e.doc.path.resolve() for e in editors):
                raise ValueError(
                    "Choose a separate output path to preserve the open source documents"
                )
            if (
                target.exists()
                and QMessageBox.question(
                    self, "Replace output?", f"Replace {target.name}?"
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            roots = None
            if self.selection.isChecked():
                if len(editors) != 1:
                    raise ValueError(
                        "Selected-resource publishing requires one source document"
                    )
                roots = editors[0].selected_ids()
                if not roots:
                    raise ValueError("Select resources to publish")
            self.result_args = (
                [copy.deepcopy(e.doc) for e in editors],
                target,
                settings,
                roots,
                self.merge.text().strip(),
            )
            self.window.settings.setValue(
                "publish_settings", json.dumps(asdict(settings))
            )
            self.window.settings.setValue("publish_output", str(target))
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Publish", str(e))


def publish_dialog(window):
    if not selected_editor(window):
        return
    d = PublishDialog(window)
    if d.exec() != QDialog.DialogCode.Accepted:
        return
    sources, path, settings, roots, merge = d.result_args
    from .document import Document

    def work():
        return publish(
            sources, path, settings, roots, Document.load(merge) if merge else None
        )

    window.background(
        "Publishing SC…",
        work,
        lambda report: QMessageBox.information(
            window,
            "Published",
            f"{report['resources']} resources, {report['exports']} exports\n{report['bytes']:,} bytes\n{report['path']}",
        ),
    )


def import_svg(window):
    ed = selected_editor(window)
    if not ed:
        return
    path, _ = native_picker(window, "Import vector artwork", "", "SVG (*.svg)")
    if not path:
        return
    d, f = dialog(window, "Rasterize vector artwork")
    size = number(f, "Raster width (0 = SVG size)", 0, 0, 8192)
    height = number(f, "Raster height (0 = SVG size)", 0, 0, 8192)
    if d.exec() != QDialog.DialogCode.Accepted:
        return
    result = []
    if ed.mutate(
        lambda: result.append(
            authoring.svg_shape(
                ed.doc,
                Path(path).read_bytes(),
                size.value() or None,
                height.value() or None,
            )
        ),
        full_validate=True,
    ):
        ed.go_to(result[0])


def primitive(window):
    ed = selected_editor(window)
    if not ed:
        return
    d, f = dialog(window, "Create artwork")
    kind = combo(
        f, "Shape", ("Rectangle", "Rounded rectangle", "Ellipse", "Polygon / path")
    )
    width = number(f, "Width", 200, 1, 8192)
    height = number(f, "Height", 100, 1, 8192)
    fill = combo(f, "Fill", ("Solid", "Linear gradient", "Radial gradient", "None"))
    c1 = QLineEdit("#5599ff")
    c2 = QLineEdit("#112244")
    f.addRow("Fill color / first stop", c1)
    f.addRow("Last gradient stop", c2)
    stroke = QLineEdit("#ffffff")
    f.addRow("Stroke color", stroke)
    sw = number(f, "Stroke width", 0, 0, 100, 2)
    radius = number(f, "Corner radius", 12, 0, 4096)
    path = QPlainTextEdit("M 10 90 L 100 10 L 190 90 Z")
    path.setMaximumHeight(80)
    f.addRow("SVG path (path mode)", path)
    if d.exec() != QDialog.DialogCode.Accepted:
        return

    def create():
        from xml.sax.saxutils import escape

        colors = [QColor(c.text()) for c in (c1, c2, stroke)]
        if any(not c.isValid() for c in colors):
            raise ValueError("Use valid color names or hexadecimal colors")
        w, h = width.value(), height.value()
        p = sw.value() / 2
        tag = (
            "ellipse"
            if kind.currentText() == "Ellipse"
            else "path"
            if kind.currentText() == "Polygon / path"
            else "rect"
        )
        attrs = (
            f'cx="{w / 2}" cy="{h / 2}" rx="{w / 2 - p}" ry="{h / 2 - p}"'
            if tag == "ellipse"
            else f'd="{escape(path.toPlainText(), {chr(34): "&quot;"})}"'
            if tag == "path"
            else f'x="{p}" y="{p}" width="{max(0, w - 2 * p)}" height="{max(0, h - 2 * p)}" rx="{radius.value() if kind.currentText() == "Rounded rectangle" else 0}"'
        )
        gradient = "linearGradient" if fill.currentIndex() == 1 else "radialGradient"
        defs = f'<defs><{gradient} id="g"><stop stop-color="{colors[0].name()}"/><stop offset="1" stop-color="{colors[1].name()}"/></{gradient}></defs>'
        color = (
            colors[0].name()
            if fill.currentIndex() == 0
            else "none"
            if fill.currentIndex() == 3
            else "url(#g)"
        )
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{defs}<{tag} {attrs} fill="{color}" stroke="{colors[2].name()}" stroke-width="{sw.value()}"/></svg>'
        return authoring.svg_shape(ed.doc, svg.encode())

    result = []
    if ed.mutate(lambda: result.append(create()), full_validate=True):
        ed.go_to(result[0])


def parenting(window):
    ed = selected_editor(window, "movieclips")
    if not ed:
        return
    clip = ed.doc.resource(ed.selected)
    d, f = dialog(window, "Bake layer parenting")
    f.addRow(
        QLabel(
            'Transforms are local before baking and become world transforms.\nUse child indices, e.g. {"1": 0, "2": 1}. This operation is undoable.'
        )
    )
    text = QPlainTextEdit("{}")
    f.addRow("Child → parent mapping", text)
    f.addRow(
        QLabel(
            "\n".join(
                f"{i}: {c['name'] or c['id']}" for i, c in enumerate(clip["children"])
            )
        )
    )
    if d.exec() == QDialog.DialogCode.Accepted:
        ed.mutate(
            lambda: authoring.bake_parenting(
                ed.doc, ed.selected, json.loads(text.toPlainText())
            ),
            full_validate=True,
        )


def masks(window):
    ed = selected_editor(window, "movieclips")
    if not ed:
        return
    clip = ed.doc.resource(ed.selected)
    if len(clip["children"]) < 2:
        return
    d, f = dialog(window, "Create stencil mask range")
    labels = [f"{i}: {c['name'] or c['id']}" for i, c in enumerate(clip["children"])]
    mask = combo(f, "Mask track", labels)
    content = QLineEdit("1")
    f.addRow("Content child indices (comma separated)", content)
    f.addRow(
        QLabel(
            "Content must be contiguous in draw order. The mask must be\npresent in every masked frame. Mask states are written to SC."
        )
    )
    if d.exec() == QDialog.DialogCode.Accepted:
        ed.mutate(
            lambda: authoring.mask_range(
                ed.doc,
                ed.selected,
                mask.currentIndex(),
                [int(v.strip()) for v in content.text().split(",")],
            ),
            full_validate=True,
        )


def morph(window):
    ed = selected_editor(window)
    if not ed:
        return
    shapes = [i for i, (k, _) in ed.doc.index.items() if k == "shapes"]
    if len(shapes) < 2:
        QMessageBox.information(
            window,
            "Shape animation",
            "Create two shapes with matching topology and textures first.",
        )
        return
    d, f = dialog(window, "Bake shape animation")
    labels = [f"{i}: {', '.join(ed.doc.names.get(i, []))}" for i in shapes]
    start = combo(f, "Start shape", labels)
    end = combo(f, "End shape", labels)
    end.setCurrentIndex(1)
    count = number(f, "Frames", 30, 2, 65535)
    fps = number(f, "Framerate", 30, 1, 255)
    curve = combo(f, "Curve", easing.PRESETS)
    result = []
    if d.exec() == QDialog.DialogCode.Accepted and ed.mutate(
        lambda: result.append(
            authoring.morph_shapes(
                ed.doc,
                shapes[start.currentIndex()],
                shapes[end.currentIndex()],
                count.value(),
                curve.currentText(),
                fps.value(),
            )
        ),
        full_validate=True,
    ):
        ed.go_to(result[0])


def color_tween(window):
    ed = selected_editor(window, "movieclips")
    if not ed:
        return
    clip = ed.doc.resource(ed.selected)
    if len(clip["frames"]) < 2 or not clip["children"]:
        return
    d, f = dialog(window, "Tween color effects")
    child = combo(
        f,
        "Track",
        [f"{i}: {c['name'] or c['id']}" for i, c in enumerate(clip["children"])],
    )
    start = number(f, "Start frame", 0, 0, len(clip["frames"]) - 1)
    end = number(f, "End frame", len(clip["frames"]) - 1, 0, len(clip["frames"]) - 1)
    curve = combo(f, "Curve", easing.PRESETS)
    if d.exec() == QDialog.DialogCode.Accepted:
        ed.mutate(
            lambda: authoring.tween_colors(
                ed.doc,
                ed.selected,
                child.currentIndex(),
                start.value(),
                end.value(),
                curve.currentText(),
            ),
            full_validate=True,
        )


def text_style(window):
    ed = selected_editor(window, "textfields")
    if not ed:
        return
    obj = ed.doc.resource(ed.selected)
    d, f = dialog(window, "Text style")
    font = QLineEdit(obj["font_name"])
    f.addRow("Font family", font)
    size = number(f, "Font size", obj["font_size"], 1, 255)
    text = QPlainTextEdit(obj["text"])
    f.addRow("Text", text)
    h = combo(f, "Horizontal alignment", ("Left", "Center", "Right", "Justify"))
    h.setCurrentIndex(obj["align"] & 3)
    v = combo(
        f, "Vertical alignment", ("Top", "Center", "Bottom", "Bottom (alternate)")
    )
    v.setCurrentIndex((obj["align"] >> 2) & 3)
    colors = []
    for key, label in [
        ("font_color", "Text color"),
        ("outline_color", "Outline color"),
    ]:
        colors.append((key, swatch(f, label, obj[key])))
    flags = [
        (bit, checkbox(f, label, bool(obj["styles"] & bit)))
        for bit, label in [
            (1, "Device font"),
            (2, "Outline"),
            (4, "Bold"),
            (8, "Italic"),
            (16, "Multiline"),
            (32, "Selectable flag"),
            (64, "Auto kerning"),
        ]
    ]
    if d.exec() != QDialog.DialogCode.Accepted:
        return

    def apply():
        if len(text.toPlainText()) > 255:
            raise ValueError(
                "Text exceeds the plugin-compatible limit of 255 characters"
            )
        obj.update(
            font_name=font.text(),
            font_size=size.value(),
            text=text.toPlainText(),
            align=(obj["align"] & 0xF0) | h.currentIndex() | (v.currentIndex() << 2),
            styles=(obj["styles"] & 128)
            | sum(bit for bit, field in flags if field.isChecked()),
        )
        for key, field in colors:
            obj[key] = field.value()
        ed.doc.update("textfields", obj)

    ed.mutate(apply)


def custom_curve(window):
    d, f = dialog(window, "Custom easing curve")
    name = QLineEdit("Custom")
    f.addRow("Name", name)
    fields = [
        number(
            f,
            label,
            value,
            0 if label[0] == "X" else -5,
            1 if label[0] == "X" else 5,
            3,
        )
        for label, value in [("X1", 0.25), ("Y1", 0.1), ("X2", 0.25), ("Y2", 1)]
    ]
    if d.exec() != QDialog.DialogCode.Accepted:
        return
    key = name.text().strip()
    if not key:
        return
    points = tuple(v.value() for v in fields)
    easing.PRESETS[key] = points
    saved = json.loads(window.settings.value("custom_curves", "{}"))
    saved[key] = points
    window.settings.setValue("custom_curves", json.dumps(saved))
    for i in range(window.editors.count()):
        ed = window.editors.widget(i)
        # Refresh the existing curve selectors, preserving their current choice.
        for field in ed.findChildren(QComboBox):
            if (
                field.count()
                and field.itemText(0) == "Linear"
                and field.findText(key) < 0
            ):
                field.addItem(key)


def rendered_preview(window):
    ed = selected_editor(window)
    if not ed or ed.selected not in ed.doc.index:
        return
    from .rendering import Renderer, export_animation, pil_image
    from PyQt6.QtGui import QPainter

    renderer = Renderer(copy.deepcopy(ed.doc))
    rid = ed.selected
    obj = renderer.resource(rid)
    count = len(obj["frames"]) if ed.doc.index[rid][0] == "movieclips" else 1
    d = QDialog(window)
    d.setWindowTitle("Rendered effects preview")
    d.resize(760, 600)
    layout = QVBoxLayout(d)
    label = QLabel()
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    scroll = QScrollArea()
    scroll.setWidget(label)
    scroll.setWidgetResizable(True)
    layout.addWidget(scroll)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, count - 1)
    layout.addWidget(slider)
    onion = QCheckBox("Onion skin")
    layout.addWidget(onion)
    play = QPushButton("Play / pause")
    layout.addWidget(play)
    timer = QTimer(d)
    timer.setInterval(max(1, round(1000 / obj.get("framerate", 30))))
    timer.timeout.connect(lambda: slider.setValue((slider.value() + 1) % count))
    play.clicked.connect(lambda: timer.stop() if timer.isActive() else timer.start())
    d.finished.connect(timer.stop)
    info = QLabel()
    layout.addWidget(info)
    row = QHBoxLayout()
    png = QPushButton("Export current PNG…")
    animation = QPushButton("Export GIF / WebP…")
    row.addWidget(png)
    row.addWidget(animation)
    layout.addLayout(row)
    error = QLabel()
    error.setWordWrap(True)
    layout.addWidget(error)
    bounds = renderer.bounds(rid, slider.value())
    # Stable canvas across the timeline, including outside-the-first-frame motion.
    for fi in range(count):
        bounds = bounds.united(renderer.bounds(rid, fi))

    def draw(*_):
        try:
            im, _ = renderer.render(rid, slider.value(), bounds)
            if onion.isChecked():
                from .rendering import composite

                background = renderer.blank()
                for fi in (slider.value() - 1, slider.value() + 1):
                    if 0 <= fi < count:
                        ghost, _ = renderer.render(rid, fi, bounds)
                        p = QPainter(background)
                        p.setOpacity(0.2)
                        p.drawImage(0, 0, ghost)
                        p.end()
                composite(background, im)
                im = background
            label.setPixmap(QPixmap.fromImage(im))
            info.setText(
                f"Frame {slider.value() + 1} / {count} · {im.width()} × {im.height()}"
            )
            error.clear()
        except Exception as e:
            error.setText(str(e))

    def export_current():
        path, _ = native_picker(d, "Export current frame", "", "PNG (*.png)", save=True)
        if path:
            try:
                pil_image(renderer.render(rid, slider.value(), bounds)[0]).save(path)
            except Exception as e:
                error.setText(str(e))

    def export_all():
        path, _ = native_picker(
            d, "Export animation", "", "WebP (*.webp);;GIF (*.gif)", save=True
        )
        if path:
            try:
                export_animation(renderer.doc, rid, path)
            except Exception as e:
                error.setText(str(e))

    slider.valueChanged.connect(draw)
    onion.toggled.connect(draw)
    png.clicked.connect(export_current)
    animation.clicked.connect(export_all)
    draw()
    d.exec()


def command_palette(window):
    d = QDialog(window)
    d.setWindowTitle("Command palette")
    d.resize(560, 440)
    layout = QVBoxLayout(d)
    query = QLineEdit()
    query.setPlaceholderText("Search commands…")
    layout.addWidget(query)
    items = QListWidget()
    layout.addWidget(items)
    entries = [
        a for a in window.actions.values() if not a.isSeparator() and a.isEnabled()
    ]

    def filter_items(text):
        items.clear()
        for action in entries:
            title = action.text().replace("&", "")
            if text.lower() in title.lower():
                item = QListWidgetItem(
                    title
                    + (
                        "  " + action.shortcut().toString()
                        if not action.shortcut().isEmpty()
                        else ""
                    )
                )
                item.setData(Qt.ItemDataRole.UserRole, action)
                items.addItem(item)
        if items.count():
            items.setCurrentRow(0)

    def execute(*_):
        item = items.currentItem()
        if item:
            d.accept()
            item.data(Qt.ItemDataRole.UserRole).trigger()

    query.textChanged.connect(filter_items)
    query.returnPressed.connect(execute)
    items.itemActivated.connect(execute)
    filter_items("")
    query.setFocus()
    d.exec()


def batch_exports(window):
    ed = selected_editor(window)
    if not ed:
        return
    d, f = dialog(window, "Batch rename export names")
    find = QLineEdit()
    replace = QLineEdit()
    f.addRow("Find literal text", find)
    f.addRow("Replace with", replace)
    selected = checkbox(f, "Selected resources only", True)
    if d.exec() != QDialog.DialogCode.Accepted or not find.text():
        return

    def apply():
        ids = ed.selected_ids() if selected.isChecked() else list(ed.doc.index)
        updates = {
            i: [n.replace(find.text(), replace.text()) for n in ed.doc.names.get(i, [])]
            for i in ids
        }
        allnames = [
            n for i in ed.doc.index for n in updates.get(i, ed.doc.names.get(i, []))
        ]
        if any(not n for n in allnames) or len(allnames) != len(set(allnames)):
            raise ValueError("Renaming would produce empty or duplicate export names")
        for i in updates:
            ed.doc.set_exports(i, [])
        ed.doc._reindex()
        for i, names in updates.items():
            ed.doc.set_exports(i, names)
            ed.doc._reindex()

    ed.mutate(apply, full_validate=True)


def nine_slice(window):
    ed = selected_editor(window, "shapes")
    if not ed:
        return
    shape = ed.doc.resource(ed.selected)
    vs = [v for c in shape["commands"] for v in c["vertices"]]
    if not vs:
        return
    x0, x1 = min(v[0] for v in vs), max(v[0] for v in vs)
    y0, y1 = min(v[1] for v in vs), max(v[1] for v in vs)
    d, f = dialog(window, "Create nine-slice symbol")
    values = [
        number(f, label, value, -32768, 32767, 2)
        for label, value in [
            ("Left", x0 + (x1 - x0) / 3),
            ("Top", y0 + (y1 - y0) / 3),
            ("Right", x0 + 2 * (x1 - x0) / 3),
            ("Bottom", y0 + 2 * (y1 - y0) / 3),
        ]
    ]
    result = []
    if d.exec() == QDialog.DialogCode.Accepted and ed.mutate(
        lambda: result.append(
            authoring.nine_slice(ed.doc, ed.selected, *[v.value() for v in values])
        ),
        full_validate=True,
    ):
        ed.go_to(result[0])


def group_tracks(window):
    ed = selected_editor(window, "movieclips")
    if not ed:
        return
    d, f = dialog(window, "Group tracks into animated symbol")
    indices = QLineEdit("0,1")
    name = QLineEdit("Group")
    f.addRow("Child indices (comma separated)", indices)
    f.addRow("Group name", name)
    if d.exec() == QDialog.DialogCode.Accepted:
        ed.mutate(
            lambda: authoring.group_tracks(
                ed.doc,
                ed.selected,
                [int(v.strip()) for v in indices.text().split(",")],
                name.text(),
            ),
            full_validate=True,
        )


def repack_current(window):
    ed = selected_editor(window)
    if not ed:
        return
    d, f = dialog(window, "Repack current document atlases")
    width = number(f, "Maximum width", 2048, 16, 8192)
    height = number(f, "Maximum height", 2048, 16, 8192)
    padding = number(f, "Padding", 2, 0, 16)
    f.addRow(
        QLabel(
            "This decodes atlases into RGBA8 and rewrites UVs. Undo restores\nthe original encodings and coordinates. Publishing can repack a copy."
        )
    )
    if d.exec() == QDialog.DialogCode.Accepted:
        ed.mutate(
            lambda: repack_atlases(
                ed.doc,
                PublishSettings(
                    max_width=width.value(),
                    max_height=height.value(),
                    padding=padding.value(),
                ),
            ),
            full_validate=True,
        )


def motion_path(window):
    ed = selected_editor(window, "movieclips")
    if not ed:
        return
    clip = ed.doc.resource(ed.selected)
    if len(clip["frames"]) < 2 or not clip["children"]:
        return
    d, f = dialog(window, "Bake motion guide")
    child = combo(
        f,
        "Track",
        [f"{i}: {c['name'] or c['id']}" for i, c in enumerate(clip["children"])],
    )
    start = number(f, "Start frame", 0, 0, len(clip["frames"]) - 1)
    end = number(f, "End frame", len(clip["frames"]) - 1, 0, len(clip["frames"]) - 1)
    points = QPlainTextEdit("[[0,0], [100,0], [100,100]]")
    f.addRow("XY guide points (JSON)", points)
    orient = checkbox(f, "Orient to guide")
    curve = combo(f, "Curve", easing.PRESETS)
    if d.exec() == QDialog.DialogCode.Accepted:
        ed.mutate(
            lambda: authoring.motion_path(
                ed.doc,
                ed.selected,
                child.currentIndex(),
                start.value(),
                end.value(),
                json.loads(points.toPlainText()),
                orient.isChecked(),
                curve.currentText(),
            ),
            full_validate=True,
        )


def install_tools(window, file_menu, create_menu, resource_menu, view_menu, help_menu):
    try:
        for name, points in json.loads(
            window.settings.value("custom_curves", "{}")
        ).items():
            if (
                len(points) == 4
                and all(isinstance(v, (int, float)) for v in points)
                and 0 <= points[0] <= 1
                and 0 <= points[2] <= 1
            ):
                easing.PRESETS[name] = tuple(points)
    except (ValueError, TypeError):
        pass
    window.action(
        file_menu, "publish", "Publish…", lambda: publish_dialog(window), "Ctrl+Alt+P"
    )
    window.action(create_menu, "svg", "Import SVG artwork…", lambda: import_svg(window))
    window.action(
        create_menu,
        "primitive",
        "Rectangle / ellipse / gradient / path…",
        lambda: primitive(window),
    )
    author = QMenu("&Author", window)
    anchor = next(
        (a for a in window.menuBar().actions() if a.text() == "&MCP"),
        help_menu.menuAction(),
    )
    window.menuBar().insertMenu(anchor, author)
    for key, label, fn in [
        ("motion_path", "Bake motion guide…", motion_path),
        ("group_tracks", "Group tracks…", group_tracks),
        ("nine_slice", "Create nine-slice symbol…", nine_slice),
        ("repack_atlases", "Repack current atlases…", repack_current),
        ("parenting", "Bake layer parenting…", parenting),
        ("mask_range", "Create stencil mask range…", masks),
        ("morph", "Bake shape animation…", morph),
        ("color_tween", "Tween color effects…", color_tween),
        ("text_style", "Text style…", text_style),
        ("custom_curve", "Custom easing curve…", custom_curve),
        ("batch_exports", "Batch rename exports…", batch_exports),
    ]:
        window.action(author, key, label, lambda fun=fn: fun(window))
    window.action(
        view_menu,
        "rendered_preview",
        "Rendered effects / onion skin / export…",
        lambda: rendered_preview(window),
        "Ctrl+Alt+R",
    )
    window.action(
        help_menu,
        "palette",
        "Command palette…",
        lambda: command_palette(window),
        "Ctrl+Shift+P",
    )
