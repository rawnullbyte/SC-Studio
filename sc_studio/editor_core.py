"""The editor core: state, refresh, undo, and the shared helpers."""

import copy
import json
import math
import pickle
import time
from pathlib import Path
from collections import deque
from PyQt6.QtCore import (
    QEvent,
    QSignalBlocker,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidgetItem,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtGui import (
    QFontDatabase,
    QImage,
    QPainter,
)
from .document import CHUNKS, FrameElements, FrameSequence, KINDS, MKEYS
from .workspace import native_picker
from .preview import AnimatorTimeline, Preview
from .widgets import COLOR_KEYS, ColorSwatch, ResourceItem, fill_table, jsonable, save_picker, table
from . import easing, textures


# Late-bound so tests and callers can patch one place.
from . import app

class CorePanel:
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
        self.preview.placementSelected.connect(self.canvas_placement_selected)
        self.preview.placementMoved.connect(self.move_placement)
        self.preview.placementTransformed.connect(self.commit_placement_transform)
        self.preview.placementsMoved.connect(self.move_placements)
        self.preview.placementsTransformed.connect(self.transform_placements)
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
        from .frame_cursor import FrameCursor
        self.cursor = FrameCursor(self)
        self.frame = QSlider(Qt.Orientation.Horizontal)
        self.frame.setRange(0, 0)
        self.frame.valueChanged.connect(self.scrub_progress)
        self.frame.sliderPressed.connect(lambda: self.scrub_progress(self.frame.value()))
        # Frames the cursor may reach past the end of the clip. The far end of
        # the slider is a ratchet, not a fixed limit: working past it moves it
        # further out, so the timeline is not capped at some arbitrary length.
        self.FRAME_HEADROOM = 8
        self.frame_span = 0  # furthest frame the slider currently reaches
        self.cursor.setRange(0, 0)
        self.frame_label = QLabel("Frame 1 / 1")
        fit = QPushButton("Fit")
        # Dragging a placement lands on whole asset pixels by default. The saved
        # value is applied by the Window once this editor is parented; here it
        # just matches the preview's own default so the two never disagree.
        self.snap_pixels = QCheckBox("Snap to pixels")
        self.snap_pixels.setToolTip("Movement lands on whole asset pixels")
        self.snap_pixels.setChecked(self.preview.snap_movement)
        self.snap_pixels.toggled.connect(self.set_snap_pixels)
        controls.addWidget(self.play)
        controls.addWidget(self.frame, 1)
        controls.addWidget(self.frame_label)
        controls.addWidget(self.snap_pixels)
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
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Timeline zoom"))
        for label, fn in (("−", lambda: self.keyframes.set_zoom(self.keyframes.zoom / 1.5)),
                          ("+", lambda: self.keyframes.set_zoom(self.keyframes.zoom * 1.5)),
                          ("Fit all", self.keyframes.fit_timeline)):
            button = QPushButton(label)
            button.clicked.connect(fn)
            zoom_row.addWidget(button)
        self.timeline_zoom_label = QLabel("Fit")
        zoom_row.addWidget(self.timeline_zoom_label)
        zoom_row.addStretch()
        zoom_row.addWidget(QLabel("Ctrl+wheel: zoom · Shift+wheel: pan"))
        self.keyframes.zoomChanged.connect(lambda z: self.timeline_zoom_label.setText("Fit" if z == 1 else f"{z:.1f}×"))
        animator_layout.addLayout(zoom_row)
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
        # How many pieces each gap is cut into, so the setting means the same
        # thing whether the gap is ten frames or sixty. Asking for more pieces
        # than the gap has frames fills every frame, so the ceiling is only
        # there to keep the number meaningful: capping it below the length of a
        # long gap silently spaced the frames out again, which read as the
        # tween ignoring the request.
        self.key_density.setRange(1, 4000)
        self.key_density.setValue(6)
        self.key_density.setToolTip(
            "How many steps to write between each pair of keys; as many as the "
            "gap has frames writes every frame"
        )
        # The description next to the button says how many frames will be
        # written, so it has to follow the number.
        self.key_density.valueChanged.connect(self.update_curve_info)
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
            box.setRange(-1e6, 1e6)
            box.setValue(0.0 if name in ("X", "Y", "Rotation") else 1.0)
            box.setMinimumWidth(70)
            # Enter writes the value straight onto the selected placement, so
            # there is no separate button to press after typing one in. Editing
            # a field and then clicking the canvas would otherwise discard it,
            # which is what "I entered -176.55 and nothing changed" was.
            box.setKeyboardTracking(False)
            box.editingFinished.connect(self.apply_animation_fields)
            transform_row.addWidget(box)
            self.key_fields[name] = box
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
        # In tab order, so an index into the picker is an index into the tabs.
        # Children leads: a movieclip is mostly the things it is made of, so it
        # is the panel an edit usually starts from.
        self.panel_picker.addItems(
            ["Children", "Properties", "Timeline", "JSON", "Textures", "Geometry"]
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
        self.children = table(["Resource ID", "Instance name", "Blend", "Visible"], "children")
        # No "CHILDREN" heading: the INSPECTOR dropdown already names this panel,
        # and it reads "Children" while this page is showing.
        self.children_search = QLineEdit()
        self.children_search.setPlaceholderText("Search children…  Ctrl+F")
        self.children_search.setToolTip("Filter by instance name, resource ID, or export name. Enter selects the first match.")
        self.children_search.setClearButtonEnabled(True)
        self.children_search.textChanged.connect(self.filter_children)
        self.children_search.returnPressed.connect(self.first_child_match)
        self.children_search.installEventFilter(self)
        instances_layout.addWidget(self.children_search)
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
        self.frames = table(["Frame", "Label", "Placements"], "frames")
        # Right-click on any of the clip's lists offers that list's own actions,
        # so they are reachable where the pointer already is.
        for grid, handler in (
            (self.children, self.children_context_menu),
            (self.frames, self.frames_context_menu),
        ):
            grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            grid.customContextMenuRequested.connect(handler)
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
        self.elements = table(["Child index", "Matrix index", "Color index"], "placements")
        self.elements.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.elements.customContextMenuRequested.connect(
            self.placements_context_menu
        )
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
        self.commands = table(["Texture atlas", "Flags", "Vertex count"], "commands")
        self.vertices = table(["X", "Y", "U16", "V16"], "vertices")
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
            ("Copy vertices", self.copy_vertices),
            ("Paste vertices", lambda: self.paste_vertices(self.vertices)),
            ("Apply geometry", self.apply_geometry),
            ("Export PNG…", self.export_shape_image),
        ]:
            button = QPushButton(title)
            button.clicked.connect(fn)
            vr.addWidget(button)
        sv.addLayout(vr)
        self.tabs.addTab(shape, "Geometry")
        # Added last but moved to the front: the widget index is what the panel
        # picker and every set_panel call use, so the tab order is the panel
        # order. Children leads, since a movieclip is mostly what it is made of.
        self.tabs.insertTab(0, instances_page, "Children")
        self.tabs.insertTab(1, props, "Properties")
        self.tabs.setCurrentIndex(0)
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
        self.cursor.valueChanged.connect(self.render_frame)
        self.play.clicked.connect(self.toggle_play)
        fit.clicked.connect(lambda: self.render_frame(fit=True))
        self.children.cellChanged.connect(self.child_changed)
        self.frames.cellChanged.connect(self.timeline_changed)
        self.frames.itemSelectionChanged.connect(self.select_frame)
        self.elements.cellChanged.connect(self.timeline_changed)
        # Picking a different placement changes which rows the keyframe strip
        # shows keys for, and whose transform the animator's fields describe, so
        # both follow the selection rather than only the strip.
        self.elements.itemSelectionChanged.connect(self.placement_selection_changed)
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
        if (watched is getattr(self, "children_search", None) and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            self.children_search.clear()
            self.children.setFocus()
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

    def update_code(self, obj):
        self.pending_json = obj
        # JSON is panel 3 in the tab order; this was left at the old index, so
        # the panel never filled in until it was re-selected.
        if self.active_panel != 3:
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
            self.cursor.value(),
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
        self.cursor.setValue(min(frame, self.cursor.maximum()))
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
        # The index and names are projections of the tables, so an edit that
        # changed them (a new resource, a renamed export) has to rebuild them
        # here; otherwise reads answer from the state before the edit and a
        # resource keeps the name it no longer has.
        self.doc._reindex()
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
        if index == 4 and self.texture_index is None and self.doc.tables["Textures"]["textures"]:
            self.view_selection_atlas()
            return
        self.active_panel = index
        self.tabs.setCurrentIndex(index)
        if index == 3 and hasattr(self, "pending_json"):
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

    def highlighted_rows(self, grid):
        """Every row the grid has picked, ascending, empty when none is.

        The tables select whole rows, so a clicked range is read from the
        selection model rather than from the current row alone - that is what
        makes Ctrl- and Shift-click mean something. Rows come out of the
        selected *ranges* rather than ``selectedRows``: a cell-range selection
        (what Shift-click and Ctrl+A produce, and what Ctrl+C copies) does not
        mark whole rows as selected, so ``selectedRows`` would miss it.
        """
        rows = set()
        for area in grid.selectionModel().selection():
            rows.update(range(area.top(), area.bottom() + 1))
        return sorted(rows)

    def clip_lists_with_focus(self):
        """Which clip list the keyboard is in: children, placements or preview.

        Focus identity alone is not enough to decide this. The panel holding the
        list can take focus itself, and a canvas click moves it to the preview,
        so a caller that asked only ``focusWidget() is self.children`` saw no
        list at all and fell through to the resource action. Naming the surface
        here keeps that decision in one place.
        """
        focus = QApplication.focusWidget()
        if focus is self.preview or (
            focus is not None and self.preview.isAncestorOf(focus)
        ):
            return "preview"
        for name, grid in (
            ("children", self.children),
            ("placements", self.elements),
        ):
            if focus is grid or (focus is not None and grid.isAncestorOf(focus)):
                return name
        return None

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

    def list_context_menu(self, grid, position, entries):
        """Right-click menu for one of the clip's lists.

        Mirrors that list's own buttons, so every action on it is reachable
        without moving the pointer to the button row. Standard right-click
        behaviour: a click inside an existing multi-selection keeps it and acts
        on all of it, otherwise the row under the cursor becomes the selection
        first.
        """
        item = grid.itemAt(position)
        row = item.row() if item is not None else -1
        if row >= 0:
            picked = set(self.highlighted_rows(grid))
            if row not in picked:
                grid.clearSelection()
                grid.selectRow(row)
            grid.setCurrentCell(row, max(0, grid.currentColumn()))
        menu = QMenu(grid)
        for title, fn in entries:
            action = menu.addAction(title)
            action.setEnabled(row >= 0 or bool(self.highlighted_rows(grid)))
            action.triggered.connect(lambda checked=False, callback=fn: callback())
        menu.exec(grid.viewport().mapToGlobal(position))

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
            self.cursor.value()
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
                if key in COLOR_KEYS and isinstance(value, int):
                    # An ARGB colour edited as a swatch, not as the number it
                    # packs to; the number stays in the tooltip for anyone who
                    # needs the exact value.
                    widget = self.color_swatch(value)
                elif key == "align" and isinstance(value, int):
                    # Alignment is a packed bitfield, not a number to do
                    # arithmetic on, so it is edited on two named dropdowns.
                    widget = self.align_control(value)
                elif isinstance(value, int):
                    widget = (
                        QLineEdit(str(value)) if abs(value) > 2147483647 else QSpinBox()
                    )
                    if isinstance(widget, QSpinBox):
                        widget.setRange(-2147483647, 2147483647)
                        widget.setValue(value)
                    widget.setEnabled(key not in ("id", "frames_count"))
                    signal = (
                        widget.valueChanged
                        if isinstance(widget, QSpinBox)
                        else widget.textEdited
                    )
                    signal.connect(lambda *_: self.mark_draft("properties"))
                elif isinstance(value, float):
                    widget = QDoubleSpinBox()
                    widget.setRange(-1e9, 1e9)
                    widget.setDecimals(6)
                    widget.setValue(value)
                    widget.setEnabled(key not in ("id", "frames_count"))
                    widget.valueChanged.connect(
                        lambda *_: self.mark_draft("properties")
                    )
                else:
                    widget = QLineEdit(str(value))
                    widget.setEnabled(key not in ("id", "frames_count"))
                    widget.textEdited.connect(lambda *_: self.mark_draft("properties"))
                self.form.addRow(key.replace("_", " ").title(), widget)
                self.property_widgets[key] = (widget, type(value))
            export = QLineEdit(", ".join(obj.get("exports", [])))
            self.form.addRow("Export names", export)
            self.property_widgets["exports"] = (export, list)
            export.textEdited.connect(lambda *_: self.mark_draft("properties"))
            # Timeline (2) and Children (0) belong to a movieclip; Geometry (5)
            # belongs to a shape. The indices follow the tab order, which is
            # what the picker and set_panel use.
            for panel, enabled in (
                (0, kind == "movieclips"),
                (2, kind == "movieclips"),
                (5, kind == "shapes"),
            ):
                self.tabs.setTabEnabled(panel, enabled)
                self.panel_picker.model().item(panel).setEnabled(enabled)
            if not self.tabs.isTabEnabled(self.active_panel):
                self.set_panel(0)
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
            # Transport seeks existing frames only. The separate orange
            # authoring cursor remains usable even on a one-frame clip.
            self.frame.setEnabled(kind == "movieclips" and count > 1)
            # The cursor may sit a little past the last frame: changing
            # something there is what extends the clip, and scrubbing must be
            # able to reach it without creating anything on its own.
            # Headroom belongs to the authoring cursor, never the transport.
            self.frame_span = max(
                old_frame,
                max(0, count - 1) + (self.FRAME_HEADROOM if count else 0),
            )
            self.cursor.setRange(0, self.frame_span)
            self.cursor.setValue(min(old_frame, self.cursor.maximum()))
            if kind == "movieclips":
                fill_table(
                    self.children,
                    [[c["id"], c["name"], c["blend"]] for c in obj["children"]],
                )
                self.fill_visibility()
                self.filter_children()
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
            value=self.cursor.value(),
            fit=getattr(self, "preview_resource", None) != self.selected,
        )
        self.preview_resource = self.selected

    def color_swatch(self, value):
        """A colour swatch wired to the draft tracking the form uses."""
        widget = ColorSwatch(value)
        widget.toggled.connect(lambda *_: self.mark_draft("properties"))
        return widget

    def scrub_progress(self, value):
        """The transport can seek only existing frames; it never grows a clip."""
        count = len(self.obj.get("frames") or []) if hasattr(self, "obj") else 0
        self.cursor.setValue(max(0, min(value, max(0, count - 1))))

    def tick(self):
        # One frame per tick, at the clip's own rate. Deriving the frame from
        # wall-clock time made a slow frame (a heavy clip, or a large document)
        # drop the animation forward by dozens of frames, so the rate is now set
        # by the timer and rendering latency can only slow playback, not skip it.
        last = max(0, len(self.obj.get("frames") or []) - 1)
        # Playback loops over the clip itself, never into the headroom.
        self.cursor.setValue((self.cursor.value() + 1) % (last + 1))

    def toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play.setText("▶ Play")
            self.show_elements()
        elif self.selected in self.doc.index:
            self.cursor.setValue(min(self.cursor.value(), max(0, len(self.obj.get("frames") or []) - 1)))
            fps = max(1, self.doc.index[self.selected][1].get("framerate", 30))
            self.timer.start(max(16, int(1000 / fps)))
            self.play.setText("⏸ Pause")

    def select_track(self, row):
        """Select a child's track, keeping the preview and panels in step."""
        if row < 0 or row >= self.elements.rowCount():
            return
        self.elements.selectRow(row)
        self.preview.selected_placement = row
        self.preview.viewport().update()
        self.load_animation_fields()

    def set_snap_pixels(self, checked):
        """Apply the snap toggle to every tab and remember it for the session.

        The setting is one choice for the session, not per tab, so a change
        reaches every open editor rather than only the one whose box was
        clicked; otherwise switching tabs would silently change behaviour.
        """
        window = self.window()
        self.preview.snap_movement = bool(checked)
        if hasattr(window, "snap_pixels"):
            window.snap_pixels = bool(checked)
            window.settings.setValue("snap_pixels", bool(checked))
            for index in range(window.editors.count()):
                other = window.editors.widget(index)
                other.preview.snap_movement = bool(checked)
                if other is not self:
                    with QSignalBlocker(other.snap_pixels):
                        other.snap_pixels.setChecked(bool(checked))

    def grow_clip(self, clip, index, update=True):
        """Give the clip a frame at ``index``, adding the frames up to it.

        New frames carry the last frame's placements, so they keep showing what
        was on screen instead of going blank, and the cursor can go past the
        end again afterwards: extending is a repeatable action, not a one-off.

        Called either inside a larger edit (``update=False``) or as the edit
        itself, which is what makes an extension one undo step.
        """
        if index < len(clip["frames"]):
            return
        template = clip["frames"][-1] if clip["frames"] else {"elements": []}
        for _ in range(index + 1 - len(clip["frames"])):
            clip["frames"].append(
                {
                    "label": "",
                    # The child each frame draws, but not the transform it was
                    # drawn with: a copied key reference would make every added
                    # frame a key in its own right, so a clip grown out to frame
                    # thirty would read as thirty keys that nothing ever set.
                    # Leaving the transform unset makes the frames inherit, so
                    # the timeline sees two keys and the tween has room to fill
                    # the frames between them.
                    "elements": [
                        [e[0], 65535, 65535] for e in template.get("elements", [])
                    ],
                }
            )
        if update:
            self.doc.update("movieclips", clip)

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

    def clip_bank(self, clip):
        """The clip's transform bank, or an empty one when it has none yet.

        A freshly created movieclip can name a bank index that does not exist,
        and document.bank() raises on that rather than returning nothing.
        """
        index = clip.get("matrix_bank_index")
        if index is None or index >= len(self.doc.storage["matrix_banks"]):
            return {"matrices": [], "colors": []}
        return self.doc.bank(index)

    def effective_matrix(self, clip, element):
        """The transform the placement renders with right now."""
        if element[1] == 65535:
            return list(easing.IDENTITY)
        matrices = self.clip_bank(clip).get("matrices") or []
        if element[1] < len(matrices):
            return list(matrices[element[1]])
        return list(easing.IDENTITY)

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

    def viewer_context_menu(self, row, position):
        menu = QMenu(self)
        if row >= 0:
            self.preview.selected_placement = row
            self.select_placement(row)
            frame = self.cursor_frame(self.obj)
            count = len(frame["elements"]) if frame is not None else 0
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

    def sync_lists_to_selection(self):
        """Follow the canvas's picked placements in the two clip lists.

        Placements mirrors the whole set, since that list is per-placement and
        the two agree row for row. Children follows only the primary: it is the
        clip's child list, not a per-placement one, and writing a whole set
        there left rows picked after the canvas pick was over, so a later
        single-row action - a plain click then Delete - acted on all of them.
        A multi-child selection is the user's to make in that list.
        """
        if self.selected not in self.doc.index:
            return
        if self.doc.index[self.selected][0] != "movieclips":
            return
        row = self.preview.selected_placement
        rows = set(self.preview.selected_rows)
        if row is not None:
            rows.add(row)
        rows = {r for r in rows if r is not None}
        frame = self.cursor_frame(self.obj)
        elements = frame["elements"] if frame is not None else []
        rows = {r for r in rows if 0 <= r < len(elements)}
        children = self.obj["children"]
        with QSignalBlocker(self.children), QSignalBlocker(self.elements):
            self.children.clearSelection()
            self.elements.clearSelection()
            for r in sorted(rows):
                self.elements.selectRow(r)
            if row is not None and 0 <= row < len(elements):
                ci = elements[row][0]
                if 0 <= ci < len(children):
                    self.children.selectRow(ci)
        if self.timer.isActive():
            self.toggle_play()
        self.set_panel(0)
        self.load_animation_fields()

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
        path, _ = app.native_picker(
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
        fi = self.cursor.value()
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
            self.set_panel(0)
            self.frames.selectRow(fi)
            self.elements.selectRow(self.elements.rowCount() - 1)
            self.render_frame(fit=True)

    def intern_matrices(self, clip, matrices):
        """Append several matrices at once and return their local indices.

        Appending one at a time rewrites the whole bank per append, which makes
        writing a long tween quadratic in its length. The bank is grown once
        here, and the clip's bank is registered a single time.
        """
        bank = self.clip_bank(clip)
        rows = list(bank.get("matrices") or [])
        first = len(rows)
        rows.extend([float(v) for v in matrix] for matrix in matrices)
        clip["matrix_bank_index"] = self.doc.add_bank(
            rows, list(bank.get("colors") or [])
        )
        fresh = self.doc.bank(clip["matrix_bank_index"])["matrices"]
        # add_bank may collapse an identical bank, so the indices are read back
        # rather than assumed: the clip may already hold these transforms.
        return [first + i for i in range(len(matrices))], fresh

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
        self.assign_matrices(clip, [(element, matrix)])

    def assign_matrices(self, clip, assignments):
        """Point several elements at new matrices in one bank compaction.

        ``assign_matrix`` compacts the whole bank on every call, so assigning a
        whole selection one element at a time would compact once per placement.
        This does it once for the set, which is what keeps a group drag a single
        edit rather than N of them.
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
        for element, matrix in assignments:
            element[1] = len(matrices)
            matrices.append(matrix)
        clip["matrix_bank_index"] = self.doc.add_bank(matrices, colors)
        self.doc.update("movieclips", clip)

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
            frame = obj["frames"][fi] if 0 <= fi < len(obj["frames"]) else None
            if frame is None or row >= len(frame["elements"]):
                return
            e = frame["elements"][row]
            old = (
                self.doc.bank(obj["matrix_bank_index"])
                if any(
                    x[1] != 65535 or x[2] != 65535
                    for f in obj["frames"]
                    for x in f["elements"]
                )
                else {"matrices": [], "colors": []}
            )
            # A stale matrix or colour index points past the bank, which raised
            # IndexError instead of opening the dialog on the identity pose.
            matrix = (
                old["matrices"][e[1]]
                if e[1] != 65535 and e[1] < len(old["matrices"])
                else [1, 0, 0, 1, 0, 0]
            )
            color = (
                old["colors"][e[2]]
                if e[2] != 65535 and e[2] < len(old["colors"])
                else [255, 255, 255, 255, 0, 0, 0]
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
                self.cursor.setValue(fi)
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
                self.set_panel(0)
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
        path, _ = app.native_picker(self, "Import image as shape", "",
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
                self.set_panel(5)
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

    def shape_from_png(self):
        if not self.resolve_drafts():
            return
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            return
        path, _ = app.native_picker(
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

    def export_shape_image(self):
        """Write the open shape's rendered image to a PNG.

        The shape is rasterized exactly as the viewer draws it - the same
        triangle-strip surfaces, at the shape's own resolution rather than the
        zoomed view - so what lands on disk is the artwork, not a screenshot.
        """
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            self.window().statusBar().showMessage(
                "Select a shape to export its image", 4000
            )
            return
        obj = self.doc.resource(self.selected)
        if not obj.get("commands"):
            self.window().statusBar().showMessage(
                "This shape has no geometry to render", 4000
            )
            return
        path, _ = app.save_picker(self, "Export shape image", "", "PNG (*.png)")
        if not path:
            return
        try:
            left, top, scale, width, height = self.render_shape_bounds(obj)
            # Render at full scale for the file; the viewer's surface is capped
            # so a huge mesh stays interactive, and that cap should not shrink
            # what is written out.
            image = QImage(
                max(1, math.ceil(width) + 1),
                max(1, math.ceil(height) + 1),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.translate(-left, -top)
            try:
                for command in obj["commands"]:
                    atlas = self.preview.atlas(command["texture_index"])
                    vertices = command["vertices"]
                    for i in range(len(vertices) - 2):
                        self.preview.render_triangle(
                            painter, vertices[i : i + 3], atlas
                        )
            finally:
                painter.end()
            if not image.save(path):
                raise ValueError("could not write the image")
            self.window().statusBar().showMessage(
                f"Exported shape image to {path}", 6000
            )
        except Exception as e:
            self.error(e)

    def render_shape_bounds(self, obj):
        """The shape's origin and pixel size, from its own vertices."""
        verts = [v for command in obj["commands"] for v in command["vertices"]]
        left, top = min(v[0] for v in verts), min(v[1] for v in verts)
        width = max(v[0] for v in verts) - left
        height = max(v[1] for v in verts) - top
        return left, top, 1.0, width, height
