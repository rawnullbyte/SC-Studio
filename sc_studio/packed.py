"""SC6 external matrix and compressed timeline decoding, based on SupercellFlash."""

import struct
from .flatcodec import decode
import zstandard


def signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def bits(value, count, shift):
    return signed(value >> shift, count)


def external_banks(data):
    size = struct.unpack_from("<I", data)[0]
    desc = decode(data[4 : 4 + size], "ExternalMatrixBanks")
    result = {}
    for bank in desc["banks"]:
        start = 4 + size + bank["compressed_data_offset"]
        raw = zstandard.ZstdDecompressor().decompress(
            data[start : start + bank["compressed_data_size"]],
            max_output_size=bank["decompressed_data_size"],
        )
        nf, ns, nblocks = (
            bank["float_matrix_count"],
            bank["short_matrix_count"],
            bank["compressed_matrix_data_size"],
        )
        # Serialized banks are padded to blocks of sixteen; 65536 entries is valid
        # even though placement index 65535 denotes identity.
        count = max(nf + ns, nblocks * 16)
        if count > 1048576:
            raise ValueError("Matrix bank exceeds preview decoder limit")
        matrices = [None] * count
        for i in range(nf):
            matrices[i] = list(struct.unpack_from("<6f", raw, i * 24))
        start = nf * 24 + nblocks * 4
        shorts = struct.unpack_from(
            "<" + "H" * bank["short_matrix_data_size"], raw, start
        )
        for i in range(ns):
            values = shorts[i * 6 : i * 6 + 6]
            matrices[nf + i] = [
                signed(v, 16) / (1024 if j < 4 else 20) for j, v in enumerate(values)
            ]
        for i in range(nf + ns, count, 16):
            encoded = struct.unpack_from("<i", raw, nf * 24 + (i >> 4) * 4)[0]
            offset = encoded >> 13
            base = encoded & 8191
            values = list(shorts[base * 6 : base * 6 + 6])
            for j in range(min(16, count - i)):
                flag = shorts[offset]
                if flag & 3:
                    lo, hi = shorts[offset + 1 : offset + 3]
                    packed = flag | (lo << 16) | (hi << 32)
                    mode = flag & 15
                    if mode == 1:
                        changes = [(4, 14, 4), (5, 14, 18)]
                        step = 2
                    elif mode == 2:
                        changes = [(0, 7, 4), (3, 7, 11), (4, 7, 18), (5, 7, 25)]
                        step = 2
                    elif mode == 3:
                        changes = [(0, 11, 4), (3, 11, 15), (4, 11, 26), (5, 11, 37)]
                        step = 3
                    elif mode == 5:
                        changes = [
                            (0, 7, 4),
                            (1, 7, 11),
                            (2, 7, 18),
                            (3, 7, 25),
                            (4, 8, 32),
                            (5, 8, 40),
                        ]
                        step = 3
                    elif mode == 6:
                        changes = [(0, 10, 4), (1, 10, 14), (2, 10, 24), (3, 10, 34)]
                        step = 4
                        tr = lo | (hi << 16) | (shorts[offset + 3] << 32)
                        values[4] += bits(tr, 10, 28)
                        values[5] += bits(tr, 10, 38)
                    elif mode == 7:
                        changes = [(0, 12, 4), (1, 12, 16), (2, 12, 28)]
                        step = 5
                        tr = (
                            hi | (shorts[offset + 3] << 16) | (shorts[offset + 4] << 32)
                        )
                        values[3] += bits(tr, 12, 8)
                        values[4] += bits(tr, 14, 20)
                        values[5] += bits(tr, 14, 34)
                    elif mode == 15:
                        changes = []
                        step = 7
                        for k in range(6):
                            values[k] += shorts[offset + 1 + k]
                    else:
                        raise ValueError(f"Unknown SC6 matrix packing {mode}")
                    for k, n, s in changes:
                        values[k] += bits(packed, n, s)
                else:
                    values[4] += bits(flag, 7, 2)
                    values[5] += bits(flag, 7, 9)
                    step = 1
                matrices[i + j] = [
                    signed(v, 16) / (1024 if k < 4 else 20)
                    for k, v in enumerate(values)
                ]
                offset += step
        color_start = nf * 24 + nblocks * 4 + bank["short_matrix_data_size"] * 2
        colors = [
            list(raw[color_start + i * 7 : color_start + (i + 1) * 7])
            for i in range(bank["color_transform_count"])
        ]
        clip = raw[
            bank["clip_data_offset"] : bank["clip_data_offset"] + bank["clip_data_size"]
        ]
        result[bank["index"]] = {"matrices": matrices, "colors": colors, "clips": clip}
    return result


def compressed_frame(raw, offset, frame):
    pointer = offset + frame * 8 + 8
    rel, start, end = struct.unpack_from("<iHH", raw, pointer)
    base = offset + rel
    src = base + start * 2
    stop = base + end * 2

    def word(p):
        return struct.unpack_from("<H", raw, p)[0]

    if src == base:
        return [list(struct.unpack_from("<3H", raw, p)) for p in range(src, stop, 6)]
    result = []
    mask = 0
    while src < stop or mask:
        if len(result) > 100000:
            raise ValueError("Compressed frame too large")
        old = list(struct.unpack_from("<3H", raw, base))
        if mask & 1:
            result.append(old)
            base += 6
            mask >>= 1
            continue
        if src >= stop:
            raise ValueError("Invalid compressed frame mask")
        mask >>= 1
        flag = word(src)
        if flag & 3:
            mode = flag & 7
            if mode == 1:
                old[1] += bits(flag, 13, 3)
                src += 2
                base += 6
                result.append(old)
            elif mode == 2:
                old[1] += bits(flag, 4, 3)
                old[2] += bits(flag, 9, 7)
                src += 2
                base += 6
                result.append(old)
            elif mode == 3:
                old[1] += word(src + 2)
                old[2] += bits(flag, 13, 3)
                src += 4
                base += 6
                result.append(old)
            elif mode == 5:
                result.append(old)
                base += 6
                src += 2
                mask = flag >> 3
            elif mode == 6:
                base += 6 * bits(flag, 13, 3)
                src += 2
            elif mode == 7:
                result.append([bits(flag, 12, 3), word(src + 2), word(src + 4)])
                src += 6
                base += 6 if flag & 32768 else 0
            else:
                raise ValueError("Invalid compressed frame encoding")
        else:
            old[1] += bits(flag, 7, 2)
            result.append(old)
            base += 6
            old = list(struct.unpack_from("<3H", raw, base))
            old[1] += bits(flag, 7, 9)
            result.append(old)
            base += 6
            src += 2
            mask >>= 1
    return [[v & 65535 for v in e] for e in result]
