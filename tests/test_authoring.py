import copy
import pytest
from PIL import Image
from sc_studio.document import Document
from sc_studio.authoring import *
from test_document import populated


def test_parenting_nested_bake_and_cycle():
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535], [1, 0, 65535]]}]
    d.update("movieclips", clip)
    bake_parenting(d, 7, {1: 0})
    pose = resolved_frames(d, d.resource(7))[0]["placements"]
    assert pose[1][1] == [1, 0, 0, 1, 80, 40]
    with pytest.raises(ValueError, match="cycle"):
        bake_parenting(d, 7, {0: 1, 1: 0})


def test_resolve_color_and_matrix_independently_when_order_changes():
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [
        {"label": "", "elements": [[0, 0, 0], [1, 65535, 65535]]},
        {"label": "", "elements": [[1, 65535, 65535], [0, 65535, 65535]]},
    ]
    d.update("movieclips", clip)
    pose = resolved_frames(d, d.resource(7))[1]["placements"]
    assert pose[0][1] == IDENTITY and pose[1][1] == [1, 0, 0, 1, 40, 20]
    assert pose[1][2][3] == 200


def test_transaction_restores_failed_compound_operation():
    d = populated()
    before = copy.deepcopy(d.tables)
    with pytest.raises(ValueError):
        with transaction(d):
            mask_range(d, 7, 1, [0])
    assert d.tables == before


def test_color_tween_bakes_intermediate_alpha():
    d = populated()
    clip = d.resource(7)
    bank = d.add_bank([IDENTITY], [[255, 255, 255, 0, 0, 0, 0], WHITE])
    clip["matrix_bank_index"] = bank
    clip["frames"] = [
        {"label": "", "elements": [[0, 0, 0]]},
        {"label": "", "elements": [[0, 65535, 65535]]},
        {"label": "", "elements": [[0, 0, 1]]},
    ]
    d.update("movieclips", clip)
    tween_colors(d, 7, 0, 0, 2)
    frames = resolved_frames(d, d.resource(7))
    assert [f["placements"][0][2][3] for f in frames] == [0, 128, 255]


def test_shape_morph_bakes_geometry_and_saves(tmp_path):
    d = populated()
    end = d.resource(3)
    end["id"] = 5
    for command in end["commands"]:
        for v in command["vertices"]:
            v[0] += 10
    d.update("shapes", end)
    cid = morph_shapes(d, 3, 5, 3)
    clip = d.resource(cid)
    middle = d.resource(clip["children"][1]["id"])
    assert middle["commands"][0]["vertices"][0][0] == 5
    p = tmp_path / "morph.sc"
    d.save(p)
    loaded = Document.load(p)
    assert loaded.validate() == [] and len(loaded.resource(cid)["frames"]) == 3


def test_svg_gradients_strokes_rasterized():
    from test_gui import application

    application()
    d = Document()
    sid = svg_shape(
        d,
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><defs><linearGradient id="g"><stop stop-color="red"/><stop offset="1" stop-color="blue"/></linearGradient></defs><rect width="20" height="10" fill="url(#g)"/></svg>',
    )
    from sc_studio.textures import image

    im = image(d.tables["Textures"]["textures"][0]["highres"])
    assert im.getpixel((1, 5))[0] > im.getpixel((18, 5))[0]
    assert d.index[sid][0] == "shapes"


def test_nine_slice_cuts_quad_and_retains_uv(tmp_path):
    d = Document()
    sid = image_shape(d, Image.new("RGBA", (30, 30), "red"))
    cid = nine_slice(d, sid, 10, 10, 20, 20)
    clip = d.resource(cid)
    shape = d.resource(clip["children"][0]["id"])
    assert len(shape["commands"]) == 9
    assert clip["scaling_grid"] == {"left": 10, "top": 10, "right": 20, "bottom": 20}
    p = tmp_path / "grid.sc"
    d.save(p)
    assert Document.load(p).resource(cid) == clip


def test_grouping_preserves_resolved_poses_and_labels():
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [
        {"label": "begin", "elements": [[0, 0, 0], [1, 0, 0]]},
        {"label": "end", "elements": [[1, 65535, 65535], [0, 65535, 65535]]},
    ]
    d.update("movieclips", clip)
    original = resolved_frames(d, d.resource(7))
    cid = group_tracks(d, 7, [0, 1], "Pair")
    grouped = resolved_frames(d, d.resource(cid))
    assert grouped == original
    parent = d.resource(7)
    assert parent["children"] == [{"id": cid, "name": "Pair", "blend": 0}]
    assert len(parent["frames"]) == 2


def test_motion_guide_bakes_distance_and_orientation():
    d = populated()
    clip = d.resource(7)
    clip["frames"] = [{"label": "", "elements": [[0, 0, 65535]]} for _ in range(3)]
    d.update("movieclips", clip)
    motion_path(d, 7, 0, 0, 2, [[0, 0], [10, 0], [10, 10]], orient=True)
    poses = resolved_frames(d, d.resource(7))
    assert [f["placements"][0][1][4:] for f in poses] == [[0, 0], [10, 0], [10, 10]]
    assert poses[-1]["placements"][0][1][:4] == pytest.approx([0, 1, -1, 0])
