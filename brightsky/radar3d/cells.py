"""
KONRAD3D convective cells (weather/radar/konrad3d/KONRAD3D_<stamp>.xml,
every 5 minutes) → the JSON objects the nano app's StormCell decodes.
Port of the app repo's tools/radar3d/cells_fixture.py; field names are the
client contract and must not change.
"""
import xml.etree.ElementTree as ET


MISSING = -1000000000
SOURCE = (
    'DWD KONRAD3D 1.8 (radar cell detection incl. LINET lightning, '
    'hydrometeors, mesocyclones)')


def _num(e, path, cast=float):
    text = e.findtext(path)
    if text is None:
        return None
    try:
        value = cast(float(text))
    except ValueError:
        return None
    return None if value <= MISSING + 1 else value


def _nums(e, path):
    text = e.findtext(path) or ''
    return [
        None if float(x) <= MISSING + 1 else int(float(x))
        for x in text.split()
    ]


def _cell(c):
    track = [
        [round(float(p.findtext('geodetic_coordinate/latitude')), 5),
         round(float(p.findtext('geodetic_coordinate/longitude')), 5)]
        for p in c.findall('forecast/centroid_forecasts/centroid_forecast')
    ]
    ident = c.findtext('metadata/identifier') or ''
    coord = 'geometry/centroid_3d/geodetic_coordinate/'
    return {
        # ids can carry a "#" marker
        'id': int(''.join(ch for ch in ident if ch.isdigit()) or 0),
        'lat': round(float(c.findtext(coord + 'latitude')), 5),
        'lon': round(float(c.findtext(coord + 'longitude')), 5),
        'heightM': _num(c, coord + 'height_msl', int),
        'echoTopM': _num(c, 'geometry/echo_top_msl', int),
        'echoBottomM': _num(c, 'geometry/echo_bottom_msl', int),
        'areaKm2': _num(c, 'geometry/covered_area'),
        'maxDbz': _num(c, 'intensity/max_value'),
        'severity': _num(c, 'intensity/severity_decimal'),
        'gustKmh': _num(c, 'intensity/maximum_estimated_wind_gust'),
        'gustFlag': _num(c, 'intensity/gust_flag', int) == 1,
        'heavyRainMm': _num(c, 'intensity/heavy_rain_potential'),
        'hail': _num(c, 'intensity/hail_flag', int) == 1,
        'largeHail': (_num(c, 'hymec/area_large_hail') or 0) > 0,
        'hailTopM': _num(c, 'hymec/echo_top_hail', int),
        'hailBottomM': _num(c, 'hymec/echo_bottom_hail', int),
        'hailAreaKm2': _num(c, 'hymec/area_hail'),
        'lightningRate': _num(c, 'lightning/lightning_rate', int) or 0,
        'lightningDensity': _num(c, 'lightning/lightning_density'),
        'rateHistory': _nums(c, 'lightning/lightning_rate_history'),
        'lightningJumps':
            _num(c, 'lightning/number_detected_lightning_jumps', int) or 0,
        'mesoIndex':
            _num(c, 'mesocyclone/mesocyclone_severity_index', int) or 0,
        'mesoBaseM': _num(c, 'mesocyclone/mesocyclone_height_base', int),
        'mesoTopM': _num(c, 'mesocyclone/mesocyclone_height_top', int),
        'cloudTopM': _num(c, 'satellite/cloud_top_height', int),
        'speedKmh': _num(c, 'tracking/cell_speed'),
        'firstSeen': c.findtext('tracking/reference_time_first_detection'),
        'track': track,
    }


def parse_konrad(path):
    """→ (reference time as ISO string, [cell dict, ...])."""
    root = ET.parse(path).getroot()
    timestamp = root.findtext('.//head/metadata/reference_time')
    cells = [_cell(c) for c in root.findall('.//cells/feature')]
    return timestamp, cells


def cells_in_bbox(cells, min_lat, max_lat, min_lon, max_lon):
    return [
        c for c in cells
        if min_lat <= c['lat'] <= max_lat and min_lon <= c['lon'] <= max_lon
    ]
