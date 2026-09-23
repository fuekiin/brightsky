"""APNs payloads, in the app's shapes.

Live Activity content is the app's `WeatherLiveContent` (WeatherCore/
Notifications/WeatherLive.swift) as Swift's synthesized Codable writes it:
an enum with an associated value is `{"rain": {"_0": {...}}}`, a
`WarningLevel` is its Int raw value, and a `Date` is what ActivityKit's
default JSONDecoder reads — seconds since 2001-01-01 (`deferredToDate`).
The start/update round-trip on hardware is what proves this (design §12.7).
"""

import datetime


APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
ATTRIBUTES_TYPE = 'WeatherLiveAttributes'


def swift_date(dt):
    return (dt - APPLE_EPOCH).total_seconds()


def unix(dt):
    return int(dt.timestamp())


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def rain_content(*, state, change_at, detail, bucket_start, buckets,
                 peak_at=None, place_name, generated_at, title=None,
                 context=None):
    return _drop_none({
        'phase': {'rain': {'_0': _drop_none({
            'state': state,
            'changeAt': change_at and swift_date(change_at),
            'detail': detail,
            'bucketStart': swift_date(bucket_start),
            'buckets': list(buckets),
            'peakAt': peak_at and swift_date(peak_at),
        })}},
        'placeName': place_name,
        'generatedAt': swift_date(generated_at),
        'title': title,
        'context': context,
    })


def warning_content(*, stage, level, event, onset, expires, detail,
                    place_name, generated_at, escalated_from=None,
                    bucket_start=None, buckets=None, title=None,
                    context=None):
    return _drop_none({
        'phase': {'warning': {'_0': _drop_none({
            'stage': stage,
            'level': level,
            'event': event,
            'onset': swift_date(onset),
            'expires': swift_date(expires),
            'detail': detail,
            'escalatedFrom': escalated_from,
            'bucketStart': bucket_start and swift_date(bucket_start),
            'buckets': buckets,
        })}},
        'placeName': place_name,
        'generatedAt': swift_date(generated_at),
        'title': title,
        'context': context,
    })


def alert(title, body, *, time_sensitive=False, thread_id=None,
          collapse=None, data=None):
    aps = {
        'alert': {'title': title, 'body': body},
        'sound': 'default',
    }
    if time_sensitive:
        aps['interruption-level'] = 'time-sensitive'
    if thread_id:
        aps['thread-id'] = thread_id
    payload = {'aps': aps}
    if data:
        payload['nano'] = data
    return payload


def live_start(content, *, now, stale, alert_title, alert_body,
               sound=True):
    return {'aps': {
        'timestamp': unix(now),
        'event': 'start',
        'content-state': content,
        'attributes-type': ATTRIBUTES_TYPE,
        'attributes': {},
        'stale-date': unix(stale),
        'alert': _live_alert(alert_title, alert_body, sound),
    }}


def live_update(content, *, now, stale, alert_title=None, alert_body=None):
    aps = {
        'timestamp': unix(now),
        'event': 'update',
        'content-state': content,
        'stale-date': unix(stale),
    }
    if alert_title:
        aps['alert'] = _live_alert(alert_title, alert_body, True)
    return {'aps': aps}


def live_end(content, *, now, dismissal):
    return {'aps': {
        'timestamp': unix(now),
        'event': 'end',
        'content-state': content,
        'dismissal-date': unix(dismissal),
    }}


def _live_alert(title, body, sound):
    a = {'title': title, 'body': body}
    if sound:
        a['sound'] = 'default'
    return a
