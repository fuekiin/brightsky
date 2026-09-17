import json

import pytest

from brightsky.radar3d.cells import cells_in_bbox, parse_konrad


@pytest.fixture
def parsed(data_dir):
    return parse_konrad(
        data_dir / 'radar3d' / 'KONRAD3D_20260916T115000_3cells.xml')


def test_parse_matches_reference_fixture(parsed, data_dir):
    timestamp, cells = parsed
    expected = json.loads(
        (data_dir / 'radar3d' / 'konrad_expected.json').read_text())
    assert timestamp == expected['timestamp'] == '2026-09-16T11:50:00Z'
    by_id = {c['id']: c for c in cells}
    assert set(by_id) == {112, 26, 115}          # "115#" → 115
    for ref in expected['cells']:
        assert by_id[ref['id']] == ref


def test_missing_values_and_history(parsed):
    _, cells = parsed
    c = {c['id']: c for c in cells}[112]
    assert c['hailTopM'] is None and c['mesoBaseM'] is None
    assert c['rateHistory'][0] is None and len(c['rateHistory']) == 3
    assert isinstance(c['track'][0], list) and len(c['track'][0]) == 2
    assert c['firstSeen'].endswith('Z')
    assert set(c) == {
        'id', 'lat', 'lon', 'heightM', 'echoTopM', 'echoBottomM', 'areaKm2',
        'maxDbz', 'severity', 'gustKmh', 'gustFlag', 'heavyRainMm', 'hail',
        'largeHail', 'hailTopM', 'hailBottomM', 'hailAreaKm2',
        'lightningRate', 'lightningDensity', 'rateHistory', 'lightningJumps',
        'mesoIndex', 'mesoBaseM', 'mesoTopM', 'cloudTopM', 'speedKmh',
        'firstSeen', 'track'}


def test_cells_in_bbox(parsed):
    _, cells = parsed
    c = cells[0]
    inside = cells_in_bbox(cells, c['lat'] - 0.01, c['lat'] + 0.01,
                           c['lon'] - 0.01, c['lon'] + 0.01)
    assert inside == [c]
    assert cells_in_bbox(cells, 0, 1, 0, 1) == []
