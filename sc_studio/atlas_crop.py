"""Lossless texture cropping for dependency-aware atlas import.

Importing a movieclip copies whole atlases, so a clip that draws a small sprite
from a 4096x4096 sheet drags the entire sheet along. This crops each copied
atlas down to the block-aligned region the imported shapes actually sample and
rewrites those shapes' UVs to match.

ASTC stores each 6x6 (or 4x4, 8x8, ...) block independently, with no
cross-block prediction, so a block-aligned crop is a byte copy: the result
decodes to exactly the same pixels as the source region. Raw pixel formats crop
the same way by trimming rows. Neither needs an ASTC encoder, and both are
lossless.

Blocks are kept whole, so the cropped sheet can be a little larger than the
image-space bounding box. Regions sharing one block therefore stay intact, which
matters because a great many commands sample a single texel.
"""

import math
import struct

from . import textures


class CropError(ValueError):
    """Raised when a texture cannot be cropped, with the reason to report."""


def _ktx_payload(raw):
    """(header, imageSize word, block bytes) of an unpacked KTX1 blob."""
    if not raw.startswith(b"\xabKTX 11"):
        return None
    if len(raw) < 68:
        raise CropError("Truncated KTX header")
    # KTX1: pixelWidth at 36, pixelHeight at 40, imageSize at 64.
    width, height = struct.unpack_from("<II", raw, 36)
    stored = struct.unpack_from("<I", raw, 64)[0]
    return raw[:64], width, height, raw[68 : 68 + stored]


def _aligned_bounds(rects, block, width, height):
    """Union of pixel rects expanded to whole blocks, as a pixel box."""
    if not rects:
        return None
    left = min(rect[0] for rect in rects)
    top = min(rect[1] for rect in rects)
    right = max(rect[0] + rect[2] for rect in rects)
    bottom = max(rect[1] + rect[3] for rect in rects)
    bx, by = block
    left = max(0, min(width - 1, math.floor(left / bx) * bx))
    top = max(0, min(height - 1, math.floor(top / by) * by))
    right = min(width, math.ceil(right / bx) * bx)
    bottom = min(height, math.ceil(bottom / by) * by)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def crop_texture(data, rects, directory=".", block=None):
    """Crop one texture to the block-aligned union of ``rects``.

    ``data`` is a TextureData dict as stored in the Textures table; ``rects``
    are atlas-space pixel rectangles for the shapes being imported. Returns a
    new dict, or ``None`` when nothing needs cropping. Raises CropError when the
    texture cannot be cropped safely.

    Refusing is always preferable to guessing: a caller that catches CropError
    can keep the original full texture, which is what previous releases shipped.
    """
    if not rects:
        return None
    width, height = data["width"], data["height"]
    if width <= 0 or height <= 0:
        raise CropError("Texture has no dimensions")
    if data.get("external_texture"):
        raise CropError("External texture is copied separately, not cropped")
    raw = bytes(data.get("data") or b"")
    if not raw:
        raise CropError("Texture has no data")

    payload = _ktx_payload(raw)
    if payload is not None:
        header, ktx_width, ktx_height, body = payload
        if (ktx_width, ktx_height) != (width, height):
            raise CropError("KTX dimensions disagree with the texture record")
        if block is None:
            block = textures.astc_block_size(data, raw)
        if block is None:
            raise CropError("Unsupported KTX encoding")
        return _crop_ktx(header, width, height, body, block, rects)

    if data.get("pixel_type") not in (0, 1):
        raise CropError("Pixel type cannot be cropped")
    return _crop_raw(data, width, height, raw, rects)


def _crop_ktx(header, width, height, body, block, rects):
    bx, by = block
    box = _aligned_bounds(rects, block, width, height)
    if box is None:
        return None
    left, top, right, bottom = box
    if (left, top, right, bottom) == (0, 0, width, height):
        return None
    cols = math.ceil(width / bx)
    rows = math.ceil(height / by)
    if len(body) != cols * rows * 16:
        raise CropError("KTX payload does not match its dimensions")
    start_col, end_col = left // bx, math.ceil(right / bx)
    start_row, end_row = top // by, math.ceil(bottom / by)
    pieces = [
        body[(row * cols + start_col) * 16 : (row * cols + end_col) * 16]
        for row in range(start_row, end_row)
    ]
    cropped = b"".join(pieces)
    new_width = (end_col - start_col) * bx
    new_height = (end_row - start_row) * by

    head = bytearray(header)
    struct.pack_into("<II", head, 36, new_width, new_height)
    return {
        "width": new_width,
        "height": new_height,
        "block": block,
        "offset": (left, top),
        "dict": {
            "texture_format": 8,
            "pixel_type": 0,
            "width": new_width,
            "height": new_height,
            "data": bytes(head) + struct.pack("<I", len(cropped)) + cropped,
            "external_texture": None,
        },
    }


def _crop_raw(data, width, height, raw, rects):
    pixel_type = data.get("pixel_type")
    stride = _raw_stride(pixel_type)
    if stride is None:
        raise CropError("Pixel type cannot be cropped")
    box = _aligned_bounds(rects, (1, 1), width, height)
    if box is None:
        return None
    left, top, right, bottom = box
    if (left, top, right, bottom) == (0, 0, width, height):
        return None
    if len(raw) < width * height * stride:
        raise CropError("Texture data is shorter than its dimensions")
    new_width, new_height = right - left, bottom - top
    rows = b"".join(
        raw[(row * width + left) * stride : (row * width + right) * stride]
        for row in range(top, bottom)
    )
    return {
        "width": new_width,
        "height": new_height,
        "block": (1, 1),
        "offset": (left, top),
        "dict": {
            **data,
            "width": new_width,
            "height": new_height,
            "data": rows,
        },
    }


def _raw_stride(pixel_type):
    """Bytes per texel for the raw formats the previewer can also read."""
    return {0: 4, 1: 4, 2: 4, 3: 2, 4: 2, 6: 2, 10: 1}.get(pixel_type)


def remap_uv(vertices, offset, block, source_size, cropped_size):
    """Shift u16 UVs into a cropped atlas, clamped to the retained region.

    Coordinates land on texel centres so that a sample taken exactly at a texel
    boundary cannot fall outside the new sheet after rounding.
    """
    left, top = offset
    bx, by = block
    src_w, src_h = source_size
    new_w, new_h = cropped_size
    out = []
    for x, y, u, v in vertices:
        px = min(max(u / 65535 * src_w, 0.0), src_w - 0.5)
        py = min(max(v / 65535 * src_h, 0.0), src_h - 0.5)
        rel_x = min(max(px - left, 0.0), new_w - 0.5)
        rel_y = min(max(py - top, 0.0), new_h - 0.5)
        out.append(
            [
                x,
                y,
                min(65535, max(0, round(rel_x / new_w * 65535))),
                min(65535, max(0, round(rel_y / new_h * 65535))),
            ]
        )
    return out