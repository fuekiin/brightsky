import math
import zlib

import numpy as np
import pytest

from brightsky.radar3d.grid import Grid
from brightsky.radar3d.rain import SiteGeometry, grid_rain
from brightsky.radar3d.sweeps import (
    parse_sweep_name,
    read_site_meta,
    read_tilt,
)


@pytest.fixture(scope='module')
def isn_cycle(data_dir):
    paths = sorted((data_dir / 'radar3d').glob('*-hd5'))
    infos = [parse_sweep_name(p.name) for p in paths]
    assert len(paths) == 10 and {i.site for i in infos} == {'isn'}
    lowest = min(zip(infos, paths), key=lambda x: x[0].tilt)[1]
    meta = read_site_meta(lowest, 'isn')
    tilts = [read_tilt(p) for p in paths]
    return meta, tilts


@pytest.fixture(scope='module')
def isen_grid(isn_cycle):
    # Exactly how the reference script builds its grid around the centre site
    meta, _ = isn_cycle
    dlat = 150.0 / 111.195
    dlon = 150.0 / (111.195 * math.cos(math.radians(meta.lat)))
    return Grid(meta.lat - dlat, meta.lat + dlat,
                meta.lon - dlon, meta.lon + dlon, 300, 300)


def test_site_geometry_covers_range_cone(isen_grid, isn_cycle):
    meta, _ = isn_cycle
    geom = SiteGeometry(isen_grid, meta)
    assert geom.shape == (24, 300, 300)
    assert 1_000_000 < geom.n < 2_400_000
    assert geom.nbytes < 40 * 1024 * 1024
    # Every stored ray/gate is addressable in the lowest tilt
    assert geom.ray.max() < meta.nrays and geom.gate.max() < meta.nbins


def test_matches_reference_gridder(isen_grid, isn_cycle, data_dir):
    meta, tilts = isn_cycle
    golden = np.frombuffer(
        zlib.decompress(
            (data_dir / 'radar3d' / 'golden_isn_1150.u8.zlib').read_bytes()),
        np.uint8).reshape(24, 300, 300)
    vol = grid_rain(isen_grid, [(SiteGeometry(isen_grid, meta), tilts)])
    assert vol.shape == golden.shape and vol.dtype == np.uint8
    # Byte-exact: the port reproduces the reference gridder's arithmetic
    assert np.array_equal(vol, golden)
    assert (vol > 0).sum() > 50_000
    assert vol.max() / 2.0 - 32.0 == pytest.approx(62.0)


def test_sites_merge_by_maximum(isen_grid, isn_cycle):
    meta, tilts = isn_cycle
    geom = SiteGeometry(isen_grid, meta)
    a = grid_rain(isen_grid, [(geom, tilts)])
    weaker = [(el, dbz - 10.0) for el, dbz in tilts]
    b = grid_rain(isen_grid, [(geom, tilts), (geom, weaker)])
    assert np.array_equal(a, b)


def test_skips_sites_with_fewer_than_two_tilts(isen_grid, isn_cycle):
    meta, tilts = isn_cycle
    geom = SiteGeometry(isen_grid, meta)
    assert not grid_rain(isen_grid, [(geom, tilts[:1])]).any()


def test_gather_tolerates_361_ray_sweeps(isen_grid, isn_cycle):
    import dataclasses
    meta, tilts = isn_cycle
    # Geometry laid out for a 361-ray site file, sampling 360-ray tilts
    geom = SiteGeometry(isen_grid, dataclasses.replace(meta, nrays=361))
    assert geom.ray.max() == 360
    vol = grid_rain(isen_grid, [(geom, tilts)])
    assert (vol > 0).sum() > 50_000
    # ...and a 360-ray geometry sampling a tilt padded to 361 rays
    padded = [(el, np.vstack([dbz, dbz[:1]])) for el, dbz in tilts]
    ref = grid_rain(isen_grid, [(SiteGeometry(isen_grid, meta), tilts)])
    assert np.array_equal(
        grid_rain(isen_grid, [(SiteGeometry(isen_grid, meta), padded)]), ref)


def test_failing_site_is_skipped(isen_grid, isn_cycle):
    meta, tilts = isn_cycle
    geom = SiteGeometry(isen_grid, meta)
    broken = [(el, dbz[:, :0]) for el, dbz in tilts]     # zero gates
    ref = grid_rain(isen_grid, [(geom, tilts)])
    assert np.array_equal(
        grid_rain(isen_grid, [(geom, broken), (geom, tilts)]), ref)
