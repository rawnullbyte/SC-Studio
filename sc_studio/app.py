"""PyQt6 SC Studio desktop editor."""

import sys
import json
import math
import copy
from collections import deque
import pickle
import time
from pathlib import Path
from PyQt6.QtCore import (
    Qt,
    QTimer,
    pyqtSignal,
    QObject,
    QEvent,
    QRunnable,
    QThreadPool,
    QSettings,
    QSignalBlocker,
)
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QImage,
    QPalette,
    QFontDatabase,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QTabWidget,
    QCheckBox,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QLineEdit,
    QComboBox,
    QTreeWidget,
    QTreeWidgetItem,
    QPushButton,
    QLabel,
    QFileDialog,
    QMessageBox,
    QPlainTextEdit,
    QFormLayout,
    QScrollArea,
    QSpinBox,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QSlider,
    QDialog,
    QDialogButtonBox,
    QProgressDialog,
    QInputDialog,
    QColorDialog,
    QMenu,
    QHeaderView,
    QAbstractSpinBox,
    QTextEdit,
    QStyle,
    QSizePolicy,
    QToolButton,
)
from .document import Document, KINDS, MKEYS, CHUNKS, FrameSequence, FrameElements
from .workspace import (
    CLIP_MIME,
    configure_theme,
    native_picker,
    clipboard_pack,
    clipboard_unpack,
    set_graph_clipboard,
)
from .preview import AnimatorTimeline, Preview
from . import textures
from . import easing
from . import themes as theme_schemes


def save_picker(parent, title, initial="", filters=""):
    return native_picker(parent, title, initial, filters, save=True)


class ResourceItem(QTreeWidgetItem):
    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column == 1:
            try:
                return int(self.text(column)) < int(other.text(column))
            except ValueError:
                pass
        return self.text(column).casefold() < other.text(column).casefold()


class Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn())
        except Exception as e:
            self.signals.failed.emit(str(e))


def jsonable(obj):
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, FrameSequence, FrameElements)):
        return [jsonable(v) for v in obj]
    return obj


def fill_table(table, rows):
    table.blockSignals(True)
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            table.setItem(i, j, QTableWidgetItem(str(value)))
    if (
        table.horizontalHeaderItem(0)
        and table.horizontalHeaderItem(0).text() == "Frame"
    ):
        for i in range(table.rowCount()):
            for j in (0, 2):
                table.item(i, j).setFlags(
                    table.item(i, j).flags() & ~Qt.ItemFlag.ItemIsEditable
                )
    if (
        table.horizontalHeaderItem(0)
        and table.horizontalHeaderItem(0).text() == "Texture atlas"
    ):
        for i in range(table.rowCount()):
            table.item(i, 2).setFlags(
                table.item(i, 2).flags() & ~Qt.ItemFlag.ItemIsEditable
            )
    table.blockSignals(False)
    table.resizeColumnsToContents()


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    return widget


class Editor(QWidget):
    changed = pyqtSignal()

    def __init__(self, doc):
        super().__init__()
        self.doc = doc
        self.selected = None
        self.texture_index = None
        self.history = []
        self.future = []
        self.loading = False
        self.property_widgets = {}
        self.drafts = set()
        self.revision = self.saved_revision = self.sequence = 0
        # Rolling cost of the last edits and frames, shown by Help > Performance.
        # A perf_counter call costs tens of nanoseconds, so this is always on and
        # the menu needs no restart or environment variable to be useful.
        self.timings = {
            name: deque(maxlen=60)
            for name in ("snapshot", "validate", "commit", "render")
        }
        self.saved_original = dict(doc.original)
        # Frames the last bake wrote for a placement, so reverting removes only
        # those and never a keyframe the author set by hand.
        self.baked = None
        self.active_panel = 0
        self.geometry_row = -1
        self.child_visibility = {}
        self.navigation_history = []
        self.navigation_index = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        split = QSplitter()
        self.splitter = split
        layout.addWidget(split)
        left = QWidget()
        left.setObjectName("assetBrowser")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search resources…  Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        self.search.setToolTip(
            "Search export name, resource ID, or child instance name"
        )
        asset_label = QLabel("ASSETS")
        asset_label.setObjectName("sectionTitle")
        lv.addWidget(asset_label)
        lv.addWidget(self.search)
        self.category = QComboBox()
        self.resource_kinds = [
            "movieclips",
            "shapes",
            "textfields",
            "modifiers",
            "textures",
            "exports",
        ]
        self.category.addItems(
            [
                "Movieclips",
                "Shapes",
                "Textfields",
                "Modifiers",
                "Texture atlases",
                "Exports",
            ]
        )
        self.category.setToolTip("The asset list shows only the selected category")
        lv.addWidget(self.category)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Resource", "ID"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.viewport().installEventFilter(self)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.browser_count = QLabel()
        self.browser_count.setObjectName("browserCount")
        lv.addWidget(self.tree)
        lv.addWidget(self.browser_count)
        row = QHBoxLayout()
        self.add = QPushButton("New resource")
        self.delete_btn = QPushButton("Delete")
        row.addWidget(self.add)
        row.addWidget(self.delete_btn)
        lv.addLayout(row)
        split.addWidget(left)
        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb = QLabel("No resource selected")
        self.breadcrumb.setObjectName("previewHeading")
        self.breadcrumb.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.breadcrumb.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        cv.addWidget(self.breadcrumb)
        self.preview = Preview()
        self.preview.placementSelected.connect(self.select_placement)
        self.preview.placementMoved.connect(self.move_placement)
        self.preview.placementTransformed.connect(self.commit_placement_transform)
        self.preview.placementContext.connect(self.viewer_context_menu)
        self.preview.atlasContext.connect(self.atlas_context_menu)
        self.preview.atlasShapeOpened.connect(self.open_atlas_shape)
        cv.addWidget(self.preview)
        controls = QHBoxLayout()
        self.play = QPushButton("▶ Play")
        # The label swaps between Play and Pause, and those two glyphs measure
        # differently, so both dimensions are pinned to the larger of the two.
        # Otherwise the button changes size under the cursor on every toggle.
        self.play.setMinimumWidth(96)
        # Measure the taller of the two labels through the widget itself, then
        # pin the height: a minimum would not stop the Pause glyph from growing
        # the button, and the button growing moves the whole transport row.
        self.play.setText("⏸ Pause")
        self.play.setFixedHeight(self.play.sizeHint().height())
        self.play.setText("▶ Play")
        self.frame = QSlider(Qt.Orientation.Horizontal)
        # Frames the cursor may reach past the end of the clip.
        self.FRAME_HEADROOM = 8
        self.frame.setRange(0, 0)
        self.frame_label = QLabel("Frame 1 / 1")
        fit = QPushButton("Fit")
        controls.addWidget(self.play)
        controls.addWidget(self.frame, 1)
        controls.addWidget(self.frame_label)
        controls.addWidget(fit)
        # The transport lives in its own fixed-height panel so the row below it
        # cannot shift the slider or the buttons while a clip plays.
        transport = QWidget()
        transport.setLayout(controls)
        transport.setFixedHeight(transport.sizeHint().height())
        cv.addWidget(transport)
        # The animator sits under the canvas, so building an animation never
        # means opening a tab: a labeled track per child, a ruler, a playhead.
        animator = QWidget()
        animator_layout = QVBoxLayout(animator)
        animator_layout.setContentsMargins(0, 2, 0, 2)
        animator_layout.setSpacing(3)
        self.keyframes = AnimatorTimeline()
        animator_layout.addWidget(self.keyframes)
        controls_row = QHBoxLayout()
        self.curve = QComboBox()
        self.curve.addItems(easing.PRESETS)
        self.curve.setMinimumWidth(118)
        self.curve.setToolTip("Shape applied to the frames you apply it to")
        controls_row.addWidget(QLabel("Curve"))
        controls_row.addWidget(self.curve)
        self.curve_preview = QLabel()
        self.curve_preview.setFixedSize(74, 30)
        controls_row.addWidget(self.curve_preview)
        controls_row.addWidget(QLabel("Keys"))
        self.key_density = QSpinBox()
        self.key_density.setRange(1, 60)
        self.key_density.setValue(6)
        self.key_density.setToolTip("Frames to write between each pair of keys")
        controls_row.addWidget(self.key_density)
        self.bake_button = QPushButton("Apply to range")
        self.bake_button.clicked.connect(self.apply_curve_to_range)
        controls_row.addWidget(self.bake_button)
        self.undo_curve_button = QPushButton("Undo curve")
        self.undo_curve_button.clicked.connect(self.revert_tween)
        controls_row.addWidget(self.undo_curve_button)
        controls_row.addStretch()
        self.remove_frame_button = QPushButton("Remove frame")
        self.remove_frame_button.clicked.connect(self.remove_frame)
        controls_row.addWidget(self.remove_frame_button)
        animator_layout.addLayout(controls_row)
        transform_row = QHBoxLayout()
        self.key_fields = {}
        for name in ("X", "Y", "Rotation", "Scale X", "Scale Y"):
            transform_row.addWidget(QLabel(name))
            box = QDoubleSpinBox()
            box.setDecimals(2)
            box.setRange(-3600.0, 3600.0)
            box.setValue(0.0 if name in ("X", "Y", "Rotation") else 1.0)
            box.setMinimumWidth(70)
            transform_row.addWidget(box)
            self.key_fields[name] = box
        self.apply_animation_button = QPushButton("Key transform")
        self.apply_animation_button.setToolTip(
            "Write these values as a keyframe on the current frame"
        )
        self.apply_animation_button.clicked.connect(self.apply_animation_fields)
        transform_row.addWidget(self.apply_animation_button)
        reload_button = QPushButton("Reload")
        reload_button.setToolTip("Read the values back from the current frame")
        reload_button.clicked.connect(self.load_animation_fields)
        transform_row.addWidget(reload_button)
        self.animation_hint = QLabel("")
        transform_row.addWidget(self.animation_hint, 1)
        self.curve_info = QLabel("")
        controls_row.addWidget(self.curve_info, 1)
        animator_layout.addLayout(transform_row)
        cv.addWidget(animator)
        self.keyframes.framePicked.connect(self.goto_frame)
        self.keyframes.trackPicked.connect(self.select_track)
        self.keyframes.keyToggled.connect(self.toggle_keypoint_at)
        self.keyframes.moveRequested.connect(self.move_keypoint_on_track)
        self.keyframes.extendRequested.connect(self.extend_to_frame)
        self.keyframes.selectionChanged.connect(self.keyframe_selection_changed)
        self.keyframes.keyContext.connect(self.keypoint_context_menu)
        self.keyframes.trackContext.connect(self.keyframe_context_menu)
        self.curve.currentTextChanged.connect(self.draw_curve)
        self.notice = QLabel("Select a resource to preview and edit.")
        self.notice.setWordWrap(True)
        self.notice.setObjectName("previewNotice")
        cv.addWidget(self.notice)
        split.addWidget(center)
        inspector = QWidget()
        iv = QVBoxLayout(inspector)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(4)
        self.panel_picker = QComboBox()
        self.panel_picker.addItems(
            ["Properties", "Timeline", "JSON", "Textures", "Geometry", "Children"]
        )
        self.panel_picker.setToolTip("Inspector panel · Ctrl+1 through Ctrl+6")
        inspector_label = QLabel("INSPECTOR")
        inspector_label.setObjectName("sectionTitle")
        iv.addWidget(inspector_label)
        # Children is one of the panel_picker entries, so it needs no separate
        # tab bar; the picker stays visible on every panel, including Children.
        iv.addWidget(self.panel_picker)
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        iv.addWidget(self.tabs)
        split.addWidget(inspector)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_host)
        props = QWidget()
        pv = QVBoxLayout(props)
        pv.addWidget(scroll)
        apply = QPushButton("Apply properties")
        pv.addWidget(apply)
        self.tabs.addTab(props, "Properties")
        timeline = QWidget()
        tv = QVBoxLayout(timeline)
        self.timeline_pages = QTabWidget()
        tv.addWidget(self.timeline_pages)
        instances_page = QWidget()
        instances_layout = QVBoxLayout(instances_page)
        frames_page = QWidget()
        frames_layout = QVBoxLayout(frames_page)
        self.timeline_pages.addTab(frames_page, "Frames")
        placements_page = QWidget()
        placements_layout = QVBoxLayout(placements_page)
        self.timeline_pages.addTab(placements_page, "Placements")
        self.children = table(["Resource ID", "Instance name", "Blend", "Visible"])
        instances_layout.addWidget(QLabel("Resources used by this movieclip"))
        instances_layout.addWidget(self.children)
        cr = QHBoxLayout()
        for title, fn in [
            ("Add child", self.add_child),
            ("Delete", self.remove_child),
            ("Duplicate", self.duplicate_child),
        ]:
            b = QPushButton(title)
            b.clicked.connect(fn)
            cr.addWidget(b)
        instances_layout.addLayout(cr)
        extra = QHBoxLayout()
        for n, (title, fn) in enumerate(
            [
                ("Rename", self.rename_child),
                ("Open child", self.open_child),
                ("Show all", self.show_all_children),
                ("Send back", lambda: self.reorder_child(False)),
                ("Bring front", lambda: self.reorder_child(True)),
            ]
        ):
            button = QPushButton(title)
            button.clicked.connect(fn)
            if n == 3:
                instances_layout.addLayout(extra)
                extra = QHBoxLayout()
            extra.addWidget(button)
        instances_layout.addLayout(extra)
        self.frames = table(["Frame", "Label", "Placements"])
        frames_layout.addWidget(QLabel("Choose a frame to preview and edit"))
        frames_layout.addWidget(self.frames)
        fr = QHBoxLayout()
        for title, fn in [
            ("New frame", lambda: self.add_frame(False)),
            ("Duplicate", lambda: self.add_frame(True)),
            ("Delete frame", self.remove_frame),
        ]:
            b = QPushButton(title)
            b.clicked.connect(fn)
            fr.addWidget(b)
        frames_layout.addLayout(fr)
        self.elements = table(["Child index", "Matrix index", "Color index"])
        placements_layout.addWidget(
            QLabel("Current frame placements · 65535 = identity")
        )
        placements_layout.addWidget(self.elements)
        er = QHBoxLayout()
        for title, fn in [
            ("Add placement", self.add_element),
            ("Remove", self.remove_element),
            ("Transform / color", self.edit_transform),
        ]:
            b = QPushButton(title)
            b.clicked.connect(fn)
            er.addWidget(b)
        self.tabs.addTab(timeline, "Timeline")
        advanced = QWidget()
        av = QVBoxLayout(advanced)
        av.addWidget(
            QLabel(
                "All resource fields. Frame elements: [child index, matrix index, color index].\nShape vertices: [x, y, u16, v16]. Apply changes before switching."
            )
        )
        self.code = QPlainTextEdit()
        self.code.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.code.viewport().installEventFilter(self)
        self.code.setTabStopDistance(self.code.fontMetrics().horizontalAdvance(" ") * 4)
        av.addWidget(self.code)
        apply_json = QPushButton("Apply resource JSON")
        av.addWidget(apply_json)
        self.tabs.addTab(advanced, "Resource JSON")
        texture = QWidget()
        texv = QVBoxLayout(texture)
        self.texture_info = QLabel("Select a texture atlas in the browser.")
        self.texture_info.setWordWrap(True)
        texv.addWidget(self.texture_info)
        self.atlas_outlines = QCheckBox("Show referenced asset outlines")
        self.atlas_outlines.setChecked(True)
        self.atlas_outlines.toggled.connect(self.toggle_atlas_outlines)
        texv.addWidget(self.atlas_outlines)
        self.atlas_shapes_button = QPushButton("Browse atlas shapes…")
        self.atlas_shapes_button.clicked.connect(self.browse_atlas_shapes)
        texv.addWidget(self.atlas_shapes_button)
        for title, fn in [
            ("Export PNG", self.export_texture),
            ("Replace highres from image…", self.replace_texture),
            ("Add texture from image…", self.add_texture),
            ("Import encoded KTX…", self.import_ktx),
        ]:
            b = QPushButton(title)
            b.clicked.connect(fn)
            texv.addWidget(b)
        texv.addStretch()
        self.tabs.addTab(texture, "Textures")
        shape = QWidget()
        sv = QVBoxLayout(shape)
        sv.addWidget(
            QLabel(
                "Bitmap commands and triangle-strip vertices. UV coordinates use 0–65535."
            )
        )
        self.commands = table(["Texture atlas", "Flags", "Vertex count"])
        self.vertices = table(["X", "Y", "U16", "V16"])
        sv.addWidget(self.commands)
        sr = QHBoxLayout()
        for title, fn in [
            ("Add command", self.add_command),
            ("Delete command", self.remove_command),
            ("Create from PNG", self.shape_from_png),
        ]:
            button = QPushButton(title)
            button.clicked.connect(fn)
            sr.addWidget(button)
        sv.addLayout(sr)
        sv.addWidget(self.vertices)
        vr = QHBoxLayout()
        for title, fn in [
            ("Add vertex", self.add_vertex),
            ("Delete vertex", self.remove_vertex),
            ("Apply geometry", self.apply_geometry),
        ]:
            button = QPushButton(title)
            button.clicked.connect(fn)
            vr.addWidget(button)
        sv.addLayout(vr)
        self.tabs.addTab(shape, "Geometry")
        self.tabs.addTab(instances_page, "Children")
        self.children.itemSelectionChanged.connect(self.children_selection_changed)
        self.commands.itemSelectionChanged.connect(self.show_vertices)
        split.setSizes([265, 565, 350])
        self.tabs.setMinimumWidth(290)
        left.setMinimumWidth(210)
        for panel_layout in (pv, tv, av, texv, sv):
            panel_layout.setContentsMargins(4, 4, 4, 4)
            panel_layout.setSpacing(4)
        self.active_panel = 0
        self.panel_picker.currentIndexChanged.connect(self.change_panel)
        self.tabs.currentChanged.connect(self.sync_panel)
        for grid in (
            self.children,
            self.frames,
            self.elements,
            self.commands,
            self.vertices,
        ):
            grid.verticalHeader().setDefaultSectionSize(23)
            grid.verticalHeader().setMinimumSectionSize(20)
            grid.setAlternatingRowColors(True)
            grid.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            grid.setTabKeyNavigation(True)
            grid.viewport().installEventFilter(self)
        self.code.textChanged.connect(lambda: self.mark_draft("json"))
        self.commands.cellChanged.connect(lambda *_: self.mark_draft("geometry"))
        self.vertices.cellChanged.connect(lambda *_: self.mark_draft("geometry"))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(160)
        self.search_timer.timeout.connect(self.populate)
        self.search.returnPressed.connect(self.first_match)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        self.category.currentIndexChanged.connect(self.category_changed)
        self.tree.itemSelectionChanged.connect(self.select)
        self.add.clicked.connect(self.new_resource)
        self.delete_btn.clicked.connect(lambda: self.window().delete_selection())
        apply.clicked.connect(self.apply_properties)
        apply_json.clicked.connect(self.apply_json)
        self.frame.valueChanged.connect(self.render_frame)
        self.play.clicked.connect(self.toggle_play)
        fit.clicked.connect(lambda: self.render_frame(fit=True))
        self.children.cellChanged.connect(self.child_changed)
        self.frames.cellChanged.connect(self.timeline_changed)
        self.frames.itemSelectionChanged.connect(self.select_frame)
        self.elements.cellChanged.connect(self.timeline_changed)
        # Picking a different placement in the timeline changes which rows the
        # keyframe strip shows keys for, so the strip follows the selection.
        self.elements.itemSelectionChanged.connect(self.refresh_keyframe_strip)
        self.populate()

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.Wheel
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            area = watched.parentWidget()
            if hasattr(area, "horizontalScrollBar"):
                pixels, angle = event.pixelDelta(), event.angleDelta()
                delta = pixels.x() or pixels.y() or (angle.x() or angle.y()) / 120 * 40
                bar = area.horizontalScrollBar()
                bar.setValue(bar.value() - round(delta))
                event.accept()
                return True
        if (
            watched is self.search
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.search.clear()
            self.search_timer.stop()
            self.populate()
            self.tree.setFocus()
            return True
        return super().eventFilter(watched, event)

    def first_match(self):
        self.search_timer.stop()
        self.populate()
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            if not group.isHidden():
                self.tree.setCurrentItem(group)
                self.tree.setFocus()
                return

    def error(self, message):
        QMessageBox.warning(self, "SC Studio", str(message))

    def mark_draft(self, area):
        if not self.loading and self.selected in self.doc.index:
            self.drafts.add(area)
            self.changed.emit()

    def update_code(self, obj):
        self.pending_json = obj
        if self.active_panel != 2:
            return
        with QSignalBlocker(self.code):
            self.code.setPlainText(
                json.dumps(jsonable(obj), indent=2, ensure_ascii=False)
            )
        self.code.document().setModified(False)

    # Undo history keeps rollback state for whole documents, and a large file
    # makes that expensive, so the sweep happens per edit rather than per frame.
    UNDO_CHECK_INTERVAL = 60

    def snapshot(self):
        state = dict(self.doc.__dict__)
        state["bank_cache"] = {}  # Reconstruct caches lazily, not in history.
        # index and names are pure projections of tables and the string pool;
        # pickle is several times faster than deepcopy and one reindex on
        # restore is far cheaper than deep-copying them every time.
        # index and names are rebuilt on restore; original holds the untouched chunk
        # blobs for lossless save and is never mutated after load, so restoring
        # only needs the live copy back rather than a 137 MB re-pickle per edit.
        for derived in ("index", "names", "original"):
            state.pop(derived, None)
        return (
            pickle.dumps(state, pickle.HIGHEST_PROTOCOL),
            # Only the atlas table, so commit_mutation can tell whether the
            # textures changed. Texture payloads are immutable bytes, which
            # deepcopy shares and == compares by identity, so this is close to
            # free even though the table is large.
            copy.deepcopy(self.doc.tables["Textures"]),
            self.revision,
            self.selected,
            self.frame.value(),
        )

    def restored_state(self, blob):
        state = pickle.loads(blob)
        state["original"] = self.doc.original
        return state

    def is_dirty(self):
        return bool(
            self.drafts or self.revision != self.saved_revision or self.doc.dirty
        )

    def mark_saved(self):
        self.saved_revision = self.revision
        self.saved_original = dict(self.doc.original)
        self.doc.dirty.clear()
        self.changed.emit()

    def restore(self, entry):
        path = self.doc.path
        blob, textures, revision, selected, frame = entry
        state = self.restored_state(blob)
        keep_atlases = self.doc.tables["Textures"] == textures
        self.doc.__dict__ = state
        self.doc.path = path
        self.doc._reindex()
        self.doc.original = dict(self.saved_original)
        self.revision = revision
        self.doc.dirty = set(CHUNKS) if revision != self.saved_revision else set()
        self.selected = selected if selected in self.doc.index else None
        self.drafts.clear()
        self.preview.invalidate(keep_atlases=keep_atlases)
        self.populate()
        self.refresh()
        self.frame.setValue(min(frame, self.frame.maximum()))
        self.changed.emit()

    def mutate(self, fn, raise_errors=False, rebuild_browser=True,
               affects=None, full_validate=False):
        """Apply an edit with rollback, then validate what it could have broken.

        ``affects`` names the resources the edit touches when they differ from
        the selection, and ``full_validate`` forces the whole-document sweep for
        edits whose reach cannot be listed in advance (a delete can invalidate
        references held by any clip).
        """
        mark = time.perf_counter()
        before = self.snapshot()
        snapped = time.perf_counter()
        try:
            touched = None if full_validate else (
                self.touched_ids() if affects is None else set(affects)
            )
            fn()
            # Structural checks on just the touched resources, with a full sweep on
            # save, when the caller asks for one, and periodically so an edit
            # that reaches past its selection is still caught. Escalating only
            # when the narrow check already failed would miss exactly those.
            if touched is None or self.validate_full():
                errors = self.doc.validate()
            else:
                errors = self.doc.validate(only=touched)
            if errors:
                raise ValueError("\n".join(errors[:20]))
        except Exception as e:
            self.record_timing("snapshot", snapped - mark)
            self.doc.__dict__ = self.restored_state(before[0])
            self.doc._reindex()
            if raise_errors:
                raise
            self.error(e)
            return False
        validated = time.perf_counter()
        self.commit_mutation(before, rebuild_browser=rebuild_browser)
        self.record_timing("snapshot", snapped - mark)
        self.record_timing("validate", validated - snapped)
        self.record_timing("commit", time.perf_counter() - validated)
        return True

    def record_timing(self, name, seconds):
        self.timings[name].append(seconds)

    def touched_ids(self):
        """Resources a pending mutation is about to change.

        Selection is the only signal the editor has without wrapping every call
        site, and it matches selection-driven editing, but the undo history
        keeps every fifth snapshot at full validation so an edit that reachers
        further still gets caught.
        """
        return {self.selected} if self.selected in self.doc.index else None

    def validate_full(self):
        # sequence is incremented by commit_mutation, so the edit about to run
        # lands on the interval when the current sequence is one short of it.
        return self.sequence % self.UNDO_CHECK_INTERVAL == self.UNDO_CHECK_INTERVAL - 1

    def commit_mutation(self, before, rebuild_browser=True):
        self.history.append(before)
        self.history = self.history[-5:]
        self.sequence += 1
        self.revision = self.sequence
        self.drafts.clear()
        self.future.clear()
        self.preview.invalidate(
            keep_atlases=self.doc.tables["Textures"] == before[1]
        )
        self.changed.emit()
        # Rebuilding every browser row costs about a second on a large file, so
        # an edit that cannot change the listing relabels its one row instead.
        if rebuild_browser:
            self.populate()
        else:
            self.refresh_browser_row()
        return True

    def refresh_browser_row(self):
        """Relabel the selected resource's row after an in-place edit."""
        if self.selected not in self.doc.index:
            return
        names = self.doc.names.get(self.selected, [])
        kind = self.doc.index[self.selected][0]
        label = " / ".join(names) if names else f"{kind[:-1].title()} {self.selected}"
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == ("resource", self.selected):
                if item.text(0) != label:
                    item.setText(0, label)
                return

    def resolve_drafts(self):
        if not self.drafts:
            return True
        answer = QMessageBox.question(
            self,
            "Unapplied edits",
            "Apply changes before continuing?",
            QMessageBox.StandardButton.Apply
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Apply,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Discard:
            self.drafts.clear()
            self.refresh()
            self.changed.emit()
            return True
        area = next(iter(self.drafts))
        if area == "properties":
            self.apply_properties()
        elif area == "json":
            self.apply_json()
        elif area == "geometry":
            self.apply_geometry()
        return not self.drafts

    def change_panel(self, index):
        if index == self.active_panel:
            return
        if not self.resolve_drafts():
            with QSignalBlocker(self.panel_picker):
                self.panel_picker.setCurrentIndex(self.active_panel)
            return
        if not self.tabs.isTabEnabled(index):
            with QSignalBlocker(self.panel_picker):
                self.panel_picker.setCurrentIndex(self.active_panel)
            return
        if index == 3 and self.texture_index is None and self.doc.tables["Textures"]["textures"]:
            self.view_selection_atlas()
            return
        self.active_panel = index
        self.tabs.setCurrentIndex(index)
        if index == 2 and hasattr(self, "pending_json"):
            self.update_code(self.pending_json)

    def sync_panel(self, index):
        self.active_panel = index
        with QSignalBlocker(self.panel_picker):
            self.panel_picker.setCurrentIndex(index)

    def set_panel(self, index):
        self.splitter.widget(2).show()
        self.panel_picker.setCurrentIndex(index)

    def undo(self):
        if not self.resolve_drafts() or not self.history:
            return
        self.future.append(self.snapshot())
        self.restore(self.history.pop())

    def redo(self):
        if not self.resolve_drafts() or not self.future:
            return
        self.history.append(self.snapshot())
        self.restore(self.future.pop())

    def selected_ids(self):
        values = [
            item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()
        ]
        ids = [data[1] for data in values if data and data[0] == "resource"]
        return ids or ([self.selected] if self.selected in self.doc.index else [])

    def context_menu(self, position):
        owner = self.window()
        menu = QMenu(self)
        for title, fn in [
            ("Rename…", owner.rename_selection),
            ("Copy resources", owner.copy),
            ("Paste resources", owner.paste),
            ("Duplicate", owner.duplicate),
            ("Find usages…", owner.references),
            ("View texture atlas…", self.view_shape_atlas),
            ("Delete", owner.delete_selection),
        ]:
            action = menu.addAction(title)
            action.triggered.connect(lambda checked=False, callback=fn: callback())
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def go_to(self, id, remember=True):
        if id not in self.doc.index:
            return False
        if not self.resolve_drafts():
            return False
        if remember and self.selected in self.doc.index and self.selected != id:
            self.navigation_history = self.navigation_history[
                : self.navigation_index + 1
            ]
            self.navigation_history.append(self.selected)
            self.navigation_index += 1
        self.timer.stop()
        self.play.setText("▶ Play")
        self.selected = id
        self.texture_index = None
        with QSignalBlocker(self.tree):
            self.tree.clearSelection()
        with QSignalBlocker(self.search):
            self.search.clear()
        self.search_timer.stop()
        with QSignalBlocker(self.category):
            self.category.setCurrentIndex(
                self.resource_kinds.index(self.doc.index[id][0])
            )
        self.populate()
        self.refresh()
        item = self.tree.currentItem()
        if item:
            self.tree.scrollToItem(item)
        return True

    def category_changed(self):
        self.populate()
        if self.resource_kinds[self.category.currentIndex()] == "textures" and self.tree.topLevelItemCount():
            items = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            item = next((item for item in items if item.data(0, Qt.ItemDataRole.UserRole)
                         == ("texture", self.texture_index)), items[0])
            self.tree.setCurrentItem(item)

    def populate(self):
        query = self.search.text().casefold()
        filter = self.resource_kinds[self.category.currentIndex()]
        selected = self.selected
        previous_ids = set(self.selected_ids())
        self.tree.blockSignals(True)
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for id, (kind, raw) in self.doc.index.items():
            if filter not in (kind, "exports"):
                continue
            names = self.doc.names.get(id, [])
            if filter == "exports" and not names:
                continue
            label = " / ".join(names) if names else f"{kind[:-1].title()} {id}"
            if query and query not in label.casefold() and query not in str(id):
                if kind != "movieclips" or not any(
                    query in self.doc.string(ref).casefold()
                    for ref in raw.get("children_name_ref_ids", [])
                ):
                    continue
            item = ResourceItem(self.tree, [label, str(id)])
            item.setData(0, Qt.ItemDataRole.UserRole, ("resource", id))
            item.setToolTip(0, f"{kind[:-1]} #{id}\n{label}")
            if selected == id:
                self.tree.setCurrentItem(item)
            if id in previous_ids:
                item.setSelected(True)
        if filter == "textures":
            values = self.doc.tables["Textures"]["textures"]
            for i, t in enumerate(values):
                d = t["highres"]
                label = f'Atlas {i} · {d["width"]} × {d["height"]}'
                if query and query not in label.casefold():
                    continue
                item = ResourceItem(self.tree, [label, str(i)])
                item.setData(0, Qt.ItemDataRole.UserRole, ("texture", i))
        shown = self.tree.topLevelItemCount()
        self.tree.setSortingEnabled(True)
        self.tree.setUpdatesEnabled(True)
        self.tree.blockSignals(False)
        self.browser_count.setText(
            f"{shown:,} shown · {len(self.doc.index):,} resources"
        )

    def select(self):
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data:
            return
        if data[0] == "resource" and data[1] == self.selected:
            return
        if not self.resolve_drafts():
            self.populate()
            return
        self.timer.stop()
        self.play.setText("▶ Play")
        if data[0] == "texture":
            self.selected = None
            self.texture_index = data[1]
            self.show_texture()
            return
        if self.selected in self.doc.index:
            self.navigation_history = self.navigation_history[
                : self.navigation_index + 1
            ]
            self.navigation_history.append(self.selected)
            self.navigation_index += 1
        self.selected = data[1]
        self.texture_index = None
        self.refresh()

    def refresh(self):
        if self.selected not in self.doc.index:
            self.preview.scene().clear()
            self.code.clear()
            self.property_widgets = {}
            self.breadcrumb.setText("No resource selected")
            while self.form.rowCount():
                self.form.removeRow(0)
            for grid in (
                self.children,
                self.frames,
                self.elements,
                self.commands,
                self.vertices,
            ):
                fill_table(grid, [])
            return
        old_frame = (
            self.frame.value()
            if getattr(self, "view_resource", None) == self.selected
            else 0
        )
        self.view_resource = self.selected
        self.loading = True
        try:
            self.preview.doc = self.doc
            obj = self.preview.resource(self.selected)
            kind = self.doc.index[self.selected][0]
            self.obj = obj
            self.update_code(obj)
            label = (
                " / ".join(obj.get("exports", []))
                or f"{kind[:-1].title()} {self.selected}"
            )
            self.breadcrumb.setText(f"{label}  /  {kind[:-1]} #{self.selected}")
            self.breadcrumb.setToolTip(self.breadcrumb.text())
            while self.form.rowCount():
                self.form.removeRow(0)
            self.property_widgets = {}
            for key, value in obj.items():
                if (
                    isinstance(value, (dict, list, FrameSequence, FrameElements))
                    or value is None
                ):
                    continue
                if isinstance(value, int):
                    widget = (
                        QLineEdit(str(value)) if abs(value) > 2147483647 else QSpinBox()
                    )
                    if isinstance(widget, QSpinBox):
                        widget.setRange(-2147483647, 2147483647)
                        widget.setValue(value)
                elif isinstance(value, float):
                    widget = QDoubleSpinBox()
                    widget.setRange(-1e9, 1e9)
                    widget.setDecimals(6)
                    widget.setValue(value)
                else:
                    widget = QLineEdit(str(value))
                widget.setEnabled(key not in ("id", "frames_count"))
                self.form.addRow(key.replace("_", " ").title(), widget)
                self.property_widgets[key] = (widget, type(value))
                signal = (
                    widget.valueChanged
                    if isinstance(widget, (QSpinBox, QDoubleSpinBox))
                    else widget.textEdited
                )
                signal.connect(lambda *_: self.mark_draft("properties"))
            export = QLineEdit(", ".join(obj.get("exports", [])))
            self.form.addRow("Export names", export)
            self.property_widgets["exports"] = (export, list)
            export.textEdited.connect(lambda *_: self.mark_draft("properties"))
            self.tabs.setTabEnabled(1, kind == "movieclips")
            self.tabs.setTabEnabled(5, kind == "movieclips")
            self.panel_picker.model().item(5).setEnabled(kind == "movieclips")
            self.tabs.setTabEnabled(4, kind == "shapes")
            self.panel_picker.model().item(1).setEnabled(kind == "movieclips")
            self.panel_picker.model().item(4).setEnabled(kind == "shapes")
            if self.active_panel == 3 or not self.tabs.isTabEnabled(self.active_panel):
                self.tabs.setCurrentIndex(0)
            if kind == "shapes":
                fill_table(
                    self.commands,
                    [
                        [c["texture_index"], c.get("unk1", 0), len(c["vertices"])]
                        for c in obj["commands"]
                    ],
                )
                self.commands.selectRow(0)
                self.show_vertices()
            else:
                fill_table(self.commands, [])
                fill_table(self.vertices, [])
            count = len(obj["frames"]) if kind == "movieclips" else 1
            self.play.setEnabled(kind == "movieclips" and count > 1)
            self.frame.setEnabled(kind == "movieclips" and count > 1)
            # The cursor may sit a little past the last frame: changing
            # something there is what extends the clip, and scrubbing must be
            # able to reach it without creating anything on its own.
            self.frame.setRange(
                0, max(0, count - 1) + (self.FRAME_HEADROOM if count else 0)
            )
            self.frame.setValue(min(old_frame, self.frame.maximum()))
            if kind == "movieclips":
                fill_table(
                    self.children,
                    [[c["id"], c["name"], c["blend"]] for c in obj["children"]],
                )
                self.fill_visibility()
                fill_table(
                    self.frames,
                    [
                        [i + 1, f["label"], len(f["elements"])]
                        for i, f in enumerate(obj["frames"])
                    ],
                )
                self.frames.selectRow(min(old_frame, max(0, len(obj["frames"]) - 1)))
                self.show_elements()
                self.refresh_keyframe_strip()
                self.load_animation_fields()
            else:
                fill_table(self.children, [])
                fill_table(self.frames, [])
                fill_table(self.elements, [])
                self.refresh_keyframe_strip()
                self.load_animation_fields()
        except Exception as e:
            self.error(e)
        finally:
            self.loading = False
        self.render_frame(
            value=self.frame.value(),
            fit=getattr(self, "preview_resource", None) != self.selected,
        )
        self.preview_resource = self.selected

    def apply_properties(self):
        if self.selected is None:
            return
        try:
            obj = self.doc.resource(self.selected)
            for key, (w, t) in self.property_widgets.items():
                v = w.value() if isinstance(w, (QSpinBox, QDoubleSpinBox)) else w.text()
                obj[key] = (
                    [n.strip() for n in v.split(",") if n.strip()]
                    if t == list
                    else int(v, 0) if t is int and isinstance(v, str) else t(v)
                )
            kind = self.doc.index[self.selected][0]
            if self.mutate(lambda: self.doc.update(kind, obj)):
                self.refresh()
        except Exception as e:
            self.error(e)

    def apply_json(self):
        if self.selected is None:
            return
        try:
            obj = json.loads(self.code.toPlainText())
            if obj["id"] != self.selected:
                raise ValueError(
                    "IDs cannot be changed directly; duplicate/import to assign a new ID"
                )
            kind = self.doc.index[self.selected][0]
            if self.mutate(lambda: self.doc.update(kind, obj)):
                self.refresh()
        except Exception as e:
            self.error(e)

    def render_frame(self, value=None, fit=False):
        if self.texture_index is not None:
            if fit:
                self.preview.fit_content()
            return
        if self.loading or self.selected not in self.doc.index:
            return
        if (
            self.doc.index[self.selected][0] == "movieclips"
            and self.frames.currentRow() != self.frame.value()
        ):
            with QSignalBlocker(self.frames):
                self.frames.selectRow(self.frame.value())
            if not self.timer.isActive():
                self.show_elements()
        count = self.frame.maximum() + 1
        self.frame_label.setText(f"Frame {self.frame.value()+1} / {count}")
        self.preview.hidden_children = self.child_visibility.get(self.selected, set())
        started = time.perf_counter()
        message = self.preview.show_resource(
            self.doc, self.selected, self.frame.value(), fit=fit or value is None
        )
        self.record_timing("render", time.perf_counter() - started)
        self.notice.setText(
            message
            or "Click / drag a placement to move · Drag empty space to pan · Wheel to zoom"
        )
        self.refresh_keyframe_strip()
        # The transform fields belong to the current frame, so they follow it.
        self.load_animation_fields()

    def tick(self):
        # One frame per tick, at the clip's own rate. Deriving the frame from
        # wall-clock time made a slow frame (a heavy clip, or a large document)
        # drop the animation forward by dozens of frames, so the rate is now set
        # by the timer and rendering latency can only slow playback, not skip it.
        last = max(0, len(self.obj.get("frames") or []) - 1)
        # Playback loops over the clip itself, never into the headroom.
        self.frame.setValue((self.frame.value() + 1) % (last + 1))

    def toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play.setText("▶ Play")
            self.show_elements()
        elif self.selected in self.doc.index:
            fps = max(1, self.doc.index[self.selected][1].get("framerate", 30))
            self.timer.start(max(16, int(1000 / fps)))
            self.play.setText("⏸ Pause")

    def frame_index(self):
        return max(0, self.frames.currentRow())

    def select_frame(self):
        if self.loading:
            return
        self.show_elements()
        self.frame.setValue(self.frame_index())

    # --- keypoint animation -------------------------------------------------
    #
    # The format stores a discrete transform per frame with nowhere to record a
    # curve, so a keypoint is simply "this frame holds a transform for this
    # placement". Baking fills the frames between keys from a chosen curve; the
    # game then reads ordinary frames. Nothing here is stored as a curve.

    def placement_keys(self, clip):
        """Frame indices where the placement's transform differs from identity.

        Identity means the placement inherits the child's own transform, which
        is the neutral state and not something a key needs to assert.
        """
        row = self.placement_row()
        if row < 0:
            return []
        keys = []
        for index, frame in enumerate(clip["frames"]):
            # A placement can be absent from some frames; skip those rather than
            # abandoning the scan, so the rest of the timeline is still reported.
            if row >= len(frame["elements"]):
                continue
            element = frame["elements"][row]
            if element[1] != 65535 or element[2] != 65535:
                keys.append(index)
        return keys

    def placement_row(self):
        """The placement row the timeline is acting on.

        currentRow() is -1 whenever the table has no current cell, which happens
        when it was rebuilt rather than clicked, so fall back to the first row.
        """
        row = self.elements.currentRow()
        if row < 0 and self.elements.rowCount():
            return 0
        return row

    # --- keyframe strip -----------------------------------------------------

    def refresh_keyframe_strip(self):
        """Push the timeline and every child's keys to the animator."""
        if not (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
        ):
            self.keyframes.set_state(0, 0, [], -1)
            return
        clip = self.obj
        tracks = []
        for row, child in enumerate(clip.get("children") or []):
            name = child.get("name") or ""
            if not name:
                kind = self.doc.index.get(child["id"], ("resource", None))[0]
                name = f"{kind} {child['id']}" if kind != "resource" else str(child["id"])
            tracks.append(
                {"row": row, "label": name, "keys": self.keys_on_row(clip, row)}
            )
        self.keyframes.set_state(
            len(clip["frames"]), self.frame.value(), tracks, self.placement_row()
        )

    def keys_on_row(self, clip, row):
        """Frames where the given child row's transform actually changes.

        A frame holding no transform of its own inherits the last one set, so it
        is never a key. Only a frame that carries a transform different from the
        one already in force starts a new key, which is why a still hold shows a
        single key instead of one per frame.
        """
        bank = self.clip_bank(clip)
        matrices = bank.get("matrices") or []
        colours = bank.get("colors") or []
        keys = []
        current = None
        for index, frame in enumerate(clip["frames"]):
            if row >= len(frame["elements"]):
                continue
            element = frame["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                # Nothing set here, so this frame inherits and changes nothing.
                continue
            matrix = (
                list(matrices[element[1]])
                if element[1] != 65535 and element[1] < len(matrices)
                else None
            )
            colour = (
                list(colours[element[2]])
                if element[2] != 65535 and element[2] < len(colours)
                else None
            )
            state = (matrix, colour)
            if state != current:
                keys.append(index)
                current = state
        if not keys and clip["frames"]:
            # The first frame is the state everything else inherits from, so it
            # is shown as the base key even when nothing assigns one.
            keys.append(0)
        return keys

    def select_track(self, row):
        """Select a child's track, keeping the preview and panels in step."""
        if row < 0 or row >= self.elements.rowCount():
            return
        self.elements.selectRow(row)
        self.preview.selected_placement = row
        self.preview.viewport().update()
        self.load_animation_fields()

    def move_keypoint_on_track(self, source, target, row):
        """Move a key on a specific track, not only the selected one."""
        if self.elements.currentRow() != row:
            self.select_track(row)
        self.move_keypoint(source, target)

    def extend_to_frame(self, needed):
        """Add frames until the clip is long enough for the frame asked for.

        Scrubbing or dragging a key past the end grows the movieclip, so a
        timeline extends by working on it rather than by pre-adding frames.
        """
        if not self.animatable():
            return
        clip = self.doc.resource(self.selected)
        missing = needed + 1 - len(clip["frames"])
        if missing <= 0:
            return
        template = clip["frames"][-1] if clip["frames"] else {"elements": []}

        def change():
            for _ in range(missing):
                # New frames carry the last frame's placements, so they keep
                # showing what was on screen instead of going blank.
                clip["frames"].append(
                    {
                        "label": "",
                        "elements": [list(e) for e in template.get("elements", [])],
                    }
                )

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.refresh()
            self.frame.setValue(min(needed, len(clip["frames"]) - 1))

    def goto_frame(self, index):
        """Jump to a frame from the strip, stopping playback first."""
        if self.timer.isActive():
            self.toggle_play()
        maximum = self.frame.maximum()
        self.frame.setValue(max(0, min(index, maximum)))

    def keyframe_selection_changed(self):
        """Keep the tween button in step with the selected keys."""
        keys = self.selected_key_frames()
        if keys:
            self.bake_button.setText(
                f"Curve between {len(keys)} key{'s' if len(keys) != 1 else ''}"
            )
        else:
            self.bake_button.setText("Curve between all keys")
        self.update_curve_info()

    def selected_key_frames(self):
        """Frames of the keypoints selected on the current track."""
        row = self.placement_row()
        return sorted(
            frame
            for frame, chosen_row in self.keyframes.selected_keys()
            if chosen_row == row
        )

    def update_curve_info(self):
        """Describe what applying the curve would affect, next to the button."""
        if not self.animatable():
            self.curve_info.setText("Select a keypoint or a placement to tween.")
            return
        keys = self.placement_keys(self.obj)
        if len(keys) < 2:
            self.curve_info.setText(
                "Set a keyframe on another frame to tween between them."
            )
            return
        chosen = self.selected_key_frames()
        span_keys = chosen if len(chosen) >= 2 else sorted(keys)
        dense = self.key_density.value()
        low, high = span_keys[0], span_keys[-1]
        span = high - low
        step = max(1, round(span / dense)) if dense else 1
        estimate = max(0, span // step) if span else 0
        scope = (
            f"the {len(chosen)} keys you picked"
            if len(chosen) >= 2
            else f"all {len(keys)} keys"
        )
        self.curve_info.setText(
            f"Curve '{self.curve.currentText()}' between {scope} writes about "
            f"{estimate} frames across {low + 1}-{high + 1}."
        )

    def selected_key_range(self):
        """The keyed frames inside the strip's selection, in order."""
        if self.selected not in self.doc.index:
            return []
        keys = self.placement_keys(self.obj)
        frames = self.keyframes.selected_frames()
        if frames:
            keys = [k for k in keys if k in set(frames)]
        return sorted(keys)

    def apply_curve_to_range(self):
        """Fill the gaps between the selected keys with the chosen curve.

        The selection is the keys themselves, not a range of frames: pick two
        keys and everything between them is tweened, pick several and each gap
        between them is filled. The keys are kept exactly and only the frames
        between them are written, at the interval "Keys to create" asks for.
        """
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        found = self.key_transforms(clip)
        if len(found) < 2:
            self.window().statusBar().showMessage(
                "Set at least two keyframes to tween between", 5000
            )
            return
        chosen = sorted(
            key for key in self.keyframes.selected_keys() if key[0] in found
        )
        # Keys selected on this track; fall back to the whole key set when
        # nothing has been picked, which is the usual "tween it all" case.
        frames = sorted({frame for frame, chosen_row in chosen if chosen_row == row})
        if len(frames) < 2:
            frames = sorted(found)
        if len(frames) < 2:
            self.window().statusBar().showMessage(
                "Select at least two keys to tween between", 5000
            )
            return

        points = easing.curve_for(self.curve.currentText())
        wanted = self.key_density.value()
        written = set()
        for start_frame, end_frame in zip(frames, frames[1:]):
            span = end_frame - start_frame
            if span < 2:
                continue
            step = max(1, round(span / wanted)) if wanted else 1
            offset = 1
            while offset < span:
                index = start_frame + offset
                if index < end_frame and index < len(clip["frames"]):
                    if row < len(clip["frames"][index]["elements"]):
                        progress = offset / span
                        eased = easing.bezier(progress, *points)
                        clip["frames"][index]["elements"][row][1] = (
                            self.intern_matrix(
                                clip,
                                easing.interpolate_matrix(
                                    found[start_frame][0], found[end_frame][0], eased
                                ),
                            )
                        )
                        written.add(index)
                offset += step
        if not written:
            self.window().statusBar().showMessage(
                "No frames between the selected keys needed filling", 5000
            )
            return
        if self.mutate(lambda: self.bake_and_compact(clip, row)):
            self.baked = (self.selected, row, written)
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(
                f"Applied '{self.curve.currentText()}' between {len(frames)} keys, "
                f"writing {len(written)} frames "
                f"({frames[0] + 1}-{frames[-1] + 1})",
                6000,
            )
    def keypoint_context_menu(self, index, row, position):
        """Actions for one keyframe, on the track it was clicked on."""
        if self.elements.currentRow() != row:
            self.select_track(row)
        menu = QMenu(self)
        menu.addAction("Remove this keypoint").triggered.connect(
            lambda: self.toggle_keypoint_at(index, row)
        )
        menu.addAction("Go to this frame").triggered.connect(
            lambda: self.goto_frame(index)
        )
        menu.addAction("Select up to this key").triggered.connect(
            lambda: self.keyframes.set_band(
                min(self.placement_keys(self.obj) or [index]), index
            )
        )
        menu.addAction("Delete this frame").triggered.connect(
            lambda: self.remove_frame_at(index)
        )
        menu.exec(position)

    def keyframe_context_menu(self, frames, position):
        """Frame and tween operations for the selected range of frames."""
        if self.selected not in self.doc.index:
            return
        frames = list(frames) or [self.frame.value()]
        menu = QMenu(self)
        keyed = [f for f in frames if f in self.placement_keys(self.doc.resource(self.selected))]
        if keyed:
            menu.addAction(f"Remove {len(keyed)} keypoint(s)").triggered.connect(
                lambda: self.remove_keypoints(keyed)
            )
        else:
            menu.addAction("Add keypoint here").triggered.connect(
                lambda: self.toggle_keypoint_at(frames[0], self.placement_row())
            )
        if len(frames) > 1:
            menu.addAction("Select these frames").triggered.connect(
                lambda: self.keyframes.set_band(frames[0], frames[-1])
            )
        menu.addSeparator()
        tween = menu.addMenu("Apply curve to selection")
        for name in easing.PRESETS:
            tween.addAction(name).triggered.connect(
                lambda checked=False, value=name: self.apply_curve_named(value, frames)
            )
        menu.addSeparator()
        menu.addAction("Go to first frame").triggered.connect(
            lambda: self.goto_frame(frames[0])
        )
        menu.addAction("Add frame after").triggered.connect(
            lambda: self.add_frame_at(frames[0], False)
        )
        menu.addAction("Duplicate this frame").triggered.connect(
            lambda: self.add_frame_at(frames[0], True)
        )
        count = len(self.obj.get("frames") or [])
        remove = menu.addAction("Delete this frame")
        remove.setEnabled(count > 1)
        remove.triggered.connect(lambda: self.remove_frame_at(frames[0]))
        menu.exec(position)

    def apply_curve_named(self, name, frames):
        """Apply a named curve to a range, from the strip's context menu."""
        self.curve.setCurrentText(name)
        self.keyframes.set_band(frames[0], frames[-1])
        self.apply_curve_to_range()

    def remove_keypoints(self, frames):
        """Drop keys on the given frames for the selected placement."""
        if not self.resolve_drafts() or self.selected not in self.doc.index:
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        removed = 0
        for index in frames:
            if not 0 <= index < len(clip["frames"]):
                continue
            if row >= len(clip["frames"][index]["elements"]):
                continue
            element = clip["frames"][index]["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                continue
            element[1] = 65535
            element[2] = 65535
            removed += 1
        if not removed:
            return
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(f"Removed {removed} keys", 4000)

    def select_all_keys(self):
        """Select from the first to the last key, so a whole tween is one gesture."""
        if self.selected in self.doc.index:
            self.keyframes.select_keys()

    def add_frame_at(self, index, duplicate):
        """Insert a frame after ``index``, keeping the playhead on the original."""
        self.goto_frame(index)
        self.add_frame(duplicate)

    def remove_frame_at(self, index):
        self.goto_frame(index)
        self.remove_frame()

    def toggle_keypoint_at(self, index, row):
        """Add a keypoint on a frame, or remove the one already there."""
        if not (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
            and 0 <= row < self.elements.rowCount()
        ) or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        if not 0 <= index < len(clip["frames"]):
            return
        if row >= len(clip["frames"][index]["elements"]):
            self.window().statusBar().showMessage(
                f"This placement is not present in frame {index + 1}", 4000
            )
            return
        element = clip["frames"][index]["elements"][row]

        def change():
            if element[1] != 65535 or element[2] != 65535:
                element[1] = 65535
                element[2] = 65535
                return False
            # A new key starts from the transform the placement already renders
            # with, so adding one never moves the object on screen.
            reference = self.matrix_at(clip, index, row)
            element[1] = self.intern_matrix(clip, reference)
            return True

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row

    def matrix_at(self, clip, index, row):
        """The transform a placement renders with on a frame, keyed or not.

        Without a key the placement inherits the nearest earlier key, which is
        how the engine resolves it; identity only if there is no earlier key.
        """
        for earlier in range(index, -1, -1):
            if row >= len(clip["frames"][earlier]["elements"]):
                continue
            element = clip["frames"][earlier]["elements"][row]
            if element[1] != 65535:
                matrices = self.clip_bank(clip).get("matrices") or []
                if element[1] < len(matrices):
                    return list(matrices[element[1]])
                return list(easing.IDENTITY)
        return list(easing.IDENTITY)

    def move_keypoint(self, source, target):
        """Drag a keypoint to another frame, carrying its transform with it.

        Dragging a key past the end extends the clip first, so a key can be
        placed on a frame that does not exist yet by moving it there.
        """
        if source == target or not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        if target >= len(clip["frames"]):
            self.extend_to_frame(target)
            if not self.resolve_drafts():
                return
            clip = self.doc.resource(self.selected)
        row = self.placement_row()
        if not 0 <= target < len(clip["frames"]):
            return
        if row >= len(clip["frames"][source]["elements"]) or row >= len(
            clip["frames"][target]["elements"]
        ):
            return
        origin = clip["frames"][source]["elements"][row]
        if origin[1] == 65535 and origin[2] == 65535:
            return
        matrix = self.matrix_at(clip, source, row)
        colours = self.clip_bank(clip).get("colors") or []
        colour = (
            list(colours[origin[2]])
            if origin[2] != 65535 and origin[2] < len(colours)
            else None
        )
        destination = clip["frames"][target]["elements"][row]

        def change():
            destination[1] = self.intern_matrix(clip, matrix)
            if colour is not None:
                destination[2] = self.intern_color(clip, colour)
            origin[1] = 65535
            origin[2] = 65535

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row
            self.window().statusBar().showMessage(
                f"Moved keypoint to frame {target + 1}", 4000
            )

    def clip_bank(self, clip):
        """The clip's transform bank, or an empty one when it has none yet.

        A freshly created movieclip can name a bank index that does not exist,
        and document.bank() raises on that rather than returning nothing.
        """
        index = clip.get("matrix_bank_index")
        if index is None or index >= len(self.doc.storage["matrix_banks"]):
            return {"matrices": [], "colors": []}
        return self.doc.bank(index)

    def key_transforms(self, clip):
        """Matrix and colour per keyed frame, for the selected placement."""
        bank = self.clip_bank(clip)
        colours = bank.get("colors") or []
        matrices = bank.get("matrices") or []
        found = {}
        for index in self.placement_keys(clip):
            element = clip["frames"][index]["elements"][self.placement_row()]
            matrix = (
                list(matrices[element[1]])
                if element[1] != 65535 and element[1] < len(matrices)
                else list(easing.IDENTITY)
            )
            colour = (
                list(colours[element[2]])
                if element[2] != 65535 and element[2] < len(colours)
                else None
            )
            found[index] = (matrix, colour)
        return found

    def load_animation_fields(self):
        """Show the placement's transform on this frame in the editor fields."""
        enabled = self.animatable()
        for box in self.key_fields.values():
            box.setEnabled(enabled)
        self.apply_animation_button.setEnabled(enabled)
        if not enabled:
            self.animation_hint.setText(
                "Select a movieclip placement to animate it."
            )
            return
        clip = self.obj
        element = clip["frames"][self.frame.value()]["elements"][
            self.placement_row()
        ]
        matrix = self.effective_matrix(clip, element)
        parts = easing.decompose(matrix)
        values = {
            "X": parts["tx"],
            "Y": parts["ty"],
            "Rotation": math.degrees(parts["rotation"]),
            "Scale X": parts["scale_x"],
            "Scale Y": parts["scale_y"],
        }
        for name, value in values.items():
            with QSignalBlocker(self.key_fields[name]):
                self.key_fields[name].setValue(value)
        keyed = element[1] != 65535
        self.animation_hint.setText(
            f"Frame {self.frame.value() + 1}: "
            + ("already has a key." if keyed else "no key yet; applying creates one.")
        )

    def apply_animation_fields(self):
        """Key the edited transform on the current frame.

        Editing the transform of a placement that shares a matrix with other
        frames would change those frames too, so this always gives the frame its
        own key rather than reusing the shared one.
        """
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        index = self.frame.value()
        element = clip["frames"][index]["elements"][row]
        matrix = easing.compose(
            {
                "tx": self.key_fields["X"].value(),
                "ty": self.key_fields["Y"].value(),
                "rotation": math.radians(self.key_fields["Rotation"].value()),
                "scale_x": self.key_fields["Scale X"].value(),
                "scale_y": self.key_fields["Scale Y"].value(),
            }
        )

        def change():
            element[1] = self.intern_matrix(clip, matrix)

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row
            self.window().statusBar().showMessage(
                f"Key set on frame {index + 1}", 4000
            )

    def refresh_keypoints(self):
        """Redraw the curve preview; the timeline itself shows the keys."""
        self.draw_curve(self.curve.currentText())
        self.update_curve_info()

    def animatable(self):
        """Whether the current frame holds a placement the strip can key."""
        if self.selected not in self.doc.index:
            return False
        if self.doc.index[self.selected][0] != "movieclips":
            return False
        frames = self.obj.get("frames") or []
        index = self.frame.value()
        if not 0 <= index < len(frames):
            return False
        return 0 <= self.placement_row() < len(frames[index]["elements"])

    def draw_curve(self, name):
        """Render the selected curve so its shape is visible before baking."""
        width, height = 110, 34
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(QColor("transparent"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#6b7688"), 1))
        painter.drawRect(0, 0, width - 1, height - 1)
        points = easing.sample(name, width)
        # Overshoot and anticipate leave 0..1, so the plot is padded to keep the
        # whole curve on screen.
        low = min([0.0] + points)
        high = max([1.0] + points)
        span = max(high - low, 1e-6)
        path = QPainterPath()
        for x, value in enumerate(points):
            y = height - 2 - (value - low) / span * (height - 4)
            if x == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(QColor("#63b5ff"), 2))
        painter.drawPath(path)
        painter.end()
        self.curve_preview.setPixmap(QPixmap.fromImage(image))

    def set_keypoint(self):
        """Give the current frame its own transform, creating a keypoint."""
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        element = clip["frames"][self.frame.value()]["elements"][row]
        matrix = self.effective_matrix(clip, element)
        if self.mutate(lambda: self.assign_matrix(clip, element, matrix)):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row
            self.window().statusBar().showMessage(
                f"Key set on frame {self.frame.value() + 1}", 4000
            )

    def effective_matrix(self, clip, element):
        """The transform the placement renders with right now."""
        if element[1] == 65535:
            return list(easing.IDENTITY)
        matrices = self.clip_bank(clip).get("matrices") or []
        if element[1] < len(matrices):
            return list(matrices[element[1]])
        return list(easing.IDENTITY)

    def delete_keypoint(self):
        """Drop the current frame's key, so the frame inherits again."""
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        element = clip["frames"][self.frame.value()]["elements"][row]
        if element[1] == 65535 and element[2] == 65535:
            self.window().statusBar().showMessage(
                f"Frame {self.frame.value() + 1} has no key", 4000
            )
            return
        element[1] = 65535
        element[2] = 65535
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.elements.selectRow(row)

    def clear_keypoints(self):
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        keys = self.placement_keys(clip)
        if not keys:
            return
        for index in keys:
            element = clip["frames"][index]["elements"][row]
            element[1] = 65535
            element[2] = 65535
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(f"Cleared {len(keys)} keys", 4000)

    def bake_tween(self):
        """Curve the whole key range, ignoring the strip's current selection."""
        self.keyframes.clear_band()
        self.apply_curve_to_range()

    def bake_and_compact(self, clip, row):
        """Compact the baked bank once, then store the clip."""
        element = clip["frames"][self.frame.value()]["elements"][row]
        self.assign_matrix(clip, element, self.effective_matrix(clip, element))

    def revert_tween(self):
        """Drop exactly the frames the last bake wrote for this placement.

        Only frames recorded by bake_tween are touched. Inferring them from the
        key layout instead would be wrong: a clip keyed on every frame looks
        identical to a baked one, and guessing would delete the author's own
        keyframes.
        """
        if not self.resolve_drafts():
            return
        if self.selected not in self.doc.index:
            return
        if self.baked is None or self.baked[0] != self.selected:
            self.window().statusBar().showMessage(
                "No baked tween recorded for this clip; bake one first", 5000
            )
            return
        _, row, frames = self.baked
        clip = self.doc.resource(self.selected)
        removed = 0
        for index in sorted(frames):
            if not 0 <= index < len(clip["frames"]):
                continue
            if row >= len(clip["frames"][index]["elements"]):
                continue
            element = clip["frames"][index]["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                continue
            element[1] = 65535
            element[2] = 65535
            removed += 1
        if not removed:
            self.window().statusBar().showMessage("Baked frames are already clear", 4000)
            return
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.baked = None
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(
                f"Reverted {removed} baked frames", 4000
            )

    def show_elements(self):
        if (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
        ):
            obj = self.obj
            i = self.frame_index()
            fill_table(
                self.elements,
                obj["frames"][i]["elements"] if i < len(obj["frames"]) else [],
            )
            self.refresh_keypoints()

    def fill_visibility(self):
        hidden = self.child_visibility.get(self.selected, set())
        with QSignalBlocker(self.children):
            for row in range(self.children.rowCount()):
                item = QTableWidgetItem()
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(
                    Qt.CheckState.Unchecked if row in hidden else Qt.CheckState.Checked
                )
                item.setToolTip("Preview visibility only; the SC file is unchanged")
                self.children.setItem(row, 3, item)
        self.children.resizeColumnsToContents()

    def child_changed(self, row, column):
        if column != 3:
            self.timeline_changed()
            return
        if self.loading or self.selected not in self.doc.index:
            return
        hidden = self.child_visibility.setdefault(self.selected, set())
        if self.children.item(row, 3).checkState() == Qt.CheckState.Checked:
            hidden.discard(row)
        else:
            hidden.add(row)
        self.render_frame(value=self.frame.value(), fit=False)

    def show_all_children(self):
        self.child_visibility.pop(self.selected, None)
        self.fill_visibility()
        self.render_frame(value=self.frame.value(), fit=False)

    def timeline_changed(self, *args):
        if self.loading or self.selected not in self.doc.index:
            return
        try:
            obj = self.doc.resource(self.selected)
            obj["children"] = [
                {
                    "id": int(self.children.item(i, 0).text()),
                    "name": self.children.item(i, 1).text(),
                    "blend": int(self.children.item(i, 2).text()),
                }
                for i in range(self.children.rowCount())
            ]
            for i, f in enumerate(obj["frames"]):
                f["label"] = self.frames.item(i, 1).text()
            if obj["frames"]:
                obj["frames"][self.frame_index()]["elements"] = [
                    [int(self.elements.item(i, j).text()) for j in range(3)]
                    for i in range(self.elements.rowCount())
                ]
            if self.mutate(lambda: self.doc.update("movieclips", obj)):
                self.obj = obj
                self.update_code(obj)
                self.render_frame(fit=False)
            else:
                self.refresh()
        except Exception as e:
            self.error(e)
            self.refresh()

    def clip_edit(self, fn):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        obj = self.doc.resource(self.selected)
        try:
            fn(obj)
        except Exception as e:
            self.error(e)
            return
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()

    def add_child(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Add child resource")
        dialog.resize(480, 400)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Filter by name or ID…")
        layout.addWidget(search)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Resource", "ID"])
        layout.addWidget(tree)

        def populate():
            tree.clear()
            query = search.text().casefold()
            for id, (kind, raw) in self.doc.index.items():
                label = " / ".join(self.doc.names.get(id, [])) or f"{kind[:-1]} {id}"
                if query and query not in label.casefold() and query not in str(id):
                    continue
                item = QTreeWidgetItem(tree, [label, str(id)])
                item.setData(0, Qt.ItemDataRole.UserRole, id)

        search.textChanged.connect(populate)
        populate()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        new_clip = QPushButton("Create new movieclip…")
        layout.addWidget(new_clip)

        def create_child():
            name, ok = QInputDialog.getText(
                dialog, "New child movieclip", "Instance name:"
            )
            if not ok:
                return
            created = []

            def change():
                child_id = self.doc.new_resource("movieclips")
                created.append(child_id)
                self.place_child(child_id, name or "movieclip")

            if self.mutate(change):
                dialog.reject()
                self.refresh()
                self.set_panel(5)
                self.elements.selectRow(self.elements.rowCount() - 1)

        new_clip.clicked.connect(create_child)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        tree.itemDoubleClicked.connect(lambda *_: dialog.accept())
        if dialog.exec() == QDialog.DialogCode.Accepted and tree.currentItem():
            id = tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
            if self.mutate(
                lambda: self.place_child(id, " / ".join(self.doc.names.get(id, [])))
            ):
                self.refresh()
                self.set_panel(5)
                self.elements.selectRow(self.elements.rowCount() - 1)

    def place_child(self, id, name):
        obj = self.doc.resource(self.selected)
        if not obj["frames"]:
            obj["frames"].append({"label": "", "elements": []})
        index = len(obj["children"])
        obj["children"].append({"id": id, "name": name, "blend": 0})
        obj["frames"][self.frame.value()]["elements"].append([index, 65535, 65535])
        self.doc.update("movieclips", obj)

    def view_shape_atlas(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
            or not self.resolve_drafts()
        ):
            return
        obj = self.doc.resource(self.selected)
        if not obj["commands"]:
            return
        self.view_selection_atlas()

    def view_selection_atlas(self):
        """Open the first reachable shape's atlas, falling back to atlas zero."""
        if not self.doc.tables["Textures"]["textures"]:
            return
        pending, seen, atlas = [self.selected], set(), 0
        while pending:
            id = pending.pop()
            if id in seen or id not in self.doc.index:
                continue
            seen.add(id)
            kind, raw = self.doc.index[id]
            if kind == "shapes" and raw["commands"]:
                row = max(0, self.commands.currentRow()) if id == self.selected else 0
                atlas = raw["commands"][min(row, len(raw["commands"]) - 1)]["texture_index"]
                break
            if kind == "movieclips":
                pending.extend(reversed(raw["children_ids"]))
        self.timer.stop()
        self.play.setText("▶ Play")
        self.texture_index = atlas
        self.selected = None
        with QSignalBlocker(self.category):
            self.category.setCurrentIndex(self.resource_kinds.index("textures"))
        self.populate()
        with QSignalBlocker(self.tree):
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item.data(0, Qt.ItemDataRole.UserRole) == ("texture", atlas):
                    self.tree.setCurrentItem(item)
                    break
        self.show_texture()

    def toggle_atlas_outlines(self, visible):
        self.preview.show_atlas_outlines = visible
        self.preview.viewport().update()

    def open_atlas_shape(self, id):
        if self.go_to(id):
            self.set_panel(4)

    def atlas_context_menu(self, regions, position):
        menu = QMenu(self)
        unique = list(dict.fromkeys(region["id"] for region in regions))
        candidates = menu.addMenu(f"Shapes at cursor ({len(unique)})")
        candidates.setEnabled(bool(unique))
        for id in unique:
            label = " / ".join(self.doc.names.get(id, [])) or f"Shape #{id}"
            target = candidates.addMenu(f"{label} · #{id}" if self.doc.names.get(id) else label)
            target.addAction(f"Open shape #{id}").triggered.connect(
                lambda checked=False, value=id: self.open_atlas_shape(value)
            )
            target.addAction("Find usages…").triggered.connect(
                lambda checked=False, value=id: self.window().references(value)
            )
            target.addAction("Reference tree…").triggered.connect(
                lambda checked=False, value=id: self.window().references(value, tree_mode=True)
            )
        if unique:
            menu.addAction(f"Choose shape… ({len(unique)} matches)").triggered.connect(
                lambda: self.browse_atlas_shapes(regions)
            )
        if not unique:
            menu.addAction("No referenced shape at this point").setEnabled(False)
        menu.addAction("Browse all atlas shapes…").triggered.connect(self.browse_atlas_shapes)
        menu.addSeparator()
        outlines = menu.addAction("Show asset outlines")
        outlines.setCheckable(True)
        outlines.setChecked(self.atlas_outlines.isChecked())
        outlines.triggered.connect(self.atlas_outlines.setChecked)
        menu.addAction("Fit atlas").triggered.connect(self.preview.fit_content)
        menu.exec(position)

    def browse_atlas_shapes(self, regions=None):
        # QAction/QPushButton triggered signals pass a bool, not a region list.
        if regions is None or isinstance(regions, bool):
            regions = self.preview.atlas_regions
        if regions is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Atlas #{self.texture_index} — Shapes")
        dialog.resize(780, 500)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Filter shape ID, export name or referencing movieclip…")
        layout.addWidget(search)
        tree = QTreeWidget()
        tree.setObjectName("atlas_shapes_picker")
        tree.setHeaderLabels(
            ["Shape", "ID", "Commands", "Refs", "UV rectangles (x, y, w, h)"]
        )
        tree.setColumnWidth(0, 220)
        layout.addWidget(tree)
        grouped = {}
        for region in regions:
            grouped.setdefault(region["id"], []).append(region)
        # Reverse lookup once: large atlases must not rescan all clips for each row.
        parents = {}
        for parent, (kind, raw) in self.doc.index.items():
            if kind == "movieclips":
                for child in set(raw["children_ids"]):
                    if child in grouped:
                        parents.setdefault(child, []).append(parent)
        rows = []
        for id, entries in grouped.items():
            label = " / ".join(self.doc.names.get(id, [])) or f"Shape #{id}"
            rects = "; ".join(", ".join(f"{v:.1f}" for v in entry["rect"]) for entry in entries)
            usages = " ".join(str(parent) + " " + " ".join(self.doc.names.get(parent, []))
                              for parent in parents.get(id, []))
            rows.append(
                (
                    id,
                    [label, str(id), str(len(entries)), str(len(parents.get(id, []))), rects],
                    usages,
                )
            )
        count = QLabel()
        layout.addWidget(count)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        def populate():
            tree.clear()
            query = search.text().casefold()
            for id, values, usages in rows:
                if query and query not in (" ".join(values) + " " + usages).casefold():
                    continue
                item = QTreeWidgetItem(tree, values)
                item.setData(0, Qt.ItemDataRole.UserRole, id)
                item.setToolTip(0, "Referenced by: " + (usages or "no movieclip"))
            count.setText(f"{tree.topLevelItemCount()} of {len(grouped)} shapes · {len(regions)} referenced regions")
            if tree.topLevelItemCount():
                tree.setCurrentItem(tree.topLevelItem(0))
            buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(bool(tree.topLevelItemCount()))

        search.textChanged.connect(populate)
        tree.itemDoubleClicked.connect(lambda *_: dialog.accept())
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        populate()
        search.setFocus()
        if dialog.exec() == QDialog.DialogCode.Accepted and tree.currentItem():
            self.open_atlas_shape(tree.currentItem().data(0, Qt.ItemDataRole.UserRole))

    def reorder_placement(self, row, target):
        if not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        elements = obj["frames"][self.frame.value()]["elements"]
        target = max(0, min(target, len(elements) - 1))
        if row == target:
            return
        element = elements.pop(row)
        elements.insert(target, element)
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            self.select_placement(target)
            self.preview.selected_placement = target

    def set_layer(self, row):
        count = len(self.obj["frames"][self.frame.value()]["elements"])
        target, ok = QInputDialog.getInt(
            self, "Layer position", "Draw order (0 = back):", row, 0, count - 1
        )
        if ok:
            self.reorder_placement(row, target)

    def viewer_context_menu(self, row, position):
        menu = QMenu(self)
        if row >= 0:
            self.preview.selected_placement = row
            self.select_placement(row)
            count = len(self.obj["frames"][self.frame.value()]["elements"])
            actions = [
                ("Rename instance…", self.rename_child),
                ("Open child", self.open_child),
                (
                    "Find child usages…",
                    lambda: self.window().references(
                        self.obj["children"][self.children.currentRow()]["id"]
                    ),
                ),
                ("Size / position…", self.resize_placement),
                ("Transform / color…", self.edit_transform),
                ("Bring to front", lambda: self.reorder_placement(row, count - 1)),
                ("Move forward", lambda: self.reorder_placement(row, row + 1)),
                ("Move backward", lambda: self.reorder_placement(row, row - 1)),
                ("Send to back", lambda: self.reorder_placement(row, 0)),
                ("Layer position…", lambda: self.set_layer(row)),
                (
                    "Reset transform",
                    lambda: self.commit_placement_transform(row, [1, 0, 0, 1, 0, 0]),
                ),
                ("Duplicate child", self.duplicate_child),
                (
                    "Hide child",
                    lambda: self.children.item(
                        self.children.currentRow(), 3
                    ).setCheckState(Qt.CheckState.Unchecked),
                ),
                ("Remove placement from current frame", self.remove_element),
                ("Delete child from all frames…", self.remove_child),
            ]
        else:
            actions = [
                ("Fit preview", self.preview.fit_content),
                ("Show all children", self.show_all_children),
            ]
            if (
                self.selected in self.doc.index
                and self.doc.index[self.selected][0] == "movieclips"
            ):
                actions += [
                    ("Add child…", self.add_child),
                    ("Import image…", self.import_image),
                ]
        if row < 0 and self.selected in self.doc.index:
            if self.doc.index[self.selected][0] == "shapes":
                actions.append(("View texture atlas…", self.view_shape_atlas))
            actions.append(
                ("Find usages…", lambda: self.window().references(self.selected))
            )
        for label, fn in actions:
            menu.addAction(label).triggered.connect(fn)
        menu.exec(position)

    def children_selection_changed(self):
        if (
            self.loading
            or self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        ci = self.children.currentRow()
        elements = (
            self.obj["frames"][self.frame.value()]["elements"]
            if self.obj["frames"]
            else []
        )
        row = next((i for i, e in enumerate(elements) if e[0] == ci), -1)
        with QSignalBlocker(self.elements):
            if row >= 0:
                self.elements.selectRow(row)
            else:
                self.elements.clearSelection()
                self.elements.setCurrentCell(-1, -1)
        self.preview.selected_placement = row if row >= 0 else None
        self.preview.viewport().update()

    def rename_child(self):
        row = self.children.currentRow()
        if row < 0 or not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        name, ok = QInputDialog.getText(
            self, "Rename child", "Instance name:", text=obj["children"][row]["name"]
        )
        if ok:
            obj["children"][row]["name"] = name
            if self.mutate(lambda: self.doc.update("movieclips", obj)):
                self.refresh()
                self.children.selectRow(row)

    def open_child(self):
        row = self.children.currentRow()
        if row >= 0 and self.selected in self.doc.index:
            self.go_to(self.doc.resource(self.selected)["children"][row]["id"])

    def duplicate_child(self):
        ci = self.children.currentRow()
        if ci < 0 or not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        child = copy.deepcopy(obj["children"][ci])
        child["name"] = (child["name"] or "child") + "_copy"
        index = len(obj["children"])
        obj["children"].append(child)
        for frame in obj["frames"]:
            frame["elements"].extend(
                [[index, e[1], e[2]] for e in list(frame["elements"]) if e[0] == ci]
            )
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            self.children.selectRow(index)

    def reorder_child(self, front):
        ci = self.children.currentRow()
        if ci < 0 or not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        elements = obj["frames"][self.frame.value()]["elements"]
        selected = [e for e in elements if e[0] == ci]
        others = [e for e in elements if e[0] != ci]
        obj["frames"][self.frame.value()]["elements"] = (
            others + selected if front else selected + others
        )
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            self.children.selectRow(ci)

    def commit_placement_transform(self, row, matrix):
        if not self.resolve_drafts():
            self.render_frame(value=self.frame.value(), fit=False)
            return
        clip = self.doc.resource(self.selected)
        fi = self.frame.value()
        element = clip["frames"][fi]["elements"][row]
        if self.mutate(lambda: self.assign_matrix(clip, element, matrix)):
            self.refresh()
            self.select_placement(row)
            self.preview.selected_placement = row
            self.preview.viewport().update()

    def remove_child(self):
        i = self.children.currentRow()
        if i < 0:
            return

        if not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        name = obj["children"][i]["name"] or f"child {i}"
        if (
            QMessageBox.question(
                self,
                "Delete child",
                f"Remove {name} and its placements from every frame?\nThe shared asset itself is kept.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        obj["children"].pop(i)
        for frame in obj["frames"]:
            frame["elements"] = [e for e in frame["elements"] if e[0] != i]
            for e in frame["elements"]:
                if e[0] > i:
                    e[0] -= 1
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            hidden = self.child_visibility.get(self.selected, set())
            self.child_visibility[self.selected] = {
                c - 1 if c > i else c for c in hidden if c != i
            }
            self.refresh()
            self.children.selectRow(min(i, self.children.rowCount() - 1))

    def add_frame(self, duplicate):
        i = self.frame_index()
        self.clip_edit(
            lambda o: o["frames"].insert(
                i + 1,
                (
                    copy.deepcopy(o["frames"][i])
                    if duplicate and o["frames"]
                    else {"label": "", "elements": []}
                ),
            )
        )

    def remove_frame(self):
        """Delete the current frame, never the last one a clip must keep."""
        i = self.frame_index()
        if len(self.obj.get("frames") or []) <= 1:
            self.window().statusBar().showMessage(
                "A movieclip must keep at least one frame", 4000
            )
            return
        self.clip_edit(lambda o: o["frames"].pop(i))

    def add_element(self):
        i = self.frame_index()

        def change(o):
            if not o["children"] or not o["frames"]:
                raise ValueError("Add a child and a frame first")
            o["frames"][i]["elements"].append([0, 65535, 65535])

        self.clip_edit(change)

    def remove_element(self):
        row = self.elements.currentRow()
        i = self.frame_index()
        if row >= 0:
            self.clip_edit(lambda o: o["frames"][i]["elements"].pop(row))

    def select_placement(self, row):
        self.preview.selected_placement = row
        self.preview.viewport().update()
        if self.timer.isActive():
            self.toggle_play()
        self.set_panel(5)
        self.elements.selectRow(row)
        if self.selected in self.doc.index and row < len(
            self.obj["frames"][self.frame.value()]["elements"]
        ):
            ci = self.obj["frames"][self.frame.value()]["elements"][row][0]
            with QSignalBlocker(self.children):
                self.children.selectRow(ci)

    def move_placement(self, row, dx, dy):
        if not self.resolve_drafts():
            self.render_frame(fit=False)
            return
        fi = self.frame.value()
        obj = self.doc.resource(self.selected)
        element = obj["frames"][fi]["elements"][row]
        matrix = (
            list(self.doc.bank(obj["matrix_bank_index"])["matrices"][element[1]])
            if element[1] != 65535
            else [1, 0, 0, 1, 0, 0]
        )
        matrix[4] += dx
        matrix[5] += dy
        # Moving a placement cannot rename anything, so the browser list is
        # left alone; rebuilding it was the bulk of the lag after a drag.
        if self.mutate(
            lambda: self.assign_matrix(obj, element, matrix), rebuild_browser=False
        ):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row

    def size_dialog(self, title, width, height, x=0, y=0, rotation=0):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        fields = {}
        for name, value in [
            ("Width", width),
            ("Height", height),
            ("X", x),
            ("Y", y),
            ("Rotation (degrees)", rotation),
        ]:
            box = QDoubleSpinBox()
            box.setDecimals(3)
            box.setRange(0.001 if name in ("Width", "Height") else -1e6, 1e6)
            box.setValue(value)
            form.addRow(name, box)
            fields[name] = box
        aspect = QCheckBox("Keep aspect ratio")
        aspect.setChecked(True)
        form.addRow(aspect)
        ratio = width / max(height, 0.001)

        def proportional(name, other, factor, value):
            if aspect.isChecked():
                with QSignalBlocker(fields[other]):
                    fields[other].setValue(value * factor)

        fields["Width"].valueChanged.connect(
            lambda v: proportional("Width", "Height", 1 / ratio, v)
        )
        fields["Height"].valueChanged.connect(
            lambda v: proportional("Height", "Width", ratio, v)
        )
        form.addRow(
            QLabel(
                "Size and position affect this placement in the current frame.\nThe original image stays at its full resolution."
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return [box.value() for box in fields.values()]

    def import_image(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            self.error("Select or create a movieclip first, then choose Import image.")
            return
        if not self.resolve_drafts():
            return
        path, _ = native_picker(
            self,
            "Import image into movieclip",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)
            values = self.size_dialog(
                "Import image — size and position", data["width"], data["height"]
            )
            if values is not None:
                self.insert_image(data, Path(path).stem, values)
        except Exception as ex:
            self.error(ex)

    def insert_image(self, data, name, values, raise_errors=False):
        import math

        w, h, x, y, angle = values
        angle = math.radians(angle)
        cs, sn = math.cos(angle), math.sin(angle)
        fi = self.frame.value()
        clip = self.doc.resource(self.selected)

        def change():
            atlas = len(self.doc.tables["Textures"]["textures"])
            self.doc.tables["Textures"]["textures"].append(
                {"highres": data, "lowres": None}
            )
            self.doc.dirty.add("Textures")
            shape_id = self.doc.new_resource("shapes")
            self.doc.update(
                "shapes",
                {
                    "id": shape_id,
                    "exports": [],
                    "commands": [
                        {
                            "unk1": 0,
                            "texture_index": atlas,
                            "vertices": [
                                [0, 0, 0, 0],
                                [data["width"], 0, 65535, 0],
                                [0, data["height"], 0, 65535],
                                [data["width"], data["height"], 65535, 65535],
                            ],
                        }
                    ],
                },
            )
            if not clip["frames"]:
                clip["frames"].append({"label": "", "elements": []})
            ci = len(clip["children"])
            clip["children"].append({"id": shape_id, "name": name, "blend": 0})
            e = [ci, 65535, 65535]
            clip["frames"][fi]["elements"].append(e)
            self.assign_matrix(
                clip,
                e,
                [
                    cs * w / data["width"],
                    sn * w / data["width"],
                    -sn * h / data["height"],
                    cs * h / data["height"],
                    x,
                    y,
                ],
            )

        if self.mutate(change, raise_errors=raise_errors):
            self.refresh()
            self.set_panel(5)
            self.frames.selectRow(fi)
            self.elements.selectRow(self.elements.rowCount() - 1)
            self.render_frame(fit=True)

    def intern_matrix(self, clip, matrix):
        """Append a matrix to the clip's bank and return its new local index.

        The whole clip is recompacted once at the end of an edit rather than
        per append, which keeps baking a long tween linear instead of quadratic.
        """
        bank = (
            self.doc.bank(clip["matrix_bank_index"])
            if clip["matrix_bank_index"] < len(self.doc.storage["matrix_banks"])
            else {"matrices": [], "colors": []}
        )
        matrices = list(bank.get("matrices") or [])
        matrices.append([float(v) for v in matrix])
        clip["matrix_bank_index"] = self.doc.add_bank(
            matrices, list(bank.get("colors") or [])
        )
        # add_bank may reuse an existing identical bank, so read the count back.
        fresh = self.doc.bank(clip["matrix_bank_index"])["matrices"]
        return len(fresh) - 1

    def intern_color(self, clip, color):
        """Append a colour transform to the clip's bank and return its index."""
        bank = self.doc.bank(clip["matrix_bank_index"])
        colors = list(bank.get("colors") or [])
        colors.append([int(v) for v in color])
        clip["matrix_bank_index"] = self.doc.add_bank(
            list(bank.get("matrices") or []), colors
        )
        fresh = self.doc.bank(clip["matrix_bank_index"])["colors"]
        return len(fresh) - 1

    def assign_matrix(self, clip, element, matrix):
        """Compact the clip's bank and point this element at ``matrix``.

        Runs at the end of an edit, so each frame element is remapped once even
        when a tween added many matrices.
        """
        usedm = sorted(
            {e[1] for f in clip["frames"] for e in f["elements"] if e[1] != 65535}
        )
        usedc = sorted(
            {e[2] for f in clip["frames"] for e in f["elements"] if e[2] != 65535}
        )
        bank = (
            self.doc.bank(clip["matrix_bank_index"])
            if usedm or usedc
            else {"matrices": [], "colors": []}
        )
        matrices = [bank["matrices"][i] for i in usedm]
        colors = [bank["colors"][i] for i in usedc]
        mi = {v: i for i, v in enumerate(usedm)}
        ci = {v: i for i, v in enumerate(usedc)}
        for f in clip["frames"]:
            for e in f["elements"]:
                e[1] = mi.get(e[1], 65535)
                e[2] = ci.get(e[2], 65535)
        element[1] = len(matrices)
        matrices.append(matrix)
        clip["matrix_bank_index"] = self.doc.add_bank(matrices, colors)
        self.doc.update("movieclips", clip)

    def resize_placement(self):
        import math

        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        if not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        fi = self.frame.value()
        if not clip["frames"] or not clip["frames"][fi]["elements"]:
            self.error("Import an image or add a placement first.")
            return
        row = self.elements.currentRow()
        if row < 0:
            row = len(clip["frames"][fi]["elements"]) - 1
        element = clip["frames"][fi]["elements"][row]
        child_id = clip["children"][element[0]]["id"]
        item = next(
            (
                item
                for item, _ in self.preview.instances.values()
                if item.data(0) == row and item.isVisible()
            ),
            None,
        )
        if item is None:
            self.error("Show the child in the preview before resizing it.")
            return
        bounds = item.boundingRect()
        bw, bh = bounds.width(), bounds.height()
        if bw <= 0 or bh <= 0:
            self.error("This shape has zero width or height.")
            return
        matrix = (
            self.doc.bank(clip["matrix_bank_index"])["matrices"][element[1]]
            if element[1] != 65535
            else [1, 0, 0, 1, 0, 0]
        )
        a, b, c, d, x, y = matrix
        values = self.size_dialog(
            "Placement — size and position",
            bw * math.hypot(a, b),
            bh * math.hypot(c, d),
            x,
            y,
            math.degrees(math.atan2(b, a)),
        )
        if values is None:
            return
        w, h, x, y, angle = values
        # Preserve skew and reflection while scaling each basis vector; apply rotation delta.
        delta = math.radians(angle) - math.atan2(b, a)
        cs, sn = math.cos(delta), math.sin(delta)
        sx, sy = w / (bw * max(math.hypot(a, b), 1e-9)), h / (
            bh * max(math.hypot(c, d), 1e-9)
        )
        matrix = [
            (a * cs - b * sn) * sx,
            (a * sn + b * cs) * sx,
            (c * cs - d * sn) * sy,
            (c * sn + d * cs) * sy,
            x,
            y,
        ]
        if self.mutate(lambda: self.assign_matrix(clip, element, matrix)):
            self.refresh()
            self.frames.selectRow(fi)
            self.elements.selectRow(row)

    def edit_transform(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        row = self.elements.currentRow()
        fi = self.frame_index()
        if row < 0:
            return
        try:
            obj = self.doc.resource(self.selected)
            e = obj["frames"][fi]["elements"][row]
            old = (
                self.doc.bank(obj["matrix_bank_index"])
                if any(
                    x[1] != 65535 or x[2] != 65535
                    for f in obj["frames"]
                    for x in f["elements"]
                )
                else {"matrices": [], "colors": []}
            )
            matrix = old["matrices"][e[1]] if e[1] != 65535 else [1, 0, 0, 1, 0, 0]
            color = (
                old["colors"][e[2]] if e[2] != 65535 else [255, 255, 255, 255, 0, 0, 0]
            )
            dialog = QDialog(self)
            dialog.setWindowTitle("Placement transform and color")
            form = QFormLayout(dialog)
            widgets = []
            for name, value in zip(
                MKEYS
                + [
                    "Red multiply",
                    "Green multiply",
                    "Blue multiply",
                    "Alpha",
                    "Red add",
                    "Green add",
                    "Blue add",
                ],
                matrix + color,
            ):
                w = QDoubleSpinBox() if len(widgets) < 6 else QSpinBox()
                w.setRange(-1e6, 1e6) if len(widgets) < 6 else w.setRange(0, 255)
                if isinstance(w, QDoubleSpinBox):
                    w.setDecimals(6)
                w.setValue(value)
                form.addRow(name, w)
                widgets.append(w)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            # Compact the clip's bank, making the selected placement independent.
            usedm = sorted(
                {x[1] for f in obj["frames"] for x in f["elements"] if x[1] != 65535}
            )
            usedc = sorted(
                {x[2] for f in obj["frames"] for x in f["elements"] if x[2] != 65535}
            )
            mi = {v: i for i, v in enumerate(usedm)}
            ci = {v: i for i, v in enumerate(usedc)}
            matrices = [old["matrices"][i] for i in usedm]
            colors = [old["colors"][i] for i in usedc]
            for f in obj["frames"]:
                for x in f["elements"]:
                    x[1] = mi.get(x[1], 65535)
                    x[2] = ci.get(x[2], 65535)
            e[1] = len(matrices)
            e[2] = len(colors)
            matrices.append([w.value() for w in widgets[:6]])
            colors.append([w.value() for w in widgets[6:]])

            def change():
                obj["matrix_bank_index"] = self.doc.add_bank(matrices, colors)
                self.doc.update("movieclips", obj)

            if self.mutate(change):
                self.refresh()
                self.frames.selectRow(fi)
                self.frame.setValue(fi)
        except Exception as ex:
            self.error(ex)

    def new_resource(self, kind=None, place=False):
        if isinstance(kind, bool):
            kind = None  # QPushButton.clicked carries its checked state.
        if not self.resolve_drafts():
            return
        if kind is None:
            kind, ok = QInputDialog.getItem(
                self, "New resource", "Type:", list(KINDS), 0, False
            )
            if not ok:
                return
        if place and (self.selected not in self.doc.index or self.doc.index[self.selected][0] != "movieclips"):
            self.error("Select a movieclip to add a child.")
            return
        created = []
        def change():
            created.append(self.doc.new_resource(kind))
            if place:
                self.place_child(created[0], f"{kind[:-1]}_{created[0]}")
        if self.mutate(change):
            if place:
                self.refresh()
                self.set_panel(5)
                self.children.selectRow(self.children.rowCount() - 1)
            else:
                self.go_to(created[0])
                self.set_panel(4 if kind == "shapes" else 0)

    def create_image_asset(self):
        if self.selected in self.doc.index and self.doc.index[self.selected][0] == "movieclips":
            self.import_image()
            return
        if not self.resolve_drafts():
            return
        path, _ = native_picker(self, "Import image as shape", "",
                                "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)
            created = []
            def change():
                atlas = len(self.doc.tables["Textures"]["textures"])
                self.doc.tables["Textures"]["textures"].append({"highres": data, "lowres": None})
                self.doc.dirty.add("Textures")
                id = self.doc.new_resource("shapes")
                created.append(id)
                w, h = data["width"], data["height"]
                self.doc.update("shapes", {"id": id, "exports": [], "commands": [{
                    "unk1": 0, "texture_index": atlas,
                    "vertices": [[0, 0, 0, 0], [w, 0, 65535, 0],
                                 [0, h, 0, 65535], [w, h, 65535, 65535]]}]})
            if self.mutate(change):
                self.go_to(created[0])
                self.set_panel(4)
        except Exception as error:
            self.error(error)

    def delete_resource(self):
        if self.selected is None:
            return
        if (
            QMessageBox.question(
                self, "Delete resource", f"Delete resource {self.selected}?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if self.mutate(lambda: self.doc.delete(self.selected)):
            self.selected = None
            self.preview.scene().clear()

    def show_vertices(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            return
        row = self.commands.currentRow()
        if not self.loading and "geometry" in self.drafts and row != self.geometry_row:
            target = row
            with QSignalBlocker(self.commands):
                self.commands.selectRow(self.geometry_row)
            if not self.resolve_drafts():
                return
            with QSignalBlocker(self.commands):
                self.commands.selectRow(target)
            row = target
        self.geometry_row = row
        obj = self.doc.resource(self.selected)
        fill_table(
            self.vertices,
            obj["commands"][row]["vertices"] if 0 <= row < len(obj["commands"]) else [],
        )

    def geometry_edit(self, fn):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            return
        obj = self.doc.resource(self.selected)
        try:
            fn(obj)
        except Exception as e:
            self.error(e)
            return
        if self.mutate(lambda: self.doc.update("shapes", obj)):
            self.refresh()

    def add_command(self):
        if not self.resolve_drafts():
            return
        if not self.doc.tables["Textures"]["textures"]:
            self.error("Add a texture atlas first.")
            return
        index, ok = QInputDialog.getInt(
            self,
            "Texture atlas",
            "Atlas index:",
            0,
            0,
            len(self.doc.tables["Textures"]["textures"]) - 1,
        )
        if ok:
            data = self.doc.tables["Textures"]["textures"][index]["highres"]
            w, h = data["width"], data["height"]
            self.geometry_edit(
                lambda o: o["commands"].append(
                    {
                        "unk1": 0,
                        "texture_index": index,
                        "vertices": [
                            [0, 0, 0, 0],
                            [w, 0, 65535, 0],
                            [0, h, 0, 65535],
                            [w, h, 65535, 65535],
                        ],
                    }
                )
            )

    def remove_command(self):
        if not self.resolve_drafts():
            return
        row = self.commands.currentRow()
        if row >= 0:
            self.geometry_edit(lambda o: o["commands"].pop(row))

    def add_vertex(self):
        row = self.vertices.rowCount()
        self.vertices.blockSignals(True)
        self.vertices.setRowCount(row + 1)
        for j in range(4):
            self.vertices.setItem(row, j, QTableWidgetItem("0"))
        self.vertices.blockSignals(False)
        self.mark_draft("geometry")

    def remove_vertex(self):
        row = self.vertices.currentRow()
        if row >= 0:
            self.vertices.removeRow(row)
            self.mark_draft("geometry")

    def apply_geometry(self):
        import math

        row = self.commands.currentRow()
        if row < 0:
            return

        def change(obj):
            for i, c in enumerate(obj["commands"]):
                c["texture_index"] = int(self.commands.item(i, 0).text())
                c["unk1"] = int(self.commands.item(i, 1).text())
            values = [
                [
                    (
                        float(self.vertices.item(i, j).text())
                        if j < 2
                        else int(self.vertices.item(i, j).text())
                    )
                    for j in range(4)
                ]
                for i in range(self.vertices.rowCount())
            ]
            if any(not math.isfinite(v) for row in values for v in row):
                raise ValueError("Coordinates must be finite")
            obj["commands"][row]["vertices"] = values

        self.geometry_edit(change)
        if row < self.commands.rowCount():
            self.commands.selectRow(row)

    def shape_from_png(self):
        if not self.resolve_drafts():
            return
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            return
        path, _ = native_picker(
            self, "Create sprite shape", "", "Images (*.png *.jpg *.webp)"
        )
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)
            w, h = data["width"], data["height"]
            obj = self.doc.resource(self.selected)

            def change():
                atlas = len(self.doc.tables["Textures"]["textures"])
                self.doc.tables["Textures"]["textures"].append(
                    {"highres": data, "lowres": None}
                )
                self.doc.dirty.add("Textures")
                obj["commands"].append(
                    {
                        "unk1": 0,
                        "texture_index": atlas,
                        "vertices": [
                            [0, 0, 0, 0],
                            [w, 0, 65535, 0],
                            [0, h, 0, 65535],
                            [w, h, 65535, 65535],
                        ],
                    }
                )
                self.doc.update("shapes", obj)

            if self.mutate(change):
                self.refresh()
        except Exception as e:
            self.error(e)

    def show_texture(self):
        self.play.setEnabled(False)
        self.frame.setEnabled(False)
        self.preview_resource = None
        t = self.doc.tables["Textures"]["textures"][self.texture_index]
        d = t["highres"]
        self.tabs.setCurrentIndex(3)
        self.preview.scene().clear()
        self.breadcrumb.setText(f"Texture atlas #{self.texture_index}")
        self.texture_info.setText(
            f'Atlas {self.texture_index}\n{d["width"]} × {d["height"]}\nPixel type {d["pixel_type"]} · Format flags {d["texture_format"]}\nExternal: {d.get("external_texture") or "embedded"}\nLowres: {"yes" if t.get("lowres") else "no"}'
        )
        try:
            self.preview.show_atlas(self.doc, self.texture_index)
            marks = len(self.preview.atlas_marks)
            self.notice.setText(
                f"{len(self.preview.atlas_regions):,} referenced regions"
                + (f" · {marks:,} zero-area markers (diamonds)" if marks else "")
                + " · Double-click to open a shape, right-click for usages"
            )
            index = self.texture_index
            QTimer.singleShot(
                0,
                lambda: (
                    self.preview.fit_content()
                    if self.texture_index == index and self.selected is None
                    else None
                ),
            )
        except Exception as e:
            self.error(e)

    def export_texture(self):
        if self.texture_index is None:
            return
        path, _ = save_picker(self, "Export atlas", "", "PNG (*.png)")
        if path:
            try:
                textures.image(
                    self.doc.tables["Textures"]["textures"][self.texture_index][
                        "highres"
                    ],
                    self.doc.path.parent if self.doc.path else ".",
                ).save(path)
            except Exception as e:
                self.error(e)

    def replace_texture(self):
        if self.texture_index is None:
            return
        path, _ = native_picker(
            self, "Replace highres atlas", "", "Images (*.png *.jpg *.webp *.bmp)"
        )
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)

            def change():
                self.doc.tables["Textures"]["textures"][self.texture_index] = {
                    "highres": data,
                    "lowres": None,
                }
                self.doc.dirty.add("Textures")

            if self.mutate(change):
                self.show_texture()
        except Exception as e:
            self.error(e)

    def add_texture(self):
        path, _ = native_picker(
            self, "Add atlas", "", "Images (*.png *.jpg *.webp *.bmp)"
        )
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)

            def change():
                self.doc.tables["Textures"]["textures"].append(
                    {"highres": data, "lowres": None}
                )
                self.doc.dirty.add("Textures")

            self.mutate(change)
        except Exception as e:
            self.error(e)

    def import_ktx(self):
        path, _ = native_picker(self, "Import encoded atlas", "", "KTX (*.ktx *.zktx)")
        if not path:
            return
        try:
            import struct, zstandard

            raw = Path(path).read_bytes()
            if raw.startswith(b"\x28\xb5\x2f\xfd"):
                raw = zstandard.ZstdDecompressor().decompress(raw)
            if raw[:12] != b"\xabKTX 11\xbb\r\n\x1a\n":
                raise ValueError("Expected KTX1")
            w, h = struct.unpack_from("<II", raw, 36)
            data = {
                "texture_format": 8,
                "pixel_type": 0,
                "width": w,
                "height": h,
                "data": raw,
                "external_texture": None,
            }

            def change():
                self.doc.tables["Textures"]["textures"].append(
                    {"highres": data, "lowres": None}
                )
                self.doc.dirty.add("Textures")

            self.mutate(change)
        except Exception as e:
            self.error(e)


def typing_into(widget):
    """True when an unmodified keystroke should go to the focused widget.

    A one-letter shortcut installed on a QAction is consumed even while the user
    is typing, so plain keys are routed by hand and these are the widgets that
    keep them: text edits, spin boxes, combo boxes and in-place table editors.
    A platform focus policy cannot stand in here -- PyQt6 reports StrongFocus
    for text fields and trees alike, and has no TextEditFocus flag.
    """
    while widget is not None:
        if isinstance(
            widget,
            (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return True
        widget = widget.parentWidget()
    return False


class Window(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or QSettings("SCStudio", "SCStudio")
        self.setWindowTitle("SC Studio")
        self.resize(1180, 760)
        self.clipboard = None
        self.jobs = []
        self.actions = {}
        self.agent_gateway = None
        self.setAcceptDrops(True)
        app = QApplication.instance()
        if not hasattr(app, "_sc_system_palette"):
            app._sc_system_palette = QPalette(app.palette())
        self.system_palette = app._sc_system_palette
        app.installEventFilter(self)
        app.styleHints().colorSchemeChanged.connect(self.system_theme_changed)
        self.editors = QTabWidget()
        self.editors.setDocumentMode(True)
        self.editors.setTabsClosable(True)
        self.editors.setMovable(True)
        self.editors.tabCloseRequested.connect(self.close_tab)
        self.editors.currentChanged.connect(self.active_changed)
        self.setCentralWidget(self.editors)
        file = self.menuBar().addMenu("&File")
        edit = self.menuBar().addMenu("&Edit")
        view = self.menuBar().addMenu("&View")
        resource = self.menuBar().addMenu("&Resource")
        create_menu = self.menuBar().addMenu("&Create")
        self.action(create_menu, "create_image", "Import image…", self.create_image)
        create_menu.addSeparator()
        for kind, label in (("movieclips", "Movieclip"), ("textfields", "Textfield"),
                            ("shapes", "Shape"), ("modifiers", "Mask modifier")):
            self.action(create_menu, f"create_{kind}", label,
                        lambda value=kind: self.create_asset(value))
        create_menu.addSeparator()
        add_menu = create_menu.addMenu("Add to current movieclip")
        for kind, label in (("textfields", "Textfield"), ("movieclips", "Movieclip"), ("shapes", "Shape")):
            self.action(add_menu, f"create_child_{kind}", label,
                        lambda value=kind: self.create_asset(value, place=True))
        self.action(add_menu, "create_child_image", "Import image…",
                    lambda: self.current() and self.current().import_image())
        self.action(add_menu, "create_child_existing", "Existing resource…",
                    lambda: self.current() and self.current().add_child())
        def update_create_menu():
            ed = self.current()
            add_menu.setEnabled(bool(ed and ed.selected in ed.doc.index
                                    and ed.doc.index[ed.selected][0] == "movieclips"))
        create_menu.aboutToShow.connect(update_create_menu)
        mcp_menu = self.menuBar().addMenu("&MCP")
        self.action(mcp_menu, "mcp_start", "Start agent access", self.start_mcp)
        self.action(mcp_menu, "mcp_stop", "Stop agent access", self.stop_mcp)
        self.action(mcp_menu, "mcp_config", "Copy client configuration", self.copy_mcp_config)
        self.action(mcp_menu, "mcp_status", "Connection status…", self.mcp_status)
        help_menu = self.menuBar().addMenu("&Help")
        self.action(
            resource,
            "import_image",
            "Import image into movieclip…",
            lambda: self.current() and self.current().import_image(),
            "Ctrl+Alt+I",
        )
        self.action(file, "new", "&New", self.new, "Ctrl+N")
        self.action(file, "open", "&Open…", self.open, "Ctrl+O")
        self.recent_menu = file.addMenu("Open recent")
        self.update_recent_menu()
        file.addSeparator()
        self.action(file, "save", "&Save", self.save, "Ctrl+S")
        self.action(
            file, "save_as", "Save &as…", lambda: self.save(True), "Ctrl+Shift+S"
        )
        self.action(file, "save_all", "Save all", self.save_all, "Ctrl+Alt+S")
        self.action(
            file,
            "export",
            "Export selected with dependencies…",
            self.export_selected,
            "Ctrl+Shift+E",
        )
        file.addSeparator()
        self.action(
            file,
            "close",
            "&Close tab",
            lambda: self.close_tab(self.editors.currentIndex()),
            "Ctrl+W",
        )
        self.action(file, "quit", "&Quit", self.close, "Ctrl+Q")
        self.action(edit, "undo", "&Undo", lambda: self.route("undo"), "Ctrl+Z")
        redo = self.action(
            edit, "redo", "&Redo", lambda: self.route("redo"), "Ctrl+Shift+Z"
        )
        redo.setShortcuts(["Ctrl+Shift+Z", "Ctrl+Y"])
        edit.addSeparator()
        self.action(edit, "copy", "&Copy", lambda: self.route("copy"), "Ctrl+C")
        self.action(edit, "paste", "&Paste", lambda: self.route("paste"), "Ctrl+V")
        self.action(
            edit, "select_all", "Select all", lambda: self.route("selectAll"), "Ctrl+A"
        )
        edit.addSeparator()
        self.action(edit, "find", "&Find resource", self.focus_search, "Ctrl+F")
        self.action(edit, "rename", "Rename…", self.rename_selection, "F2")
        self.action(edit, "goto", "&Go to ID…", self.go_to_id, "Ctrl+G")
        self.actions["goto"].setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.action(edit, "find_json", "Find in JSON…", self.find_json, "Ctrl+Shift+F")
        self.action(edit, "next_match", "Next JSON match", self.find_next, "F3")
        self.action(view, "back", "Previous resource", self.navigate_back, "Alt+Left")
        self.action(view, "next_tab", "Next tab", lambda: self.cycle_tab(1), "Ctrl+Tab")
        self.action(
            view,
            "prev_tab",
            "Previous tab",
            lambda: self.cycle_tab(-1),
            "Ctrl+Shift+Tab",
        )
        view.addSeparator()
        self.action(
            view,
            "fit",
            "Fit preview",
            lambda: self.current() and self.current().render_frame(fit=True),
            "F",
        )
        self.actions["fit"].setShortcut("Ctrl+0")
        self.action(
            view,
            "play",
            "Play / pause",
            lambda: self.current() and self.current().toggle_play(),
            "Ctrl+Space",
        )
        self.action(
            view, "browser", "Show resource browser", self.toggle_browser, "Ctrl+B"
        ).setCheckable(True)
        self.actions["browser"].setChecked(True)
        self.action(
            view, "inspector", "Show inspector", self.toggle_inspector, "Ctrl+I"
        ).setCheckable(True)
        self.actions["inspector"].setChecked(True)
        self.action(view, "reset_layout", "Reset layout", self.reset_layout)
        panels = view.addMenu("Inspector")
        for index, name in enumerate(
            ("Properties", "Timeline", "JSON", "Textures", "Geometry", "Children")
        ):
            self.action(
                panels,
                f"panel{index}",
                name,
                lambda i=index: self.current() and self.current().set_panel(i),
                f"Ctrl+{index+1}",
            )
        themes = view.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for label, mode in [("System", "system"), ("Light", "light"), ("Dark", "dark")]:
            action = QAction(label, self)
            action.setCheckable(True)
            self.theme_group.addAction(action)
            themes.addAction(action)
            action.triggered.connect(lambda checked, value=mode: self.set_theme(value))
            self.theme_actions[mode] = action
        themes.addSeparator()
        # Named schemes from sc_studio.themes, grouped so a long list stays
        # navigable. Each is a plain palette swap, so switching is instant.
        for group, entries in (
            (
                "Dark schemes",
                [n for n in theme_schemes.names() if theme_schemes.is_dark(n)],
            ),
            (
                "Light schemes",
                [n for n in theme_schemes.names() if not theme_schemes.is_dark(n)],
            ),
        ):
            submenu = themes.addMenu(group)
            for name in entries:
                action = QAction(name, self)
                action.setCheckable(True)
                self.theme_group.addAction(action)
                submenu.addAction(action)
                action.triggered.connect(
                    lambda checked, value=name: self.set_theme(value)
                )
                self.theme_actions[name] = action
        self.set_theme(str(self.settings.value("theme", "system")))
        self.action(
            resource,
            "create",
            "New resource…",
            lambda: self.current() and self.current().new_resource(),
            "Ctrl+Shift+N",
        )
        self.action(
            resource, "copy_graph", "Copy with dependencies", self.copy, "Ctrl+Shift+C"
        )
        self.action(
            resource, "paste_graph", "Paste resource graph", self.paste, "Ctrl+Shift+V"
        )
        self.action(
            resource, "duplicate", "Duplicate selected", self.duplicate, "Ctrl+D"
        )
        self.action(resource, "duplicate_graph", "Duplicate with dependencies…", self.duplicate_graph)
        self.action(resource, "delete", "Delete selected", self.delete_selection)
        self.action(
            resource,
            "import",
            "Import from opened file…",
            self.import_from,
            "Ctrl+Shift+I",
        )
        resource.addSeparator()
        self.action(
            resource, "references", "Find usages…", self.references, "Shift+F12"
        )
        self.action(resource, "reference_tree", "Reference tree…",
                    lambda: self.references(tree_mode=True), "Ctrl+Shift+R")
        self.action(
            resource, "follow", "Follow child reference", self.follow_reference, "F12"
        )
        self.action(resource, "validate", "Validate references", self.validate, "F5")
        self.action(
            resource,
            "apply",
            "Apply inspector edits",
            self.apply_current,
            "Ctrl+Return",
        )
        self.action(help_menu, "shortcuts", "Keyboard shortcuts", self.shortcuts, "F1")
        self.action(help_menu, "performance", "Performance…", self.performance)
        self.action(help_menu, "about", "Support and limitations", self.about)
        # Delete is confined to the resource browser, not text or table cell editing.
        self.delete_shortcut = QAction(self)
        self.delete_shortcut.setShortcut("Delete")
        self.delete_shortcut.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.delete_shortcut.triggered.connect(self.delete_selection)
        self.info = QLabel()
        self.statusBar().addPermanentWidget(self.info)
        self.statusBar().showMessage("Ready · Ctrl+O to open · Ctrl+F to find")
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def action(self, parent, key, title, fn, shortcut=None):
        action = QAction(title, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda checked=False: fn() if not self.jobs else None)
        parent.addAction(action)
        self.actions[key] = action
        return action

    def current(self):
        return self.editors.currentWidget()

    def create_asset(self, kind, place=False):
        if self.current() is None:
            self.new()
        self.current().new_resource(kind, place=place)

    def create_image(self):
        if self.current() is None:
            self.new()
        self.current().create_image_asset()

    def rename_selection(self):
        editor = self.current()
        if not editor or not editor.resolve_drafts():
            return
        focus = QApplication.focusWidget()
        if focus is editor.children or editor.children.isAncestorOf(focus):
            row = editor.children.currentRow()
            if row >= 0:
                editor.children.setCurrentCell(row, 1)
                editor.children.editItem(editor.children.item(row, 1))
            return
        if editor.selected not in editor.doc.index:
            return
        obj = editor.doc.resource(editor.selected)
        names = obj.get("exports", [])
        name, ok = QInputDialog.getText(
            self,
            "Rename asset",
            "Export name (other aliases are kept):",
            text=names[0] if names else "",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if any(
            name in values and id != editor.selected
            for id, values in editor.doc.names.items()
        ):
            editor.error("That export name is already used by another asset.")
            return
        obj["exports"] = [name] + [v for v in names[1:] if v != name]
        kind = editor.doc.index[editor.selected][0]
        if editor.mutate(lambda: editor.doc.update(kind, obj)):
            editor.refresh()

    def system_theme_changed(self, scheme):
        if getattr(self, "theme", "system") == "system":
            configure_theme(
                QApplication.instance(), "system", self.system_palette, scheme
            )

    def set_theme(self, mode):
        if mode not in ("system", "light", "dark") and mode not in theme_schemes.SCHEMES:
            mode = "system"
        configure_theme(QApplication.instance(), mode, self.system_palette)
        self.theme_actions[mode].setChecked(True)
        self.settings.setValue("theme", mode)
        self.theme = mode

    def title(self, editor):
        i = self.editors.indexOf(editor)
        if i >= 0:
            name = editor.doc.path.name if editor.doc.path else "Untitled"
            self.editors.setTabText(i, name + (" *" if editor.is_dirty() else ""))
            self.editors.setTabToolTip(i, str(editor.doc.path or "Unsaved document"))
        self.active_changed()

    def active_changed(self, *args):
        ed = self.current()
        if not hasattr(self, "info"):
            return
        if ed is None:
            self.setWindowTitle("SC Studio")
            self.info.setText("")
            return
        name = ed.doc.path.name if ed.doc.path else "Untitled"
        self.setWindowTitle(f'{name}{" *" if ed.is_dirty() else ""} — SC Studio')
        self.info.setText(f"SC{ed.doc.version} · {len(ed.doc.index):,} objects")
        self.actions["browser"].setChecked(not ed.splitter.widget(0).isHidden())
        self.actions["inspector"].setChecked(not ed.splitter.widget(2).isHidden())
        for grid in (ed.tree,):
            if self.delete_shortcut not in grid.actions():
                grid.addAction(self.delete_shortcut)

    def add_doc(self, doc):
        editor = Editor(doc)
        editor.changed.connect(lambda: self.title(editor))
        self.editors.addTab(editor, "")
        self.editors.setCurrentWidget(editor)
        editor.tree.setFocus()
        self.title(editor)
        sizes = self.settings.value("splitter", [265, 565, 350])
        try:
            editor.splitter.setSizes([int(x) for x in sizes])
        except (TypeError, ValueError):
            pass
        if doc.path:
            self.remember_path(doc.path)

    def new(self):
        self.add_doc(Document())

    def remember_path(self, path):
        path = str(Path(path).resolve())
        recent = self.recent_paths()
        recent = [path] + [value for value in recent if value != path]
        self.settings.setValue("recent", recent[:10])
        self.settings.setValue("directory", str(Path(path).parent))
        self.update_recent_menu()

    def recent_paths(self):
        value = self.settings.value("recent", [])
        return [value] if isinstance(value, str) else list(value or [])

    def update_recent_menu(self):
        self.recent_menu.clear()
        for path in self.recent_paths():
            action = self.recent_menu.addAction(Path(path).name)
            action.setToolTip(path)
            action.triggered.connect(lambda checked=False, value=path: self.open(value))
        self.recent_menu.setEnabled(bool(self.recent_paths()))

    def background(self, title, fn, done):
        if self.jobs:
            return
        for i in range(self.editors.count()):
            ed = self.editors.widget(i)
            ed.timer.stop()
            ed.play.setText("▶ Play")
        progress = QProgressDialog(title, "", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        self.editors.setEnabled(False)
        self.menuBar().setEnabled(False)
        task = Task(fn)
        self.jobs.append(task)

        def finish_ui():
            progress.close()
            progress.deleteLater()
            self.activateWindow()
            self.jobs.remove(task)
            self.editors.setEnabled(True)
            self.menuBar().setEnabled(True)

        def finish(value):
            finish_ui()
            try:
                done(value)
            except Exception as e:
                QMessageBox.critical(self, "SC Studio", str(e))

        def failure(message):
            finish_ui()
            QMessageBox.critical(self, "SC Studio", message)

        task.signals.done.connect(finish)
        task.signals.failed.connect(failure)
        QThreadPool.globalInstance().start(task)

    def open(self, path=None):
        if self.jobs:
            return
        if path is None:
            path, _ = native_picker(
                self,
                "Open",
                self.settings.value("directory", ""),
                "Supercell files (*.sc);;All files (*)",
            )
        if not path:
            return
        canonical = Path(path).resolve()
        for i in range(self.editors.count()):
            if self.editors.widget(i).doc.path == canonical:
                self.editors.setCurrentIndex(i)
                return
        self.background(
            "Reading SC file…", lambda: Document.load(canonical), self.add_doc
        )

    def save(self, as_new=False, editor=None, after=None):
        ed = editor or self.current()
        if ed is None or not ed.resolve_drafts():
            return False
        path = ed.doc.path
        if as_new or path is None:
            path, _ = save_picker(
                self,
                "Save",
                str(
                    path
                    or Path(str(self.settings.value("directory", ""))) / "untitled.sc"
                ),
                "Supercell files (*.sc)",
            )
        if not path:
            return False
        if not str(path).lower().endswith(".sc"):
            path = str(path) + ".sc"

        def finished(_):
            ed.mark_saved()
            self.title(ed)
            self.remember_path(ed.doc.path)
            self.statusBar().showMessage(f"Saved {ed.doc.path.name}", 6000)
            if after:
                after()

        self.background("Validating and saving…", lambda: ed.doc.save(path), finished)
        return True

    def save_all(self):
        pending = [
            self.editors.widget(i)
            for i in range(self.editors.count())
            if self.editors.widget(i).is_dirty()
            or self.editors.widget(i).doc.path is None
        ]

        def next_file():
            if pending:
                self.save(editor=pending.pop(0), after=next_file)

        next_file()

    def copy(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        ids = ed.selected_ids()
        if not ids:
            return

        def ready(value):
            doc, roots, payload = value
            self.clipboard = (doc, roots)
            set_graph_clipboard(doc, roots, payload)
            self.statusBar().showMessage(
                f"Copied {len(roots)} selected resources with dependencies", 8000
            )

        self.background(
            "Copying resource graph…", lambda: clipboard_pack(ed.doc, ids), ready
        )

    def import_graph(self, source_fn, label="Importing resources…"):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        before = ed.snapshot()

        def build():
            source, ids = source_fn()
            if isinstance(ids, int):
                ids = [ids]
            try:
                mapping = ed.doc.import_resources(source, ids)
                errors = ed.doc.validate()
                if errors:
                    raise ValueError("\n".join(errors[:20]))
                return mapping, ids
            except Exception:
                ed.doc.__dict__ = self.restored_state(before[0])
                ed.doc._reindex()
                raise

        def ready(value):
            mapping, ids = value
            ed.commit_mutation(before)
            ed.go_to(mapping[ids[0]])
            self.statusBar().showMessage(f"Imported {len(mapping)} resources", 8000)

        self.background(label, build, ready)

    def paste(self):
        mime = QApplication.clipboard().mimeData()
        payload = (
            bytes(mime.data(CLIP_MIME)) if mime and mime.hasFormat(CLIP_MIME) else None
        )
        if payload is None:
            self.statusBar().showMessage(
                "Clipboard does not contain SC resources", 5000
            )
            return
        self.import_graph(
            lambda: clipboard_unpack(payload),
            "Importing clipboard graph…",
        )

    def duplicate(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        ids = ed.selected_ids()
        if ids:
            mapping = {}
            if ed.mutate(lambda: mapping.update(ed.doc.duplicate_resources(ids))):
                ed.go_to(mapping[ids[0]])
                self.statusBar().showMessage(f"Duplicated {len(mapping)} resource(s); child assets are shared", 5000)

    def duplicate_graph(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        ids = ed.selected_ids()
        if ids:
            self.import_graph(lambda: (ed.doc, ids), "Duplicating resource graph…")

    def delete_selection(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        ids = ed.selected_ids()
        if not ids:
            return
        if (
            QMessageBox.question(
                self,
                "Delete resources",
                f"Delete {len(ids)} selected resource(s)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if ed.mutate(
            lambda: ed.doc.delete_many(ids), full_validate=True
        ):
            ed.selected = None
            ed.refresh()
            ed.changed.emit()

    def route(self, command):
        focus = QApplication.focusWidget()
        ed = self.current()
        if isinstance(focus, (QLineEdit, QPlainTextEdit, QTextEdit)):
            method = getattr(focus, command, None)
            if method:
                method()
            return
        if ed is None:
            return
        if isinstance(focus, QTableWidget):
            if command == "copy":
                ranges = focus.selectedRanges()
                if ranges:
                    area = ranges[0]
                    rows = []
                    for r in range(area.topRow(), area.bottomRow() + 1):
                        rows.append(
                            "\t".join(
                                focus.item(r, c).text() if focus.item(r, c) else ""
                                for c in range(
                                    area.leftColumn(), area.rightColumn() + 1
                                )
                            )
                        )
                    QApplication.clipboard().setText("\n".join(rows))
            elif command == "paste":
                self.paste_cells(focus)
            elif command == "selectAll":
                focus.selectAll()
            elif command == "undo":
                ed.undo()
            elif command == "redo":
                ed.redo()
            return
        if command == "copy":
            self.copy()
        elif command == "paste":
            mime = QApplication.clipboard().mimeData()
            if mime and mime.hasFormat(CLIP_MIME):
                self.paste()
            else:
                self.statusBar().showMessage(
                    "Copy an SC resource graph to paste here", 4000
                )
        elif command == "undo":
            ed.undo()
        elif command == "redo":
            ed.redo()
        elif command == "selectAll":
            ed.tree.selectAll()

    def paste_cells(self, grid):
        ed = self.current()
        text = QApplication.clipboard().text()
        row, col = grid.currentRow(), grid.currentColumn()
        if row < 0 or col < 0:
            return
        rows = [line.split("\t") for line in text.splitlines()]
        edits = []
        for y, values in enumerate(rows):
            for x, value in enumerate(values):
                r, c = row + y, col + x
                item = (
                    grid.item(r, c)
                    if r < grid.rowCount() and c < grid.columnCount()
                    else None
                )
                if item is None or not item.flags() & Qt.ItemFlag.ItemIsEditable:
                    ed.error("Paste exceeds the editable cell range")
                    return
                edits.append((item, value, item.text()))
        with QSignalBlocker(grid):
            for item, value, _ in edits:
                item.setText(value)
        if grid in (ed.children, ed.frames, ed.elements):
            ed.timeline_changed()
        elif grid in (ed.commands, ed.vertices):
            ed.mark_draft("geometry")

    def focus_search(self):
        ed = self.current()
        if ed:
            ed.splitter.widget(0).show()
            ed.search.setFocus()
            ed.search.selectAll()
            self.active_changed()

    def eventFilter(self, watched, event):
        # Plain G opens Go to ID from anywhere except text entry, where the
        # letter belongs to whatever is being typed. Ctrl+G stays the ordinary
        # QAction shortcut, so this supplies the convenience key only. The event
        # receiver identifies the target more reliably than focusWidget(), which
        # reports nothing on the offscreen platform the tests run under.
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_G
            and not event.modifiers()
            and QApplication.activeModalWidget() is None
            and self.editors.count()
            and not typing_into(
                watched if isinstance(watched, QWidget) else QApplication.focusWidget()
            )
        ):
            self.go_to_id()
            return True
        return super().eventFilter(watched, event)

    def go_to_id(self):
        ed = self.current()
        if ed is None:
            return
        value, ok = QInputDialog.getInt(
            self, "Go to resource", "Resource ID:", ed.selected or 0, 0, 65535
        )
        if ok and not ed.go_to(value):
            self.statusBar().showMessage(f"Resource {value} does not exist", 5000)

    def navigate_back(self):
        ed = self.current()
        if ed and ed.navigation_index >= 0:
            target = ed.navigation_history[ed.navigation_index]
            if ed.go_to(target, remember=False):
                ed.navigation_index -= 1

    def cycle_tab(self, delta):
        if self.editors.count():
            self.editors.setCurrentIndex(
                (self.editors.currentIndex() + delta) % self.editors.count()
            )

    def toggle_browser(self):
        ed = self.current()
        if ed:
            ed.splitter.widget(0).setVisible(self.actions["browser"].isChecked())

    def toggle_inspector(self):
        ed = self.current()
        if ed:
            ed.splitter.widget(2).setVisible(self.actions["inspector"].isChecked())

    def reset_layout(self):
        ed = self.current()
        if ed:
            ed.splitter.widget(0).show()
            ed.splitter.widget(2).show()
            ed.splitter.setSizes([235, 570, 375])
            self.active_changed()

    def apply_current(self):
        ed = self.current()
        if not ed:
            return
        if ed.active_panel == 0:
            ed.apply_properties()
        elif ed.active_panel == 2:
            ed.apply_json()
        elif ed.active_panel == 4:
            ed.apply_geometry()

    def find_json(self):
        ed = self.current()
        if not ed:
            return
        term, ok = QInputDialog.getText(
            self, "Find in JSON", "Text:", text=getattr(self, "json_term", "")
        )
        if ok and term:
            self.json_term = term
            ed.set_panel(2)
            ed.code.setFocus()
            self.find_next()

    def find_next(self):
        ed = self.current()
        term = getattr(self, "json_term", "")
        if ed and term:
            if not ed.code.find(term):
                cursor = ed.code.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                ed.code.setTextCursor(cursor)
                ed.code.find(term)

    def follow_reference(self):
        ed = self.current()
        if not ed or ed.selected not in ed.doc.index:
            return
        focus = QApplication.focusWidget()
        target = None
        if focus is ed.children and ed.children.currentRow() >= 0:
            target = int(ed.children.item(ed.children.currentRow(), 0).text())
        elif focus is ed.elements and ed.elements.currentRow() >= 0:
            index = int(ed.elements.item(ed.elements.currentRow(), 0).text())
            target = ed.doc.resource(ed.selected)["children"][index]["id"]
        if target is not None:
            ed.go_to(target)

    def references(self, target=None, tree_mode=False):
        ed = self.current()
        if ed is None:
            return
        target = ed.selected if target is None else target
        if target not in ed.doc.index:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Find usages — #{target}")
        dialog.resize(720, 420)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Filter movieclip, instance name or ID…")
        layout.addWidget(search)
        recursive = QCheckBox("Include indirect parents")
        recursive.setChecked(True)
        layout.addWidget(recursive)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Movieclip", "ID", "Instance", "Child index", "Relation"])
        tree.setObjectName("usages_list")
        reference_tabs = QTabWidget()
        reference_tabs.addTab(tree, "Usages")
        from .references import ReferenceTree
        reference_tree = ReferenceTree(ed.doc, target)
        reference_tabs.addTab(reference_tree, "Reference tree")
        layout.addWidget(reference_tabs)
        count = QLabel()
        layout.addWidget(count)
        rows = ed.doc.usages(target, indirect=True)

        def populate():
            tree.clear()
            query = search.text().casefold()
            for row in rows:
                if row["depth"] > 1 and not recursive.isChecked():
                    continue
                label = (
                    " / ".join(ed.doc.names.get(row["id"], []))
                    or f"Movieclip {row['id']}"
                )
                relation = (
                    "Direct" if row["depth"] == 1 else f"Indirect via #{row['via']}"
                )
                values = [
                    label,
                    str(row["id"]),
                    row["name"],
                    str(row["child"]),
                    relation,
                ]
                if query and query not in " ".join(values).casefold():
                    continue
                item = QTreeWidgetItem(tree, values)
                item.setData(0, Qt.ItemDataRole.UserRole, (row["id"], row["child"]))
            count.setText(
                f"{tree.topLevelItemCount()} matching instances · Double-click to open their parent"
            )

        search.textChanged.connect(populate)
        recursive.toggled.connect(populate)
        populate()
        def reference_tab_changed(index):
            search.setEnabled(index == 0)
            recursive.setEnabled(index == 0)
            if index == 0:
                populate()
            else:
                count.setText("Expand each branch to follow parents to their roots · Double-click to open")
        reference_tabs.currentChanged.connect(reference_tab_changed)
        if tree_mode:
            reference_tabs.setCurrentIndex(1)

        def follow(item, *_):
            parent, child = item.data(0, Qt.ItemDataRole.UserRole)
            dialog.accept()
            if ed.go_to(parent):
                ed.set_panel(5)
                ed.children.selectRow(child)

        tree.itemDoubleClicked.connect(follow)
        def follow_reference(id):
            dialog.accept()
            ed.go_to(id)
        reference_tree.open_resource.connect(follow_reference)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def start_mcp(self):
        from .agent_gateway import AgentGateway
        if self.agent_gateway is None:
            self.agent_gateway = AgentGateway(self)
            try:
                self.agent_gateway.start()
            except Exception as error:
                self.agent_gateway = None
                QMessageBox.critical(self, "MCP", str(error))
                return
        self.statusBar().showMessage("MCP agent access is running", 5000)

    def stop_mcp(self):
        if self.agent_gateway is not None:
            self.agent_gateway.stop()
            self.agent_gateway = None
        self.statusBar().showMessage("MCP agent access stopped", 5000)

    def copy_mcp_config(self):
        self.start_mcp()
        if self.agent_gateway:
            QApplication.clipboard().setText(json.dumps(self.agent_gateway.client_config(), indent=2))
            self.statusBar().showMessage("MCP client configuration copied", 5000)

    def mcp_status(self):
        state = (f"Running at {self.agent_gateway.url}\n"
                 f"Connection file: {self.agent_gateway.connection_path}\n"
                 "Use Copy client configuration to connect your agent."
                 if self.agent_gateway else "Stopped. Start agent access to connect an MCP client.")
        QMessageBox.information(self, "MCP connection", state)

    def import_from(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        others = [
            self.editors.widget(i)
            for i in range(self.editors.count())
            if self.editors.widget(i) is not ed
        ]
        if not others:
            QMessageBox.information(
                self, "Import", "Open another SC file in a second tab first."
            )
            return
        labels = [
            f'{i+1}. {o.doc.path.name if o.doc.path else "Untitled"}'
            for i, o in enumerate(others)
        ]
        label, ok = QInputDialog.getItem(
            self, "Source SC", "Opened file:", labels, 0, False
        )
        if not ok:
            return
        source = others[labels.index(label)].doc
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose movieclip to import")
        dialog.resize(680, 600)
        v = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Search clip name or ID…")
        v.addWidget(search)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Movieclip", "ID"])
        v.addWidget(tree)

        def populate():
            tree.clear()
            q = search.text().casefold()
            for id, (kind, raw) in source.index.items():
                if kind != "movieclips":
                    continue
                name = " / ".join(source.names.get(id, [])) or f"Movieclip {id}"
                if q and q not in name.casefold() and q not in str(id):
                    continue
                item = QTreeWidgetItem(tree, [name, str(id)])
                item.setData(0, Qt.ItemDataRole.UserRole, id)

        search.textChanged.connect(populate)
        populate()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        v.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        tree.itemDoubleClicked.connect(lambda *_: dialog.accept())
        if dialog.exec() == QDialog.DialogCode.Accepted and tree.currentItem():
            id = tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
            self.import_graph(lambda: (source, [id]))

    def export_selected(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return
        ids = ed.selected_ids()
        if not ids:
            return
        path, _ = save_picker(
            self, "Export resources", "resources.sc", "Supercell files (*.sc)"
        )
        if path:

            def build():
                doc = Document()
                doc.import_resources(ed.doc, ids)
                doc.save(path)

            self.background(
                "Exporting resource graph…",
                build,
                lambda _: self.statusBar().showMessage("Exported resources", 6000),
            )

    def validate(self):
        ed = self.current()
        if ed is None or not ed.resolve_drafts():
            return

        def done(errors):
            QMessageBox.information(
                self,
                "Validation",
                (
                    "\n".join(errors[:50])
                    if errors
                    else f"Valid references · {len(ed.doc.index):,} resources"
                ),
            )

        self.background("Validating references…", ed.doc.validate, done)

    def shortcuts(self):
        rows = []
        for key, action in self.actions.items():
            keys = ", ".join(
                shortcut.toString(QKeySequence.SequenceFormat.NativeText)
                for shortcut in action.shortcuts()
            )
            # G is routed by the window event filter rather than by the action,
            # so it is listed here to keep the dialog matching real behaviour.
            if key == "goto":
                keys = ", ".join(filter(None, [keys, "G"]))
            if keys:
                rows.append((keys, action.text().replace("&", "")))
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard shortcuts")
        dialog.resize(560, 620)
        layout = QVBoxLayout(dialog)
        grid = table(["Shortcut", "Action"])
        fill_table(grid, rows)
        grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        grid.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(grid)
        layout.addWidget(
            QLabel(
                "Copy/paste/undo follow focus: text, table cells, or resource graph.\nDelete applies to resources only when the browser has focus."
            )
        )
        dialog.exec()

    def performance(self):
        """Report where editing and playback time goes on the open document."""
        editor = self.current()
        # Every stage gets a row whether or not it has run yet, so the dialog
        # looks the same on a fresh window as on a busy one.
        stats = editor.timings if editor is not None else {}
        rows = []
        for label in ("snapshot", "validate", "commit", "render"):
            samples = stats.get(label) or []
            if not samples:
                rows.append((label.title(), "not measured yet", "no samples"))
                continue
            ordered = sorted(samples)
            last = samples[-1] * 1000
            mean = sum(samples) / len(samples) * 1000
            worst = ordered[-1] * 1000
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000
            rows.append(
                (
                    label.title(),
                    f"{last:8.1f} ms",
                    f"mean {mean:7.1f} · p95 {p95:7.1f} · worst {worst:7.1f} ms "
                    f"({len(samples)} samples)",
                )
            )
        if editor is not None:
            document = editor.doc
            summary = (
                f"{len(document.index):,} resources · "
                f"{len(document.tables['Textures']['textures'])} atlases · "
                f"{editor.preview.items_count:,} preview objects this frame · "
                f"textures decoded into cache: {len(editor.preview.cache)}"
            )
        else:
            summary = "No document open."
        dialog = QDialog(self)
        dialog.setWindowTitle("Performance")
        dialog.resize(680, 380)
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "Rolling cost of the last 60 operations. snapshot and validate run "
            "once per edit, commit rebuilds the browser, and render runs for "
            "every animated frame."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        grid = table(["Stage", "Latest", "Distribution"])
        fill_table(grid, rows)
        grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        grid.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(grid)
        detail = QLabel(summary)
        detail.setWordWrap(True)
        layout.addWidget(detail)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def about(self):
        QMessageBox.information(
            self,
            "SC Studio",
            "SC Studio 0.5.1\nNew documents and exported resource graphs use SC6. Existing SC5/SC6 files retain their version.\n\nSC1 is unsupported. Rendering is an approximate preview; RGB effects, stencil masks, blend modes, engine text outlines are stored but not simulated. Common identity-child scaling grids are previewed.\nNative file dialogs use the platform implementation when available.\n\nF1 lists keyboard shortcuts.",
        )

    def close_tab(self, i, after=None):
        if self.jobs or i < 0 or i >= self.editors.count():
            return False
        ed = self.editors.widget(i)
        if not ed.resolve_drafts():
            return False

        def remove():
            index = self.editors.indexOf(ed)
            if index >= 0:
                self.editors.removeTab(index)
                ed.deleteLater()
            if after:
                after()

        if ed.is_dirty():
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                f'Save changes to {ed.doc.path.name if ed.doc.path else "Untitled"}?',
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save:
                self.save(editor=ed, after=remove)
                return False
        remove()
        return True

    def closeEvent(self, event):
        if self.jobs:
            event.ignore()
            return
        ed = self.current()
        if ed:
            self.settings.setValue("splitter", ed.splitter.sizes())
        self.settings.setValue("geometry", self.saveGeometry())
        while self.editors.count():
            if not self.close_tab(
                self.editors.count() - 1, after=lambda: QTimer.singleShot(0, self.close)
            ):
                event.ignore()
                return
        self.settings.sync()
        self.stop_mcp()
        event.accept()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() and url.toLocalFile().lower().endswith(".sc")
            for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".sc")
        ]
        self.open_queue(paths)
        event.acceptProposedAction()

    def open_queue(self, paths):
        paths = list(paths)

        def next_file():
            if not paths:
                return
            path = paths.pop(0)
            self.background(
                "Reading SC file…",
                lambda: Document.load(path),
                lambda doc: (self.add_doc(doc), next_file()),
            )

        next_file()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="SC Studio — SC2 v5/v6 editor")
    parser.add_argument("files", nargs="*", help="SC files to open")
    parser.add_argument("--mcp", action="store_true", help="Start local MCP agent access")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("SC Studio")
    app.setOrganizationName("SCStudio")
    window = Window()
    window.show()
    if args.files:
        window.open_queue(args.files)
    else:
        window.new()
    if args.mcp:
        window.start_mcp()
    sys.exit(app.exec())
