import datetime

import numpy as np
import pytest

from brightsky.radar3d.sweeps import (
    SiteMeta,
    parse_sweep_name,
    read_site_meta,
    read_tilt,
)


NAME = 'ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn-10873-hd5'
LOWEST = NAME.replace('_09-2026091611540200', '_00-2026091611505700')


def test_parse_sweep_name():
    info = parse_sweep_name(NAME)
    assert info.site == 'isn' and info.tilt == 9
    assert info.timestamp == datetime.datetime(
        2026, 9, 16, 11, 54, 2, tzinfo=datetime.UTC)
    assert info.cycle == datetime.datetime(
        2026, 9, 16, 11, 50, tzinfo=datetime.UTC)
    assert parse_sweep_name('Beschreibung.pdf') is None
    assert parse_sweep_name('../') is None


@pytest.fixture
def sweep_dir(data_dir):
    return data_dir / 'radar3d'


def test_read_site_meta(sweep_dir):
    meta = read_site_meta(sweep_dir / LOWEST, 'isn')
    assert meta == SiteMeta(
        site='isn', lat=48.174705, lon=12.101779, height=677.77,
        nbins=720, nrays=360, rscale=250.0, rstart=0.0)


def test_read_tilt_decodes_dbz(sweep_dir):
    elangle, dbz = read_tilt(sweep_dir / NAME)
    assert abs(elangle - 25.0) < 0.01
    assert dbz.shape == (360, 240) and dbz.dtype == np.float32
    assert np.isnan(dbz).any()
    finite = dbz[~np.isnan(dbz)]
    assert finite.size > 0 and finite.min() >= 0.0 and finite.max() < 80
    _, lowest = read_tilt(sweep_dir / LOWEST)
    assert lowest.shape == (360, 720)
