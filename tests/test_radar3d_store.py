import datetime

import numpy as np
import pytest

from brightsky.radar3d.grid import Crop
from brightsky.radar3d.store import FrameMissing, FrameStore


TS = datetime.datetime(2026, 9, 16, 11, 50, tzinfo=datetime.UTC)


def test_write_open_crop(tmp_path):
    store = FrameStore(tmp_path)
    vol = np.zeros((24, 40, 30), np.uint8)
    vol[2, 10:20, 5:15] = 100
    vol[2, 11, 6] = 200
    path = store.write('rain', TS, vol)
    assert path == tmp_path / 'rain' / '20260916T1150Z.npy'
    assert not list(tmp_path.glob('**/*.tmp'))
    assert np.array_equal(store.open('rain', TS), vol)
    sub = store.crop('rain', TS, Crop(10, 20, 5, 15))
    assert sub.shape == (24, 10, 10) and sub[2].min() == 100
    pooled = store.crop('rain', TS, Crop(10, 20, 4, 16), scale=2)
    assert pooled.shape == (24, 5, 6)
    assert pooled[2, 0, 1] == 200                # max-pool keeps the peak
    assert store.timestamps('rain') == [TS]
    with pytest.raises(FrameMissing):
        store.open('rain', TS + datetime.timedelta(minutes=5))


def test_delete_before(tmp_path):
    store = FrameStore(tmp_path)
    vol = np.zeros((2, 2, 2), np.uint8)
    old = TS - datetime.timedelta(hours=4)
    store.write('rain', old, vol)
    store.write('rain', TS, vol)
    assert store.delete_before('rain', TS - datetime.timedelta(hours=3)) \
        == [old]
    assert store.timestamps('rain') == [TS]
