"""Scrollable multi-track timeline, fit view, anchored zoom and authoring cursor."""

import math
from bisect import bisect_left
from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import QPainter, QPalette, QColor, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget, QScrollBar


class AnimatorTimeline(QWidget):
    """A multi-track keyframe timeline, one labeled track per child.

    Every object in the movieclip gets a row showing its own keyframes, under a
    shared ruler with a draggable playhead, so a whole animation is visible and
    editable at once instead of one placement at a time. Dragging the playhead
    or a key past the end asks for the clip to be extended, which is how a
    timeline grows by working on it rather than by pre-adding frames.

    Fit view includes the entire clip. Zoom and scroll expose dense keys and
    additional tracks without discarding any timeline data. Playback follows
    the current frame only when it leaves the visible horizontal viewport.
    """

    framePicked = pyqtSignal(int)
    zoomChanged = pyqtSignal(float)
    trackPicked = pyqtSignal(int)
    keyToggled = pyqtSignal(int, int)
    moveRequested = pyqtSignal(int, int, int)
    extendRequested = pyqtSignal(int)
    selectionChanged = pyqtSignal()
    keyContext = pyqtSignal(int, int, object)
    trackContext = pyqtSignal(object, object)

    LABEL_WIDTH = 96
    RULER_HEIGHT = 15
    TRACK_HEIGHT = 17
    EDGE = 5
    VISIBLE_TRACKS = 6
    HEADROOM = 6  # frames drawn past the end of the clip
    SCROLLBAR = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame_count = 0
        self.current = 0
        self.tracks = []
        self.row = -1
        self.band = None
        self.picked = set()  # frames added individually with ctrl
        self.anchor = 0  # where a shift range starts
        self.key_anchor = None  # last keypoint touched
        self.picked_keys = set()  # (frame, row) keypoints selected directly
        self.scrubbing = False
        self.dragging = None
        self.playhead_drag = False
        self.zoom = 1.0
        self.hscroll = QScrollBar(Qt.Orientation.Horizontal, self)
        self.vscroll = QScrollBar(Qt.Orientation.Vertical, self)
        self.hscroll.valueChanged.connect(lambda _: self.update())
        self.vscroll.valueChanged.connect(lambda _: self.update())
        self.setMinimumHeight(self.content_height())
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setToolTip(
            "Drag the orange playhead beyond the end to author there - drag a "
            "diamond to move a key - drag along a track to select a range - "
            "right click for actions - Ctrl+wheel to zoom - Shift+wheel to pan"
        )

    def track_area(self):
        return max(1, self.width() - self.LABEL_WIDTH - self.SCROLLBAR)

    def width_per_frame(self):
        # Fit every frame by default, including dense timelines. Zoom supplies
        # precision instead of clipping keys at a fixed minimum spacing.
        count = max(1, self.frame_count + self.HEADROOM)
        return min(80.0, min(26.0, self.track_area() / count) * self.zoom)

    def x_for(self, frame):
        step = self.width_per_frame()
        return self.LABEL_WIDTH + frame * step + step / 2 - self.hscroll.value()

    def visible_frames(self):
        step = self.width_per_frame()
        first = min(self.frame_count, max(0, int(self.hscroll.value() // step)))
        last = min(
            self.frame_count,
            math.ceil((self.hscroll.value() + self.track_area()) / step) + 1,
        )
        return first, last

    def sync_scrollbars(self):
        self.hscroll.setGeometry(
            self.LABEL_WIDTH,
            self.height() - self.SCROLLBAR,
            self.track_area(),
            self.SCROLLBAR,
        )
        self.vscroll.setGeometry(
            self.width() - self.SCROLLBAR,
            self.RULER_HEIGHT,
            self.SCROLLBAR,
            max(1, self.height() - self.RULER_HEIGHT - self.SCROLLBAR),
        )
        self.hscroll.setPageStep(self.track_area())
        self.hscroll.setRange(
            0,
            max(
                0,
                math.ceil(
                    self.total_frames() * self.width_per_frame() - self.track_area()
                ),
            ),
        )
        self.hscroll.setSingleStep(max(1, round(self.width_per_frame() * 3)))
        height = max(1, self.height() - self.RULER_HEIGHT - self.SCROLLBAR)
        self.vscroll.setPageStep(height)
        self.vscroll.setSingleStep(self.TRACK_HEIGHT)
        self.vscroll.setRange(0, max(0, self.tracks_height() - height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_scrollbars()

    def set_zoom(self, zoom, anchor_x=None):
        anchor_x = (
            self.LABEL_WIDTH + self.track_area() / 2 if anchor_x is None else anchor_x
        )
        offset = max(0, min(self.track_area(), anchor_x - self.LABEL_WIDTH))
        anchor_frame = (offset + self.hscroll.value()) / self.width_per_frame()
        self.zoom = max(1.0, min(16384.0, float(zoom)))
        self.sync_scrollbars()
        self.hscroll.setValue(round(anchor_frame * self.width_per_frame() - offset))
        self.zoomChanged.emit(self.zoom)
        self.update()

    def fit_timeline(self):
        self.zoom = 1.0
        self.sync_scrollbars()
        self.hscroll.setValue(0)
        self.zoomChanged.emit(self.zoom)
        self.update()

    def follow_cursor(self):
        x = self.current * self.width_per_frame()
        left = self.hscroll.value()
        if x < left:
            self.hscroll.setValue(math.floor(x))
        elif x + self.width_per_frame() > left + self.track_area():
            self.hscroll.setValue(
                math.ceil(x + self.width_per_frame() - self.track_area())
            )

    def total_frames(self):
        """Frames drawn, which extends a little past the clip's end.

        The cursor has to be able to sit beyond the last frame so a change made
        there can extend the clip; it must not be pushed there by scrubbing.
        """
        if not self.frame_count:
            return 0
        # The headroom is measured from wherever the cursor is, not from the
        # end of the clip. A fixed band beyond the clip would run out after a
        # drag or two, which is what capped how far the timeline could be
        # worked: scrubbing is bounded by what is drawn, so drawing past the
        # cursor is what lets the cursor keep going.
        anchor = max(self.current, 0)
        return self.frame_count + self.HEADROOM + max(0, anchor + 1 - self.frame_count)

    def frame_at(self, x):
        step = self.width_per_frame()
        frame = int((x - self.LABEL_WIDTH + self.hscroll.value()) // step)
        return max(0, min(frame, max(0, self.total_frames() - 1)))

    def raw_frame_at(self, x):
        """Unclamped frame under x, so a drag past the end can be detected."""
        step = self.width_per_frame()
        return max(0, int((x - self.LABEL_WIDTH + self.hscroll.value()) // step))

    def track_at(self, y):
        index = int((y - self.RULER_HEIGHT + self.vscroll.value()) // self.TRACK_HEIGHT)
        return max(0, min(index, max(0, len(self.tracks) - 1)))

    def y_for(self, index):
        return self.RULER_HEIGHT + index * self.TRACK_HEIGHT - self.vscroll.value()

    def content_height(self):
        """Height for the reserved band, independent of the current clip.

        Deliberately constant: a widget that resized itself to fit its tracks
        would re-flow the splitter every time the selection changed, which is
        what made the panels jump when clicking the timeline.
        """
        return (
            self.RULER_HEIGHT
            + self.VISIBLE_TRACKS * self.TRACK_HEIGHT
            + self.SCROLLBAR
            + 2
        )

    def tracks_height(self):
        return len(self.tracks) * self.TRACK_HEIGHT

    def selected_frames(self):
        """Selected frames, whether picked one by one or dragged as a range."""
        limit = max(1, self.frame_count)
        frames = {f for f in self.picked if f < limit}
        if self.band is not None:
            low, high = sorted(self.band)
            frames.update(f for f in range(low, high + 1) if f < limit)
        return sorted(frames)

    def keys_of(self, row):
        for track in self.tracks:
            if track["row"] == row:
                return track["keys"]
        return set()

    def set_cursor(self, current):
        """Move the playhead without rebuilding the tracks.

        Playback changes only where the cursor is, but the full state update
        copies every track's key set and compares them all, so a clip with
        thousands of keys spent more time re-deriving that nothing had changed
        than it did drawing the frame. This is the cheap path for it.
        """
        current = max(0, current)
        if current == self.current:
            return
        self.current = current
        self.sync_scrollbars()
        self.follow_cursor()
        self.update()

    def set_state(self, frame_count, current, tracks, row):
        normalised = [
            {
                "row": t["row"],
                "label": t["label"],
                "keys": set(t["keys"]),
                "key_frames": sorted(set(t["keys"])),
            }
            for t in tracks
        ]
        if (
            frame_count == self.frame_count
            and current == self.current
            and row == self.row
            and normalised == self.tracks
        ):
            return
        self.frame_count = max(0, frame_count)
        # Clamped to the drawn range, not the clip's own length: the cursor is
        # meant to sit out in the headroom past the end, and clamping it to the
        # last real frame would leave the playhead stuck there while the slider
        # moved on without it.
        self.tracks = normalised
        self.current = max(0, current)
        self.row = row
        self.sync_scrollbars()
        self.follow_cursor()
        if self.band is not None:
            low, high = sorted(self.band)
            limit = max(0, self.total_frames() - 1)
            if low > limit:
                self.band = None
            else:
                self.band = (low, min(high, limit))
        self.update()

    def set_band(self, low, high):
        if self.frame_count <= 0:
            return
        self.picked = set()
        self.picked_keys = set()
        clamped = (
            max(0, min(low, self.frame_count - 1)),
            max(0, min(high, self.frame_count - 1)),
        )
        if self.band != clamped:
            self.band = clamped
            self.selectionChanged.emit()
            self.update()

    def clear_band(self):
        if self.band is not None or self.picked or self.picked_keys:
            self.band = None
            self.picked = set()
            self.picked_keys = set()
            self.selectionChanged.emit()
            self.update()

    def selected_keys(self):
        """Keypoints selected directly, as (frame, row) pairs."""
        return set(self.picked_keys)

    def pick_keys(self, keys):
        """Select a set of keypoints, as the clicks on them would.

        The editor's key operations act on what is picked here, so an agent
        driving a curve authors the same selection a person would build by
        clicking, rather than a parallel selection of its own.
        """
        self.picked_keys = {tuple(key) for key in keys}
        self.picked = set()
        self.band = None
        self.selectionChanged.emit()
        self.update()

    def key_block(self, anchor, target):
        """Every key in the block between two keys, across frames and tracks.

        A block rather than a single row: shift-clicking from one key to
        another picks everything between them whatever track it is on, which is
        how a whole run of keys is taken in one gesture on a multi-track
        timeline. Anchoring on one frame and reaching another on the same frame
        takes the tracks between them instead.
        """
        low_row, high_row = sorted((anchor[1], target[1]))
        low_frame, high_frame = sorted((anchor[0], target[0]))
        return {
            (frame, track["row"])
            for track in self.tracks
            if low_row <= track["row"] <= high_row
            for frame in track["keys"]
            if low_frame <= frame <= high_frame
        }

    def pick_frame(self, frame, modifiers):
        """Shift extends a range from the anchor; ctrl toggles one frame."""
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(
            modifiers
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )
        if shift:
            self.set_band(self.anchor, frame)
        elif ctrl:
            if frame in self.picked:
                self.picked.discard(frame)
            else:
                self.picked.add(frame)
                self.anchor = frame
            self.selectionChanged.emit()
            self.update()
        else:
            self.picked = {frame}
            self.picked_keys = set()
            self.band = None
            self.anchor = frame
            self.selectionChanged.emit()
            self.update()

    def select_keys(self, row=None):
        keys = self.keys_of(self.row if row is None else row)
        if keys:
            self.set_band(min(keys), max(keys))

    def paintEvent(self, event):
        painter = QPainter(self)
        metrics = self.fontMetrics()
        palette = self.palette()
        painter.fillRect(self.rect(), palette.window())
        accent = palette.color(QPalette.ColorRole.Highlight)
        text = palette.color(QPalette.ColorRole.WindowText)
        muted = palette.color(QPalette.ColorRole.PlaceholderText)
        playhead = QColor("#ff9d3a")
        bottom = self.height() - self.SCROLLBAR
        if not self.frame_count:
            painter.setPen(muted)
            painter.drawText(
                QPointF(6, self.RULER_HEIGHT - 3), "Select a movieclip to animate"
            )
            return
        step = self.width_per_frame()
        first, last = self.visible_frames()
        painter.fillRect(
            QRectF(self.LABEL_WIDTH, 0, self.track_area(), bottom), palette.base()
        )
        # Fixed labels, vertically scrollable tracks.
        for index, track in enumerate(self.tracks):
            top = self.y_for(index)
            if top + self.TRACK_HEIGHT <= self.RULER_HEIGHT or top >= bottom:
                continue
            selected = track["row"] == self.row
            painter.save()
            painter.setClipRect(
                QRectF(
                    0,
                    self.RULER_HEIGHT,
                    self.width() - self.SCROLLBAR,
                    bottom - self.RULER_HEIGHT,
                )
            )
            if selected:
                shade = QColor(accent)
                shade.setAlpha(38)
                painter.fillRect(QRectF(0, top, self.width(), self.TRACK_HEIGHT), shade)
            painter.setPen(text if selected else muted)
            painter.drawText(
                QPointF(6, top + self.TRACK_HEIGHT - 5),
                metrics.elidedText(
                    track["label"], Qt.TextElideMode.ElideRight, self.LABEL_WIDTH - 10
                ),
            )
            painter.setPen(QPen(muted, 1))
            painter.drawLine(
                QPointF(0, top + self.TRACK_HEIGHT),
                QPointF(self.width(), top + self.TRACK_HEIGHT),
            )
            painter.setClipRect(
                QRectF(
                    self.LABEL_WIDTH,
                    self.RULER_HEIGHT,
                    self.track_area(),
                    bottom - self.RULER_HEIGHT,
                )
            )
            mid = top + self.TRACK_HEIGHT / 2
            size = max(0.4, min(step / 2 - 0.2, 6.5))
            keys = track["key_frames"]
            for frame in keys[bisect_left(keys, first) : bisect_left(keys, last)]:
                x = self.x_for(frame)
                colour = (
                    playhead
                    if (frame, track["row"]) in self.picked_keys
                    else accent
                    if selected
                    else muted
                )
                painter.setPen(QPen(colour, 1))
                if step < 4:
                    # Dense fit view shows every key as a narrow tick, rather
                    # than oversized diamonds covering neighboring frames.
                    painter.drawLine(QPointF(x, mid - 3), QPointF(x, mid + 3))
                else:
                    painter.setBrush(colour)
                    painter.drawPolygon(
                        QPolygonF(
                            [
                                QPointF(x, mid - size),
                                QPointF(x + size, mid),
                                QPointF(x, mid + size),
                                QPointF(x - size, mid),
                            ]
                        )
                    )
            painter.restore()
        painter.save()
        painter.setClipRect(QRectF(self.LABEL_WIDTH, 0, self.track_area(), bottom))
        if self.band is not None:
            low, high = sorted(self.band)
            shade = QColor(accent)
            shade.setAlpha(46)
            painter.fillRect(
                QRectF(
                    self.x_for(low) - step / 2,
                    self.RULER_HEIGHT,
                    (high - low + 1) * step,
                    bottom - self.RULER_HEIGHT,
                ),
                shade,
            )
        for frame in self.picked:
            if first <= frame < last:
                shade = QColor(accent)
                shade.setAlpha(75)
                painter.fillRect(
                    QRectF(
                        self.x_for(frame) - step / 2,
                        self.RULER_HEIGHT,
                        step,
                        bottom - self.RULER_HEIGHT,
                    ),
                    shade,
                )
        label_step = 1
        while label_step * step < 38:
            label_step *= 5 if label_step % 2 else 2
        painter.setPen(muted)
        ruler_first = max(0, int(self.hscroll.value() // step))
        ruler_last = math.ceil((self.hscroll.value() + self.track_area()) / step)
        for frame in range(
            (ruler_first // label_step) * label_step,
            min(self.total_frames(), ruler_last + label_step),
            label_step,
        ):
            x = self.x_for(frame) - step / 2
            painter.drawLine(
                QPointF(x, self.RULER_HEIGHT - 4), QPointF(x, self.RULER_HEIGHT)
            )
            painter.drawText(QPointF(x + 3, metrics.ascent() + 1), str(frame + 1))
        end_x = self.x_for(self.frame_count) - step / 2
        pen = QPen(muted)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(end_x, 0), QPointF(end_x, bottom))
        x = self.x_for(self.current) - step / 2
        shade = QColor(playhead)
        shade.setAlpha(40)
        painter.fillRect(QRectF(x, 0, max(1, step), bottom), shade)
        painter.setPen(QPen(playhead, 2))
        painter.drawLine(QPointF(x, 0), QPointF(x, bottom))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(playhead)
        painter.drawPolygon(
            QPolygonF(
                [QPointF(x, 1), QPointF(x + 5, 7), QPointF(x, 13), QPointF(x - 5, 7)]
            )
        )
        painter.restore()

    def _on_playhead(self, x):
        step = self.width_per_frame()
        return abs(x - (self.x_for(self.current) - step / 2)) <= self.EDGE

    def mousePressEvent(self, event):
        if not self.frame_count:
            return
        point = event.position()
        x, y = point.x(), point.y()
        index = self.track_at(y)
        track = self.tracks[index] if index < len(self.tracks) else None
        if event.button() == Qt.MouseButton.RightButton:
            if track is None or y < self.RULER_HEIGHT:
                self.trackContext.emit(
                    self.selected_frames() or [self.current],
                    event.globalPosition().toPoint(),
                )
                return
            frame = self.frame_at(x)
            self.trackPicked.emit(track["row"])
            if frame in track["keys"]:
                self.keyContext.emit(
                    frame, track["row"], event.globalPosition().toPoint()
                )
            else:
                self.trackContext.emit(
                    self.selected_frames() or [frame],
                    event.globalPosition().toPoint(),
                )
            return
        modifiers = event.modifiers()
        if x < self.LABEL_WIDTH:
            if track is not None:
                self.trackPicked.emit(track["row"])
            return
        if track is not None:
            self.trackPicked.emit(track["row"])
        frame = min(self.frame_at(x), self.frame_count - 1)
        # The playhead line wins over anything under it, on the ruler as much as
        # on a track: it is stroked down the whole widget, so that is what a
        # press on it means. Everywhere else the ruler is for frames and the
        # tracks are for keys, so the row under the ruler does not answer for
        # keys on the ruler's behalf.
        on_playhead = self._on_playhead(x)
        on_track = track is not None and y >= self.RULER_HEIGHT
        # What a modifier augments is decided by where the eye is, not by where
        # the pointer happened to land: with keys selected, shift and click take
        # a block of keys, and with none selected they take a run of frames. A
        # pointer-based rule made a shift-click on a key's own pixel pick frames
        # instead, which is what stopped keys being mass-selected.
        picking_keys = on_track or bool(self.picked_keys)
        if not on_playhead and not picking_keys:
            # A press on the ruler is about frames: shift extends a range from
            # the last frame touched and ctrl toggles one, which is what marks
            # frames 2 through 6.
            self.pick_frame(frame, modifiers)
            self.framePicked.emit(frame)
            return
        if not on_playhead and on_track and frame in track["keys"]:
            # Clicking a key selects that keypoint, which is a finer selection
            # than a frame range. Ctrl adds or removes one, shift extends from
            # the last key touched, and a plain click replaces the selection.
            key = (frame, track["row"])
            if modifiers & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
            ):
                if key in self.picked_keys:
                    self.picked_keys.discard(key)
                else:
                    self.picked_keys.add(key)
            elif modifiers & Qt.KeyboardModifier.ShiftModifier:
                # Shift takes the whole block from the last key touched to this
                # one, on every track it spans, and adds it to the selection.
                anchor = self.key_anchor or key
                self.picked_keys |= self.key_block(anchor, key)
            elif key not in self.picked_keys:
                # Clicking a key that is not already picked selects just it.
                # Clicking one that is part of a picked set keeps the whole set,
                # so the drag that follows carries every key along instead of
                # quietly dropping back to the one under the cursor.
                self.picked_keys = {key}
            self.key_anchor = key
            self.dragging = key
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.selectionChanged.emit()
            self.update()
            self.framePicked.emit(frame)
            return
        # Clicking a track between its keys scrubs, and leaves any selected keys
        # alone: building up a run of keys across a row should not be undone by
        # a click on the empty space next to them. Pressing on the ruler is what
        # clears the key selection and starts a fresh frame range instead.
        self.scrubbing = True
        self.playhead_drag = on_playhead
        if on_playhead:
            frame = self.frame_at(x)
        if y < self.RULER_HEIGHT:
            self.picked = {frame}
            self.picked_keys = set()
            self.key_anchor = None
        else:
            self.picked = set()
        self.anchor = frame
        self.band = None
        self.selectionChanged.emit()
        self.update()
        self.framePicked.emit(frame)

    def mouseMoveEvent(self, event):
        point = event.position()
        x = point.x()
        if self.scrubbing:
            # The cursor moves freely and never creates a frame; only moving an
            # object or a key past the end does that.
            if self.playhead_drag:
                # Scrolling while dragging keeps outward authoring available
                # even when the timeline is zoomed in.
                if x > self.width() - self.SCROLLBAR - 8:
                    self.hscroll.setValue(
                        self.hscroll.value() + round(self.width_per_frame() * 2)
                    )
                elif x < self.LABEL_WIDTH + 8:
                    self.hscroll.setValue(
                        self.hscroll.value() - round(self.width_per_frame() * 2)
                    )
                self.framePicked.emit(min(65534, self.raw_frame_at(x)))
            else:
                self.framePicked.emit(min(self.frame_count - 1, self.frame_at(x)))
            return
        if self.dragging is not None:
            frame, row = self.dragging
            target = self.raw_frame_at(x)
            # A key can only be dropped on a frame that belongs to the clip; a
            # drag further out extends it first, and the move follows once the
            # frames are there.
            before = set(self.picked_keys)
            if target >= self.frame_count:
                self.extendRequested.emit(target + 1)
                self.moveRequested.emit(frame, target, row)
            elif target != frame:
                self.moveRequested.emit(frame, target, row)
            # The anchor follows the drag only when the key actually moved. The
            # editor writes the landed keys back into its selection on a move
            # and leaves it untouched when it refuses one, so an unchanged
            # selection means the key is still on the frame it started from and
            # the anchor must stay there too; otherwise the next step would drag
            # from a frame the key is not on.
            if self.picked_keys != before:
                self.dragging = (target, row)
            # The selection itself is the editor's, which moves every key that
            # was picked and reports where they ended up.
            return
        self.setCursor(
            Qt.CursorShape.SizeHorCursor
            if self._on_playhead(x)
            else Qt.CursorShape.ArrowCursor
        )

    def mouseReleaseEvent(self, event):
        self.scrubbing = False
        self.playhead_drag = False
        self.marquee = None
        self.dragging = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event):
        # A double click does nothing here on purpose: it was toggling a key,
        # which reads as "double clicking a frame deleted it" when the first
        # click of the pair had already selected that frame. Keys are added and
        # removed from the context menu, or by typing a value into the fields.
        event.ignore()

    def wheelEvent(self, event):
        pixels, angle = event.pixelDelta(), event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = angle.y() or angle.x() or pixels.y()
            self.set_zoom(self.zoom * 1.25 ** (delta / 120), event.position().x())
        elif (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            or angle.x()
            or pixels.x()
        ):
            delta = pixels.x() or pixels.y() or (angle.x() or angle.y()) / 120 * 60
            self.hscroll.setValue(self.hscroll.value() - round(delta))
        elif self.vscroll.maximum():
            delta = pixels.y() or angle.y() / 120 * self.TRACK_HEIGHT * 3
            self.vscroll.setValue(self.vscroll.value() - round(delta))
        else:
            delta = pixels.y() or angle.y() / 120 * 60
            self.hscroll.setValue(self.hscroll.value() - round(delta))
        event.accept()
