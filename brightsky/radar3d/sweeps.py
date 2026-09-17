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
