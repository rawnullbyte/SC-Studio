from collections import OrderedDict
import builtins
import math
from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QTransform,
    QImage,
    QPalette,
    QPixmap,
    QPainterPath,
    QPolygonF,
    QFont,
    QColor,
    QPen,
    QPainter,
)
from PyQt6.QtWidgets import (
    QGraphicsView,
    QGraphicsScene,
    QGraphicsItem,
    QGraphicsTextItem,
    QGraphicsPixmapItem,
    QWidget,
)
from . import textures


# Screen-pixel radius for zero-area atlas regions: both the diamond drawn for
# them and the radius they answer picks at, so drawn markers stay clickable.
ATLAS_MARKER_RADIUS = 3.0


def grid_coordinate(value, low, high, inner_low, inner_high, scale):
    """Stretch the center and keep edge dimensions, in pre-transform coordinates."""
    if scale <= 0 or high <= low:
        return value
    start, end = low * scale, high * scale
    edge_low = max(0, inner_low - low)
    edge_high = max(0, high - inner_high)
    shrink = min(1, (end - start) / max(edge_low + edge_high, 1e-9))
    edge_low *= shrink
    edge_high *= shrink
    if value <= inner_low:
        mapped = start + (value - low) * shrink
    elif value >= inner_high:
        mapped = end - (high - value) * shrink
    else:
        mapped = (
            start
            + edge_low
            + (value - inner_low)
            / max(inner_high - inner_low, 1e-9)
            * (end - start - edge_low - edge_high)
        )
    return mapped / scale


def grid_commands(obj, deformation):
    if deformation is None:
        return obj["commands"]
    left, top, right, bottom, sx, sy = deformation
    vertices = [v for c in obj["commands"] for v in c["vertices"]]
    if not vertices:
        return obj["commands"]
    xlo, xhi = min(v[0] for v in vertices), max(v[0] for v in vertices)
    ylo, yhi = min(v[1] for v in vertices), max(v[1] for v in vertices)
    return [
        {
            **c,
            "vertices": [
                [
                    grid_coordinate(v[0], xlo, xhi, left, right, sx),
                    grid_coordinate(v[1], ylo, yhi, top, bottom, sy),
                    v[2],
                    v[3],
                ]
                for v in c["vertices"]
            ],
        }
        for c in obj["commands"]
    ]


class Triangle(QGraphicsItem):
    def __init__(self, vertices, image, parent):
        super().__init__(parent)
        self.points = [QPointF(v[0], v[1]) for v in vertices]
        self.path = QPainterPath()
        self.path.moveTo(self.points[0])
        self.path.lineTo(self.points[1])
        self.path.lineTo(self.points[2])
        self.path.closeSubpath()
        us = [v[2] / 65535 * image.width() for v in vertices]
        vs = [v[3] / 65535 * image.height() for v in vertices]
        self.image = image  # Implicitly shared atlas; never copy a crop per triangle.
        u = us
        v = vs
        dx = [p.x() for p in self.points]
        dy = [p.y() for p in self.points]
        det = (u[1] - u[0]) * (v[2] - v[0]) - (u[2] - u[0]) * (v[1] - v[0])
        self.transform_image = None
        if abs(det) > 1e-10:
            a = (
                (dx[1] - dx[0]) * (v[2] - v[0]) - (dx[2] - dx[0]) * (v[1] - v[0])
            ) / det
            c = (
                (u[1] - u[0]) * (dx[2] - dx[0]) - (u[2] - u[0]) * (dx[1] - dx[0])
            ) / det
            b = (
                (dy[1] - dy[0]) * (v[2] - v[0]) - (dy[2] - dy[0]) * (v[1] - v[0])
            ) / det
            d = (
                (u[1] - u[0]) * (dy[2] - dy[0]) - (u[2] - u[0]) * (dy[1] - dy[0])
            ) / det
            self.transform_image = QTransform(
                a, b, c, d, dx[0] - a * u[0] - c * v[0], dy[0] - b * u[0] - d * v[0]
            )

    def boundingRect(self):
        return self.path.boundingRect()

    def paint(self, painter, option, widget=None):
        if self.transform_image:
            painter.save()
            # Antialiasing and SmoothPixmapTransform are both set on the view, so
            # magnified art shows its texels instead of a blur.
            painter.setClipPath(self.path)
            painter.setTransform(self.transform_image, True)
            painter.drawImage(0, 0, self.image)
            painter.restore()


class Sprite(QGraphicsPixmapItem):
    def boundingRect(self):
        return getattr(self, "logical_bounds", super().boundingRect())

    def set_bounds(self, width, height):
        self.prepareGeometryChange()
        self.logical_bounds = QRectF(0, 0, width, height)


class Container(QGraphicsItem):
    """A transform parent whose bounds track animated descendants."""

    def boundingRect(self):
        return self.childrenBoundingRect()

    def paint(self, painter, option, widget=None):
        pass


class KeyframeStrip(QWidget):
    """A multi-select keyframe timeline, under the transport bar.

    Shows every frame of the open movieclip with the selected placement's
    keyframes, the playhead, and a translucent band over a range of frames so a
    gap between two keys can be acted on directly. There is no keyboard
    modifier: a plain drag across empty timeline selects a range, which is the
    gesture an animator reaches for anyway.
    """

    framePicked = pyqtSignal(int)
    selectionChanged = pyqtSignal()
    moveRequested = pyqtSignal(int, int)  # dragged key, frame it moved to
    frameContext = pyqtSignal(object, object)  # frames, global position
    keyContext = pyqtSignal(int, object)  # frame, global position

    EDGE = 6  # grab width for nudging the selection band's ends
    LABEL_HEIGHT = 13
    KEY_ROW = 15  # height of the key track; below it is the ruler
    REPEAT_MS = 130

    @property
    def ruler_top(self):
        """Y where the ruler begins; above it is the draggable key track."""
        return self.LABEL_HEIGHT + self.KEY_ROW

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame_count = 0
        self.current = 0
        self.keys = set()
        self.row = -1
        self.band = None  # (anchor, pivot) frame indices
        self.dragging = None
        self.marquee = None
        self.resizing = None
        self._repeat = None
        self.width_per_frame = 16
        self.setMinimumHeight(52)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setToolTip(
            "Drag in the ruler below to select a range of frames · drag a key "
            "in the row above to move it · drag the band edge to lengthen or "
            "shorten it · right click for key and frame actions"
        )

    # --- geometry -----------------------------------------------------------

    def width_for(self, count):
        """Fit the timeline to the widget, with a readable floor per frame."""
        available = max(1, self.width() - 8)
        if count <= 0:
            return 16
        return max(6, min(26, available / count))

    def x_for(self, frame):
        return frame * self.width_per_frame + self.width_per_frame / 2

    def frame_at(self, x):
        frame = int(x // self.width_per_frame)
        return max(0, min(frame, max(0, self.frame_count - 1)))

    def selected_frames(self):
        """Frames in the band, or an empty list when nothing is selected."""
        if self.band is None:
            return []
        low, high = sorted(self.band)
        return list(range(low, high + 1))

    # --- state --------------------------------------------------------------

    def set_state(self, frame_count, current, keys, row):
        """Adopt a new timeline, repainting only when something changed."""
        keys = set(keys)
        width = self.width_for(frame_count)
        if (
            frame_count == self.frame_count
            and current == self.current
            and keys == self.keys
            and row == self.row
            and width == self.width_per_frame
        ):
            return
        self.frame_count = max(0, frame_count)
        self.current = max(0, min(current, max(0, self.frame_count - 1)))
        self.keys = keys
        self.row = row
        self.width_per_frame = width
        if self.band is not None:
            low, high = sorted(self.band)
            if low >= self.frame_count:
                self.band = None
            else:
                self.band = (min(low, self.frame_count - 1),
                             min(high, self.frame_count - 1))
        self.update()

    def set_band(self, low, high):
        """Select an inclusive range of frames."""
        if self.frame_count <= 0:
            return
        clamped = (max(0, min(low, self.frame_count - 1)),
                   max(0, min(high, self.frame_count - 1)))
        if self.band != clamped:
            self.band = clamped
            self.selectionChanged.emit()
            self.update()

    def clear_band(self):
        if self.band is not None:
            self.band = None
            self.selectionChanged.emit()
            self.update()

    def select_keys(self):
        """Select from the first to the last key, so a tween is one gesture."""
        if self.keys:
            self.set_band(min(self.keys), max(self.keys))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.width_per_frame = self.width_for(self.frame_count)
        self.update()

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        height = self.height()
        metrics = self.fontMetrics()
        ruler_top = self.ruler_top
        painter.fillRect(self.rect(), self.palette().window())
        if not self.frame_count:
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(4, metrics.ascent() + 4, "Select a movieclip to animate")
            return

        step = self.width_per_frame
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        muted = self.palette().color(QPalette.ColorRole.PlaceholderText)
        playhead = QColor("#ff9d3a")

        # Selection band first, so keys and the playhead stay legible on top.
        if self.band is not None:
            low, high = sorted(self.band)
            shade = QColor(accent)
            shade.setAlpha(48)
            painter.fillRect(
                QRectF(low * step, 0, (high - low + 1) * step, height), shade
            )
            pen = QPen(accent)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(low * step, 0), QPointF(low * step, height)
            )
            painter.drawLine(
                QPointF((high + 1) * step, 0), QPointF((high + 1) * step, height)
            )

        label_step = 1
        while label_step * step < 34:
            label_step *= 5 if label_step % 2 else 2
        painter.setPen(muted)
        for frame in range(0, self.frame_count, label_step):
            x = self.x_for(frame)
            painter.drawLine(QPointF(x, ruler_top), QPointF(x, ruler_top + 5))
            if step >= 10:
                painter.drawText(
                    QPointF(x + 2, ruler_top + 4 + metrics.ascent()), str(frame + 1)
                )
        painter.drawLine(QPointF(0, ruler_top), QPointF(self.width(), ruler_top))

        top = self.LABEL_HEIGHT + 2
        size = max(3.0, min(step / 2 - 1, 8.0))
        for frame in sorted(self.keys):
            if frame >= self.frame_count:
                continue
            x = self.x_for(frame)
            painter.setBrush(accent)
            painter.setPen(QPen(text, 1))
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(x, top),
                        QPointF(x + size, top + size),
                        QPointF(x, top + size * 2),
                        QPointF(x - size, top + size),
                    ]
                )
            )

        play = self.current * step
        highlight = QColor(playhead)
        highlight.setAlpha(46)
        painter.fillRect(QRectF(play, 0, step, height), highlight)
        pen = QPen(playhead)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(QPointF(play, 0), QPointF(play, height))

        if self.marquee is not None:
            low, high = sorted(self.marquee)
            pen = QPen(text)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(
                QRectF(low * step, top, (high - low + 1) * step, size * 2)
            )

        # A hint that the band edges can be dragged, only when a band exists.
        if self.band is not None and step >= 8:
            low, high = sorted(self.band)
            painter.setPen(muted)
            painter.drawText(
                QPointF(low * step + 2, height - 3),
                f"{len(self.selected_frames())} frames selected",
            )

    # --- interaction --------------------------------------------------------

    def _stop_repeat(self):
        if self._repeat is not None:
            self._repeat.stop()
            self._repeat = None

    def _start_repeat(self, delta):
        self._stop_repeat()
        timer = QTimer(self)
        timer.setInterval(self.REPEAT_MS)
        timer.timeout.connect(lambda: self.framePicked.emit(
            max(0, min(self.current + delta, self.frame_count - 1))
        ))
        timer.start()
        self._repeat = timer

    def _band_edges(self, x):
        """True when x is on a band edge, with the side it belongs to."""
        if self.band is None:
            return None
        low, high = sorted(self.band)
        left, right = low * self.width_per_frame, (high + 1) * self.width_per_frame
        if abs(x - left) <= self.EDGE:
            return "low"
        if abs(x - right) <= self.EDGE:
            return "high"
        return None

    def mousePressEvent(self, event):
        if not self.frame_count:
            return
        x = event.position().x()
        frame = self.frame_at(x)
        if event.button() == Qt.MouseButton.RightButton:
            if frame in self.keys:
                self.keyContext.emit(frame, event.globalPosition().toPoint())
            else:
                self.frameContext.emit(
                    self.selected_frames() or [frame],
                    event.globalPosition().toPoint(),
                )
            return

        # The ruler selects a range and the key track moves keys, so a range
        # spanning keyframes can be selected without having to aim between them.
        on_ruler = event.position().y() >= self.ruler_top
        if not on_ruler:
            edge = self._band_edges(x)
            if edge is not None:
                self.resizing = edge
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
            if frame in self.keys:
                self.dragging = frame
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.framePicked.emit(frame)
                return
        # Ruler, or the key track where there is no key: begin a band. A plain
        # click is a one-frame band, so the same gesture covers both.
        self.marquee = (frame, frame)
        self.framePicked.emit(frame)

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if self.resizing is not None:
            frame = self.frame_at(x)
            low, high = sorted(self.band)
            if self.resizing == "low":
                self.set_band(min(frame, high), high)
            else:
                self.set_band(low, max(frame, low))
            return
        if self.dragging is not None:
            target = self.frame_at(x)
            if target != self.dragging:
                self.moveRequested.emit(self.dragging, target)
                self.dragging = target
            return
        if self.marquee is not None:
            self.marquee = (self.marquee[0], self.frame_at(x))
            self.update()
            return
        # Nudge cursor only when there is nothing else in progress.
        prompting = (
            event.position().y() < self.ruler_top and self._band_edges(x) is not None
        )
        self.setCursor(
            Qt.CursorShape.SizeHorCursor
            if prompting
            else Qt.CursorShape.ArrowCursor
        )

    def mouseReleaseEvent(self, event):
        if self.marquee is not None:
            low, high = sorted(self.marquee)
            self.marquee = None
            self.set_band(low, high)
            return
        self._stop_repeat()
        self.dragging = None
        self.resizing = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event):
        if self.frame_count:
            self.framePicked.emit(self.frame_at(event.position().x()))

    def wheelEvent(self, event):
        event.ignore()


class Preview(QGraphicsView):
    placementSelected = pyqtSignal(int)
    placementMoved = pyqtSignal(int, float, float)
    placementContext = pyqtSignal(int, object)
    atlasContext = pyqtSignal(object, object)
    atlasShapeOpened = pyqtSignal(int)

    placementTransformed = pyqtSignal(int, object)

    def contextMenuEvent(self, event):
        if self.atlas_regions is not None:
            point = self.mapToScene(event.pos())
            matches = self.atlas_shapes_at(point)
            self.atlasContext.emit(matches, event.globalPos())
            event.accept()
            return
        row = -1
        if getattr(self, "editable", False):
            for hit in self.scene().items(self.mapToScene(event.pos())):
                while hit and hit.data(0) is None:
                    hit = hit.parentItem()
                if hit is not None:
                    row = int(hit.data(0))
                    break
        self.placementContext.emit(row, event.globalPos())
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self.atlas_regions is not None:
            point = self.mapToScene(event.pos())
            matches = self.atlas_shapes_at(point)
            if matches:
                # Innermost match first: the smallest footprint is the sprite
                # the user aimed at, not the atlas-sized shape behind it.
                self.atlasShapeOpened.emit(matches[0]["id"])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def atlas_shapes_at(self, point):
        """Pick every UV region under the cursor, tolerance measured in screen pixels.

        Zero-area regions have no interior to sit inside, so they answer to the
        same radius the borders are drawn at.
        """
        if self.atlas_regions is None:
            return []
        tolerance = ATLAS_MARKER_RADIUS / max(abs(self.transform().m11()), 1e-9)
        return sorted(
            (
                region
                for region in self.atlas_regions
                if (
                    region["bounds"].adjusted(
                        -tolerance, -tolerance, tolerance, tolerance
                    ).contains(point)
                    # A zero-area region is a single texel, so its origin is the
                    # only place left to test; the rect test above never matches it.
                    or (
                        (
                            not region["bounds"].width()
                            or not region["bounds"].height()
                        )
                        and abs(region["bounds"].x() - point.x()) <= tolerance
                        and abs(region["bounds"].y() - point.y()) <= tolerance
                    )
                )
            ),
            key=lambda region: (
                region["bounds"].width() * region["bounds"].height(),
                region["id"],
                region["command"],
            ),
        )

    def keyPressEvent(self, event):
        if (
            event.key() == Qt.Key.Key_Escape
            and getattr(self, "dragged", None) is not None
        ):
            self.dragged = None
            self.resize_handle = None
            self.matrix_override = None
            if self.last:
                self.show_resource(*self.last, fit=False)
            event.accept()
            return
        directions = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        if event.key() in directions and self.selected_item() is not None:
            dx, dy = directions[event.key()]
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self.placementMoved.emit(self.selected_placement, dx * step, dy * step)
            event.accept()
            return
        super().keyPressEvent(event)

    def selected_item(self):
        row = getattr(self, "selected_placement", None)
        return (
            next(
                (
                    item
                    for item, _ in self.instances.values()
                    if item.isVisible() and item.data(0) == row
                ),
                None,
            )
            if row is not None
            else None
        )

    def resize_handles(self, item):
        r = item.boundingRect()
        return {
            "nw": r.topLeft(),
            "n": QPointF(r.center().x(), r.top()),
            "ne": r.topRight(),
            "e": QPointF(r.right(), r.center().y()),
            "se": r.bottomRight(),
            "s": QPointF(r.center().x(), r.bottom()),
            "sw": r.bottomLeft(),
            "w": QPointF(r.left(), r.center().y()),
        }

    def resize_hit(self, position):
        item = self.selected_item()
        if item is None:
            return None
        for name, point in self.resize_handles(item).items():
            screen = self.mapFromScene(item.mapToScene(point))
            if (screen - position).manhattanLength() <= 10:
                return name
        return None

    def resize_matrix(self, point, shift=False):
        r, m, handle = self.resize_rect, self.drag_matrix, self.resize_handle
        inv, valid = m.inverted()
        if not valid or r.width() <= 0 or r.height() <= 0:
            return m
        local = inv.map(point)
        ax = (
            r.right()
            if "w" in handle
            else r.left() if "e" in handle else r.center().x()
        )
        ay = (
            r.bottom()
            if "n" in handle
            else r.top() if "s" in handle else r.center().y()
        )
        sx = (
            max(0.01, (ax - local.x()) / r.width())
            if "w" in handle
            else max(0.01, (local.x() - ax) / r.width()) if "e" in handle else 1
        )
        sy = (
            max(0.01, (ay - local.y()) / r.height())
            if "n" in handle
            else max(0.01, (local.y() - ay) / r.height()) if "s" in handle else 1
        )
        if shift:
            sx = sy = sx if "e" in handle or "w" in handle else sy
        return QTransform(
            m.m11() * sx,
            m.m12() * sx,
            m.m21() * sy,
            m.m22() * sy,
            m.dx() + m.m11() * ax * (1 - sx) + m.m21() * ay * (1 - sy),
            m.dy() + m.m12() * ax * (1 - sx) + m.m22() * ay * (1 - sy),
        )

    def preview_transform(self, matrix):
        self.matrix_override = (
            self.selected_placement,
            [
                matrix.m11(),
                matrix.m12(),
                matrix.m21(),
                matrix.m22(),
                matrix.dx(),
                matrix.dy(),
            ],
        )
        if self.last:
            doc, id, frame = self.last
            self.show_resource(doc, id, frame, fit=False)

    def mousePressEvent(self, event):
        self.dragged = None
        self.resize_handle = None
        if (
            event.button() == Qt.MouseButton.LeftButton
            and getattr(self, "editable", False)
            and not event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            point = self.mapToScene(event.position().toPoint())
            handle = self.resize_hit(event.position().toPoint())
            if handle:
                self.dragged = self.selected_item()
                self.resize_handle = handle
                self.resize_rect = self.dragged.boundingRect()
                self.drag_start = point
                self.drag_matrix = QTransform(self.dragged.transform())
                event.accept()
                return
            for hit in self.scene().items(point):
                item = hit
                while item and item.data(0) is None:
                    item = item.parentItem()
                if item is not None:
                    self.dragged = item
                    self.selected_placement = int(item.data(0))
                    self.drag_start = point
                    self.drag_matrix = QTransform(item.transform())
                    self.placementSelected.emit(self.selected_placement)
                    if not any(group is item for group, _ in self.instances.values()):
                        self.dragged = None
                        event.accept()
                        return
                    self.viewport().update()
                    event.accept()
                    return
            self.selected_placement = None
            self.viewport().update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "dragged", None) is not None:
            if self.resize_handle:
                self.preview_transform(
                    self.resize_matrix(
                        self.mapToScene(event.position().toPoint()),
                        bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                    )
                )
                event.accept()
                return
            delta = self.mapToScene(event.position().toPoint()) - self.drag_start
            m = self.drag_matrix
            self.dragged.setTransform(
                QTransform(
                    m.m11(),
                    m.m12(),
                    m.m21(),
                    m.m22(),
                    m.dx() + delta.x(),
                    m.dy() + delta.y(),
                )
            )
            self.viewport().update()
            event.accept()
            return
        handle = (
            self.resize_hit(event.position().toPoint())
            if getattr(self, "editable", False)
            else None
        )
        cursor = (
            Qt.CursorShape.SizeHorCursor
            if handle in ("e", "w")
            else (
                Qt.CursorShape.SizeVerCursor
                if handle in ("n", "s")
                else (
                    Qt.CursorShape.SizeFDiagCursor
                    if handle in ("nw", "se")
                    else (
                        Qt.CursorShape.SizeBDiagCursor
                        if handle
                        else Qt.CursorShape.ArrowCursor
                    )
                )
            )
        )
        self.viewport().setCursor(cursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, "dragged", None) is not None:
            if self.resize_handle:
                matrix = self.resize_matrix(
                    self.mapToScene(event.position().toPoint()),
                    bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                )
                row = self.selected_placement
                self.dragged = None
                self.resize_handle = None
                self.matrix_override = None
                if matrix != self.drag_matrix:
                    self.placementTransformed.emit(
                        row,
                        [
                            matrix.m11(),
                            matrix.m12(),
                            matrix.m21(),
                            matrix.m22(),
                            matrix.dx(),
                            matrix.dy(),
                        ],
                    )
                elif self.last:
                    self.show_resource(*self.last, fit=False)
                event.accept()
                return
            delta = self.mapToScene(event.position().toPoint()) - self.drag_start
            row = self.selected_placement
            self.dragged = None
            if delta.manhattanLength() > 0.01:
                self.placementMoved.emit(row, delta.x(), delta.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def drawForeground(self, painter, rect):
        if self.atlas_regions is not None and self.show_atlas_outlines:
            # Same two-pass stroke for shapes and for the zero-area markers, so
            # redrawing an already-stroked polygon does not build one path per
            # marker. Positions repeat heavily (atlas 8 collapses 52,049
            # commands onto a single point), so draw only what is on screen.
            borders = [bounds for bounds in self.atlas_borders if bounds.intersects(rect)]
            scale = abs(self.transform().m11())
            # Radius in scene units so markers hold their screen size.
            # atlas_shapes_at picks with this same radius, so anything visibly
            # drawn is clickable.
            size = ATLAS_MARKER_RADIUS / scale if scale > 0 else 0.0
            for color, width in ((QColor(15, 20, 28, 230), 3), (QColor(255, 205, 70, 255), 1)):
                pen = QPen(color)
                pen.setCosmetic(True)
                pen.setWidthF(width)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if borders:
                    painter.drawRects(borders)
                if size > 0:
                    for x, y in self.atlas_marks:
                        if x < rect.left() - size or x > rect.right() + size:
                            continue
                        if y < rect.top() - size or y > rect.bottom() + size:
                            continue
                        painter.drawPolygon(
                            QPolygonF(
                                [
                                    QPointF(x, y - size),
                                    QPointF(x + size, y),
                                    QPointF(x, y + size),
                                    QPointF(x - size, y),
                                ]
                            )
                        )
        row = getattr(self, "selected_placement", None)
        if row is not None:
            for item, _ in self.instances.values():
                if item.isVisible() and item.data(0) == row:
                    pen = QPen(QColor("#63b5ff"))
                    pen.setCosmetic(True)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPolygon(item.mapToScene(item.boundingRect()))
                    radius = 4 / max(self.transform().m11(), 1e-9)
                    painter.setBrush(QColor("#63b5ff"))
                    for point in self.resize_handles(item).values():
                        p = item.mapToScene(point)
                        painter.drawRect(
                            QRectF(
                                p.x() - radius, p.y() - radius, radius * 2, radius * 2
                            )
                        )
                    break

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.scene().setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.instances = {}
        self.scene_key = None
        self.setBackgroundBrush(QColor("#202733"))
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(self.ViewportAnchor.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_rect = QRectF(-100, -100, 200, 200)
        self.setRenderHints(
            __import__(
                "PyQt6.QtGui", fromlist=["QPainter"]
            ).QPainter.RenderHint.Antialiasing
        )
        # Nearest-neighbour sampling, so zooming in shows the stored texels
        # rather than an interpolated blur. This governs every image and pixmap
        # the view draws, including the triangle shapes.
        self.setRenderHint(
            __import__(
                "PyQt6.QtGui", fromlist=["QPainter"]
            ).QPainter.RenderHint.SmoothPixmapTransform,
            False,
        )
        self.cache = OrderedDict()
        self.resource_cache = OrderedDict()
        self.shape_cache = OrderedDict()
        self.messages = []
        self.doc = None
        self.last = None
        self.atlas_regions = None
        self.atlas_borders = []
        self.atlas_marks = []
        # Objects drawn for the current frame, reported by Help > Performance.
        # Reset in __init__ as well as per draw, since that dialog can be opened
        # before anything has been rendered.
        self.items_count = 0
        self.show_atlas_outlines = True
        self.hidden_children = set()
        self.matrix_override = None
        # Scrollable area held fixed while a clip plays; None when not playing.
        self.play_rect = None
        self.setMouseTracking(True)

    def invalidate(self, keep_atlases=False):
        self.atlas_regions = None
        self.atlas_borders = []
        self.atlas_marks = []
        self.play_rect = None
        self.matrix_override = None
        if not keep_atlases:
            self.cache.clear()
        self.resource_cache.clear()
        self.shape_cache.clear()
        self.instances.clear()
        self.scene_key = None
        self.scene().clear()

    def fit_content(self):
        self.scene().setSceneRect(self.content_rect)
        self.resetTransform()
        self.fitInView(self.content_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.fit_scale = self.transform().m11()

    def show_atlas(self, doc, index):
        self.atlas_regions = [
            {**region, "bounds": QRectF(*region["rect"])}
            for region in doc.atlas_regions(index)
        ]
        # A command whose four corners resolve to one texel has no rectangle to
        # stroke, so it becomes a point marker instead: every atlas shape stays
        # visible and right-clickable. Deduplicated because these repeat heavily
        # (a whole atlas's worth can collapse onto one texel).
        rects = list(
            dict.fromkeys(tuple(region["rect"]) for region in self.atlas_regions)
        )
        self.atlas_borders = [
            QRectF(*values) for values in rects if values[2] and values[3]
        ]
        self.atlas_marks = list(
            dict.fromkeys(
                (values[0], values[1])
                for values in rects
                if not values[2] or not values[3]
            )
        )
        self.doc = doc
        self.editable = False
        self.selected_placement = None
        self.scene().clear()
        self.instances.clear()
        self.scene_key = None
        image = self.atlas(index)
        atlas_item = self.scene().addPixmap(QPixmap.fromImage(image))
        # The view already samples nearest-neighbour; an item-level mode would
        # override it and blur a magnified sheet.
        atlas_item.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self.content_rect = QRectF(0, 0, image.width(), image.height())
        self.scene().setSceneRect(self.content_rect)
        self.fit_content()
        self.last = None
        self.viewport().update()

    def wheelEvent(self, event):
        pixels, angle = event.pixelDelta(), event.angleDelta()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        horizontal = abs(pixels.x()) > abs(pixels.y()) or abs(angle.x()) > abs(
            angle.y()
        )
        if shift or alt or horizontal:
            if shift:
                dx = pixels.x() or pixels.y() or (angle.x() or angle.y()) / 120 * 40
                dy = 0
            elif alt:
                dx, dy = 0, pixels.y() or angle.y() / 120 * 40
            else:
                dx, dy = (pixels.x() or angle.x() / 120 * 40), (
                    pixels.y() or angle.y() / 120 * 40
                )
            scale = max(self.transform().m11(), 1e-9)
            target = self.mapToScene(self.viewport().rect().center()) - QPointF(
                dx / scale, dy / scale
            )
            w, h = (self.viewport().width() + 16) / scale, (
                self.viewport().height() + 16
            ) / scale
            camera = QRectF(target.x() - w / 2, target.y() - h / 2, w, h)
            self.scene().setSceneRect(self.content_rect.united(camera))
            self.centerOn(target)
            event.accept()
            return
        # Zoom relative to fit: 10% to 64x, with panning around the mouse.
        base = getattr(self, "fit_scale", 1.0)
        current = self.transform().m11()
        steps = (
            event.angleDelta().y() / 120
            if event.angleDelta().y()
            else event.pixelDelta().y() / 100
        )
        if not steps:
            event.ignore()
            return
        desired = min(
            base * 64,
            max(base * 0.1, current * 1.15**steps),
        )
        anchor = self.mapToScene(event.position().toPoint())
        self.scale(desired / max(current, 1e-12), desired / max(current, 1e-12))
        cursor = event.position()
        center = QPointF(self.viewport().width() / 2, self.viewport().height() / 2)
        target = anchor + QPointF(
            (center.x() - cursor.x()) / desired, (center.y() - cursor.y()) / desired
        )
        half_w = (self.viewport().width() / 2 + 8) / desired
        half_h = (self.viewport().height() / 2 + 8) / desired
        camera = QRectF(
            target.x() - half_w, target.y() - half_h, half_w * 2, half_h * 2
        )
        # Extra camera space prevents Qt's small-scene alignment from cancelling the anchor.
        self.scene().setSceneRect(self.content_rect.united(camera))
        self.centerOn(target)
        # Scrollbars store integer pixels; retain the remaining subpixel offset in the transform.
        residual = self.mapToScene(event.position().toPoint()) - anchor
        self.translate(residual.x(), residual.y())
        event.accept()

    def shape_surface(self, id, obj, deformation=None):
        key = (
            builtins.id(self.doc),
            id,
            builtins.id(self.doc.index[id][1]),
            deformation,
        )
        if key not in self.shape_cache:
            commands = grid_commands(obj, deformation)
            verts = [v for command in commands for v in command["vertices"]]
            if not verts:
                return None
            left, top = min(v[0] for v in verts), min(v[1] for v in verts)
            width = max(v[0] for v in verts) - left
            height = max(v[1] for v in verts) - top
            # Preserve full geometry in the document; bound preview surfaces for huge meshes.
            scale = min(
                max(deformation[4:]) if deformation else 1.0,
                2048 / max(width, height, 1),
            )
            surface = QImage(
                max(1, math.ceil(width * scale) + 2),
                max(1, math.ceil(height * scale) + 2),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            surface.fill(Qt.GlobalColor.transparent)
            painter = QPainter(surface)
            painter.scale(scale, scale)
            painter.translate(-left, -top)
            try:
                for command in commands:
                    im = self.atlas(command["texture_index"])
                    vertices = command["vertices"]
                    for i in range(len(vertices) - 2):
                        triangle = Triangle(vertices[i : i + 3], im, None)
                        triangle.paint(painter, None)
            finally:
                painter.end()
            self.shape_cache[key] = (
                QPixmap.fromImage(surface),
                left,
                top,
                scale,
                width,
                height,
            )
            while (
                sum(v[0].width() * v[0].height() * 4 for v in self.shape_cache.values())
                > 128 * 1024 * 1024
                and len(self.shape_cache) > 1
            ):
                self.shape_cache.popitem(last=False)
        self.shape_cache.move_to_end(key)
        return self.shape_cache[key]

    def atlas(self, index):
        key = (id(self.doc), index)
        if key not in self.cache:
            t = self.doc.tables["Textures"]["textures"][index]
            data = t["highres"]
            im = textures.image(data, self.doc.path.parent if self.doc.path else ".")
            raw = im.tobytes()
            self.cache[key] = QImage(
                raw, im.width, im.height, im.width * 4, QImage.Format.Format_RGBA8888
            ).copy()
            while (
                sum(im.sizeInBytes() for im in self.cache.values()) > 640 * 1024 * 1024
                and len(self.cache) > 1
            ):
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def resource(self, id):
        raw = self.doc.index[id][1]
        key = (id, builtins.id(self.doc), builtins.id(raw))
        if key not in self.resource_cache:
            self.resource_cache[key] = (raw, self.doc.resource(id, lazy_frames=True))
            while len(self.resource_cache) > 256:
                self.resource_cache.popitem(last=False)
        self.resource_cache.move_to_end(key)
        return self.resource_cache[key][1]

    def show_resource(self, doc, id, frame=0, fit=True):
        self.atlas_regions = None
        self.doc = doc
        self.editable = doc.index[id][0] == "movieclips"
        key = (builtins.id(doc), id, builtins.id(doc.index[id][1]))
        if key != self.scene_key:
            # A different resource: drop any area locked by a previous
            # playthrough, or the next view would keep scrolling inside it.
            self.play_rect = None
        if (
            len(self.instances) > 6000
            or key != self.scene_key
            or (self.instances and not self.scene().items())
        ):
            self.scene().clear()
            self.instances.clear()
            self.scene_key = key
            self.selected_placement = None
        for item, _ in self.instances.values():
            item.setVisible(False)
        self.messages = []
        self.items_count = 0
        try:
            self.draw(id, frame, None, set(), ())
        except Exception as e:
            self.messages.append(str(e))
        rect = QRectF()
        for item, _ in self.instances.values():
            if item.isVisible():
                rect = rect.united(item.sceneBoundingRect())
        if rect.isEmpty():
            rect = QRectF(-100, -100, 200, 200)
        self.content_rect = rect.adjusted(-30, -30, 30, 30)
        if self.play_rect is not None:
            # Playback keeps one scrollable area covering every frame of the
            # clip. Following the current frame instead moved the camera
            # whenever an animation shifted the scene, which read as the view
            # drifting while the clip played. Only playback freezes it; the
            # interactive path below is left exactly as it was.
            self.play_rect = self.play_rect.united(self.content_rect)
            self.scene().setSceneRect(self.play_rect)
        elif getattr(self, "dragged", None) is None:
            camera = self.mapToScene(self.viewport().rect()).boundingRect()
            self.scene().setSceneRect(
                self.content_rect if fit else self.content_rect.united(camera)
            )
        if fit:
            self.fit_content()
        self.last = (doc, id, frame)
        return "; ".join(dict.fromkeys(self.messages))

    def draw(self, id, frame, parent, stack, path, incoming=None, deformation=None):
        if id in stack:
            self.messages.append(f"Cycle at {id} omitted from preview")
            return
        self.items_count += 1
        if self.items_count > 3000:
            self.messages.append("Preview capped at 3000 objects")
            return
        kind, _ = self.doc.index[id]
        obj = self.resource(id)
        instance_key = (path, id)
        cached = self.instances.get(instance_key)
        fresh = cached is None
        if fresh:
            group = Container(parent)
            if parent is None:
                self.scene().addItem(group)
            self.instances[instance_key] = (group, obj)
        else:
            group = cached[0]
        if len(path) == 1:
            group.setData(0, path[0])
        group.setVisible(True)
        group.setTransform(QTransform())
        group.setOpacity(1)
        if kind == "movieclips":
            if not obj["frames"]:
                return
            f = obj["frames"][frame % len(obj["frames"])]
            bank = (
                self.doc.bank(obj["matrix_bank_index"])
                if any(e[1] != 65535 or e[2] != 65535 for e in f["elements"])
                else None
            )
            grid = obj.get("scaling_grid")
            grid_deformation = None
            if grid and incoming is not None:
                sx = math.hypot(incoming[0], incoming[1])
                sy = math.hypot(incoming[2], incoming[3])
                if sx > 1e-9 and sy > 1e-9:
                    grid_deformation = (
                        grid["left"],
                        grid["top"],
                        grid["right"],
                        grid["bottom"],
                        sx,
                        sy,
                    )
            for i, e in enumerate(f["elements"]):
                ci, mi, co = e
                if not path and ci in self.hidden_children:
                    continue
                child = obj["children"][ci]
                matrix = bank["matrices"][mi] if mi != 65535 else [1, 0, 0, 1, 0, 0]
                if not path and self.matrix_override and self.matrix_override[0] == i:
                    matrix = self.matrix_override[1]
                # The common SC nine-slice asset is a clip with identity shape children
                # whose bitmap commands are already cut at the grid boundaries.
                child_grid = grid_deformation if matrix == [1, 0, 0, 1, 0, 0] else None
                if grid_deformation and child_grid is None:
                    self.messages.append(
                        "Scaling grid with transformed children uses affine preview"
                    )
                sub = self.draw(
                    child["id"],
                    frame,
                    group,
                    stack | {id},
                    path + (i,),
                    matrix,
                    child_grid,
                )
                if sub is None:
                    continue
                sub.setZValue(i)
                sub.setTransform(QTransform(*matrix))
                if co != 65535:
                    color = bank["colors"][co]
                    sub.setOpacity(color[3] / 255)
                    if color[:3] != [255, 255, 255] or color[4:] != [0, 0, 0]:
                        self.messages.append(
                            "RGB color effects are stored; preview applies alpha only"
                        )
                if child.get("blend", 0):
                    self.messages.append(
                        "Blend modes are stored; preview uses normal blending"
                    )
        elif kind == "shapes" and (
            fresh or getattr(group, "surface_deformation", None) != deformation
        ):
            try:
                surface = self.shape_surface(id, obj, deformation)
                group.surface_deformation = deformation
                if surface:
                    pixmap, left, top, scale, width, height = surface
                    image = getattr(group, "surface_item", None)
                    if image is None:
                        image = Sprite(pixmap, group)
                        group.surface_item = image
                    else:
                        image.setPixmap(pixmap)
                    image.set_bounds(width * scale, height * scale)
                    image.setTransformationMode(
                        Qt.TransformationMode.FastTransformation
                    )
                    image.setPos(left, top)
                    image.setScale(1 / scale)
            except Exception as e:
                self.messages.append(str(e))
        elif kind == "textfields" and fresh:
            text = QGraphicsTextItem(obj["text"], group)
            font = QFont(obj["font_name"])
            font.setPixelSize(max(1, obj["font_size"]))
            font.setBold(bool(obj["styles"] & 4))
            font.setItalic(bool(obj["styles"] & 8))
            text.setFont(font)
            text.setDefaultTextColor(QColor.fromRgba(obj["font_color"]))
            text.setPos(obj["left"], obj["top"])
            text.setTextWidth(max(1, obj["right"] - obj["left"]))
            bounds = self.scene().addRect(
                QRectF(
                    obj["left"],
                    obj["top"],
                    obj["right"] - obj["left"],
                    obj["bottom"] - obj["top"],
                ),
                QPen(QColor("#68778e")),
            )
            bounds.setParentItem(group)
        elif kind == "modifiers":
            self.messages.append(
                "Mask modifiers are preserved; stencil masks are not simulated"
            )
        return group
