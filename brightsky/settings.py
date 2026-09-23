import datetime
import os
from multiprocessing import cpu_count

from dateutil.tz import tzutc

from brightsky.utils import load_dotenv


CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_ALL_HEADERS = False
CORS_ALLOWED_ORIGINS = []
CORS_ALLOWED_HEADERS = []
DATABASE_CONNECTION_POOL_SIZE = cpu_count()
DATABASE_URL = 'postgres://localhost'
ICON_CLOUDY_THRESHOLD = 80
ICON_PARTLY_CLOUDY_THRESHOLD = 25
ICON_RAIN_THRESHOLD = 0.5
ICON_WIND_THRESHOLD = 10.8
IGNORED_CURRENT_OBSERVATIONS_STATIONS = ['K386']
KEEP_DOWNLOADS = False
MIN_DATE = datetime.datetime(2010, 1, 1, tzinfo=tzutc())
MAX_DATE = None
POLLING_CRONTAB_MINUTE = '*'
# nano radar3d (docs/nano/architecture.md)
RADAR3D_BACKFILL_MINUTES = 70
RADAR3D_CYCLE_TIMEOUT = 420
RADAR3D_DATA_DIR = '.data/radar3d'
RADAR3D_FORECAST_MINUTES = 60
RADAR3D_ICON_STEPS = 6
RADAR3D_ICON_URL = 'https://opendata.dwd.de/weather/nwp/icon-d2/grib/'
RADAR3D_KONRAD_URL = 'https://opendata.dwd.de/weather/radar/konrad3d/'
RADAR3D_LISTING_INTERVAL = 900
RADAR3D_MIN_FREE_GB = 5.0
RADAR3D_POLL_INTERVAL = 60
RADAR3D_RETENTION_HOURS = 3
RADAR3D_SITES = [
    'asb', 'boo', 'drs', 'eis', 'ess', 'fbg', 'fld', 'hnr', 'isn', 'mem',
    'neu', 'nhb', 'oft', 'pro', 'ros', 'tur', 'umd',
]
RADAR3D_SWEEPS_URL = (
    'https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/')
BIOWETTER_ZONES_URL = (
    'https://maps.dwd.de/geoserver/wfs'
    '?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature'
    '&TYPENAMES=Biowettergebiete&OUTPUTFORMAT=json'
)
POLLEN_REGIONS_URL = (
    'https://maps.dwd.de/geoserver/wfs'
    '?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature'
    '&TYPENAMES=Pollenfluggebiete&OUTPUTFORMAT=json'
)
# nano push service (docs/nano/push.md)
PUSH_APNS_KEY_ID = ''
PUSH_APNS_KEY_PATH = ''
PUSH_APNS_TEAM_ID = ''
PUSH_APNS_TOPIC = 'de.nano-wetter.app'
PUSH_MAX_ALERTS_PER_HOUR = 10
PUSH_MAX_RULES_PER_DEVICE = 50
# Unauthenticated registrations per client IP and hour
PUSH_REGISTER_RATE_LIMIT = 30
# Loopback base URL of the `web` container the evaluator reads from
PUSH_WEATHER_URL = 'http://localhost:5000'
REDIS_URL = 'redis://localhost'
SERVER_URL = 'http://localhost:5000'
UV_STATIONS_URL = (
    'https://maps.dwd.de/geoserver/wfs'
    '?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature'
    '&TYPENAMES=Uv_Stationen&OUTPUTFORMAT=json'
)
WARN_CELLS_URL = (
    'https://maps.dwd.de/geoserver/wfs'
    '?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature'
    '&TYPENAMES=Warngebiete_Gemeinden&OUTPUTFORMAT=json'
)


def _make_bool(bool_str):
    return bool_str == '1'


def _make_date(date_str):
    return datetime.datetime.fromisoformat(date_str).replace(tzinfo=tzutc())


def _make_list(list_str, separator=','):
    if not list_str:
        return []
    return list_str.split(separator)


_SETTING_PARSERS = {
    'MAX_DATE': _make_date,

    bool: _make_bool,
    datetime.datetime: _make_date,
    float: float,
    int: int,
    list: _make_list,
}


class Settings(dict):
    """A dictionary that makes its keys available as attributes"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loaded = False

    def load(self):
        load_dotenv()
        for k, v in globals().items():
            if k.isupper() and not k.startswith('_'):
                self[k] = v
        for k, v in os.environ.items():
            if k.startswith('BRIGHTSKY_') and k.isupper():
                setting_name = k.split('_', 1)[1]
                setting_type = type(self.get(setting_name))
                setting_parser = _SETTING_PARSERS.get(
                    setting_name, _SETTING_PARSERS.get(setting_type))
                if setting_parser:
                    v = setting_parser(v)
                self[setting_name] = v

    def __getattr__(self, name):
        if not self.loaded:
            self.load()
            self.loaded = True
        return self[name]


settings = Settings()
