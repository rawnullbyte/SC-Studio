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
    QTransform,
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
    QComboBox,
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
from .widgets import (
    AlignControl,
    ColorSwatch,
    ResourceItem,
    Task,
    _uv,
    fill_table,
    jsonable,
    save_picker,
    table,
    typing_into,
)
from . import textures
from . import easing
from . import themes as theme_schemes

# Textfield fields that hold a packed 0xAARRGGBB colour and are therefore edited
# with a colour picker rather than as the integer they are stored as.
COLOR_KEYS = ("font_color", "outline_color")



from .editor_core import CorePanel
from .panel_animation import AnimationPanel
from .panel_children import ChildrenPanel
from .panel_frames import FramesPanel
from .panel_geometry import GeometryPanel
from .panel_properties import PropertiesPanel
from .panel_textures import TexturesPanel

class Editor(
    CorePanel,
    PropertiesPanel,
    ChildrenPanel,
    FramesPanel,
    TexturesPanel,
    GeometryPanel,
    AnimationPanel,
    QWidget,
):
    """One document's editor: a stack of panel mixins over a QWidget.

    Each mixin holds the methods for one inspector panel, moved verbatim from
    what used to be a single class. They define no __init__, so they contribute
    bound methods and nothing else, and Editor's own __init__ still runs when
    super() reaches QWidget. CorePanel is first because it holds the two
    methods that collide with Qt's: __init__, whose super() call has to reach
    the rest of the stack, and eventFilter.
    """

    changed = pyqtSignal()



class Window(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or QSettings("SCStudio", "SCStudio")
        self.setWindowTitle("SC Studio")
        self.resize(1180, 760)
        self.clipboard = None
        self.jobs = []
        self.actions = {}
        # Snap is on by default; the saved choice is honoured when present.
        self.snap_pixels = self.settings.value("snap_pixels", True, type=bool)
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
        self.action(
            resource,
            "import_animation",
            "Import animation (GIF/WebP)…",
            lambda: self.current() and self.current().import_animation(),
            "Ctrl+Alt+A",
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
            ("Children", "Properties", "Timeline", "JSON", "Textures", "Geometry")
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
        from .studio_tools import install_tools
        install_tools(self, file, create_menu, resource, view, help_menu)
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

    def shortcuts(self, parent, key, title, fn, *keys):
        """One action reachable by several keys, tried in order.

        Qt tries a window's actions in an unspecified order and takes the first
        whose key matches, so registering the same key on several hidden actions
        is a coin toss. Here the first key is the action's own shortcut and the
        rest are matched by the event filter, which is deterministic: the first
        one listed that applies wins.
        """
        action = self.action(parent, key, title, fn, keys[0] if keys else None)
        action.setProperty("extra_shortcuts", [QKeySequence(k) for k in keys[1:]])
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
        # The canvas sits behind the artwork as a fixed colour, so it has to be
        # told about the change: the palette reaches every other widget on its
        # own, but not a brush set on a graphics view.
        for i in range(self.editors.count()):
            self.editors.widget(i).preview.apply_theme()

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
        for grid in (
            ed.tree,
            ed.keyframes,
            ed.children,
            ed.elements,
            ed.frames,
            ed.preview,
        ):
            if self.delete_shortcut not in grid.actions():
                grid.addAction(self.delete_shortcut)

    def add_doc(self, doc):
        editor = Editor(doc)
        editor.changed.connect(lambda: self.title(editor))
        self.editors.addTab(editor, "")
        self.editors.setCurrentWidget(editor)
        editor.tree.setFocus()
        self.title(editor)
        # The snap choice is saved once for the session; each new tab starts
        # from it (and from whatever the last toggle set) rather than from the
        # widget's own default.
        with QSignalBlocker(editor.snap_pixels):
            editor.snap_pixels.setChecked(self.snap_pixels)
        editor.preview.snap_movement = self.snap_pixels
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
        # A child picked in the Children or Placements list is what Copy is
        # about: it copies the resource each picked child instances, not the
        # movieclip they sit in. Every picked row counts, so a set of instances
        # copies its several resources. Everywhere else the browser selection,
        # or the open resource, is what is copied.
        children = ed.picked_child_ids()
        ids = children if children else ed.selected_ids()
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
        # Duplicating a picked child is the child-list action, which copies the
        # instance into the same movieclip; duplicating a resource makes a new
        # resource. The two are different enough that the child pick decides.
        # Read from the picked rows, not the focus widget: with several rows
        # marked and the table unfocused, that answer was empty and this fell
        # through to duplicating the whole movieclip.
        child = ed.focused_child_index()
        if child < 0:
            rows = ed.selected_child_rows()
            child = rows[-1] if rows else -1
        if child >= 0:
            ed.duplicate_child(child)
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
        # Delete belongs to whatever has the keyboard. With the timeline
        # focused it removes the picked keys or the marked frame range; with
        # neither picked it removes the frame under the playhead, which is the
        # frame the timeline is showing. In no case does it delete the resource:
        # falling through to the browser selection is what made a frame press
        # take out the whole movieclip.
        if ed.keyframes.hasFocus():
            if ed.delete_selected_keys():
                return
            ed.remove_frames([ed.cursor.value()])
            return
        # The Frames table names a frame of the open clip, so Delete there is
        # the frame action, not deleting the resource. Without this the browser
        # fallback below deleted the whole movieclip, which is why a frame press
        # could take out something other than the frame that was picked.
        focus = QApplication.focusWidget()
        if focus is ed.frames:
            ed.remove_frame()
            return
        # A child focused in the Children or Placements list is what Delete is
        # about: it removes that instance from every frame, which is the
        # child-list action, not deleting the resource it instances. Every
        # picked row goes, so a Ctrl-clicked set is removed together.
        #
        # Which clip list a press means is decided by where the rows are picked
        # rather than by asking which widget has focus. Focus identity is not
        # dependable here - the panel itself can hold it, and the preview takes
        # it on a canvas click - so a test that could not rely on it deleted the
        # wrong thing. A picked row is unambiguous; focus is not.
        # The current row is the robust signal here; the picked ranges and the
        # focus widget both proved unreliable, so the row decides the surface
        # and its picked range - if any - says what to take.
        inside = ed.clip_lists_with_focus()
        if inside == "children":
            # Single row only. A marked range in this list can outlive the
            # layout it was made in (the rows are rebuilt with signals blocked),
            # so reading a set here could remove a row the user never picked.
            # Multi-select in the Children list still drives the canvas; only
            # Delete stays on the current row.
            row = ed.children.currentRow()
            rows = [row] if row >= 0 else []
        elif inside == "placements":
            rows = ed.selected_placement_rows()
        else:
            rows = []
        if rows and inside == "placements":
            ed.remove_element_rows(rows)
            return
        if rows and inside == "children":
            ed.remove_child_rows(rows)
            return
        if inside is None and ed.children.currentRow() >= 0:
            ed.remove_child_rows([ed.children.currentRow()])
            return
        # The canvas is an editing surface too: Delete there acts on the picked
        # placements and on nothing else. It must never fall through to deleting
        # the whole resource, the one thing a press in the canvas cannot mean.
        if ed.preview.hasFocus() or inside == "preview":
            rows = ed.selected_placement_rows()
            if rows:
                ed.remove_element_rows(rows)
            return
        child = ed.focused_child_index()
        if child >= 0:
            ed.remove_child(child)
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
                if focus is ed.vertices:
                    # The vertex list is a portable unit: copying it takes the
                    # whole list, so it can be pasted onto another shape's
                    # command. A click that leaves a current cell selected is
                    # still "copy these vertices", not "copy this one value".
                    ed.copy_vertices()
                    return
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
                elif focus is ed.children or focus is ed.elements:
                    # A whole row is not marked out as a cell range, so a Copy
                    # on the Children or Placements list with nothing swept
                    # copies the child that row stands for rather than doing
                    # nothing. An explicit range still copies its text.
                    self.copy()
            elif command == "paste":
                if focus is ed.vertices:
                    ed.paste_vertices(focus)
                else:
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
        """Ctrl+F searches whatever is focused.

        One key means "find in the thing I am looking at" rather than always the
        asset browser: in the browser it filters assets, at the timeline it
        filters frames and placements, in a panel it picks a row, and in text it
        is the editor's own find. Searching the wrong pane and having to correct
        it is the cost this avoids.
        """
        ed = self.current()
        if ed is None:
            return
        focus = QApplication.focusWidget()
        if focus is ed.children_search:
            ed.children_search.selectAll()
            return
        if focus in (ed.children, ed.frames, ed.elements):
            self.find_in_table(focus)
            return
        if focus is ed.code:
            self.find_json()
            return
        ed.splitter.widget(0).show()
        ed.search.setFocus()
        ed.search.selectAll()
        self.active_changed()

    def find_in_table(self, grid):
        """Use the inline children filter, or find a row in other tables."""
        if grid is self.current().children:
            self.current().children_search.setFocus()
            self.current().children_search.selectAll()
            return
        ed = self.current()
        term, ok = QInputDialog.getText(
            self,
            "Find in table",
            f"Find in {grid.objectName() or 'table'}:",
            text=getattr(self, "table_term", ""),
        )
        if not ok or not term:
            return
        self.table_term = term
        needle = term.casefold()
        # The scan starts at the current row and wraps, so the first press finds
        # the topmost match rather than skipping the row already selected. A
        # repeated press steps past the row it last landed on, which is what
        # makes it walk the matches.
        start = max(0, grid.currentRow())
        hit = getattr(self, "table_hit", None)
        if hit and hit[0] is grid and hit[1] == start:
            start += 1
        rows = list(range(start, grid.rowCount()))
        rows += list(range(0, min(start, grid.rowCount())))
        for row in rows:
            for column in range(grid.columnCount()):
                item = grid.item(row, column)
                if item and needle in item.text().casefold():
                    grid.selectRow(row)
                    grid.scrollToItem(item)
                    grid.setFocus()
                    self.table_hit = (grid, row)
                    return
        self.statusBar().showMessage(f"No match for {term!r}", 4000)

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
        # Plain X opens Find usages on whatever is focused, the same way G opens
        # Go to ID: a child focused in the Children or Placements list, or the
        # open resource otherwise. Ctrl+Shift+R and Shift+F12 still reach the
        # dialog through their own actions.
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_X
            and not event.modifiers()
            and QApplication.activeModalWidget() is None
            and self.editors.count()
            and not typing_into(
                watched if isinstance(watched, QWidget) else QApplication.focusWidget()
            )
        ):
            self.references()
            return True
        if event.type() == QEvent.Type.KeyPress:
            # Extra shortcuts, tried in the order they were declared, so one
            # function can be reachable by several keys without depending on the
            # order Qt happens to try its actions in.
            for action in self.actions.values():
                extra = action.property("extra_shortcuts") or []
                for sequence in extra:
                    if event.keyCombination() == sequence:
                        if action.isEnabled():
                            action.trigger()
                        return True
        if event.type() == QEvent.Type.MouseButtonPress:
            # Mouse side buttons are the conventional undo and redo, and Qt does
            # not map them to anything by default.
            ed = self.current()
            if ed is not None and QApplication.activeModalWidget() is None:
                if event.button() == Qt.MouseButton.BackButton:
                    ed.undo()
                    return True
                if event.button() == Qt.MouseButton.ForwardButton:
                    ed.redo()
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
        if ed.active_panel == 1:
            ed.apply_properties()
        elif ed.active_panel == 3:
            ed.apply_json()
        elif ed.active_panel == 5:
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
            ed.set_panel(3)
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
        # A focused child is what Find usages is about when the keyboard is on
        # the Children or Placements list; elsewhere the selected resource is.
        if target is None:
            target = ed.focused_child_id()
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
                ed.set_panel(0)
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
            # G and X are routed by the window event filter rather than by the
            # action, so they are listed here to keep the dialog matching real
            # behaviour.
            if key == "goto":
                keys = ", ".join(filter(None, [keys, "G"]))
            elif key == "references":
                keys = ", ".join(filter(None, [keys, "X"]))
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
            "SC Studio 0.6.1\nNew documents and exported resource graphs use SC6. Existing SC5/SC6 files retain their version.\n\nSC1 is unsupported. The editable scene is an approximate preview. Use View → Rendered effects for RGB/alpha effects, blending, stencil masks, text styles and animation export. Game-specific metrics and rendering can differ. Common identity-child scaling grids are previewed. SC1, SCTX, new ASTC compression and direct FLA/XFL import remain unsupported.\nNative file dialogs use the platform implementation when available.\n\nF1 lists keyboard shortcuts.",
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
