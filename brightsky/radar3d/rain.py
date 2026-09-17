"""
Rain volumes from the DWD's per-site volume scans, voxel-centric ("pull"):
for every voxel, its (range, azimuth, elevation) seen from each site via
the exact inverse of the 4/3-earth beam model; reflectivity interpolated
linearly in elevation angle between the two bracketing tilts; sites merged
by maximum. Port of the app repo's tools/radar3d/grid_radar_volume.py.

The geometry (which sweep ray and gate a voxel falls into, at what
elevation) does not change between cycles, so `SiteGeometry` computes it
once and a cycle is a gather plus arithmetic.
"""
import logging
import math

import numpy as np


logger = logging.getLogger(__name__)

RE = 4.0 / 3.0 * 6371000.0
HALF_BEAM = math.radians(0.5)   # tolerance outside the lowest/highest tilt
MAX_RANGE = 180_000.0
DBZ_FLOOR = 0.0
# Geometry pre-filter: a superset of every plausible scanned cone
# (lowest tilt 0.5° minus half a beam, highest 25° plus half a beam).
MIN_EL = math.radians(-1.0)
MAX_EL = math.radians(27.0)


class SiteGeometry:
    """Which voxels a site can see and where they fall in its sweeps."""

    def __init__(self, grid, meta):
        self.grid = grid
        self.meta = meta
        lat_rows = grid.lat_rows()
        lon_cols = grid.lon_cols()
        dlat = MAX_RANGE / 111195.0
        dlon = MAX_RANGE / (111195.0 * math.cos(math.radians(meta.lat)))
        rows = np.flatnonzero(np.abs(lat_rows - meta.lat) <= dlat)
        cols = np.flatnonzero(np.abs(lon_cols - meta.lon) <= dlon)
        if not rows.size or not cols.size:
            self.row0 = self.row1 = self.col0 = self.col1 = 0
            self.n = 0
            return
        self.row0, self.row1 = int(rows[0]), int(rows[-1]) + 1
        self.col0, self.col1 = int(cols[0]), int(cols[-1]) + 1
        lat = lat_rows[self.row0:self.row1][None, :, None]
        lon = lon_cols[self.col0:self.col1][None, None, :]
        hgt = grid.level_heights()[:, None, None]
        dy = (lat - meta.lat) * 111195.0
        dx = (lon - meta.lon) * 111195.0 * math.cos(math.radians(meta.lat))
        s = np.hypot(dx, dy)                      # ground arc ≈ chord
        az = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0
        a = s / RE
        hrel = hgt - meta.height
        x = (RE + hrel) * np.sin(a)
        y = (RE + hrel) * np.cos(a) - RE
        r = np.hypot(x, y)
        el = np.arctan2(y, x)
        mask = (
            (r < MAX_RANGE) & (r >= meta.rstart)
            & (el >= MIN_EL) & (el <= MAX_EL)
        )
        ray = np.floor(az * meta.nrays / 360.0).astype(np.int32) % meta.nrays
        gate = np.clip(
            ((r - meta.rstart) / meta.rscale).astype(np.int32),
            0, meta.nbins - 1)
        self.flat = np.flatnonzero(mask).astype(np.int32)
        self.ray = np.broadcast_to(ray, mask.shape)[mask].astype(np.uint16)
        self.gate = gate[mask].astype(np.uint16)
        self.el = el[mask].astype(np.float32)
        self.n = int(self.flat.size)

    @property
    def shape(self):
        return (self.grid.levels, self.row1 - self.row0, self.col1 - self.col0)

    @property
    def nbytes(self):
        if not self.n:
            return 0
        return sum(a.nbytes for a in (self.flat, self.ray, self.gate, self.el))

    def _gather(self, dbz, sel):
        # Higher tilts have fewer gates; and the DWD occasionally emits a
        # sweep with 361 instead of 360 one-degree rays (an extra ray at
        # the wrap), so a ray index from another tilt's layout is clamped.
        nrays, nbins = dbz.shape
        gate = self.gate[sel]
        ray = np.minimum(self.ray[sel], nrays - 1)
        v = dbz[ray, np.minimum(gate, nbins - 1)]
        v[gate >= nbins] = np.nan
        return v

    def grid_cycle(self, tilts):
        """
        `tilts`: [(elangle_deg, dbz[nrays, nbins]), ...] of one cycle →
        uint8 sub-volume of `self.shape` (0 = no echo, else dBZ*2+64).
        """
        out8 = np.zeros(self.shape, np.uint8)
        if self.n == 0 or len(tilts) < 2:
            return out8
        tilts = sorted(tilts, key=lambda t: t[0])
        els = np.radians(np.array([t[0] for t in tilts], dtype=np.float64))
        el = self.el.astype(np.float64)
        idx = np.searchsorted(els, el) - 1        # tilt below each voxel
        below = idx < 0
        above = idx >= len(els) - 1
        idx = np.clip(idx, 0, len(els) - 2)
        v0 = np.full(self.n, np.nan, np.float32)
        v1 = np.full(self.n, np.nan, np.float32)
        for k in range(len(els) - 1):
            sel = np.flatnonzero(idx == k)
            if sel.size:
                v0[sel] = self._gather(tilts[k][1], sel)
                v1[sel] = self._gather(tilts[k + 1][1], sel)
        e0, e1 = els[idx], els[idx + 1]
        t = np.clip((el - e0) / (e1 - e0), 0, 1)
        nan0, nan1 = np.isnan(v0), np.isnan(v1)
        out = np.where(~nan0 & ~nan1, v0 * (1 - t) + v1 * t, np.nan)
        # If one side has no echo, keep the other only near that tilt
        only0 = ~nan0 & nan1 & (np.abs(el - e0) <= HALF_BEAM)
        only1 = nan0 & ~nan1 & (np.abs(el - e1) <= HALF_BEAM)
        out = np.where(only0, v0, out)
        out = np.where(only1, v1, out)
        # Outside the scanned cone (beyond half a beam past the lowest or
        # highest tilt) there is nothing; within that margin, the edge tilt
        edge_lo = below & (el >= els[0] - HALF_BEAM)
        edge_hi = above & (el <= els[-1] + HALF_BEAM)
        out = np.where(below & ~edge_lo, np.nan, out)
        out = np.where(above & ~edge_hi, np.nan, out)
        out = np.where(edge_lo, v0, out)
        out = np.where(edge_hi, v1, out)
        u8 = np.where(
            np.isnan(out), 0,
            np.clip(np.round((out + 32.0) * 2.0), 1, 255)).astype(np.uint8)
        out8.reshape(-1)[self.flat] = u8
        return out8


def grid_rain(grid, contributions):
    """
    Merge sites into one national volume. `contributions` yields
    `(SiteGeometry, tilts)` pairs; returns uint8 `grid.shape`.
    """
    vol = np.zeros(grid.shape, np.uint8)
    for geom, tilts in contributions:
        if geom.n == 0:
            continue
        try:
            sub = geom.grid_cycle(tilts)
        except Exception:
            logger.exception(
                'Skipping site %s: gridding failed', geom.meta.site)
            continue
        view = vol[:, geom.row0:geom.row1, geom.col0:geom.col1]
        np.maximum(view, sub, out=view)
    return vol
