"""Texture cropping for dependency-aware import: byte fidelity and maths."""

import math
import struct

import pytest

from sc_studio import atlas_crop
from sc_studio.document import Document
from sc_studio.textures import astc_block_size, image

# GL_COMPRESSED_RGBA_ASTC_<NxM>_KHR base, matching sc_studio.textures.
ASTC_BASE = 0x93B0
ASTC_BLOCKS = [(4, 4), (5, 4), (5, 5), (6, 5), (6, 6), (8, 5), (8, 6), (8, 8),
               (10, 5), (10, 6), (10, 8), (10, 10), (12, 10), (12, 12)]


def astc_ktx(width, height, block, seed=0):
    """A synthetic ASTC KTX1 blob with distinguishable, position-stable blocks."""
    bx, by = block
    cols = math.ceil(width / bx)
    rows = math.ceil(height / by)
    body = b"".join(bytes([(seed + i) % 256]) * 16 for i in range(cols * rows))
    header = bytearray(64)
    header[:12] = b"\xabKTX 11\xbb\r\n\x1a\n"
    struct.pack_into(
        "<13I", header, 12,
        0x04030201, 0, 1, 0, ASTC_BASE + ASTC_BLOCKS.index(block),
        6408, width, height, 0, 0, 0, 1, 1,
    )
    return bytes(header) + struct.pack("<I", len(body)) + body


def ktx_blocks(blob):
    size = struct.unpack_from("<I", blob, 64)[0]
    return blob[68 : 68 + size]


def astc_texture(width, height, block, seed=0):
    return {"texture_format": 8, "pixel_type": 0, "width": width, "height": height,
            "data": astc_ktx(width, height, block, seed), "external_texture": None}


def raw_texture(width, height):
    """A raw RGBA atlas where every texel encodes its own position."""
    pixels = b"".join(
        bytes([row % 256, col % 256, 7, 255])
        for row in range(height)
        for col in range(width)
    )
    return {"texture_format": 0, "pixel_type": 0, "width": width, "height": height,
            "data": pixels, "external_texture": None}


def test_astc_block_size_reads_the_internal_format():
    for block in ((4, 4), (6, 6), (12, 12)):
        assert astc_block_size({}, astc_ktx(48, 48, block)) == block
    assert astc_block_size({}, b"not a ktx blob") is None


def test_crop_is_a_pure_block_copy():
    """The crop must be byte-identical to the source blocks it selects."""
    width, height, block = 120, 96, (6, 6)
    data = astc_texture(width, height, block, seed=3)
    # a 6-aligned rect, so the box is exactly blocks [2,4) x [1,3)
    crop = atlas_crop.crop_texture(data, [[12, 6, 12, 12]])
    assert (crop["width"], crop["height"]) == (12, 12)
    assert crop["offset"] == (12, 6)
    assert crop["block"] == block

    cols = math.ceil(width / 6)
    source = ktx_blocks(data["data"])
    expected = b"".join(
        source[(row * cols + col) * 16 : (row * cols + col + 1) * 16]
        for row in (1, 2)
        for col in (2, 3)
    )
    assert ktx_blocks(crop["dict"]["data"]) == expected
    # the header must describe the new size, not the old one
    assert struct.unpack_from("<II", crop["dict"]["data"], 36) == (12, 12)


def test_crop_expands_a_rect_to_whole_blocks():
    data = astc_texture(120, 120, (6, 6))
    # a 10x10 rect at (7,7) must pull in every block it touches
    crop = atlas_crop.crop_texture(data, [[7, 7, 10, 10]])
    assert crop["offset"] == (6, 6)
    assert (crop["width"], crop["height"]) == (12, 12)


def test_full_extent_and_external_are_left_alone():
    data = astc_texture(60, 60, (6, 6))
    assert atlas_crop.crop_texture(data, [[0, 0, 60, 60]]) is None
    assert atlas_crop.crop_texture(data, []) is None
    with pytest.raises(atlas_crop.CropError):
        atlas_crop.crop_texture({**data, "external_texture": "a.ktx"}, [[0, 0, 6, 6]])


def test_unsupported_textures_raise_rather_than_guess():
    short = {"texture_format": 0, "pixel_type": 0, "width": 64, "height": 64,
             "data": b"\x00" * 16, "external_texture": None}
    with pytest.raises(atlas_crop.CropError):
        atlas_crop.crop_texture(short, [[0, 0, 8, 8]])
    odd_type = {**short, "pixel_type": 99, "data": bytes(64 * 64 * 4)}
    with pytest.raises(atlas_crop.CropError):
        atlas_crop.crop_texture(odd_type, [[0, 0, 8, 8]])


def test_raw_textures_crop_by_rows():
    data = raw_texture(16, 16)
    crop = atlas_crop.crop_texture(data, [[4, 4, 8, 8]])
    assert (crop["width"], crop["height"]) == (8, 8)
    assert crop["offset"] == (4, 4)
    assert crop["block"] == (1, 1)
    assert len(crop["dict"]["data"]) == 8 * 8 * 4
    assert crop["dict"]["data"][: 8 * 4] == data["data"][(4 * 16 + 4) * 4 : (4 * 16 + 12) * 4]


def test_remap_uv_keeps_the_sampled_texel():
    # texels (2402, 2404), a 36x36 crop starting at (2400, 2400)
    verts = [[0, 0, round(2402 / 4096 * 65535), round(2404 / 4096 * 65535)]]
    out = atlas_crop.remap_uv(verts, (2400, 2400), (6, 6), (4096, 4096), (36, 36))
    x, y, u, v = out[0]
    assert round(u / 65535 * 36) == 2
    assert round(v / 65535 * 36) == 4
    assert (x, y) == (0, 0)


def test_remap_uv_clamps_outside_the_crop():
    verts = [[0, 0, 0, 0], [1, 1, 65535, 65535]]
    for _, _, u, v in atlas_crop.remap_uv(
        verts, (100, 100), (6, 6), (200, 200), (50, 50)
    ):
        assert 0 <= u <= 65535 and 0 <= v <= 65535


def import_single_shape(source, version=6):
    destination = Document(version=version)
    destination.import_resources(source, [3])
    return destination


def test_import_crops_the_atlas_and_keeps_pixels():
    """The end-to-end guarantee: an imported shape samples exactly what it did."""
    source = Document(version=6)
    source.tables["Textures"]["textures"] = [{"highres": raw_texture(120, 120),
                                              "lowres": None}]
    source.update("shapes", {
        "id": 3, "exports": ["sprite"],
        "commands": [{"unk1": 0, "texture_index": 0,
                      "vertices": [[0, 0, round(40 / 120 * 65535), round(50 / 120 * 65535)],
                                   [20, 0, round(60 / 120 * 65535), round(70 / 120 * 65535)],
                                   [0, 20, round(40 / 120 * 65535), round(70 / 120 * 65535)]]}],
    })
    destination = import_single_shape(source)

    assert len(destination.tables["Textures"]["textures"]) == 1
    cropped = destination.tables["Textures"]["textures"][0]["highres"]
    assert cropped["width"] < 120 and cropped["height"] < 120

    source_image = image(source.tables["Textures"]["textures"][0]["highres"])
    cropped_image = image(cropped)
    new_id = next(i for i, (kind, _) in destination.index.items() if kind == "shapes")
    for old, new in zip(source.resource(3)["commands"][0]["vertices"],
                        destination.resource(new_id)["commands"][0]["vertices"]):
        assert source_image.getpixel(
            (int(old[2] / 65535 * 120), int(old[3] / 65535 * 120))
        ) == cropped_image.getpixel(
            (int(new[2] / 65535 * cropped["width"]),
             int(new[3] / 65535 * cropped["height"]))
        )


def test_import_keeps_astc_blocks_byte_identical():
    """A cropped ASTC atlas must be bit-identical to the source blocks.

    This is what makes cropping lossless: no ASTC encoder is involved, so the
    game sees exactly the bytes the original sheet held.
    """
    source = Document(version=6)
    atlas = astc_texture(600, 600, (6, 6), seed=11)
    source.tables["Textures"]["textures"] = [{"highres": atlas, "lowres": None}]
    source.update("shapes", {
        "id": 3, "exports": ["sprite"],
        "commands": [{"unk1": 0, "texture_index": 0,
                      "vertices": [[0, 0, 30000, 30000], [30, 0, 31000, 30000],
                                   [0, 30, 30000, 31000]]}],
    })
    destination = import_single_shape(source)
    cropped = destination.tables["Textures"]["textures"][0]["highres"]
    assert (cropped["width"], cropped["height"]) != (600, 600)

    # every block of the crop must appear verbatim in the source atlas
    source_blocks = ktx_blocks(atlas["data"])
    cols = math.ceil(600 / 6)
    available = {
        source_blocks[(row * cols + col) * 16 : (row * cols + col + 1) * 16]
        for row in range(cols)
        for col in range(cols)
    }
    cropped_blocks = ktx_blocks(cropped["data"])
    assert len(cropped_blocks) % 16 == 0
    for i in range(0, len(cropped_blocks), 16):
        assert cropped_blocks[i : i + 16] in available


def test_lowres_is_cropped_at_its_own_scale():
    """atlas_regions reports highres pixels; lowres is smaller and must scale.

    Cropping lowres with highres rects would keep a region twice as large as it
    should, throwing away most of the saving exactly where it matters.
    """
    source = Document(version=6)
    source.tables["Textures"]["textures"] = [{
        "highres": raw_texture(200, 200),
        "lowres": raw_texture(100, 100),
    }]
    source.update("shapes", {
        "id": 3, "exports": ["sprite"],
        "commands": [{"unk1": 0, "texture_index": 0,
                      "vertices": [[0, 0, round(60 / 200 * 65535), round(60 / 200 * 65535)],
                                   [4, 0, round(100 / 200 * 65535), round(60 / 200 * 65535)],
                                   [0, 4, round(60 / 200 * 65535), round(100 / 200 * 65535)]]}],
    })
    destination = import_single_shape(source)
    texture = destination.tables["Textures"]["textures"][0]
    high, low = texture["highres"], texture["lowres"]
    assert high["width"] < 200 and low["width"] < 100
    # lowres covers the same UV span, so it stays about half the highres size
    assert abs(low["width"] / high["width"] - 0.5) < 0.35


def test_cropped_import_survives_save_and_reload(tmp_path):
    source = Document(version=6)
    source.tables["Textures"]["textures"] = [{"highres": raw_texture(128, 128),
                                              "lowres": None}]
    source.update("shapes", {
        "id": 3, "exports": ["tile"],
        "commands": [{"unk1": 0, "texture_index": 0,
                      "vertices": [[0, 0, 8192, 8192], [4, 0, 16384, 8192],
                                   [0, 4, 8192, 16384]]}],
    })
    destination = import_single_shape(source)
    target = tmp_path / "cropped.sc"
    destination.save(target)

    reloaded = Document.load(target)
    assert reloaded.validate() == []
    assert len(reloaded.tables["Textures"]["textures"]) == 1
    texture = reloaded.tables["Textures"]["textures"][0]["highres"]
    assert texture["width"] < 128 and texture["height"] < 128
    assert image(texture).size == (texture["width"], texture["height"])
