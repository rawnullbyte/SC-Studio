"""Small schema-driven FlatBuffers codec; retains every declared field, including unknowns."""

from pathlib import Path
import re
import array
import sys
import struct
import flatbuffers

SCALARS = {
    "ubyte": ("B", 1),
    "ushort": ("H", 2),
    "uint": ("I", 4),
    "short": ("h", 2),
    "int": ("i", 4),
    "float": ("f", 4),
}
SCHEMAS = {}
ENUMS = {}
for path in sorted((Path(__file__).parent / "schemas").glob("*.fbs")):
    source = re.sub(r"//[^\n]*", "", path.read_text())
    for name, base in re.findall(r"enum\s+(\w+)\s*:\s*(\w+)", source):
        ENUMS[name] = base
    for kind, name, body in re.findall(
        r"(table|struct)\s+(\w+)\s*\{(.*?)\}", source, re.S
    ):
        fields = []
        for field, typ, default in re.findall(
            r"(\w+)\s*:\s*([\w.]+|\[[\w.]+\])\s*(?:=\s*(\w+))?\s*(?:\([^)]*\))?\s*;",
            body,
        ):
            typ = typ.replace("Typing.", "")
            default = (
                None
                if default == "null"
                else int(default, 0) if default and default not in ("None",) else 0
            )
            fields.append((field, typ, default))
        SCHEMAS[name] = (kind, fields)


def scalar(t):
    return SCALARS[ENUMS.get(t, t)]


def layout(name):
    offset, align, entries = 0, 1, []
    for field, t, _ in SCHEMAS[name][1]:
        _, size = scalar(t)
        offset = (offset + size - 1) // size * size
        entries.append((field, t, offset))
        offset += size
        align = max(align, size)
    return (offset + align - 1) // align * align, align, entries


def unpack(buf, pos, t):
    return struct.unpack_from("<" + scalar(t)[0], buf, pos)[0]


def indirect(buf, pos):
    return pos + unpack(buf, pos, "uint")


def read_struct(buf, pos, name):
    return {field: unpack(buf, pos + off, t) for field, t, off in layout(name)[2]}


def read_table(buf, pos, name):
    vt = pos - struct.unpack_from("<i", buf, pos)[0]
    vsize = unpack(buf, vt, "ushort")
    fields = SCHEMAS[name][1]
    if (vsize - 4) // 2 > len(fields):
        raise ValueError(
            f"{name} contains newer unknown fields; editing is blocked to avoid loss"
        )
    result = {}
    for i, (field, t, default) in enumerate(fields):
        off = unpack(buf, vt + 4 + i * 2, "ushort") if 4 + i * 2 < vsize else 0
        if not off:
            result[field] = (
                []
                if t.startswith("[")
                else None if t == "string" or t in SCHEMAS else default
            )
            continue
        p = pos + off
        if t == "string":
            p = indirect(buf, p)
            n = unpack(buf, p, "uint")
            result[field] = bytes(buf[p + 4 : p + 4 + n]).decode("utf-8")
        elif t.startswith("["):
            base = t[1:-1]
            p = indirect(buf, p)
            n = unpack(buf, p, "uint")
            p += 4
            if base == "ubyte":
                result[field] = bytes(buf[p : p + n])
            elif base in SCHEMAS and SCHEMAS[base][0] == "struct":
                size = layout(base)[0]
                result[field] = [read_struct(buf, p + j * size, base) for j in range(n)]
            elif base == "string" or base in SCHEMAS:
                vals = []
                for j in range(n):
                    q = indirect(buf, p + j * 4)
                    if base == "string":
                        size = unpack(buf, q, "uint")
                        vals.append(bytes(buf[q + 4 : q + 4 + size]).decode("utf-8"))
                    else:
                        vals.append(read_table(buf, q, base))
                result[field] = vals
            else:
                size = scalar(base)[1]
                values = array.array(scalar(base)[0])
                values.frombytes(bytes(buf[p : p + n * size]))
                if sys.byteorder != "little":
                    values.byteswap()
                result[field] = values
        elif t in SCHEMAS:
            result[field] = read_table(buf, indirect(buf, p), t)
        else:
            result[field] = unpack(buf, p, t)
    return result


def decode(buf, name):
    return read_table(buf, unpack(buf, 0, "uint"), name)


def pack_struct(obj, name):
    size, _, entries = layout(name)
    buf = bytearray(size)
    for field, t, off in entries:
        struct.pack_into("<" + scalar(t)[0], buf, off, obj.get(field, 0))
    return bytes(buf)


def prepend(b, t, v):
    names = {
        "B": "Uint8",
        "H": "Uint16",
        "I": "Uint32",
        "h": "Int16",
        "i": "Int32",
        "f": "Float32",
    }
    getattr(b, "Prepend" + names[scalar(t)[0]])(v)


def write_table(b, obj, name):
    fields = SCHEMAS[name][1]
    offsets = {}
    for i, (field, t, _) in enumerate(fields):
        value = obj.get(field)
        if value is None:
            continue
        if t == "string":
            offsets[i] = b.CreateString(value)
        elif t.startswith("[") and len(value):
            base = t[1:-1]
            if base == "ubyte":
                offsets[i] = b.CreateByteVector(bytes(value))
            elif base in SCHEMAS and SCHEMAS[base][0] == "struct":
                size, align, _ = layout(base)
                raw = b"".join(pack_struct(v, base) for v in value)
                b.StartVector(size, len(value), align)
                b.head -= len(raw)
                b.Bytes[b.head : b.head + len(raw)] = raw
                offsets[i] = b.EndVector()
            elif base == "string" or base in SCHEMAS:
                vs = [
                    b.CreateString(v) if base == "string" else write_table(b, v, base)
                    for v in value
                ]
                b.StartVector(4, len(vs), 4)
                for v in reversed(vs):
                    b.PrependUOffsetTRelative(v)
                offsets[i] = b.EndVector()
            else:
                size = scalar(base)[1]
                b.StartVector(size, len(value), size)
                if isinstance(value, array.array):
                    raw = value.tobytes()
                    b.head -= len(raw)
                    b.Bytes[b.head : b.head + len(raw)] = raw
                else:
                    for v in reversed(value):
                        prepend(b, base, v)
                offsets[i] = b.EndVector()
        elif t in SCHEMAS:
            offsets[i] = write_table(b, value, t)
    b.StartObject(len(fields))
    for i, (field, t, default) in enumerate(fields):
        v = obj.get(field, default)
        if i in offsets:
            b.PrependUOffsetTRelativeSlot(i, offsets[i], 0)
        elif (
            t not in SCHEMAS
            and t != "string"
            and not t.startswith("[")
            and v is not None
        ):
            if v != default or default is None:
                prepend(b, t, v)
                b.Slot(i)
    return b.EndObject()


def encode(obj, name, prefixed=True):
    b = flatbuffers.Builder(1024)
    root = write_table(b, obj, name)
    b.FinishSizePrefixed(root) if prefixed else b.Finish(root)
    return bytes(b.Output())
