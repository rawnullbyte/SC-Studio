import os
from pathlib import Path
import copy
import pytest
from sc_studio.document import Document, CHUNKS
from sc_studio.flatcodec import decode, encode, SCHEMAS, layout
from sc_studio.textures import image


def populated(version=5):
    d = Document(version)
    d.tables["Textures"]["textures"] = [
        {
            "highres": {
                "texture_format": 0,
                "pixel_type": 0,
                "width": 2,
                "height": 2,
                "data": bytes([255, 0, 0, 255]) * 4,
                "external_texture": None,
            },
            "lowres": None,
        }
    ]
    d.update(
        "shapes",
        {
            "id": 3,
            "commands": [
                {
                    "unk1": 0,
                    "texture_index": 0,
                    "vertices": [[0, 0, 0, 0], [20, 0, 65535, 0], [0, 20, 0, 65535]],
                }
            ],
            "exports": [],
        },
    )
    d.update(
        "textfields",
        {
            "id": 4,
            "unused1": 7,
            "font_name": "Arial",
            "left": 0,
            "top": 0,
            "right": 100,
            "bottom": 30,
            "font_color": 0xFFFFFFFF,
            "outline_color": 0xFF000000,
            "text": "Hello world",
            "typography": "",
            "styles": 5,
            "align": 0,
            "font_size": 20,
            "unused2": 2,
            "outline_angle": -12,
            "exports": [],
        },
    )
    bi = d.add_bank([[1, 0, 0, 1, 40, 20]], [[255, 255, 255, 200, 0, 0, 0]])
    d.update(
        "movieclips",
        {
            "id": 7,
            "framerate": 30,
            "unknown_bool": 1,
            "matrix_bank_index": bi,
            "children": [
                {"id": 3, "name": "sprite", "blend": 0},
                {"id": 4, "name": "label", "blend": 1},
            ],
            "frames": [
                {"label": "start", "elements": [[0, 0, 0], [1, 65535, 65535]]},
                {"label": "end", "elements": [[0, 0, 65535]]},
            ],
            "scaling_grid": {"left": 0, "top": 0, "right": 100, "bottom": 100},
            "exports": ["demo"],
        },
    )
    return d


@pytest.mark.parametrize("version", [5, 6])
def test_duplicate_clip_shares_children_and_has_independent_timeline(version, tmp_path):
    d = populated(version)
    original = d.resource(7)
    counts = (len(d.index), len(d.tables["Textures"]["textures"]), len(d.storage["matrix_banks"]))
    copied = d.duplicate_resources([7])[7]
    clone = d.resource(copied)
    assert [child["id"] for child in clone["children"]] == [3, 4]
    assert clone["matrix_bank_index"] == original["matrix_bank_index"]
    assert clone["frames"] == original["frames"]
    assert clone["exports"] == ["demo_copy2"]
    assert (len(d.index), len(d.tables["Textures"]["textures"]), len(d.storage["matrix_banks"])) == (counts[0] + 1, counts[1], counts[2])
    clone["children"][0]["name"] = "independent instance"
    clone["frames"][0]["elements"].pop()
    clone["frames"][0]["label"] = "edited copy"
    d.update("movieclips", clone)
    assert d.resource(7) == original
    path = tmp_path / f"duplicate-v{version}.sc"
    d.save(path)
    reopened = Document.load(path)
    assert reopened.resource(7) == original
    assert reopened.resource(copied) == clone
    assert reopened.validate() == []


def test_v5_create_edit_roundtrip(tmp_path):
    d = populated()
    p = tmp_path / "new.sc"
    d.save(p)
    other = Document.load(p)
    for id in d.index:
        assert other.resource(id) == d.resource(id)
    assert other.bank(0)["matrices"] == d.bank(0)["matrices"]
    assert image(other.tables["Textures"]["textures"][0]["highres"]).getpixel(
        (0, 0)
    ) == (255, 0, 0, 255)
    obj = other.resource(4)
    obj["text"] = "Edited ✓"
    other.update("textfields", obj)
    other.save(p)
    assert Document.load(p).resource(4)["text"] == "Edited ✓"


def test_nested_import_collision_shared_dependency(tmp_path):
    source = populated()
    source.update(
        "movieclips",
        {
            "id": 8,
            "framerate": 30,
            "unknown_bool": 0,
            "matrix_bank_index": 0,
            "children": [
                {"id": 7, "name": "nested", "blend": 0},
                {"id": 3, "name": "shared", "blend": 0},
            ],
            "frames": [
                {"label": "", "elements": [[0, 65535, 65535], [1, 65535, 65535]]}
            ],
            "scaling_grid": None,
            "exports": ["root"],
        },
    )
    dest = populated()
    mapping = dest.import_resources(source, [8])
    assert len(mapping) == 4
    clip = dest.resource(mapping[8])
    assert clip["children"][0]["id"] == mapping[7]
    assert clip["children"][1]["id"] == mapping[3]
    inner = dest.resource(mapping[7])
    assert inner["children"][0]["id"] == mapping[3]
    assert inner["exports"] == ["demo_copy2"]
    assert dest.bank(inner["matrix_bank_index"])["matrices"][0] == [1, 0, 0, 1, 40, 20]
    dest.save(tmp_path / "merged.sc")
    again = Document.load(tmp_path / "merged.sc")
    assert again.validate() == []
    assert again.resource(mapping[8]) == clip


def test_import_cycles():
    s = Document()
    s.update(
        "movieclips",
        {
            "id": 1,
            "framerate": 30,
            "unknown_bool": 0,
            "matrix_bank_index": 0,
            "children": [{"id": 1, "name": "self", "blend": 0}],
            "frames": [],
            "scaling_grid": None,
            "exports": [],
        },
    )
    d = Document()
    m = d.import_resources(s, [1])
    assert d.resource(m[1])["children"][0]["id"] == m[1]


def test_failed_import_is_atomic():
    s = populated()
    s.tables["MovieClips"]["movieclips"][0]["children_ids"][0] = 12345
    d = populated()
    before = copy.deepcopy(d.tables)
    with pytest.raises(KeyError):
        d.import_resources(s, [7])
    assert d.tables == before


def test_delete_referenced_resource():
    d = populated()
    with pytest.raises(ValueError, match="referenced"):
        d.delete(3)


def test_v6_external_bank_append(tmp_path):
    import struct, zstandard

    d = populated()
    d.version = 6
    buf = struct.pack("<6f", 1, 0, 0, 1, 40, 20) * 16 + bytes(
        [255, 255, 255, 200, 0, 0, 0]
    )
    c = zstandard.ZstdCompressor().compress(buf)
    descriptor = {
        "banks": [
            {
                "index": 0,
                "compressed_data_offset": 0,
                "compressed_data_size": len(c),
                "decompressed_data_size": len(buf),
                "compressed_matrix_data_size": 0,
                "short_matrix_data_size": 0,
                "short_matrix_count": 0,
                "float_matrix_count": 16,
                "color_transform_count": 1,
                "clip_data_offset": len(buf),
                "clip_data_size": 0,
                "unk2": 0,
                "unk3": 10,
                "unk4": 0,
            }
        ]
    }
    d.external = encode(descriptor, "ExternalMatrixBanks") + c
    d.bank_cache.clear()
    assert d.bank(0)["matrices"][0] == [1, 0, 0, 1, 40, 20]
    source = populated()
    mapping = d.import_resources(source, [7])
    d.save(tmp_path / "v6.sc")
    r = Document.load(tmp_path / "v6.sc")
    assert r.version == 6
    assert r.bank(r.resource(mapping[7])["matrix_bank_index"])["matrices"][0] == [
        1,
        0,
        0,
        1,
        40,
        20,
    ]


@pytest.mark.skipif(not os.environ.get("SC_SAMPLE"), reason="SC_SAMPLE not supplied")
def test_supplied_sample(tmp_path):
    sample = Path(os.environ["SC_SAMPLE"])
    d = Document.load(sample)
    assert d.version == 6
    assert len(d.index) == 9698 + 14700 + 3278 + 3
    assert sum(map(len, d.names.values())) == 4135
    assert not d.validate()
    raw_before = dict(d.original)
    d.save(tmp_path / "roundtrip.sc")
    r = Document.load(tmp_path / "roundtrip.sc")
    assert r.original == raw_before
    assert r.external == d.external
    for kind in ["movieclips", "textfields", "shapes", "modifiers"]:
        id = next(i for i, (k, o) in d.index.items() if k == kind)
        assert d.resource(id) == r.resource(id)
    # Import a real game clip and its full graph into a new SC5.
    id = (
        next(i for i, n in d.names.items() if "button_small" in n)
        if any("button_small" in n for n in d.names.values())
        else next(iter(d.names))
    )
    dest = Document(5)
    mapping = dest.import_resources(d, [id])
    dest.save(tmp_path / "sample_clip_v5.sc")
    reload = Document.load(tmp_path / "sample_clip_v5.sc")
    assert reload.validate() == []
    assert reload.resource(mapping[id]) == dest.resource(mapping[id])


def test_sctx_sidecar_copy(tmp_path):
    s = populated()
    s.path = tmp_path / "source.sc"
    (tmp_path / "atlas.sctx").write_bytes(b"SCTX-test-payload")
    s.tables["Textures"]["textures"][0]["highres"]["external_texture"] = "atlas.sctx"
    s.tables["Textures"]["textures"][0]["highres"]["data"] = b""
    clip = Document()
    first = clip.import_resources(s, [7])
    dest = Document()
    second = dest.import_resources(clip, [first[7]])
    out = tmp_path / "destination" / "copied.sc"
    dest.save(out)
    r = Document.load(out)
    rel = r.tables["Textures"]["textures"][0]["highres"]["external_texture"]
    assert (out.parent / rel).read_bytes() == b"SCTX-test-payload"


def test_sidecar_keys_are_separator_independent(tmp_path):
    """Sidecar keys are POSIX, so a Windows save must still find them.

    The save path re-derives a lookup key from ``external_texture``; doing
    that with ``str(Path(...))`` yields backslashes on Windows and misses
    every key, which only the Windows CI lane caught.
    """
    from sc_studio.document import sidecar_key

    assert sidecar_key("textures/abc.zktx") == "textures/abc.zktx"
    assert sidecar_key(Path("textures") / "abc.zktx") == "textures/abc.zktx"

    d = populated()
    d.tables["Textures"]["textures"][0]["highres"]["data"] = b""
    d.tables["Textures"]["textures"][0]["highres"]["external_texture"] = (
        "textures/abc.zktx"
    )
    d.sidecars["textures/abc.zktx"] = b"SCTX-payload"
    assert d.path is None
    d.save(tmp_path / "out.sc")
    assert (tmp_path / "textures" / "abc.zktx").read_bytes() == b"SCTX-payload"


def test_texture_deduplication():
    s = populated()
    d = populated()
    d.import_resources(s, [7])
    assert len(d.tables["Textures"]["textures"]) == 1


def test_invalid_transform_validation():
    d = populated()
    obj = d.resource(7)
    obj["frames"][0]["elements"][0][1] = 100
    d.update("movieclips", obj)
    assert any("matrix index" in e for e in d.validate())


@pytest.mark.skipif(not os.environ.get("SC_SAMPLE"), reason="SC_SAMPLE not supplied")
def test_edit_and_import_into_sample(tmp_path):
    d = Document.load(os.environ["SC_SAMPLE"])
    id = next(i for i, (kind, o) in d.index.items() if kind == "textfields")
    o = d.resource(id)
    o["text"] = "SC Studio modified text"
    d.update("textfields", o)
    s = populated()
    mapping = d.import_resources(s, [7])
    out = tmp_path / "modified_sample.sc"
    d.save(out)
    r = Document.load(out)
    assert r.resource(id)["text"] == "SC Studio modified text"
    assert r.resource(mapping[7]) == d.resource(mapping[7])
    assert not r.validate()
    assert r.bank(r.resource(mapping[7])["matrix_bank_index"])["matrices"][0] == [
        1,
        0,
        0,
        1,
        40,
        20,
    ]


def test_default_new_document_is_v6_and_roundtrips(tmp_path):
    assert Document().version == 6
    doc = populated(6)
    path = tmp_path / "new-v6.sc"
    doc.save(path)
    loaded = Document.load(path)
    assert loaded.version == 6
    assert loaded.resource(7) == doc.resource(7)
    assert loaded.validate() == []


def test_atlas_regions_and_transitive_instance_usages():
    d = populated()
    other = d.resource(3)
    other["id"] = 5
    d.update("shapes", other)
    root = d.resource(7)
    root["id"] = 8
    root["exports"] = ["outer"]
    root["children"] = [{"id": 7, "name": "nested", "blend": 0}]
    root["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]}]
    d.update("movieclips", root)
    regions = d.atlas_regions(0)
    assert {r["id"] for r in regions} == {3, 5}
    assert all(r["rect"] == [0, 0, 2, 2] for r in regions)
    direct = d.usages(3)
    assert [(r["id"], r["child"], r["name"]) for r in direct] == [(7, 0, "sprite")]
    indirect = d.usages(3, indirect=True)
    assert {(r["id"], r["depth"]) for r in indirect} == {(7, 1), (8, 2)}
    # A parent cycle must not make the reverse traversal loop forever.
    root = d.resource(7)
    root["children"].append({"id": 8, "name": "cycle", "blend": 0})
    d.update("movieclips", root)
    assert len(d.usages(3, indirect=True)) == 3
