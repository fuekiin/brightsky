import math
import struct
import zlib

import numpy as np
import pytest

from brightsky.radar3d import frame
from brightsky.radar3d.grid import GERMANY_1KM, Crop, Grid, OutsideGrid


def test_germany_grid_shape():
    assert GERMANY_1KM.shape == (24, 912, 698)
    lat = GERMANY_1KM.lat_rows()
    lon = GERMANY_1KM.lon_cols()
    assert lat[0] > lat[-1]                      # row 0 is north
    assert 55.2 > lat[0] > 55.18
    assert lon[0] > 5.5 and lon[-1] < 15.5
    assert np.all(np.diff(lon) > 0)
    assert GERMANY_1KM.level_heights()[0] == 250.0
    assert GERMANY_1KM.level_heights()[-1] == 11750.0


def test_crop_snaps_outward_and_bounds_roundtrip():
    g = GERMANY_1KM
    crop = g.crop(52.4, 52.6, 13.3, 13.5)
    b = g.bounds(crop)
    assert b[0] <= 52.4 and b[1] >= 52.6
    assert b[2] <= 13.3 and b[3] >= 13.5
    # Re-snapping the (6-decimal rounded) bounds is a no-op
    rounded = tuple(round(x, 6) for x in b)
    assert g.crop(*rounded) == crop


def test_crop_aligned_to_two_cells():
    crop = GERMANY_1KM.crop(52.4, 52.6, 13.3, 13.5, align=2)
    assert crop.row0 % 2 == 0 and crop.col0 % 2 == 0
    assert crop.width % 2 == 0 and crop.height % 2 == 0


def test_around_clips_to_grid_and_rejects_outside():
    g = GERMANY_1KM
    crop = g.around(52.52, 13.41, 100000)
    assert 195 <= crop.width <= 215 and 195 <= crop.height <= 215
    edge = g.around(47.1, 5.6, 100000)
    assert edge.col0 == 0 and edge.row1 == g.height
    with pytest.raises(OutsideGrid):
        g.around(40.0, 13.0, 1000)
    with pytest.raises(OutsideGrid):
        g.crop(40.0, 41.0, 13.0, 14.0)


def test_small_grid_matches_reference_construction():
    # The reference gridder's Isen fixture grid: 300 km box around isn
    clat, clon, half = 48.174705, 12.101779, 150.0
    dlat = half / 111.195
    dlon = half / (111.195 * math.cos(math.radians(clat)))
    g = Grid(clat - dlat, clat + dlat, clon - dlon, clon + dlon, 300, 300)
    assert g.crop(clat - dlat, clat + dlat, clon - dlon, clon + dlon) == \
        Crop(0, 300, 0, 300)
    assert abs(g.lat_rows()[150] - clat) < 0.02      # within two rows


def test_frame_roundtrip_and_header_layout():
    vox = np.zeros((24, 5, 7), np.uint8)
    vox[3, 1, 2] = 200
    data = frame.encode(vox, extra=b'FLOW')
    assert data[:6] == b'NANO3D'
    version, w, h, levels, ch, n, m = struct.unpack_from('<HHHHHII', data, 6)
    assert (version, w, h, levels, ch) == (1, 7, 5, 24, 1)
    assert data[24:32] == bytes(8)
    assert len(data) == 32 + n + m and m == 4
    assert zlib.decompress(data[32:32 + n]) == vox.tobytes()
    header, out, extra = frame.decode(data)
    assert header == {'version': 1, 'width': 7, 'height': 5, 'levels': 24,
                      'channels': 1}
    assert np.array_equal(out, vox) and extra == b'FLOW'
