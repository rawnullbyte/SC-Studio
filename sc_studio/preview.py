from collections import OrderedDict
import builtins
import math
from PyQt6.QtCore import QSize, Qt, QPointF, QRectF, pyqtSignal
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
    QTextOption,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsItem,
    QGraphicsTextItem,
    QGraphicsPixmapItem,
)
from . import textures
from .timeline import AnimatorTimeline as AnimatorTimeline


# Screen-pixel radius for zero-area atlas regions: both the diamond drawn for
# them and the radius they answer picks at, so drawn markers stay clickable.
ATLAS_MARKER_RADIUS = 3.0

# The transform a placement starts from when nothing earlier in the clip has
# set one; the same value the editor uses for a placement with no key at all.
IDENTITY_MATRIX = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


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
    """A transform parent whose bounds track animated descendants.

    A textfield declares its own field box, which is not the extent of its
    glyphs: the text usually overflows the box. When ``logical_bounds`` is set
    the container measures itself by that box, so the selection outline, the
    resize handles and the size dialog all agree on the field rather than on
    however tall the text happens to be.
    """

    def boundingRect(self):
        return getattr(self, "logical_bounds", self.childrenBoundingRect())

    def paint(self, painter, option, widget=None):
        pass




class Preview(QGraphicsView):
    placementSelected = pyqtSignal(int)
    placementMoved = pyqtSignal(int, float, float)
    placementContext = pyqtSignal(int, object)
    atlasContext = pyqtSignal(object, object)
    atlasShapeOpened = pyqtSignal(int)

    placementTransformed = pyqtSignal(int, object)
    # The batched forms. A drag carries whole rows rather than one, so the app
    # can make the edit once instead of once per placement.
    placementsMoved = pyqtSignal(object, float, float)
    placementsTransformed = pyqtSignal(object, object)

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
            rows = sorted(self.selected_rows) or [self.selected_placement]
            if len(rows) > 1:
                self.placementsMoved.emit(rows, dx * step, dy * step)
            else:
                self.placementMoved.emit(
                    self.selected_placement, dx * step, dy * step
                )
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

    def items_for_rows(self, rows):
        """The visible top-level items for a set of placement rows.

        ``data(0)`` carries the row only on a direct child of the clip being
        viewed (a nested container has no row of its own), so this walks
        ``instances`` the same way ``selected_item`` does and keeps every match
        rather than the first.
        """
        wanted = set(rows)
        return [
            item
            for item, _ in self.instances.values()
            if item.isVisible() and item.data(0) in wanted
        ]

    def selection_extent(self):
        """The scene-space union of every selected item, or None."""
        items = self.items_for_rows(self.selected_rows)
        if not items:
            return None
        extent = QRectF()
        for item in items:
            box = item.mapToScene(item.boundingRect()).boundingRect()
            extent = box if extent.isNull() else extent.united(box)
        return extent

    def resize_handles(self, item):
        return self.rect_handles(item.boundingRect())

    def rect_handles(self, r):
        """The eight named handle points of a rect, in that rect's own space.

        Shared by a single item's outline and the group box, so the two agree on
        where a handle is and what dragging one means.
        """
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

    def group_extent_hit(self, position):
        """The group handle under the cursor, when several rows are selected.

        The box is in scene space with no item transform of its own, so the
        handles map to screen directly.
        """
        if len(self.selected_rows) < 2:
            return None
        extent = self.selection_extent()
        if extent is None:
            return None
        for name, point in self.rect_handles(extent).items():
            screen = self.mapFromScene(point)
            if (screen - position).manhattanLength() <= 10:
                return name
        return None

    def snap_to_pixel(self, point):
        """Round a scene point onto the device pixel grid.

        Dragging works in whole screen pixels, so a placement lands on the grid
        the artwork was authored against instead of on a fraction of one, which
        reads as a soft edge the moment the view is not aligned to it.
        """
        view = self.transform().map(point)
        inverse = self.transform().inverted()[0]
        return inverse.map(QPointF(round(view.x()), round(view.y())))

    def resize_matrix(self, point, shift=False):
        r, m, handle = self.resize_rect, self.drag_matrix, self.resize_handle
        inv, valid = m.inverted()
        if not valid or r.width() <= 0 or r.height() <= 0:
            return m
        # The handle is dragged in whole pixels, so the edge it moves lands on
        # the pixel grid rather than between two of them.
        local = inv.map(self.snap_to_pixel(point))
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
        self.set_preview_overrides({self.selected_placement: matrix})

    def set_preview_overrides(self, matrices):
        """Hold live transforms for a render without writing them to the file.

        Keyed by placement row, so one selected placement and a whole group use
        the same mechanism; the render walk applies whichever it finds.
        """
        self.matrix_override = {
            row: [
                m.m11(),
                m.m12(),
                m.m21(),
                m.m22(),
                m.dx(),
                m.dy(),
            ]
            for row, m in matrices.items()
        }
        if self.last:
            doc, id, frame = self.last
            self.show_resource(doc, id, frame, fit=False)

    def row_items(self):
        """Top-level row -> item, for every visible item that carries a row."""
        return {
            int(item.data(0)): item
            for item, _ in self.instances.values()
            if item.isVisible() and item.data(0) is not None
        }

    def pick_row(self, row, modifiers):
        """Update the selection for a click on ``row``, honouring modifiers.

        Plain click replaces the selection, Ctrl toggles one row, Shift extends
        from the primary. The primary is the last row picked: it is what the
        per-object resize handles and the animator's transform fields follow,
        while ``selected_rows`` is everything a drag or Delete acts on.
        """
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if ctrl:
            if row in self.selected_rows:
                self.selected_rows.discard(row)
                if self.selected_placement == row:
                    self.selected_placement = (
                        max(self.selected_rows) if self.selected_rows else None
                    )
            else:
                self.selected_rows.add(row)
                self.selected_placement = row
        elif shift and self.selected_placement is not None:
            low, high = sorted((self.selected_placement, row))
            self.selected_rows.update(range(low, high + 1))
        else:
            # A plain press on something already picked keeps the whole
            # selection, so grabbing any member of a group and dragging it moves
            # the group rather than collapsing to the one grabbed.
            if row not in self.selected_rows:
                self.selected_rows = {row}
            self.selected_placement = row

    def mousePressEvent(self, event):
        self.dragged = None
        self.resize_handle = None
        self.group_resize = False
        self.drag_origin = event.position().toPoint()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and getattr(self, "editable", False)
            and not event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            point = self.mapToScene(event.position().toPoint())
            # The group box is drawn in place of the per-object handles when
            # several rows are picked, so its handles are tested first.
            handle = self.group_extent_hit(event.position().toPoint())
            if handle:
                self.group_resize = True
                self.resize_handle = handle
                self.resize_rect = self.selection_extent()
                self.drag_start = point
                self.drag_targets = {
                    row: (item, QTransform(item.transform()))
                    for row, item in self.row_items().items()
                    if row in self.selected_rows
                }
                event.accept()
                return
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
                    row = int(item.data(0))
                    self.pick_row(row, event.modifiers())
                    top_level = any(
                        group is item for group, _ in self.instances.values()
                    )
                    self.drag_start = point
                    self.drag_targets = {
                        r: (i, QTransform(i.transform()))
                        for r, i in self.row_items().items()
                        if r in self.selected_rows
                    }
                    # Dragging offsets every picked placement from where it
                    # started, so a group keeps its shape while it moves.
                    self.dragged = item if top_level else None
                    self.drag_matrix = QTransform(item.transform())
                    if self.selected_placement is not None:
                        self.placementSelected.emit(self.selected_placement)
                    if not top_level:
                        event.accept()
                        return
                    self.viewport().update()
                    event.accept()
                    return
            self.selected_placement = None
            self.selected_rows = set()
            self.viewport().update()
        # A drag on empty space pans the view. Qt rewrites the scene rect as it
        # scrolls, so the free area has to be restored on every move, while the
        # button is still held; a repair after release comes too late.
        self.panning = not (
            getattr(self, "dragged", None) is not None or self.resize_handle
        )
        super().mousePressEvent(event)

    def drag_translation(self, point):
        """The ``(dx, dy)`` a drag from ``drag_start`` to ``point`` applies.

        The raw offset is a difference of two ``mapToScene()`` of integer device
        points, so it is already whole *screen* pixels. That is the device grid,
        not the asset grid: on a scaled or panned view one screen pixel is a
        fraction of a scene unit, so a drag lands between the pixels the artwork
        was authored against. With snapping on the resulting translation is
        rounded in scene units - the units the file stores and that pixel art is
        drawn on - so the placement lands on whole pixels.
        """
        moved = self.mapToScene(point) - self.drag_start
        m = self.drag_matrix
        dx, dy = moved.x(), moved.y()
        if self.snap_movement:
            dx, dy = round(m.dx() + dx) - m.dx(), round(m.dy() + dy) - m.dy()
        return dx, dy

    def drag_past_slop(self, point):
        """True once the pointer has left the pixel the press landed on.

        A press and release with no movement between them is a click, not a
        move. Rounding the zero offset onto the grid would otherwise turn that
        click into a real nudge, because snapping a placement already sitting
        between two pixels moves it onto one. Requiring the pointer to change
        pixel is what separates picking something up from dragging it.
        """
        return point != getattr(self, "drag_origin", point)

    def group_resize_matrix(self, point, shift=False):
        """The scene-space matrix that scales the picked group about its anchor.

        The group box is already in scene space and the picked placements are
        top-level, so one matrix in that space applies to all of them: each
        placement's new transform is its own start transform composed with this.
        """
        r, handle = self.resize_rect, self.resize_handle
        if r is None or r.width() <= 0 or r.height() <= 0:
            return QTransform()
        local = self.snap_to_pixel(point)
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
        # Scaling about an anchor leaves the anchor fixed: p' = (p - a)*s + a,
        # which in this row-vector form puts the whole offset in the
        # translation, the same shape resize_matrix uses.
        return QTransform(sx, 0, 0, sy, ax * (1 - sx), ay * (1 - sy))

    def group_resize_override(self, point, shift=False):
        """Per-row matrices for a group scale, keyed by placement row."""
        group = self.group_resize_matrix(point, shift)
        return {
            row: start * group for row, (_, start) in self.drag_targets.items()
        }

    def mouseMoveEvent(self, event):
        if self.panning and getattr(self, "dragged", None) is None:
            self.keep_free()
        if self.group_resize:
            if not self.drag_past_slop(event.position().toPoint()):
                event.accept()
                return
            self.set_preview_overrides(
                self.group_resize_override(
                    self.mapToScene(event.position().toPoint()),
                    bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                )
            )
            event.accept()
            return
        if getattr(self, "dragged", None) is not None:
            if self.resize_handle:
                # A press on a handle that never moves is not a resize either.
                # The recomputed matrix is not bit-identical to the stored one,
                # so letting it through committed float noise as a real edit.
                if not self.drag_past_slop(event.position().toPoint()):
                    event.accept()
                    return
                self.preview_transform(
                    self.resize_matrix(
                        self.mapToScene(event.position().toPoint()),
                        bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                    )
                )
                event.accept()
                return
            # The offset is whole screen pixels, and snapping rounds it onto the
            # asset grid, so the movement is locked to whole scene pixels rather
            # than to a fraction of one. Nothing happens until the pointer
            # leaves the pixel it started on, so a click never nudges.
            if not self.drag_past_slop(event.position().toPoint()):
                event.accept()
                return
            dx, dy = self.drag_translation(event.position().toPoint())
            # Every picked placement keeps its shape: each is offset from the
            # transform it had when the drag started, not from the last move.
            for row, (item, start) in self.drag_targets.items():
                item.setTransform(
                    QTransform(
                        start.m11(),
                        start.m12(),
                        start.m21(),
                        start.m22(),
                        start.dx() + dx,
                        start.dy() + dy,
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
        if self.group_resize:
            moved = self.drag_past_slop(event.position().toPoint())
            # The group matrix is worked out before the drag state is cleared:
            # it anchors on self.resize_rect, so clearing that first made it
            # fall back to the identity and the resize committed as a no-op.
            group = (
                self.group_resize_matrix(
                    self.mapToScene(event.position().toPoint()),
                    bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                )
                if moved
                else None
            )
            rows = sorted(self.drag_targets)
            self.group_resize = False
            self.resize_handle = None
            self.resize_rect = None
            self.drag_targets = {}
            self.matrix_override = None
            if group is not None:
                self.placementsTransformed.emit(rows, group)
            elif self.last:
                self.show_resource(*self.last, fit=False)
            event.accept()
            return
        if getattr(self, "dragged", None) is not None:
            if self.resize_handle:
                # A press on a handle that never moved is not a resize. The
                # recomputed matrix differs from the stored one by float noise,
                # so comparing alone committed that noise as a real edit.
                moved = self.drag_past_slop(event.position().toPoint())
                matrix = (
                    self.resize_matrix(
                        self.mapToScene(event.position().toPoint()),
                        bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                    )
                    if moved
                    else self.drag_matrix
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
            # A press and release with no movement between them is not a move, so it
            # never extends the clip: only a real drag does. The emitted delta is
            # the same one the live drag applied, so the committed transform
            # matches what was on screen - snapped or not. Snapping a placement
            # that already sits between pixels rounds it onto one, so the slop
            # check is what stops a plain click from nudging it.
            dx, dy = self.drag_translation(event.position().toPoint())
            row = self.selected_placement
            rows = sorted(self.drag_targets)
            self.dragged = None
            self.drag_targets = {}
            if self.drag_past_slop(event.position().toPoint()) and (
                abs(dx) > 0.01 or abs(dy) > 0.01
            ):
                # One edit for the whole set, so a group drag is one undo step
                # rather than one per placement.
                if len(rows) > 1:
                    self.placementsMoved.emit(rows, dx, dy)
                else:
                    self.placementMoved.emit(row, dx, dy)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self.panning = False
        self.keep_free()

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
        pen = QPen(QColor("#63b5ff"))
        radius = 4 / max(self.transform().m11(), 1e-9)
        selected = self.items_for_rows(self.selected_rows)
        if selected:
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for item in selected:
                painter.drawPolygon(item.mapToScene(item.boundingRect()))
            painter.setBrush(QColor("#63b5ff"))
            if len(selected) > 1:
                # Several placements: one box around them all, with handles that
                # scale the group about its centre. Per-object handles would be
                # ambiguous - eight squares per placement, all doing different
                # things - so only the group box carries them.
                extent = self.selection_extent()
                if extent is not None:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(extent)
                    painter.setBrush(QColor("#63b5ff"))
                    for point in self.rect_handles(extent).values():
                        painter.drawRect(
                            QRectF(
                                point.x() - radius,
                                point.y() - radius,
                                radius * 2,
                                radius * 2,
                            )
                        )
            else:
                for point in self.resize_handles(selected[0]).values():
                    p = selected[0].mapToScene(point)
                    painter.drawRect(
                        QRectF(
                            p.x() - radius, p.y() - radius, radius * 2, radius * 2
                        )
                    )

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.scene().setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.instances = {}
        self.scene_key = None
        self.apply_theme()
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(self.ViewportAnchor.NoAnchor)
        # Repaint the whole viewport on every update rather than only the
        # changed region. The selection outline and its handles are drawn in
        # the foreground, outside the scene's own item tracking, so a partly
        # repainted viewport leaves the handles where they used to be instead
        # of clearing them.
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # True while a pan drag is in progress. Qt reads the scene rect during
        # the drag, so it has to be repaired while the mouse is still down.
        self.panning = False
        # Device-pixel position of the last press, so a click that never moves
        # the pointer is told apart from a real drag.
        self.drag_origin = None
        # The primary row is the last one picked; it is what the resize handles
        # and the animator's transform fields follow. selected_rows is every row
        # picked, which is what a drag and Delete act on. Keeping the primary a
        # scalar leaves the whole single-selection path - selected_item, the
        # resize rect and matrix, the live override - exactly as it was.
        self.selected_rows = set()
        # Per-row transform snapshots taken at press, so a group drag offsets
        # every placement from where it started rather than compounding.
        self.drag_targets = {}
        # True while a group-box handle is being dragged rather than one
        # placement's own handle, which take different paths to a matrix.
        self.group_resize = False
        # Off for a sheet fitted to the view, so keep_free() never widens the
        # browsable area away from the image.
        self.free_camera = True
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
        # Cleared per render: keyed by clip identity, frame and row.
        self.inherit_cache = {}
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
        # Scrollable area for the open resource; set once, then left alone.
        self.view_rect = None
        # Snap a dragged placement onto whole scene pixels, which are the asset
        # pixels the file stores. Off means a drag follows the screen pixel grid
        # exactly, which on a zoomed view lands between stored pixels.
        self.snap_movement = True
        self.setMouseTracking(True)

    def apply_theme(self):
        """Colour the canvas from the current theme.

        Derived from the palette rather than a fixed colour, so every scheme
        recolours it. The window colour is used for the near-part: a canvas
        matching the surrounding chrome makes the artwork read as being on top
        of the interface rather than in a separate place.
        """
        # Read the application palette rather than this widget's: the theme is
        # installed there, and a widget's own copy is only refreshed once Qt
        # re-polishes it, which has not happened yet at this point.
        window = QApplication.instance().palette().color(
            QPalette.ColorRole.Window
        )
        if window.lightness() < 128:
            # Darker than the chrome on a dark theme, so the canvas still reads
            # as a distinct surface, and the default for game art, which is
            # drawn to be seen against a dark backdrop.
            back = window.darker(140)
        else:
            # On a light theme the artwork needs to sit on something quieter
            # than the chrome, or the whole window is one flat white.
            back = window.darker(112)
        self.setBackgroundBrush(back)
        self.viewport().update()

    def invalidate(self, keep_atlases=False):
        self.atlas_regions = None
        self.atlas_borders = []
        self.atlas_marks = []
        self.view_rect = None
        self.matrix_override = None
        if not keep_atlases:
            self.cache.clear()
        self.resource_cache.clear()
        self.shape_cache.clear()
        self.instances.clear()
        self.scene_key = None
        self.scene().clear()

    # Half-extent of the pannable area, in scene units. Large enough that the
    # view never runs into an edge while working, so the camera is effectively
    # free rather than boxed into the artwork's bounds.
    PAN_MARGIN = 20000.0

    def free_rect(self):
        """A wide, symmetric area around the content, used while editing.

        Qt clamps the view to the scene rect, so a rect sized to the artwork
        would stop the camera at the artwork's edge. Anchoring a much larger
        rect on the content leaves room to pan in any direction.
        """
        rect = self.content_rect
        if rect.isEmpty():
            rect = QRectF(-100, -100, 200, 200)
        if not self.free_camera:
            # A sheet fitted to the view (an atlas) is browsed by zooming and
            # scrolling around the image itself, so it keeps an area that
            # matches the sheet exactly instead of the wide editing one.
            return QRectF(rect)
        center = rect.center()
        return QRectF(
            center.x() - self.PAN_MARGIN,
            center.y() - self.PAN_MARGIN,
            self.PAN_MARGIN * 2,
            self.PAN_MARGIN * 2,
        )

    def fit_content(self):
        """Frame the content without narrowing the pannable area to it."""
        self.view_rect = self.free_rect()
        self.scene().setSceneRect(self.view_rect)
        self.resetTransform()
        self.fitInView(self.content_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.fit_scale = self.transform().m11()
        # fitInView refits the scene rect around the content, undoing the line
        # above; the free area is restored so the camera stays unconstrained.
        self.keep_free()

    def keep_free(self):
        """Put the wide scrollable area back after Qt rewrites it.

        ``fitInView`` and ``centerOn`` both fit the scene rect around what they
        are showing, which would narrow the pannable area back onto the artwork
        and lock the camera to it. The area is therefore re-applied immediately
        after every such call, and once more from the paint event, since that is
        where the camera has its final say before anything is drawn.
        """
        if self.view_rect is None:
            return
        scene = self.scene().sceneRect()
        # An open atlas is zoomed around freely and needs an area that follows
        # the camera out, so it only ever widens; elsewhere the area is put back
        # exactly, since that is the whole point of the free camera.
        wanted = (
            scene.united(self.view_rect) if not self.free_camera else self.view_rect
        )
        if scene != wanted:
            self.scene().setSceneRect(wanted)

    def sizeHint(self):
        """Prevent the scene rect from dictating the view's preferred size.

        A view's stock hint is taken from its scene, so the wide pannable area
        would advertise a forty-thousand-pixel canvas and the layout would grow
        to match — every pane re-flowing the moment a resource was shown. The
        view is sized by the splitter it sits in, so a modest hint is enough.
        """
        return QSize(480, 320)

    def paintEvent(self, event):
        self.keep_free()
        super().paintEvent(event)

    def show_atlas(self, doc, index):
        # The atlas is a flat sheet fitted to the view, so any scrollable area
        # kept for a movieclip must not carry over to it, and the area it does
        # get is the sheet itself rather than the wide editing one.
        self.view_rect = None
        self.free_camera = False
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
        self.selected_rows = set()
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
            # Growing the area only when it is actually needed is what keeps a
            # pan away from the artwork's edge working: reclaiming the scene
            # rect first would bounce the camera back, one scroll at a time.
            camera = QRectF(target.x() - w / 2, target.y() - h / 2, w, h)
            if not self.scene().sceneRect().contains(camera):
                self.scene().setSceneRect(self.scene().sceneRect().united(camera))
            self.centerOn(target)
            self.keep_free()
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
        # Zoom-out needs to be able to pan to whatever is being shown; the area
        # grows to meet it, never shrinking back towards the artwork.
        if not self.scene().sceneRect().contains(camera):
            self.scene().setSceneRect(self.scene().sceneRect().united(camera))
        self.centerOn(target)
        self.keep_free()
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

    def render_triangle(self, painter, vertices, image):
        """Paint one triangle-strip face, for exports outside a scene.

        ``shape_surface`` builds a scene-bound surface; an export needs the same
        three-vertex draw against a plain painter, so the geometry is shared
        here rather than duplicated in the caller.
        """
        Triangle(vertices, image, None).paint(painter, None)

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
        self.inherit_cache = {}
        self.free_camera = True
        self.doc = doc
        self.editable = doc.index[id][0] == "movieclips"
        key = (builtins.id(doc), id, builtins.id(doc.index[id][1]))
        if key != self.scene_key:
            # A different resource gets its own scrollable area.
            self.view_rect = None
        if (
            len(self.instances) > 6000
            or key != self.scene_key
            or (self.instances and not self.scene().items())
        ):
            self.scene().clear()
            self.instances.clear()
            self.scene_key = key
            self.selected_placement = None
            self.selected_rows = set()
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
        if fit or self.view_rect is None:
            # The scrollable area is set once for the resource and left alone
            # afterwards, so scrubbing and playback never move the camera even
            # though each frame has different bounds. It is anchored on the
            # content but far larger than it, so the camera stays free to pan
            # anywhere rather than stopping at the artwork's edge.
            camera = self.mapToScene(self.viewport().rect()).boundingRect()
            self.view_rect = self.free_rect().united(camera)
            self.scene().setSceneRect(self.view_rect)
        if getattr(self, "dragged", None) is None and self.view_rect is not None:
            # Panning may leave the visible area outside the stored rect; grow
            # it so the scrollbars stay usable, but never shrink it on a frame
            # change, which is what made the view drift before.
            camera = self.mapToScene(self.viewport().rect()).boundingRect()
            if not self.view_rect.contains(camera):
                self.view_rect = self.view_rect.united(camera)
                self.scene().setSceneRect(self.view_rect)
        if fit:
            self.fit_content()
        self.last = (doc, id, frame)
        return "; ".join(dict.fromkeys(self.messages))

    def inherited_matrix(self, clip, frame, row):
        """The transform a frame with no key of its own renders with.

        The engine resolves an unset placement by carrying the last transform
        that was set forward, so a frame between two keys holds the earlier one
        rather than snapping back to no transform at all. Identity is only the
        answer when nothing earlier in the clip has set anything, which is also
        why removing the first key of a clip is what makes it read as origin.

        The bank is read here rather than passed in: the caller skips loading it
        for a frame whose own elements are all unset, which is exactly the frame
        that needs the earlier values looked up.
        """
        # Cached per clip and frame for the length of one render: a clip draws
        # many elements that inherit, and each lookup otherwise walks backwards
        # through every earlier frame, which is quadratic in the clip's length.
        key = (builtins.id(clip), frame, row)
        cached = self.inherit_cache.get(key)
        if cached is not None:
            return cached
        frames = clip["frames"]
        index = clip.get("matrix_bank_index")
        banks = self.doc.storage["matrix_banks"]
        matrices = (
            self.doc.bank(index)["matrices"]
            if index is not None and index < len(banks)
            else []
        )
        start = min(frame, len(frames) - 1)
        answer = list(IDENTITY_MATRIX)
        for earlier in range(start, -1, -1):
            elements = frames[earlier]["elements"]
            if row >= len(elements):
                continue
            matrix_index = elements[row][1]
            if matrix_index != 65535:
                answer = (
                    matrices[matrix_index]
                    if matrix_index < len(matrices)
                    else list(IDENTITY_MATRIX)
                )
                break
        # Stored as a tuple so a caller that mutates what it is given cannot
        # poison the cache for the rest of the render.
        self.inherit_cache[key] = answer
        return answer

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
                # A frame that sets no transform of its own inherits the
                # nearest earlier one, exactly as the engine resolves it. Only
                # a placement with no earlier key at all falls back to identity.
                if mi != 65535:
                    matrix = bank["matrices"][mi]
                else:
                    matrix = self.inherited_matrix(obj, frame, i)
                if not path and self.matrix_override and i in self.matrix_override:
                    matrix = self.matrix_override[i]
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
            text.setTextWidth(max(1, obj["right"] - obj["left"]))
            # Alignment is stored as a packed bitfield and has to be applied to
            # the document's text option; without this every textfield previewed
            # left-aligned at the top, so changing the alignment changed nothing
            # on screen. The vertical offset is worked out after layout, when the
            # text's own height is known, and the same packing the renderer uses
            # is read here so the two agree.
            option = text.document().defaultTextOption()
            option.setAlignment(
                {
                    0: Qt.AlignmentFlag.AlignLeft,
                    1: Qt.AlignmentFlag.AlignHCenter,
                    2: Qt.AlignmentFlag.AlignRight,
                    3: Qt.AlignmentFlag.AlignJustify,
                }.get(obj["align"] & 3, Qt.AlignmentFlag.AlignLeft)
            )
            option.setWrapMode(
                QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
                if obj["styles"] & 16
                else QTextOption.WrapMode.NoWrap
            )
            text.document().setDefaultTextOption(option)
            vertical = (obj["align"] >> 2) & 3
            space = (obj["bottom"] - obj["top"]) - text.boundingRect().height()
            dy = space / 2 if vertical == 1 else space if vertical in (2, 3) else 0
            text.setPos(obj["left"], obj["top"] + dy)
            box = QRectF(
                obj["left"],
                obj["top"],
                obj["right"] - obj["left"],
                obj["bottom"] - obj["top"],
            )
            # Cosmetic, so the helper stays a hairline whatever it inherits. A
            # textfield nested in a movieclip that is then scaled inside
            # another movieclip otherwise draws a stroke multiplied by every
            # ancestor transform - the outline thickens into a band and stops
            # marking the field box it is there to mark.
            outline = QPen(QColor("#68778e"))
            outline.setCosmetic(True)
            bounds = self.scene().addRect(box, outline)
            bounds.setParentItem(group)
            # The textfield is measured by its field box, not by its glyphs, so
            # the grey box, the blue selection outline and the resize handles
            # all frame the same rectangle. Without this the container reported
            # the text's height instead, and a vertical resize scaled about the
            # wrong edge, leaving the box short of the cursor. The half-pen
            # padding matches the stroke the grey rect is drawn with, so the two
            # outlines coincide exactly.
            group.logical_bounds = box.adjusted(-0.5, -0.5, 0.5, 0.5)
        elif kind == "modifiers":
            self.messages.append(
                "Mask modifiers are preserved; stencil masks are not simulated"
            )
        return group
