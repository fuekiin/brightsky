import datetime

import numpy as np
import pytest

from brightsky.radar3d.clouds import (
    CloudModel,
    flow_block,
    parse_flow_block,
    quantise,
    warp,
)
from brightsky.radar3d.grid import Crop, Grid
from brightsky.radar3d.icon import StepFields


T0 = datetime.datetime(2026, 9, 16, 11, tzinfo=datetime.UTC)
T1 = T0 + datetime.timedelta(hours=1)


def test_warp_shifts_by_whole_cells():
    vol = np.zeros((2, 6, 7), np.float32)
    vol[1, 2, 3] = 5.0
    out = warp(vol, 2, -1)           # two cells east, one cell north
    assert out[1, 1, 5] == 5.0 and out[1].sum() == 5.0 and out[0].sum() == 0
    half = warp(vol, 0.5, 0)
    assert half[1, 2, 3] == pytest.approx(2.5) and half[1, 2, 4] == \
        pytest.approx(2.5)


def test_quantise_thresholds():
    lwc = np.array([[[0.019, 0.03, 0.26, 0.7]]], np.float32)
    cov = np.array([[[29.0, 30.0, 88.0, 100.0]]], np.float32)
    rg = quantise(lwc, cov)
    assert rg.shape == (1, 1, 4, 2)
    assert rg[0, 0, :, 0].tolist() == [0, 5, 25, 70]
    assert rg[0, 0, :, 1].tolist() == [0, 62, 250, 250]


@pytest.fixture
def model():
    grid = Grid(50.0, 50.2, 8.0, 8.4, 20, 12)      # ~2 km cells
    m = CloudModel(grid)
    L, H, W = grid.shape

    def step(t, col, fu):
        lwc = np.zeros((L, H, W), np.float32)
        lwc[5, 6, col] = 1.0                              # a 1 g/m³ blob
        cov = np.full((L, H, W), 80.0, np.float32)
        return StepFields(t, lwc.astype(np.float16), cov.astype(np.float16),
                          np.full((H, W), fu, np.float32),
                          np.zeros((H, W), np.float32))
    # wind of one cell east per 5 minutes: 12 cells per hour
    fu = m.cell_x / 300.0
    m.add_run([step(T0, 4, fu), step(T1, 16, fu)])
    return m


def test_frame_at_moves_the_blob_with_the_wind(model):
    assert model.covers(T0 + datetime.timedelta(minutes=30))
    assert not model.covers(T1 + datetime.timedelta(minutes=5))
    rg, flow = model.frame_at(T0 + datetime.timedelta(minutes=30))
    assert rg.shape == (24, 12, 20, 2) and flow.shape == (12, 20, 2)
    assert flow[0, 0].tolist() == pytest.approx([1.0, 0.0], abs=1e-3)
    water = rg[5, :, :, 0]
    row, col = np.unravel_index(water.argmax(), water.shape)
    assert (row, col) == (6, 10)                     # six cells along
    assert water[row, col] == 100                    # 1 g/m³ → 100 counts
    assert rg[5, 6, 10, 1] == 188          # 80 % → 75 % step → 75/0.4
    rg0, _ = model.frame_at(T0)
    assert rg0[5, 6, 4, 0] == 100
    with pytest.raises(LookupError):
        model.frame_at(T1 + datetime.timedelta(hours=2))


def test_flow_block_layout():
    flow = np.zeros((40, 30, 2), np.float32)
    flow[..., 0] = 1.5
    flow[..., 1] = -0.25
    data = flow_block(flow, Crop(2, 20, 3, 13))       # 18 rows × 10 cols
    fw, fh, vectors = parse_flow_block(data)
    assert (fw, fh) == (3, 5)                         # ceil(10/4), ceil(18/4)
    assert len(data) == 4 + 3 * 5 * 2 * 2
    assert np.allclose(vectors[..., 0], 1.5)
    assert np.allclose(vectors[..., 1], -0.25)


def test_forecast_at_exact_step(model):
    with pytest.raises(LookupError):
        model.frame_at  # model fixture steps carry no pwc yet
        model.forecast_at(T0)
    L, H, W = model.grid.shape
    step = model.steps[T0]
    pwc = np.zeros((L, H, W), np.float32)
    pwc[3, 2, 2] = 1.0
    step.pwc = pwc.astype(np.float16)
    rain, rg, flow = model.forecast_at(T0)
    assert rain.shape == (L, H, W) and rain[3, 2, 2] == 152    # ≈ 44 dBZ
    assert (rain > 0).sum() == 1
    assert rg.shape == (L, H, W, 2) and rg[5, 6, 4, 0] == 100
    assert flow[0, 0].tolist() == pytest.approx([1.0, 0.0], abs=1e-3)
    with pytest.raises(LookupError):
        model.forecast_at(T0 + datetime.timedelta(minutes=30))
