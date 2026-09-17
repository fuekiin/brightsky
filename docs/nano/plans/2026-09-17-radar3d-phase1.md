# Radar 3D backend, phase 1 (manifest + rain crops) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve nano's 3D radar view a national 1 km × 500 m reflectivity volume every 5 minutes, gridded from the DWD's 17 `sweep_vol_z` volume scans, as viewport crops behind a `/radar3d` manifest.

**Architecture:** A new `brightsky.radar3d` package (grid geometry, ODIM-HDF5 sweep reading, the ported voxel-centric gridder with per-site precomputed index maps, an on-disk frame store, and a stand-alone polling worker) plus three additive web routes. The worker runs in its own container (`radar3d-work`), writes one uncompressed `.npy` national volume per cycle into `RADAR3D_DATA_DIR` and indexes it in a new `radar3d_frames` table; the web app memory-maps the volume, slices the requested bbox, zlib-compresses it and returns a `NANO3D` binary frame with immutable caching. The existing huey worker, parsers, and `weather` tables are untouched.

**Tech Stack:** Python 3.12 (dev) / 3.14 (image), numpy, h5py, requests + parsel (listing), FastAPI + asyncpg (web), psycopg2 (worker), pytest.

**Spec:** `../WeatherGermany/docs/superpowers/specs/2026-09-17-radar-3d-backend-brief.md` (the brief) and the reference gridder `../WeatherGermany/tools/radar3d/grid_radar_volume.py`.

## Global Constraints

- DWD open data only (`https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/<site>/hdf5/filter_polarimetric/`); never write to the `weather` table; own table + endpoints; strictly additive files so rebases on upstream stay clean (`docs/nano/fork-management.md`).
- Sites (17): `asb boo drs eis ess fbg fld hnr isn mem neu nhb oft pro ros tur umd`; 10 tilts per site per 5 min; tilt file index ≠ elevation order (00–05 = 5.5°…0.5°, 06–09 = 8/12/17/25°) — always read `elangle` from the file. Gain 0.00293, offset −64, nodata 65535, undetect 0 — read from `dataset1/data1/what`, never hard-code.
- Rain grid: **1 km × 500 m**, 24 slabs 0–12 km, Web Mercator rows (row 0 north), regular longitude columns; encoding `dBZ = v × 0.5 − 32`, `0` = no echo, level-major `[level][row][col]`, one channel.
- Gridding = the reference algorithm exactly: 4/3-earth beam model inverted per voxel, linear interpolation in elevation angle between bracketing tilts, sites merged by maximum, half-beam (0.5°) tolerance outside the lowest/highest tilt, 180 km range, dBZ floor 0.
- A 1 km Germany cycle must grid well inside 5 minutes; the existing worker stays untouched (radar3d has its own container).
- API: `GET /radar3d?lat=&lon=&distance=[&from=&to=][&resolution=1000|2000]` → manifest; `GET /radar3d/rain/{ts}?bbox=minLat,maxLat,minLon,maxLon` → binary frame with `Cache-Control: immutable, max-age=86400`. Default window last 60 min (12 frames); `distance` capped at 250 km (1 km) / 600 km (2 km).
- Retention 3 h on disk; Postgres keeps only the index.
- Every phase: worklog entry, `docs/nano/architecture.md` section, gridder tests against a saved sweep.
- ruff: `line-length = 79`, `select = ["E", "F"]`.

## Deviations from the brief (decided here, reported to the app session)

1. **Binary header layout** (the brief lists the fields but not the bytes). Little-endian, 32 bytes:
   `0–5` magic `NANO3D`, `6–7` u16 version (=1), `8–9` u16 width, `10–11` u16 height, `12–13` u16 levels, `14–15` u16 channels, `16–19` u32 zlib payload length, `20–23` u32 extra block length (0 in phase 1; clouds' flow block in phase 2), `24–31` zero. Then the zlib payload, then the extra block.
2. **Storage path** defaults to `.data/radar3d/` (setting `RADAR3D_DATA_DIR`) rather than `/data/radar3d`, so local dev works without overrides; the compose service bind-mounts it. National frames are stored as uncompressed `.npy` (memory-mapped for slicing), not as `.bin` — the `.bin` framing is applied per request.
3. **Phase 1 manifest** has `"clouds": null` and `"cells": null` in every frame and `"flows_clouds": false`; `grid` is the rain grid. `resolution=2000` is served by 2×2 max-pooling the 1 km grid (clouds get their own native 2 km grid in phase 2).
4. `distance` is in **meters** (like `/radar`), default 100000. `bbox` in frame URLs is emitted with 6 decimals; both endpoints snap identically (outward to voxel edges, aligned to 2 cells at 2 km).
5. Timestamps in the manifest JSON are Bright Sky style (`2026-09-16T12:00:00+00:00`); the frame URL path uses `2026-09-16T12:00:00Z`. Both parse with ISO 8601.

## File structure

- Create `brightsky/radar3d/__init__.py` — empty package marker.
- Create `brightsky/radar3d/grid.py` — `Grid`, `Crop`, `OutsideGrid`, `GERMANY_1KM`: voxel-centre geometry, bbox→crop snapping, crop→bounds.
- Create `brightsky/radar3d/frame.py` — `encode()` / `decode()` of the `NANO3D` wire format.
- Create `brightsky/radar3d/sweeps.py` — sweep filename parsing (`SweepInfo`), ODIM-HDF5 reading (`SiteMeta`, `read_site_meta`, `read_tilt`).
- Create `brightsky/radar3d/rain.py` — `SiteGeometry` (precomputed index maps) and `grid_rain()`.
- Create `brightsky/radar3d/store.py` — `FrameStore` (npy files) + `index_frame()` / `indexed_timestamps()` / `delete_index_before()` (psycopg2).
- Create `brightsky/radar3d/ingest.py` — `SweepSource` (listing + download), `Radar3DIngest` (cycle tracking, gridding, retention), `run_forever()`.
- Create `migrations/0021_radar3d.sql` — `radar3d_frames` index table.
- Modify `brightsky/settings.py` — `RADAR3D_*` settings.
- Modify `brightsky/cli.py` — `radar3d-work`, `radar3d-grid` commands.
- Modify `brightsky/query.py` — `radar3d()` manifest query, `radar3d_frame_exists()`.
- Modify `brightsky/web/params.py`, `models.py`, `app.py` — params, OpenAPI models, routes.
- Modify `docker-compose.yml` — `radar3d` service + shared volume.
- Modify `tests/conftest.py` — clear `radar3d_frames`.
- Create `tests/data/radar3d/` — 10 `isn` sweeps of the 2026-09-16 11:50 cycle + golden volume.
- Create `tests/test_radar3d_grid.py`, `tests/test_radar3d_sweeps.py`, `tests/test_radar3d_rain.py`, `tests/test_radar3d_store.py`, `tests/test_radar3d_ingest.py`; modify `tests/test_web.py`.
- Modify `docs/nano/worklog.md`, `docs/nano/architecture.md`, `docs/nano/deployment.md`, `docs/nano/README.md`.

---

### Task 1: Grid geometry

**Files:**
- Create: `brightsky/radar3d/__init__.py`, `brightsky/radar3d/grid.py`
- Test: `tests/test_radar3d_grid.py`

**Interfaces:**
- Produces: `Grid(min_lat, max_lat, min_lon, max_lon, width, height, levels=24, level_m=500.0, base_m=0.0)` with `.shape`, `.lat_rows()`, `.lon_cols()`, `.level_heights()`, `.crop(min_lat, max_lat, min_lon, max_lon, align=1) -> Crop`, `.around(lat, lon, distance_m, align=1) -> Crop`, `.bounds(crop) -> (min_lat, max_lat, min_lon, max_lon)`; `Crop(row0, row1, col0, col1)` half-open with `.width`/`.height`; `OutsideGrid(ValueError)`; `GERMANY_1KM`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_radar3d_grid.py
import numpy as np
import pytest

from brightsky.radar3d.grid import GERMANY_1KM, Crop, Grid, OutsideGrid


def test_germany_grid_shape():
    assert GERMANY_1KM.shape == (24, 912, 698)
    lat = GERMANY_1KM.lat_rows()
    lon = GERMANY_1KM.lon_cols()
    assert lat[0] > lat[-1]                      # row 0 is north
    assert 55.19 > lat[0] > 55.18
    assert lon[0] > 5.5 and lon[-1] < 15.5
    assert np.all(np.diff(lon) > 0)
    assert GERMANY_1KM.level_heights()[0] == 250.0
    assert GERMANY_1KM.level_heights()[-1] == 11750.0


def test_crop_snaps_outward_and_bounds_roundtrip():
    g = GERMANY_1KM
    crop = g.crop(52.4, 52.6, 13.3, 13.5)
    b = g.bounds(crop)
    assert b[0] <= 52.4 and b[1] >= 52.6
    assert b[2] <= 13.3 and b[3] >= 13.5
    # Re-snapping the (6-decimal rounded) bounds is a no-op
    rounded = tuple(round(x, 6) for x in b)
    assert g.crop(*rounded) == crop


def test_crop_aligned_to_two_cells():
    crop = GERMANY_1KM.crop(52.4, 52.6, 13.3, 13.5, align=2)
    assert crop.row0 % 2 == 0 and crop.col0 % 2 == 0
    assert crop.width % 2 == 0 and crop.height % 2 == 0


def test_around_clips_to_grid_and_rejects_outside():
    g = GERMANY_1KM
    crop = g.around(52.52, 13.41, 100000)
    assert 190 <= crop.width <= 210 and 220 <= crop.height <= 250
    edge = g.around(47.1, 5.6, 100000)
    assert edge.col0 == 0 and edge.row1 == g.height
    with pytest.raises(OutsideGrid):
        g.around(40.0, 13.0, 1000)
    with pytest.raises(OutsideGrid):
        g.crop(40.0, 41.0, 13.0, 14.0)


def test_small_grid_matches_reference_construction():
    # The reference gridder's Isen fixture grid: 300 km box around isn
    import math
    clat, clon, half = 48.174705, 12.101779, 150.0
    dlat = half / 111.195
    dlon = half / (111.195 * math.cos(math.radians(clat)))
    g = Grid(clat - dlat, clat + dlat, clon - dlon, clon + dlon, 300, 300)
    assert g.crop(clat - dlat, clat + dlat, clon - dlon, clon + dlon) == \
        Crop(0, 300, 0, 300)
    assert abs(g.lat_rows()[150] - clat) < 0.01
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_radar3d_grid.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'brightsky.radar3d'`

- [ ] **Step 3: Implement**

`brightsky/radar3d/__init__.py` is empty. `brightsky/radar3d/grid.py`:

```python
"""
Voxel grid geometry for the nano radar3d products.

Rows are regular in Web Mercator y (row 0 = north), columns regular in
longitude; `levels` slabs of `level_m` metres stack up from `base_m` above
sea level. Same construction as the reference gridder in the app repo
(tools/radar3d/grid_radar_volume.py), so the app's map quad matches.
"""
import math
from dataclasses import dataclass

import numpy as np


# Snapping tolerance in cells: a bbox that is already on voxel edges (as
# emitted with 6 decimals in the manifest) must snap back to the same crop.
_EPS = 1e-3


class OutsideGrid(ValueError):
    pass


def merc_y(lat):
    s = math.sin(math.radians(lat))
    return 0.5 - 0.25 * math.log((1 + s) / (1 - s)) / math.pi


def merc_lat(y):
    return math.degrees(math.atan(math.sinh((0.5 - y) * 2 * math.pi)))


@dataclass(frozen=True)
class Crop:
    """Half-open row/column window into a grid."""

    row0: int
    row1: int
    col0: int
    col1: int

    @property
    def width(self):
        return self.col1 - self.col0

    @property
    def height(self):
        return self.row1 - self.row0


@dataclass(frozen=True)
class Grid:
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    width: int
    height: int
    levels: int = 24
    level_m: float = 500.0
    base_m: float = 0.0

    @property
    def shape(self):
        return (self.levels, self.height, self.width)

    def lat_rows(self):
        """Voxel-centre latitudes per row, row 0 = north."""
        my0, my1 = merc_y(self.max_lat), merc_y(self.min_lat)
        rows = my0 + (np.arange(self.height) + 0.5) / self.height * (my1 - my0)
        return np.degrees(np.arctan(np.sinh((0.5 - rows) * 2 * math.pi)))

    def lon_cols(self):
        """Voxel-centre longitudes per column."""
        span = self.max_lon - self.min_lon
        return self.min_lon + (np.arange(self.width) + 0.5) / self.width * span

    def level_heights(self):
        """Slab-centre heights above sea level, in metres."""
        return self.base_m + (np.arange(self.levels) + 0.5) * self.level_m

    def _row(self, lat):
        my0, my1 = merc_y(self.max_lat), merc_y(self.min_lat)
        return (merc_y(lat) - my0) / (my1 - my0) * self.height

    def _col(self, lon):
        return (lon - self.min_lon) / (self.max_lon - self.min_lon) * self.width

    def crop(self, min_lat, max_lat, min_lon, max_lon, align=1):
        """
        Smallest crop covering the box, snapped outward to voxel edges (and
        to multiples of `align` cells), clipped to the grid.
        """
        row0 = math.floor(self._row(max_lat) + _EPS)
        row1 = math.ceil(self._row(min_lat) - _EPS)
        col0 = math.floor(self._col(min_lon) + _EPS)
        col1 = math.ceil(self._col(max_lon) - _EPS)
        row0 = max(0, row0 // align * align)
        col0 = max(0, col0 // align * align)
        row1 = min(self.height, -(-row1 // align) * align)
        col1 = min(self.width, -(-col1 // align) * align)
        if row1 <= row0 or col1 <= col0:
            raise OutsideGrid("Requested area lies outside the grid")
        return Crop(row0, row1, col0, col1)

    def around(self, lat, lon, distance_m, align=1):
        """Crop reaching `distance_m` metres to each side of a position."""
        if not (self.min_lat <= lat <= self.max_lat
                and self.min_lon <= lon <= self.max_lon):
            raise OutsideGrid("Position lies outside the grid")
        dlat = distance_m / 111195.0
        dlon = distance_m / (111195.0 * math.cos(math.radians(lat)))
        return self.crop(lat - dlat, lat + dlat, lon - dlon, lon + dlon, align)

    def bounds(self, crop):
        """(min_lat, max_lat, min_lon, max_lon) of a crop's outer edges."""
        my0, my1 = merc_y(self.max_lat), merc_y(self.min_lat)
        span = self.max_lon - self.min_lon
        return (
            merc_lat(my0 + crop.row1 / self.height * (my1 - my0)),
            merc_lat(my0 + crop.row0 / self.height * (my1 - my0)),
            self.min_lon + crop.col0 / self.width * span,
            self.min_lon + crop.col1 / self.width * span,
        )


# All of Germany at (nominally) 1 km: 10° of longitude at 51° N is ~700 km,
# 8.2° of latitude ~912 km. Both even so the 2 km max-pool aligns.
GERMANY_1KM = Grid(47.0, 55.2, 5.5, 15.5, width=698, height=912)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_radar3d_grid.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add brightsky/radar3d/__init__.py brightsky/radar3d/grid.py tests/test_radar3d_grid.py
git commit -m "feat(radar3d): add mercator voxel grid geometry"
```

---

### Task 2: NANO3D frame codec

**Files:**
- Create: `brightsky/radar3d/frame.py`
- Test: `tests/test_radar3d_grid.py` (append)

**Interfaces:**
- Produces: `encode(voxels: np.ndarray[uint8], channels=1, extra=b'') -> bytes`; `decode(data: bytes) -> (header: dict, voxels: np.ndarray, extra: bytes)`; `HEADER_SIZE = 32`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_radar3d_grid.py
import struct
import zlib

from brightsky.radar3d import frame


def test_frame_roundtrip_and_header_layout():
    vox = np.zeros((24, 5, 7), np.uint8)
    vox[3, 1, 2] = 200
    data = frame.encode(vox, extra=b'FLOW')
    assert data[:6] == b'NANO3D'
    version, w, h, levels, ch, n, m = struct.unpack_from('<HHHHHII', data, 6)
    assert (version, w, h, levels, ch) == (1, 7, 5, 24, 1)
    assert data[24:32] == bytes(8)
    assert len(data) == 32 + n + m and m == 4
    assert zlib.decompress(data[32:32 + n]) == vox.tobytes()
    header, out, extra = frame.decode(data)
    assert header == {'version': 1, 'width': 7, 'height': 5, 'levels': 24,
                      'channels': 1}
    assert np.array_equal(out, vox) and extra == b'FLOW'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_radar3d_grid.py -q -k frame`
Expected: FAIL with `ImportError: cannot import name 'frame'`

- [ ] **Step 3: Implement**

```python
# brightsky/radar3d/frame.py
"""
The `NANO3D` binary frame the app decodes: a 32-byte little-endian header,
a zlib block of level-major voxel bytes, and an optional extra block (the
clouds' flow field in phase 2).

    0   6s  magic 'NANO3D'
    6   u16 version (1)
    8   u16 width
    10  u16 height
    12  u16 levels
    14  u16 channels (bytes per voxel)
    16  u32 zlib payload length
    20  u32 extra block length
    24  8 bytes reserved (zero)
"""
import struct
import zlib

import numpy as np


MAGIC = b'NANO3D'
VERSION = 1
HEADER = struct.Struct('<6sHHHHHII8x')
HEADER_SIZE = HEADER.size
assert HEADER_SIZE == 32


def encode(voxels, channels=1, extra=b'', level=6):
    levels, height, width = voxels.shape[:3]
    raw = np.ascontiguousarray(voxels, dtype=np.uint8).tobytes()
    payload = zlib.compress(raw, level)
    header = HEADER.pack(
        MAGIC, VERSION, width, height, levels, channels,
        len(payload), len(extra))
    return header + payload + extra


def decode(data):
    magic, version, width, height, levels, channels, n, m = \
        HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("Not a NANO3D frame")
    raw = zlib.decompress(data[HEADER_SIZE:HEADER_SIZE + n])
    shape = (levels, height, width)
    if channels > 1:
        shape += (channels,)
    voxels = np.frombuffer(raw, np.uint8).reshape(shape)
    header = {
        'version': version, 'width': width, 'height': height,
        'levels': levels, 'channels': channels,
    }
    return header, voxels, data[HEADER_SIZE + n:HEADER_SIZE + n + m]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_radar3d_grid.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add brightsky/radar3d/frame.py tests/test_radar3d_grid.py
git commit -m "feat(radar3d): add NANO3D binary frame codec"
```

---

### Task 3: Sweep files (names + ODIM HDF5) and test fixtures

**Files:**
- Create: `brightsky/radar3d/sweeps.py`
- Create: `tests/data/radar3d/` — the 10 `isn` tilt files of the 2026-09-16 11:50 cycle, copied from the app session's scratchpad `volfixture/raw/` (`*2026091611[5][0-4]*-isn-*`, ~900 KB total)
- Test: `tests/test_radar3d_sweeps.py`

**Interfaces:**
- Produces: `SweepInfo(name, site, tilt, timestamp)` with `.cycle` (timestamp floored to 5 min, UTC); `parse_sweep_name(name) -> SweepInfo | None`; `SiteMeta(site, lat, lon, height, nbins, nrays, rscale, rstart)` (metres); `read_site_meta(path, site) -> SiteMeta`; `read_tilt(path, dbz_floor=0.0) -> (elangle_deg: float, dbz: np.ndarray[float32, (nrays, nbins)])` with NaN where nodata/undetect/below floor.

- [ ] **Step 1: Copy the fixtures**

```bash
S=/private/tmp/claude-501/-Users-benjaminkramser-Development-WeatherGermany/d2a6d76e-9bdf-4d85-b536-2003c9b74034/scratchpad/volfixture
mkdir -p tests/data/radar3d
cp $S/raw/*2026091611[5][0-4]*-isn-*-hd5 tests/data/radar3d/
ls tests/data/radar3d | wc -l   # 10
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_radar3d_sweeps.py
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
    path = sweep_dir / NAME.replace('_09-2026091611540200',
                                    '_00-2026091611505700')
    meta = read_site_meta(path, 'isn')
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
    _, lowest = read_tilt(
        sweep_dir / NAME.replace('_09-2026091611540200',
                                 '_00-2026091611505700'))
    assert lowest.shape == (360, 720)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_radar3d_sweeps.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement**

```python
# brightsky/radar3d/sweeps.py
"""
DWD `sweep_vol_z` volume scans: one ODIM-HDF5 file per site, tilt and
5-minute cycle at
https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/<site>/hdf5/filter_polarimetric/
"""
import datetime
import re
from dataclasses import dataclass

import h5py
import numpy as np


SWEEP_NAME = re.compile(
    r'^ras07-vol5minng01_sweeph5onem_dbzh_(\d{2})-(\d{14})\d{2}'
    r'-([a-z]{3})-\d+-hd5$')


@dataclass(frozen=True)
class SweepInfo:
    name: str
    site: str
    tilt: int
    timestamp: datetime.datetime

    @property
    def cycle(self):
        """The 5-minute volume scan this sweep belongs to."""
        ts = self.timestamp
        return ts.replace(minute=ts.minute - ts.minute % 5, second=0)


def parse_sweep_name(name):
    match = SWEEP_NAME.match(name)
    if not match:
        return None
    timestamp = datetime.datetime.strptime(
        match.group(2), '%Y%m%d%H%M%S').replace(tzinfo=datetime.UTC)
    return SweepInfo(name, match.group(3), int(match.group(1)), timestamp)


@dataclass(frozen=True)
class SiteMeta:
    site: str
    lat: float
    lon: float
    height: float
    nbins: int
    nrays: int
    rscale: float
    rstart: float


def read_site_meta(path, site):
    """Site position and the (lowest tilt's) ray/gate layout, in metres."""
    with h5py.File(path) as h:
        where = h['where'].attrs
        dwhere = h['dataset1/where'].attrs
        return SiteMeta(
            site=site,
            lat=float(where['lat']),
            lon=float(where['lon']),
            height=float(where['height']),
            nbins=int(dwhere['nbins']),
            nrays=int(dwhere['nrays']),
            rscale=float(dwhere['rscale']),
            rstart=float(dwhere['rstart']) * 1000.0,
        )


def read_tilt(path, dbz_floor=0.0):
    """Elevation angle (degrees) and reflectivity (dBZ, NaN = no echo)."""
    with h5py.File(path) as h:
        elangle = float(h['dataset1/where'].attrs['elangle'])
        what = h['dataset1/data1/what'].attrs
        raw = h['dataset1/data1/data'][:]
        dbz = raw.astype(np.float32) * float(what['gain']) \
            + float(what['offset'])
        dbz[
            (raw == what['nodata'])
            | (raw == what['undetect'])
            | (dbz < dbz_floor)
        ] = np.nan
    return elangle, dbz
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_radar3d_sweeps.py -q`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add brightsky/radar3d/sweeps.py tests/test_radar3d_sweeps.py tests/data/radar3d
git commit -m "feat(radar3d): read DWD sweep_vol_z ODIM-HDF5 sweeps"
```

---

### Task 4: The rain gridder (port with precomputed index maps) + golden test

**Files:**
- Create: `brightsky/radar3d/rain.py`
- Create: `tests/data/radar3d/golden_isn_1150.u8.zlib` — the reference gridder's output for site `isn`, cycle 11:50, on its 300×300 Isen grid (produced by `tools/radar3d/grid_radar_volume.py` with `SITES=["isn"]`, `CYCLES=["1150"]`; already at scratchpad `ref_isn_1150.json` → base64-decode `radar3d[0].reflectivity`, ~62 KB)
- Test: `tests/test_radar3d_rain.py`

**Interfaces:**
- Consumes: `Grid` (Task 1), `SiteMeta`, `read_tilt` (Task 3).
- Produces: `SiteGeometry(grid, meta)` with `.n`, `.row0/.row1/.col0/.col1`, `.shape`, `.nbytes`, `.grid_cycle(tilts: list[(elangle_deg, dbz)]) -> np.ndarray[uint8, shape]`; `grid_rain(grid, contributions: Iterable[(SiteGeometry, tilts)]) -> np.ndarray[uint8, grid.shape]`.

- [ ] **Step 1: Produce the golden file**

```bash
.venv/bin/python - <<'EOF'
import base64, json
P = '/private/tmp/claude-501/-Users-benjaminkramser-Development-brightsky/6de1724a-2fd4-4c07-9194-399715ec3ce9/scratchpad/ref_isn_1150.json'
doc = json.load(open(P))
frame = doc['radar3d'][0]
assert frame['timestamp'] == '2026-09-16T11:50:00+00:00'
open('tests/data/radar3d/golden_isn_1150.u8.zlib', 'wb').write(
    base64.b64decode(frame['reflectivity']))
print(doc['grid'])
EOF
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_radar3d_rain.py
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
    diff = vol.astype(int) - golden.astype(int)
    # Float32 elevation angles vs the reference's float64: at most one
    # count (0.5 dBZ) apart, and only on a vanishing fraction of voxels.
    assert np.abs(diff).max() <= 1
    assert (diff != 0).mean() < 0.001
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_radar3d_rain.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement**

```python
# brightsky/radar3d/rain.py
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
        nbins = dbz.shape[1]                      # higher tilts have fewer gates
        gate = self.gate[sel]
        v = dbz[self.ray[sel], np.minimum(gate, nbins - 1)]
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
        sub = geom.grid_cycle(tilts)
        view = vol[:, geom.row0:geom.row1, geom.col0:geom.col1]
        np.maximum(view, sub, out=view)
    return vol
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_radar3d_rain.py -q`
Expected: 4 passed. If `test_matches_reference_gridder` fails on the fraction, print `(diff != 0).mean()` and the max diff and look for a porting error before loosening anything — the reference logic must be reproduced, not approximated.

- [ ] **Step 6: Commit**

```bash
git add brightsky/radar3d/rain.py tests/test_radar3d_rain.py tests/data/radar3d/golden_isn_1150.u8.zlib
git commit -m "feat(radar3d): port the voxel-centric rain gridder with precomputed site index maps"
```

---

### Task 5: Frame store, settings, migration, conftest

**Files:**
- Create: `brightsky/radar3d/store.py`, `migrations/0021_radar3d.sql`
- Modify: `brightsky/settings.py` (after `POLLING_CRONTAB_MINUTE`), `tests/conftest.py` (`db` fixture cleanup)
- Test: `tests/test_radar3d_store.py`

**Interfaces:**
- Produces: `FrameStore(root)` with `.path(product, ts) -> Path`, `.write(product, ts, voxels) -> Path` (atomic), `.open(product, ts) -> np.memmap` (raises `FrameMissing`), `.crop(product, ts, crop: Crop, scale=1) -> np.ndarray[uint8]`, `.timestamps(product) -> list[datetime]`, `.delete_before(product, cutoff) -> list[datetime]`; `FrameMissing(LookupError)`; DB helpers `index_frame(conn, product, ts, path, sites)`, `indexed_timestamps(conn, product, since)`, `delete_index_before(conn, product, cutoff)`.
- Settings: `RADAR3D_DATA_DIR='.data/radar3d'`, `RADAR3D_SWEEPS_URL='https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/'`, `RADAR3D_SITES=[17 sites]`, `RADAR3D_POLL_INTERVAL=60`, `RADAR3D_CYCLE_TIMEOUT=420`, `RADAR3D_BACKFILL_MINUTES=70`, `RADAR3D_RETENTION_HOURS=3`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_radar3d_store.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_radar3d_store.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`migrations/0021_radar3d.sql`:

```sql
CREATE TABLE radar3d_frames (
  product     text NOT NULL,
  timestamp   timestamptz NOT NULL,
  path        text NOT NULL,
  sites       smallint,
  created_at  timestamptz NOT NULL DEFAULT current_timestamp,

  CONSTRAINT radar3d_frames_key UNIQUE (product, timestamp)
);
```

`brightsky/settings.py`, after `POLLING_CRONTAB_MINUTE = '*'`:

```python
# nano radar3d (docs/nano/architecture.md)
RADAR3D_BACKFILL_MINUTES = 70
RADAR3D_CYCLE_TIMEOUT = 420
RADAR3D_DATA_DIR = '.data/radar3d'
RADAR3D_POLL_INTERVAL = 60
RADAR3D_RETENTION_HOURS = 3
RADAR3D_SITES = [
    'asb', 'boo', 'drs', 'eis', 'ess', 'fbg', 'fld', 'hnr', 'isn', 'mem',
    'neu', 'nhb', 'oft', 'pro', 'ros', 'tur', 'umd',
]
RADAR3D_SWEEPS_URL = (
    'https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/')
```

`brightsky/radar3d/store.py`:

```python
"""
On-disk store of national radar3d volumes, one uncompressed `.npy` per
product and cycle under `RADAR3D_DATA_DIR/<product>/<YYYYMMDDTHHMMZ>.npy`.
Files are memory-mapped when served, so a viewport crop is a slice the OS
page cache serves. Postgres (`radar3d_frames`) only keeps the index.
"""
import datetime
import os
from pathlib import Path

import numpy as np


TS_FORMAT = '%Y%m%dT%H%MZ'


class FrameMissing(LookupError):
    pass


class FrameStore:

    def __init__(self, root):
        self.root = Path(root)

    def path(self, product, ts):
        return self.root / product / f'{ts:{TS_FORMAT}}.npy'

    def write(self, product, ts, voxels):
        path = self.path(product, ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'wb') as f:
            np.save(f, np.ascontiguousarray(voxels, dtype=np.uint8))
        os.replace(tmp, path)
        return path

    def open(self, product, ts):
        path = self.path(product, ts)
        if not path.is_file():
            raise FrameMissing(f'No {product} frame for {ts:%Y-%m-%dT%H:%MZ}')
        return np.load(path, mmap_mode='r')

    def crop(self, product, ts, crop, scale=1):
        vol = self.open(product, ts)
        sub = np.asarray(vol[:, crop.row0:crop.row1, crop.col0:crop.col1])
        if scale > 1:
            levels, height, width = sub.shape
            sub = sub.reshape(
                levels, height // scale, scale, width // scale, scale,
            ).max(axis=(2, 4))
        return sub

    def timestamps(self, product):
        out = []
        for path in (self.root / product).glob('*.npy'):
            try:
                ts = datetime.datetime.strptime(path.stem, TS_FORMAT)
            except ValueError:
                continue
            out.append(ts.replace(tzinfo=datetime.UTC))
        return sorted(out)

    def delete_before(self, product, cutoff):
        deleted = []
        for ts in self.timestamps(product):
            if ts < cutoff:
                self.path(product, ts).unlink(missing_ok=True)
                deleted.append(ts)
        return deleted


def index_frame(conn, product, ts, path, sites):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO radar3d_frames (product, timestamp, path, sites)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT radar3d_frames_key DO UPDATE SET
              path = EXCLUDED.path,
              sites = EXCLUDED.sites,
              created_at = current_timestamp
            """,
            (product, ts, str(path), sites),
        )
    conn.commit()


def indexed_timestamps(conn, product, since):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp FROM radar3d_frames
            WHERE product = %s AND timestamp >= %s
            """,
            (product, since),
        )
        return {row[0] for row in cur.fetchall()}


def delete_index_before(conn, product, cutoff):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM radar3d_frames WHERE product = %s AND timestamp < %s",
            (product, cutoff),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted
```

`tests/conftest.py`: add `DELETE FROM radar3d_frames;` as the first statement of the `db` fixture's cleanup block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_radar3d_store.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add brightsky/radar3d/store.py migrations/0021_radar3d.sql brightsky/settings.py tests/conftest.py tests/test_radar3d_store.py
git commit -m "feat(radar3d): add frame store, index table and settings"
```

---

### Task 6: Ingest worker (listing, download, cycle tracking, gridding, retention) + CLI

**Files:**
- Create: `brightsky/radar3d/ingest.py`
- Modify: `brightsky/cli.py` (append two commands)
- Test: `tests/test_radar3d_ingest.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `SweepSource(base_url, session=None)` with `.list_site(site) -> list[(SweepInfo, url)]`, `.download(url, dest: Path)`; `Radar3DIngest(store, raw_dir, grid=GERMANY_1KM, sites=settings.RADAR3D_SITES, source=None, index=None, now=None)` with `.poll_once()`, `.process_cycle(cycle)`, `.clean()`, `.geometry(site, meta)`; `run_forever()`; CLI `radar3d-work`, `radar3d-grid DIRECTORY`.
- `index` is a callable `(ts, path, sites) -> None` and `indexed` a callable `(since) -> set[datetime]`; defaults talk to Postgres via `brightsky.db.get_connection`. Tests inject lists.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_radar3d_ingest.py
import datetime
import math
import shutil

import numpy as np
import pytest

from brightsky.radar3d.grid import Grid
from brightsky.radar3d.ingest import Radar3DIngest, SweepSource
from brightsky.radar3d.store import FrameStore
from brightsky.radar3d.sweeps import parse_sweep_name


CYCLE = datetime.datetime(2026, 9, 16, 11, 50, tzinfo=datetime.UTC)
LISTING = """
<html><body><h1>Index of /x/</h1><hr><pre><a href="../">../</a>
<a href="ras07-vol5minng01_sweeph5onem_dbzh_00-2026091611505700-isn-10873-hd5">ras07-...</a> 16-Sep-2026 11:51:20               62576
<a href="ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn-10873-hd5">ras07-...</a> 16-Sep-2026 11:54:30               53973
<a href="Beschreibung.pdf">Beschreibung.pdf</a> 01-Jan-2026 00:00 1234
</pre><hr></body></html>
"""


def test_source_parses_listing():
    source = SweepSource('https://example.test/sweep_vol_z/')
    sweeps = source.parse_listing(
        'isn', 'https://example.test/sweep_vol_z/isn/hdf5/'
        'filter_polarimetric/', LISTING)
    assert [(s.tilt, url.rsplit('/', 1)[1][:40]) for s, url in sweeps] == [
        (0, 'ras07-vol5minng01_sweeph5onem_dbzh_00-20'),
        (9, 'ras07-vol5minng01_sweeph5onem_dbzh_09-20'),
    ]
    assert sweeps[0][1].startswith(
        'https://example.test/sweep_vol_z/isn/hdf5/filter_polarimetric/')


class FakeSource:
    """Serves the isn fixture files, optionally holding some back."""

    def __init__(self, sweep_dir, hold=()):
        self.files = {p.name: p for p in sweep_dir.glob('*-hd5')}
        self.hold = set(hold)
        self.listings = 0

    def list_site(self, site):
        self.listings += 1
        return [
            (parse_sweep_name(name), f'fake://{name}')
            for name in sorted(self.files) if name not in self.hold
            and parse_sweep_name(name).site == site
        ]

    def download(self, url, dest):
        shutil.copy(self.files[url.removeprefix('fake://')], dest)


@pytest.fixture
def small_grid():
    clat, clon = 48.174705, 12.101779
    dlat = 50.0 / 111.195
    dlon = 50.0 / (111.195 * math.cos(math.radians(clat)))
    return Grid(clat - dlat, clat + dlat, clon - dlon, clon + dlon, 100, 100)


@pytest.fixture
def ingest(tmp_path, data_dir, small_grid):
    def make(hold=(), now=CYCLE + datetime.timedelta(minutes=5)):
        indexed = []
        source = FakeSource(data_dir / 'radar3d', hold)
        ing = Radar3DIngest(
            FrameStore(tmp_path / 'frames'), tmp_path / 'raw',
            grid=small_grid, sites=['isn'], source=source,
            index=lambda ts, path, sites: indexed.append((ts, sites)),
            indexed=lambda since: set(),
            now=lambda: now,
        )
        ing.settings = dict(
            poll_interval=60, cycle_timeout=420, backfill_minutes=70,
            retention_hours=3)
        return ing, source, indexed
    return make


def test_complete_cycle_is_gridded_and_indexed(ingest, tmp_path):
    ing, source, indexed = ingest()
    ing.poll_once()
    assert indexed == [(CYCLE, 1)]
    vol = np.load(tmp_path / 'frames' / 'rain' / '20260916T1150Z.npy')
    assert vol.shape == (24, 100, 100) and vol.max() > 150
    assert not (tmp_path / 'raw' / '20260916T1150Z').exists()
    # A second poll neither re-downloads nor re-grids
    ing.poll_once()
    assert indexed == [(CYCLE, 1)]


def test_incomplete_cycle_waits_then_times_out(ingest):
    hold = ['ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn'
            '-10873-hd5']
    ing, source, indexed = ingest(hold=hold)
    ing.poll_once()
    assert indexed == []                     # 9 of 10 tilts: not yet
    ing.now = lambda: CYCLE + datetime.timedelta(seconds=421)
    ing.poll_once()
    assert indexed == [(CYCLE, 1)]           # timed out: grid what we have


def test_old_cycles_are_ignored_and_cleaned(ingest, tmp_path):
    ing, source, indexed = ingest(now=CYCLE + datetime.timedelta(hours=4))
    ing.poll_once()
    assert indexed == []                     # older than the backfill window
    store = ing.store
    store.write('rain', CYCLE, np.zeros((24, 100, 100), np.uint8))
    ing.clean()
    assert store.timestamps('rain') == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_radar3d_ingest.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# brightsky/radar3d/ingest.py
"""
The radar3d worker: polls the 17 sweep_vol_z site listings, downloads
new sweeps of recent cycles, grids a cycle once every site has delivered
all tilts (or the cycle timed out), writes the national volume to the
frame store, indexes it, and expires old frames. Runs in its own container
(`python -m brightsky radar3d-work`) — the huey worker is untouched.
"""
import datetime
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from parsel import Selector

from brightsky.db import get_connection
from brightsky.radar3d.grid import GERMANY_1KM
from brightsky.radar3d.rain import SiteGeometry, grid_rain
from brightsky.radar3d.store import (
    FrameStore,
    delete_index_before,
    index_frame,
    indexed_timestamps,
)
from brightsky.radar3d.sweeps import (
    parse_sweep_name,
    read_site_meta,
    read_tilt,
)
from brightsky.settings import settings
from brightsky.utils import USER_AGENT


logger = logging.getLogger(__name__)

TILTS_PER_SITE = 10
PRODUCT = 'rain'


def utcnow():
    return datetime.datetime.now(datetime.UTC)


class SweepSource:
    """The DWD open data server: directory listings and sweep downloads."""

    def __init__(self, base_url, session=None):
        self.base_url = base_url
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT

    def site_url(self, site):
        return f'{self.base_url}{site}/hdf5/filter_polarimetric/'

    def list_site(self, site):
        url = self.site_url(site)
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        return self.parse_listing(site, url, resp.text)

    def parse_listing(self, site, url, text):
        sweeps = []
        for href in Selector(text).css('a::attr(href)').extract():
            info = parse_sweep_name(href)
            if info and info.site == site:
                sweeps.append((info, f'{url}{href}'))
        return sweeps

    def download(self, url, dest):
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        tmp = dest.with_name(dest.name + '.part')
        tmp.write_bytes(resp.content)
        tmp.replace(dest)


class Radar3DIngest:

    def __init__(self, store, raw_dir, grid=GERMANY_1KM, sites=None,
                 source=None, index=None, indexed=None, now=None):
        self.store = store
        self.raw_dir = Path(raw_dir)
        self.grid = grid
        self.sites = list(sites or settings.RADAR3D_SITES)
        self.source = source or SweepSource(settings.RADAR3D_SWEEPS_URL)
        self.index = index or self._index_db
        self.indexed = indexed or self._indexed_db
        self.now = now or utcnow
        self.settings = dict(
            poll_interval=settings.RADAR3D_POLL_INTERVAL,
            cycle_timeout=settings.RADAR3D_CYCLE_TIMEOUT,
            backfill_minutes=settings.RADAR3D_BACKFILL_MINUTES,
            retention_hours=settings.RADAR3D_RETENTION_HOURS,
        )
        self.geometries = {}
        self.done = set()

    # -- Postgres index (default) -------------------------------------

    def _index_db(self, ts, path, sites):
        with get_connection() as conn:
            index_frame(conn, PRODUCT, ts, path, sites)

    def _indexed_db(self, since):
        with get_connection() as conn:
            return indexed_timestamps(conn, PRODUCT, since)

    # -- cycle bookkeeping --------------------------------------------

    def cycle_dir(self, cycle):
        return self.raw_dir / f'{cycle:%Y%m%dT%H%MZ}'

    def _cycle_from_dir(self, path):
        try:
            ts = datetime.datetime.strptime(path.name, '%Y%m%dT%H%MZ')
        except ValueError:
            return None
        return ts.replace(tzinfo=datetime.UTC)

    def pending_cycles(self):
        cycles = []
        if self.raw_dir.is_dir():
            for path in self.raw_dir.iterdir():
                cycle = self._cycle_from_dir(path)
                if cycle is not None and cycle not in self.done:
                    cycles.append(cycle)
        return sorted(cycles)

    def cycle_files(self, cycle):
        """{(site, tilt): path} of the sweeps downloaded for a cycle."""
        files = {}
        for path in self.cycle_dir(cycle).glob('*-hd5'):
            info = parse_sweep_name(path.name)
            if info:
                files[(info.site, info.tilt)] = path
        return files

    def is_complete(self, files):
        return all(
            sum(1 for site, _ in files if site == s) >= TILTS_PER_SITE
            for s in self.sites
        )

    # -- polling ------------------------------------------------------

    def poll_once(self):
        now = self.now()
        since = now - datetime.timedelta(
            minutes=self.settings['backfill_minutes'])
        self.done |= self.indexed(since)
        with ThreadPoolExecutor(max_workers=8) as pool:
            listings = list(pool.map(self._safe_list, self.sites))
            downloads = []
            for site, sweeps in zip(self.sites, listings):
                for info, url in sweeps:
                    if info.cycle < since or info.cycle in self.done:
                        continue
                    dest = self.cycle_dir(info.cycle) / info.name
                    if dest.exists():
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    downloads.append((url, dest))
            list(pool.map(self._safe_download, downloads))
        if downloads:
            logger.info('Downloaded %d new sweeps', len(downloads))
        for cycle in self.pending_cycles():
            files = self.cycle_files(cycle)
            age = (self.now() - cycle).total_seconds()
            if self.is_complete(files):
                self.process_cycle(cycle)
            elif age >= self.settings['cycle_timeout']:
                if files:
                    logger.warning(
                        'Cycle %s timed out with %d/%d sweeps',
                        cycle, len(files),
                        len(self.sites) * TILTS_PER_SITE)
                    self.process_cycle(cycle)
                else:
                    self._discard(cycle)

    def _safe_list(self, site):
        try:
            return self.source.list_site(site)
        except Exception:
            logger.exception('Listing %s failed', site)
            return []

    def _safe_download(self, item):
        url, dest = item
        try:
            self.source.download(url, dest)
        except Exception:
            logger.exception('Download of %s failed', url)

    # -- gridding -----------------------------------------------------

    def geometry(self, site, meta):
        geom = self.geometries.get(site)
        if geom is None or geom.meta != meta:
            started = time.monotonic()
            geom = SiteGeometry(self.grid, meta)
            self.geometries[site] = geom
            logger.info(
                'Precomputed %s geometry: %d voxels, %.0f MiB, %.1fs',
                site, geom.n, geom.nbytes / 2 ** 20,
                time.monotonic() - started)
        return geom

    def contributions(self, files):
        by_site = {}
        for (site, tilt), path in files.items():
            by_site.setdefault(site, []).append((tilt, path))
        for site, tilts in by_site.items():
            if len(tilts) < 2:
                continue
            lowest = max(tilts)[1]          # tilt 05 = 0.5°; 00 = 5.5°
            try:
                meta = read_site_meta(lowest, site)
                data = [read_tilt(path) for _, path in tilts]
            except Exception:
                logger.exception('Skipping %s: unreadable sweep', site)
                continue
            yield self.geometry(site, meta), data

    def process_cycle(self, cycle):
        started = time.monotonic()
        files = self.cycle_files(cycle)
        contributions = list(self.contributions(files))
        vol = grid_rain(self.grid, contributions)
        path = self.store.write(PRODUCT, cycle, vol)
        self.index(cycle, path, len(contributions))
        self.done.add(cycle)
        self._discard(cycle)
        logger.info(
            'Gridded %s from %d sites (%d sweeps) in %.1fs; %d voxels',
            cycle, len(contributions), len(files),
            time.monotonic() - started, int((vol > 0).sum()))

    def _discard(self, cycle):
        shutil.rmtree(self.cycle_dir(cycle), ignore_errors=True)
        self.done.add(cycle)

    # -- retention ----------------------------------------------------

    def clean(self):
        cutoff = self.now() - datetime.timedelta(
            hours=self.settings['retention_hours'])
        deleted = self.store.delete_before(PRODUCT, cutoff)
        if self.index is self._index_db:
            with get_connection() as conn:
                delete_index_before(conn, PRODUCT, cutoff)
        if deleted:
            logger.info('Deleted %d expired frames', len(deleted))
        for cycle in self.pending_cycles():
            if cycle < cutoff:
                self._discard(cycle)


def make_ingest(**kwargs):
    return Radar3DIngest(
        FrameStore(settings.RADAR3D_DATA_DIR),
        Path(settings.RADAR3D_DATA_DIR) / 'raw',
        **kwargs,
    )


def run_forever():
    ingest = make_ingest()
    logger.info('radar3d worker started; %d sites', len(ingest.sites))
    while True:
        started = time.monotonic()
        try:
            ingest.poll_once()
            ingest.clean()
        except Exception:
            logger.exception('radar3d poll failed')
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, ingest.settings['poll_interval'] - elapsed))


def grid_directory(directory):
    """Grid every complete cycle found in a directory of saved sweeps."""
    ingest = make_ingest()
    cycles = {}
    for path in Path(directory).glob('*-hd5'):
        info = parse_sweep_name(path.name)
        if info and info.site in ingest.sites:
            cycles.setdefault(info.cycle, []).append(path)
    for cycle in sorted(cycles):
        dest = ingest.cycle_dir(cycle)
        dest.mkdir(parents=True, exist_ok=True)
        for path in cycles[cycle]:
            shutil.copy(path, dest / path.name)
        ingest.process_cycle(cycle)
    return sorted(cycles)
```

`brightsky/cli.py`, append:

```python
@cli.command(name='radar3d-work')
def radar3d_work():
    """Start the nano radar3d worker (sweep polling + gridding)."""
    from brightsky.radar3d.ingest import run_forever
    run_forever()


@cli.command(name='radar3d-grid')
@click.argument('directory')
def radar3d_grid(directory):
    """Grid saved sweep_vol_z files from DIRECTORY into the frame store."""
    from brightsky.radar3d.ingest import grid_directory
    for cycle in grid_directory(directory):
        print(cycle.isoformat())
```

Check that `brightsky.utils` exports `USER_AGENT` (it is used by `download()`); if it is module-private, import the module and use `utils.USER_AGENT`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_radar3d_ingest.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add brightsky/radar3d/ingest.py brightsky/cli.py tests/test_radar3d_ingest.py
git commit -m "feat(radar3d): add the stand-alone sweep polling and gridding worker"
```

---

### Task 7: Manifest query and web routes

**Files:**
- Modify: `brightsky/query.py` (append), `brightsky/web/params.py` (append), `brightsky/web/models.py` (append), `brightsky/web/app.py` (imports + two routes after `/thermal_hazard`), `brightsky/web/intro.md` (one line)
- Test: `tests/test_web.py` (append)

**Interfaces:**
- Produces: `query.radar3d(conn, lat, lon, distance, from_date, to_date, resolution) -> dict`; `query.radar3d_frame_exists(conn, product, ts) -> bool`; params `Radar3DParams` (`lat`, `lon`, `distance`, `from_date` alias `from`, `to_date` alias `to`, `resolution`), `Radar3DFrameParams` (`bbox`, `resolution`); routes `GET /radar3d`, `GET /radar3d/rain/{timestamp}`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_web.py
from brightsky.radar3d import frame as radar3d_frame
from brightsky.radar3d.grid import GERMANY_1KM
from brightsky.radar3d.store import FrameStore, index_frame


RADAR3D_TS = datetime.datetime(2026, 9, 16, 12, 0, tzinfo=tzutc())


@pytest.fixture
def radar3d_data(db, tmp_path):
    store = FrameStore(tmp_path)
    vol = np.zeros(GERMANY_1KM.shape, np.uint8)
    crop = GERMANY_1KM.around(52.52, 13.41, 5000)
    vol[4, crop.row0:crop.row1, crop.col0:crop.col1] = 150
    for minutes in (0, 5, 10):
        ts = RADAR3D_TS + datetime.timedelta(minutes=minutes)
        index_frame(db.conn, 'rain', ts, store.write('rain', ts, vol), 17)
    with settings(RADAR3D_DATA_DIR=str(tmp_path)):
        yield store


def test_radar3d_manifest(radar3d_data, api):
    resp = api.get('/radar3d?lat=52.52&lon=13.41&distance=20000')
    assert resp.status_code == 200
    data = resp.json()
    grid = data['grid']
    assert grid['levels'] == 24 and grid['level_m'] == 500.0
    assert grid['channels'] == 1 and grid['resolution'] == 1000
    assert grid['encoding'] == {
        'scale': 0.5, 'offset': -32.0, 'nodata': 0, 'unit': 'dBZ'}
    assert grid['min_lat'] < 52.52 < grid['max_lat']
    assert grid['min_lon'] < 13.41 < grid['max_lon']
    assert 38 <= grid['width'] <= 42 and 44 <= grid['height'] <= 50
    assert [f['timestamp'] for f in data['frames']] == [
        '2026-09-16T12:00:00+00:00',
        '2026-09-16T12:05:00+00:00',
        '2026-09-16T12:10:00+00:00',
    ]
    f = data['frames'][0]
    assert f['clouds'] is None and f['cells'] is None
    assert f['rain'].startswith('/radar3d/rain/2026-09-16T12:00:00Z?bbox=')
    assert data['flows_clouds'] is False
    # Explicit window and the 2 km level of detail
    resp = api.get(
        '/radar3d?lat=52.52&lon=13.41&distance=20000'
        '&from=2026-09-16T12:05Z&to=2026-09-16T12:05Z&resolution=2000')
    assert [f['timestamp'] for f in resp.json()['frames']] == [
        '2026-09-16T12:05:00+00:00']
    assert resp.json()['grid']['resolution'] == 2000
    assert resp.json()['frames'][0]['rain'].endswith('&resolution=2000')
    # Validation and coverage
    assert api.get('/radar3d?lat=52.52').status_code == 422
    assert api.get('/radar3d?lat=52.52&lon=13.41&distance=300000') \
        .status_code == 422
    assert api.get('/radar3d?lat=40&lon=13.41').status_code == 404
    assert api.get(
        '/radar3d?lat=52.52&lon=13.41&from=2020-01-01&to=2020-01-02'
    ).json()['frames'] == []


def test_radar3d_rain_frame(radar3d_data, api):
    manifest = api.get('/radar3d?lat=52.52&lon=13.41&distance=20000').json()
    grid = manifest['grid']
    resp = api.get(manifest['frames'][0]['rain'])
    assert resp.status_code == 200
    assert resp.headers['content-type'] == 'application/octet-stream'
    assert 'immutable' in resp.headers['cache-control']
    header, voxels, extra = radar3d_frame.decode(resp.content)
    assert (header['width'], header['height'], header['levels']) == \
        (grid['width'], grid['height'], 24)
    assert extra == b''
    assert voxels[4].max() == 150 and voxels[3].max() == 0
    assert voxels[4].mean() < 150            # box smaller than the crop
    # 2 km: half the size, peak preserved
    manifest2 = api.get(
        '/radar3d?lat=52.52&lon=13.41&distance=20000&resolution=2000').json()
    header2, voxels2, _ = radar3d_frame.decode(
        api.get(manifest2['frames'][0]['rain']).content)
    assert header2['width'] * 2 == grid['width'] or \
        header2['width'] * 2 == grid['width'] + 2
    assert voxels2[4].max() == 150
    # Missing frame, bad timestamp, bad bbox
    assert api.get(
        '/radar3d/rain/2026-09-16T13:00:00Z?bbox=52,53,13,14').status_code \
        == 404
    assert api.get('/radar3d/rain/yesterday?bbox=52,53,13,14').status_code \
        == 422
    assert api.get('/radar3d/rain/2026-09-16T12:00:00Z?bbox=1,2,3') \
        .status_code == 422
    assert api.get('/radar3d/rain/2026-09-16T12:00:00Z?bbox=40,41,13,14') \
        .status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `BRIGHTSKY_DATABASE_URL=<test db url> .venv/bin/python -m pytest tests/test_web.py -q -k radar3d`
Expected: FAIL (404 on `/radar3d`, import errors). Without a database the tests skip — see "Verification" below.

- [ ] **Step 3: Implement**

`brightsky/query.py`, append:

```python
RADAR3D_ENCODING = {
    'scale': 0.5, 'offset': -32.0, 'nodata': 0, 'unit': 'dBZ'}
RADAR3D_SOURCE = (
    'Deutscher Wetterdienst, sweep_vol_z volume scans (17 sites)')


async def radar3d(
    conn, lat, lon, distance=100000, from_date=None, to_date=None,
    resolution=1000,
):
    from brightsky.radar3d.grid import GERMANY_1KM, OutsideGrid
    grid = GERMANY_1KM
    scale = resolution // 1000
    try:
        crop = grid.around(lat, lon, distance, align=scale)
    except OutsideGrid:
        raise NoData("lat/lon lies outside the radar3d coverage")
    if to_date is None:
        to_date = await conn.fetchval(
            "SELECT MAX(timestamp) FROM radar3d_frames WHERE product = 'rain'"
        )
        if to_date is None:
            raise NoData("No radar3d frames are available yet")
    if from_date is None:
        from_date = to_date - datetime.timedelta(minutes=55)
    rows = await conn.fetch(
        """
        SELECT timestamp, sites FROM radar3d_frames
        WHERE product = 'rain' AND timestamp BETWEEN $1 AND $2
        ORDER BY timestamp
        """,
        from_date, to_date,
    )
    min_lat, max_lat, min_lon, max_lon = grid.bounds(crop)
    query = f'?bbox={min_lat:.6f},{max_lat:.6f},{min_lon:.6f},{max_lon:.6f}'
    if scale > 1:
        query += f'&resolution={resolution}'
    return {
        'grid': {
            'width': crop.width // scale,
            'height': crop.height // scale,
            'levels': grid.levels,
            'level_m': grid.level_m,
            'base_m': grid.base_m,
            'min_lat': round(min_lat, 6),
            'max_lat': round(max_lat, 6),
            'min_lon': round(min_lon, 6),
            'max_lon': round(max_lon, 6),
            'encoding': RADAR3D_ENCODING,
            'channels': 1,
            'resolution': resolution,
        },
        'frames': [
            {
                'timestamp': row['timestamp'],
                'sites': row['sites'],
                'rain': f"/radar3d/rain/{row['timestamp']:%Y-%m-%dT%H:%M:%SZ}"
                        f"{query}",
                'clouds': None,
                'cells': None,
            }
            for row in rows
        ],
        'flows_clouds': False,
        'source': RADAR3D_SOURCE,
    }


async def radar3d_frame_exists(conn, product, timestamp):
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM radar3d_frames WHERE product = $1 AND timestamp = $2
        """,
        product, timestamp,
    ))
```

`brightsky/web/params.py`, append:

```python
class Radar3DResolution(BaseModel):
    resolution: Literal[1000, 2000] = Field(
        default=1000,
        description="Horizontal voxel size in metres. `2000` is the 1 km grid max-pooled 2×2.",  # noqa
        examples=[1000],
    )


class Radar3DParams(
    Radar3DResolution,
    LatLon,
):
    distance: int = Field(
        default=100000,
        ge=1000,
        description="Half-width of the crop in metres: data reaches this far to each side of `lat`/`lon`, cut off at the edges of the national grid. At most 250000 at 1 km resolution and 600000 at 2 km.",  # noqa
        examples=[100000],
    )
    from_date: Annotated[
        datetime.datetime,
        Query(alias='from'),
    ] = Field(
        default=None,
        description="Timestamp of the first frame, ISO 8601. (_Defaults to 55 minutes before `to`._)",  # noqa
        examples=["2026-09-16T11:05:00Z"],
    )
    to_date: Annotated[
        datetime.datetime,
        Query(alias='to'),
    ] = Field(
        default=None,
        description="Timestamp of the last frame, ISO 8601. (_Defaults to the latest available frame._)",  # noqa
        examples=["2026-09-16T12:00:00Z"],
    )

    @model_validator(mode='after')
    def validate_position_and_distance(self):
        if self.lat is None or self.lon is None:
            raise ValueError("Please supply lat & lon")
        limit = 250000 if self.resolution == 1000 else 600000
        if self.distance > limit:
            raise ValueError(
                f"distance must not exceed {limit} m at {self.resolution} m "
                f"resolution")
        return self

    @field_validator('from_date', 'to_date', mode='after')
    @classmethod
    def ensure_tzinfo(cls, value):
        if value is None or value.tzinfo:
            return value
        return value.replace(tzinfo=datetime.UTC)

    @model_validator(mode='after')
    def validate_window(self):
        if self.from_date and self.to_date:
            if self.to_date < self.from_date:
                raise ValueError("'to' must not lie before 'from'")
            if self.to_date - self.from_date > datetime.timedelta(hours=3):
                raise ValueError("The window must not exceed 3 hours")
        return self


class Radar3DFrameParams(Radar3DResolution):
    bbox: list[float] = Field(
        description="Crop bounds `minLat,maxLat,minLon,maxLon` in decimal degrees, as given in the manifest's frame URLs.",  # noqa
        examples=["52.3,52.7,13.1,13.7"],
    )

    @field_validator('bbox', mode='before')
    @classmethod
    def validate_bbox(cls, value):
        bbox = _split(value, converter=float)
        if len(bbox) != 4 or bbox[0] >= bbox[1] or bbox[2] >= bbox[3]:
            raise ValueError(
                "The 'bbox' parameter must be minLat,maxLat,minLon,maxLon")
        return bbox
```

`brightsky/web/models.py`, append:

```python
class Radar3DGrid(ResponseModel):
    width: int = Field(description="Voxel columns of the crop")
    height: int = Field(description="Voxel rows of the crop (row 0 = north)")
    levels: int = Field(description="Vertical slabs", examples=[24])
    level_m: float = Field(description="Slab thickness in metres", examples=[500.0])  # noqa
    base_m: float = Field(description="Height above sea level of the lowest slab's bottom, in metres", examples=[0.0])  # noqa
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    encoding: dict = Field(
        description="How to decode a voxel byte: `value × scale + offset`, `nodata` = no echo",  # noqa
        examples=[{'scale': 0.5, 'offset': -32.0, 'nodata': 0, 'unit': 'dBZ'}],  # noqa
    )
    channels: int = Field(description="Bytes per voxel", examples=[1])
    resolution: int = Field(description="Horizontal voxel size in metres", examples=[1000])  # noqa


class Radar3DFrame(ResponseModel):
    timestamp: datetime.datetime = Field(
        description="Start of the 5-minute volume scan cycle (UTC)",
        examples=["2026-09-16T12:00:00+00:00"],
    )
    sites: int = Field(
        description="Number of radar sites that contributed to this frame",
        examples=[17],
        json_schema_extra={'nullable': True},
    )
    rain: str = Field(
        description="URL of the binary reflectivity frame for this crop",
        examples=["/radar3d/rain/2026-09-16T12:00:00Z?bbox=52.3,52.7,13.1,13.7"],  # noqa
    )
    clouds: str = Field(
        default=None,
        description="URL of the cloud volume frame (phase 2; `null` for now)",
        json_schema_extra={'nullable': True},
    )
    cells: str = Field(
        default=None,
        description="URL of the convective cells JSON (phase 2; `null` for now)",  # noqa
        json_schema_extra={'nullable': True},
    )


class Radar3DResponse(ResponseModel):
    grid: Radar3DGrid
    frames: list[Radar3DFrame]
    flows_clouds: bool = Field(
        description="Whether cloud frames carry a flow field (phase 2)")
    source: str = Field(
        description="Attribution of the underlying DWD product",
        examples=["Deutscher Wetterdienst, sweep_vol_z volume scans (17 sites)"],  # noqa
    )
```

`brightsky/web/app.py`: add `Radar3DResponse` to the models import, `Radar3DFrameParams, Radar3DParams` to the params import, `from starlette.concurrency import run_in_threadpool` to the imports, and append after the `/thermal_hazard` route:

```python
@app.get(
    '/radar3d',
    operation_id='getRadar3D',
    summary='Radar 3D (nano)',
    responses=common_responses,
)
async def radar3d(
    q: Annotated[Radar3DParams, Query()],
) -> Radar3DResponse:
    """
    Manifest of the nano 3D radar products: a voxel grid (regular in Web
    Mercator between the returned bounds, row 0 = north; `levels` slabs of
    `level_m` metres from `base_m` above sea level) around `lat`/`lon`, and
    one entry per 5-minute frame with the URLs of the binary crops.

    Frames are `NANO3D` binaries: a 32-byte little-endian header (magic
    `NANO3D`, u16 version, width, height, levels, channels, u32 zlib
    payload length, u32 extra block length, 8 reserved bytes), then the
    zlib-compressed level-major voxel bytes. Rain voxels are one byte,
    `dBZ = v × 0.5 − 32`, `0` = no echo. Frames are immutable and cached
    for a day.

    Data: DWD `sweep_vol_z` volume scans of all 17 German radar sites,
    gridded to 1 km × 500 m (CC BY 4.0, Deutscher Wetterdienst).
    """
    result = await query.radar3d(
        ctx['pool'],
        lat=q.lat,
        lon=q.lon,
        distance=q.distance,
        from_date=q.from_date,
        to_date=q.to_date,
        resolution=q.resolution,
    )
    return ORJSONResponse(result)


def _parse_frame_timestamp(value):
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Invalid ISO 8601 timestamp")
    return ts if ts.tzinfo else ts.replace(tzinfo=datetime.UTC)


def _rain_crop_bytes(timestamp, bbox, resolution):
    from brightsky.radar3d import frame
    from brightsky.radar3d.grid import GERMANY_1KM, OutsideGrid
    from brightsky.radar3d.store import FrameMissing, FrameStore
    scale = resolution // 1000
    try:
        crop = GERMANY_1KM.crop(*bbox, align=scale)
        voxels = FrameStore(settings.RADAR3D_DATA_DIR).crop(
            'rain', timestamp, crop, scale)
    except (OutsideGrid, FrameMissing) as e:
        raise query.NoData(str(e))
    return frame.encode(voxels)


@app.get(
    '/radar3d/rain/{timestamp}',
    operation_id='getRadar3DRain',
    summary='Radar 3D rain frame (nano)',
    responses=common_responses,
    response_class=Response,
)
async def radar3d_rain(
    timestamp: str,
    q: Annotated[Radar3DFrameParams, Query()],
    response: Response,
):
    """
    One reflectivity frame, cropped to `bbox`, as a `NANO3D` binary (see
    [`/radar3d`](/operations/getRadar3D)). Use the URLs from the manifest.
    """
    ts = _parse_frame_timestamp(timestamp)
    if not await query.radar3d_frame_exists(ctx['pool'], 'rain', ts):
        raise query.NoData(f"No rain frame for {timestamp}")
    data = await run_in_threadpool(
        _rain_crop_bytes, ts, q.bbox, q.resolution)
    return Response(
        content=data,
        media_type='application/octet-stream',
        headers={
            'Cache-Control': 'public, max-age=86400, immutable',
            # Prevent traefik from gzipping the pre-compressed content
            'Content-Encoding': 'identity',
        },
    )
```

Add `import datetime` to app.py's imports. In `brightsky/web/intro.md`, extend the nano line listing the health endpoints with `/radar3d`.

- [ ] **Step 4: Run tests and lint**

Run: `BRIGHTSKY_DATABASE_URL=<test db url> .venv/bin/python -m pytest tests/test_web.py -q -k radar3d && .venv/bin/ruff check .`
Expected: 2 passed, ruff clean

- [ ] **Step 5: Commit**

```bash
git add brightsky/query.py brightsky/web/params.py brightsky/web/models.py brightsky/web/app.py brightsky/web/intro.md tests/test_web.py
git commit -m "feat(radar3d): add /radar3d manifest and /radar3d/rain/{ts} crop endpoints"
```

---

### Task 8: Container wiring, docs, worklog

**Files:**
- Modify: `docker-compose.yml`, `docs/nano/README.md`, `docs/nano/architecture.md`, `docs/nano/deployment.md`, `docs/nano/worklog.md`

- [ ] **Step 1: docker-compose.yml**

Add to the `x-brightsky` anchor a `volumes:` list with `- .data/radar3d:/app/.data/radar3d` and add the service:

```yaml
  radar3d:
    <<: *brightsky
    command: --migrate radar3d-work
    restart: unless-stopped
```

(`web` reads the same bind mount through the anchor.)

- [ ] **Step 2: Docs**

- `docs/nano/README.md`: a "Radar 3D (nano)" section pointing at the architecture section, the brief's location, and the worker container.
- `docs/nano/architecture.md`: a "Radar 3D pipeline (phase 1: rain)" section — stations (poll → download → cycle → grid → store → index → web), the grid definition, the header layout, memory/CPU numbers measured in Task 9, retention, settings.
- `docs/nano/deployment.md`: a "radar3d container" subsection with the compose overlay lines for `bright_sky_config` (`radar3d` service with `command: --migrate radar3d-work`, the `.data/radar3d` bind mount on both `radar3d` and `web`, expected disk ~600 MB and RAM ~0.5 GB) — **not executed**, the deploy stays a `bright_sky_config` task.
- `docs/nano/worklog.md`: dated entry "2026-09-17 — radar3d phase 1: manifest + rain crops" with what was built, measured numbers, deviations, open questions (listing traffic 17 MB/min, memmap fallback for geometry, `pro` gaps).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml docs/nano
git commit -m "docs(radar3d): document phase 1 pipeline, container and deployment overlay"
```

---

### Task 9: Verification against real data and the local endpoint for the app session

- [ ] **Step 1: Full test suite + lint** with the embedded Postgres from the scratchpad (`pgserver`), `BRIGHTSKY_DATABASE_URL=<uri>/brightsky_test`:

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check .`
Expected: all green (the pre-existing suite plus the new modules).

- [ ] **Step 2: Grid a real national cycle from the saved sweeps** (17 sites, 2026-09-16 11:00–11:29 in the app session's scratchpad `volfixture/raw_de/`) with `radar3d-grid`, timing it and logging geometry memory:

```bash
export BRIGHTSKY_DATABASE_URL=<uri>/brightsky_smoke
export BRIGHTSKY_RADAR3D_DATA_DIR=<scratchpad>/radar3d-data
.venv/bin/python -m brightsky migrate
time .venv/bin/python -m brightsky radar3d-grid <raw_de dir>
```
Expected: geometry precompute ≤ ~2 s per site, total < 500 MiB; each cycle gridded in well under 60 s; frames of 15.3 MB each in the store.

- [ ] **Step 3: Compare a national crop against the app session's `radar3d-de-2026-09-16.json`** (their 2 km fixture, same bounds): decode both for 11:10 UTC and check that echo positions agree (correlation of level-4 maxima, top-of-echo heights) — a sanity check, not byte equality (different sampling).

- [ ] **Step 4: Run the live worker and the web server locally** in the background: `radar3d-work` (polls DWD, backfills the last 70 min) and `serve --bind 127.0.0.1:5599`; verify `curl 'http://127.0.0.1:5599/radar3d?lat=48.17&lon=12.10&distance=50000'` and one frame URL decode.

- [ ] **Step 5: Message `weathergermany-f2`** with the URL, the manifest example, the header layout and the deviations list above.
