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
