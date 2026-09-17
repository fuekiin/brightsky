import datetime

import numpy as np
import pytest

from brightsky.radar3d.grid import GERMANY_2KM, Grid
from brightsky.radar3d.icon import (
    Sampler,
    choose_levels,
    column_flow,
    grib_name,
    read_grib,
    to_slabs,
    wind_levels,
)


RUN = datetime.datetime(2026, 9, 16, 9, tzinfo=datetime.UTC)


def test_grib_name():
    assert grib_name(RUN, 2, 40, 'qc') == (
        'icon-d2_germany_regular-lat-lon_model-level_2026091609_002_40_qc'
        '.grib2.bz2')
    assert grib_name(RUN, 0, 66, 'hhl') == (
        'icon-d2_germany_regular-lat-lon_time-invariant_2026091609_000_66_hhl'
        '.grib2.bz2')


def test_read_grib_and_sample(data_dir):
    path = data_dir / 'radar3d' / grib_name(RUN, 2, 40, 'qc')
    values, lat0, lon0, dlat, dlon = read_grib(path)
    assert values.shape == (746, 1215) and values.dtype == np.float32
    assert (lat0, dlat, dlon) == (43.18, 0.02, 0.02)
    assert lon0 == pytest.approx(-3.94)
    assert np.isnan(values).any() and np.nanmax(values) > 0
    field = Sampler(GERMANY_2KM).sample(values, lat0, lon0, dlat, dlon)
    assert field.shape == (456, 349)
    assert np.nanmax(field) <= np.nanmax(values)


def test_sampler_is_bilinear():
    grid = Grid(50.0, 51.0, 8.0, 9.0, 4, 4)
    lat0, lon0, d = 49.0, 7.0, 0.5
    field = np.fromfunction(
        lambda j, i: 2.0 * j + 3.0 * i, (8, 8), dtype=float)
    out = Sampler(grid).sample(field, lat0, lon0, d, d)
    expect = (2.0 * (grid.lat_rows() - lat0) / d)[:, None] \
        + (3.0 * (grid.lon_cols() - lon0) / d)[None, :]
    assert np.allclose(out, expect)


def test_to_slabs_matches_np_interp_per_column():
    rng = np.random.default_rng(1)
    K, H, W = 12, 5, 6
    # top first, like the model levels
    heights = np.sort(rng.uniform(0, 13000, (K, H, W)), axis=0)[::-1]
    field = rng.uniform(0, 1, (K, H, W)).astype(np.float32)
    field[rng.uniform(size=field.shape) < 0.15] = np.nan            # holes
    heights[0, 0, 0] = np.nan
    slab_h = np.arange(24) * 500.0 + 250.0
    out = to_slabs(field, heights, slab_h)
    for r in range(H):
        for c in range(W):
            h, v = heights[::-1, r, c], field[::-1, r, c]
            ok = ~np.isnan(h) & ~np.isnan(v)
            ref = np.interp(slab_h, h[ok], v[ok], left=0.0, right=0.0)
            assert np.allclose(out[:, r, c], ref, atol=1e-5), (r, c)


def test_choose_and_wind_levels():
    # 10 levels whose heights fall 2 km per level from 20 km, flat terrain
    steps = (20000 - 2000 * np.arange(10))[:, None, None]
    full_h = np.zeros((10, 2, 2)) + steps
    assert choose_levels(full_h, top_m=12500) == [4, 5, 6, 7, 8, 9, 10]
    assert wind_levels(full_h, 1000, 10000, stride=2) == [6, 8]


def test_column_flow_weights_by_water():
    slab_h = np.arange(24) * 500.0 + 250.0
    lwc = np.zeros((24, 1, 1), np.float32)
    cov = np.zeros((24, 1, 1), np.float32)
    u = np.arange(24, dtype=np.float32)[:, None, None]
    v = -u
    fu, fv = column_flow(lwc, cov, u, v, slab_h)
    mid = (slab_h >= 3000) & (slab_h <= 8000)
    assert fu[0, 0] == pytest.approx(u[mid].mean()) and fv[0, 0] == -fu[0, 0]
    lwc[20] = 1.0
    fu, fv = column_flow(lwc, cov, u, v, slab_h)
    assert fu[0, 0] == pytest.approx(20.0, abs=1e-3)
