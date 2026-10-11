"""Portable authoring operations; GUI and automation share these functions.

SC stores baked frames rather than authoring curves, layer folders or rigs.
Operations deliberately produce ordinary SC objects and placement arrays.
"""

import copy
import math
from contextlib import contextmanager
from PIL import Image
from . import easing

IDENTITY = [1, 0, 0, 1, 0, 0]
WHITE = [255, 255, 255, 255, 0, 0, 0]
BLENDS = (
    "Normal",
    "Layer",
    "Multiply",
    "Screen",
    "Lighten",
    "Darken",
    "Difference",
    "Add",
    "Subtract",
    "Invert",
    "Alpha",
    "Erase",
    "Overlay",
    "Hard light",
)
MASK_BEGIN, MASK_CONTENT, MASK_END = 38, 39, 40


@contextmanager
def transaction(doc):
    """Restore the complete document if a compound operation fails validation."""
    before = copy.deepcopy(doc.__dict__)
    try:
        yield
        errors = doc.validate()
        if errors:
            raise ValueError("\n".join(errors[:20]))
    except Exception:
        doc.__dict__ = before
        raise


def multiply(parent, local):
    a, b, c, d, x, y = parent
    e, f, g, h, u, v = local
    return [
        a * e + c * f,
        b * e + d * f,
        a * g + c * h,
        b * g + d * h,
        a * u + c * v + x,
        b * u + d * v + y,
    ]


def resolved_frames(doc, clip):
    """Resolve independent matrix/color holds by child occurrence, not draw order.

    A child can move in the draw order or disappear and return. Its inherited
    pose belongs to that instance, rather than whichever occupies its old row.
    """
    bank = None
    matrices, colors, result = {}, {}, []
    for frame in clip["frames"]:
        occurrences, placements = {}, []
        for ci, mi, co in frame["elements"]:
            occurrence = occurrences.get(ci, 0)
            occurrences[ci] = occurrence + 1
            key = (ci, occurrence)
            if mi != 65535 or co != 65535:
                bank = bank or doc.bank(clip["matrix_bank_index"])
            if mi != 65535:
                matrices[key] = list(bank["matrices"][mi])
            if co != 65535:
                colors[key] = list(bank["colors"][co])
            placements.append(
                (ci, list(matrices.get(key, IDENTITY)), list(colors.get(key, WHITE)))
            )
        result.append({"label": frame.get("label", ""), "placements": placements})
    return result


def write_frames(doc, clip, frames):
    matrices, colors, matrix_ids, color_ids = [], [], {}, {}
    output = []
    for frame in frames:
        elements = []
        for ci, matrix, color in frame["placements"]:
            if len(matrix) != 6 or not all(math.isfinite(v) for v in matrix):
                raise ValueError("Transform must have six finite numbers")
            if len(color) != 7 or any(not 0 <= v <= 255 for v in color):
                raise ValueError("Color transform must have seven values in 0..255")
            mk, ck = tuple(matrix), tuple(color)
            if mk not in matrix_ids:
                matrix_ids[mk] = len(matrices)
                matrices.append(matrix)
            if ck not in color_ids:
                color_ids[ck] = len(colors)
                colors.append(color)
            elements.append([ci, matrix_ids[mk], color_ids[ck]])
        output.append({"label": frame.get("label", ""), "elements": elements})
    if max(len(matrices), len(colors)) > 65535:
        raise ValueError("Transform bank exceeds SC limits; split the animation")
    clip["matrix_bank_index"] = doc.add_bank(matrices, colors)
    clip["frames"] = output
    doc.update("movieclips", clip)


def image_shape(doc, image, width=None, height=None):
    image = image.convert("RGBA")
    if not 1 <= image.width <= 65535 or not 1 <= image.height <= 65535:
        raise ValueError("Image dimensions exceed SC limits")
    textures = doc.tables["Textures"]["textures"]
    if len(textures) >= 65535:
        raise ValueError("Texture index limit reached")
    ti = len(textures)
    textures.append(
        {
            "highres": {
                "texture_format": 0,
                "pixel_type": 0,
                "width": image.width,
                "height": image.height,
                "data": image.tobytes(),
                "external_texture": None,
            },
            "lowres": None,
        }
    )
    doc.dirty.add("Textures")
    sid = doc.next_id()
    w, h = width or image.width, height or image.height
    doc.update(
        "shapes",
        {
            "id": sid,
            "exports": [],
            "commands": [
                {
                    "unk1": 0,
                    "texture_index": ti,
                    "vertices": [
                        [0, 0, 0, 0],
                        [w, 0, 65535, 0],
                        [0, h, 0, 65535],
                        [w, h, 65535, 65535],
                    ],
                }
            ],
        },
    )
    return sid


def svg_shape(doc, data, width=None, height=None):
    """Rasterize SVG paths, strokes, gradients and bitmap fills using Qt SVG."""
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtCore import QByteArray, QRectF

    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise ValueError("Invalid or unsupported SVG")
    size = renderer.defaultSize()
    w, h = int(width or size.width()), int(height or size.height())
    if not 1 <= w <= 8192 or not 1 <= h <= 8192 or w * h > 32_000_000:
        raise ValueError("SVG raster size must be within 8192 pixels and 32 megapixels")
    image = QImage(w, h, QImage.Format.Format_RGBA8888)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer.render(painter, QRectF(0, 0, w, h))
    finally:
        painter.end()
    pixels = image.constBits().asstring(image.sizeInBytes())
    return image_shape(doc, Image.frombytes("RGBA", (w, h), pixels))


def bake_parenting(doc, clip_id, parents):
    """Compose local tracks through a rig and bake world transforms in one edit."""
    parents = {int(k): int(v) for k, v in parents.items()}
    clip = doc.resource(clip_id)
    count = len(clip["children"])
    if any(not 0 <= i < count for pair in parents.items() for i in pair):
        raise ValueError("Rig references a missing child track")
    for child in parents:
        seen = set()
        cursor = child
        while cursor in parents:
            if cursor in seen:
                raise ValueError("Parenting cycle")
            seen.add(cursor)
            cursor = parents[cursor]
    frames = resolved_frames(doc, clip)
    for frame in frames:
        by_child = {ci: m for ci, m, _ in frame["placements"]}
        if len(by_child) != len(frame["placements"]):
            raise ValueError("Parenting needs unique child instances per frame")

        def world(ci):
            local = by_child.get(ci)
            if local is None:
                raise ValueError(f"Parent track {ci} is not placed in a frame")
            return multiply(world(parents[ci]), local) if ci in parents else local

        frame["placements"] = [(ci, world(ci), co) for ci, _, co in frame["placements"]]
    write_frames(doc, clip, frames)


def tween_colors(doc, clip_id, child_index, start, end, curve="Linear"):
    clip = doc.resource(clip_id)
    frames = resolved_frames(doc, clip)
    if not 0 <= start < end < len(frames):
        raise ValueError("Choose an increasing frame range")

    def placement(fi):
        matches = [p for p in frames[fi]["placements"] if p[0] == child_index]
        if len(matches) != 1:
            raise ValueError(
                "Color tween needs exactly one placement of the child in every frame"
            )
        return matches[0]

    a, b = placement(start)[2], placement(end)[2]
    points = easing.curve_for(curve)
    for fi in range(start + 1, end):
        ci, m, _ = placement(fi)
        t = easing.bezier((fi - start) / (end - start), *points)
        color = [min(255, max(0, round(x + (y - x) * t))) for x, y in zip(a, b)]
        frames[fi]["placements"] = [
            (i, mat, color if i == ci else co)
            for i, mat, co in frames[fi]["placements"]
        ]
    write_frames(doc, clip, frames)


def morph_shapes(doc, start_id, end_id, count, curve="Linear", framerate=30):
    """Bake matching shape topology to a movieclip of distinct shape frames."""
    if not 2 <= count <= 65535 or not 1 <= framerate <= 255:
        raise ValueError("Invalid frame count or framerate")
    a, b = doc.resource(start_id), doc.resource(end_id)
    if doc.index[start_id][0] != "shapes" or doc.index[end_id][0] != "shapes":
        raise ValueError("Morph endpoints must be shapes")
    if len(a["commands"]) != len(b["commands"]) or any(
        len(x["vertices"]) != len(y["vertices"])
        or x["texture_index"] != y["texture_index"]
        for x, y in zip(a["commands"], b["commands"])
    ):
        raise ValueError("Morph endpoints need matching command topology and textures")
    if count > 65535 - len(doc.index):
        raise ValueError("Not enough resource IDs for the morph")
    cid = doc.new_resource("movieclips")
    clip = doc.resource(cid)
    clip["framerate"] = framerate
    clip["children"] = []
    clip["frames"] = []
    for fi in range(count):
        t = easing.bezier(fi / (count - 1), *easing.curve_for(curve))
        shape = copy.deepcopy(a)
        shape["id"] = doc.next_id()
        shape["exports"] = []
        for out, end in zip(shape["commands"], b["commands"]):
            out["vertices"] = [
                [
                    v[k] + (w[k] - v[k]) * t
                    if k < 2
                    else min(65535, max(0, round(v[k] + (w[k] - v[k]) * t)))
                    for k in range(4)
                ]
                for v, w in zip(out["vertices"], end["vertices"])
            ]
        doc.update("shapes", shape)
        clip["children"].append({"id": shape["id"], "name": f"morph_{fi}", "blend": 0})
        clip["frames"].append({"label": "", "elements": [[fi, 65535, 65535]]})
    doc.update("movieclips", clip)
    return cid


def mask_range(doc, clip_id, mask_child, content_children):
    """Wrap a contiguous placement range in SC stencil state modifiers."""
    content_children = list(content_children)
    if not content_children or mask_child in content_children:
        raise ValueError("Choose separate mask and content tracks")
    clip = doc.resource(clip_id)
    if any(not 0 <= i < len(clip["children"]) for i in [mask_child, *content_children]):
        raise ValueError("Invalid mask/content child index")
    additions = []
    for typ in (MASK_BEGIN, MASK_CONTENT, MASK_END):
        mid = doc.next_id()
        doc.update("modifiers", {"id": mid, "type": typ, "exports": []})
        additions.append(len(clip["children"]))
        clip["children"].append({"id": mid, "name": f"mask_state_{typ}", "blend": 0})
    for frame in clip["frames"]:
        elements = frame["elements"]
        mask = [e for e in elements if e[0] == mask_child]
        content = [e for e in elements if e[0] in content_children]
        if not content:
            continue
        if len(mask) != 1:
            raise ValueError("Mask must be placed once in every masked frame")
        positions = [i for i, e in enumerate(elements) if e[0] in content_children]
        if positions != list(range(min(positions), max(positions) + 1)):
            raise ValueError("Masked content must occupy a contiguous draw-order range")
        first = min(positions)
        before = [e for e in elements[:first] if e[0] != mask_child]
        after = [e for e in elements[max(positions) + 1 :] if e[0] != mask_child]
        frame["elements"] = (
            before
            + [[additions[0], 65535, 65535]]
            + mask
            + [[additions[1], 65535, 65535]]
            + content
            + [[additions[2], 65535, 65535]]
            + after
        )
    doc.update("movieclips", clip)


def nine_slice(doc, shape_id, left, top, right, bottom):
    """Cut an axis-aligned bitmap quad into nine regions and wrap it in a grid clip."""
    shape = doc.resource(shape_id)
    if doc.index[shape_id][0] != "shapes" or len(shape["commands"]) != 1:
        raise ValueError("Nine-slice needs a shape with one bitmap quad")
    command = shape["commands"][0]
    vs = command["vertices"]
    if len(vs) != 4:
        raise ValueError("Nine-slice needs four quad vertices")
    xs = sorted(set(v[0] for v in vs))
    ys = sorted(set(v[1] for v in vs))
    if len(xs) != 2 or len(ys) != 2 or len({(v[0], v[1]) for v in vs}) != 4:
        raise ValueError("Nine-slice requires an axis-aligned rectangle")
    x0, x1 = xs
    y0, y1 = ys
    if not x0 <= left < right <= x1 or not y0 <= top < bottom <= y1:
        raise ValueError("Grid must lie inside the shape bounds")
    points = {(v[0], v[1]): v for v in vs}

    def vertex(x, y):
        tx = (x - x0) / (x1 - x0)
        ty = (y - y0) / (y1 - y0)
        uv = []
        for k in (2, 3):
            a = points[(x0, y0)][k] * (1 - tx) + points[(x1, y0)][k] * tx
            b = points[(x0, y1)][k] * (1 - tx) + points[(x1, y1)][k] * tx
            uv.append(round(a * (1 - ty) + b * ty))
        return [x, y, *uv]

    commands = []
    xx = [x0, left, right, x1]
    yy = [y0, top, bottom, y1]
    for j in range(3):
        for i in range(3):
            if xx[i] == xx[i + 1] or yy[j] == yy[j + 1]:
                continue
            commands.append(
                {
                    "unk1": command.get("unk1", 0),
                    "texture_index": command["texture_index"],
                    "vertices": [
                        vertex(xx[i], yy[j]),
                        vertex(xx[i + 1], yy[j]),
                        vertex(xx[i], yy[j + 1]),
                        vertex(xx[i + 1], yy[j + 1]),
                    ],
                }
            )
    sid = doc.next_id()
    doc.update("shapes", {"id": sid, "exports": [], "commands": commands})
    cid = doc.new_resource("movieclips")
    clip = doc.resource(cid)
    clip.update(
        children=[{"id": sid, "name": "sliced_artwork", "blend": 0}],
        frames=[{"label": "", "elements": [[0, 65535, 65535]]}],
        scaling_grid={"left": left, "top": top, "right": right, "bottom": bottom},
    )
    doc.update("movieclips", clip)
    return cid


def group_tracks(doc, clip_id, children, name="Group"):
    """Group contiguous tracks into a nested animated symbol, preserving poses."""
    clip = doc.resource(clip_id)
    selected = set(children)
    if not selected or any(not 0 <= i < len(clip["children"]) for i in selected):
        raise ValueError("Choose valid child indices to group")
    frames = resolved_frames(doc, clip)
    for f in frames:
        positions = [i for i, p in enumerate(f["placements"]) if p[0] in selected]
        if positions and positions != list(range(min(positions), max(positions) + 1)):
            raise ValueError("Grouped tracks must be contiguous in every frame")
    ordered = sorted(selected)
    grouped_map = {old: new for new, old in enumerate(ordered)}
    nested_id = doc.new_resource("movieclips")
    nested = doc.resource(nested_id)
    nested["framerate"] = clip["framerate"]
    nested["children"] = [copy.deepcopy(clip["children"][i]) for i in ordered]
    nested_frames = [
        {
            "label": f["label"],
            "placements": [
                (grouped_map[ci], m, co)
                for ci, m, co in f["placements"]
                if ci in selected
            ],
        }
        for f in frames
    ]
    write_frames(doc, nested, nested_frames)
    kept = [i for i in range(len(clip["children"])) if i not in selected]
    mapping = {old: new for new, old in enumerate(kept)}
    clip["children"] = [clip["children"][i] for i in kept]
    new_index = len(kept)
    clip["children"].append({"id": nested_id, "name": name, "blend": 0})
    for f in frames:
        result = []
        placed = False
        for ci, m, co in f["placements"]:
            if ci in selected:
                if not placed:
                    result.append((new_index, list(IDENTITY), list(WHITE)))
                    placed = True
            else:
                result.append((mapping[ci], m, co))
        f["placements"] = result
    write_frames(doc, clip, frames)
    return nested_id


def motion_path(
    doc, clip_id, child_index, start, end, points, orient=False, curve="Linear"
):
    """Bake a polyline motion guide with distance-based progress and orientation."""
    if len(points) < 2 or any(
        len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points
    ):
        raise ValueError("Motion guide needs at least two finite XY points")
    clip = doc.resource(clip_id)
    frames = resolved_frames(doc, clip)
    if not 0 <= start < end < len(frames):
        raise ValueError("Choose an increasing frame range")
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
    total = sum(lengths)
    if total <= 1e-12:
        raise ValueError("Motion guide has no length")
    for fi in range(start, end + 1):
        matches = [
            i for i, p in enumerate(frames[fi]["placements"]) if p[0] == child_index
        ]
        if len(matches) != 1:
            raise ValueError(
                "Motion path needs one placement of the child in every frame"
            )
        progress = easing.bezier((fi - start) / (end - start), *easing.curve_for(curve))
        distance = min(total, max(0, progress * total))
        accum = 0
        for segment, length in enumerate(lengths):
            if length > 0 and (
                distance <= accum + length or segment == len(lengths) - 1
            ):
                break
            accum += length
        a, b = points[segment], points[segment + 1]
        t = (distance - accum) / max(lengths[segment], 1e-12)
        index = matches[0]
        ci, m, co = frames[fi]["placements"][index]
        m = list(m)
        if orient:
            parts = easing.decompose(m)
            parts["rotation"] = math.atan2(b[1] - a[1], b[0] - a[0])
            m = easing.compose(parts)
        m[4] = a[0] + (b[0] - a[0]) * t
        m[5] = a[1] + (b[1] - a[1]) * t
        frames[fi]["placements"][index] = (ci, m, co)
    write_frames(doc, clip, frames)
