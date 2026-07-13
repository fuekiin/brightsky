import datetime
import json
import math
import os
import tempfile
from functools import cached_property

import numpy as np
import requests
from isal import isal_zlib as zlib
from pyproj import CRS, Transformer
from shapely import MultiPolygon, STRtree, Point

from brightsky.settings import settings
from brightsky.utils import USER_AGENT


class NoData(LookupError):
    pass


class PgParams(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.map = {}

    def __getitem__(self, key):
        return f'${self.map.setdefault(key, len(self.map) + 1)}'

    def get_params(self):
        get_value = super().__getitem__
        return tuple(get_value(k) for k in self.map)


def topg(query, params):
    p = PgParams(params)
    return query.format_map(p), p.get_params()


def make_dicts(rows):
    return [dict(row) for row in rows]


async def weather(
    conn,
    date,
    last_date,
    lat=None,
    lon=None,
    max_dist=50000,
    dwd_station_ids=None,
    wmo_station_ids=None,
    source_ids=None,
):
    sources_data = await sources(
        conn,
        lat=lat,
        lon=lon,
        max_dist=max_dist,
        dwd_station_ids=dwd_station_ids,
        wmo_station_ids=wmo_station_ids,
        source_ids=source_ids,
        observation_types=['historical', 'current', 'forecast'],
        date=date,
        last_date=last_date,
    )
    sources_rows = sources_data['sources']
    primary_source_ids = {}
    for row in sources_rows:
        primary_source_ids.setdefault(row['observation_type'], row['id'])
    primary_source_ids = list(primary_source_ids.values())
    weather_rows = await _weather(conn, date, last_date, primary_source_ids)
    source_ids = [row['id'] for row in sources_rows]
    if len(weather_rows) < int((last_date - date).total_seconds()) // 3600:
        weather_rows = await _weather(conn, date, last_date, source_ids)
    await _fill_missing_fields(conn, weather_rows, source_ids)
    used_source_ids = {row['source_id'] for row in weather_rows}
    used_source_ids.update(
        source_id
        for row in weather_rows
        for source_id in row.get('fallback_source_ids', {}).values()
    )
    return {
        'weather': weather_rows,
        'sources': [s for s in sources_rows if s['id'] in used_source_ids],
    }


async def _weather(conn, date, last_date, source_ids):
    params = {
        'date': date,
        'last_date': last_date,
        'source_ids': source_ids,
    }
    sql = """
        SELECT DISTINCT ON (timestamp) *
        FROM weather
        WHERE
            timestamp BETWEEN {date} AND {last_date} AND
            source_id = ANY({source_ids}::int[])
        ORDER BY timestamp, array_position({source_ids}::int[], source_id)
    """
    sql, params = topg(sql, params)
    rows = await conn.fetch(sql, *params)
    return make_dicts(rows)


IGNORED_MISSING_FIELDS = {
    # Not available in MOSMIX
    'relative_humidity',
    'wind_gust_direction',
    # Not available in recent and historical measurements
    'precipitation_probability',
    'precipitation_probability_6h',
}


async def _fill_missing_fields(conn, weather_rows, source_ids):
    incomplete_rows = []
    missing_fields = set()
    for row in weather_rows:
        missing_row_fields = {k for k, v in row.items() if v is None}
        relevant_fields = missing_row_fields - IGNORED_MISSING_FIELDS
        if relevant_fields:
            incomplete_rows.append((row, missing_row_fields))
            missing_fields.update(relevant_fields)
    if not incomplete_rows:
        return
    min_date = incomplete_rows[0][0]['timestamp']
    max_date = incomplete_rows[-1][0]['timestamp']
    not_null = ' OR '.join(f"{f} IS NOT NULL" for f in missing_fields)
    params = {
        'date': min_date,
        'last_date': max_date,
        'source_ids': source_ids,
    }
    if (max_date - min_date).days >= 3:
        per_field_sql = ' UNION '.join(
            f"""(
            SELECT source_id
            FROM weather
            WHERE
                timestamp BETWEEN {{date}} AND {{last_date}} AND
                source_id = ANY({{source_ids}}::int[]) AND
                {f} IS NOT NULL
            GROUP BY source_id
            ORDER BY array_position({{source_ids}}::int[], source_id)
            LIMIT 3
            )"""
            for f in missing_fields
        )
        trim_sql, trim_params = topg(per_field_sql, params)
        useful_ids = [
            r['source_id']
            for r in await conn.fetch(trim_sql, *trim_params)
        ]
        if not useful_ids:
            return
        params['source_ids'] = useful_ids
    sql = f"""
        SELECT *
        FROM weather
        WHERE
            timestamp BETWEEN {{date}} AND {{last_date}} AND
            source_id = ANY({{source_ids}}::int[]) AND
            ({not_null})
        ORDER BY timestamp, array_position({{source_ids}}::int[], source_id)
    """
    sql, params = topg(sql, params)
    all_rows = await conn.fetch(sql, *params)
    fallback_by_ts = {}
    for fb_row in all_rows:
        fallback_by_ts.setdefault(fb_row['timestamp'], []).append(fb_row)
    for row, fields in incomplete_rows:
        _apply_fallback(row, fields, fallback_by_ts.get(row['timestamp'], []))


def _apply_fallback(row, missing_fields, fallback_rows):
    for fb_row in fallback_rows:
        if not missing_fields:
            break
        filled = []
        for f in missing_fields:
            if fb_row[f] is not None:
                row.setdefault('fallback_source_ids', {})
                row[f] = fb_row[f]
                row['fallback_source_ids'][f] = fb_row['source_id']
                filled.append(f)
        for f in filled:
            missing_fields.discard(f)


async def current_weather(
    conn,
    lat=None,
    lon=None,
    max_dist=50000,
    dwd_station_ids=None,
    wmo_station_ids=None,
    source_ids=None,
):
    sources_data = await sources(
        conn,
        lat=lat,
        lon=lon,
        max_dist=max_dist,
        dwd_station_ids=dwd_station_ids,
        wmo_station_ids=wmo_station_ids,
        source_ids=source_ids,
        observation_types=['synop'],
    )
    sources_rows = sources_data['sources']
    source_ids = [row['id'] for row in sources_rows]
    params = {'source_ids': source_ids}
    sql = """
        SELECT *
        FROM current_weather
        WHERE source_id = ANY({source_ids}::int[])
        ORDER BY array_position({source_ids}::int[], source_id)
    """
    sql, params = topg(sql, params)
    rows = make_dicts(await conn.fetch(sql, *params))
    if not rows:
        raise NoData(
            "Could not find current weather for your location criteria",
        )
    weather = rows[0]
    missing_fields = {k for k, v in weather.items() if v is None}
    _apply_fallback(weather, missing_fields, rows[1:])
    used_source_ids = {weather['source_id']}
    used_source_ids.update(weather.get('fallback_source_ids', {}).values())
    return {
        'weather': weather,
        'sources': [s for s in sources_rows if s['id'] in used_source_ids],
    }


async def synop(
    conn,
    date,
    last_date,
    dwd_station_ids=None,
    wmo_station_ids=None,
    source_ids=None,
):
    sources_data = await sources(
        conn,
        dwd_station_ids=dwd_station_ids,
        wmo_station_ids=wmo_station_ids,
        source_ids=source_ids,
        observation_types=['synop'],
    )
    sources_rows = sources_data['sources']
    source_ids = [row['id'] for row in sources_rows]
    sql = """
        SELECT *
        FROM synop
        WHERE
            timestamp BETWEEN {date} AND {last_date} AND
            source_id = ANY({source_ids}::int[])
        ORDER BY timestamp
        """
    params = {
        'date': date,
        'last_date': last_date,
        'source_ids': source_ids,
    }
    sql, params = topg(sql, params)
    rows = await conn.fetch(sql, *params)
    return {
        'weather': make_dicts(rows),
        'sources': make_dicts(sources_rows),
    }


async def radar(
    conn,
    date=None,
    last_date=None,
    lat=None,
    lon=None,
    distance=200000,
    fmt='compressed',
    bbox=None,
):
    extra = {}
    if not date:
        date = await conn.fetchval(
            "SELECT MAX(timestamp) - '3 hours'::interval FROM radar"
        )
    if not last_date:
        last_date = date + datetime.timedelta(hours=2)
    if lat is not None and lon is not None:
        x, y = _transformer.to_xy(lat, lon)
        if not -0.5 <= x <= 1099.5 or not -0.5 <= y <= 1199.5:
            raise NoData("lat/lon lies outside the radar data range")
        center_x = int(round(x))
        center_y = int(round(y))
        pixels = distance // 1000
        bbox = (
            max(center_y - pixels, 0),
            max(center_x - pixels, 0),
            min(center_y + pixels, 1199),
            min(center_x + pixels, 1099),
        )
        extra['bbox'] = bbox
        extra['latlon_position'] = {
            'x': round(x - bbox[1], 3),
            'y': round(y - bbox[0], 3),
        }
    sql = """
        SELECT *
        FROM radar
        WHERE timestamp BETWEEN {date} AND {last_date}
        ORDER BY timestamp
        """
    params = {
        'date': date,
        'last_date': last_date,
    }
    sql, params = topg(sql, params)
    rows = make_dicts(await conn.fetch(sql, *params))
    if fmt == 'plain':
        for row in rows:
            row['precipitation_5'] = _load_radar(row['precipitation_5'], bbox)
    elif fmt == 'bytes':
        for row in rows:
            row['precipitation_5'] = memoryview(
                _load_radar(row['precipitation_5'], bbox),
            )
    elif fmt == 'compressed' and bbox:
        for row in rows:
            row['precipitation_5'] = zlib.compress(
                _load_radar(row['precipitation_5'], bbox),
            )
    elif fmt != 'compressed':
        raise ValueError(f"Unknown format: '{fmt}'")
    return {
        'radar': rows,
        'geometry': _transformer.bbox_to_geometry(bbox),
        **extra,
    }


def _load_radar(raw, bbox, width=1100, height=1200):
    precip = np.frombuffer(
        zlib.decompress(raw),
        dtype='i2',
    ).reshape((height, width))
    if bbox:
        top, left, bottom, right = bbox
        precip = precip[top:bottom+1, left:right+1]
        # Arrays must be C-contiguous for orjson and zlib
        precip = np.ascontiguousarray(precip).reshape(precip.shape)
    return precip


class RadarCoordinatesTransformer:

    PROJ_STR = (
        "+proj=stere +lat_0=90 +lat_ts=60 +lon_0=10 +a=6378137 "
        "+b=6356752.3142451802 +no_defs +x_0=543196.83521776402 "
        "+y_0=3622588.8619310018"
    )

    @cached_property
    def de1200(self):
        return CRS.from_proj4(self.PROJ_STR)

    @cached_property
    def wgs84_to_de1200(self):
        return Transformer.from_crs(4326, self.de1200)

    @cached_property
    def de1200_to_wgs84(self):
        return Transformer.from_crs(self.de1200, 4326)

    def to_xy(self, lat, lon):
        x, y = self.wgs84_to_de1200.transform(lat, lon)
        return round(x) / 1000, -round(y) / 1000

    def to_latlon(self, x, y):
        lat, lon = self.de1200_to_wgs84.transform(x * 1000, -y * 1000)
        return round(lat, 5), round(lon, 5)

    def to_lonlat(self, x, y):
        return tuple(reversed(self.to_latlon(x, y)))

    def bbox_to_geometry(self, bbox):
        if not bbox:
            bbox = (0, 0, 1199, 1099)
        top, left, bottom, right = bbox
        return {
            'type': 'Polygon',
            'coordinates': [
                self.to_lonlat(left - .5, top - .5),
                self.to_lonlat(left - .5, bottom + .5),
                self.to_lonlat(right + .5, bottom + .5),
                self.to_lonlat(right + .5, top - .5),
            ],
        }


_transformer = RadarCoordinatesTransformer()


async def alerts(
    conn,
    lat=None,
    lon=None,
    warn_cell_id=None,
):
    if lat is not None and lon is not None:
        meta = _warn_cells.find(lat, lon)
    elif warn_cell_id is not None:
        try:
            meta = _warn_cells.get_meta(warn_cell_id)
        except KeyError:
            raise NoData(
                "Unknown warn_cell_id, please use commune (Gemeinden), not "
                "district (Landkreis) ids"
            )
    else:
        sql = """
            SELECT *
            FROM alerts
            JOIN (
                SELECT alert_id, array_agg(warn_cell_id) as warn_cell_ids
                FROM alert_cells
                GROUP BY alert_id
            ) cells ON alerts.id = cells.alert_id
            ORDER BY severity DESC
        """
        rows = await conn.fetch(sql)
        return {'alerts': make_dicts(rows)}
    sql = """
        SELECT *
        FROM alerts
        WHERE id IN (
            SELECT alert_id
            FROM alert_cells
            WHERE warn_cell_id = {warn_cell_id}
        )
        ORDER BY severity DESC
        """
    params = {
        'warn_cell_id': meta['warn_cell_id'],
    }
    sql, params = topg(sql, params)
    rows = await conn.fetch(sql, *params)
    return {
        'alerts': make_dicts(rows),
        'location': meta,
    }


class WarnCellManager:

    CELLS_CACHE_PATH = os.path.join(tempfile.gettempdir(), 'alert_cells.json')

    @cached_property
    def tree(self):
        self.cell_meta = {}
        self.cell_meta_by_id = {}
        for f in self.get_cell_data()['features']:
            polygons = [
                # shell, holes
                (c[0], c[1:])
                for c in f['geometry']['coordinates']
            ]
            p = MultiPolygon(polygons)
            meta = {
                'warn_cell_id': f['properties']['WARNCELLID'],
                'name': f['properties']['NAME'],
                'name_short': f['properties']['KURZNAME'],
                'district': f['properties']['KREIS'],
                'state': f['properties']['BUNDESLAND'],
                'state_short': f['properties']['BL_KUERZEL'],
            }
            self.cell_meta[p] = meta
            self.cell_meta_by_id[meta['warn_cell_id']] = meta
        return STRtree(list(self.cell_meta.keys()))

    def get_cell_data(self):
        path = self.CELLS_CACHE_PATH
        if not os.path.isfile(path):
            resp = requests.get(
                settings.WARN_CELLS_URL,
                headers={'User-Agent': USER_AGENT},
            )
            with open(path, 'wb') as f:
                f.write(resp.content)
        with open(path) as f:
            return json.load(f)

    def find(self, lat, lon):
        p = Point(lon, lat)
        cell = self.tree.geometries[self.tree.nearest(p)]
        if cell.distance(p) > 0.01:
            raise NoData("Requested position is not covered by the DWD")
        return self.cell_meta[cell]

    def get_meta(self, warn_cell_id):
        # Make sure cells have been parsed
        self.tree
        return self.cell_meta_by_id[warn_cell_id]


_warn_cells = WarnCellManager()


async def pollen(
    conn,
    lat=None,
    lon=None,
    region_id=None,
):
    if lat is not None and lon is not None:
        region_id = _pollen_regions.find(lat, lon)['region_id']
    elif region_id is None:
        raise ValueError("Please supply lat & lon, or region_id")
    sql = """
        SELECT *
        FROM pollen
        WHERE
            (partregion_id = {region_id} OR
             (partregion_id = -1 AND region_id = {region_id})) AND
            date >= current_date
        ORDER BY date, species
    """
    params = {'region_id': region_id}
    sql, params = topg(sql, params)
    rows = make_dicts(await conn.fetch(sql, *params))
    if not rows:
        raise NoData("No pollen data for the given location criteria")
    return {
        'pollen': [
            {k: row[k] for k in ['species', 'date', 'index', 'severity']}
            for row in rows
        ],
        'location': {
            k: rows[0][k]
            for k in [
                'region_id', 'partregion_id', 'region_name',
                'partregion_name',
            ]
        },
        'last_update': rows[0]['last_update'],
        'next_update': rows[0]['next_update'],
        'sender': rows[0]['sender'],
    }


class RegionManager:
    """Point-in-polygon resolution over a DWD GeoServer region layer."""

    REGIONS_CACHE_PATH = None
    REGIONS_URL_SETTING = None

    def make_meta(self, properties):
        raise NotImplementedError

    @cached_property
    def tree(self):
        self.region_meta = {}
        for f in self.get_region_data()['features']:
            geometry = f['geometry']
            if geometry['type'] == 'Polygon':
                coordinates = [geometry['coordinates']]
            else:
                coordinates = geometry['coordinates']
            polygons = [
                # shell, holes
                (c[0], c[1:])
                for c in coordinates
            ]
            p = MultiPolygon(polygons)
            self.region_meta[p] = self.make_meta(f['properties'])
        return STRtree(list(self.region_meta.keys()))

    def get_region_data(self):
        path = self.REGIONS_CACHE_PATH
        if not os.path.isfile(path):
            resp = requests.get(
                getattr(settings, self.REGIONS_URL_SETTING),
                headers={'User-Agent': USER_AGENT},
            )
            with open(path, 'wb') as f:
                f.write(resp.content)
        with open(path) as f:
            return json.load(f)

    def find(self, lat, lon):
        p = Point(lon, lat)
        region = self.tree.geometries[self.tree.nearest(p)]
        if region.distance(p) > 0.01:
            raise NoData("Requested position is not covered by the DWD")
        return self.region_meta[region]


class PollenRegionManager(RegionManager):
    """Resolves lat/lon to DWD pollen regions (Pollenflugbereiche).

    Region polygons come from the DWD GeoServer's 'Pollenfluggebiete'
    layer, where the 'GF' property matches s31fg.json's partregion_id
    (or region_id for regions without part-regions).
    """

    REGIONS_CACHE_PATH = os.path.join(
        tempfile.gettempdir(), 'pollen_regions.json')
    REGIONS_URL_SETTING = 'POLLEN_REGIONS_URL'

    def make_meta(self, properties):
        return {
            'region_id': properties['GF'],
            'name': properties['GEN'],
        }


class BiowetterZoneManager(RegionManager):
    """Resolves lat/lon to DWD Biowetter zones (A-K).

    Zone polygons come from the DWD GeoServer's 'Biowettergebiete'
    layer. Its 'GF' property numbers the zones in the DWD's canonical
    order, which is NOT alphabetical — F and G are swapped. This mapping
    was verified against the zone names in biowetter.json (2026-07-14).
    """

    REGIONS_CACHE_PATH = os.path.join(
        tempfile.gettempdir(), 'biowetter_zones.json')
    REGIONS_URL_SETTING = 'BIOWETTER_ZONES_URL'
    ZONE_LETTERS = {
        1: 'A',
        2: 'B',
        3: 'C',
        4: 'D',
        5: 'E',
        6: 'G',
        7: 'F',
        8: 'H',
        9: 'I',
        10: 'J',
        11: 'K',
    }

    def make_meta(self, properties):
        return {
            'zone_id': self.ZONE_LETTERS[properties['GF']],
            'name': properties['GEN'],
        }


class CityLocationManager:
    """Coordinates for the cities of the DWD's city-based health products
    (uvi.json, gt.json).

    The products identify locations by name only. Coordinates come from
    the DWD GeoServer's 'Uv_Stationen' layer (matched via ALIASNAME,
    covers all uvi.json cities); a handful of gt.json-only cities are
    missing from that layer and use static coordinates instead. Nearest-
    city responses always include the matched city and its distance, so
    the resolution is transparent to clients.
    """

    STATIONS_CACHE_PATH = os.path.join(
        tempfile.gettempdir(), 'uv_stations.json')
    # Product city names that differ from the layer's ALIASNAME
    # (verified unique among German stations)
    ALIASES = {
        'Frankfurt': 'Frankfurt/Main',
        'List': 'List auf Sylt',
    }
    # gt.json cities missing from the Uv_Stationen layer (city centers)
    STATIC_LOCATIONS = {
        'Köln': (50.94, 6.96),
        'Schwerin': (53.63, 11.41),
        'Saarbrücken': (49.24, 7.0),
        'Mannheim': (49.49, 8.47),
        'Erfurt': (50.98, 11.03),
    }
    MAX_DIST = 200000

    @cached_property
    def locations(self):
        locations = {}
        for f in self.get_station_data()['features']:
            lon, lat = f['geometry']['coordinates'][:2]
            locations[f['properties']['ALIASNAME'].strip()] = (lat, lon)
        locations.update(self.STATIC_LOCATIONS)
        return locations

    def get_station_data(self):
        path = self.STATIONS_CACHE_PATH
        if not os.path.isfile(path):
            resp = requests.get(
                settings.UV_STATIONS_URL,
                headers={'User-Agent': USER_AGENT},
            )
            with open(path, 'wb') as f:
                f.write(resp.content)
        with open(path) as f:
            return json.load(f)

    def get_location(self, city):
        return self.locations.get(self.ALIASES.get(city, city))

    def find_nearest(self, lat, lon, cities):
        best = None
        for city in cities:
            location = self.get_location(city)
            if not location:
                continue
            distance = self._distance(lat, lon, *location)
            if best is None or distance < best['distance']:
                best = {
                    'city': city,
                    'lat': location[0],
                    'lon': location[1],
                    'distance': round(distance),
                }
        if best is None or best['distance'] > self.MAX_DIST:
            raise NoData("Requested position is not covered by the DWD")
        return best

    @staticmethod
    def _distance(lat1, lon1, lat2, lon2):
        # Haversine, sufficient for nearest-city selection
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2 +
            math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        return 2 * 6371000 * math.asin(math.sqrt(a))


_pollen_regions = PollenRegionManager()
_biowetter_zones = BiowetterZoneManager()
_city_locations = CityLocationManager()


async def biowetter(
    conn,
    lat=None,
    lon=None,
    zone_id=None,
):
    if lat is not None and lon is not None:
        zone_id = _biowetter_zones.find(lat, lon)['zone_id']
    elif zone_id is None:
        raise ValueError("Please supply lat & lon, or zone_id")
    sql = """
        SELECT *
        FROM biowetter
        WHERE zone_id = {zone_id} AND date >= current_date
        ORDER BY date, period DESC
    """
    params = {'zone_id': zone_id.upper()}
    sql, params = topg(sql, params)
    rows = make_dicts(await conn.fetch(sql, *params))
    if not rows:
        raise NoData("No Biowetter data for the given location criteria")
    return {
        'biowetter': [
            {
                'date': row['date'],
                'period': row['period'],
                'weather_class': row['weather_class'],
                # asyncpg returns jsonb as text
                'effects': json.loads(row['effects']),
                'recommendations': json.loads(row['recommendations']),
            }
            for row in rows
        ],
        'location': {
            'zone_id': rows[0]['zone_id'],
            'zone_name': rows[0]['zone_name'],
        },
        'last_update': rows[0]['last_update'],
        'next_update': rows[0]['next_update'],
        'sender': rows[0]['sender'],
    }


async def uv_index(
    conn,
    lat=None,
    lon=None,
    city=None,
):
    return await _city_product(
        conn,
        table='uv_index',
        record_fields=['city', 'date', 'uv_index'],
        time_field='date',
        lat=lat,
        lon=lon,
        city=city,
    )


async def thermal_hazard(
    conn,
    lat=None,
    lon=None,
    city=None,
):
    return await _city_product(
        conn,
        table='thermal_hazard',
        record_fields=['city', 'timestamp', 'level'],
        time_field='timestamp',
        lat=lat,
        lon=lon,
        city=city,
    )


async def _city_product(
    conn,
    table,
    record_fields,
    time_field,
    lat=None,
    lon=None,
    city=None,
):
    params = {}
    where = f"{time_field} >= current_date"
    if city is not None:
        where += " AND city = {city}"
        params['city'] = city
    sql = f"""
        SELECT *
        FROM {table}
        WHERE {where}
        ORDER BY city, {time_field}
    """
    sql, params = topg(sql, params)
    rows = make_dicts(await conn.fetch(sql, *params))
    location = None
    if city is None and lat is not None and lon is not None:
        cities = sorted({row['city'] for row in rows})
        location = _city_locations.find_nearest(lat, lon, cities)
        rows = [row for row in rows if row['city'] == location['city']]
    if not rows:
        raise NoData(f"No data in {table} for the given location criteria")
    result = {
        table: [
            {k: row[k] for k in record_fields}
            for row in rows
        ],
        'last_update': rows[0]['last_update'],
        'next_update': rows[0]['next_update'],
        'sender': rows[0]['sender'],
    }
    if location:
        result['location'] = location
    return result


async def sources(
    conn,
    lat=None,
    lon=None,
    max_dist=50000,
    dwd_station_ids=None,
    wmo_station_ids=None,
    source_ids=None,
    observation_types=None,
    ignore_type=False,
    date=None,
    last_date=None,
):
    select = "*"
    order_by = "observation_type"
    params = {
        'lat': lat,
        'lon': lon,
        'max_dist': max_dist,
        'dwd_station_ids': dwd_station_ids,
        'wmo_station_ids': wmo_station_ids,
        'source_ids': source_ids,
        'observation_types': observation_types,
        'date': date,
        'last_date': last_date,
    }
    if source_ids:
        where = "id = ANY({source_ids}::int[])"
        order_by = "array_position({source_ids}, id), observation_type"
    elif dwd_station_ids:
        where = "dwd_station_id = ANY({dwd_station_ids}::text[])"
        order_by = """
            array_position({dwd_station_ids}, dwd_station_id::text),
            observation_type
        """
    elif wmo_station_ids:
        where = "wmo_station_id = ANY({wmo_station_ids}::text[])"
        order_by = """
            array_position({wmo_station_ids}, wmo_station_id::text),
            observation_type
        """
    elif (lat is not None and lon is not None):
        distance = """
            earth_distance(ll_to_earth({lat}, {lon}), ll_to_earth(lat, lon))
        """
        select += f", round({distance}) AS distance"
        where = f"""
            earth_box(
                ll_to_earth({{lat}}, {{lon}}),
                {{max_dist}}
            ) @> ll_to_earth(lat, lon) AND
            {distance} < {{max_dist}}
        """
        if ignore_type:
            order_by = "distance"
        else:
            order_by += ", distance"
    else:
        raise ValueError(
            "Please supply lat & lon, or dwd_station_ids, or wmo_station_ids, "
            "or source_ids",
        )
    if observation_types:
        where += " AND observation_type = ANY({observation_types}::observation_type[])"  # noqa
    if date is not None:
        where += " AND last_record >= {date}"
    if last_date is not None:
        where += " AND first_record <= {last_date}"
    sql = f"""
        SELECT {select}
        FROM sources
        WHERE {where}
        ORDER BY {order_by}
    """
    sql, params = topg(sql, params)
    rows = await conn.fetch(sql, *params)
    if not rows:
        raise NoData("No sources match your criteria")
    return {'sources': make_dicts(rows)}
