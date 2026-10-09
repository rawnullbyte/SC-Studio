import struct
import zstandard
from sc_studio.flatcodec import encode
from sc_studio.packed import external_banks, compressed_frame


def test_short_and_delta_external_matrices():
    # One base block of 16 short matrices followed by a block of translation deltas.
    # Flags mode 0: signed seven-bit tx/ty deltas, accumulated against the base.
    descriptors = struct.pack("<2i", 0, (96 << 13))
    bases = struct.pack("<6H", 1024, 0, 0, 1024, 0, 0) * 16
    delta = struct.pack("<16H", *([4] * 16))
    raw = descriptors + bases + delta
    compressed = zstandard.ZstdCompressor().compress(raw)
    b = {
        "index": 0,
        "compressed_data_offset": 0,
        "compressed_data_size": len(compressed),
        "decompressed_data_size": len(raw),
        "compressed_matrix_data_size": 2,
        "short_matrix_data_size": 112,
        "short_matrix_count": 16,
        "float_matrix_count": 0,
        "color_transform_count": 0,
        "clip_data_offset": len(raw),
        "clip_data_size": 0,
    }
    bank = external_banks(encode({"banks": [b]}, "ExternalMatrixBanks") + compressed)[0]
    assert bank["matrices"][0] == [1, 0, 0, 1, 0, 0]
    assert bank["matrices"][16] == [1, 0, 0, 1, 0.05, 0]
    assert bank["matrices"][31] == [1, 0, 0, 1, 0.8, 0]


def test_uncompressed_and_delta_timeline_frames():
    raw = (
        b"\0" * 8 + struct.pack("<iHH", 16, 0, 6) + struct.pack("<6H", 2, 3, 4, 5, 6, 7)
    )
    assert compressed_frame(raw, 0, 0) == [[2, 3, 4], [5, 6, 7]]
    # Delta stream begins after base placement, mode 1 adds +2 to matrix index.
    raw = (
        b"\0" * 8
        + struct.pack("<iHH", 16, 3, 4)
        + struct.pack("<4H", 2, 3, 4, (2 << 3) | 1)
    )
    assert compressed_frame(raw, 0, 0) == [[2, 5, 4]]


def test_full_padded_short_bank_65536_entries():
    count = 65536
    descriptors = bytes((count // 16) * 4)
    bases = struct.pack("<6H", 1024, 0, 0, 1024, 20, 40) * count
    raw = descriptors + bases
    compressed = zstandard.ZstdCompressor().compress(raw)
    b = {
        "index": 0,
        "compressed_data_offset": 0,
        "compressed_data_size": len(compressed),
        "decompressed_data_size": len(raw),
        "compressed_matrix_data_size": count // 16,
        "short_matrix_data_size": count * 6,
        "short_matrix_count": count,
        "float_matrix_count": 0,
        "color_transform_count": 0,
        "clip_data_offset": len(raw),
        "clip_data_size": 0,
    }
    bank = external_banks(encode({"banks": [b]}, "ExternalMatrixBanks") + compressed)[0]
    assert len(bank["matrices"]) == count
    assert bank["matrices"][65534] == [1, 0, 0, 1, 1, 2]
