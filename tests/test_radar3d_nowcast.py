import numpy as np
import pytest

from brightsky.radar3d import nowcast


def blob(height, width, cy, cx, r=6, value=150):
    yy, xx = np.mgrid[0:height, 0:width]
    field = np.zeros((height, width), np.uint8)
    field[(yy - cy) ** 2 + (xx - cx) ** 2 <= r * r] = value
    return field


def test_bytes_z_roundtrip_and_floor():
    v = np.array([[[0, 64, 104, 152, 255]]], np.uint8)
    z = nowcast.bytes_to_z(v)
    assert z[0, 0, 0] == 0 and z[0, 0, 1] == pytest.approx(1.0)      # 0 dBZ
    assert z[0, 0, 3] == pytest.approx(10 ** 4.4, rel=1e-4)            # 44 dBZ
    back = nowcast.z_to_bytes(z)
    assert back.tolist() == v.tolist()
    assert nowcast.z_to_bytes(np.array([0.5, 0.0]))[0] == 0    # below 0 dBZ


def test_estimate_motion_recovers_a_shift_with_subcell_precision():
    prev = blob(96, 96, 48, 40)
    cur = blob(96, 96, 50, 43)                 # moved 3 east, 2 south
    u, v, known = nowcast.estimate_motion(prev, cur)
    assert known[6, 5]                         # the tile holding the blob
    assert u[6, 5] == pytest.approx(3.0, abs=0.35)
    assert v[6, 5] == pytest.approx(2.0, abs=0.35)
    assert not known[0, 0] and u[0, 0] == 0 and v[0, 0] == 0
    # a half-cell shift is refined below one cell
    prev = blob(96, 96, 48, 40, r=10)
    cur = np.maximum(blob(96, 96, 48, 42, r=10), blob(96, 96, 48, 43, r=10))
    u, v, known = nowcast.estimate_motion(prev, cur)
    assert 2.0 < u[6, 5] < 3.0


def test_fill_smooth_upsample_and_motion_field():
    u = np.zeros((4, 4), np.float32)
    v = np.zeros((4, 4), np.float32)
    known = np.zeros((4, 4), bool)
    known[1, 1] = True
    u[1, 1] = 3.0
    model_u = np.full((32, 32), 1.0, np.float32)
    model_v = np.full((32, 32), -0.5, np.float32)
    fu, fv = nowcast.fill_and_smooth(u, v, known, model_u, model_v)
    assert fu[3, 3] == pytest.approx(1.0) and fv[3, 3] == pytest.approx(-0.5)
    assert 1.0 < fu[1, 1] < 3.0                # smoothed toward the model
    full = nowcast.upsample(fu, 8, 32, 32)
    assert full.shape == (32, 32)
    prev = blob(64, 64, 32, 20)
    cur = blob(64, 64, 32, 24)
    U, V, known = nowcast.motion_field(prev, cur, model_u=np.zeros((64, 64)),
                                       model_v=np.zeros((64, 64)))
    assert U.shape == (64, 64) and U[32, 22] == pytest.approx(4.0, abs=0.6)
    U2, V2, k2 = nowcast.motion_field(None, cur, np.ones((64, 64)),
                                      np.zeros((64, 64)))
    assert k2 is None and U2[0, 0] == 1.0


def test_advect_and_blend():
    z = np.zeros((2, 20, 20), np.float32)
    z[1, 10, 5] = 1000.0
    u = np.full((20, 20), 1.0, np.float32)
    v = np.zeros((20, 20), np.float32)
    moved = nowcast.advect_z(z, u, v, steps=3)
    assert moved[1, 10, 8] == pytest.approx(1000.0) and moved[1].sum() == \
        pytest.approx(1000.0)
    model = np.zeros_like(z)
    model[1, 2, 2] = 400.0
    assert nowcast.blend_weight(5) == pytest.approx(1 / 144)
    assert nowcast.blend_weight(30) == pytest.approx(0.25)
    assert nowcast.blend_weight(60) == 1.0 and nowcast.blend_weight(90) == 1.0
    b = nowcast.blend_z(moved, model, nowcast.blend_weight(30))
    assert b[1, 10, 8] == pytest.approx(750.0)
    assert b[1, 2, 2] == pytest.approx(100.0)
    assert np.array_equal(nowcast.blend_z(moved, model, 1.0), model)
    pooled = nowcast.maxpool2(np.arange(16, dtype=np.uint8).reshape(1, 4, 4))
    assert pooled[0].tolist() == [[5, 7], [13, 15]]
