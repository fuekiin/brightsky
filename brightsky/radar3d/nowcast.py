"""
Nowcast for the forecast hour: the newest observed rain volume moved along
its own motion (Lagrangian persistence) and blended into the ICON-D2 field
in linear reflectivity, so the first frames continue the radar image and
the last ones are the model.

Motion is estimated by block matching between the two newest observed
column-maximum projections on the 2 km grid (tiles ~16 km apart, search
±10 km per 5 min, parabolic sub-cell refinement; the app's RadarDenseFlow
does the same), model wind filling tiles without trackable echo.
"""
import numpy as np

from brightsky.radar3d.clouds import warp


TILE = 8            # vector spacing in 2 km cells (16 km)
HALF = 8            # matching half-window in cells
SEARCH = 5          # max shift in cells per 5 minutes (120 km/h)
PENALTY = 0.002     # bias toward smaller motion, like the app's estimator
MIN_ECHO = 80       # byte value of 8 dBZ: below that a tile is "empty"


def bytes_to_z(vol):
    """Rain frame bytes (dBZ = v × 0.5 − 32, 0 = none) → linear Z."""
    v = np.asarray(vol, dtype=np.float32)
    z = np.power(10.0, (v * 0.5 - 32.0) / 10.0).astype(np.float32)
    z[v == 0] = 0.0
    return z


def z_to_bytes(z):
    """Linear Z → rain frame bytes; below 0 dBZ (Z < 1) is no echo, like the
    observed frames' floor."""
    z = np.asarray(z, dtype=np.float32)
    with np.errstate(divide='ignore'):
        dbz = 10.0 * np.log10(np.maximum(z, 1e-6))
    out = np.clip(np.round((dbz + 32.0) * 2.0), 1, 255).astype(np.uint8)
    out[z < 1.0] = 0
    return out


def maxpool2(vol):
    """[L, H, W] → [L, H/2, W/2] by 2×2 maximum (H, W even)."""
    levels, height, width = vol.shape
    return vol.reshape(levels, height // 2, 2, width // 2, 2).max(axis=(2, 4))


def column_max(vol):
    return vol.max(axis=0)


def _box_sums(field, half):
    """Sum of `field` over the (2·half+1)² window around every cell, edges
    clipped (summed-area table)."""
    padded = np.pad(field, half + 1)
    sat = padded.cumsum(0).cumsum(1)
    h, w = field.shape
    size = 2 * half + 1
    return (sat[size:size + h, size:size + w] - sat[:h, size:size + w]
            - sat[size:size + h, :w] + sat[:h, :w])


def estimate_motion(prev, cur, tile=TILE, half=HALF, search=SEARCH):
    """
    Block matching of the column-maximum projections `prev` → `cur`
    (uint8 [H, W], 5 minutes apart) → per-tile motion (u east, v south in
    cells per 5 min) on a [H/tile, W/tile] grid and a mask of tiles with
    trackable echo. Backward matching: cur(x) ≈ prev(x − motion).
    """
    a = prev.astype(np.float32)
    b = cur.astype(np.float32)
    height, width = b.shape
    th, tw = height // tile, width // tile
    rows = (np.arange(th) * tile + tile // 2).clip(0, height - 1)
    cols = (np.arange(tw) * tile + tile // 2).clip(0, width - 1)
    energy = _box_sums(b * b, half)[rows][:, cols] + 1e-3
    costs = np.empty((2 * search + 1, 2 * search + 1, th, tw), np.float32)
    padded = np.pad(a, search)
    for iy, dy in enumerate(range(-search, search + 1)):
        for ix, dx in enumerate(range(-search, search + 1)):
            # prev shifted by (dy, dx): shifted[y, x] = a[y - dy, x - dx]
            shifted = padded[search - dy:search - dy + height,
                             search - dx:search - dx + width]
            diff = (b - shifted) ** 2
            costs[iy, ix] = _box_sums(diff, half)[rows][:, cols] \
                + PENALTY * (dy * dy + dx * dx) * energy
    flat = costs.reshape(-1, th, tw)
    best = flat.argmin(axis=0)
    by, bx = np.divmod(best, 2 * search + 1)
    v = (by - search).astype(np.float32)
    u = (bx - search).astype(np.float32)
    # parabolic sub-cell refinement, only strictly inside the window
    ty, tx = np.indices((th, tw))
    mid = costs[by, bx, ty, tx]
    inner_x = (bx > 0) & (bx < 2 * search)
    lo = costs[by, np.clip(bx - 1, 0, 2 * search), ty, tx]
    hi = costs[by, np.clip(bx + 1, 0, 2 * search), ty, tx]
    denom = lo - 2 * mid + hi
    u += np.where(inner_x & (denom > 0), 0.5 * (lo - hi) / np.where(
        denom > 0, denom, 1), 0.0)
    inner_y = (by > 0) & (by < 2 * search)
    lo = costs[np.clip(by - 1, 0, 2 * search), bx, ty, tx]
    hi = costs[np.clip(by + 1, 0, 2 * search), bx, ty, tx]
    denom = lo - 2 * mid + hi
    v += np.where(inner_y & (denom > 0), 0.5 * (lo - hi) / np.where(
        denom > 0, denom, 1), 0.0)
    # tiles without echo in either frame are not trackable
    present = np.maximum(_box_max(b, half), _box_max(a, half))
    known = present[rows][:, cols] >= MIN_ECHO
    u[~known] = 0.0
    v[~known] = 0.0
    return u, v, known


def _box_max(field, half):
    """Maximum over the (2·half+1)² window around every cell."""
    from numpy.lib.stride_tricks import sliding_window_view
    padded = np.pad(field, half, mode='edge')
    size = 2 * half + 1
    return sliding_window_view(padded, (size, size)).max(axis=(2, 3))


def fill_and_smooth(u, v, known, model_u, model_v, tile=TILE):
    """Unknown tiles take the model wind (averaged over the tile); the
    field is then smoothed with a 3×3 mean so vectors vary gently."""
    th, tw = u.shape
    mu = model_u[:th * tile, :tw * tile].reshape(th, tile, tw, tile).mean(
        axis=(1, 3))
    mv = model_v[:th * tile, :tw * tile].reshape(th, tile, tw, tile).mean(
        axis=(1, 3))
    u = np.where(known, u, mu)
    v = np.where(known, v, mv)
    return _smooth3(u), _smooth3(v)


def _smooth3(field):
    padded = np.pad(field, 1, mode='edge')
    out = np.zeros_like(field)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out += padded[dy:dy + field.shape[0], dx:dx + field.shape[1]]
    return out / 9.0


def upsample(tiles, tile, height, width):
    """Tile-centre values → a [height, width] field, bilinear."""
    th, tw = tiles.shape
    ys = (np.arange(height) - tile // 2) / tile
    xs = (np.arange(width) - tile // 2) / tile
    y0 = np.clip(np.floor(ys).astype(int), 0, max(th - 2, 0))
    x0 = np.clip(np.floor(xs).astype(int), 0, max(tw - 2, 0))
    y1 = np.minimum(y0 + 1, th - 1)
    x1 = np.minimum(x0 + 1, tw - 1)
    ty = np.clip(ys - y0, 0, 1)[:, None]
    tx = np.clip(xs - x0, 0, 1)[None, :]
    return ((tiles[y0][:, x0] * (1 - tx) + tiles[y0][:, x1] * tx) * (1 - ty)
            + (tiles[y1][:, x0] * (1 - tx) + tiles[y1][:, x1] * tx) * ty)


def motion_field(prev_colmax, cur_colmax, model_u, model_v):
    """Full-resolution motion (cells per 5 min) for the newest observation:
    radar block matching where echo is trackable, model wind elsewhere."""
    height, width = cur_colmax.shape
    if prev_colmax is None:
        return model_u.astype(np.float32), model_v.astype(np.float32), None
    u, v, known = estimate_motion(prev_colmax, cur_colmax)
    u, v = fill_and_smooth(u, v, known, model_u, model_v)
    return (upsample(u, TILE, height, width).astype(np.float32),
            upsample(v, TILE, height, width).astype(np.float32), known)


def advect_z(z_vol, u, v, steps):
    """Move a linear-Z volume `steps` × 5 minutes along (u, v): backward
    sampling, the same horizontal vector for every slab of a column."""
    return warp(z_vol, u * steps, v * steps)


def blend_weight(lead_min, horizon_min=60):
    """Model weight w = (lead / horizon)²: ~1 % at +5 min, 25 % at +30,
    1 at +60."""
    return min(1.0, (lead_min / horizon_min) ** 2)


def blend_z(z_extrapolated, z_model, w):
    return (1.0 - w) * z_extrapolated + w * z_model
