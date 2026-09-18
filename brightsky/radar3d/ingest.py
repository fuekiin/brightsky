"""
The radar3d worker (`python -m brightsky radar3d-work`), one container next
to the huey worker, three products on one 5-minute timeline:

* rain — the 17 sweep_vol_z sites, gridded per cycle (`rain.py`);
* clouds — ICON-D2 runs loaded in a background thread (`icon.py`) and
  turned into a frame for every rain stamp (`clouds.py`);
* cells — KONRAD3D XML per stamp (`cells.py`).

Sweep discovery learns each site's per-tilt schedule from one directory
listing and afterwards fetches the predicted file names directly (a site
listing is ~1 MB, so listing 17 sites every minute would cost 17 MB/min);
a listing is only taken again when a predicted file is overdue, or every
`RADAR3D_LISTING_INTERVAL` seconds per site (staggered).
"""
import datetime
import logging
import os
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from parsel import Selector

from brightsky.db import get_connection
from brightsky.radar3d import cells as konrad
from brightsky.radar3d import nowcast
from brightsky.radar3d.clouds import CloudModel, quantise
from brightsky.radar3d.grid import GERMANY_1KM, GERMANY_2KM
from brightsky.radar3d.icon import (
    Sampler,
    choose_levels,
    hydrometeors_to_z,
    grib_name,
    level_heights,
    load_step,
    wind_levels,
)
from brightsky.radar3d.rain import SiteGeometry, grid_rain
from brightsky.radar3d.store import (
    FrameStore,
    delete_index_before,
    index_frame,
    indexed_products,
    indexed_timestamps,
)
from brightsky.radar3d.sweeps import (
    SWEEP_NAME,
    parse_sweep_name,
    read_site_meta,
    read_tilt,
)
from brightsky.settings import settings
from brightsky.utils import USER_AGENT


logger = logging.getLogger(__name__)

TILTS_PER_SITE = 10
CYCLE_DIR_FORMAT = '%Y%m%dT%H%MZ'
CYCLE = datetime.timedelta(minutes=5)
PRODUCTS = ('rain', 'rain_flow', 'clouds', 'clouds_flow', 'cells')
FORECAST_PRODUCTS = ('forecast_rain', 'forecast_clouds', 'forecast_flow')
RUN_FORMAT = '%Y%m%dT%HZ'


BASIS_FORMAT = '%Y%m%dT%H%MZ'


def forecast_product(product, run, basis=None):
    """Forecast frames are keyed by the model run and the observation they
    extrapolate: product 'forecast_rain/<run>-<basis>' (URLs stay
    immutable; a new cycle writes a new key)."""
    key = f'{run:{RUN_FORMAT}}'
    if basis is not None:
        key += f'-{basis:{BASIS_FORMAT}}'
    return f'{product}/{key}'


def parse_forecast_key(key):
    """'<run>-<basis>' (or a bare '<run>') → (run, basis or None)."""
    run_part, _, basis_part = key.partition('-')
    run = datetime.datetime.strptime(run_part, RUN_FORMAT).replace(
        tzinfo=datetime.UTC)
    basis = None
    if basis_part:
        basis = datetime.datetime.strptime(basis_part, BASIS_FORMAT).replace(
            tzinfo=datetime.UTC)
    return run, basis
# Predicted sweep fetches: start this long after the learned time, give up
# (and take a listing) this long after it
PREDICT_AFTER = 20
OVERDUE_AFTER = 120
FORCED_LISTING_GAP = 300
ICON_RUN_HOURS = (0, 3, 6, 9, 12, 15, 18, 21)
ICON_AVAILABLE_AFTER = datetime.timedelta(minutes=45)
ICON_KEEP = datetime.timedelta(hours=6)


def utcnow():
    return datetime.datetime.now(datetime.UTC)


def floor_cycle(ts):
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0,
                      microsecond=0)


class HttpSource:

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT

    def fetch(self, url, dest):
        """GET `url` into `dest` atomically; False if it does not exist."""
        resp = self.session.get(url, timeout=120)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + '.part')
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        return True

    def download(self, url, dest):
        if not self.fetch(url, dest):
            raise FileNotFoundError(url)

    def listing(self, url):
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        return resp.text


class SweepSource(HttpSource):
    """The sweep_vol_z site directories."""

    def __init__(self, base_url, session=None):
        super().__init__(session)
        self.base_url = base_url

    def site_url(self, site):
        return f'{self.base_url}{site}/hdf5/filter_polarimetric/'

    def list_site(self, site):
        url = self.site_url(site)
        return self.parse_listing(site, url, self.listing(url))

    def parse_listing(self, site, url, text):
        sweeps = []
        for href in Selector(text).css('a::attr(href)').extract():
            info = parse_sweep_name(href)
            if info and info.site == site:
                sweeps.append((info, f'{url}{href}'))
        return sweeps

    @staticmethod
    def predicted_name(site, tilt, wmo, timestamp):
        return (
            f'ras07-vol5minng01_sweeph5onem_dbzh_{tilt:02d}'
            f'-{timestamp:%Y%m%d%H%M%S}00-{site}-{wmo}-hd5'
        )

    def predicted_url(self, site, tilt, wmo, timestamp):
        return self.site_url(site) + self.predicted_name(
            site, tilt, wmo, timestamp)


class IconSource(HttpSource):
    """The ICON-D2 run directories."""

    def __init__(self, base_url, session=None):
        super().__init__(session)
        self.base_url = base_url

    def url(self, run, step, level, var):
        name = grib_name(run, step, level, var)
        return f'{self.base_url}{run:%H}/{var}/{name}'


class KonradSource(HttpSource):
    """The KONRAD3D directory: one XML per 5 minutes."""

    def __init__(self, base_url, session=None):
        super().__init__(session)
        self.base_url = base_url

    def list_files(self):
        out = []
        for href in Selector(self.listing(self.base_url)).css(
                'a::attr(href)').extract():
            ts = self.parse_name(href)
            if ts is not None:
                out.append((ts, f'{self.base_url}{href}'))
        return out

    @staticmethod
    def parse_name(name):
        if not (name.startswith('KONRAD3D_') and name.endswith('.xml')):
            return None
        try:
            return datetime.datetime.strptime(
                name[9:-4], '%Y%m%dT%H%M%S').replace(tzinfo=datetime.UTC)
        except ValueError:
            return None


class Radar3DIngest:

    def __init__(self, store, raw_dir, grid=GERMANY_1KM, sites=None,
                 source=None, icon_source=None, konrad_source=None,
                 index=None, indexed=None, now=None, cloud_grid=GERMANY_2KM):
        self.store = store
        self.raw_dir = Path(raw_dir)
        self.grid = grid
        self.cloud_grid = cloud_grid
        self.sites = list(sites or settings.RADAR3D_SITES)
        self.source = source or SweepSource(settings.RADAR3D_SWEEPS_URL)
        self.icon_source = icon_source
        self.konrad_source = konrad_source
        self.index = index or self._index_db
        self.indexed = indexed or self._indexed_db
        self.now = now or utcnow
        self.settings = dict(
            poll_interval=settings.RADAR3D_POLL_INTERVAL,
            cycle_timeout=settings.RADAR3D_CYCLE_TIMEOUT,
            backfill_minutes=settings.RADAR3D_BACKFILL_MINUTES,
            retention_hours=settings.RADAR3D_RETENTION_HOURS,
            listing_interval=settings.RADAR3D_LISTING_INTERVAL,
            icon_steps=settings.RADAR3D_ICON_STEPS,
            forecast_minutes=settings.RADAR3D_FORECAST_MINUTES,
            min_free_gb=settings.RADAR3D_MIN_FREE_GB,
        )
        self.geometries = {}
        self.done = set()
        # sweep schedule learning
        self.schedule = {}          # site → {tilt: (wmo, offset seconds)}
        self.listed_up_to = {}      # site → newest cycle a listing covered
        self.last_listing = {}      # site → datetime
        self.last_forced = {}       # site → datetime
        self.gave_up = set()        # (site, cycle, tilt)
        # clouds
        self.clouds = CloudModel(cloud_grid)
        self.clouds_lock = threading.Lock()
        self.sampler = Sampler(cloud_grid)
        self.full_h = None
        self.icon_loaded = set()
        self.icon_runs = {}          # run → [valid times loaded]
        self.icon_attempts = Counter()
        # cells
        self.cells_done = set()

    # -- Postgres index (default) -------------------------------------

    def _index_db(self, product, ts, path, sites):
        with get_connection() as conn:
            index_frame(conn, product, ts, path, sites)

    def _indexed_db(self, product, since):
        with get_connection() as conn:
            return indexed_timestamps(conn, product, since)

    # -- cycle bookkeeping --------------------------------------------

    def cycle_dir(self, cycle):
        return self.raw_dir / f'{cycle:{CYCLE_DIR_FORMAT}}'

    def _cycle_from_dir(self, path):
        try:
            ts = datetime.datetime.strptime(path.name, CYCLE_DIR_FORMAT)
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

    def window(self):
        now = self.now()
        since = now - datetime.timedelta(
            minutes=self.settings['backfill_minutes'])
        return now, since

    # -- disk guard ---------------------------------------------------

    def free_gb(self):
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        st = os.statvfs(self.raw_dir)
        return st.f_bavail * st.f_frsize / 2 ** 30

    def has_space(self, what):
        """Never be the process that fills the disk Postgres lives on."""
        free = self.free_gb()
        if free < self.settings['min_free_gb']:
            logger.error(
                'Only %.1f GB free (floor %.1f GB): skipping %s downloads',
                free, self.settings['min_free_gb'], what)
            return False
        return True

    # -- rain: polling ------------------------------------------------

    def poll_once(self):
        now, since = self.window()
        if not self.has_space('sweep'):
            self.clean()
            return
        self.done |= self.indexed('rain', since)
        with ThreadPoolExecutor(max_workers=8) as pool:
            listed = [s for s in self.sites if self.needs_listing(s, now)]
            listings = dict(zip(listed, pool.map(self._safe_list, listed)))
            downloads = []
            for site, sweeps in listings.items():
                self.last_listing[site] = now
                self.learn_schedule(site, sweeps)
                for info, url in sweeps:
                    dest = self._wanted(info, since)
                    if dest is not None:
                        downloads.append((url, dest))
            list(pool.map(self._safe_download, downloads))
            predicted = [
                item for site in self.sites if site not in listings
                for item in self.predicted_fetches(site, now, since)
            ]
            found = list(pool.map(self._safe_fetch, predicted))
        if downloads or predicted:
            logger.info(
                'Sweeps: %d from %d listings, %d of %d predicted',
                len(downloads), len(listings), sum(found), len(predicted))
        for cycle in self.pending_cycles():
            files = self.cycle_files(cycle)
            age = (self.now() - cycle).total_seconds()
            if self.is_complete(files):
                self._process_safely(cycle)
            elif age >= self.settings['cycle_timeout']:
                if files:
                    logger.warning(
                        'Cycle %s timed out with %d/%d sweeps',
                        cycle, len(files),
                        len(self.sites) * TILTS_PER_SITE)
                    self._process_safely(cycle)
                else:
                    self._discard(cycle)

    def _wanted(self, info, since):
        if info.cycle < since or info.cycle in self.done:
            return None
        dest = self.cycle_dir(info.cycle) / info.name
        if dest.exists():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def needs_listing(self, site, now):
        if site not in self.schedule:
            return True
        last = self.last_listing.get(site)
        stagger = self.sites.index(site) * self.settings['listing_interval'] \
            / max(len(self.sites), 1)
        due = last + datetime.timedelta(
            seconds=self.settings['listing_interval'] + stagger)
        if now >= due:
            return True
        return self._overdue(site, now)

    def learn_schedule(self, site, sweeps):
        """Per tilt: WMO id and seconds into the cycle of the newest file."""
        newest = {}
        for info, _ in sweeps:
            if info.tilt not in newest or \
                    info.timestamp > newest[info.tilt].timestamp:
                newest[info.tilt] = info
        if not newest:
            return
        schedule = {}
        for tilt, info in newest.items():
            wmo = SWEEP_NAME.match(info.name).group(0).rsplit('-', 2)[1]
            schedule[tilt] = (wmo, int((info.timestamp - info.cycle)
                                       .total_seconds()))
        self.schedule[site] = schedule
        # The listing is authoritative up to its newest complete cycle (or
        # the cycle before a partial one); prediction only bridges the gap
        # from there to now.
        per_cycle = Counter(info.cycle for info, _ in sweeps)
        complete = [c for c, n in per_cycle.items() if n >= TILTS_PER_SITE]
        if complete:
            covered = max(complete)
        else:
            covered = max(per_cycle) - CYCLE
        self.listed_up_to[site] = covered
        self.gave_up = {
            g for g in self.gave_up if g[0] != site or g[1] > covered}

    def _predictable(self, site, now, since):
        """(cycle, tilt, expected time) not yet downloaded, after the last
        listing's coverage."""
        schedule = self.schedule.get(site)
        covered = self.listed_up_to.get(site)
        if not schedule or covered is None:
            return []
        cycle = max(floor_cycle(since), covered + CYCLE)
        out = []
        while cycle <= floor_cycle(now):
            if cycle not in self.done:
                files = self.cycle_files(cycle)
                for tilt, (wmo, offset) in schedule.items():
                    if (site, tilt) in files or \
                            (site, cycle, tilt) in self.gave_up:
                        continue
                    expected = cycle + datetime.timedelta(seconds=offset)
                    out.append((cycle, tilt, wmo, expected))
            cycle += CYCLE
        return out

    def _overdue(self, site, now):
        now_, since = self.window()
        overdue = [
            (cycle, tilt) for cycle, tilt, _, expected
            in self._predictable(site, now, since)
            if (now - expected).total_seconds() > OVERDUE_AFTER
        ]
        if not overdue:
            return False
        last = self.last_forced.get(site)
        if last and (now - last).total_seconds() < FORCED_LISTING_GAP:
            # Already listed for this; stop waiting for these files
            for cycle, tilt in overdue:
                self.gave_up.add((site, cycle, tilt))
            return False
        self.last_forced[site] = now
        for cycle, tilt in overdue:
            self.gave_up.add((site, cycle, tilt))
        return True

    def predicted_fetches(self, site, now, since):
        items = []
        for cycle, tilt, wmo, expected in self._predictable(site, now, since):
            if (now - expected).total_seconds() < PREDICT_AFTER:
                continue
            urls = [
                self.source.predicted_url(
                    site, tilt, wmo, expected + datetime.timedelta(seconds=d))
                for d in (0, -1, 1)
            ]
            items.append((cycle, urls))
        return items

    def _safe_fetch(self, item):
        cycle, urls = item
        for url in urls:
            dest = self.cycle_dir(cycle) / url.rsplit('/', 1)[1]
            try:
                if self.source.fetch(url, dest):
                    return True
            except Exception:
                logger.exception('Fetch of %s failed', url)
                return False
        return False

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

    # -- rain: gridding -----------------------------------------------

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

    def contributions(self, files, counter):
        """Yields one site at a time so only one site's sweeps are in
        memory; `counter` receives the number of sites contributed."""
        by_site = {}
        for (site, tilt), path in files.items():
            by_site.setdefault(site, []).append((tilt, path))
        for site, tilts in by_site.items():
            if len(tilts) < 2:
                continue
            try:
                # Every tilt carries the site metadata; take the layout
                # most tilts agree on (a sweep occasionally has 361 rays)
                metas = [read_site_meta(path, site) for _, path in tilts]
                meta = Counter(metas).most_common(1)[0][0]
                data = [read_tilt(path) for _, path in tilts]
            except Exception:
                logger.exception('Skipping %s: unreadable sweep', site)
                continue
            counter.append(site)
            yield self.geometry(site, meta), data

    def _process_safely(self, cycle):
        # One bad cycle must not stall the pipeline: log, drop it, move on
        try:
            self.process_cycle(cycle)
        except Exception:
            logger.exception('Gridding cycle %s failed; discarding it', cycle)
            self._discard(cycle)

    def process_cycle(self, cycle):
        started = time.monotonic()
        files = self.cycle_files(cycle)
        sites = []
        vol = grid_rain(self.grid, self.contributions(files, sites))
        path = self.store.write('rain', cycle, vol)
        self.index('rain', cycle, path, len(sites))
        self.done.add(cycle)
        self._discard(cycle)
        logger.info(
            'Gridded %s from %d sites (%d sweeps) in %.1fs; %d voxels',
            cycle, len(sites), len(files),
            time.monotonic() - started, int((vol > 0).sum()))
        self.write_cloud_frame(cycle)
        try:
            self.write_motion(cycle)
        except Exception:
            logger.exception('Motion field for %s failed', cycle)
        try:
            self.write_forecast(cycle)
        except Exception:
            logger.exception('Forecast frames after %s failed', cycle)

    def _discard(self, cycle):
        shutil.rmtree(self.cycle_dir(cycle), ignore_errors=True)
        self.done.add(cycle)

    # -- clouds -------------------------------------------------------

    def icon_candidates(self, now):
        """The two newest runs that should be published by now."""
        runs = []
        day = now.date()
        for delta in (0, 1):
            date = day - datetime.timedelta(days=delta)
            for hour in ICON_RUN_HOURS:
                run = datetime.datetime(date.year, date.month, date.day, hour,
                                        tzinfo=datetime.UTC)
                if run + ICON_AVAILABLE_AFTER <= now:
                    runs.append(run)
        return sorted(runs)[-2:]

    def icon_dir(self, run):
        return self.raw_dir / 'icon' / f'{run:%Y%m%dT%HZ}'

    def ensure_heights(self, run):
        """Full-level heights of the model columns, cached on disk."""
        if self.full_h is not None:
            return self.full_h
        cache = self.store.root / 'icon-full-heights.npy'
        if cache.is_file():
            self.full_h = np.load(cache)
            return self.full_h
        hhl_dir = self.raw_dir / 'icon-hhl'
        for level in range(1, 67):
            dest = hhl_dir / grib_name(run, 0, level, 'hhl')
            if not dest.exists() and not self.icon_source.fetch(
                    self.icon_source.url(run, 0, level, 'hhl'), dest):
                raise FileNotFoundError(f'hhl level {level} of {run}')
        self.full_h = level_heights(hhl_dir, run, self.sampler)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, self.full_h)
        shutil.rmtree(hhl_dir, ignore_errors=True)
        return self.full_h

    def poll_icon(self):
        if self.icon_source is None:
            return
        now = self.now()
        candidates = self.icon_candidates(now)
        self.clean_icon_raw(keep=candidates)
        for run in candidates:
            if run in self.icon_loaded or self.icon_attempts[run] > 60:
                continue
            if not self.has_space('ICON-D2'):
                break
            self.icon_attempts[run] += 1
            try:
                if self.load_run(run):
                    self.backfill_clouds()
                    self.write_forecast()
            except Exception:
                logger.exception('ICON-D2 run %s failed', run)
        with self.clouds_lock:
            self.clouds.drop_before(now - ICON_KEEP)

    def clean_icon_raw(self, keep):
        """Raw run directories of runs that are no longer candidates (late,
        partial or abandoned runs would otherwise leak ~1 GB each)."""
        base = self.raw_dir / 'icon'
        if not base.is_dir():
            return
        keep_names = {f'{run:{RUN_FORMAT}}' for run in keep}
        for path in base.iterdir():
            if path.is_dir() and path.name not in keep_names:
                shutil.rmtree(path, ignore_errors=True)
                logger.info('Removed stale raw ICON-D2 run %s', path.name)

    def load_run(self, run):
        """Download and load a run's steps; False while files are missing."""
        full_h = self.ensure_heights(run)
        q_levels = choose_levels(full_h)
        w_levels = wind_levels(full_h)
        run_dir = self.icon_dir(run)
        needed = [
            (step, level, var)
            for step in range(1, self.settings['icon_steps'] + 1)
            for var, levels in (('qc', q_levels), ('qi', q_levels),
                                ('qs', q_levels), ('qg', q_levels),
                                ('qr', q_levels), ('clc', q_levels),
                                ('u', w_levels), ('v', w_levels))
            for level in levels
        ]
        missing = [
            (step, level, var) for step, level, var in needed
            if not (run_dir / grib_name(run, step, level, var)).exists()
        ]
        if missing:
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=6) as pool:
                got = list(pool.map(
                    lambda item: self.icon_source.fetch(
                        self.icon_source.url(run, *item),
                        run_dir / grib_name(run, *item)),
                    missing))
            logger.info(
                'ICON-D2 %s: fetched %d/%d missing files in %.0fs',
                run, sum(got), len(missing), time.monotonic() - started)
            if not all(got):
                return False
        started = time.monotonic()
        steps = [
            load_step(run_dir, run, step, self.sampler, full_h,
                      q_levels, w_levels)
            for step in range(1, self.settings['icon_steps'] + 1)
        ]
        with self.clouds_lock:
            self.clouds.add_run(steps)
        self.icon_loaded.add(run)
        self.icon_runs[run] = [s.valid_time for s in steps]
        shutil.rmtree(run_dir, ignore_errors=True)
        logger.info(
            'ICON-D2 %s loaded: %d steps (%d + %d levels) in %.0fs',
            run, len(steps), len(q_levels), len(w_levels),
            time.monotonic() - started)
        return True

    def write_cloud_frame(self, ts):
        with self.clouds_lock:
            if not self.clouds.covers(ts):
                return False
            rg, flow = self.clouds.frame_at(ts)
        path = self.store.write('clouds', ts, rg)
        self.store.write('clouds_flow', ts, flow, dtype=np.float32)
        self.index('clouds', ts, path, None)
        return True

    # -- forecast ----------------------------------------------------

    def newest_run(self):
        return max(self.icon_runs) if self.icon_runs else None

    # -- motion -------------------------------------------------------

    def write_motion(self, ts):
        """The motion field at an observed frame's time (radar block
        matching against the previous frame, model wind where no echo is
        trackable, zero without either), in 2 km cells per 5 minutes →
        `rain_flow/<ts>` [H, W, 2]; shipped with the rain frame and used
        by the nowcast built on that frame."""
        cur2 = nowcast.maxpool2(np.asarray(self.store.open('rain', ts)))
        prev2 = None
        try:
            prev2 = nowcast.maxpool2(np.asarray(
                self.store.open('rain', ts - CYCLE)))
        except LookupError:
            pass
        height, width = cur2.shape[1:]
        model_u = np.zeros((height, width), np.float32)
        model_v = np.zeros((height, width), np.float32)
        with self.clouds_lock:
            try:
                _, model_flow = self.clouds._blend(ts, ())
                model_u, model_v = model_flow[..., 0], model_flow[..., 1]
            except LookupError:
                pass
        u, v, known = nowcast.motion_field(
            None if prev2 is None else nowcast.column_max(prev2),
            nowcast.column_max(cur2), model_u, model_v)
        flow = np.stack([u, v], axis=-1).astype(np.float32)
        path = self.store.write('rain_flow', ts, flow, dtype=np.float32)
        self.index('rain_flow', ts, path, None)
        return flow, known

    def write_forecast(self, newest_observed=None):
        """
        Nowcast-blended forecast frames for the next `forecast_minutes`
        after the newest observed rain frame, every 5 minutes: the observed
        volume moved along its estimated motion (Lagrangian persistence),
        blended in linear Z into the ICON-D2 field with w = (lead/60)²;
        clouds are the model clouds; the flow block carries the motion used.
        Keyed by run and basis observation so served URLs never change.
        """
        run = self.newest_run()
        if run is None:
            return 0
        if newest_observed is None:
            now, since = self.window()
            observed = self.indexed('rain', since)
            if not observed:
                return 0
            newest_observed = max(observed)
        rain_key = forecast_product('forecast_rain', run, newest_observed)
        if self.indexed(rain_key, newest_observed):
            return 0                          # this basis is already done
        try:
            cur = np.asarray(self.store.open('rain', newest_observed))
        except LookupError:
            return 0
        cur2 = nowcast.maxpool2(cur)
        z_cur = nowcast.bytes_to_z(cur2)
        with self.clouds_lock:
            if not self.clouds.covers(newest_observed):
                return 0
        try:
            flow = np.asarray(self.store.open('rain_flow', newest_observed))
            known = True
        except LookupError:
            flow, known = self.write_motion(newest_observed)
        motion_u, motion_v = flow[..., 0], flow[..., 1]
        written = 0
        steps = self.settings['forecast_minutes'] // 5
        for k in range(1, steps + 1):
            ts = newest_observed + k * CYCLE
            with self.clouds_lock:
                try:
                    fields, _ = self.clouds._blend(
                        ts, ('lwc', 'cov', 'qr', 'qs', 'qg'))
                except LookupError:
                    continue
            w = nowcast.blend_weight(5 * k, self.settings['forecast_minutes'])
            z_model = hydrometeors_to_z(
                fields['qr'], fields['qs'], fields['qg'])
            z_obs = nowcast.advect_z(z_cur, motion_u, motion_v, k)
            rain = nowcast.z_to_bytes(nowcast.blend_z(z_obs, z_model, w))
            rg = quantise(fields['lwc'], fields['cov'])
            path = self.store.write(rain_key, ts, rain)
            self.store.write(
                forecast_product('forecast_clouds', run, newest_observed),
                ts, rg)
            self.store.write(
                forecast_product('forecast_flow', run, newest_observed),
                ts, flow, dtype=np.float32)
            self.index(rain_key, ts, path, None)
            written += 1
        if written:
            logger.info(
                'Wrote %d nowcast frames (run %s, basis %s)',
                written, run, newest_observed)
        self.clean_forecasts(keep=rain_key.split('/', 1)[1])
        return written

    def _index_keys(self):
        """Forecast keys ('<run>-<basis>') known to the index."""
        return {
            key.split('/', 1)[1] for key in self.indexed_forecast_products()
            if '/' in key
        }

    def indexed_forecast_products(self):
        if self.index != self._index_db:
            return set()
        with get_connection() as conn:
            return indexed_products(conn, 'forecast_rain/')

    def clean_forecasts(self, keep):
        """Keep the newest two forecast keys (and `keep`); drop the rest —
        files and index rows, whichever of the two still exists."""
        keys = self._index_keys()
        for product in FORECAST_PRODUCTS:
            base = self.store.root / product
            if base.is_dir():
                keys |= {p.name for p in base.iterdir() if p.is_dir()}
        keepers = set(sorted(keys)[-2:]) | {keep}
        for key in keys - keepers:
            for product in FORECAST_PRODUCTS:
                shutil.rmtree(self.store.root / product / key,
                              ignore_errors=True)
            if self.index == self._index_db:
                with get_connection() as conn:
                    delete_index_before(
                        conn, f'forecast_rain/{key}',
                        datetime.datetime.max.replace(tzinfo=datetime.UTC))
            logger.info('Dropped forecast key %s', key)

    def backfill_clouds(self):
        now, since = self.window()
        have = self.indexed('clouds', since)
        written = 0
        for ts in sorted(self.indexed('rain', since) - have):
            if self.write_cloud_frame(ts):
                written += 1
        if written:
            logger.info('Wrote %d cloud frames for existing rain stamps',
                        written)

    # -- cells --------------------------------------------------------

    def poll_cells(self):
        if self.konrad_source is None:
            return
        now, since = self.window()
        self.cells_done |= self.indexed('cells', since)
        try:
            files = self.konrad_source.list_files()
        except Exception:
            logger.exception('KONRAD3D listing failed')
            return
        for ts, url in sorted(files):
            if ts < since or ts in self.cells_done:
                continue
            dest = self.raw_dir / 'konrad' / url.rsplit('/', 1)[1]
            try:
                self.konrad_source.download(url, dest)
                timestamp, cells = konrad.parse_konrad(dest)
                path = self.store.write_json(
                    'cells', ts, {'timestamp': timestamp, 'cells': cells})
                self.index('cells', ts, path, len(cells))
                self.cells_done.add(ts)
                logger.info('KONRAD3D %s: %d cells', ts, len(cells))
            except Exception:
                logger.exception('KONRAD3D %s failed', url)
            finally:
                dest.unlink(missing_ok=True)

    # -- retention ----------------------------------------------------

    def clean(self):
        cutoff = self.now() - datetime.timedelta(
            hours=self.settings['retention_hours'])
        for product in PRODUCTS:
            deleted = self.store.delete_before(product, cutoff)
            if self.index == self._index_db:
                with get_connection() as conn:
                    delete_index_before(conn, product, cutoff)
            if deleted:
                logger.info('Deleted %d expired %s frames',
                            len(deleted), product)
        for cycle in self.pending_cycles():
            if cycle < cutoff:
                self._discard(cycle)
        for path in self.raw_dir.glob('konrad/*'):
            if path.is_file() and path.stat().st_mtime < cutoff.timestamp():
                path.unlink(missing_ok=True)


def make_ingest(**kwargs):
    kwargs.setdefault('icon_source', IconSource(settings.RADAR3D_ICON_URL))
    kwargs.setdefault(
        'konrad_source', KonradSource(settings.RADAR3D_KONRAD_URL))
    return Radar3DIngest(
        FrameStore(settings.RADAR3D_DATA_DIR),
        Path(settings.RADAR3D_DATA_DIR) / 'raw',
        **kwargs,
    )


def _loop(name, fn, interval):
    while True:
        started = time.monotonic()
        try:
            fn()
        except Exception:
            logger.exception('radar3d %s failed', name)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def run_forever():
    ingest = make_ingest()
    logger.info('radar3d worker started; %d sites', len(ingest.sites))
    interval = ingest.settings['poll_interval']
    threading.Thread(
        target=_loop, args=('icon', ingest.poll_icon, interval),
        daemon=True, name='icon').start()
    threading.Thread(
        target=_loop, args=('cells', ingest.poll_cells, interval),
        daemon=True, name='cells').start()

    def rain():
        ingest.poll_once()
        ingest.clean()
    _loop('rain', rain, interval)


def grid_directory(directory):
    """Grid every cycle found in a directory of saved sweeps."""
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
