import os
from collections import deque

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
    start = p.mapToScene(p.viewport().rect()).boundingRect().center()
    scale = p.transform().m11()
    # Panning does not zoom, so every drag owes exactly this much movement.
    step = (390.0 / scale, 330.0 / scale)
    for _ in range(6):
        drag_view(p, (10, 10), (400, 340))
    moved = p.mapToScene(p.viewport().rect()).boundingRect().center() - start
    # Qt clamps the view to the scene rect, so a rect sized to the artwork
    # would swallow the later drags instead of moving the camera by the full
    # distance each one asks for.
    assert abs(moved.x()) == pytest.approx(step[0] * 6, rel=0.05)
    assert abs(moved.y()) == pytest.approx(step[1] * 6, rel=0.05)
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


def test_strip_survives_a_placement_absent_from_a_frame(window):
    """Placements appear and disappear between frames in real files."""
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
    e.bake_tween()                            # must skip frame 2, not crash
    assert 2 not in e.placement_keys(e.obj)
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
    e.frame.setValue(frame)
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
    assert e.frame.value() == far
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
        e.goto_frame(e.frame.maximum())
        application().processEvents()
        key_at(e, e.frame.value(), 20.0)
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
        e.goto_frame(e.frame.maximum())
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
    line_x = strip.LABEL_WIDTH + e.frame.value() * step
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
    assert e.frame.value() < 3
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
    # A single-frame clip cannot be played, but its slider has to stay live:
    # reaching past the only frame is how the clip gets its second one.
    assert not e.play.isEnabled()
    assert e.frame.isEnabled()
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
        assert e.keyframes.current == e.frame.value() == target


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
        target = e.frame.maximum() + 2 if reached else 6
        e.goto_frame(target)
        application().processEvents()
        reached.append(e.frame.value())
        # The strip's playhead is where the slider is, even past the last frame.
        assert e.keyframes.current == e.frame.value()
        # Grow the clip there so the next walk starts from further out.
        key_at(e, e.frame.value(), 20.0)
        assert e.frame.maximum() > reached[-1]
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
    assert e.frame.value() == 6 > len(e.obj["frames"]) - 1
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


def test_a_key_cannot_be_dropped_onto_another_key(window):
    """Dropping a key on an occupied frame leaves both where they were."""
    w = window
    w.add_doc(animatable_clip(8))
    e = w.current()
    e.go_to(7)
    e.elements.selectRow(0)
    application().processEvents()
    for frame, value in ((0, 0.0), (2, 20.0), (4, 40.0)):
        key_at(e, frame, value)
    before = tween_x(e)
    drag_key(e, 2, 4)
    # The old code overwrote the key already on frame 4 and cleared frame 2,
    # so one pose was lost; now the drag simply does not land.
    assert e.keyframes.keys_of(0) == {0, 2, 4}
    assert tween_x(e) == before


def test_adjacent_keys_report_why_nothing_can_be_tweened(window):
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
    assert "adjacent" in message, message


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
