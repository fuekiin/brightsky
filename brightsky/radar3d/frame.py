"""
The `NANO3D` binary frame the app decodes: a 32-byte little-endian header,
a zlib block of level-major voxel bytes, and an optional extra block (the
clouds' flow field in phase 2).

    0   6s  magic 'NANO3D'
    6   u16 version (1)
    8   u16 width
    10  u16 height
    12  u16 levels
    14  u16 channels (bytes per voxel)
    16  u32 zlib payload length
    20  u32 extra block length
    24  8 bytes reserved (zero)
"""
import struct
import zlib

import numpy as np


MAGIC = b'NANO3D'
VERSION = 1
HEADER = struct.Struct('<6sHHHHHII8x')
HEADER_SIZE = HEADER.size
assert HEADER_SIZE == 32


def encode(voxels, channels=1, extra=b'', level=6):
    levels, height, width = voxels.shape[:3]
    raw = np.ascontiguousarray(voxels, dtype=np.uint8).tobytes()
    payload = zlib.compress(raw, level)
    header = HEADER.pack(
        MAGIC, VERSION, width, height, levels, channels,
        len(payload), len(extra))
    return header + payload + extra


def decode(data):
    magic, version, width, height, levels, channels, n, m = \
        HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("Not a NANO3D frame")
    raw = zlib.decompress(data[HEADER_SIZE:HEADER_SIZE + n])
    shape = (levels, height, width)
    if channels > 1:
        shape += (channels,)
    voxels = np.frombuffer(raw, np.uint8).reshape(shape)
    header = {
        'version': version, 'width': width, 'height': height,
        'levels': levels, 'channels': channels,
    }
    return header, voxels, data[HEADER_SIZE + n:HEADER_SIZE + n + m]
