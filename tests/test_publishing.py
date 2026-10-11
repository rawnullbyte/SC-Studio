import copy
from pathlib import Path
import pytest
from PIL import Image
from sc_studio.document import Document
from sc_studio.authoring import image_shape, resolved_frames, transaction
from sc_studio.publishing import PublishSettings, prepare_publish, publish, _raw
from sc_studio.textures import image
from test_document import populated


@pytest.mark.parametrize("encoding", ["RGBA8", "RGBA4", "RGB565", "KTX"])
def test_texture_encoding_pixels(encoding):
    im = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    decoded = image(_raw(im, encoding))
    assert decoded.getpixel((0, 0)) == (255, 0, 0, 255)


def test_rgb565_refuses_alpha():
    with pytest.raises(ValueError, match="transparency"):
        _raw(Image.new("RGBA", (1, 1)), "RGB565")


@pytest.mark.parametrize("version", [5, 6])
def test_publish_roundtrip_is_non_destructive(version, tmp_path):
    d = populated()
    before = copy.deepcopy(d.tables)
    out = tmp_path / "published.sc"
    settings = PublishSettings(
        version=version, lowres=True, encoding="KTX", external=True
    )
    report = publish([d], out, settings)
    assert d.tables == before and d.path is None
    reopened = Document.load(out)
    assert reopened.version == version and reopened.validate() == []
    assert report["exports"] == 1 and "demo" in sum(reopened.names.values(), [])
    atlas = reopened.tables["Textures"]["textures"][0]
    assert atlas["highres"]["external_texture"].endswith(".zktx")
    assert image(atlas["highres"], tmp_path).getpixel((2, 2)) == (255, 0, 0, 255)
    assert atlas["lowres"] is not None


def test_atlas_dedup_and_coordinates(tmp_path):
    d = Document()
    a = image_shape(d, Image.new("RGBA", (3, 4), "red"))
    b = image_shape(d, Image.new("RGBA", (3, 4), "red"))
    result, report = prepare_publish([d], PublishSettings(linked_only=False, padding=2))
    assert (
        report["unique_regions"] == 1
        and len(result.tables["Textures"]["textures"]) == 1
    )
    for rid, (kind, _) in result.index.items():
        if kind != "shapes":
            continue
        command = result.resource(rid)["commands"][0]
        assert [v[:2] for v in command["vertices"]] == [[0, 0], [3, 0], [0, 4], [3, 4]]
        for v in command["vertices"]:
            assert 0 < v[2] < 65535 and 0 < v[3] < 65535


def test_atlas_multi_page_and_size_limit():
    d = Document()
    for color in ["red", "blue", "green"]:
        image_shape(d, Image.new("RGBA", (12, 12), color))
    out, r = prepare_publish(
        [d], PublishSettings(linked_only=False, max_width=16, max_height=16, padding=2)
    )
    assert len(out.tables["Textures"]["textures"]) == 3
    with pytest.raises(ValueError, match="exceeds atlas"):
        prepare_publish(
            [d],
            PublishSettings(linked_only=False, max_width=16, max_height=16, padding=3),
        )


def test_empty_linkages_give_actionable_error():
    with pytest.raises(ValueError, match="No linked"):
        prepare_publish([Document()])


def test_write_text_and_matrix_quantization():
    d = populated()
    out, _ = prepare_publish([d], PublishSettings(write_text=False, low_precision=True))
    assert all(
        out.resource(i)["text"] == ""
        for i, (k, _) in out.index.items()
        if k == "textfields"
    )
    assert out.header["scale_precision"] == 3
    clip = out.resource(next(i for i, (k, _) in out.index.items() if k == "movieclips"))
    assert resolved_frames(out, clip)[0]["placements"][0][1] == [1, 0, 0, 1, 40, 20]


def test_short_frames_roundtrip(tmp_path):
    d = populated()
    clip = d.resource(7)
    for f in clip["frames"]:
        f["label"] = ""
    d.update("movieclips", clip)
    path = tmp_path / "short.sc"
    publish([d], path, PublishSettings(version=5, short_frames=True))
    d = Document.load(path)
    clip = d.resource(next(i for i, (k, _) in d.index.items() if k == "movieclips"))
    assert len(clip["frames"]) == 2 and all(f["label"] == "" for f in clip["frames"])
    assert any(
        raw.get("short_frames")
        for kind, raw in d.index.values()
        if kind == "movieclips"
    )


def test_merge_replaces_export_and_parent_references():
    dest = populated()
    parent = dest.resource(7)
    parent["id"] = 8
    parent["children"] = [{"id": 7, "name": "nested", "blend": 0}]
    parent["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]}]
    parent["exports"] = ["parent"]
    dest.update("movieclips", parent)
    src = populated()
    clip = src.resource(7)
    clip["framerate"] = 18
    src.update("movieclips", clip)
    out, r = prepare_publish([src], destination=dest)
    assert r["replaced_exports"] == ["demo"]
    demo = next(i for i, n in out.names.items() if "demo" in n)
    parent = out.resource(next(i for i, n in out.names.items() if "parent" in n))
    assert parent["children"][0]["id"] == demo and out.resource(demo)["framerate"] == 18
    assert all(not n.endswith("_copy2") for names in out.names.values() for n in names)


def test_multiple_exports_are_preserved_when_merged():
    source = populated()
    source.set_exports(7, ["demo", "alias"])
    source._reindex()
    dest = populated()
    out, _ = prepare_publish([source], destination=dest)
    assert set(sum(out.names.values(), [])) == {"demo", "alias"}


def test_presets_roundtrip(tmp_path):
    settings = PublishSettings(encoding="KTX", external=True)
    path = tmp_path / "preset.json"
    settings.save(path)
    assert PublishSettings.load(path) == settings
