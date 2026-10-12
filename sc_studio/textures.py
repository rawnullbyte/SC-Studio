"""Texture previews for raw SC pixel formats and KTX1 ASTC/ETC/BC/PVRTC."""

import io
import math
import struct
from pathlib import Path
from PIL import Image
import texture2ddecoder as dec
import zstandard


# GL_COMPRESSED_RGBA_ASTC_<NxM>_KHR starts at 0x93B0, in this order. The SRGB
# variants start at 0x93D0 with the same layout.
ASTC_BLOCKS = [
    (4, 4),
    (5, 4),
    (5, 5),
    (6, 5),
    (6, 6),
    (8, 5),
    (8, 6),
    (8, 8),
    (10, 5),
    (10, 6),
    (10, 8),
    (10, 10),
    (12, 10),
    (12, 12),
]


def astc_block_size(data, raw):
    """(width, height) of one ASTC block, or None when the bytes are not ASTC.

    Accepts the unpacked KTX blob so callers can reuse a payload already read
    from disk or from an embedded external sidecar.
    """
    if not raw.startswith(b"\xabKTX 11"):
        return None
    internal = struct.unpack_from("<I", raw, 28)[0]
    if 0x93B0 <= internal <= 0x93BD:
        return ASTC_BLOCKS[internal - 0x93B0]
    if 0x93D0 <= internal <= 0x93DD:
        return ASTC_BLOCKS[internal - 0x93D0]
    return None


def texture_bytes(data, directory):
    if data.get("external_texture"):
        raw = (Path(directory) / data["external_texture"]).read_bytes()
    else:
        raw = bytes(data.get("data", b""))
    if raw.startswith(b"\x28\xb5\x2f\xfd"):
        raw = zstandard.ZstdDecompressor().decompress(raw)
    return raw


def image(data, directory=Path(".")):
    raw = texture_bytes(data, directory)
    w, h = data["width"], data["height"]
    if raw.startswith(b"\xabKTX 11\xbb\r\n\x1a\n"):
        values = struct.unpack_from("<13I", raw, 12)
        (
            endian,
            typ,
            typ_size,
            fmt,
            internal,
            base,
            w,
            h,
            depth,
            arrays,
            faces,
            levels,
            kvs,
        ) = values
        if endian != 0x04030201 or depth or arrays or faces != 1:
            raise ValueError("Only little-endian 2D KTX textures are previewed")
        pos = 64 + kvs
        n = struct.unpack_from("<I", raw, pos)[0]
        pixels = raw[pos + 4 : pos + 4 + n]
        if typ:
            if typ == 0x1401 and fmt in (0x1908, 0x1907):
                return Image.frombytes(
                    "RGBA" if fmt == 0x1908 else "RGB", (w, h), pixels
                ).convert("RGBA")
            raise ValueError(f"Unsupported uncompressed KTX GL format {fmt:#x}")
        block = astc_block_size(data, raw)
        if block is not None:
            out = dec.decode_astc(pixels, w, h, *block)
        elif internal in (0x8D64,):
            out = dec.decode_etc1(pixels, w, h)
        elif internal in (0x9274, 0x9275):
            out = dec.decode_etc2(pixels, w, h)
        elif internal in (0x9276, 0x9277):
            out = dec.decode_etc2a1(pixels, w, h)
        elif internal in (0x9278, 0x9279):
            out = dec.decode_etc2a8(pixels, w, h)
        elif internal in (0x83F0, 0x83F1):
            out = dec.decode_bc1(pixels, w, h)
        elif internal == 0x83F3:
            out = dec.decode_bc3(pixels, w, h)
        elif internal in (0x8C00, 0x8C01, 0x8C02, 0x8C03):
            out = dec.decode_pvrtc(pixels, w, h, internal in (0x8C01, 0x8C03))
        else:
            raise ValueError(
                f"KTX compressed format {internal:#x} cannot be previewed; its bytes are preserved"
            )
        return Image.frombytes("RGBA", (w, h), out, "raw", "BGRA")
    modes = {
        0: "RGBA",
        1: "RGBA",
        2: "RGBA;4B",
        3: "RGBA;15",
        4: "BGR;16",
        6: "LA",
        10: "L",
    }
    p = data["pixel_type"]
    if p not in modes:
        raise ValueError(f"Pixel type {p} cannot be previewed; bytes are preserved")
    mode = "LA" if p == 6 else "L" if p == 10 else "RGB" if p == 4 else "RGBA"
    return Image.frombytes(mode, (w, h), raw, "raw", modes[p]).convert("RGBA")


def replace_from_png(data, path):
    im = Image.open(path).convert("RGBA")
    if im.width > 65535 or im.height > 65535:
        raise ValueError("Texture dimensions exceed 16-bit range")
    return {
        "texture_format": 0,
        "pixel_type": 0,
        "width": im.width,
        "height": im.height,
        "data": im.tobytes(),
        "external_texture": None,
    }


def frame_atlas_bytes(image):
    """A single RGBA texture resource from a ready-made image."""
    if image.width > 65535 or image.height > 65535:
        raise ValueError("Texture dimensions exceed 16-bit range")
    return {
        "texture_format": 0,
        "pixel_type": 0,
        "width": image.width,
        "height": image.height,
        "data": image.convert("RGBA").tobytes(),
        "external_texture": None,
    }


def decode_animation(path, max_frames=600):
    """Decode an animated image into a list of RGBA frames.

    Pillow already reads GIF and animated WebP frame by frame, so those need no
    extra dependency. Each frame is composited onto a full-size canvas, since
    GIF frames are commonly stored as a small changed rectangle over the
    previous frame and only the composed result is the picture that is seen.
    Returns ``(frames, duration_ms)`` where the duration is the total play time
    of every decoded frame, which a caller uses to derive a framerate; it is 0
    for a still image.
    """
    frames = []
    delays = []
    with Image.open(path) as im:
        if not getattr(im, "is_animated", False):
            return [im.convert("RGBA")], 0
        for index in range(getattr(im, "n_frames", 1)):
            if index >= max_frames:
                break
            im.seek(index)
            delays.append(int(im.info.get("duration", 0) or 0))
            frames.append(im.convert("RGBA"))
    return frames, sum(delays)


def pack_frames(frames):
    """Lay frames out in a shelf-packed atlas, returning boxes and placed size.

    A single texture holding every frame keeps one resource to manage and is how
    real SC animations are usually packed. Shelves are used rather than a
    general bin packer because the result has to be simple to reason about:
    every frame in a row shares a top, which is what the shape UVs assume.
    """
    if not frames:
        return [], None, 0, 0
    width = max(frame.width for frame in frames)
    height = max(frame.height for frame in frames)
    # Aim at a roughly square sheet: a single row of a long animation is a very
    # wide, short texture, which is awkward to crop and to sample.
    target = math.isqrt(max(1, len(frames))) + 1
    columns = max(1, min(len(frames), max(target, math.ceil(width / max(height, 1)))))
    cell_w, cell_h = width, height
    placements = []
    for index in range(len(frames)):
        col, row = index % columns, index // columns
        placements.append((col * cell_w, row * cell_h))
    sheet_w = columns * cell_w
    sheet_h = ((len(frames) + columns - 1) // columns) * cell_h
    sheet = Image.new("RGBA", (sheet_w, sheet_h), (0, 0, 0, 0))
    for (x, y), frame in zip(placements, frames):
        sheet.paste(frame, (x, y))
    return placements, sheet, sheet_w, sheet_h
