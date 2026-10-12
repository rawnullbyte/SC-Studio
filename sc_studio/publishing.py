"""Non-destructive publishing, deterministic padded atlas packing and presets."""

from dataclasses import dataclass, asdict
import hashlib
import json
import math
from pathlib import Path
import struct
import zstandard
from PIL import Image
from .document import Document, MKEYS, sidecar_key
from . import textures


@dataclass
class PublishSettings:
    version: int = 6
    linked_only: bool = True
    repack: bool = True
    max_width: int = 2048
    max_height: int = 2048
    padding: int = 2
    texture_scale: float = 1.0
    lowres: bool = False
    encoding: str = "RGBA8"
    external: bool = False
    compress_external: bool = True
    write_text: bool = True
    low_precision: bool = False
    short_frames: bool = False
    compression_level: int = 6
    replace_exports: bool = True

    def validate(self):
        if self.version not in (5, 6):
            raise ValueError("Publishing supports SC2 v5 and v6; SC1 is not supported")
        if self.encoding not in ("RGBA8", "RGBA4", "RGB565", "KTX"):
            raise ValueError("Encoding must be RGBA8, RGBA4, RGB565 or KTX")
        if not 16 <= self.max_width <= 8192 or not 16 <= self.max_height <= 8192:
            raise ValueError("Atlas dimensions must be 16..8192")
        if not 0 <= self.padding <= 16 or not 0.01 <= self.texture_scale <= 1:
            raise ValueError("Invalid padding or texture scale")
        if not 1 <= self.compression_level <= 22:
            raise ValueError("Zstandard level must be 1..22")
        if self.external and self.encoding != "KTX":
            raise ValueError("External textures require KTX encoding")
        if self.short_frames and self.version == 6:
            raise ValueError("Short frame descriptors are supported only by SC2 v5")

    @classmethod
    def load(cls, path):
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        result = cls(**values)
        result.validate()
        return result

    def save(self, path):
        self.validate()
        Path(path).write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )


def decode_texture(doc, data):
    name = data.get("external_texture")
    if name and sidecar_key(name) in doc.sidecars:
        data = dict(
            data, data=doc.sidecars[sidecar_key(name)], external_texture=None
        )
    return textures.image(data, doc.path.parent if doc.path else Path("."))


def _raw(image, encoding):
    image = image.convert("RGBA")
    data = image.tobytes()
    if encoding == "RGBA4":
        # SC/Pillow RGBA;4B uses G/R and A/B nibbles in byte order.
        data = bytes(
            value
            for r, g, b, a in zip(data[0::4], data[1::4], data[2::4], data[3::4])
            for value in ((g & 0xF0) | (r >> 4), (a & 0xF0) | (b >> 4))
        )
        pixel = 2
    elif encoding == "RGB565":
        if image.getextrema()[3][0] != 255:
            raise ValueError("RGB565 cannot encode transparency; use RGBA8 or RGBA4")
        data = b"".join(
            struct.pack("<H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
            for r, g, b, a in zip(data[0::4], data[1::4], data[2::4], data[3::4])
        )
        pixel = 4
    elif encoding == "KTX":
        header = b"\xabKTX 11\xbb\r\n\x1a\n" + struct.pack(
            "<13I",
            0x04030201,
            0x1401,
            1,
            0x1908,
            0x8058,
            0x1908,
            image.width,
            image.height,
            0,
            0,
            1,
            1,
            0,
        )
        data = header + struct.pack("<I", len(data)) + data
        pixel = 0
    else:
        pixel = 0
    return {
        "texture_format": 8 if encoding == "KTX" else 0,
        "pixel_type": pixel,
        "width": image.width,
        "height": image.height,
        "data": data,
        "external_texture": None,
    }


def repack_atlases(doc, settings):
    """Pack independently sampled command regions; preserve original geometry.

    Decode before editing anything. All commands sharing exactly the same crop
    share a tile. Pixel-edge UV conventions and constant-texel UVs are retained.
    """
    source = doc.tables["Textures"]["textures"]
    decoded = {
        i: decode_texture(doc, t["highres"] or t["lowres"])
        for i, t in enumerate(source)
    }
    tiles, commands = {}, []
    shapes = [doc.resource(i) for i, (k, _) in doc.index.items() if k == "shapes"]
    for shape in shapes:
        for command in shape["commands"]:
            vs = command["vertices"]
            if not vs:
                continue
            im = decoded[command["texture_index"]]
            us = [v[2] / 65535 * im.width for v in vs]
            ts = [v[3] / 65535 * im.height for v in vs]
            x0 = min(im.width - 1, max(0, math.floor(min(us))))
            y0 = min(im.height - 1, max(0, math.floor(min(ts))))
            x1 = min(im.width, max(x0 + 1, math.ceil(max(us))))
            y1 = min(im.height, max(y0 + 1, math.ceil(max(ts))))
            crop = im.crop((x0, y0, x1, y1))
            nw, nh = (
                max(1, round(crop.width * settings.texture_scale)),
                max(1, round(crop.height * settings.texture_scale)),
            )
            if crop.size != (nw, nh):
                crop = crop.resize((nw, nh), Image.Resampling.LANCZOS)
            key = (crop.size, hashlib.sha256(crop.tobytes()).digest())
            tiles.setdefault(key, {"image": crop})
            commands.append((command, key, x0, y0, x1 - x0, y1 - y0, im.size))
    pad = settings.padding
    pages = []
    for key, tile in sorted(
        tiles.items(),
        key=lambda kv: (-kv[1]["image"].height, -kv[1]["image"].width, kv[0][1]),
    ):
        im = tile["image"]
        w, h = im.width + 2 * pad, im.height + 2 * pad
        if w > settings.max_width or h > settings.max_height:
            raise ValueError(
                f"Sampled region {im.width}×{im.height} exceeds atlas size with padding"
            )
        selected = None
        for pi, page in enumerate(pages):
            if (
                page["x"] + w <= settings.max_width
                and page["y"] + h <= settings.max_height
            ):
                selected = pi
                break
            if page["y"] + page["row"] + h <= settings.max_height:
                page["y"] += page["row"]
                page["x"] = 0
                page["row"] = 0
                selected = pi
                break
        if selected is None:
            selected = len(pages)
            pages.append(
                {"x": 0, "y": 0, "row": 0, "width": 0, "height": 0, "tiles": []}
            )
        page = pages[selected]
        x, y = page["x"] + pad, page["y"] + pad
        tile.update(page=selected, x=x, y=y)
        page["tiles"].append(tile)
        page["x"] += w
        page["row"] = max(page["row"], h)
        page["width"] = max(page["width"], page["x"])
        page["height"] = max(page["height"], page["y"] + h)
    output = []
    for page in pages:
        atlas = Image.new("RGBA", (page["width"], page["height"]))
        for tile in page["tiles"]:
            im, x, y = tile["image"], tile["x"], tile["y"]
            # Clamp extrusion avoids dark seams at bilinear-sampled sprite edges.
            if pad:
                atlas.paste(
                    im.crop((0, 0, 1, im.height)).resize((pad, im.height)), (x - pad, y)
                )
                atlas.paste(
                    im.crop((im.width - 1, 0, im.width, im.height)).resize(
                        (pad, im.height)
                    ),
                    (x + im.width, y),
                )
                atlas.paste(
                    im.crop((0, 0, im.width, 1)).resize((im.width, pad)), (x, y - pad)
                )
                atlas.paste(
                    im.crop((0, im.height - 1, im.width, im.height)).resize(
                        (im.width, pad)
                    ),
                    (x, y + im.height),
                )
                for dx, dy, sx, sy in (
                    (-pad, -pad, 0, 0),
                    (im.width, -pad, im.width - 1, 0),
                    (-pad, im.height, 0, im.height - 1),
                    (im.width, im.height, im.width - 1, im.height - 1),
                ):
                    atlas.paste(
                        im.getpixel((sx, sy)),
                        (x + dx, y + dy, x + dx + pad, y + dy + pad),
                    )
            atlas.paste(im, (x, y))
        output.append(
            {
                "highres": _raw(atlas, settings.encoding),
                "lowres": _raw(
                    atlas.resize(
                        (max(1, atlas.width // 2), max(1, atlas.height // 2)),
                        Image.Resampling.LANCZOS,
                    ),
                    settings.encoding,
                )
                if settings.lowres
                else None,
            }
        )
    for command, key, x0, y0, w, h, (iw, ih) in commands:
        tile = tiles[key]
        page = pages[tile["page"]]
        for v in command["vertices"]:
            u = (v[2] / 65535 * iw - x0) / w * tile["image"].width + tile["x"]
            t = (v[3] / 65535 * ih - y0) / h * tile["image"].height + tile["y"]
            v[2] = min(65535, max(0, round(u / page["width"] * 65535)))
            v[3] = min(65535, max(0, round(t / page["height"] * 65535)))
        command["texture_index"] = tile["page"]
    doc.tables["Textures"]["textures"] = output
    doc.dirty.add("Textures")
    for shape in shapes:
        doc.update("shapes", shape)
    return {
        "source_atlases": len(source),
        "atlases": len(output),
        "unique_regions": len(tiles),
    }


def prepare_publish(sources, settings=None, roots=None, destination=None):
    settings = settings or PublishSettings()
    settings.validate()
    sources = list(sources)
    if not sources:
        raise ValueError("No source documents")
    output = Document(settings.version)
    report = {"sources": len(sources), "replaced_exports": []}
    if destination:
        output.import_resources(destination, list(destination.index))
    for si, source in enumerate(sources):
        ids = (
            list(roots)
            if roots is not None and si == 0
            else (list(source.names) if settings.linked_only else list(source.index))
        )
        if not ids:
            raise ValueError(
                "No linked symbols to publish; add an export name or choose all resources"
            )
        mapping = output.import_resources(source, ids)
        if settings.replace_exports:
            # Match original linkage names, replace old links and all references
            # to those entry points; unrelated destination content stays intact.
            for old, new in mapping.items():
                names = source.names.get(old, [])
                for name in names:
                    previous = next(
                        (
                            i
                            for i, ns in output.names.items()
                            if i != new and name in ns
                        ),
                        None,
                    )
                    if previous is not None:
                        output.set_exports(
                            previous, [n for n in output.names[previous] if n != name]
                        )
                        output._reindex()
                        for ci, (kind, raw) in list(output.index.items()):
                            if kind == "movieclips" and previous in raw["children_ids"]:
                                clip = output.resource(ci)
                                for child in clip["children"]:
                                    if child["id"] == previous:
                                        child["id"] = new
                                output.update("movieclips", clip)
                        report["replaced_exports"].append(name)
                if names:
                    output.set_exports(new, names)
                    output._reindex()
    if settings.repack:
        report.update(repack_atlases(output, settings))
    else:
        for texture in output.tables["Textures"]["textures"]:
            for variant in ("highres", "lowres"):
                if texture.get(variant):
                    im = decode_texture(output, texture[variant])
                    size = tuple(
                        max(1, round(x * settings.texture_scale)) for x in im.size
                    )
                    texture[variant] = _raw(
                        im.resize(size, Image.Resampling.LANCZOS), settings.encoding
                    )
            if settings.lowres and texture.get("highres"):
                im = decode_texture(output, texture["highres"])
                texture["lowres"] = _raw(
                    im.resize(
                        (max(1, im.width // 2), max(1, im.height // 2)),
                        Image.Resampling.LANCZOS,
                    ),
                    settings.encoding,
                )
        output.dirty.add("Textures")
    if not settings.write_text:
        for rid, (kind, _) in list(output.index.items()):
            if kind == "textfields":
                text = output.resource(rid)
                text["text"] = ""
                output.update(kind, text)
    if settings.low_precision:
        output.header.update(scale_precision=3, translation_precision=2)
        for bank in output.storage["matrix_banks"]:
            half = []
            for m in bank.get("matrices", []):
                values = {
                    k: round(m[k] * (1024 if i < 4 else 20))
                    for i, k in enumerate(MKEYS)
                }
                if any(not -32768 <= v <= 32767 for v in values.values()):
                    break
                half.append(values)
            else:
                bank["half_matrices"] = half
                bank["matrices"] = []
        output.bank_cache.clear()
        output.dirty.add("DataStorage")
    if settings.short_frames:
        for _, (kind, raw) in output.index.items():
            if kind == "movieclips" and all(
                f["label_ref_id"] == 0 for f in raw["frames"]
            ):
                raw["short_frames"] = [
                    {"used_transform": f["used_transform"]} for f in raw["frames"]
                ]
                raw["frames"] = []
        output.dirty.add("MovieClips")
    if settings.external:
        for texture in output.tables["Textures"]["textures"]:
            for variant in ("highres", "lowres"):
                data = texture.get(variant)
                if not data:
                    continue
                payload = data["data"]
                if settings.compress_external:
                    payload = zstandard.ZstdCompressor(
                        level=settings.compression_level
                    ).compress(payload)
                suffix = ".zktx" if settings.compress_external else ".ktx"
                name = "textures/" + hashlib.sha256(payload).hexdigest()[:24] + suffix
                output.sidecars[name] = payload
                data["data"] = b""
                data["external_texture"] = name
        output.dirty.add("Textures")
    errors = output.validate()
    if errors:
        raise ValueError("\n".join(errors))
    report.update(
        resources=len(output.index), exports=sum(map(len, output.names.values()))
    )
    return output, report


def publish(sources, path, settings=None, roots=None, destination=None):
    settings = settings or PublishSettings()
    output, report = prepare_publish(sources, settings, roots, destination)
    output.save(path, compression_level=settings.compression_level)
    report["path"] = str(output.path)
    report["bytes"] = output.path.stat().st_size
    return report


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Publish SC resources without changing source files"
    )
    parser.add_argument("sources", nargs="+")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--preset")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--merge", help="SC file whose exports should be replaced")
    args = parser.parse_args()
    settings = PublishSettings.load(args.preset) if args.preset else PublishSettings()
    if args.all:
        settings.linked_only = False
    target = Path(args.output).expanduser().resolve()
    if any(target == Path(p).expanduser().resolve() for p in args.sources):
        parser.error("Choose a separate output filename to preserve source files")
    result = publish(
        [Document.load(p) for p in args.sources],
        args.output,
        settings,
        destination=Document.load(args.merge) if args.merge else None,
    )
    print(json.dumps(result, indent=2))
