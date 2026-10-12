import copy
import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from sc_studio.preview import AnimatorTimeline
from sc_studio.widgets import fill_table
from test_gui import window, application, animatable_clip


def animated(window, count=170):
    doc = animatable_clip(count)
    clip = doc.resource(7)
    bank = doc.add_bank([[1, 0, 0, 1, i, 0] for i in range(count)], [])
    clip["matrix_bank_index"] = bank
    for i, f in enumerate(clip["frames"]):
        f["elements"] = [[0, i, 65535]]
    doc.update("movieclips", clip)
    window.add_doc(doc)
    ed = window.current()
    ed.go_to(7)
    ed.elements.selectRow(0)
    application().processEvents()
    return ed


def mouse(widget, kind, x, y, button=Qt.MouseButton.LeftButton):
    widget.mousePressEvent(
        QMouseEvent(kind, QPointF(x, y), button, button, Qt.KeyboardModifier.NoModifier)
    )


def test_playback_transport_is_exact_even_after_outward_authoring(window):
    ed = animated(window, 100)
    ed.goto_frame(160)
    assert (
        ed.cursor.value() == 160 and ed.frame.maximum() == 99 and ed.frame.value() == 99
    )
    assert len(ed.obj["frames"]) == 100
    ed.frame.setValue(98)
    assert ed.cursor.value() == 98 and ed.keyframes.current == 98
    assert ed.frame_label.text() == "Frame 99 / 100"
    ed.tick()
    assert ed.frame.value() == 99 and ed.cursor.value() == 99
    assert ed.frame_label.text() == "Frame 100 / 100"
    ed.tick()
    assert ed.frame.value() == 0 and ed.keyframes.current == 0
    assert ed.frame.maximum() == 99


def test_progress_slider_cannot_seek_past_or_mutate_clip(window):
    ed = animated(window, 100)
    before = copy.deepcopy(ed.doc.tables)
    ed.frame.setValue(500)
    assert ed.frame.value() == ed.cursor.value() == 99
    assert ed.doc.tables == before and not ed.history
    ed.frame.setValue(-10)
    assert ed.cursor.value() == 0


def test_range_resets_when_switching_resources(window):
    ed = animated(window, 170)
    cid = ed.doc.new_resource("movieclips")
    ed.populate()
    ed.go_to(cid)
    assert ed.frame.maximum() == 0 and ed.frame.value() == 0
    assert not ed.frame.isEnabled()
    assert ed.frame_label.text() == "Frame 1 / 1"


def test_fit_view_includes_every_key_of_170_frames(window):
    ed = animated(window)
    strip = ed.keyframes
    strip.resize(420, strip.content_height())
    strip.fit_timeline()
    assert strip.keys_of(0) == set(range(170))
    assert strip.visible_frames() == (0, 170)
    assert strip.x_for(169) < strip.width() - strip.SCROLLBAR
    assert strip.hscroll.maximum() == 0


def test_zoom_pan_and_fit_keep_all_keys(window):
    ed = animated(window)
    strip = ed.keyframes
    allkeys = strip.keys_of(0).copy()
    initial = strip.width_per_frame()
    strip.set_zoom(8)
    assert strip.width_per_frame() > initial and strip.hscroll.maximum() > 0
    strip.hscroll.setValue(strip.hscroll.maximum())
    first, last = strip.visible_frames()
    assert first > 0 and last == 170
    assert strip.frame_at(strip.x_for(169)) == 169
    assert strip.keys_of(0) == allkeys
    strip.fit_timeline()
    assert strip.visible_frames() == (0, 170) and strip.hscroll.value() == 0


def test_zoom_anchor_stays_under_pointer(window):
    ed = animated(window)
    strip = ed.keyframes
    x = strip.LABEL_WIDTH + strip.track_area() * 0.7
    before = (x - strip.LABEL_WIDTH + strip.hscroll.value()) / strip.width_per_frame()
    strip.set_zoom(4, x)
    after = (x - strip.LABEL_WIDTH + strip.hscroll.value()) / strip.width_per_frame()
    assert after == pytest.approx(before, abs=0.25)


def test_scroll_reaches_last_track_and_hit_test(window):
    ed = animated(window)
    strip = ed.keyframes
    strip.set_state(
        170,
        0,
        [{"row": i, "label": f"Track {i}", "keys": {0, 169}} for i in range(20)],
        0,
    )
    strip.resize(420, strip.content_height())
    strip.sync_scrollbars()
    assert strip.vscroll.maximum() > 0
    strip.vscroll.setValue(strip.vscroll.maximum())
    y = strip.y_for(19) + strip.TRACK_HEIGHT / 2
    assert strip.RULER_HEIGHT <= y < strip.height() - strip.SCROLLBAR
    assert strip.track_at(y) == 19


def test_only_orange_cursor_can_scrub_headroom(window):
    ed = animated(window, 10)
    strip = ed.keyframes
    # Plain ruler seek into headroom stays on the last real frame.
    mouse(strip, QEvent.Type.MouseButtonPress, strip.x_for(13), 3)
    assert ed.cursor.value() == 9
    ed.goto_frame(0)
    x = strip.x_for(0) - strip.width_per_frame() / 2
    mouse(strip, QEvent.Type.MouseButtonPress, x, 3)
    assert strip.playhead_drag
    strip.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(strip.x_for(13), 3),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    assert ed.cursor.value() > 9 and ed.frame.value() == 9
    assert len(ed.obj["frames"]) == 10
    strip.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(strip.x_for(13), 3),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def test_children_search_retains_indices_and_is_case_insensitive(window):
    ed = animated(window)
    fill_table(ed.children, [[3, "Sprite One", 0], [4, "Label", 0], [7, "Nested", 0]])
    ed.children_search.setText("SPRITE")
    assert not ed.children.isRowHidden(0) and ed.children.isRowHidden(1)
    ed.children_search.setText("demo")
    assert not ed.children.isRowHidden(2)
    ed.first_child_match()
    assert ed.children.currentRow() == 2
    ed.children_search.setText("4")
    assert not ed.children.isRowHidden(1) and ed.children.isRowHidden(0)
    ed.children_search.clear()
    assert not any(ed.children.isRowHidden(i) for i in range(3))
