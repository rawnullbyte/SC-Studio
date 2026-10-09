"""Texture previews for raw SC pixel formats and KTX1 ASTC/ETC/BC/PVRTC."""

import io
import struct
from pathlib import Path
from PIL import Image
import texture2ddecoder as dec
import zstandard


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
        astc = [
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
        if 0x93B0 <= internal <= 0x93BD or 0x93D0 <= internal <= 0x93DD:
            block = astc[internal - (0x93D0 if internal >= 0x93D0 else 0x93B0)]
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
