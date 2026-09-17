"""
Cloud volumes on the rain frames' 5-minute timeline. `CloudModel` keeps the
loaded ICON-D2 runs (their hourly `StepFields`) and synthesises a frame for
any stamp by blending the bracketing steps *along the model wind*
(semi-Lagrangian: pull the earlier field forward, the later one back), so
the frames themselves move. Port of the app repo's grid_clouds2.py.

Voxel channels: R = condensed water g/m³ (v × 0.01), G = cloud cover %
(v × 0.4). Flow: cells per 5 minutes, x east, y south.
"""
import datetime
import struct

import numpy as np

from brightsky.radar3d.icon import cell_metres, hydrometeors_to_dbz_bytes


FRAME_SECONDS = 300.0
FLOW_FACTOR = 4          # flow field cells per frame cell, each axis


def warp(vol, dx, dy):
    """Shift a [L, H, W] volume by (dx, dy) cells (x east, y south): pull
    with bilinear taps from clipped source positions."""
    levels, height, width = vol.shape
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    sx = np.clip(xs - dx, 0, width - 1.001)
    sy = np.clip(ys - dy, 0, height - 1.001)
    x0 = np.floor(sx).astype(int)
    y0 = np.floor(sy).astype(int)
    tx = (sx - x0).astype(np.float32)
    ty = (sy - y0).astype(np.float32)
    out = np.empty(vol.shape, np.float32)
    for k in range(levels):
        p = vol[k]
        out[k] = (
            (p[y0, x0] * (1 - tx) + p[y0, x0 + 1] * tx) * (1 - ty)
            + (p[y0 + 1, x0] * (1 - tx) + p[y0 + 1, x0 + 1] * tx) * ty
        )
    return out


def quantise(lwc, cov):
    """Water in 0.05 g/m³ steps below 0.5 and 0.1 above (nothing below
    0.02); cover in 25 % steps, nothing below 30 %. → uint8 [L, H, W, 2]."""
    lq = np.where(lwc < 0.5, np.round(lwc / 0.05) * 0.05,
                  np.round(lwc / 0.1) * 0.1)
    lq[lwc < 0.02] = 0
    r = np.clip(np.round(lq / 0.01), 0, 255).astype(np.uint8)
    cq = np.round(cov / 25.0) * 25.0
    cq[cov < 30] = 0
    g = np.clip(np.round(cq / 0.4), 0, 250).astype(np.uint8)
    return np.stack([r, g], axis=-1)


class CloudModel:

    def __init__(self, grid):
        self.grid = grid
        self.cell_x, self.cell_y = cell_metres(grid)
        self.steps = {}          # valid time → StepFields (newest run wins)

    def add_run(self, steps):
        """`steps`: iterable of StepFields; later runs override earlier ones
        at the same valid time."""
        for step in steps:
            self.steps[step.valid_time] = step

    def drop_before(self, cutoff):
        for t in [t for t in self.steps if t < cutoff]:
            del self.steps[t]

    def bracket(self, ts):
        times = sorted(self.steps)
        lo = max([t for t in times if t <= ts], default=None)
        hi = min([t for t in times if t >= ts], default=None)
        return lo, hi

    def covers(self, ts):
        lo, hi = self.bracket(ts)
        return lo is not None and hi is not None

    def cells_per_second(self, fu, fv):
        return fu / self.cell_x, -fv / self.cell_y        # +y is south

    def _blend(self, ts, names):
        """Fields `names` of the bracketing steps pulled along the flow to
        `ts` and blended (semi-Lagrangian) → dict name → float32 [L, H, W],
        plus the flow in cells per 5 minutes."""
        lo, hi = self.bracket(ts)
        if lo is None or hi is None:
            raise LookupError(
                f'No cloud model steps around {ts:%Y-%m-%dT%H:%MZ}')
        s_lo, s_hi = self.steps[lo], self.steps[hi]
        w = 0.0
        if hi != lo:
            w = (ts - lo).total_seconds() / (hi - lo).total_seconds()
        fu = s_lo.fu * (1 - w) + s_hi.fu * w
        fv = s_lo.fv * (1 - w) + s_hi.fv * w
        cx, cy = self.cells_per_second(fu, fv)
        out = {}
        for name in names:
            a, b = getattr(s_lo, name), getattr(s_hi, name)
            if a is None or b is None:
                raise LookupError(f'Step lacks {name}')
            a, b = a.astype(np.float32), b.astype(np.float32)
            if hi == lo:
                out[name] = a
            else:
                s0 = (ts - lo).total_seconds()
                s1 = (hi - ts).total_seconds()
                out[name] = (warp(a, cx * s0, cy * s0) * (1 - w)
                             + warp(b, -cx * s1, -cy * s1) * w)
        flow = np.stack([cx * FRAME_SECONDS, cy * FRAME_SECONDS], axis=-1)
        return out, flow.astype(np.float32)

    def frame_at(self, ts):
        """→ (rg uint8 [L, H, W, 2], flow float32 [H, W, 2] in cells per
        5 minutes). Raises LookupError outside the loaded steps."""
        fields, flow = self._blend(ts, ('lwc', 'cov'))
        return quantise(fields['lwc'], fields['cov']), flow

    def forecast_frame_at(self, ts):
        """Forecast frame at any stamp, synthesised like the observed cloud
        frames → (rain uint8 [L, H, W] via Z–M, rg uint8 [L, H, W, 2],
        flow [H, W, 2])."""
        fields, flow = self._blend(ts, ('lwc', 'cov', 'qr', 'qs', 'qg'))
        rain = hydrometeors_to_dbz_bytes(
            fields['qr'], fields['qs'], fields['qg'])
        return rain, quantise(fields['lwc'], fields['cov']), flow


def flow_block(flow, crop, factor=FLOW_FACTOR):
    """
    The clouds frame's extra block: `u16 flow_w, u16 flow_h`, then
    flow_h × flow_w float16 (dx, dy) pairs, row-major, row 0 north,
    spanning exactly the crop's bounds (one flow cell ≈ `factor` frame
    cells; the field is bilinearly resampled at the flow cells' centres).
    """
    sub = flow[crop.row0:crop.row1, crop.col0:crop.col1]
    height, width = sub.shape[:2]
    fw = -(-width // factor)
    fh = -(-height // factor)
    # centres of the coarse cells in the crop's cell coordinates
    xs = (np.arange(fw) + 0.5) * width / fw - 0.5
    ys = (np.arange(fh) + 0.5) * height / fh - 0.5
    xs = np.clip(xs, 0, width - 1)
    ys = np.clip(ys, 0, height - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, max(width - 2, 0))
    y0 = np.clip(np.floor(ys).astype(int), 0, max(height - 2, 0))
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    tx = (xs - x0)[None, :, None]
    ty = (ys - y0)[:, None, None]
    coarse = (
        (sub[y0][:, x0] * (1 - tx) + sub[y0][:, x1] * tx) * (1 - ty)
        + (sub[y1][:, x0] * (1 - tx) + sub[y1][:, x1] * tx) * ty
    )
    return struct.pack('<HH', fw, fh) + coarse.astype('<f2').tobytes()


def parse_flow_block(data):
    fw, fh = struct.unpack_from('<HH', data)
    vectors = np.frombuffer(data, '<f2', offset=4).reshape(fh, fw, 2)
    return fw, fh, vectors.astype(np.float32)


def valid_time(run, step):
    return run + datetime.timedelta(hours=step)
