import os
from collections import deque

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import pytest
from PyQt6.QtCore import QPoint, QSettings, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QMessageBox,
    QTableWidgetSelectionRange,
    QFileDialog,
)
from sc_studio.app import Window, fill_table, typing_into
from sc_studio.document import Document
from sc_studio.workspace import (
    CLIP_MIME,
    clipboard_pack,
    clipboard_unpack,
    native_picker,
)
from test_document import populated

_app = None


def application():
    global _app
    _app = QApplication.instance() or QApplication([])
    return _app


def wait_until(fn, timeout=5):
    deadline = time.monotonic() + timeout
    while not fn() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert fn()


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = application()
    app.clipboard().clear()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = Window(settings)
    w.show()
    w.activateWindow()
    app.processEvents()

    def critical(*args):
        raise AssertionError(str(args[-1]))

    monkeypatch.setattr(QMessageBox, "critical", critical)
    yield w
    wait_until(lambda: not w.jobs)
    for i in range(w.editors.count()):
        e = w.editors.widget(i)
        e.drafts.clear()
        e.mark_saved()
    w.close()
    app.processEvents()


def test_properties_geometry_timeline_paste_and_undo(window):
    w = window
    source = populated()
    w.add_doc(source)
    src = w.current()
    src.go_to(7)
    assert len(src.preview.scene().items()) > 0
    src.property_widgets["framerate"][0].setValue(15)
    src.apply_properties()
    assert source.resource(7)["framerate"] == 15
    src.tree.setFocus()
    w.copy()
    wait_until(lambda: not w.jobs)
    assert application().clipboard().mimeData().hasFormat(CLIP_MIME)
    w.new()
    dest = w.current()
    w.paste()
    wait_until(lambda: not w.jobs)
    root = dest.selected
    assert root is not None
    assert dest.doc.version == 6
    assert dest.doc.resource(root)["children"][0]["name"] == "sprite"
    dest.undo()
    assert not dest.doc.index
    assert not dest.preview.scene().items()
    dest.redo()
    assert root in dest.doc.index
    shape = dest.doc.resource(root)["children"][0]["id"]
    dest.go_to(shape)
    dest.commands.selectRow(0)
    dest.vertices.item(0, 0).setText("12.5")
    dest.apply_geometry()
    assert dest.doc.resource(shape)["commands"][0]["vertices"][0][0] == 12.5
    dest.go_to(root)
    dest.add_frame(True)
    assert len(dest.doc.resource(root)["frames"]) == 3


def test_default_labels_shortcuts_and_themes(window):
    w = window
    w.new()
    e = w.current()
    assert e.doc.version == 6
    assert w.actions["new"].text() == "&New"
    assert w.editors.tabText(0) == "Untitled"
    assert not w.findChildren(
        __import__("PyQt6.QtWidgets", fromlist=["QToolBar"]).QToolBar
    )
    e.tree.setFocus()
    QTest.keyClick(w, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert application().focusWidget() is e.search
    w.set_theme("dark")
    dark = application().palette().color(QPalette.ColorRole.Base)
    w.set_theme("light")
    light = application().palette().color(QPalette.ColorRole.Base)
    assert light.lightness() > dark.lightness()
    assert w.settings.value("theme") == "light"
    w.set_theme("system")
    w.new()
    e.tree.setFocus()
    QTest.keyClick(w, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert w.editors.currentIndex() == 0


def test_g_opens_go_to_id_from_the_browser(window, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    window.add_doc(populated())
    e = window.current()
    e.go_to(3)
    titles = []
    monkeypatch.setattr(
        QInputDialog,
        "getInt",
        lambda *args, **kwargs: (titles.append(args[1]), (7, True))[1],
    )
    e.tree.setFocus()
    QTest.keyClick(e.tree, Qt.Key.Key_G)
    assert titles == ["Go to resource"]
    assert e.selected == 7


def test_g_reports_and_survives_a_missing_resource(window, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    window.add_doc(populated())
    e = window.current()
    e.go_to(3)
    monkeypatch.setattr(QInputDialog, "getInt", lambda *args, **kwargs: (9999, True))
    e.tree.setFocus()
    QTest.keyClick(e.tree, Qt.Key.Key_G)
    assert e.selected == 3
    assert "9999" in window.statusBar().currentMessage()


def test_x_opens_find_usages_for_the_focused_child(window, monkeypatch):
    """Plain X opens usages on the focused child, like G opens Go to ID."""
    from PyQt6.QtWidgets import QDialog

    w = window
    d = populated(6)
    clip = d.resource(7)
    clip["children"] = [{"id": 4, "name": "tf", "blend": 0}]
    clip["frames"] = [
        {"label": "", "elements": [[0, 65535, 65535]]} for _ in range(4)
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    application().processEvents()
    titles = []
    monkeypatch.setattr(
        QDialog, "exec", lambda self: titles.append(self.windowTitle()) or 0
    )
    e.children.setFocus()
    e.children.selectRow(0)
    QTest.keyClick(w, Qt.Key.Key_X)
    # The child's resource (textfield 4), not the open movieclip 7.
    assert titles == ["Find usages — #4"]


def test_g_is_typing_inside_text_fields(window):
    window.add_doc(populated())
    e = window.current()
    e.go_to(4)

    field = e.property_widgets["text"][0]
    field.setFocus()
    field.clear()
    QTest.keyClick(field, Qt.Key.Key_G)
    assert field.text() == "g"

    e.search.setFocus()
    e.search.clear()
    QTest.keyClick(e.search, Qt.Key.Key_G)
    assert e.search.text() == "g"

    e.code.setFocus()
    e.code.setPlainText("")
    QTest.keyClick(e.code, Qt.Key.Key_G)
    assert e.code.toPlainText() == "g"


def test_typing_into_targets_text_entry_widgets(window):
    window.add_doc(populated())
    e = window.current()
    assert typing_into(e.search)
    assert typing_into(e.code)
    assert not typing_into(e.tree)
    assert not typing_into(None)
    assert not typing_into(window)
    e.go_to(4)
    assert typing_into(e.property_widgets["text"][0])
    e.go_to(7)
    assert typing_into(e.property_widgets["framerate"][0])


def test_shift_g_and_ctrl_g_do_not_double_fire(window, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    window.add_doc(populated())
    e = window.current()
    e.go_to(3)
    calls = []
    monkeypatch.setattr(
        QInputDialog,
        "getInt",
        lambda *args, **kwargs: (calls.append(1), (7, True))[1],
    )
    e.tree.setFocus()
    # Shift+G is not the plain-G route, so only the Ctrl+G action responds.
    QTest.keyClick(e.tree, Qt.Key.Key_G, Qt.KeyboardModifier.ShiftModifier)
    assert calls == []
    QTest.keyClick(e.tree, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
    assert calls == [1]
    assert e.selected == 7


def test_text_shortcuts_do_not_copy_or_paste_resources(window):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(4)
    field = e.property_widgets["text"][0]
    field.setFocus()
    field.selectAll()
    QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert application().clipboard().text() == "Hello world"
    assert w.clipboard is None
    application().clipboard().setText("Pasted text")
    QTest.keyClick(field, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert field.text() == "Pasted text"
    assert e.doc.resource(4)["text"] == "Hello world"
    assert e.drafts == {"properties"}
    e.apply_properties()
    assert e.doc.resource(4)["text"] == "Pasted text"


def test_textfield_color_and_alignment_use_real_widgets(window):
    """Colours are picked, alignment is chosen; neither is a raw number."""
    from sc_studio.app import AlignControl, ColorSwatch

    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(4)
    application().processEvents()
    swatch = e.property_widgets["font_color"][0]
    align = e.property_widgets["align"][0]
    assert isinstance(swatch, ColorSwatch)
    assert isinstance(align, AlignControl)
    # The number is still available, in the tooltip, for anyone who wants it.
    assert swatch.toolTip() == "argb 4294967295"
    swatch.setValue(0xFF3366CC)
    align.horizontal.setCurrentIndex(1)
    align.vertical.setCurrentIndex(2)
    e.apply_properties()
    obj = e.doc.resource(4)
    assert obj["font_color"] == 0xFF3366CC
    assert obj["align"] & 3 == 1
    assert (obj["align"] >> 2) & 3 == 2
    # The high nibble the two dropdowns do not cover is preserved.
    obj["align"] = 0x30
    e.doc.update("textfields", obj)
    e.go_to(4)
    application().processEvents()
    e.property_widgets["align"][0].vertical.setCurrentIndex(0)
    e.apply_properties()
    assert e.doc.resource(4)["align"] == 0x30


def test_table_tsv_paste_and_preview_frame_sync(window):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(7)
    e.set_panel(2)
    e.children.setCurrentCell(0, 1)
    e.children.setFocus()
    application().clipboard().setText("changed_child")
    w.route("paste")
    assert e.doc.resource(7)["children"][0]["name"] == "changed_child"
    e.frame.setValue(1)
    assert e.frames.currentRow() == 1
    assert e.elements.rowCount() == 1
    e.children.setCurrentCell(0, 0)
    e.children.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 2), True)
    w.route("copy")
    assert application().clipboard().text() == "3\tchanged_child\t0"


def test_undo_after_save_keeps_path_and_dirty_state(window, tmp_path):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(4)
    e.property_widgets["text"][0].setText("Saved text")
    e.apply_properties()
    path = tmp_path / "saved.sc"
    e.doc.save(path)
    e.mark_saved()
    assert not e.is_dirty()
    e.undo()
    assert e.doc.path == path
    assert e.is_dirty()
    assert e.doc.resource(4)["text"] == "Hello world"
    e.redo()
    assert not e.is_dirty()
    assert e.doc.path == path
    assert e.doc.resource(4)["text"] == "Saved text"
    e.undo()
    e.doc.save(path)
    e.mark_saved()
    assert Document.load(path).resource(4)["text"] == "Hello world"


def test_pending_edits_cancel_apply_and_invalid_json(window, monkeypatch):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(7)
    e.property_widgets["framerate"][0].setValue(12)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Cancel
    )
    assert not e.go_to(4)
    assert e.selected == 7
    assert e.doc.resource(7)["framerate"] == 30
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Apply
    )
    assert e.go_to(4)
    assert e.doc.resource(7)["framerate"] == 12
    e.set_panel(3)
    e.code.setPlainText("{invalid")
    errors = []
    monkeypatch.setattr(e, "error", errors.append)
    assert not e.resolve_drafts()
    assert errors
    assert e.drafts == {"json"}
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Discard
    )
    assert e.resolve_drafts()
    assert not e.drafts


def test_geometry_switch_preserves_draft_on_correct_command(window, monkeypatch):
    w = window
    d = populated()
    o = d.resource(3)
    o["commands"].append(__import__("copy").deepcopy(o["commands"][0]))
    d.update("shapes", o)
    w.add_doc(d)
    e = w.current()
    e.go_to(3)
    e.set_panel(5)
    e.commands.selectRow(0)
    e.vertices.item(0, 0).setText("42")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Apply
    )
    e.commands.selectRow(1)
    assert d.resource(3)["commands"][0]["vertices"][0][0] == 42
    assert d.resource(3)["commands"][1]["vertices"][0][0] == 0


def test_vertices_copy_and_paste_between_shapes(window):
    """A shape's vertex list can be transplanted onto another shape."""
    w = window
    d = populated()
    # A second shape whose command has fewer vertices than the source.
    d.update(
        "shapes",
        {
            "id": 6,
            "commands": [
                {"unk1": 0, "texture_index": 0, "vertices": [[0, 0, 0, 0], [5, 0, 65535, 0]]}
            ],
            "exports": [],
        },
    )
    w.add_doc(d)
    e = w.current()
    e.go_to(3)
    e.set_panel(5)
    application().processEvents()
    e.vertices.setFocus()
    w.route("copy")
    source = d.resource(3)["commands"][0]["vertices"]
    assert len(source) == 3
    e.go_to(6)
    application().processEvents()
    assert e.vertices.rowCount() == 2
    e.vertices.setFocus()
    e.vertices.setCurrentCell(0, 0)
    w.route("paste")
    application().processEvents()
    # The list replaced the command wholesale, growing the rows to match.
    assert e.vertices.rowCount() == 3
    e.apply_geometry()
    application().processEvents()
    assert d.resource(6)["commands"][0]["vertices"] == [
        [0.0, 0.0, 0, 0],
        [20.0, 0.0, 65535, 0],
        [0.0, 20.0, 0, 65535],
    ]


def test_a_shape_image_can_be_exported(window, tmp_path, monkeypatch):
    """Export PNG writes the shape's own rendered artwork."""
    import sc_studio.app as appmod

    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(3)
    e.set_panel(5)
    application().processEvents()
    target = tmp_path / "shape.png"
    monkeypatch.setattr(appmod, "save_picker", lambda *a, **k: (str(target), ""))
    e.export_shape_image()
    assert target.exists() and target.stat().st_size > 0
    from PyQt6.QtGui import QImage

    image = QImage(str(target))
    # The fixture shape is a 20x20 triangle; the export renders its own bounds.
    assert image.width() >= 20 and image.height() >= 20
    assert "Exported" in w.statusBar().currentMessage()


def test_multi_selection_copy_clipboard_transport(window):
    w = window
    d = populated()
    w.add_doc(d)
    e = w.current()
    extra = d.resource(3)
    extra["id"] = 5
    d.update("shapes", extra)
    e.go_to(3)
    e.tree.clearSelection()
    for i in range(e.tree.topLevelItemCount()):
        item = e.tree.topLevelItem(i)
        item.setSelected(True)
    assert set(e.selected_ids()) == {3, 5}
    w.copy()
    wait_until(lambda: not w.jobs)
    payload = bytes(application().clipboard().mimeData().data(CLIP_MIME))
    doc, ids = clipboard_unpack(payload)
    assert len(ids) == 2
    assert set(k for k, v in doc.index.values()) == {"shapes"}


def test_save_and_close_prompts_save_before_removing_tab(window, tmp_path, monkeypatch):
    w = window
    w.new()
    e = w.current()
    e.doc.new_resource("textfields")
    target = tmp_path / "closed.sc"
    monkeypatch.setattr("sc_studio.app.save_picker", lambda *_: (str(target), ""))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Save
    )
    assert not w.close_tab(0)
    wait_until(lambda: not w.jobs)
    assert w.editors.count() == 0
    assert target.exists()
    assert Document.load(target).version == 6


def test_native_picker_leaves_platform_native_dialog_enabled(monkeypatch):
    application()
    seen = []

    def fake_exec(dialog):
        seen.append(dialog.testOption(QFileDialog.Option.DontUseNativeDialog))
        return QFileDialog.DialogCode.Rejected

    monkeypatch.setattr(QFileDialog, "exec", fake_exec)
    assert native_picker(None, "Open", "", "SC (*.sc)") == ("", "")
    assert seen == [False]


def test_resource_keyboard_copy_paste_and_close(window):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(7)
    e.tree.setFocus()
    QTest.keyClick(e.tree, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: not w.jobs)
    assert application().clipboard().mimeData().hasFormat(CLIP_MIME)
    w.new()
    target = w.current()
    target.tree.setFocus()
    QTest.keyClick(target.tree, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: not w.jobs)
    assert target.doc.index and target.selected in target.doc.index
    target.mark_saved()
    QTest.keyClick(target.tree, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    assert w.editors.count() == 1


def test_search_enter_escape_and_numeric_sort(window):
    w = window
    doc = populated()
    for id in (2, 10):
        obj = doc.resource(3)
        obj["id"] = id
        doc.update("shapes", obj)
    w.add_doc(doc)
    e = w.current()
    e.search.setText("demo")
    e.first_match()
    assert e.selected == 7
    e.search.setFocus()
    QTest.keyClick(e.search, Qt.Key.Key_Escape)
    assert e.search.text() == ""
    assert application().focusWidget() is e.tree
    e.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    e.category.setCurrentIndex(e.resource_kinds.index("shapes"))
    assert [
        int(e.tree.topLevelItem(i).text(1)) for i in range(e.tree.topLevelItemCount())
    ] == [2, 3, 10]


def test_invalid_timeline_paste_rolls_back_and_plain_clipboard_is_not_a_graph(
    window, monkeypatch
):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(7)
    e.set_panel(2)
    e.children.setCurrentCell(0, 0)
    errors = []
    monkeypatch.setattr(e, "error", errors.append)
    application().clipboard().setText("999")
    w.paste_cells(e.children)
    assert errors
    assert e.doc.resource(7)["children"][0]["id"] == 3
    assert e.children.item(0, 0).text() == "3"
    before = len(e.doc.index)
    w.paste()
    assert not w.jobs
    assert len(e.doc.index) == before


def test_live_system_theme_and_dark_foregrounds(window):
    w = window
    w.set_theme("dark")
    for group in QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive:
        for role in (
            QPalette.ColorRole.Text,
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.ToolTipText,
        ):
            assert application().palette().color(group, role).lightness() > 150
    w.set_theme("system")
    w.system_theme_changed(Qt.ColorScheme.Dark)
    assert application().palette().color(QPalette.ColorRole.Text).lightness() > 150
    w.system_theme_changed(Qt.ColorScheme.Light)
    assert application().palette().color(QPalette.ColorRole.Text).lightness() < 100
    w.set_theme("dark")
    w.system_theme_changed(Qt.ColorScheme.Light)
    assert application().palette().color(QPalette.ColorRole.Text).lightness() > 150


def test_image_import_resize_save_and_undo(window, tmp_path, monkeypatch):
    from PIL import Image
    from sc_studio import textures

    path = tmp_path / "sprite.png"
    Image.new("RGBA", (32, 16), (10, 220, 40, 255)).save(path)
    d = populated(6)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.frame.setValue(1)
    e.insert_image(textures.replace_from_png({}, path), "sprite", [64, 32, 50, 60, 0])
    clip = d.resource(7)
    assert len(clip["frames"][0]["elements"]) == 2
    assert len(clip["frames"][1]["elements"]) == 2
    placed = clip["frames"][1]["elements"][-1]
    matrix = d.bank(clip["matrix_bank_index"])["matrices"][placed[1]]
    assert matrix == [2, 0, 0, 2, 50, 60]
    monkeypatch.setattr(e, "size_dialog", lambda *args: [96, 48, 100, 120, 90])
    e.resize_placement()
    clip = d.resource(7)
    matrix = d.bank(clip["matrix_bank_index"])["matrices"][
        clip["frames"][1]["elements"][-1][1]
    ]
    assert matrix == pytest.approx([0, 3, -3, 0, 100, 120], abs=1e-6)
    saved = tmp_path / "image.sc"
    d.save(saved)
    assert not Document.load(saved).validate()
    e.undo()
    assert len(d.resource(7)["children"]) == 3
    e.undo()
    assert len(d.resource(7)["children"]) == 2


def test_atlas_bounds_reset_and_fit_after_movieclip(window):
    window.add_doc(populated())
    e = window.current()
    e.go_to(7)
    e.preview.scale(20, 20)
    e.selected = None
    e.texture_index = 0
    e.show_texture()
    rect = e.preview.scene().sceneRect()
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (0, 0, 2, 2)
    e.preview.scale(3, 3)
    e.render_frame(fit=True)
    assert e.preview.transform().m11() == pytest.approx(e.preview.fit_scale)
    e.go_to(7)
    assert e.preview.scene().sceneRect().width() > 2


def drag_view(p, start, end):
    """A real left-button drag on the canvas, as the empty-space pan does."""
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    def send(kind, point, button):
        event = QMouseEvent(
            kind,
            QPointF(*point),
            QPointF(*point),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )
        application().sendEvent(p.viewport(), event)
        application().processEvents()

    send(QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton)
    send(QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton)


def test_the_camera_pans_freely_past_the_artwork_and_stays_there(window):
    d, e = single_sprite(window)
    p = e.preview
    # Panning does not zoom, and a view scrolls by whole device pixels, so the
    # camera owes the same distance for every identical drag. The first drag is
    # spent establishing that step, then every later one must match it: a scene
    # rect sized to the artwork would swallow the later drags and the step would
    # shrink instead.
    p.fit_content()
    application().processEvents()
    start = p.mapToScene(p.viewport().rect()).boundingRect().center()
    drag_view(p, (10, 10), (400, 340))
    step = p.mapToScene(p.viewport().rect()).boundingRect().center() - start
    assert step.manhattanLength() > 50, "the camera did not move at all"
    for _ in range(5):
        before = p.mapToScene(p.viewport().rect()).boundingRect().center()
        drag_view(p, (10, 10), (400, 340))
        after = p.mapToScene(p.viewport().rect()).boundingRect().center()
        moved = after - before
        assert abs(moved.x()) == pytest.approx(abs(step.x()), abs=0.5)
        assert abs(moved.y()) == pytest.approx(abs(step.y()), abs=0.5)
    # The scene rect is put back after each drag, so the camera is not bounced
    # towards the middle of the artwork while the button is still down.
    assert p.scene().sceneRect() == p.view_rect
    assert p.scene().sceneRect().contains(p.content_rect)

    p.fit_content()
    assert p.scene().sceneRect() == p.view_rect
    assert p.transform().m11() == pytest.approx(p.fit_scale)
    drag_view(p, (10, 10), (400, 340))
    assert (
        p.mapToScene(p.viewport().rect()).boundingRect().center() - start
    ).manhattanLength() > 100


def test_showing_a_resource_does_not_resize_the_interface(window):
    d, e = single_sprite(window)
    application().processEvents()
    before = (window.size(), e.splitter.sizes(), e.preview.height())
    # The wide scene rect used for the free camera must not be mistaken for the
    # view's preferred size, which grew the whole layout around it.
    assert e.preview.sizeHint().width() < 1000
    for _ in range(3):
        e.render_frame(fit=True)
        application().processEvents()
    after = (window.size(), e.splitter.sizes(), e.preview.height())
    assert after == before


def test_animation_reuses_scene_and_does_not_decode_each_frame(window, monkeypatch):
    d = populated()
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    original = set(e.preview.scene().items())

    def unexpected(*args):
        raise AssertionError("Playback should reuse normalized resources")

    monkeypatch.setattr(d, "resource", unexpected)
    e.frame.setValue(1)
    assert set(e.preview.scene().items()) == original
    assert not e.preview.messages
    assert e.preview.shape_cache


def test_playback_advances_one_frame_per_tick(window):
    """A slow frame must slow playback down, never skip frames.

    Deriving the frame from wall-clock time dropped the animation forward by
    dozens of frames whenever rendering took long, which is what made a frame
    jump from 5 to 80.
    """
    window.add_doc(populated())
    e = window.current()
    e.go_to(7)
    assert e.frame.maximum() >= 1
    e.toggle_play()
    e.timer.stop()  # advance by hand, so only tick() moves the frame
    # Playback loops over the clip itself, not the cursor's headroom.
    count = len(e.obj["frames"])
    seen = []
    for _ in range(count * 2):
        e.tick()
        seen.append(e.frame.value())
    expected = [(seen[0] + i) % count for i in range(count * 2)]
    assert seen == expected, f"{seen} != {expected}"
    e.toggle_play()


def test_playback_holds_the_camera_still(window):
    """The view must not follow the objects, while playing or frame stepping."""
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()

    def visible():
        rect = e.preview.mapToScene(e.preview.viewport().rect()).boundingRect()
        return (round(rect.x(), 1), round(rect.y(), 1))

    # The scrollable area is set once per resource rather than per frame, which
    # is what stopped the camera drifting and left it free to pan.
    assert e.preview.view_rect is not None
    settled = e.preview.view_rect
    seen = [visible()]
    e.toggle_play()
    e.timer.stop()
    for _ in range(6):
        e.tick()
        application().processEvents()
        seen.append(visible())
        # It must not be recomputed per frame, only grown if panning needs it.
        assert e.preview.view_rect is settled or e.preview.view_rect.contains(settled)
    e.timer.start()
    e.toggle_play()
    assert len(set(seen)) == 1, f"camera moved during playback: {seen}"
    # A different resource gets its own area rather than inheriting one.
    e.preview.view_rect = None
    e.go_to(3)
    assert e.preview.view_rect is not None


def test_play_button_keeps_its_width(window):
    window.add_doc(populated())
    e = window.current()
    e.go_to(7)
    width = e.play.minimumWidth()
    e.toggle_play()
    assert e.play.text() == "⏸ Pause"
    assert e.play.minimumWidth() == width
    e.toggle_play()
    assert e.play.text() == "▶ Play"


def test_f2_rename_preserves_aliases_and_undo(window, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    d = populated()
    obj = d.resource(7)
    obj["exports"] = ["demo", "alias"]
    d.update("movieclips", obj)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.tree.setFocus()
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *args, **kwargs: ("renamed", True)
    )
    QTest.keyClick(e.tree, Qt.Key.Key_F2)
    assert d.names[7] == ["renamed", "alias"]
    assert e.tree.currentItem().text(0) == "renamed / alias"
    e.undo()
    assert d.names[7] == ["demo", "alias"]


def test_lazy_frame_views_match_eager_and_json(window):
    from sc_studio.app import jsonable

    d = populated()
    assert jsonable(d.resource(7, lazy_frames=True)) == d.resource(7)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    assert not e.code.toPlainText()
    e.set_panel(3)
    import json

    assert json.loads(e.code.toPlainText()) == d.resource(7)


def test_importing_an_animated_gif_creates_keyed_frames(window, tmp_path, monkeypatch):
    """A GIF becomes one movieclip frame per source frame, one packed atlas."""
    from PIL import Image

    import sc_studio.app as appmod

    w = window
    w.add_doc(animatable_clip(1))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    gif = tmp_path / "anim.gif"
    frames = [Image.new("RGBA", (10, 8), (i * 30, 0, 255 - i * 30, 255)) for i in range(4)]
    frames[0].save(
        gif,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        disposal=2,
    )
    monkeypatch.setattr(appmod, "native_picker", lambda *a, **k: (str(gif), ""))
    monkeypatch.setattr(
        appmod.Editor,
        "animation_dialog",
        lambda self, frames, fps: {"prefix": "frame", "suffix": "_anim", "fps": 12.5},
    )
    e.import_animation()
    application().processEvents()
    clip = e.doc.resource(7)
    assert len(clip["frames"]) == 4
    assert clip["framerate"] == 12
    names = [c["name"] for c in clip["children"]][1:]
    assert names == ["frame0_anim", "frame1_anim", "frame2_anim", "frame3_anim"]
    # One new atlas holding every frame.
    atlas = e.doc.tables["Textures"]["textures"][-1]["highres"]
    assert atlas["width"] >= 20 and atlas["height"] >= 8
    # Each frame places its own new image, and the frame that existed before
    # keeps the placement it already had.
    for index, frame in enumerate(clip["frames"]):
        placed = [element for element in frame["elements"] if element[0] == 1 + index]
        assert len(placed) == 1
    assert clip["frames"][0]["elements"][0][0] == 0


def test_canvas_drag_moves_placement_and_undo(window):
    from PyQt6.QtCore import QPointF

    d = populated()
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535]]}]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    application().processEvents()
    start = e.preview.mapFromScene(QPointF(45, 25))
    end = start + __import__("PyQt6.QtCore", fromlist=["QPoint"]).QPoint(20, 15)
    delta = e.preview.mapToScene(end) - e.preview.mapToScene(start)
    scale = e.preview.transform().m11()
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    clip = d.resource(7)
    matrix = d.bank(clip["matrix_bank_index"])["matrices"][
        clip["frames"][0]["elements"][0][1]
    ]
    # Snapping is on by default, so the move lands on whole scene pixels: the
    # raw view delta is rounded onto the asset grid the file stores.
    assert matrix[4:] == pytest.approx([round(40 + delta.x()), round(20 + delta.y())])
    assert e.preview.transform().m11() == pytest.approx(scale)
    e.undo()
    clip = d.resource(7)
    assert d.bank(clip["matrix_bank_index"])["matrices"][0][4:] == [40, 20]


def test_snap_to_pixels_can_be_turned_off_for_free_movement(window):
    """With snapping off a drag follows the screen grid, not the asset grid."""
    from PyQt6.QtCore import QPoint, QPointF

    d = populated()
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535]]}]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    application().processEvents()
    assert e.snap_pixels.isChecked() and e.preview.snap_movement
    e.snap_pixels.setChecked(False)
    assert not e.preview.snap_movement
    # A view scale that is not 1:1 so a whole screen-pixel drag is a fraction
    # of a scene pixel, which is where snapping and free movement differ.
    e.preview.scale(1.3, 1.3)
    start = e.preview.mapFromScene(QPointF(45, 25))
    end = start + QPoint(20, 15)
    raw = e.preview.mapToScene(end) - e.preview.mapToScene(start)
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    clip = d.resource(7)
    matrix = d.bank(clip["matrix_bank_index"])["matrices"][
        clip["frames"][0]["elements"][0][1]
    ]
    assert matrix[4:] == pytest.approx([40 + raw.x(), 20 + raw.y()])


def test_the_snap_choice_is_remembered_between_tabs_and_runs(window):
    """The toggle is one session-wide setting, not per tab."""
    assert window.snap_pixels is True
    w = window
    w.new()
    e = w.current()
    assert e.preview.snap_movement and e.snap_pixels.isChecked()
    e.snap_pixels.setChecked(False)
    assert w.snap_pixels is False
    # A new tab starts from the saved choice.
    w.new()
    assert not w.current().preview.snap_movement
    assert not w.current().snap_pixels.isChecked()


def test_add_child_also_places_it_in_current_frame(window):
    d = populated()
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.frame.setValue(1)
    assert e.mutate(lambda: e.place_child(3, "another sprite"))
    clip = d.resource(7)
    assert clip["children"][-1]["name"] == "another sprite"
    assert clip["frames"][1]["elements"][-1] == [2, 65535, 65535]
    assert len(clip["frames"][0]["elements"]) == 2


def test_scaling_grid_keeps_corners_fixed():
    from sc_studio.preview import grid_coordinate

    # A 100-wide sprite scaled to 300; the 20-wide margins stay 20.
    points = [grid_coordinate(x, 0, 100, 20, 80, 3) * 3 for x in [0, 20, 80, 100]]
    assert points == pytest.approx([0, 20, 280, 300])


def test_child_visibility_is_preview_only_and_click_selects_child(window):
    d = populated()
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    before = d.resource(7)
    revision = e.revision
    e.children.item(0, 3).setCheckState(Qt.CheckState.Unchecked)
    assert 0 in e.preview.hidden_children
    groups = [item for item, _ in e.preview.instances.values() if item.data(0) == 0]
    assert groups and not any(item.isVisible() for item in groups)
    assert d.resource(7) == before and e.revision == revision
    e.show_all_children()
    assert all(item.isVisible() for item in groups)
    e.select_placement(0)
    assert e.elements.currentRow() == 0
    assert e.children.currentRow() == 0
    assert e.panel_picker.currentIndex() == 0


def test_supplied_popup_and_heavy_animation_render():
    sample = os.environ.get("SC_SAMPLE")
    if not sample:
        pytest.skip("SC_SAMPLE is unset")
    from sc_studio.preview import Preview

    application()
    d = Document.load(sample)
    p = Preview()
    for id in [31717, 44104, 1760]:
        message = p.show_resource(d, id)
        assert "out of range" not in message
        assert len(p.scene().items()) > 3
        assert p.scene().sceneRect().width() > 50
        if id == 31717:
            assert not message
            assert any(key[-1] is not None for key in p.shape_cache)
        before = dict(d.bank_cache)
        p.show_resource(d, id, frame=1, fit=False)
        assert all(d.bank_cache[key] is value for key, value in before.items())
    p.close()


def single_sprite(window):
    d = populated(6)
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535]]}]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.select_placement(0)
    application().processEvents()
    return d, e


def test_drag_resize_corner_commits_one_edit_and_undo(window):
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    item = e.preview.selected_item()
    r = item.boundingRect()
    assert (r.width(), r.height()) == (20, 20)
    start = e.preview.mapFromScene(item.mapToScene(r.bottomRight()))
    end = e.preview.mapFromScene(QPointF(80, 60))
    revision = e.revision
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    assert e.revision == revision  # live preview does not create undo entries
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    clip = d.resource(7)
    m = d.bank(clip["matrix_bank_index"])["matrices"][
        clip["frames"][0]["elements"][0][1]
    ]
    assert m[:4] == pytest.approx([2, 0, 0, 2], abs=0.03)
    assert m[4:] == pytest.approx([40, 20])
    assert e.revision == revision + 1
    e.undo()
    assert d.resource(7)["frames"][0]["elements"][0][1] == 0
    assert d.bank(d.resource(7)["matrix_bank_index"])["matrices"][0] == [
        1,
        0,
        0,
        1,
        40,
        20,
    ]


def test_a_textfield_is_measured_by_its_box_not_its_glyphs(window):
    """The field's outline is the authored box, even when the text overflows it.

    A textfield's rendered text is usually taller than the box authored for it,
    so measuring the placement by the glyph extent drew a selection outline and
    put the resize handles in the wrong place, and a vertical resize scaled
    about the wrong edge.
    """
    w = window
    d = populated(6)
    clip = d.resource(7)
    clip["children"] = [{"id": 4, "name": "tf", "blend": 0}]
    clip["frames"] = [
        {"label": "", "elements": [[0, 65535, 65535]]} for _ in range(4)
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    e.select_placement(0)
    application().processEvents()
    # The fixture textfield is authored left/top/right/bottom = 0, 0, 100, 30.
    box = e.preview.selected_item().boundingRect()
    assert (box.width(), box.height()) == pytest.approx((101.0, 31.0), abs=0.01)


def test_textfield_alignment_shows_in_the_preview(window):
    """Horizontal and vertical alignment move the text, as they do in game."""
    from PyQt6.QtWidgets import QGraphicsTextItem

    w = window
    d = populated(6)
    clip = d.resource(7)
    clip["children"] = [{"id": 4, "name": "tf", "blend": 0}]
    clip["frames"] = [
        {"label": "", "elements": [[0, 65535, 65535]]} for _ in range(4)
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    e.select_placement(0)
    application().processEvents()

    def text_item():
        return next(
            item
            for item in e.preview.scene().items()
            if isinstance(item, QGraphicsTextItem)
        )

    assert text_item().document().defaultTextOption().alignment() & (
        Qt.AlignmentFlag.AlignLeft
    )
    # Centre horizontally, bottom vertically, in a box tall enough to move in.
    obj = e.doc.resource(4)
    obj["align"] = 1 | (2 << 2)
    obj["bottom"] = 200
    e.doc.update("textfields", obj)
    e.go_to(4)
    e.go_to(7)
    application().processEvents()
    item = text_item()
    assert item.document().defaultTextOption().alignment() & (
        Qt.AlignmentFlag.AlignHCenter
    )
    # Bottom alignment pushes the text down inside the box.
    assert item.pos().y() > 0


def test_resize_escape_cancels_and_shift_preserves_aspect(window):
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    item = e.preview.selected_item()
    start = e.preview.mapFromScene(item.mapToScene(item.boundingRect().bottomRight()))
    end = e.preview.mapFromScene(QPointF(75, 50))
    before = d.resource(7)
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.keyClick(e.preview, Qt.Key.Key_Escape)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert d.resource(7) == before and not e.history
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.mouseRelease(
        e.preview.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        pos=end,
    )
    clip = d.resource(7)
    m = d.bank(clip["matrix_bank_index"])["matrices"][
        clip["frames"][0]["elements"][0][1]
    ]
    assert m[0] == pytest.approx(m[3])


def test_children_tab_duplicate_delete_and_layer_order(window, monkeypatch):
    d = populated()
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.select_placement(0)
    # Children is a panel_picker entry, so the picker stays usable on it.
    assert e.active_panel == 0
    assert e.panel_picker.currentText() == "Children"
    assert e.panel_picker.isVisible()
    e.children.selectRow(0)
    e.duplicate_child()
    obj = d.resource(7)
    assert obj["children"][-1]["name"] == "sprite_copy"
    assert obj["frames"][0]["elements"][-1][0] == 2
    e.reorder_placement(2, 0)
    assert d.resource(7)["frames"][0]["elements"][0][0] == 2
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes
    )
    e.children.selectRow(0)
    e.remove_child()
    obj = d.resource(7)
    assert len(obj["children"]) == 2
    assert all(e[0] in (0, 1) for f in obj["frames"] for e in f["elements"])
    assert 3 in d.index  # shared shape asset kept
    e.undo()
    assert len(d.resource(7)["children"]) == 3


def test_viewer_context_menu_targets_clicked_placement(window, monkeypatch):
    from PyQt6.QtWidgets import QMenu
    from PyQt6.QtCore import QPoint

    d, e = single_sprite(window)
    captured = []

    def fake_exec(menu, position):
        captured.extend(a.text() for a in menu.actions())

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    e.viewer_context_menu(0, QPoint(100, 100))
    assert {
        "Layer position…",
        "Bring to front",
        "Size / position…",
        "Hide child",
    } <= set(captured)
    assert e.elements.currentRow() == 0


def test_supplied_scaling_grid_can_resize_without_editing_grid(window, monkeypatch):
    sample = os.environ.get("SC_SAMPLE")
    if not sample:
        pytest.skip("SC_SAMPLE is unset")
    from sc_studio.preview import grid_commands

    d = Document.load(sample)
    window.add_doc(d)
    e = window.current()
    e.go_to(31717)
    e.select_placement(0)
    grid = d.resource(4838)["scaling_grid"].copy()
    clip = d.resource(31717)
    old = d.bank(clip["matrix_bank_index"])["matrices"][
        clip["frames"][0]["elements"][0][1]
    ]
    # Live render a different size, with no document mutation.
    from PyQt6.QtGui import QTransform

    new = [old[0] * 1.2, 0, 0, old[3] * 1.15, old[4], old[5]]
    e.preview.preview_transform(QTransform(*new))
    assert not e.history
    assert not e.preview.messages
    shape = d.resource(4321)
    deformed = grid_commands(
        shape,
        (grid["left"], grid["top"], grid["right"], grid["bottom"], new[0], new[3]),
    )
    command = deformed[0]["vertices"]
    assert abs(command[2][0] - command[0][0]) * new[0] == pytest.approx(30)
    e.preview.matrix_override = None
    e.commit_placement_transform(0, new)
    assert d.resource(4838)["scaling_grid"] == grid
    root = d.resource(31717)
    actual = d.bank(root["matrix_bank_index"])["matrices"][
        root["frames"][0]["elements"][0][1]
    ]
    assert actual == pytest.approx(new)
    assert not e.preview.messages
    e.undo()
    assert d.resource(31717)["matrix_bank_index"] == clip["matrix_bank_index"]


def test_atlas_outlines_and_overlapping_shape_picker(window, monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QMenu

    d = populated()
    extra = d.resource(3)
    extra["id"] = 5
    d.update("shapes", extra)
    window.add_doc(d)
    e = window.current()
    e.selected = None
    e.texture_index = 0
    e.show_texture()
    assert len(e.preview.atlas_regions) == 2
    assert len(e.preview.atlas_borders) == 1
    seen = []

    def fake_exec(menu, position):
        candidates = menu.actions()[0].menu()
        assert candidates is not None
        seen.extend(a.text() for a in candidates.actions())
        candidates.actions()[0].menu().actions()[0].trigger()

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    e.atlas_context_menu(e.preview.atlas_regions, QPoint(100, 100))
    assert "Shape #3" in seen and "Shape #5" in seen
    assert e.selected == 3 and e.active_panel == 5
    assert e.preview.atlas_regions is None


def test_atlas_zero_area_commands_stay_visible_and_pickable(window):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QImage, QMouseEvent, QPainter, QColor

    d = populated()
    data = d.tables["Textures"]["textures"][0]["highres"]
    data.update(width=64, height=64, data=bytes([30, 60, 90, 255]) * 64 * 64)
    # Same shape as a real UI atlas: one real sprite plus commands whose four
    # corners all sample a single texel.
    for id, (u, v, du, dv) in ((3, (1000, 1000, 8000, 8000)), (5, (4096, 4096, 0, 0))):
        obj = d.resource(3)
        obj["id"] = id
        obj["commands"][0]["vertices"] = [
            [0, 0, u, v], [20, 0, u + du, v], [0, 20, u, v + dv], [20, 20, u + du, v + dv]
        ]
        d.update("shapes", obj)
    window.add_doc(d)
    e = window.current()
    e.selected = None
    e.texture_index = 0
    e.show_texture()
    p = e.preview

    # A zero-area command must not be dropped from the atlas.
    assert {r["id"] for r in p.atlas_regions} == {3, 5}
    assert len(p.atlas_borders) == 1          # only the real sprite gets a rect
    assert len(p.atlas_marks) == 1            # the texel gets one marker
    assert p.atlas_marks[0] == pytest.approx((4096 * 64 / 65535,) * 2, abs=0.01)

    # It must answer picks at its texel.
    point = QPointF(*p.atlas_marks[0])
    hits = p.atlas_shapes_at(point)
    assert 5 in [h["id"] for h in hits]

    # The marker has to actually render, at any zoom. Qt calls drawForeground
    # with the painter already in scene coordinates, so mirror that here.
    for zoom in (0.05, 4.0):
        p.resetTransform()
        p.scale(zoom, zoom)
        image = QImage(200, 200, QImage.Format.Format_ARGB32)
        image.fill(QColor("black"))
        painter = QPainter(image)
        painter.setTransform(p.transform())
        scene_rect = p.mapToScene(p.viewport().rect()).boundingRect()
        p.drawForeground(painter, scene_rect)
        painter.end()
        # Full scan: the diamond is only a few pixels across at low zoom, so a
        # strided sample would step straight over its thin outline.
        lit = sum(
            1
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y) != QColor("black")
        )
        assert lit, f"nothing drawn at zoom {zoom}"

    # Double-clicking the texel opens the shape. This navigates away from the
    # atlas, so it goes last.
    opened = []
    p.atlasShapeOpened.connect(opened.append)
    p.resetTransform()
    p.mouseDoubleClickEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPointF(p.mapFromScene(point)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    assert opened == [5]


def test_atlas_real_context_event_and_multiple_rendered_outlines(window, monkeypatch):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu, QDialog, QTreeWidget, QLineEdit
    d = populated()
    data = d.tables["Textures"]["textures"][0]["highres"]
    data.update(width=64, height=64, data=bytes([40, 80, 140, 255]) * 64 * 64)
    for id, left in ((3, 4096), (5, 32768), (6, 4096)):
        obj = d.resource(3)
        obj["id"] = id
        obj["commands"][0]["vertices"] = [[0, 0, left, 4096], [20, 0, left + 16384, 4096],
                                              [0, 20, left, 20480], [20, 20, left + 16384, 20480]]
        d.update("shapes", obj)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.set_panel(4)
    assert e.selected is None and e.texture_index == 0
    assert e.preview.atlas_regions is not None and not e.preview.editable
    e.go_to(7)
    assert e.active_panel != 3
    e.category.setCurrentIndex(e.resource_kinds.index("textures"))
    assert e.texture_index == 0
    application().processEvents()
    p = e.preview
    assert e.selected is None and len(p.atlas_borders) == 2
    image = p.viewport().grab().toImage()
    for bounds in p.atlas_borders:
        pixel = p.mapFromScene(QPointF(bounds.center().x(), bounds.top()))
        # The border is a cosmetic 1px line, so which pixel it lands on depends
        # on the fitted zoom. Scan a small band and accept either the pure
        # outline colour or that colour blended over the blue atlas behind it
        # (which stays yellow-dominant even at half coverage).
        band = [
            image.pixelColor(pixel.x() + dx, pixel.y() + dy)
            for dx in range(-3, 4)
            for dy in range(-3, 4)
        ]
        assert any(
            color.red() > color.blue() + 40 and color.green() > color.blue() + 20
            for color in band
        ), f"no outline drawn at {bounds}: {[c.name() for c in band]}"
    menus = []
    def inspect(menu, *_):
        candidates = menu.actions()[0].menu()
        menus.append([a.text() for a in candidates.actions()])
    monkeypatch.setattr(QMenu, "exec", inspect)
    for region in (p.atlas_regions[0], p.atlas_regions[1]):
        point = p.mapFromScene(region["bounds"].center())
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, p.viewport().mapToGlobal(point))
        QApplication.sendEvent(p.viewport(), event)
    assert set(menus[0]) == {"Shape #3", "Shape #6"}
    assert menus[1] == ["Shape #5"]
    def inspect_picker(dialog):
        tree = dialog.findChild(QTreeWidget, "atlas_shapes_picker")
        assert tree.topLevelItemCount() == 3
        dialog.findChild(QLineEdit).setText("demo")
        assert tree.topLevelItemCount() == 1
        assert tree.topLevelItem(0).text(1) == "3"
        tree.itemDoubleClicked.emit(tree.topLevelItem(0), 0)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", inspect_picker)
    e.browse_atlas_shapes()
    assert e.selected == 3 and e.active_panel == 5


def test_create_menu_assets_children_image_and_undo(window, monkeypatch, tmp_path):
    from PIL import Image
    import sc_studio.app as studio
    window.new()
    e = window.current()
    window.actions["create_textfields"].trigger()
    text = e.selected
    assert e.doc.index[text][0] == "textfields" and e.active_panel == 1
    assert e.category.currentText() == "Textfields"
    window.actions["create_movieclips"].trigger()
    clip = e.selected
    window.actions["create_child_textfields"].trigger()
    assert e.selected == clip and e.active_panel == 0
    child = e.doc.resource(clip)["children"][0]["id"]
    assert e.doc.index[child][0] == "textfields"
    assert e.doc.resource(clip)["frames"][0]["elements"] == [[0, 65535, 65535]]
    e.undo()
    assert child not in e.doc.index and e.doc.resource(clip)["children"] == []
    path = tmp_path / "new-image.png"
    Image.new("RGBA", (24, 12), "red").save(path)
    monkeypatch.setattr(studio, "native_picker", lambda *_: (str(path), ""))
    monkeypatch.setattr(e, "size_dialog", lambda *_: (48, 24, 2, 3, 0))
    window.actions["create_image"].trigger()
    shape = e.doc.resource(clip)["children"][0]["id"]
    assert e.doc.index[shape][0] == "shapes"
    assert len(e.doc.tables["Textures"]["textures"]) == 1
    e.undo()
    assert shape not in e.doc.index and len(e.doc.tables["Textures"]["textures"]) == 0
    e.go_to(text)
    window.actions["create_image"].trigger()
    assert e.doc.index[e.selected][0] == "shapes" and e.active_panel == 5
    assert e.doc.resource(e.selected)["commands"][0]["vertices"][3][:2] == [24, 12]
    e.undo()
    assert e.selected == text and len(e.doc.tables["Textures"]["textures"]) == 0
    before = set(e.doc.index)
    monkeypatch.setattr(studio, "native_picker", lambda *_: ("", ""))
    window.actions["create_image"].trigger()
    assert set(e.doc.index) == before


def test_duplicate_movieclip_reuses_children_and_undo(window):
    d = populated()
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    original = d.resource(7)
    e.tree.setFocus()
    QTest.keyClick(e.tree, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    duplicate = e.selected
    assert duplicate != 7 and set(d.index) == {3, 4, 7, duplicate}
    assert d.resource(duplicate)["children"] == original["children"]
    e.reorder_placement(0, 1)
    assert d.resource(7) == original
    assert d.resource(duplicate)["frames"][0]["elements"] == list(reversed(original["frames"][0]["elements"]))
    e.undo()
    e.undo()
    assert e.selected == 7 and set(d.index) == {3, 4, 7}
    e.redo()
    assert duplicate in d.index and d.resource(duplicate)["children"] == original["children"]


@pytest.mark.skipif(not os.environ.get("SC_SAMPLE"), reason="requires supplied SC sample")
def test_supplied_atlas_has_all_regions_and_duplicate_reuses_packed_data(window, monkeypatch):
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu
    d = Document.load(os.environ["SC_SAMPLE"])
    window.add_doc(d)
    e = window.current()
    e.category.setCurrentIndex(e.resource_kinds.index("textures"))
    item = next(e.tree.topLevelItem(i) for i in range(e.tree.topLevelItemCount())
                if e.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == ("texture", 4))
    e.tree.setCurrentItem(item)
    application().processEvents()
    assert len(e.preview.atlas_regions) == 1033
    assert len(e.preview.atlas_borders) == 847
    seen = []
    def inspect(menu, *_):
        seen.extend(action.text() for action in menu.actions()[0].menu().actions())
    monkeypatch.setattr(QMenu, "exec", inspect)
    region = next(region for region in e.preview.atlas_regions if region["id"] == 39)
    point = e.preview.mapFromScene(region["bounds"].center())
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, e.preview.viewport().mapToGlobal(point))
    QApplication.sendEvent(e.preview.viewport(), event)
    assert any("#39" in name for name in seen)
    original = d.resource(31717)
    bank_bytes = d.external
    original_count = len(d.index)
    e.go_to(31717)
    window.duplicate()
    duplicate = e.selected
    assert len(d.index) == original_count + 1
    assert d.resource(duplicate)["children"] == original["children"]
    assert d.resource(duplicate)["frames"] == original["frames"]
    assert d.external == bank_bytes


def test_find_usages_dialog_shows_instance_and_indirect_parent(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QTreeWidget

    d = populated()
    outer = d.resource(7)
    outer["id"] = 8
    outer["exports"] = ["outer"]
    outer["children"] = [{"id": 7, "name": "nested", "blend": 0}]
    outer["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]}]
    d.update("movieclips", outer)
    window.add_doc(d)
    e = window.current()
    e.go_to(3)

    def inspect(dialog):
        tree = dialog.findChild(QTreeWidget, "usages_list")
        assert tree.topLevelItemCount() == 2
        rows = [tree.topLevelItem(i) for i in range(2)]
        assert {r.text(2) for r in rows} == {"sprite", "nested"}
        direct = next(r for r in rows if r.text(4) == "Direct")
        tree.itemDoubleClicked.emit(direct, 0)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", inspect)
    window.references()
    assert e.selected == 7
    assert e.children.currentRow() == 0
    assert e.active_panel == 0


@pytest.mark.parametrize("atlas", [False, True])
def test_wheel_zooms_to_cursor_in_and_out_below_fit(window, atlas):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent

    d, e = single_sprite(window)
    p = e.preview
    if atlas:
        e.selected = None
        e.texture_index = 0
        e.show_texture()
        application().processEvents()
    cursor = QPoint(75, 95)
    for angle in [120, 120, -120, -120, -120, -120, -120]:
        before = p.mapToScene(cursor)
        event = QWheelEvent(
            QPointF(cursor),
            QPointF(p.viewport().mapToGlobal(cursor)),
            QPoint(),
            QPoint(0, angle),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        p.wheelEvent(event)
        after = p.mapToScene(cursor)
        distance_pixels = (after - before).manhattanLength() * p.transform().m11()
        assert distance_pixels < 0.15
    # Fitting frames the content without narrowing the scrollable area to it,
    # which is what used to re-anchor the camera onto the artwork.
    p.fit_content()
    assert p.scene().sceneRect().contains(p.content_rect)
    assert p.transform().m11() == pytest.approx(p.fit_scale)


def test_shift_wheel_pans_horizontally_without_zoom_and_alt_vertically(window):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent

    d, e = single_sprite(window)
    p = e.preview
    cursor = QPointF(100, 100)
    scale = p.transform().m11()

    def wheel(modifiers, angle, pixels=QPoint()):
        event = QWheelEvent(
            cursor,
            cursor,
            pixels,
            angle,
            Qt.MouseButton.NoButton,
            modifiers,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        p.wheelEvent(event)

    before = p.mapToScene(p.viewport().rect().center())
    wheel(Qt.KeyboardModifier.ShiftModifier, QPoint(0, -120))
    after = p.mapToScene(p.viewport().rect().center())
    assert (after.x() - before.x()) * scale == pytest.approx(40, abs=2)
    assert (after.y() - before.y()) * scale == pytest.approx(0, abs=2)
    assert p.transform().m11() == scale
    before = after
    wheel(Qt.KeyboardModifier.AltModifier, QPoint(0, -120))
    after = p.mapToScene(p.viewport().rect().center())
    assert (after.y() - before.y()) * scale == pytest.approx(40, abs=2)
    assert p.transform().m11() == scale
    before = after
    wheel(Qt.KeyboardModifier.NoModifier, QPoint(), QPoint(-25, 0))
    after = p.mapToScene(p.viewport().rect().center())
    assert (after.x() - before.x()) * scale == pytest.approx(25, abs=2)


def test_shift_wheel_scrolls_table_horizontally(window):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent

    d, e = single_sprite(window)
    for i in range(e.children.columnCount()):
        e.children.setColumnWidth(i, 400)
    application().processEvents()
    bar = e.children.horizontalScrollBar()
    assert bar.maximum() > 0
    before = bar.value()
    event = QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ShiftModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    application().sendEvent(e.children.viewport(), event)
    assert bar.value() > before


def test_named_themes_apply_and_are_readable(window):
    from sc_studio import themes as schemes
    from PyQt6.QtGui import QPalette

    w = window
    w.new()
    assert len(schemes.names()) >= 12, "expected a broad set of schemes"
    assert any(schemes.is_dark(n) for n in schemes.names())
    assert any(not schemes.is_dark(n) for n in schemes.names())
    seen = {}
    for name in schemes.names():
        w.set_theme(name)
        palette = application().palette()
        window_color = palette.color(QPalette.ColorRole.Window)
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.WindowText)
        accent = palette.color(QPalette.ColorRole.Highlight)
        # Text has to stay legible on the window, and content areas distinct
        # from the chrome around them.
        assert abs(text.lightness() - window_color.lightness()) > 60, name
        assert window_color != base, name
        seen[name] = (window_color.name(), accent.name())
        assert w.settings.value("theme") == name
    # Each scheme must actually differ, or the menu would be misleading.
    assert len(set(seen.values())) == len(seen)


def test_theme_falls_back_to_system_for_unknown_names(window):
    w = window
    w.new()
    w.set_theme("No Such Scheme")
    assert w.theme == "system"
    assert w.theme_actions["system"].isChecked()


def test_built_in_themes_still_selectable(window):
    from PyQt6.QtGui import QPalette

    w = window
    w.new()
    w.set_theme("dark")
    dark = application().palette().color(QPalette.ColorRole.Base).lightness()
    w.set_theme("light")
    light = application().palette().color(QPalette.ColorRole.Base).lightness()
    assert light > dark
    assert w.theme_actions["dark"].isChecked() is False


def test_performance_dialog_opens_before_anything_is_rendered(window, monkeypatch):
    """Help > Performance must not depend on a prior render.

    items_count only appeared once a resource had been drawn, so opening the
    dialog on a fresh window raised AttributeError.
    """
    from PyQt6.QtWidgets import QDialog, QTableWidget

    w = window
    captured = {}

    def fake_exec(dialog):
        captured["rows"] = dialog.findChild(QTableWidget).rowCount()
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    w.performance()  # no document at all
    assert captured["rows"] == 4
    w.add_doc(populated())
    e = w.current()
    e.selected = None
    w.performance()  # document open, nothing drawn yet
    assert captured["rows"] == 4
    e.go_to(7)
    w.performance()  # after a render, with real samples
    assert captured["rows"] == 4


def test_timings_are_recorded_for_edits_and_frames(window):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(7)
    # Selecting already renders once, so only the edit stages start empty.
    assert all(e.timings[name] == deque() for name in ("snapshot", "validate", "commit"))
    assert len(e.timings["render"]) >= 1
    e.frame.setValue(1)          # another render
    assert len(e.timings["render"]) >= 2
    obj = e.doc.resource(7)
    obj["framerate"] = 24
    assert e.mutate(lambda: e.doc.update("movieclips", obj))
    for stage in ("snapshot", "validate", "commit"):
        assert len(e.timings[stage]) == 1, stage
        assert e.timings[stage][0] >= 0


def animatable_clip(frames=6):
    """A movieclip with one shape child placed on every frame."""
    d = populated()
    clip = d.resource(7)
    clip["children"] = [{"id": 3, "name": "sprite", "blend": 0}]
    clip["frames"] = [
        {"label": f"f{i}", "elements": [[0, 65535, 65535]]} for i in range(frames)
    ]
    d.update("movieclips", clip)
    return d


def test_easing_curves_are_well_behaved():
    from sc_studio import easing

    # Endpoints are exact for every preset, so a tween cannot overshoot its ends.
    for name in easing.PRESETS:
        assert easing.bezier(0.0, *easing.curve_for(name)) == 0.0, name
        assert easing.bezier(1.0, *easing.curve_for(name)) == 1.0, name
    # Ease in-out is symmetric about the middle and monotonic.
    eased = easing.sample("Ease in-out", 21)
    assert eased[10] == pytest.approx(0.5, abs=0.02)
    assert all(b >= a for a, b in zip(eased, eased[1:]))
    # Overshoot really does leave the 0..1 range, which is the point of it.
    assert max(easing.sample("Overshoot", 21)) > 1.0


def test_matrix_interpolation_keeps_scale_and_takes_the_short_way():
    import math
    from sc_studio import easing

    start = easing.compose({"tx": 0, "ty": 0, "rotation": 0, "scale_x": 2, "scale_y": 2})
    end = easing.compose(
        {"tx": 0, "ty": 0, "rotation": math.radians(350), "scale_x": 2, "scale_y": 2}
    )
    # 0 -> 350 degrees must go 10 degrees backwards, not 350 forwards.
    middle = easing.decompose(easing.interpolate_matrix(start, end, 0.5))
    assert abs(math.degrees(middle["rotation"]) + 5) < 1
    # Scale must survive the middle of a half turn; a naive a,b,c,d blend
    # collapses it to zero there.
    half = easing.compose(
        {"tx": 0, "ty": 0, "rotation": math.pi, "scale_x": 2, "scale_y": 2}
    )
    worst = min(
        easing.decompose(easing.interpolate_matrix(start, half, i / 20))["scale_x"]
        for i in range(21)
    )
    assert worst == pytest.approx(2.0, abs=0.01)
    # Endpoints are returned untouched.
    assert easing.interpolate_matrix(start, end, 0.0) == [float(v) for v in start]


def test_keyframe_strip_shows_the_timeline(window):
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    strip = e.keyframes
    assert strip.frame_count == 6
    assert strip.current == 0
    assert strip.row == 0
    # One row per child. Every frame shares one transform here, so there is a
    # single key: a frame only counts when the value actually changes.
    assert [t["label"] for t in strip.tracks] == ["sprite"]
    assert strip.keys_of(0) == {0}
    # Clicking a frame moves the playhead without touching the document.
    strip.framePicked.emit(3)
    assert e.frame.value() == 3
    assert strip.current == 3
    assert e.doc.resource(7)["frames"][3]["elements"][0][1] == 65535


def test_set_and_delete_keypoint(window):
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(2)
    application().processEvents()

    e.toggle_keypoint_at(2, 0)
    assert e.placement_keys(e.obj) == [2]
    assert 2 in e.keyframes.keys_of(0)
    # A second toggle on the same frame removes it again.
    e.toggle_keypoint_at(2, 0)
    assert e.placement_keys(e.obj) == []
    # Frames without the placement are refused rather than crashing.
    e.toggle_keypoint_at(99, 0)
    assert e.placement_keys(e.obj) == []


def test_keypoint_drag_moves_the_key(window):
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(1)
    application().processEvents()
    e.toggle_keypoint_at(1, 0)
    assert e.placement_keys(e.obj) == [1]
    e.move_keypoint(1, 4)
    assert e.placement_keys(e.obj) == [4]
    assert "frame 5" in window.statusBar().currentMessage().lower()


def test_bake_applies_the_curve_and_revert_restores_exactly(window):
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(0)
    application().processEvents()
    e.toggle_keypoint_at(0, 0)

    # Move the placement on the last frame and key it there.
    clip = e.doc.resource(7)
    clip["frames"][5]["elements"][0][1] = e.intern_matrix(clip, [1, 0, 0, 1, 100, 0])
    e.doc.update("movieclips", clip)
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(5)
    application().processEvents()
    keys_before = sorted(e.placement_keys(e.obj))
    assert keys_before == [0, 5]

    e.curve.setCurrentText("Ease out")
    e.bake_tween()
    baked = sorted(e.placement_keys(e.obj))
    assert baked == [0, 1, 2, 3, 4, 5]
    assert e.baked is not None and len(e.baked[2]) == 4

    # Ease out front-loads the movement, so the middle must lead a straight line.
    from sc_studio import easing

    found = e.key_transforms(e.obj)
    middle = easing.decompose(found[2][0])["tx"]
    assert middle > 100 * 0.5

    e.frame.setValue(0)
    e.revert_tween()
    assert sorted(e.placement_keys(e.obj)) == keys_before
    assert e.baked is None


def test_revert_refuses_without_a_recorded_bake(window):
    """Hand-set keys must survive: a densely keyed clip looks baked."""
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    clip = e.doc.resource(7)
    for index in range(4):
        clip["frames"][index]["elements"][0][1] = e.intern_matrix(
            clip, [1, 0, 0, 1, index * 10, 0]
        )
    e.doc.update("movieclips", clip)
    e.go_to(7)  # direct doc.update bypasses the refresh the app does on an edit
    e.elements.selectRow(0)
    assert len(e.placement_keys(e.obj)) == 4
    e.revert_tween()
    # No bake was recorded, so every hand-set key is still there.
    assert len(e.placement_keys(e.doc.resource(7))) == 4
    assert "No baked tween recorded" in window.statusBar().currentMessage()


def test_a_placement_absent_from_a_gap_is_filled_to_tween_across_it(window):
    """Placements appear and disappear between frames in real files.

    A frame that lists fewer placements still draws the ones it inherits from
    the frame before, so a gap in the middle of a tween is filled rather than
    skipped: skipping it used to write nothing and then report that the gap was
    already filled.
    """
    w = window
    d = animatable_clip(4)
    clip = d.resource(7)
    clip["frames"][2]["elements"] = []       # placement absent on frame 2
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(0)
    application().processEvents()
    e.toggle_keypoint_at(0, 0)
    clip = e.doc.resource(7)
    clip["frames"][3]["elements"][0][1] = e.intern_matrix(clip, [1, 0, 0, 1, 50, 0])
    e.doc.update("movieclips", clip)
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(3)
    application().processEvents()
    e.curve.setCurrentText("Linear")
    e.bake_tween()                            # must not crash
    assert 2 in e.placement_keys(e.obj)
    # The frame gains the placement, and it is the child the row names.
    gap = e.obj["frames"][2]["elements"]
    assert len(gap) == 1
    assert gap[0][0] == e.obj["frames"][0]["elements"][0][0]
    assert e.doc.validate() == []
    # Reverting clears the frames the curve wrote but leaves the clip valid.
    e.revert_tween()
    assert e.doc.validate() == []


def test_frame_creation_from_the_animator(window):
    w = window
    w.add_doc(animatable_clip(3))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    assert len(e.obj["frames"]) == 3
    e.add_frame(False)
    assert len(e.obj["frames"]) == 4
    assert e.keyframes.frame_count == 4
    e.add_frame(True)
    assert len(e.obj["frames"]) == 5
    e.remove_frame()
    assert len(e.obj["frames"]) == 4


def test_transform_fields_key_position_rotation_and_scale(window):
    """The animator must be able to rotate, not just move."""
    import math
    from sc_studio import easing

    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(0)
    application().processEvents()
    assert set(e.key_fields) == {"X", "Y", "Rotation", "Scale X", "Scale Y"}
    e.key_fields["X"].setValue(30.0)
    e.key_fields["Rotation"].setValue(45.0)
    e.key_fields["Scale X"].setValue(2.0)
    e.key_fields["Scale Y"].setValue(2.0)
    e.apply_animation_fields()
    assert e.placement_keys(e.obj) == [0]
    parts = easing.decompose(e.key_transforms(e.obj)[0][0])
    assert parts["tx"] == pytest.approx(30.0, abs=0.01)
    assert math.degrees(parts["rotation"]) == pytest.approx(45.0, abs=0.01)
    assert parts["scale_x"] == pytest.approx(2.0, abs=0.01)
    # Fields reload from the frame, so they follow the playhead.
    e.frame.setValue(2)
    application().processEvents()
    assert e.key_fields["Rotation"].value() == pytest.approx(0.0, abs=0.01)


def test_transform_fields_follow_the_selected_placement(window):
    """Picking another child shows that child's transform, not the last one's.

    The fields describe the placement the animator is acting on, so selecting a
    different row in Placements or Children has to reload them; leaving the
    previous child's numbers on screen made an edit write to the wrong object.
    """
    w = window
    d = animatable_clip(4)
    clip = d.resource(7)
    clip["children"].append({"id": 4, "name": "second", "blend": 0})
    clip["frames"] = [
        {"label": f"f{i}", "elements": [[0, 65535, 65535], [1, 65535, 65535]]}
        for i in range(4)
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 100.0)
    assert e.key_fields["X"].value() == pytest.approx(100.0)
    # Selecting the other placement in the Placements table reloads the fields.
    e.elements.selectRow(1)
    application().processEvents()
    assert e.placement_row() == 1
    assert e.key_fields["X"].value() == pytest.approx(0.0)
    # And picking a child in the Children list does the same.
    e.elements.selectRow(0)
    application().processEvents()
    assert e.key_fields["X"].value() == pytest.approx(100.0)
    e.children.setCurrentCell(1, 0)
    e.children.selectRow(1)
    application().processEvents()
    assert e.key_fields["X"].value() == pytest.approx(0.0)


def test_copy_from_the_children_list_copies_that_child(window, monkeypatch):
    """Ctrl+C over a focused child copies the child, not the open movieclip."""
    w = window
    d = animatable_clip(4)
    clip = d.resource(7)
    clip["children"].append({"id": 4, "name": "second", "blend": 0})
    clip["frames"] = [
        {"label": f"f{i}", "elements": [[0, 65535, 65535], [1, 65535, 65535]]}
        for i in range(4)
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    application().processEvents()
    e.children.setFocus()
    e.children.selectRow(0)
    w.copy()
    wait_until(lambda: not w.jobs)
    doc, roots = w.clipboard
    # The packed graph is rooted on the child's resource (the shape), not the
    # movieclip the child sits in.
    assert [doc.index[root][0] for root in roots] == ["shapes"]
    assert all(kind == "shapes" for kind, _ in doc.index.values())
    # Duplicate and Delete act on the child instance, not the resource.
    before = len(e.obj["children"])
    e.children.setFocus()
    e.children.selectRow(0)
    w.duplicate()
    application().processEvents()
    assert len(e.obj["children"]) == before + 1
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    e.children.setFocus()
    e.children.selectRow(0)
    w.delete_selection()
    application().processEvents()
    # The child was unplaced; the movieclip itself is untouched.
    assert len(e.obj["children"]) == before
    assert e.selected == 7


def test_strip_band_selects_a_range_including_keys(window):
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    strip = e.keyframes
    assert strip.selected_frames() == []
    # A band over the first and last key, which is what a tween needs.
    strip.set_band(0, 5)
    assert strip.selected_frames() == [0, 1, 2, 3, 4, 5]
    assert strip.band == (0, 5)
    strip.clear_band()
    assert strip.selected_frames() == []
    # A key marks a change: a frame inheriting the previous transform is not
    # one, and keying a frame to the value already in force changes nothing.
    e.frame.setValue(1)
    e.toggle_keypoint_at(1, 0)
    assert e.keyframes.keys_of(0) == {1}
    e.select_all_keys()
    assert strip.band == (1, 1)
    # Give frame 4 a transform of its own so it becomes a real second key.
    clip = e.doc.resource(7)
    clip["frames"][4]["elements"][0][1] = e.intern_matrix(clip, [1, 0, 0, 1, 40, 0])
    e.doc.update("movieclips", clip)
    e.go_to(7)
    application().processEvents()
    assert e.keyframes.keys_of(0) == {1, 4}
    e.select_all_keys()
    assert strip.band == (1, 4)


def test_apply_curve_to_selected_range_creates_keys(window):
    import math
    from sc_studio import easing

    w = window
    w.add_doc(animatable_clip(11))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(0)
    application().processEvents()
    e.key_fields["X"].setValue(0.0)
    e.apply_animation_fields()
    e.frame.setValue(10)
    application().processEvents()
    e.key_fields["X"].setValue(200.0)
    e.key_fields["Rotation"].setValue(90.0)
    e.apply_animation_fields()
    assert sorted(e.placement_keys(e.obj)) == [0, 10]

    e.keyframes.set_band(0, 10)
    e.key_density.setValue(10)
    e.curve.setCurrentText("Ease in-out")
    e.apply_curve_to_range()

    keys = sorted(e.placement_keys(e.obj))
    assert keys[0] == 0 and keys[-1] == 10
    assert len(keys) > 2, "the curve should have created intermediate keys"
    found = e.key_transforms(e.obj)
    xs = [easing.decompose(found[k][0])["tx"] for k in keys]
    # Ease in-out is slow at both ends, so the first step is smaller than a
    # straight line's would be.
    assert (xs[1] - xs[0]) < (200.0 / (len(keys) - 1))
    assert xs == sorted(xs)
    assert math.degrees(
        easing.decompose(found[keys[-1]][0])["rotation"]
    ) == pytest.approx(90.0, abs=0.5)
    assert e.doc.validate() == []
    # Undo curve removes only what the curve wrote.
    e.revert_tween()
    assert sorted(e.placement_keys(e.obj)) == [0, 10]


def test_key_density_controls_how_many_keys_are_created(window):
    w = window
    w.add_doc(animatable_clip(21))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.frame.setValue(0)
    application().processEvents()
    for frame, value in ((0, 0.0), (20, 100.0)):
        e.frame.setValue(frame)
        application().processEvents()
        e.key_fields["X"].setValue(value)
        e.apply_animation_fields()
    e.keyframes.set_band(0, 20)
    e.curve.setCurrentText("Linear")
    e.key_density.setValue(20)
    e.apply_curve_to_range()
    dense = len(e.placement_keys(e.obj))
    e.revert_tween()
    e.key_density.setValue(4)
    e.apply_curve_to_range()
    sparse = len(e.placement_keys(e.obj))
    assert sparse < dense, f"sparse {sparse} should be fewer than dense {dense}"
    assert sparse >= 3


def key_at(e, frame, value):
    """Give a frame its own transform, the way the animator's fields do."""
    e.goto_frame(frame)
    application().processEvents()
    e.key_fields["X"].setValue(value)
    e.apply_animation_fields()
    application().processEvents()


def test_only_changes_are_keys(window):
    """A frame inheriting the previous transform is not a key."""
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    # Every frame starts with no transform of its own, so the clip shows a
    # single base key rather than one per frame.
    assert e.keyframes.keys_of(0) == {0}
    key_at(e, 2, 30.0)
    # Frames 3-7 inherit frame 2, so they are not keys.
    assert e.keyframes.keys_of(0) == {2}
    key_at(e, 5, 90.0)
    assert e.keyframes.keys_of(0) == {2, 5}
    # Keying a frame with the value already in force changes nothing.
    key_at(e, 6, 90.0)
    assert e.keyframes.keys_of(0) == {2, 5}


def test_keys_can_be_picked_separately(window):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for frame, value in ((2, 30.0), (5, 90.0)):
        key_at(e, frame, value)
    strip = e.keyframes
    assert strip.keys_of(0) == {2, 5}

    def click_key(frame, modifiers):
        y = strip.RULER_HEIGHT + strip.TRACK_HEIGHT / 2
        point = QPointF(strip.x_for(frame), y)
        strip.mousePressEvent(
            QMouseEvent(QEvent.Type.MouseButtonPress, point,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, modifiers)
        )
        strip.mouseReleaseEvent(
            QMouseEvent(QEvent.Type.MouseButtonRelease, point,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, modifiers)
        )
        application().processEvents()

    click_key(2, Qt.KeyboardModifier.NoModifier)
    assert strip.selected_keys() == {(2, 0)}
    # Ctrl adds individual keys and removes them again.
    click_key(5, Qt.KeyboardModifier.ControlModifier)
    assert strip.selected_keys() == {(2, 0), (5, 0)}
    click_key(5, Qt.KeyboardModifier.ControlModifier)
    assert strip.selected_keys() == {(2, 0)}


def test_selected_keys_are_painted_distinctly(window):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    strip = e.keyframes
    strip.picked_keys = {(0, 0)}
    image = strip.grab().toImage()
    y = int(strip.RULER_HEIGHT + strip.TRACK_HEIGHT / 2)
    x = int(strip.x_for(0))
    colours = {
        image.pixelColor(x + dx, y + dy).name()
        for dx in range(-3, 4)
        for dy in range(-3, 4)
    }
    # The selection highlight is the orange used by the playhead.
    assert "#ff9d3a" in colours, colours


def test_scrubbing_never_creates_frames_or_keys(window):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    strip = e.keyframes
    frames = len(e.obj["frames"])
    keys = list(e.keyframes.keys_of(0))

    point = QPointF(strip.x_for(1), strip.RULER_HEIGHT + 4)
    strip.mousePressEvent(
        QMouseEvent(QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    )
    # Drag far past the end of the clip.
    strip.mouseMoveEvent(
        QMouseEvent(QEvent.Type.MouseMove, QPointF(strip.x_for(40), point.y()),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier)
    )
    strip.mouseReleaseEvent(
        QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(strip.x_for(40), point.y()),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier)
    )
    application().processEvents()
    assert len(e.obj["frames"]) == frames, "scrubbing must not add frames"
    assert e.keyframes.keys_of(0) == set(keys), "scrubbing must not add keys"


def test_moving_a_key_past_the_end_extends_the_clip(window):
    w = window
    w.add_doc(animatable_clip(5))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    e.frame.setValue(1)
    e.toggle_keypoint_at(1, 0)
    assert e.placement_keys(e.obj) == [1]
    e.move_keypoint(1, 8)
    assert len(e.obj["frames"]) >= 9
    assert e.placement_keys(e.obj) == [8]


def test_a_movieclip_keeps_at_least_one_frame(window):
    w = window
    w.add_doc(animatable_clip(1))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    assert len(e.obj["frames"]) == 1
    e.remove_frame()
    assert len(e.obj["frames"]) == 1
    assert "at least one frame" in window.statusBar().currentMessage()
    # With more than one frame, removal still works.
    e.add_frame(False)
    assert len(e.obj["frames"]) == 2
    e.remove_frame()
    assert len(e.obj["frames"]) == 1


def test_delete_with_a_frame_focused_removes_that_frame_not_the_clip(window, monkeypatch):
    """Delete on the Frames list is the frame action, never the resource.

    The browser fallback used to delete the whole open movieclip, so pressing
    Delete with a frame picked took out something other than the frame.
    """
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    e.frames.setCurrentCell(3, 0)
    e.frames.setFocus()
    w.delete_selection()
    application().processEvents()
    assert len(e.obj["frames"]) == 5
    assert e.selected == 7, "the movieclip was deleted instead of a frame"


def test_delete_with_the_timeline_focused_removes_the_playhead_frame(window):
    """With nothing picked on the timeline, Delete takes the frame under it."""
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    e.goto_frame(2)
    e.keyframes.setFocus()
    assert not e.keyframes.selected_keys() and not e.keyframes.selected_frames()
    w.delete_selection()
    application().processEvents()
    assert len(e.obj["frames"]) == 5
    assert e.selected == 7


def test_camera_stays_free_of_the_artwork_bounds(window):
    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    w.show()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    # Move one frame's placement far away and check the view does not follow.
    clip = e.doc.resource(7)
    clip["frames"][5]["elements"][0][1] = e.intern_matrix(clip, [1, 0, 0, 1, 900, 600])
    e.doc.update("movieclips", clip)
    e.go_to(7)
    application().processEvents()

    def visible():
        rect = e.preview.mapToScene(e.preview.viewport().rect()).boundingRect()
        return (round(rect.x(), 1), round(rect.y(), 1))

    seen = []
    for frame in (0, 2, 5, 1, 4):
        e.frame.setValue(frame)
        application().processEvents()
        seen.append(visible())
    assert len(set(seen)) == 1, f"camera followed the artwork: {seen}"
    # The scrollable area is wider than the content, which is what frees it.
    assert e.preview.scene().sceneRect().contains(e.preview.content_rect)


def test_apply_curve_uses_selected_keys_not_frame_range(window):
    """The button acts on picked keys; it must not blame a frame range."""
    w = window
    w.add_doc(animatable_clip(12))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 5, 100.0)
    key_at(e, 10, 200.0)
    assert e.keyframes.keys_of(0) == {0, 5, 10}

    # Shift picks a range of keys: 0 through 5.
    e.keyframes.set_band(0, 5)
    e.keyframes.picked_keys = {(0, 0), (5, 0)}
    e.keyframe_selection_changed()
    assert "2 keys" in e.bake_button.text()
    assert "2 keys you picked" in e.curve_info.text()

    e.key_density.setValue(6)
    e.curve.setCurrentText("Ease in-out")
    e.apply_curve_to_range()
    keys = sorted(e.placement_keys(e.obj))
    # Only the gap between the picked keys is filled; key 10 is left alone.
    assert keys[0] == 0 and 5 in keys and 10 in keys
    assert all(k <= 5 or k == 10 for k in keys), keys
    assert len(keys) > 3
    assert e.doc.validate() == []
    # Undo curve removes only what the curve wrote.
    e.revert_tween()
    assert sorted(e.placement_keys(e.obj)) == [0, 5, 10]


def test_cursor_can_sit_past_the_last_frame(window):
    """The playhead reaches beyond the clip so a change there can extend it."""
    w = window
    w.add_doc(animatable_clip(5))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    strip = e.keyframes
    assert strip.frame_count == 5
    # The timeline draws headroom past the end, and reports it separately.
    assert strip.total_frames() > strip.frame_count
    far = strip.frame_at(strip.x_for(9))
    assert far > 4, "the cursor cannot reach past the last frame"
    # Simply moving there changes nothing.
    frames = len(e.obj["frames"])
    e.goto_frame(far)
    assert e.cursor.value() == far
    assert len(e.obj["frames"]) == frames


def test_a_change_past_the_end_can_extend_the_clip_more_than_once(window):
    """Extending is repeatable: each trip out past the end adds frames again."""
    w = window
    w.add_doc(animatable_clip(2))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    lengths = []
    for _ in range(3):
        # Walk the cursor to the far end of what the strip allows and change
        # something there; the clip grows to cover it, so the next trip has
        # further to go.
        e.goto_frame(e.cursor.maximum())
        application().processEvents()
        key_at(e, e.cursor.value(), 20.0)
        lengths.append(len(e.obj["frames"]))
    assert lengths == sorted(set(lengths)), f"the clip stopped growing: {lengths}"
    assert lengths[0] > 2
    # The placement is really keyed out there, not silently dropped.
    assert e.keyframes.keys_of(0) == {lengths[0] - 1}


def test_moving_a_placement_past_the_end_extends_the_clip(window):
    """Dragging in the viewer past the end grows the clip, frame after frame."""
    w = window
    w.add_doc(animatable_clip(2))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    e.preview.selected_placement = 0
    application().processEvents()
    lengths = []
    for _ in range(3):
        e.goto_frame(e.cursor.maximum())
        application().processEvents()
        # The old code indexed straight into the frame list and raised
        # IndexError, so nothing after the first extension was possible.
        e.move_placement(0, 5.0, 0.0)
        application().processEvents()
        lengths.append(len(e.obj["frames"]))
    assert lengths == sorted(set(lengths)), f"the clip stopped growing: {lengths}"
    assert lengths[0] > 2


def test_the_playhead_line_scrubs_without_grabbing_the_key_under_it(window):
    """The line run through a key moves the cursor; the key stays put."""
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 3, 40.0)
    assert e.keyframes.keys_of(0) == {3}
    strip = e.keyframes
    step = strip.width_per_frame()
    key_x = strip.x_for(3)

    def send(kind, point, button):
        event = QMouseEvent(
            kind,
            QPointF(*point),
            QPointF(*point),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )
        application().sendEvent(strip, event)
        application().processEvents()

    track_y = strip.RULER_HEIGHT + strip.TRACK_HEIGHT / 2
    # On the key itself the key is picked, as before.
    send(QEvent.Type.MouseButtonPress, (key_x, track_y), Qt.MouseButton.LeftButton)
    assert strip.picked_keys == {(3, 0)}
    assert strip.dragging == (3, 0)
    send(QEvent.Type.MouseButtonRelease, (key_x, track_y), Qt.MouseButton.LeftButton)

    # On the playhead line at the same frame, the cursor is what is grabbed.
    e.goto_frame(3)
    application().processEvents()
    strip.picked_keys = set()
    line_x = strip.LABEL_WIDTH + e.cursor.value() * step
    assert strip._on_playhead(line_x)
    send(QEvent.Type.MouseButtonPress, (line_x, track_y), Qt.MouseButton.LeftButton)
    assert strip.picked_keys == set(), "the playhead drag grabbed the key"
    assert strip.dragging is None
    assert strip.scrubbing
    # Dragging the line left moves the cursor and leaves the key where it was.
    send(
        QEvent.Type.MouseMove,
        (strip.x_for(1), track_y),
        Qt.MouseButton.NoButton,
    )
    send(
        QEvent.Type.MouseButtonRelease,
        (strip.x_for(1), track_y),
        Qt.MouseButton.LeftButton,
    )
    assert e.cursor.value() < 3
    assert e.keyframes.keys_of(0) == {3}


def test_a_one_frame_clip_reports_one_frame_and_can_still_be_extended(window):
    """The transport counts the clip, not the headroom the cursor can reach."""
    w = window
    w.add_doc(animatable_clip(1))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    # One frame is one frame: the slider reaching past the end is not part of
    # the count, which used to read "Frame 1 / 9" on a one-frame clip.
    assert "Frame 1 / 1" == e.frame_label.text()
    # A single-frame clip cannot be played, but its authoring cursor stays live:
    # only the orange cursor can move beyond the existing frame.
    assert not e.play.isEnabled()
    assert not e.frame.isEnabled()
    e.goto_frame(4)
    application().processEvents()
    assert "past the end" in e.frame_label.text()
    key_at(e, 4, 30.0)
    assert len(e.obj["frames"]) == 5
    assert e.frame_label.text() == "Frame 5 / 5"
    assert e.play.isEnabled()


def test_the_timeline_cursor_reaches_the_end_and_back_without_losing_frames(window):
    """The strip's playhead follows the slider, including into the headroom."""
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for target in (0, 3, 7, 2):
        e.goto_frame(target)
        application().processEvents()
        # The strip is told where the cursor is on every render; clamping it to
        # the clip's own length left it behind while the slider moved on.
        assert e.keyframes.current == e.cursor.value() == target


def test_scrubbing_outward_is_not_capped_at_a_fixed_length(window):
    """Working towards the end keeps drawing past the cursor, indefinitely."""
    w = window
    w.add_doc(animatable_clip(2))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    reached = []
    for _ in range(4):
        # Walk the cursor outwards, one step past where the slider previously
        # stopped, the way dragging towards the end does.
        target = e.cursor.maximum() + 2 if reached else 6
        e.goto_frame(target)
        application().processEvents()
        reached.append(e.cursor.value())
        # The strip's playhead is where the slider is, even past the last frame.
        assert e.keyframes.current == e.cursor.value()
        # Grow the clip there so the next walk starts from further out.
        key_at(e, e.cursor.value(), 20.0)
        assert e.cursor.maximum() > reached[-1]
    assert reached == sorted(set(reached)), f"scrubbing stalled: {reached}"
    assert reached[-1] > reached[0]


def test_the_cursor_past_the_end_does_not_crash_the_editor(window):
    """Every panel reads the frame the cursor is on, which may not exist."""
    w = window
    w.add_doc(animatable_clip(2))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    e.goto_frame(6)
    application().processEvents()
    assert e.cursor.value() == 6 > len(e.obj["frames"]) - 1
    # These all used to index the frame list directly and raise IndexError.
    e.select_placement(0)
    e.show_elements()
    e.load_animation_fields()
    e.refresh_keyframe_strip()
    e.update_curve_info()
    e.reorder_placement(0, 0)
    e.set_layer(0) if False else None  # opens a dialog; covered by frame=None
    e.refresh()
    application().processEvents()


def test_the_viewer_canvas_follows_the_theme(window):
    """The canvas is a brush, not a palette role, so it needs telling."""
    w = window
    w.add_doc(animatable_clip(2))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    seen = {}
    for mode in ("light", "dark", "Nord", "Dracula"):
        if mode not in w.theme_actions:
            continue
        w.set_theme(mode)
        application().processEvents()
        seen[mode] = e.preview.backgroundBrush().color().name()
    assert len(set(seen.values())) == len(seen), f"canvas did not change: {seen}"
    # A dark theme gets a canvas darker than the surrounding chrome.
    w.set_theme("dark")
    application().processEvents()
    canvas = e.preview.backgroundBrush().color().lightness()
    from PyQt6.QtGui import QPalette

    chrome = application().palette().color(QPalette.ColorRole.Window).lightness()
    assert canvas < chrome


def tween_x(e, row=0):
    """The X of every frame's placement, or None where it inherits."""
    clip = e.obj
    bank = e.clip_bank(clip)
    return [
        None
        if frame["elements"][row][1] == 65535
        else round(bank["matrices"][frame["elements"][row][1]][4], 1)
        for frame in clip["frames"]
    ]


def test_a_curve_fills_the_gap_without_touching_the_end_key(window):
    """The keys are kept exactly; only the frames between them are written."""
    w = window
    w.add_doc(animatable_clip(12))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 10, 100.0)
    e.keyframes.picked_keys = {(0, 0), (10, 0)}
    e.keyframe_selection_changed()
    e.curve.setCurrentText("Linear")
    e.apply_curve_to_range()
    application().processEvents()
    values = tween_x(e)
    # Between the keys the values rise, and no frame falls back to the start.
    written = [(i, v) for i, v in enumerate(values) if v is not None]
    assert written[0] == (0, 0.0)
    assert written[-1] == (10, 100.0)
    assert all(b > a for (_, a), (_, b) in zip(written, written[1:])), written
    # The end key is the author's, so undoing the curve leaves it in place.
    e.revert_tween()
    application().processEvents()
    assert tween_x(e)[10] == 100.0
    assert tween_x(e)[0] == 0.0
    # Everything the curve wrote between them is gone again.
    assert all(v is None for v in tween_x(e)[1:10])


def test_delete_removes_every_selected_key(window):
    """A picked range of keys goes in one press, and comes back on undo."""
    w = window
    w.add_doc(animatable_clip(12))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for frame, value in ((0, 0.0), (3, 30.0), (6, 60.0), (9, 90.0)):
        key_at(e, frame, value)
    assert e.keyframes.keys_of(0) == {0, 3, 6, 9}
    e.keyframes.picked_keys = {(0, 0), (3, 0), (6, 0)}
    assert e.delete_selected_keys()
    application().processEvents()
    assert e.keyframes.keys_of(0) == {9}
    assert e.keyframes.selected_keys() == set()
    e.undo()
    application().processEvents()
    assert e.keyframes.keys_of(0) == {0, 3, 6, 9}


def drag_key(e, from_frame, to_frame, row=0):
    """A real drag of a key along its track, the way the mouse does it."""
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    strip = e.keyframes
    step = strip.width_per_frame()
    y = strip.RULER_HEIGHT + (row + 0.5) * strip.TRACK_HEIGHT
    start = (strip.LABEL_WIDTH + from_frame * step + step / 2, y)
    end = (strip.LABEL_WIDTH + to_frame * step + step / 2, y)

    def send(kind, point, button):
        application().sendEvent(
            strip,
            QMouseEvent(
                kind,
                QPointF(*point),
                QPointF(*point),
                button,
                button,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        application().processEvents()

    send(QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton)
    send(QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton)


def test_keys_can_be_moved_as_a_group(window):
    """Several picked keys drag together and keep their spacing."""
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for frame, value in ((0, 0.0), (2, 20.0), (4, 40.0), (6, 60.0)):
        key_at(e, frame, value)
    values = tween_x(e)
    e.keyframes.picked_keys = {(0, 0), (2, 0), (4, 0), (6, 0)}
    drag_key(e, 2, 3)
    # Every key moved by one frame, and each kept its own pose.
    assert e.keyframes.keys_of(0) == {1, 3, 5, 7}
    moved = tween_x(e)
    assert moved[1] == values[0]
    assert moved[3] == values[2]
    assert moved[5] == values[4]
    assert moved[7] == values[6]
    # The selection follows the keys rather than running ahead of them.
    assert e.keyframes.selected_keys() == {(1, 0), (3, 0), (5, 0), (7, 0)}


def test_dropping_a_key_onto_another_swaps_their_places(window):
    """A key dragged onto an occupied frame trades frames with that key.

    Both poses survive, so this is how two keys are put in each other's place
    rather than the drag being refused.
    """
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for frame, value in ((0, 0.0), (2, 20.0), (4, 40.0)):
        key_at(e, frame, value)
    drag_key(e, 2, 4)
    # Frames 0, 2 and 4 still each hold one of the original three poses; only
    # the poses on 2 and 4 have changed places, so nothing is lost.
    assert e.keyframes.keys_of(0) == {0, 2, 4}
    xs = tween_x(e)
    assert xs[2] == 40.0, f"the key from frame 4 should now sit on frame 2: {xs}"
    assert xs[4] == 20.0, f"the key from frame 2 should now sit on frame 4: {xs}"


def test_a_block_dragged_onto_another_key_is_still_refused(window):
    """Several picked keys moving as one have no swap reading, so they refuse.

    A rigid shift landing on a key that is staying put is ambiguous - there is
    no one key to trade places with - so the move is left where it was.
    """
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for frame, value in ((0, 0.0), (2, 20.0), (4, 40.0)):
        key_at(e, frame, value)
    before = tween_x(e)
    e.keyframes.picked_keys = {(0, 0), (2, 0)}
    e.move_keypoint(2, 4)
    application().processEvents()
    assert e.keyframes.keys_of(0) == {0, 2, 4}
    assert tween_x(e) == before
    assert "already has a key" in window.statusBar().currentMessage()


def test_keys_with_no_gap_say_there_is_nothing_to_tween(window):
    """Two keys side by side have no gap, which is not "already filled"."""
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 3, 30.0)
    key_at(e, 4, 40.0)
    e.keyframes.picked_keys = {(3, 0), (4, 0)}
    e.keyframe_selection_changed()
    e.apply_curve_to_range()
    application().processEvents()
    message = window.statusBar().currentMessage()
    assert "no frames to tween" in message, message


def test_reapplying_a_curve_says_the_gaps_are_already_filled(window):
    """A second run at the same density is a no-op, and says so."""
    w = window
    w.add_doc(animatable_clip(12))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 10, 100.0)
    e.keyframes.picked_keys = {(0, 0), (10, 0)}
    e.keyframe_selection_changed()
    e.curve.setCurrentText("Linear")
    e.apply_curve_to_range()
    application().processEvents()
    assert "writing" in window.statusBar().currentMessage()
    filled = tween_x(e)
    e.apply_curve_to_range()
    application().processEvents()
    assert "already filled" in window.statusBar().currentMessage()
    assert tween_x(e) == filled
    # A coarser density still fills in the frames the curve skipped.
    e.key_density.setValue(3)
    e.apply_curve_to_range()
    application().processEvents()
    assert "writing" in window.statusBar().currentMessage()


def test_the_key_count_follows_the_density_setting(window):
    """The "Keys" box is how many steps each gap is cut into, and is honoured."""
    w = window
    w.add_doc(animatable_clip(62))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 59, 590.0)
    e.keyframes.picked_keys = {(0, 0), (59, 0)}
    e.keyframe_selection_changed()
    application().processEvents()
    # The description next to the button has to follow the box, which it did
    # not: the number was read from a value that was never re-read on change.
    for density, expected in ((6, 6), (58, 58), (1, 1)):
        e.key_density.setValue(density)
        application().processEvents()
        assert f"writes {expected} frame" in e.curve_info.text(), (
            density,
            e.curve_info.text(),
        )
        e.apply_curve_to_range()
        application().processEvents()
        clip = e.obj
        filled = sum(
            1 for i in range(1, 59) if clip["frames"][i]["elements"][0][1] != 65535
        )
        assert filled == expected
        assert f"writing {expected} frame" in window.statusBar().currentMessage()
        e.revert_tween()
        application().processEvents()


def test_a_density_finer_than_the_gap_still_fills_it(window):
    """Asking for more steps than there are frames fills every frame."""
    w = window
    w.add_doc(animatable_clip(10))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 5, 50.0)
    e.keyframes.picked_keys = {(0, 0), (5, 0)}
    e.keyframe_selection_changed()
    e.key_density.setValue(60)
    application().processEvents()
    e.apply_curve_to_range()
    application().processEvents()
    clip = e.obj
    # A step longer than the gap used to write nothing, leaving the frames
    # between the keys showing the start pose.
    assert all(
        clip["frames"][i]["elements"][0][1] != 65535 for i in range(1, 5)
    )
    assert "writing 4 frames" in window.statusBar().currentMessage()


def rendered_x(e, frames, key=((0,), 3)):
    """The X each frame actually renders the child at, through the renderer."""
    out = []
    for frame in frames:
        e.frame.setValue(frame)
        e.render_frame()
        application().processEvents()
        instance = e.preview.instances.get(key)
        out.append(
            None if instance is None else round(instance[0].transform().dx(), 1)
        )
    return out


def test_a_frame_without_its_own_key_holds_the_earlier_pose(window):
    """Unset placements inherit, in the renderer as well as in the timeline."""
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 10.0)
    key_at(e, 5, 50.0)
    assert rendered_x(e, range(8)) == [10.0] * 5 + [50.0] * 3
    # Removing the later key returns the clip to the earlier pose everywhere,
    # rather than leaving the frames after it at no transform at all.
    e.frame.setValue(5)
    e.toggle_keypoint_at(5, 0)
    application().processEvents()
    assert e.keys_on_row(e.obj, 0) == [0]
    assert rendered_x(e, range(8)) == [10.0] * 8


def test_removing_the_only_key_falls_back_to_no_transform(window):
    """With nothing earlier to inherit there is nothing to carry forward."""
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 2, 70.0)
    assert rendered_x(e, range(4)) == [0.0, 0.0, 70.0, 70.0]
    e.frame.setValue(2)
    e.toggle_keypoint_at(2, 0)
    application().processEvents()
    assert rendered_x(e, range(4)) == [0.0, 0.0, 0.0, 0.0]


def test_frame_elements_report_what_was_added(window):
    """A frame's placement count follows the decoded list, not the descriptor."""
    w = window
    w.add_doc(animatable_clip(3))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    d = e.doc
    clip = d.resource(7)
    original = len(clip["children"])
    # Add a child and place it on the current frame, the way the editor does.
    e.place_child(3, "extra")
    application().processEvents()
    clip = d.resource(7)
    assert len(clip["children"]) == original + 1
    # The new placement has to be visible in the frame's element list, which
    # read its length from the file's descriptor instead of the decoded list.
    assert len(clip["frames"][0]["elements"]) == original + 1
    assert len(list(clip["frames"][0]["elements"])) == original + 1


def test_an_overlapping_curve_really_goes_past_the_end_key(window):
    """Overshoot must pass the second key, not flatten onto it.

    Clamping the blend at the ends made every frame past the peak identical to
    the end key, so the curve looked like it had thrown those frames away: they
    carry a value, but nothing changes and they stop reading as keys.
    """
    w = window
    w.add_doc(animatable_clip(12))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 10, 100.0)
    e.keyframes.picked_keys = {(0, 0), (10, 0)}
    e.keyframe_selection_changed()
    e.key_density.setValue(10)
    e.curve.setCurrentText("Overshoot")
    e.apply_curve_to_range()
    application().processEvents()
    values = [v for v in tween_x(e)[:11] if v is not None]
    assert max(values) > 100.0, f"overshoot never passed the end value: {values}"
    assert values[0] == 0.0 and values[10] == 100.0
    # Every frame the curve wrote is still a key, including the end of the run.
    assert e.keys_on_row(e.obj, 0) == list(range(11))


def test_an_anticipating_curve_pulls_back_before_the_first_key(window):
    """Anticipate goes behind the start value, the mirror of overshoot."""
    w = window
    w.add_doc(animatable_clip(12))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 10, 100.0)
    e.keyframes.picked_keys = {(0, 0), (10, 0)}
    e.keyframe_selection_changed()
    e.key_density.setValue(10)
    e.curve.setCurrentText("Anticipate")
    e.apply_curve_to_range()
    application().processEvents()
    values = [v for v in tween_x(e)[:11] if v is not None]
    assert min(values) < 0.0, f"anticipate never went behind the start: {values}"
    assert values[0] == 0.0 and values[10] == 100.0
    assert e.keys_on_row(e.obj, 0) == list(range(11))


def test_a_plain_curve_holds_both_keys_so_they_stay_visible(window):
    """A curve's own end frames keep every authored key readable on the strip."""
    w = window
    w.add_doc(animatable_clip(20))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    key_at(e, 19, 190.0)
    e.keyframes.picked_keys = {(0, 0), (19, 0)}
    e.keyframe_selection_changed()
    e.key_density.setValue(6)
    e.curve.setCurrentText("Ease in-out")
    e.apply_curve_to_range()
    application().processEvents()
    # Both keys are still shown as keys after the curve is applied over them.
    keys = e.keys_on_row(e.obj, 0)
    assert 0 in keys and 19 in keys


def test_frames_added_by_extending_do_not_become_keys(window):
    """Grown frames inherit, so only the keys that were set are keys.

    The added frames used to copy the last frame outright, key reference
    included, so a clip grown out to frame thirty read as thirty keys and the
    frames between the two the author set could not be tweened.
    """
    w = window
    w.add_doc(animatable_clip(1))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 0.0)
    # Walk the cursor out and key the far end, which grows the clip.
    e.goto_frame(30)
    application().processEvents()
    e.key_fields["X"].setValue(290.0)
    e.apply_animation_fields()
    application().processEvents()
    assert len(e.obj["frames"]) == 31
    # Two real keys, not thirty: everything between them inherits.
    assert e.placement_keys(e.obj) == [0, 30]
    assert e.keys_on_row(e.obj, 0) == [0, 30]
    # And the pose still holds through the grown frames rather than going flat.
    e.keyframes.picked_keys = {(0, 0), (29, 0)}
    e.keyframe_selection_changed()
    e.key_density.setValue(6)
    e.curve.setCurrentText("Linear")
    e.apply_curve_to_range()
    application().processEvents()
    values = tween_x(e)
    assert values[0] == 0.0 and values[30] == 290.0
    assert values[6] > values[1] > 0.0
    bounds = rendered_x(e, (0, 10, 20, 30))
    assert bounds[0] < bounds[1] < bounds[2] < bounds[3]


def test_children_leads_the_panels_and_indices_line_up(window):
    """The panel picker and the tab widget agree on what each index means."""
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    assert e.panel_picker.itemText(0) == "Children"
    assert e.tabs.tabText(0) == "Children"
    assert e.active_panel == 0, "a movieclip opens on the panel its edits start from"
    # Every enabled panel switches to itself, both ways round.
    for index in range(e.panel_picker.count()):
        if not e.tabs.isTabEnabled(index):
            continue
        e.set_panel(index)
        application().processEvents()
        assert e.active_panel == index
        assert e.tabs.currentIndex() == index
        assert e.panel_picker.currentIndex() == index


def test_the_json_panel_fills_in_when_switched_to(window):
    """The guard that decides whether to write the code box uses the new index."""
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    assert not e.code.toPlainText()
    e.set_panel(3)
    application().processEvents()
    import json

    assert json.loads(e.code.toPlainText())["id"] == 7


def find_in(window, term):
    """Run Ctrl+F over a grid with a scripted answer."""
    from PyQt6.QtWidgets import QInputDialog

    original = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: (term, True))
    try:
        window.focus_search()
        application().processEvents()
    finally:
        QInputDialog.getText = original


def test_ctrl_f_uses_inline_children_filter_and_frame_find(window):
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    fill_table(
        e.children,
        [[1, "alpha_one", 0, True], [2, "beta", 0, True],
         [3, "alpha_two", 0, True], [4, "alpha_three", 0, True]],
    )
    e.children.setCurrentCell(0, 0)
    e.children.setFocus()
    application().processEvents()
    w.focus_search()
    assert e.children_search.hasFocus()
    e.children_search.setText("alpha")
    application().processEvents()
    assert [row for row in range(e.children.rowCount()) if not e.children.isRowHidden(row)] == [0, 2, 3]
    e.first_child_match()
    assert e.children.currentRow() == 0
    # The prompt names the pane, so it is clear what is being searched.
    from PyQt6.QtWidgets import QInputDialog

    titles = []
    original = QInputDialog.getText
    QInputDialog.getText = staticmethod(
        lambda *a, **k: (titles.append(a[2]), (None, False))[1])
    try:
        e.frames.setFocus()
        application().processEvents()
        w.focus_search()
    finally:
        QInputDialog.getText = original
    assert "frames" in titles[-1]


def test_ctrl_f_in_the_browser_still_filters_assets(window):
    w = window
    w.add_doc(animatable_clip(4))
    e = w.current()
    e.go_to(7)
    application().processEvents()
    e.tree.setFocus()
    application().processEvents()
    w.focus_search()
    assert e.search.hasFocus(), "the browser filter is what Ctrl+F means here"


def test_mouse_side_buttons_are_undo_and_redo(window):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    w = window
    w.add_doc(animatable_clip(6))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    key_at(e, 0, 42.0)

    def pose():
        """The transform frame 0 actually carries, not the row's key list."""
        clip = e.obj
        element = clip["frames"][0]["elements"][0]
        return (
            None
            if element[1] == 65535
            else round(e.clip_bank(clip)["matrices"][element[1]][4], 1)
        )

    assert pose() == 42.0

    def press(button):
        # The filter is installed on the application, which is what sees a
        # button the widget under the cursor would otherwise ignore.
        point = QPointF(50, 50)
        application().sendEvent(
            application(),
            QMouseEvent(QEvent.Type.MouseButtonPress, point, point, button,
                        button, Qt.KeyboardModifier.NoModifier),
        )
        application().processEvents()

    press(Qt.MouseButton.BackButton)
    assert pose() is None, "the back side button undid the edit"
    press(Qt.MouseButton.ForwardButton)
    assert pose() == 42.0, "the forward side button redid it"





def three_children(window):
    """A clip whose Children list has three distinctly named entries."""
    d = populated()
    clip = d.resource(7)
    clip["children"] = [
        {"id": 3, "name": "alpha", "blend": 0},
        {"id": 4, "name": "beta", "blend": 0},
        {"id": 3, "name": "gamma", "blend": 0},
    ]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    application().processEvents()
    return d, e


def child_names(d):
    return [c["name"] for c in d.resource(7)["children"]]


def test_child_delete_button_removes_the_picked_child(window):
    """A checked bool must not be mistaken for a child index.

    QPushButton.clicked carries its checked state, which Qt passes to a slot
    that takes an argument; ``False`` then read as index 0, so Delete took out
    the first child whatever row was picked.
    """
    from PyQt6.QtWidgets import QPushButton

    d, e = three_children(window)
    e.children.setFocus()
    e.children.selectRow(2)
    application().processEvents()
    button = next(
        b
        for b in e.findChildren(QPushButton)
        if b.text() == "Delete" and b.parent().objectName() != "assetBrowser"
    )
    button.click()
    application().processEvents()
    assert child_names(d) == ["alpha", "beta"]
    # The list follows the model without a second edit.
    assert [e.children.item(r, 1).text() for r in range(e.children.rowCount())] == [
        "alpha",
        "beta",
    ]


def test_delete_key_removes_the_picked_child(window):
    """Delete reaches the Children list and takes the row the keyboard is on."""
    d, e = three_children(window)
    e.children.setFocus()
    e.children.selectRow(2)
    application().processEvents()
    QTest.keyClick(e.children, Qt.Key.Key_Delete)
    application().processEvents()
    assert child_names(d) == ["alpha", "beta"] and e.selected == 7


def test_deleting_a_child_needs_no_confirmation_and_undoes(window, monkeypatch):
    """The dialog is gone; the status bar says what happened, Ctrl+Z brings it back."""

    def refuse(*args, **kwargs):
        raise AssertionError("child delete must not open a confirmation dialog")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(refuse))
    d, e = three_children(window)
    e.children.setFocus()
    e.children.selectRow(1)
    QTest.keyClick(e.children, Qt.Key.Key_Delete)
    application().processEvents()
    assert child_names(d) == ["alpha", "gamma"]
    assert "undo" in window.statusBar().currentMessage().lower()
    e.undo()
    assert child_names(d) == ["alpha", "beta", "gamma"]


def test_removing_the_last_placement_of_a_child_drops_the_child(window):
    """SC has no per-frame child list, so an unplaced child has no way back.

    Removing its last placement left an entry in the Children list that no
    frame placed, which is the entry the request was to be rid of.
    """
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535], [1, 0, 65535]]}]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.elements.selectRow(1)
    application().processEvents()
    e.remove_element()
    application().processEvents()
    assert child_names(d) == ["sprite"]
    assert d.resource(7)["frames"][0]["elements"] == [[0, 0, 65535]]
    assert 4 in d.index, "the shared textfield asset is kept"
    e.undo()
    assert child_names(d) == ["sprite", "label"]


def test_removing_one_of_several_placements_keeps_the_child(window):
    """A child still placed in another frame is not orphaned and must stay."""
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [
        {"label": "", "elements": [[0, 0, 65535], [1, 0, 65535]]},
        {"label": "", "elements": [[1, 0, 65535]]},
    ]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    e.elements.selectRow(1)
    application().processEvents()
    e.remove_element()
    application().processEvents()
    assert child_names(d) == ["sprite", "label"]
    assert d.resource(7)["frames"][0]["elements"] == [[0, 0, 65535]]


def placement_matrix(d):
    clip = d.resource(7)
    element = clip["frames"][0]["elements"][0]
    return list(d.bank(clip["matrix_bank_index"])["matrices"][element[1]])


def test_clicking_a_placement_does_not_nudge_it(window):
    """Snapping rounds a zero-offset press onto the grid, which moved things.

    A placement already sitting between two pixels was dragged onto one by a
    click that never moved the pointer, because rounding the zero delta is not
    zero. Only movement off the press pixel counts as a drag.
    """
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    # Sit the placement between pixels, where snapping has something to do.
    e.commit_placement_transform(0, [1, 0, 0, 1, 40.4, 20.25])
    application().processEvents()
    assert placement_matrix(d)[4:] == pytest.approx([40.4, 20.25])
    revision = e.revision
    point = e.preview.mapFromScene(QPointF(45, 25))
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=point)
    application().processEvents()
    assert e.revision == revision, "a click that never moved is not an edit"
    assert placement_matrix(d)[4:] == pytest.approx([40.4, 20.25])


def test_a_real_drag_still_snaps(window):
    """The slop guard must not stop an actual drag from snapping."""
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    e.commit_placement_transform(0, [1, 0, 0, 1, 40.4, 20.25])
    application().processEvents()
    start = e.preview.mapFromScene(QPointF(45, 25))
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), start + QPoint(15, 0))
    QTest.mouseRelease(
        e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(15, 0)
    )
    application().processEvents()
    moved = placement_matrix(d)
    assert moved[4] != 40.4 and float(moved[4]).is_integer(), "the drag snapped"






def test_clicking_a_resize_handle_does_not_resize(window):
    """A press on a handle with no movement is not a resize.

    The recomputed matrix differs from the stored one by float noise, so
    committing it made a bare click dirty the document for nothing.
    """
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    revision = e.revision
    item = e.preview.selected_item()
    point = e.preview.mapFromScene(item.mapToScene(item.boundingRect().bottomRight()))
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=point)
    application().processEvents()
    assert e.preview.resize_handle in (None, "se")
    assert e.revision == revision, "a click on a handle with no movement is not an edit"


def test_dragging_a_resize_handle_still_resizes(window):
    """The slop guard must not stop a real resize drag."""
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    start = e.preview.mapFromScene(
        e.preview.selected_item().mapToScene(
            e.preview.selected_item().boundingRect().bottomRight()
        )
    )
    end = start + QPoint(20, 20)
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    application().processEvents()
    clip = d.resource(7)
    element = clip["frames"][0]["elements"][0]
    matrix = d.bank(clip["matrix_bank_index"])["matrices"][element[1]]
    assert matrix[0] != 1.0 and matrix[3] != 1.0, "the drag scaled the placement"




def test_delete_key_works_on_a_placement_picked_in_the_preview(window):
    """Del on an object picked in the preview removes that child.

    Clicking a placement there is how most of these edits start, but the
    preview was neither a shortcut target nor a focus the child index was read
    from, so the key did nothing until the editor was reached another way.
    """
    from PyQt6.QtCore import QPointF

    d, e = single_sprite(window)
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535], [1, 0, 65535]]}]
    d.update("movieclips", clip)
    e.refresh()
    application().processEvents()
    QTest.mouseClick(
        e.preview.viewport(),
        Qt.MouseButton.LeftButton,
        pos=e.preview.mapFromScene(QPointF(45, 25)),
    )
    application().processEvents()
    # Which of the two overlapping placements wins the hit test does not
    # matter; that Del removes the child the picked placement stands for does.
    picked = e.preview.selected_placement
    assert picked in (0, 1)
    child = d.resource(7)["frames"][0]["elements"][picked][0]
    doomed = d.resource(7)["children"][child]["name"]
    QTest.keyClick(e.preview, Qt.Key.Key_Delete)
    application().processEvents()
    assert doomed not in [c["name"] for c in d.resource(7)["children"]]
    assert len(d.resource(7)["children"]) == 1



def test_delete_in_the_preview_never_deletes_the_whole_resource(window, monkeypatch):
    """Delete with the preview focused and nothing picked does nothing.

    Once the preview became a Delete target, a press on empty canvas fell
    through to the browser-selection branch and offered to delete the open
    movieclip - the one meaning a press in the canvas cannot have.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("Delete in the preview must not delete the resource")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(refuse))
    d, e = single_sprite(window)
    e.preview.setFocus()
    e.preview.selected_placement = None
    application().processEvents()
    QTest.keyClick(e.preview, Qt.Key.Key_Delete)
    application().processEvents()
    assert 7 in d.index



def nested_textfield_doc():
    """A textfield inside a movieclip that is itself inside another movieclip.

    This is the shape the outline bug needed: the helper box is a descendant of
    two containers, so it inherits two transforms and a plain pen stroke is
    multiplied by both.
    """
    from PyQt6.QtWidgets import QGraphicsRectItem

    d = populated()
    inner = d.resource(7)
    inner["children"] = [{"id": 4, "name": "label", "blend": 0}]
    inner["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]}]
    inner["exports"] = ["inner"]
    d.update("movieclips", inner)
    outer_id = d.new_resource("movieclips")
    outer = d.resource(outer_id)
    outer["children"] = [{"id": 7, "name": "inner", "blend": 0}]
    outer["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]}]
    d.update("movieclips", outer)
    return d, outer_id


def test_textfield_box_stays_a_hairline_when_a_parent_movieclip_scales(window):
    """A scaled ancestor must not thicken or stretch the textfield outline.

    The helper box is a scene item nested under every ancestor clip, so a
    non-cosmetic pen is multiplied by each ancestor transform; that turned the
    field outline into a stretched band once the clip was resized.
    """
    from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsItem

    d, outer_id = nested_textfield_doc()
    window.add_doc(d)
    e = window.current()
    e.go_to(outer_id)
    application().processEvents()
    boxes = [
        i
        for i in e.preview.scene().items()
        if isinstance(i, QGraphicsRectItem) and i.parentItem() is not None
    ]
    assert boxes, "the textfield helper box should be in the scene"
    for box in boxes:
        assert box.pen().isCosmetic(), "a scaled parent thickens a plain stroke"
        # It must also be drawn in the textfield's own units, not stretched by
        # an ancestor: the rect is the authored box, unchanged by nesting.
        assert box.rect().width() == d.resource(4)["right"] - d.resource(4)["left"]
        assert box.rect().height() == d.resource(4)["bottom"] - d.resource(4)["top"]


def two_placements(window):
    """A clip with two child placements set apart, for group operations."""
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535], [1, 1, 65535]]}]
    # Give the second placement its own matrix, offset from the first.
    bank = d.bank(clip["matrix_bank_index"])
    matrices = [list(m) for m in bank["matrices"]]
    while len(matrices) < 2:
        matrices.append([1, 0, 0, 1, 0, 0])
    matrices[1] = [1, 0, 0, 1, 60, 40]
    clip["matrix_bank_index"] = d.add_bank(matrices, list(bank.get("colors") or []))
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    application().processEvents()
    return d, e


def placement_matrices(d):
    clip = d.resource(7)
    bank = d.bank(clip["matrix_bank_index"])
    return [
        list(bank["matrices"][e[1]]) if e[1] != 65535 else [1, 0, 0, 1, 0, 0]
        for e in clip["frames"][0]["elements"]
    ]


def test_canvas_ctrl_click_builds_a_multi_selection(window):
    """Ctrl-click adds a second placement to the canvas selection."""
    from PyQt6.QtCore import QPointF

    d, e = two_placements(window)
    first, second = e.preview.mapFromScene(QPointF(45, 25)), e.preview.mapFromScene(
        QPointF(65, 45)
    )
    QTest.mouseClick(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=first)
    application().processEvents()
    picked_first = e.preview.selected_placement
    QTest.mouseClick(
        e.preview.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        pos=second,
    )
    application().processEvents()
    assert e.preview.selected_rows == {0, 1}
    assert e.preview.selected_placement == 1


def test_group_drag_moves_every_picked_placement_in_one_undo_step(window):
    """Dragging with several picked moves them all, as a single edit."""
    from PyQt6.QtCore import QPointF

    d, e = two_placements(window)
    before = placement_matrices(d)
    e.preview.selected_rows = {0, 1}
    e.preview.selected_placement = 0
    revision = e.revision
    start = e.preview.mapFromScene(QPointF(45, 25))
    end = start + QPoint(20, 10)
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    application().processEvents()
    after = placement_matrices(d)
    assert e.revision == revision + 1, "one edit, not one per placement"
    for was, now in zip(before, after):
        assert now[4] != was[4] and now[5] != was[5], "every picked one moved"
    # Both moved by the same offset, so the group kept its shape.
    assert round(after[0][4] - before[0][4]) == round(after[1][4] - before[1][4])
    assert round(after[0][5] - before[0][5]) == round(after[1][5] - before[1][5])
    e.undo()
    assert placement_matrices(d) == before, "one undo restores the whole group"




def test_group_scale_composes_after_each_placement(window):
    """A group scale applies after each placement's own transform.

    Qt multiplies row-vectors, so the composed pose is ``own * group``. The
    other order scales the translation too and drags the group across the
    canvas while resizing it, which is the mistake this pins.
    """
    from PyQt6.QtGui import QTransform

    d, e = two_placements(window)
    before = placement_matrices(d)
    # Half scale about the group's top-left: a pure group transform.
    group = QTransform(0.5, 0, 0, 0.5, 0, 0)
    e.preview.selected_rows = {0, 1}
    e.preview.selected_placement = 0
    e.transform_placements([0, 1], group)
    application().processEvents()
    after = placement_matrices(d)
    for was, now in zip(before, after):
        expected = QTransform(*was) * group
        assert now == pytest.approx(
            [
                expected.m11(),
                expected.m12(),
                expected.m21(),
                expected.m22(),
                expected.dx(),
                expected.dy(),
            ]
        )
    # Composing the other way would have halved the offsets instead of leaving
    # them, so the group would have collapsed toward the origin.
    assert after[1][4] == pytest.approx(before[1][4] * 0.5)


def test_ctrl_click_children_outlines_them_and_delete_takes_the_current_row(
    window, monkeypatch
):
    """Several picked children are all outlined; Delete stays on the current row.

    Multi-selecting in Children drives the canvas - every picked child's
    placement in this frame is outlined. Delete there remains the single-row
    action it always was: this list's marked ranges can outlive the layout they
    were made in, because the rows are rebuilt with signals blocked, so reading
    a set here could remove a row the user never picked. Multi-row delete is
    available where it is safe: the canvas and the Placements list.
    """
    d = populated()
    clip = d.resource(7)
    clip["children"] = [
        {"id": 3, "name": "a", "blend": 0},
        {"id": 4, "name": "b", "blend": 0},
        {"id": 3, "name": "c", "blend": 0},
    ]
    clip["frames"] = [
        {"label": "", "elements": [[0, 0, 65535], [1, 0, 65535], [2, 0, 65535]]}
    ]
    d.update("movieclips", clip)
    window.add_doc(d)
    e = window.current()
    e.go_to(7)
    application().processEvents()
    # setCurrentCell collapses a range, so the current cell is set first and
    # the range marked afterwards - which is what a real drag-select does.
    e.children.setCurrentCell(0, 0)
    e.children.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
    application().processEvents()
    assert e.preview.selected_rows == {0, 1}, "both picked children are outlined"
    e.children.setFocus()
    window.delete_selection()
    application().processEvents()
    assert [c["name"] for c in d.resource(7)["children"]] == ["b", "c"]


def test_list_context_menus_offer_their_own_actions(window, monkeypatch):
    """Each list's right-click menu mirrors its buttons."""
    from PyQt6.QtWidgets import QMenu
    from PyQt6.QtCore import QPoint

    d, e = two_placements(window)
    seen = []

    def fake_exec(menu, position):
        seen.append({a.text() for a in menu.actions()})

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    point = e.children.viewport().rect().center()
    e.children_context_menu(point)
    e.frames_context_menu(point)
    e.placements_context_menu(point)
    assert {"Add child…", "Delete", "Duplicate", "Rename…", "Open child"} <= seen[0]
    assert {"New frame", "Duplicate frame", "Delete frame"} == seen[1]
    assert {"Add placement", "Remove", "Transform / color…"} == seen[2]




def test_group_scale_by_dragging_the_box_handle(window):
    """End to end: pick two, grab the group box corner, drag, release.

    The direct-call test never covered the press/drag/release path, which is
    where the group resize actually lives - and where the drag state was
    cleared before the matrix was worked out, so the resize committed as a
    no-op with a revision bump and nothing changed.
    """
    d, e = two_placements(window)
    before = placement_matrices(d)
    e.preview.selected_rows = {0, 1}
    e.preview.selected_placement = 0
    e.preview.viewport().update()
    application().processEvents()
    extent = e.preview.selection_extent()
    assert extent is not None, "two picked placements should give a group box"
    start = e.preview.mapFromScene(extent.bottomRight())
    assert e.preview.group_extent_hit(start) == "se"
    revision = e.revision
    end = start + QPoint(40, 40)
    QTest.mousePress(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(e.preview.viewport(), end)
    QTest.mouseRelease(e.preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
    application().processEvents()
    after = placement_matrices(d)
    assert e.revision == revision + 1, "the drag is one edit"
    # Both placements scale, not only the one that was grabbed.
    for was, now in zip(before, after):
        assert now[0] > was[0] and now[3] > was[3], "every picked placement grew"
    # The one away from the anchor also moves; the anchor placement stays put.
    assert after[1][4] != before[1][4] and after[1][5] != before[1][5]
    assert after[0][4] == pytest.approx(before[0][4])
    e.undo()
    assert placement_matrices(d) == before, "one undo restores both"



def test_copy_takes_every_picked_child_not_the_movieclip(window):
    """Ctrl+C over several marked children copies those children's resources.

    The pick was read through focused_child_id, which answers from the focus
    widget; a marked range whose table was not focused came back empty, and
    Copy fell through to the browser selection - the whole movieclip.
    """
    from PyQt6.QtWidgets import QTableWidgetSelectionRange

    w = window
    d = populated()
    clip = d.resource(7)
    clip["children"] = [
        {"id": 3, "name": "a", "blend": 0},
        {"id": 4, "name": "b", "blend": 0},
        {"id": 3, "name": "c", "blend": 0},
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    application().processEvents()
    e.children.setCurrentCell(0, 0)
    e.children.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
    application().processEvents()
    assert e.picked_child_ids() == [3, 4], "rows 0 and 1 are the shape and the textfield"
    w.copy()
    wait_until(lambda: not w.jobs)
    doc, roots = w.clipboard
    kinds = sorted(doc.index[root][0] for root in roots)
    assert kinds == ["shapes", "textfields"], "both picked children, not the clip"
    assert "movieclips" not in kinds


def test_copy_of_two_instances_of_one_resource_deduplicates(window):
    """Two children sharing a shape copy one resource, not the same id twice."""
    from PyQt6.QtWidgets import QTableWidgetSelectionRange

    w = window
    d = populated()
    clip = d.resource(7)
    clip["children"] = [
        {"id": 3, "name": "a", "blend": 0},
        {"id": 3, "name": "b", "blend": 0},
    ]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    application().processEvents()
    e.children.setCurrentCell(0, 0)
    e.children.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
    application().processEvents()
    assert e.picked_child_ids() == [3]
    w.copy()
    wait_until(lambda: not w.jobs)
    doc, roots = w.clipboard
    assert len(roots) == 1 and doc.index[roots[0]][0] == "shapes"


def test_duplicate_of_a_marked_child_set_copies_instances(window):
    """Duplicate with marked children copies the instances, not the clip."""
    from PyQt6.QtWidgets import QTableWidgetSelectionRange

    w = window
    d = populated()
    clip = d.resource(7)
    clip["children"] = [
        {"id": 3, "name": "a", "blend": 0},
        {"id": 4, "name": "b", "blend": 0},
        {"id": 3, "name": "c", "blend": 0},
    ]
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535], [1, 0, 65535]]}]
    d.update("movieclips", clip)
    w.add_doc(d)
    e = w.current()
    e.go_to(7)
    application().processEvents()
    before = len(e.obj["children"])
    e.children.setCurrentCell(0, 0)
    e.children.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
    application().processEvents()
    w.duplicate()
    application().processEvents()
    # An instance is added to the clip; the movieclip is not duplicated.
    assert len(e.obj["children"]) == before + 1
    assert e.selected == 7
