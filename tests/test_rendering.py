import pytest
from PIL import Image
from PyQt6.QtCore import QRectF
from sc_studio.document import Document
from sc_studio.authoring import image_shape, mask_range, WHITE, IDENTITY
from sc_studio.rendering import (
    Renderer,
    pil_image,
    export_animation,
    composite,
    qt_image,
)
from test_gui import application


def scene():
    application()
    d = Document()
    blue = image_shape(d, Image.new("RGBA", (10, 10), "blue"))
    red = image_shape(d, Image.new("RGBA", (5, 5), "red"))
    cid = d.new_resource("movieclips")
    clip = d.resource(cid)
    bank = d.add_bank([IDENTITY], [WHITE])
    clip.update(
        children=[
            {"id": blue, "name": "background", "blend": 0},
            {"id": red, "name": "overlay", "blend": 0},
        ],
        frames=[{"label": "", "elements": [[0, 0, 0], [1, 0, 0]]}],
        matrix_bank_index=bank,
    )
    d.update("movieclips", clip)
    return d, cid


def test_render_draw_order_and_rgb_transform():
    d, cid = scene()
    renderer = Renderer(d)
    im = pil_image(renderer.render(cid)[0])
    assert im.getpixel((2, 2)) == (255, 0, 0, 255)
    assert im.getpixel((8, 8)) == (0, 0, 255, 255)
    clip = d.resource(cid)
    bank = d.add_bank([IDENTITY], [[0, 255, 255, 255, 0, 255, 0]])
    clip["matrix_bank_index"] = bank
    clip["frames"][0]["elements"] = [[1, 0, 0]]
    d.update("movieclips", clip)
    im = pil_image(Renderer(d).render(cid)[0])
    assert im.getpixel((2, 2)) == (0, 255, 0, 255)


def test_stencil_mask_pixel_clipping():
    d, cid = scene()
    mask_range(d, cid, 1, [0])
    im = pil_image(Renderer(d).render(cid)[0])
    assert im.getpixel((2, 2)) == (0, 0, 255, 255)
    assert im.getpixel((8, 8))[3] == 0


def test_multiply_and_erase_pixels():
    application()
    target = qt_image(Image.new("RGBA", (2, 2), "blue"))
    layer = qt_image(Image.new("RGBA", (2, 2), "red"))
    composite(target, layer, 2)
    assert pil_image(target).getpixel((0, 0)) == (0, 0, 0, 255)
    composite(target, layer, 11)
    assert pil_image(target).getpixel((0, 0))[3] == 0


def test_constant_texel_quad():
    d, cid = scene()
    sid = d.resource(cid)["children"][0]["id"]
    shape = d.resource(sid)
    for v in shape["commands"][0]["vertices"]:
        v[2] = v[3] = 0
    d.update("shapes", shape)
    im = pil_image(Renderer(d).render(sid)[0])
    assert im.getpixel((8, 8)) == (0, 0, 255, 255)
    assert im.getpixel((1, 8)) == (0, 0, 255, 255)


def test_export_animation_stable_canvas(tmp_path):
    d, cid = scene()
    clip = d.resource(cid)
    bi = d.add_bank([IDENTITY, [1, 0, 0, 1, 20, 0]], [WHITE])
    clip["matrix_bank_index"] = bi
    clip["frames"].append({"label": "", "elements": [[0, 1, 0], [1, 1, 0]]})
    d.update("movieclips", clip)
    path = tmp_path / "clip.webp"
    report = export_animation(d, cid, path)
    assert report["width"] == 30 and report["frames"] == 2
    with Image.open(path) as im:
        assert im.n_frames == 2


def test_cycle_error_and_raster_limit():
    d, cid = scene()
    clip = d.resource(cid)
    clip["children"][0]["id"] = cid
    d.update("movieclips", clip)
    with pytest.raises(ValueError, match="Recursive"):
        Renderer(d).render(cid, bounds=QRectF(0, 0, 10, 10))
    with pytest.raises(ValueError, match="exceeds"):
        Renderer(d).render(cid, bounds=QRectF(0, 0, 9000, 1))


def test_skew_survives_tween():
    from sc_studio.easing import interpolate_matrix

    matrix = [1, 0, 0.5, 1, 10, 20]
    assert interpolate_matrix(matrix, matrix, 0.5) == pytest.approx(matrix)
