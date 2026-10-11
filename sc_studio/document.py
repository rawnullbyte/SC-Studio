"""SC2 v5/v6 document editing, lossless untouched chunks and dependency-aware import."""

from pathlib import Path
import array
import copy
import hashlib
import io
import math
import os
import struct
import tempfile
import zstandard
from . import atlas_crop
from .flatcodec import decode, encode
from .packed import external_banks, compressed_frame

KINDS = {
    "movieclips": ("MovieClips", "movieclips"),
    "shapes": ("Shapes", "shapes"),
    "textfields": ("TextFields", "textfields"),
    "modifiers": ("MovieClipModifiers", "modifiers"),
}
CHUNKS = [
    "DataStorage",
    "ExportNames",
    "TextFields",
    "Shapes",
    "MovieClips",
    "MovieClipModifiers",
    "Textures",
]
MKEYS = ["a", "b", "c", "d", "tx", "ty"]
CKEYS = ["r_mul", "g_mul", "b_mul", "alpha", "r_add", "g_add", "b_add"]


class FrameElements:
    """Read-only frame data, decoded only when preview/inspector needs its placements."""

    def __init__(self, frames, index):
        self.frames, self.index = frames, index

    def __len__(self):
        # The decoded placements, not the descriptor's count: the two agree for
        # a frame read straight from the file, but an edit appends to the
        # decoded list without touching the descriptor, and a length taken from
        # there would hide everything that was added.
        return len(self.frames.elements(self.index))

    def __iter__(self):
        return iter(self.frames.elements(self.index))

    def __getitem__(self, index):
        return self.frames.elements(self.index)[index]


class FrameSequence:
    def __init__(self, doc, raw, descriptors):
        from collections import OrderedDict

        self.doc, self.raw, self.descriptors = doc, raw, descriptors
        self.cache = OrderedDict()
        self.offsets = []
        offset = raw.get("frame_elements_offset")
        for f in descriptors:
            self.offsets.append(offset)
            if offset is not None:
                offset += 3 * f["used_transform"]

    def __len__(self):
        return len(self.descriptors)

    def __iter__(self):
        return (self[i] for i in range(len(self)))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return {
            "label": self.doc.string(self.descriptors[index]["label_ref_id"]),
            "elements": FrameElements(self, index),
        }

    def elements(self, index):
        if index not in self.cache:
            offset = self.offsets[index]
            if offset is None:
                bank = self.doc.bank(self.raw["matrix_bank_index"])
                values = compressed_frame(
                    bank["clips"], self.raw.get("compressed_data_offset", 0), index
                )
            else:
                stop = offset + 3 * self.descriptors[index]["used_transform"]
                storage = self.doc.storage["movieclips_frame_elements"]
                if stop > len(storage):
                    raise ValueError("Clip frame element range out of bounds")
                values = [list(storage[p : p + 3]) for p in range(offset, stop, 3)]
            self.cache[index] = values
            while len(self.cache) > 8:
                self.cache.popitem(last=False)
        self.cache.move_to_end(index)
        return self.cache[index]


class Document:
    def __init__(self, version=6):
        if version not in (5, 6):
            raise ValueError("Only SC2 versions 5 and 6 are supported")
        self.path = None
        self.version = version
        self.v6_flags = b"\0\0"
        self.dirty = set()
        self.original = {}
        self.external = b""
        self.trailing = b""
        self.bank_cache = {}
        self.sidecars = {}
        self.header = {"translation_precision": 1, "scale_precision": 1, "metadata": []}
        self.tables = {name: {} for name in CHUNKS}
        self.tables["DataStorage"] = {
            "strings": [""],
            "rectangles": [],
            "movieclips_frame_elements": array.array("H"),
            "shapes_bitmap_poins": b"",
            "matrix_banks": [],
            "unk2": b"",
            "unk3": b"",
        }
        for kind, (table, key) in KINDS.items():
            self.tables[table][key] = []
        self.tables["ExportNames"] = {
            "object_ids": array.array("H"),
            "name_ref_ids": array.array("I"),
        }
        self.tables["Textures"] = {"textures": []}
        self._reindex()

    @classmethod
    def load(cls, path):
        result = cls()
        result.path = Path(path).resolve()
        data = result.path.read_bytes()
        if data[:2] != b"SC":
            raise ValueError(
                "Expected SC2 file with SC signature. Legacy SC1 is not supported by this editor."
            )
        result.version = struct.unpack_from("<I", data, 2)[0]
        if result.version not in (5, 6):
            raise ValueError(
                f"SC version {result.version} unsupported; this editor reads SC2 versions 5 and 6"
            )
        pos = 6
        if result.version == 6:
            result.v6_flags = data[pos : pos + 2]
            pos += 2
        size = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if size > len(data) - pos:
            raise ValueError("Truncated SC header")
        result.header = decode(data[pos : pos + size], "Header")
        pos += size
        csize = result.header.get("compressed_size") or len(data) - pos
        payload = data[pos : pos + csize]
        pos += csize
        if payload.startswith(b"\x28\xb5\x2f\xfd"):
            payload = zstandard.ZstdDecompressor().decompress(
                payload, max_output_size=1024 * 1024 * 1024
            )
        esize = result.header.get("external_matrix_bank_size", 0)
        result.external = data[pos : pos + esize]
        result.trailing = data[pos + esize :]
        cursor = 0
        for index, name in enumerate(CHUNKS):
            if index == 1:
                if cursor != result.header["resources_offset"]:
                    raise ValueError(
                        "Unrecognized data before resource chunks; refused to discard it"
                    )
                cursor = result.header["resources_offset"]
            n = struct.unpack_from("<I", payload, cursor)[0]
            chunk = payload[cursor : cursor + 4 + n]
            if len(chunk) != 4 + n:
                raise ValueError(f"Truncated {name} chunk")
            schema = (
                "CompressedMovieClips"
                if name == "MovieClips" and result.version == 6
                else name
            )
            result.tables[name] = decode(chunk[4:], schema)
            result.original[name] = chunk
            cursor += 4 + n
        if cursor != len(payload):
            raise ValueError(
                "Unrecognized extra SC payload data; refused to discard it"
            )
        result._reindex()
        return result

    def _reindex(self):
        self.index = {}
        for kind, (table, key) in KINDS.items():
            for obj in self.tables[table].get(key, []):
                if obj["id"] in self.index:
                    raise ValueError(f'Duplicate resource id {obj["id"]}')
                self.index[obj["id"]] = (kind, obj)
        self.names = {}
        for id, ref in zip(
            self.tables["ExportNames"]["object_ids"],
            self.tables["ExportNames"]["name_ref_ids"],
        ):
            self.names.setdefault(id, []).append(self.string(ref))

    @property
    def storage(self):
        return self.tables["DataStorage"]

    def string(self, ref):
        values = self.storage["strings"]
        if ref >= len(values):
            raise ValueError(f"String reference {ref} out of bounds")
        return values[ref]

    def intern(self, value):
        try:
            return self.storage["strings"].index(value)
        except ValueError:
            self.storage["strings"].append(value)
            self.dirty.add("DataStorage")
            return len(self.storage["strings"]) - 1

    def next_id(self):
        for i in range(65535):
            if i not in self.index:
                return i
        raise ValueError("No unused 16-bit resource IDs remain")

    def atlas_regions(self, index):
        """Referenced UV regions in atlas pixels, retaining overlapping shape/command IDs."""
        data = self.tables["Textures"]["textures"][index]["highres"]
        width, height = data["width"], data["height"]
        buf = self.storage["shapes_bitmap_poins"]
        regions = []
        for id, (kind, raw) in self.index.items():
            if kind != "shapes":
                continue
            for ci, command in enumerate(raw["commands"]):
                if command["texture_index"] != index or not command["points_count"]:
                    continue
                start = command["points_offset"] * 12
                stop = start + command["points_count"] * 12
                if stop > len(buf):
                    raise ValueError(f"Shape {id}: atlas vertex range out of bounds")
                uv = [
                    struct.unpack_from("<HH", buf, p + 8)
                    for p in range(start, stop, 12)
                ]
                left, right = (
                    min(p[0] for p in uv) * width / 65535,
                    max(p[0] for p in uv) * width / 65535,
                )
                top, bottom = (
                    min(p[1] for p in uv) * height / 65535,
                    max(p[1] for p in uv) * height / 65535,
                )
                # Commands whose corners all sample one texel have no area, but
                # they are kept rather than dropped: over half the commands in a
                # full UI atlas are these, and discarding them hid their shapes
                # from the atlas view and from picking entirely. The rect is then
                # the single texel, which is what the viewer draws as a marker.
                regions.append(
                    {
                        "id": id,
                        "command": ci,
                        "rect": [left, top, right - left, bottom - top],
                    }
                )
        return regions

    def usages(self, target, indirect=False):
        """Referencing child instances, optionally walking reverse edges up to exported roots."""
        parents = {}
        for id, (kind, raw) in self.index.items():
            if kind == "movieclips":
                for ci, child in enumerate(raw["children_ids"]):
                    parents.setdefault(child, []).append((id, ci, raw))
        from collections import deque

        results, queue, seen = [], deque([(target, 0)]), {target}
        while queue:
            child, depth = queue.popleft()
            for id, ci, raw in parents.get(child, []):
                refs = raw.get("children_name_ref_ids", [])
                results.append(
                    {
                        "id": id,
                        "child": ci,
                        "name": self.string(refs[ci]) if ci < len(refs) else "",
                        "via": child,
                        "depth": depth + 1,
                    }
                )
                if indirect and id not in seen:
                    seen.add(id)
                    queue.append((id, depth + 1))
            if not indirect:
                break
        return results

    def reference_graph(self, target):
        """All reachable reverse references, retaining shared edges and cycles."""
        if target not in self.index:
            raise KeyError(target)
        rows = self.usages(target, indirect=True)
        ids = {target}
        edges = []
        for row in rows:
            ids.update((row["id"], row["via"]))
            edges.append({"from": row["via"], "to": row["id"],
                          "child": row["child"], "name": row["name"]})
        outgoing = {edge["from"] for edge in edges}
        return {"root": target,
                "nodes": [{"id": id, "kind": self.index[id][0],
                           "exports": self.names.get(id, []),
                           "terminal": id not in outgoing} for id in sorted(ids)],
                "edges": edges}

    def resource(self, id, lazy_frames=False):
        kind, raw = self.index[id]
        obj = copy.deepcopy(raw)
        obj["exports"] = self.names.get(id, []).copy()
        if kind == "movieclips":
            obj["children"] = [
                {
                    "id": v,
                    "name": (
                        self.string(raw["children_name_ref_ids"][i])
                        if i < len(raw.get("children_name_ref_ids", []))
                        else ""
                    ),
                    "blend": (
                        raw["children_blending"][i]
                        if i < len(raw.get("children_blending", []))
                        else 0
                    ),
                }
                for i, v in enumerate(raw["children_ids"])
            ]
            frames = raw["frames"] or [
                {"used_transform": f["used_transform"], "label_ref_id": 0}
                for f in raw.get("short_frames", [])
            ]
            obj["frames"] = []
            offset = raw.get("frame_elements_offset")
            elements = self.storage["movieclips_frame_elements"]
            rect = raw.get("scaling_grid_index")
            if lazy_frames:
                obj["frames"] = FrameSequence(self, raw, frames)
            else:
                for i, f in enumerate(frames):
                    n = f["used_transform"]
                    frame = {"label": self.string(f["label_ref_id"]), "elements": []}
                    if offset is None:
                        bank = self.bank(raw["matrix_bank_index"])
                        frame["elements"] = compressed_frame(
                            bank["clips"], raw.get("compressed_data_offset", 0), i
                        )
                    else:
                        stop = offset + 3 * n
                        if stop > len(elements):
                            raise ValueError(
                                f"Clip {id}: frame element range out of bounds"
                            )
                        frame["elements"] = [
                            list(elements[p : p + 3]) for p in range(offset, stop, 3)
                        ]
                        offset = stop
                    obj["frames"].append(frame)
            obj["scaling_grid"] = (
                copy.deepcopy(self.storage["rectangles"][rect])
                if rect is not None
                else None
            )
            for k in [
                "children_ids",
                "children_name_ref_ids",
                "children_blending",
                "short_frames",
                "frame_elements_offset",
                "scaling_grid_index",
                "export_name_ref_id",
                "compressed_data_offset",
            ]:
                obj.pop(k, None)
        elif kind == "textfields":
            for k in ["font_name", "text", "typography"]:
                obj[k] = self.string(obj.pop(k + "_ref_id"))
        elif kind == "shapes":
            for command in obj["commands"]:
                p = command.pop("points_offset") * 12
                n = command.pop("points_count")
                buf = self.storage["shapes_bitmap_poins"]
                if p + 12 * n > len(buf):
                    raise ValueError("Shape vertex buffer out of bounds")
                command["vertices"] = [
                    list(struct.unpack_from("<ffHH", buf, p + i * 12)) for i in range(n)
                ]
        return obj

    def update(self, kind, obj):
        obj = copy.deepcopy(obj)
        exports = obj.pop("exports", [])
        id = obj["id"]
        if id in self.index and self.index[id][0] != kind:
            raise ValueError("ID belongs to a different resource type")
        if kind == "movieclips":
            children = obj.pop("children")
            frames = obj.pop("frames")
            grid = obj.pop("scaling_grid", None)
            obj["children_ids"] = array.array("H", [c["id"] for c in children])
            obj["children_name_ref_ids"] = array.array(
                "I", [self.intern(c.get("name", "")) for c in children]
            )
            obj["children_blending"] = bytes(c.get("blend", 0) for c in children)
            obj["frames_count"] = len(frames)
            obj["frames"] = []
            obj["short_frames"] = []
            obj["frame_elements_offset"] = len(
                self.storage["movieclips_frame_elements"]
            )
            obj["compressed_data_offset"] = 0
            for f in frames:
                obj["frames"].append(
                    {
                        "used_transform": len(f["elements"]),
                        "label_ref_id": self.intern(f.get("label", "")),
                    }
                )
                for e in f["elements"]:
                    if len(e) != 3 or e[0] >= len(children):
                        raise ValueError(
                            "Each frame element must be [child_index, matrix_index, color_index]"
                        )
                    self.storage["movieclips_frame_elements"].extend(e)
            obj["scaling_grid_index"] = None
            if grid is not None:
                obj["scaling_grid_index"] = len(self.storage["rectangles"])
                self.storage["rectangles"].append(grid)
            obj["export_name_ref_id"] = self.intern(exports[0]) if exports else None
            self.dirty.add("DataStorage")
        elif kind == "textfields":
            for k in ["font_name", "text", "typography"]:
                obj[k + "_ref_id"] = self.intern(obj.pop(k, ""))
        elif kind == "shapes":
            buf = bytearray(self.storage["shapes_bitmap_poins"])
            for command in obj["commands"]:
                vertices = command.pop("vertices")
                command["points_count"] = len(vertices)
                command["points_offset"] = len(buf) // 12
                for v in vertices:
                    buf.extend(struct.pack("<ffHH", *v))
            self.storage["shapes_bitmap_poins"] = bytes(buf)
            self.dirty.add("DataStorage")
        table, key = KINDS[kind]
        schema = (
            "CompressedMovieClips"
            if table == "MovieClips" and self.version == 6
            else table
        )
        encode(
            {key: [obj]}, schema
        )  # Validate scalar widths before committing the object.
        items = self.tables[table][key]
        if id in self.index:
            items[items.index(self.index[id][1])] = obj
        else:
            items.append(obj)
        self.dirty.add(table)
        self.set_exports(id, exports)
        self._reindex()

    def set_exports(self, id, names):
        for name in names:
            if any(
                name in values and other != id for other, values in self.names.items()
            ):
                raise ValueError(f"Export name already used: {name}")
        e = self.tables["ExportNames"]
        pairs = [(a, b) for a, b in zip(e["object_ids"], e["name_ref_ids"]) if a != id]
        pairs.extend((id, self.intern(name)) for name in names)
        e["object_ids"] = array.array("H", (x[0] for x in pairs))
        e["name_ref_ids"] = array.array("I", (x[1] for x in pairs))
        self.dirty.add("ExportNames")

    def bank(self, index):
        if index in self.bank_cache:
            return self.bank_cache[index]
        if self.external:
            # Decode only the requested bank by filtering its descriptor, retaining offsets.
            n = struct.unpack_from("<I", self.external)[0]
            desc = decode(self.external[4 : 4 + n], "ExternalMatrixBanks")
            selected = [b for b in desc["banks"] if b["index"] == index]
            if not selected:
                raise ValueError(f"Matrix bank {index} missing")
            prefix = encode({"banks": selected}, "ExternalMatrixBanks")
            bank = external_banks(prefix + self.external[4 + n :])[index]
        else:
            raw = self.storage["matrix_banks"][index]
            ms = raw.get("matrices", [])
            if ms:
                matrices = [[v[k] for k in MKEYS] for v in ms]
            else:
                precision = {0: 1, 1: 1, 2: 20, 3: 1024}
                sm = precision.get(self.header["scale_precision"])
                tm = precision.get(self.header["translation_precision"])
                if sm is None or tm is None:
                    raise ValueError("Unknown half-matrix precision")
                matrices = [
                    [v[k] / (sm if i < 4 else tm) for i, k in enumerate(MKEYS)]
                    for v in raw.get("half_matrices", [])
                ]
            bank = {
                "matrices": matrices,
                "colors": [[v[k] for k in CKEYS] for v in raw.get("colors", [])],
                "clips": b"",
            }
        self.bank_cache[index] = bank
        return bank

    def add_bank(self, matrices, colors):
        index = len(self.storage["matrix_banks"])
        raw = {
            "matrices": [dict(zip(MKEYS, m)) for m in matrices],
            "colors": [dict(zip(CKEYS, c)) for c in colors],
            "half_matrices": [],
        }
        self.storage["matrix_banks"].append(raw)
        self.bank_cache[index] = {"matrices": matrices, "colors": colors, "clips": b""}
        self.dirty.add("DataStorage")
        if self.external:
            n = struct.unpack_from("<I", self.external)[0]
            desc = decode(self.external[4 : 4 + n], "ExternalMatrixBanks")
            data = self.external[4 + n :]
            # External format requires uncompressed matrix counts rounded to a block of 16.
            padded = matrices + [[1, 0, 0, 1, 0, 0]] * ((-len(matrices)) % 16)
            buf = b"".join(struct.pack("<6f", *m) for m in padded) + b"".join(
                bytes(c) for c in colors
            )
            compressed = zstandard.ZstdCompressor().compress(buf)
            desc["banks"].append(
                {
                    "index": index,
                    "compressed_data_offset": len(data),
                    "compressed_data_size": len(compressed),
                    "decompressed_data_size": len(buf),
                    "compressed_matrix_data_size": 0,
                    "short_matrix_data_size": 0,
                    "short_matrix_count": 0,
                    "float_matrix_count": len(padded),
                    "color_transform_count": len(colors),
                    "clip_data_offset": len(buf),
                    "clip_data_size": 0,
                    "unk2": 0,
                    "unk3": 10,
                    "unk4": 0,
                }
            )
            self.external = encode(desc, "ExternalMatrixBanks") + data + compressed
        return index

    def new_resource(self, kind):
        id = self.next_id()
        if kind == "movieclips":
            obj = {
                "id": id,
                "framerate": 30,
                "unknown_bool": 0,
                "matrix_bank_index": 0,
                "children": [],
                "frames": [{"label": "", "elements": []}],
                "scaling_grid": None,
                "exports": [],
            }
        elif kind == "shapes":
            obj = {"id": id, "commands": [], "exports": []}
        elif kind == "textfields":
            obj = {
                "id": id,
                "unused1": 0,
                "font_name": "Arial",
                "left": 0,
                "top": 0,
                "right": 200,
                "bottom": 50,
                "font_color": 0xFFFFFFFF,
                "outline_color": 0xFF000000,
                "text": "Text",
                "typography": "",
                "styles": 0,
                "align": 0,
                "font_size": 24,
                "unused2": 0,
                "outline_angle": 0,
                "exports": [],
            }
        else:
            obj = {"id": id, "type": 38, "exports": []}
        self.update(kind, obj)
        return id

    def delete(self, id):
        for other, (kind, raw) in self.index.items():
            if kind == "movieclips" and id in raw["children_ids"]:
                raise ValueError(
                    f"Resource is referenced by movieclip {other}; remove that child first"
                )
        kind, raw = self.index[id]
        table, key = KINDS[kind]
        self.tables[table][key].remove(raw)
        self.dirty.add(table)
        self.set_exports(id, [])
        self._reindex()

    def delete_many(self, ids):
        ids = set(ids)
        for owner, (kind, raw) in self.index.items():
            if owner not in ids and kind == "movieclips":
                blocked = ids.intersection(raw["children_ids"])
                if blocked:
                    raise ValueError(
                        f"Resource {min(blocked)} is referenced by movieclip {owner}"
                    )
        for id in ids:
            kind, raw = self.index[id]
            table, key = KINDS[kind]
            self.tables[table][key].remove(raw)
            self.dirty.add(table)
            self.set_exports(id, [])
        self._reindex()

    def duplicate_resources(self, ids):
        """Copy only selected definitions; child IDs, atlases and banks stay shared.

        Packed frame/vertex ranges are immutable: update() writes new ranges on edit.
        Prepare every copy and ID before touching the document.
        """
        ids = list(dict.fromkeys(ids))
        for id in ids:
            if id not in self.index:
                raise ValueError(f"Resource {id} does not exist")
        free = (id for id in range(65535) if id not in self.index)
        mapping, copies = {}, []
        used_names = {name for values in self.names.values() for name in values}
        for old in ids:
            id = next(free, None)
            if id is None:
                raise ValueError("No unused 16-bit resource IDs remain")
            kind, raw = self.index[old]
            clone = copy.deepcopy(raw)
            clone["id"] = id
            names = []
            for base in self.names.get(old, []):
                suffix = 2
                name = f"{base}_copy{suffix}"
                while name in used_names:
                    suffix += 1
                    name = f"{base}_copy{suffix}"
                used_names.add(name)
                names.append(name)
            copies.append((kind, clone, names))
            mapping[old] = id
        for kind, clone, names in copies:
            if kind == "movieclips":
                clone["export_name_ref_id"] = self.intern(names[0]) if names else None
            table, key = KINDS[kind]
            self.tables[table][key].append(clone)
            self.dirty.add(table)
            self.set_exports(clone["id"], names)
        self._reindex()
        return mapping

    def import_resources(self, source, ids):
        """Atomic graph import, including cycles, shared children, banks and atlases."""
        backup = copy.deepcopy(self.__dict__)
        try:
            return self._import(source, ids)
        except Exception:
            self.__dict__ = backup
            raise

    def _plan_crops(self, source, objects):
        """Cropped replacement per atlas, keyed by texture index.

        Runs before anything is copied so each atlas is cropped once against
        every shape that samples it. highres and lowres must stay in step, since
        one UV rewrite has to serve both, so an atlas whose variants cannot all
        be cropped is left out and copied whole, as earlier releases did.
        """
        by_atlas = {}
        for _, (kind, obj) in objects.items():
            if kind != "shapes":
                continue
            for command in obj["commands"]:
                by_atlas.setdefault(command["texture_index"], set()).add(obj["id"])
        plan = {}
        for index, shape_ids in by_atlas.items():
            if index >= len(source.tables["Textures"]["textures"]):
                continue
            texture = source.tables["Textures"]["textures"][index]
            # atlas_regions measures UVs against highres, so a texture without
            # one (only lowres present) has no usable geometry to crop against.
            if not texture.get("highres"):
                continue
            rects = [
                region["rect"]
                for region in source.atlas_regions(index)
                if region["id"] in shape_ids
            ]
            if not rects:
                continue
            variants = {}
            for variant in ("highres", "lowres"):
                data = texture.get(variant)
                if not data:
                    continue
                # atlas_regions reports highres pixels, and lowres is usually
                # half size, so its rects must be scaled or it crops to the
                # wrong region (too large, wasting exactly the space this is
                # meant to save).
                scaled = rects
                highres = texture.get("highres")
                if variant == "lowres" and highres and highres["width"]:
                    ratio = data["width"] / highres["width"]
                    scaled = [
                        [x * ratio, y * ratio, w * ratio, h * ratio]
                        for x, y, w, h in rects
                    ]
                try:
                    crop = atlas_crop.crop_texture(data, scaled)
                except atlas_crop.CropError:
                    variants = {}
                    break
                if crop is None:
                    variants = {}
                    break
                variants[variant] = crop
            if variants:
                plan[index] = variants
        return plan

    def _texture_for(self, source, tex, crops, cropped):
        """Copy one atlas, cropped when it has a crop plan, and cache its result.

        Returns ``(texture_index, offset, block, source_size, cropped_size)``,
        where ``offset`` is None when the atlas was copied whole and the shape's
        UVs must be left alone.
        """
        if tex in cropped:
            return cropped[tex]
        texture = copy.deepcopy(source.tables["Textures"]["textures"][tex])
        for variant in ("highres", "lowres"):
            data = texture.get(variant)
            if data and data.get("external_texture"):
                rel = data["external_texture"]
                payload = source.sidecars.get(rel)
                if payload is None:
                    if source.path is None:
                        raise ValueError(f"Missing external texture: {rel}")
                    payload = (source.path.parent / rel).read_bytes()
                unpacked = payload
                if payload.startswith(b"\x28\xb5\x2f\xfd"):
                    unpacked = zstandard.ZstdDecompressor().decompress(payload)
                if unpacked.startswith(b"\xabKTX 11"):
                    data["data"] = unpacked
                    data["external_texture"] = None
                    data["texture_format"] = 8
                else:
                    suffix = Path(rel).suffix or ".bin"
                    name = (
                        "import_" + hashlib.sha256(payload).hexdigest()[:24] + suffix
                    )
                    self.sidecars[name] = payload
                    data["external_texture"] = name
        crop = crops.get(tex)
        source_size = cropped_size = None
        block = offset = None
        if crop:
            for variant, values in crop.items():
                texture[variant] = values["dict"]
            # One crop geometry serves both variants, so take it from highres
            # when present and fall back to whichever variant does exist.
            lead_variant = "highres" if "highres" in crop else next(iter(crop))
            lead = crop[lead_variant]
            original = source.tables["Textures"]["textures"][tex][lead_variant]
            source_size = (original["width"], original["height"])
            cropped_size = (lead["width"], lead["height"])
            block = lead["block"]
            offset = lead["offset"]
        target = self.tables["Textures"]["textures"]
        existing = next(
            (i for i, value in enumerate(target) if value == texture), None
        )
        index = existing if existing is not None else len(target)
        if existing is None:
            target.append(texture)
            self.dirty.add("Textures")
        result = (index, offset, block, source_size, cropped_size)
        cropped[tex] = result
        return result

    def _import(self, source, ids):
        pending = list(ids)
        objects = {}
        while pending:
            id = pending.pop()
            if id in objects:
                continue
            kind, _ = source.index[id]
            obj = source.resource(id)
            objects[id] = (kind, obj)
            if kind == "movieclips":
                pending.extend(c["id"] for c in obj["children"])
        free = (i for i in range(65535) if i not in self.index)
        mapping = {i: next(free) for i in objects}
        textures = {}
        banks = {}
        crops = self._plan_crops(source, objects)
        cropped = {}
        for old, (kind, obj) in objects.items():
            obj["id"] = mapping[old]
            used = set(n for values in self.names.values() for n in values)
            names = []
            for name in obj["exports"]:
                base = name
                suffix = 2
                while name in used:
                    name = f"{base}_copy{suffix}"
                    suffix += 1
                used.add(name)
                names.append(name)
            obj["exports"] = names
            if kind == "shapes":
                for c in obj["commands"]:
                    tex = c["texture_index"]
                    if tex not in textures:
                        textures[tex] = self._texture_for(source, tex, crops, cropped)
                    index, offset, block, source_size, cropped_size = textures[tex]
                    c["texture_index"] = index
                    if offset is not None:
                        c["vertices"] = atlas_crop.remap_uv(
                            c["vertices"], offset, block, source_size, cropped_size
                        )
            elif kind == "movieclips":
                for c in obj["children"]:
                    c["id"] = mapping[c["id"]]
                oldbank = obj["matrix_bank_index"]
                usedm = sorted(
                    {
                        e[1]
                        for f in obj["frames"]
                        for e in f["elements"]
                        if e[1] != 65535
                    }
                )
                usedc = sorted(
                    {
                        e[2]
                        for f in obj["frames"]
                        for e in f["elements"]
                        if e[2] != 65535
                    }
                )
                bank = (
                    source.bank(oldbank)
                    if usedm or usedc
                    else {"matrices": [], "colors": []}
                )
                if len(usedm) > 65535 or len(usedc) > 65535:
                    raise ValueError("Transform bank overflow")
                mi = {old: i for i, old in enumerate(usedm)}
                ci = {old: i for i, old in enumerate(usedc)}
                obj["matrix_bank_index"] = self.add_bank(
                    [bank["matrices"][i] for i in usedm],
                    [bank["colors"][i] for i in usedc],
                )
                for f in obj["frames"]:
                    for e in f["elements"]:
                        e[1] = mi.get(e[1], 65535)
                        e[2] = ci.get(e[2], 65535)
            self.update(kind, obj)
        return mapping

    def validate(self, only=None):
        """Structural errors in the document.

        ``only`` restricts the per-resource sweep to that set of ids, which the
        editor uses for single-resource edits: the full sweep is O(resources)
        and a large file makes it the dominant cost of every edit. The whole
        file is still validated on save, so this is a latency optimisation and
        never the last line of defence.
        """
        errors = []
        if self.external:
            n = struct.unpack_from("<I", self.external)[0]
            descriptor = decode(self.external[4 : 4 + n], "ExternalMatrixBanks")
            counts = {
                b["index"]: (
                    min(
                        max(
                            b["short_matrix_count"] + b["float_matrix_count"],
                            b["compressed_matrix_data_size"] * 16,
                        ),
                        65535,
                    ),
                    b["color_transform_count"],
                )
                for b in descriptor["banks"]
            }
        else:
            counts = {
                i: (
                    len(b.get("matrices", [])) or len(b.get("half_matrices", [])),
                    len(b.get("colors", [])),
                )
                for i, b in enumerate(self.storage["matrix_banks"])
            }
        elements = self.storage["movieclips_frame_elements"]
        for id, (kind, raw) in self.index.items():
            if only is not None and id not in only:
                continue
            if kind == "movieclips":
                for child in raw["children_ids"]:
                    if child not in self.index:
                        errors.append(f"Clip {id} references missing object {child}")
                frames = raw["frames"] or raw.get("short_frames", [])
                if len(frames) != raw["frames_count"]:
                    errors.append(f"Clip {id}: frame count differs from timeline")
                count = sum(f["used_transform"] for f in frames)
                offset = raw.get("frame_elements_offset")
                if offset is not None and count:
                    end = offset + count * 3
                    if end > len(elements):
                        errors.append(f"Clip {id}: placement buffer out of bounds")
                        continue
                    if max(elements[offset:end:3], default=0) >= len(
                        raw["children_ids"]
                    ):
                        errors.append(f"Clip {id}: invalid child placement index")
                    mc, cc = counts.get(raw["matrix_bank_index"], (0, 0))
                    if any(
                        v != 65535 and v >= mc for v in elements[offset + 1 : end : 3]
                    ):
                        errors.append(f"Clip {id}: matrix index out of bounds")
                    if any(
                        v != 65535 and v >= cc for v in elements[offset + 2 : end : 3]
                    ):
                        errors.append(f"Clip {id}: color index out of bounds")
                rect = raw.get("scaling_grid_index")
                if rect is not None and rect >= len(self.storage["rectangles"]):
                    errors.append(f"Clip {id}: scaling grid out of bounds")
            elif kind == "shapes":
                for c in raw["commands"]:
                    if c["texture_index"] >= len(self.tables["Textures"]["textures"]):
                        errors.append(f"Shape {id}: missing texture")
                    if (c["points_offset"] + c["points_count"]) * 12 > len(
                        self.storage["shapes_bitmap_poins"]
                    ):
                        errors.append(f"Shape {id}: vertex range out of bounds")
        for id, ref in zip(
            self.tables["ExportNames"]["object_ids"],
            self.tables["ExportNames"]["name_ref_ids"],
        ):
            if id not in self.index:
                errors.append(f"Export references missing object {id}")
            if ref >= len(self.storage["strings"]):
                errors.append("Export string reference out of bounds")
        return errors

    def save(self, path, compression_level=6):
        errors = self.validate()
        if errors:
            raise ValueError("\n".join(errors[:20]))
        chunks = []
        for name in CHUNKS:
            schema = (
                "CompressedMovieClips"
                if name == "MovieClips" and self.version == 6
                else name
            )
            chunks.append(
                self.original[name]
                if name in self.original and name not in self.dirty
                else encode(self.tables[name], schema)
            )
        header = copy.deepcopy(self.header)
        header.update(
            shape_count=len(self.tables["Shapes"]["shapes"]),
            movie_clips_count=len(self.tables["MovieClips"]["movieclips"]),
            texture_count=len(self.tables["Textures"]["textures"]),
            text_fields_count=len(self.tables["TextFields"]["textfields"]),
            resources_offset=len(chunks[0]),
            textures_length=len(chunks[-1]),
            external_matrix_bank_size=len(self.external),
        )
        previous = {x["name"]: x for x in header.get("metadata", [])}
        header["metadata"] = [
            previous.get(n, {"name": n, "hash": b""})
            for values in self.names.values()
            for n in values
        ]
        compressed = zstandard.ZstdCompressor(level=compression_level).compress(b"".join(chunks))
        header["compressed_size"] = len(compressed)
        output = (
            b"SC"
            + struct.pack("<I", self.version)
            + (self.v6_flags if self.version == 6 else b"")
            + encode(header, "Header")
            + compressed
            + self.external
            + self.trailing
        )
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep relative external textures valid when using Save As.
        for texture in self.tables["Textures"]["textures"]:
            for variant in ("highres", "lowres"):
                data = texture.get(variant)
                if data and data.get("external_texture"):
                    rel = Path(data["external_texture"])
                    if rel.is_absolute() or ".." in rel.parts:
                        raise ValueError("Unsafe external texture path")
                    dest = path.parent / rel
                    payload = self.sidecars.get(str(rel))
                    if payload is None:
                        if self.path is None:
                            raise ValueError(f"Missing external texture {rel}")
                        src = self.path.parent / rel
                        if src.resolve() == dest.resolve():
                            continue
                        payload = src.read_bytes()
                    if dest.exists() and dest.read_bytes() != payload:
                        raise ValueError(f"External texture conflict: {rel}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(payload)
        fd, tmp = tempfile.mkstemp(prefix=".scstudio-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(output)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.path = path
        self.header = header
        self.original = dict(zip(CHUNKS, chunks))
        self.dirty.clear()
