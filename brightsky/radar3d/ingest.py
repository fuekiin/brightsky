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
from collections import Counter
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
CYCLE_DIR_FORMAT = '%Y%m%dT%H%MZ'


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

    def _process_safely(self, cycle):
        # One bad cycle must not stall the pipeline: log, drop it, move on
        try:
            self.process_cycle(cycle)
        except Exception:
            logger.exception('Gridding cycle %s failed; discarding it', cycle)
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
            try:
                # Every tilt carries the site metadata; take the layout
                # most tilts agree on (a sweep occasionally has 361 rays)
                metas = [read_site_meta(path, site) for _, path in tilts]
                meta = Counter(metas).most_common(1)[0][0]
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
        if self.index == self._index_db:
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
