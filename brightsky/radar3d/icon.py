"""
ICON-D2 model-level fields for the nano cloud volumes: GRIB2 (bz2) reading
with eccodes, bilinear sampling onto a `Grid`'s columns, and the vertical
interpolation from terrain-following model levels onto the fixed slabs.
Port of the app repo's grid_clouds2.py (vectorised).

Files: weather/nwp/icon-d2/grib/<HH>/<var>/
  icon-d2_germany_regular-lat-lon_model-level_<YYYYMMDDHH>_<step>_<level>_<var>.grib2.bz2
  (hhl: ..._time-invariant_<run>_000_<level>_hhl.grib2.bz2, 66 half levels)
Regular 0.02° lat/lon grid 1215 × 746, 9999 = missing, row 0 = south.
"""
import bz2
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


HALF_LEVELS = 66
Q_VARS = ('qc', 'qi', 'qs', 'qg')        # condensed water: the cloud body
P_VARS = ('qr', 'qs', 'qg')              # precipitation water: forecast rain
# Z–M relations per hydrometeor, Z (mm⁶/m³) = a · M^b with M in g/m³, summed
# in linear Z. Rain: 2.4e4·M^1.82 (0.1 g/m³ ≈ 26 dBZ, 1 ≈ 44). Dry snow and
# graupel reflect far less at equal mass (ice dielectric factor, density):
# snow ≈ 13 dB below rain at 1 g/m³, graupel in between.
ZM = {'qr': (2.4e4, 1.82), 'qs': (1.1e3, 1.6), 'qg': (5.0e3, 1.7)}
PWC_FLOOR = 0.02                         # g/m³ total below which no echo


def grib_name(run, step, level, var):
    kind = 'time-invariant' if var == 'hhl' else 'model-level'
    return (
        f'icon-d2_germany_regular-lat-lon_{kind}_{run:%Y%m%d%H}'
        f'_{step:03d}_{level}_{var}.grib2.bz2'
    )


def read_grib(path):
    """One field → (values [Nj, Ni] float32, NaN = missing, lat0, lon0,
    dlat, dlon); row 0 is the southern edge."""
    import eccodes
    raw = Path(path).read_bytes()
    data = bz2.decompress(raw) if str(path).endswith('.bz2') else raw
    gid = eccodes.codes_new_from_message(data)
    try:
        ni = eccodes.codes_get(gid, 'Ni')
        nj = eccodes.codes_get(gid, 'Nj')
        lat0 = eccodes.codes_get(gid, 'latitudeOfFirstGridPointInDegrees')
        lon0 = eccodes.codes_get(gid, 'longitudeOfFirstGridPointInDegrees')
        dlat = eccodes.codes_get(gid, 'jDirectionIncrementInDegrees')
        dlon = eccodes.codes_get(gid, 'iDirectionIncrementInDegrees')
        missing = eccodes.codes_get(gid, 'missingValue')
        values = eccodes.codes_get_values(gid).reshape(nj, ni)
    finally:
        eccodes.codes_release(gid)
    if lon0 > 180:
        lon0 -= 360
    values = values.astype(np.float32)
    values[values == missing] = np.nan
    return values, lat0, lon0, dlat, dlon


class Sampler:
    """Bilinear sampling of ICON's lat/lon fields at a Grid's voxel columns."""

    def __init__(self, grid):
        self.grid = grid
        self.lat = grid.lat_rows()
        self.lon = grid.lon_cols()
        self.layout = None

    def _prepare(self, shape, lat0, lon0, dlat, dlon):
        fy = (self.lat - lat0) / dlat
        fx = (self.lon - lon0) / dlon
        y0 = np.floor(fy).astype(int)
        x0 = np.floor(fx).astype(int)
        self.ty = (fy - y0)[:, None].astype(np.float32)
        self.tx = (fx - x0)[None, :].astype(np.float32)
        self.y0 = np.clip(y0, 0, shape[0] - 2)[:, None]
        self.x0 = np.clip(x0, 0, shape[1] - 2)[None, :]
        self.layout = (shape, lat0, lon0, dlat, dlon)

    def sample(self, field, lat0, lon0, dlat, dlon):
        layout = (field.shape, lat0, lon0, dlat, dlon)
        if self.layout != layout:
            self._prepare(*layout)
        y0, x0, ty, tx = self.y0, self.x0, self.ty, self.tx
        a = field[y0, x0]
        b = field[y0, x0 + 1]
        c = field[y0 + 1, x0]
        d = field[y0 + 1, x0 + 1]
        top = a * (1 - tx) + b * tx
        bottom = c * (1 - tx) + d * tx
        return top * (1 - ty) + bottom * ty

    def read(self, path):
        return self.sample(*read_grib(path))


def level_heights(hhl_dir, run, sampler):
    """Full-level heights (mean of the bounding half levels) at the grid's
    columns: [65, H, W], model level 1 (top) first."""
    hhl = np.stack([
        sampler.read(Path(hhl_dir) / grib_name(run, 0, k, 'hhl'))
        for k in range(1, HALF_LEVELS + 1)
    ])
    return 0.5 * (hhl[:-1] + hhl[1:])


def choose_levels(full_h, top_m=12500.0):
    """Model levels (1-based, top first) needed to fill slabs up to `top_m`:
    from the highest level that dips below `top_m` anywhere down to 65."""
    lowest = np.nanmin(full_h.reshape(full_h.shape[0], -1), axis=1)
    first = int(np.argmax(lowest <= top_m)) + 1
    first = max(1, first - 1)                # one more above, to bracket
    return list(range(first, full_h.shape[0] + 1))


def wind_levels(full_h, bottom_m=1000.0, top_m=10000.0, stride=3):
    """A coarser subset of levels for the flow field: every `stride`th level
    whose mean height lies between `bottom_m` and `top_m`."""
    mean = np.nanmean(full_h.reshape(full_h.shape[0], -1), axis=1)
    inside = [k + 1 for k in range(full_h.shape[0])
              if bottom_m <= mean[k] <= top_m]
    return inside[::stride]


def to_slabs(field, heights, slab_h):
    """
    Interpolate `field` [K, H, W] given per-column `heights` [K, H, W] (any
    vertical order, NaN = missing) linearly in height to the slab centres
    `slab_h` [L] → [L, H, W]; 0 outside the column's valid range.
    Equivalent to np.interp(slab_h, h[ok], v[ok], left=0, right=0) per column.
    """
    valid = ~np.isnan(heights) & ~np.isnan(field)
    h = np.where(valid, heights, np.nan)
    v = np.where(valid, field, 0.0).astype(np.float32)
    out = np.zeros((len(slab_h),) + field.shape[1:], np.float32)
    for s, hs in enumerate(slab_h):
        above = h >= hs                          # valid and at/above hs
        below = h < hs                           # valid and below hs
        any_above = above.any(axis=0)
        any_below = below.any(axis=0)
        # nearest valid level at/above: the one with the smallest height
        h_above = np.where(above, h, np.inf)
        idx1 = np.argmin(h_above, axis=0)
        h_below = np.where(below, h, -np.inf)
        idx0 = np.argmax(h_below, axis=0)
        h0 = np.take_along_axis(h, idx0[None], 0)[0]
        h1 = np.take_along_axis(h, idx1[None], 0)[0]
        v0 = np.take_along_axis(v, idx0[None], 0)[0]
        v1 = np.take_along_axis(v, idx1[None], 0)[0]
        ok = any_above & any_below
        with np.errstate(invalid='ignore', divide='ignore'):
            t = np.where(ok, (hs - h0) / (h1 - h0), 0.0)
        exact = ok & (h1 == hs)
        out[s] = np.where(exact, v1, np.where(ok, v0 * (1 - t) + v1 * t, 0.0))
    return out


@dataclass
class StepFields:
    """One model step on the cloud grid: condensed water (g/m³) and cover
    (%) on the slabs, the column flow (m/s, east/north), and the
    precipitation water (g/m³) for the forecast rain."""

    valid_time: object
    lwc: np.ndarray        # [L, H, W] float16
    cov: np.ndarray        # [L, H, W] float16
    fu: np.ndarray         # [H, W] float32
    fv: np.ndarray         # [H, W] float32
    qr: np.ndarray = None  # [L, H, W] float16 g/m³ rain water
    qs: np.ndarray = None  # snow
    qg: np.ndarray = None  # graupel


def hydrometeors_to_z(qr, qs, qg):
    """Rain, snow and graupel water contents (g/m³) → linear reflectivity
    Z (mm⁶/m³) via per-hydrometeor Z–M relations summed; 0 below the
    total-mass floor."""
    total = np.zeros(np.shape(qr), np.float32)
    z = np.zeros(np.shape(qr), np.float32)
    for name, m in (('qr', qr), ('qs', qs), ('qg', qg)):
        m = np.maximum(np.asarray(m, dtype=np.float32), 0.0)
        a, b = ZM[name]
        z += a * np.power(m, b)
        total += m
    z[total < PWC_FLOOR] = 0.0
    return z


def hydrometeors_to_dbz_bytes(qr, qs, qg):
    """→ the rain frames' byte encoding (dBZ = v × 0.5 − 32, 0 = no echo)."""
    z = hydrometeors_to_z(qr, qs, qg)
    with np.errstate(divide='ignore'):
        dbz = 10.0 * np.log10(np.maximum(z, 1e-6))
    out = np.clip(np.round((dbz + 32.0) * 2.0), 1, 255).astype(np.uint8)
    out[z <= 0.0] = 0
    return out


def column_flow(lwc, cov, u, v, slab_h):
    """Water-weighted mean wind over the column, else the 3–8 km mean."""
    wgt = lwc + 0.02 * (cov / 100.0)
    mid = (slab_h >= 3000) & (slab_h <= 8000)
    den = wgt.sum(0)
    num_u = (u * wgt).sum(0)
    num_v = (v * wgt).sum(0)
    fu = np.where(den > 1e-3, num_u / np.maximum(den, 1e-6), u[mid].mean(0))
    fv = np.where(den > 1e-3, num_v / np.maximum(den, 1e-6), v[mid].mean(0))
    return fu.astype(np.float32), fv.astype(np.float32)


def load_step(run_dir, run, step, sampler, full_h, q_levels, w_levels,
              read=None):
    """Read one step's files from `run_dir` and build its StepFields."""
    import datetime
    read = read or sampler.read
    run_dir = Path(run_dir)
    grid = sampler.grid
    slab_h = grid.level_heights()

    def stack(var, levels):
        return np.stack([
            read(run_dir / grib_name(run, step, k, var)) for k in levels])

    h_q = full_h[[k - 1 for k in q_levels]]
    h_w = full_h[[k - 1 for k in w_levels]]
    fields = {var: stack(var, q_levels) for var in set(Q_VARS) | set(P_VARS)}
    rho = 1.225 * np.exp(-np.clip(h_q, 0, None) / 8500.0)
    q = sum(fields[var] for var in Q_VARS)
    lwc = to_slabs(q * rho * 1000.0, h_q, slab_h)
    hydro = {
        var: to_slabs(fields[var] * rho * 1000.0, h_q, slab_h).astype(
            np.float16)
        for var in P_VARS
    }
    del fields
    cov = to_slabs(stack('clc', q_levels), h_q, slab_h)
    u = to_slabs(stack('u', w_levels), h_w, slab_h)
    v = to_slabs(stack('v', w_levels), h_w, slab_h)
    fu, fv = column_flow(lwc, cov, u, v, slab_h)
    return StepFields(
        valid_time=run + datetime.timedelta(hours=step),
        lwc=lwc.astype(np.float16), cov=cov.astype(np.float16),
        fu=fu, fv=fv, **hydro,
    )


def cell_metres(grid):
    """Horizontal cell size (m) of a grid at its mid-latitude: (x, y)."""
    mid = (grid.min_lat + grid.max_lat) / 2
    cell_x = ((grid.max_lon - grid.min_lon) / grid.width
              * 111195.0 * math.cos(math.radians(mid)))
    cell_y = (grid.max_lat - grid.min_lat) / grid.height * 111195.0
    return cell_x, cell_y
