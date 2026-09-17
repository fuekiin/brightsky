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
"""  # noqa


def test_source_parses_listing():
    source = SweepSource('https://example.test/sweep_vol_z/')
    url = 'https://example.test/sweep_vol_z/isn/hdf5/filter_polarimetric/'
    assert source.site_url('isn') == url
    sweeps = source.parse_listing('isn', url, LISTING)
    assert [(s.tilt, u.rsplit('/', 1)[1][:40]) for s, u in sweeps] == [
        (0, 'ras07-vol5minng01_sweeph5onem_dbzh_00-20'),
        (9, 'ras07-vol5minng01_sweeph5onem_dbzh_09-20'),
    ]
    assert sweeps[0][1].startswith(url)


class FakeSource:
    """Serves the isn fixture files, optionally holding some back."""

    def __init__(self, sweep_dir, hold=()):
        self.files = {p.name: p for p in sweep_dir.glob('*-hd5')}
        self.hold = set(hold)
        self.downloads = 0

    def list_site(self, site):
        return [
            (parse_sweep_name(name), f'fake://{name}')
            for name in sorted(self.files) if name not in self.hold
            and parse_sweep_name(name).site == site
        ]

    def download(self, url, dest):
        self.downloads += 1
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
    assert source.downloads == 10
    vol = np.load(tmp_path / 'frames' / 'rain' / '20260916T1150Z.npy')
    assert vol.shape == (24, 100, 100) and vol.max() > 150
    assert not (tmp_path / 'raw' / '20260916T1150Z').exists()
    # A second poll neither re-downloads nor re-grids
    ing.poll_once()
    assert indexed == [(CYCLE, 1)] and source.downloads == 10


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
    assert indexed == [] and source.downloads == 0
    store = ing.store
    store.write('rain', CYCLE, np.zeros((24, 100, 100), np.uint8))
    ing.clean()
    assert store.timestamps('rain') == []


def test_already_indexed_cycles_are_skipped(ingest):
    ing, source, indexed = ingest()
    ing.indexed = lambda since: {CYCLE}
    ing.poll_once()
    assert indexed == [] and source.downloads == 0


def test_failing_cycle_is_discarded_not_fatal(ingest, tmp_path):
    ing, source, indexed = ingest()

    def boom(cycle):
        raise RuntimeError('corrupt sweep')
    ing.process_cycle = boom
    ing.poll_once()                          # must not raise
    assert indexed == [] and CYCLE in ing.done
    assert not (tmp_path / 'raw' / '20260916T1150Z').exists()
