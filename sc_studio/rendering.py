"""Isolated-layer compositor for effects, masks and image/animation export.

Uses the same bitmap triangle mapper and nine-slice geometry as the editor.
Unlike the interactive scene, each instance is rendered into an isolated layer
so color transforms and blend operations affect the complete symbol.
"""

import math
from pathlib import Path
from PIL import Image, ImageChops
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import (
    QImage,
    QPainter,
    QTransform,
    QFont,
    QColor,
    QTextDocument,
    QTextOption,
    QTextCharFormat,
    QPen,
    QTextCursor,
)
from PyQt6.QtWidgets import QGraphicsScene
from .authoring import (
    resolved_frames,
    multiply,
    IDENTITY,
    WHITE,
    MASK_BEGIN,
    MASK_CONTENT,
    MASK_END,
)
from .preview import Triangle, grid_commands
from .publishing import decode_texture


def pil_image(image):
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes(
        "RGBA",
        (rgba.width(), rgba.height()),
        rgba.constBits().asstring(rgba.sizeInBytes()),
    )


def qt_image(image):
    image = image.convert("RGBA")
    return QImage(
        image.tobytes(),
        image.width,
        image.height,
        image.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()


def colorize(image, color):
    if color == WHITE:
        return image
    im = pil_image(image)
    channels = im.split()
    out = [
        channels[i].point(
            [
                min(
                    255,
                    max(0, round(v * color[i] / 255 + (color[i + 4] if i < 3 else 0))),
                )
                for v in range(256)
            ]
        )
        for i in range(4)
    ]
    return qt_image(Image.merge("RGBA", out))


COMPOSITIONS = {
    0: QPainter.CompositionMode.CompositionMode_SourceOver,
    1: QPainter.CompositionMode.CompositionMode_SourceOver,
    2: QPainter.CompositionMode.CompositionMode_Multiply,
    3: QPainter.CompositionMode.CompositionMode_Screen,
    4: QPainter.CompositionMode.CompositionMode_Lighten,
    5: QPainter.CompositionMode.CompositionMode_Darken,
    6: QPainter.CompositionMode.CompositionMode_Difference,
    7: QPainter.CompositionMode.CompositionMode_Plus,
    10: QPainter.CompositionMode.CompositionMode_DestinationIn,
    11: QPainter.CompositionMode.CompositionMode_DestinationOut,
    12: QPainter.CompositionMode.CompositionMode_Overlay,
    13: QPainter.CompositionMode.CompositionMode_HardLight,
}


def composite(target, layer, blend=0):
    blend &= 0x3F  # upper bits contain instance flags, not blend IDs
    if blend == 8:
        dest, src = pil_image(target), pil_image(layer)
        subtract = ImageChops.subtract(dest.convert("RGB"), src.convert("RGB")).convert(
            "RGBA"
        )
        subtract.putalpha(dest.getchannel("A"))
        result = Image.composite(subtract, dest, src.getchannel("A"))
        painter = QPainter(target)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(0, 0, qt_image(result))
        painter.end()
        return
    if blend == 9:
        source = pil_image(layer)
        white = Image.new("RGBA", source.size, "white")
        white.putalpha(source.getchannel("A"))
        layer = qt_image(white)
        blend = 6
    if blend not in COMPOSITIONS:
        raise ValueError(f"Unsupported blend mode {blend}")
    painter = QPainter(target)
    try:
        painter.setCompositionMode(COMPOSITIONS[blend])
        painter.drawImage(0, 0, layer)
    finally:
        painter.end()


class Renderer:
    def __init__(self, doc):
        self.doc = doc
        self.resources = {}
        self.frames = {}
        self.atlases = {}
        self.count = 0

    def resource(self, id):
        if id not in self.resources:
            self.resources[id] = self.doc.resource(id)
        return self.resources[id]

    def timeline(self, id):
        if id not in self.frames:
            self.frames[id] = resolved_frames(self.doc, self.resource(id))
        return self.frames[id]

    def atlas(self, index):
        if index not in self.atlases:
            texture = self.doc.tables["Textures"]["textures"][index]
            self.atlases[index] = qt_image(
                decode_texture(self.doc, texture["highres"] or texture["lowres"])
            )
        return self.atlases[index]

    def bounds(self, id, frame=0, matrix=None, stack=frozenset()):
        if id in stack:
            return QRectF()
        matrix = matrix or IDENTITY
        kind = self.doc.index[id][0]
        obj = self.resource(id)
        if kind == "movieclips":
            frames = self.timeline(id)
            if not frames:
                return QRectF()
            rect = QRectF()
            for ci, m, _ in frames[frame % len(frames)]["placements"]:
                rect = rect.united(
                    self.bounds(
                        obj["children"][ci]["id"],
                        frame,
                        multiply(matrix, m),
                        stack | {id},
                    )
                )
            return rect
        if kind == "shapes":
            vs = [v for c in obj["commands"] for v in c["vertices"]]
            if not vs:
                return QRectF()
            rect = QRectF(
                QPointF(min(v[0] for v in vs), min(v[1] for v in vs)),
                QPointF(max(v[0] for v in vs), max(v[1] for v in vs)),
            )
        elif kind == "textfields":
            rect = QRectF(
                obj["left"] - 3,
                obj["top"] - 3,
                obj["right"] - obj["left"] + 6,
                obj["bottom"] - obj["top"] + 6,
            )
        else:
            return QRectF()
        return QTransform(*matrix).mapRect(rect)

    def blank(self):
        out = QImage(self.width, self.height, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(0)
        return out

    def render(self, id, frame=0, bounds=None, scale=1):
        rect = bounds or self.bounds(id, frame)
        if rect.isEmpty():
            rect = QRectF(0, 0, 1, 1)
        if scale <= 0 or not math.isfinite(scale):
            raise ValueError("Render scale must be positive")
        self.width = max(1, math.ceil(rect.width() * scale))
        self.height = max(1, math.ceil(rect.height() * scale))
        if max(self.width, self.height) > 8192 or self.width * self.height > 32_000_000:
            raise ValueError("Render exceeds 8192 pixels or 32 megapixels")
        self.view = [scale, 0, 0, scale, -rect.left() * scale, -rect.top() * scale]
        self.count = 0
        return self.draw(id, frame, IDENTITY, frozenset()), rect

    def draw(self, id, frame, matrix, stack, deformation=None):
        if id in stack:
            raise ValueError(f"Recursive resource graph at {id}")
        if len(stack) > 64:
            raise ValueError("Render nesting limit exceeded")
        if self.width * self.height * (len(stack) + 1) > 64_000_000:
            raise ValueError("Render layer memory budget exceeded; use a smaller scale")
        self.count += 1
        if self.count > 10000:
            raise ValueError("Render object limit exceeded")
        out = self.blank()
        obj = self.resource(id)
        kind = self.doc.index[id][0]
        transform = QTransform(*multiply(self.view, matrix))
        if kind == "movieclips":
            frames = self.timeline(id)
            if not frames:
                return out
            scopes = []
            active = out
            grid = obj.get("scaling_grid")
            gd = None
            if grid:
                sx, sy = (
                    math.hypot(matrix[0], matrix[1]),
                    math.hypot(matrix[2], matrix[3]),
                )
                gd = (grid["left"], grid["top"], grid["right"], grid["bottom"], sx, sy)
            for ci, m, co in frames[frame % len(frames)]["placements"]:
                child = obj["children"][ci]
                child_kind = self.doc.index[child["id"]][0]
                if child_kind == "modifiers":
                    typ = self.resource(child["id"])["type"]
                    if typ == MASK_BEGIN:
                        scopes.append(
                            {
                                "parent": active,
                                "mask": self.blank(),
                                "content": self.blank(),
                                "mode": "mask",
                            }
                        )
                        active = scopes[-1]["mask"]
                    elif typ == MASK_CONTENT:
                        if not scopes:
                            raise ValueError("Masked content without mask begin")
                        scopes[-1]["mode"] = "content"
                        active = scopes[-1]["content"]
                    elif typ == MASK_END:
                        if not scopes:
                            raise ValueError("Mask end without mask begin")
                        scope = scopes.pop()
                        composite(scope["content"], scope["mask"], 10)
                        active = scope["parent"]
                        composite(active, scope["content"])
                    continue
                layer = self.draw(
                    child["id"],
                    frame,
                    multiply(matrix, m),
                    stack | {id},
                    gd if m == IDENTITY else None,
                )
                composite(active, colorize(layer, co), child.get("blend", 0))
            if scopes:
                raise ValueError("Unclosed stencil mask range")
        elif kind == "shapes":
            scene = QGraphicsScene()
            for command in grid_commands(obj, deformation):
                vertices = command["vertices"]
                image = self.atlas(command["texture_index"])
                if len(vertices) < 3:
                    continue
                # Constant UV fills are common in SC UI assets.
                if len({(v[2], v[3]) for v in vertices}) == 1:
                    u, v = vertices[0][2:]
                    color = image.pixelColor(
                        min(image.width() - 1, int(u / 65535 * image.width())),
                        min(image.height() - 1, int(v / 65535 * image.height())),
                    )
                    from PyQt6.QtGui import QPainterPath

                    path = QPainterPath()
                    for i in range(2, len(vertices)):
                        triangle = vertices[i - 2 : i + 1]
                        path.moveTo(triangle[0][0], triangle[0][1])
                        path.lineTo(triangle[1][0], triangle[1][1])
                        path.lineTo(triangle[2][0], triangle[2][1])
                        path.closeSubpath()
                    path = path.simplified()
                    item = scene.addPath(path, QPen(Qt.PenStyle.NoPen), color)
                    item.setTransform(transform)
                else:
                    for i in range(2, len(vertices)):
                        # SC bitmap geometry uses triangle strips.
                        item = Triangle(vertices[i - 2 : i + 1], image, None)
                        item.setTransform(transform)
                        scene.addItem(item)
            painter = QPainter(out)
            try:
                # Triangles share edges; independent antialiasing leaves alpha seams.
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                scene.render(
                    painter,
                    QRectF(0, 0, self.width, self.height),
                    QRectF(0, 0, self.width, self.height),
                )
            finally:
                painter.end()
        elif kind == "textfields":
            document = QTextDocument()
            document.setDocumentMargin(0)
            font = QFont(obj["font_name"])
            font.setPixelSize(max(1, obj["font_size"]))
            font.setBold(bool(obj["styles"] & 4))
            font.setItalic(bool(obj["styles"] & 8))
            font.setKerning(bool(obj["styles"] & 64))
            document.setDefaultFont(font)
            option = QTextOption()
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
            document.setDefaultTextOption(option)
            document.setPlainText(obj["text"])
            document.setTextWidth(max(1, obj["right"] - obj["left"]))
            cursor = QTextCursor(document)
            cursor.select(QTextCursor.SelectionType.Document)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor.fromRgba(obj["font_color"]))
            if obj["styles"] & 2:
                fmt.setTextOutline(QPen(QColor.fromRgba(obj["outline_color"]), 1.5))
            cursor.mergeCharFormat(fmt)
            vertical = (obj["align"] >> 2) & 3
            space = obj["bottom"] - obj["top"] - document.size().height()
            dy = space / 2 if vertical == 1 else space if vertical in (2, 3) else 0
            painter = QPainter(out)
            try:
                painter.setTransform(transform)
                painter.translate(obj["left"], obj["top"] + dy)
                document.drawContents(painter)
            finally:
                painter.end()
        return out


def export_animation(doc, id, path, scale=1):
    renderer = Renderer(doc)
    obj = renderer.resource(id)
    count = len(obj["frames"]) if doc.index[id][0] == "movieclips" else 1
    bounds = QRectF()
    for fi in range(count):
        bounds = bounds.united(renderer.bounds(id, fi))
    if (
        count * max(1, bounds.width() * scale) * max(1, bounds.height() * scale)
        > 100_000_000
    ):
        raise ValueError(
            "Animation export exceeds 100 megapixels; export a smaller scale"
        )
    frames = [
        pil_image(renderer.render(id, fi, bounds, scale)[0]) for fi in range(count)
    ]
    path = Path(path)
    duration = max(1, round(1000 / obj.get("framerate", 30)))
    if path.suffix.lower() == ".gif":
        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=0,
            disposal=2,
        )
    elif path.suffix.lower() == ".webp":
        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=0,
            lossless=True,
        )
    elif path.suffix.lower() == ".png" and count == 1:
        frames[0].save(path)
    else:
        raise ValueError("Use GIF/WebP for animation, PNG for a single frame")
    return {
        "frames": count,
        "width": frames[0].width,
        "height": frames[0].height,
        "path": str(path),
    }
