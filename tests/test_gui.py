import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QMessageBox,
    QTableWidgetSelectionRange,
    QFileDialog,
)
from sc_studio.app import Window, typing_into
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


def test_table_tsv_paste_and_preview_frame_sync(window):
    w = window
    w.add_doc(populated())
    e = w.current()
    e.go_to(7)
    e.set_panel(1)
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
    e.set_panel(2)
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
    e.set_panel(4)
    e.commands.selectRow(0)
    e.vertices.item(0, 0).setText("42")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Apply
    )
    e.commands.selectRow(1)
    assert d.resource(3)["commands"][0]["vertices"][0][0] == 42
    assert d.resource(3)["commands"][1]["vertices"][0][0] == 0


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
    e.set_panel(1)
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
    count = e.frame.maximum() + 1
    seen = []
    for _ in range(count * 2):
        e.tick()
        seen.append(e.frame.value())
    expected = [(seen[0] + i) % count for i in range(count * 2)]
    assert seen == expected, f"{seen} != {expected}"
    e.toggle_play()


def test_playback_holds_the_camera_still(window):
    window.add_doc(populated())
    e = window.current()
    e.go_to(7)
    assert e.preview.play_rect is None

    def visible():
        rect = e.preview.mapToScene(e.preview.viewport().rect()).boundingRect()
        return (round(rect.x(), 2), round(rect.y(), 2))

    e.toggle_play()
    e.timer.stop()
    assert e.preview.play_rect is not None
    seen = [visible()]
    locked = []
    for _ in range(6):
        e.tick()
        application().processEvents()
        seen.append(visible())
        locked.append(e.preview.sceneRect() == e.preview.play_rect)
    # The scrollable area stays the locked one, and the view within it holds
    # still. Scroll positions are whole pixels, so allow sub-pixel rounding.
    assert all(locked), "scene rect stopped tracking the locked play area"
    xs = [v[0] for v in seen]
    ys = [v[1] for v in seen]
    assert max(xs) - min(xs) < 1 and max(ys) - min(ys) < 1, f"camera moved: {seen}"

    # toggle_play reads its state from the timer, which the manual ticks above
    # stopped, so restart it to exercise the real stop path.
    e.timer.start()
    e.toggle_play()
    assert e.preview.play_rect is None
    # A different resource must not inherit the locked area.
    e.preview.play_rect = e.preview.content_rect
    e.go_to(3)
    assert e.preview.play_rect is None


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
    e.set_panel(2)
    import json

    assert json.loads(e.code.toPlainText()) == d.resource(7)


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
    assert matrix[4:] == pytest.approx([40 + delta.x(), 20 + delta.y()])
    assert e.preview.transform().m11() == pytest.approx(scale)
    e.undo()
    clip = d.resource(7)
    assert d.bank(clip["matrix_bank_index"])["matrices"][0][4:] == [40, 20]


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
    assert e.panel_picker.currentIndex() == 5


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
    assert e.active_panel == 5
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
    assert e.selected == 3 and e.active_panel == 4
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
    e.set_panel(3)
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
        assert any((color := image.pixelColor(pixel.x()+dx, pixel.y()+dy)).red() > 180
                   and color.green() > 120 and color.blue() < 110
                   for dx in range(-2, 3) for dy in range(-2, 3))
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
    assert e.selected == 3 and e.active_panel == 4


def test_create_menu_assets_children_image_and_undo(window, monkeypatch, tmp_path):
    from PIL import Image
    import sc_studio.app as studio
    window.new()
    e = window.current()
    window.actions["create_textfields"].trigger()
    text = e.selected
    assert e.doc.index[text][0] == "textfields" and e.active_panel == 0
    assert e.category.currentText() == "Textfields"
    window.actions["create_movieclips"].trigger()
    clip = e.selected
    window.actions["create_child_textfields"].trigger()
    assert e.selected == clip and e.active_panel == 5
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
    assert e.doc.index[e.selected][0] == "shapes" and e.active_panel == 4
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
    assert e.active_panel == 5


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
    # Fitting returns to asset bounds rather than the camera's expanded pan region.
    p.fit_content()
    assert p.scene().sceneRect() == p.content_rect


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
