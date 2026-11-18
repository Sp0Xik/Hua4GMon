from dataclasses import replace

import pytest

from core.band_lock import NetModeSettings, locked_bands, plan_auto, plan_restore, verify
from core.export import mask_sensitive
from core.local_transport import select_local_network
from core.models import Snapshot, build_snapshot
from core.parsers import format_mimo
from core.scanner import Survey
from core.state import SignalState


def test_partial_net_mode_cannot_be_restored():
    from core.errors import NetModeUnavailable

    assert NetModeSettings.from_response({'LTEBand': '4'}) is None
    with pytest.raises(NetModeUnavailable):
        plan_restore(NetModeSettings('4', '', ''))
    current = NetModeSettings('7', '1', '00')
    assert locked_bands(current, [1, 2]) == []
    assert verify(plan_auto(current, current), current, [1, 2])


def test_nested_diagnostic_addresses_are_masked():
    values = mask_sensitive({'BSSID': 'aa:bb', 'Gateway': 'test',
                             'MacAddress': [{'value': 'abcd'}], 'cell_id': 123})
    assert values == {'BSSID': '*****', 'Gateway': '****',
                      'MacAddress': [{'value': '****'}], 'cell_id': 123}


def test_cell_identity_survives_partial_samples_and_distinguishes_reuse():
    state = SignalState()
    sample = Snapshot(registration='registered', earfcn=1300, pci=0, plmn='25001')
    for i, identity in enumerate((None, 161490247, None, 161490248)):
        change = state.ingest(replace(sample, cell_id=identity), i)
        assert (change is not None) == (i == 3)
    assert sorted(row.samples for row in state.cells.values()) == [1, 3]
    assert state.ingest(replace(sample, cell_id=161490248, plmn='25002'), 5)
    nr = Snapshot(registration='registered', nci=12345678901, nr_pci=0, nr_arfcn=632000,
                  nr_sinr=19, nr_rsrp=-85, nr_band='n78')
    state.ingest(nr, 6)
    row = state.cells[('', nr.cell)]
    assert state.is_current_cell(row)
    assert (row.cell_id, row.best_sinr, row.best_rsrp) == (nr.nci, 19, -85)
    assert state.log[-1]['nci'] == nr.nci


def test_tm_matrix_and_status_have_independent_parsers():
    snap = build_snapshot({'mimo': ' 4×4 ', 'transmode': 'TM[3]', 'ServiceStatus': '2.9'})
    assert (snap.mimo_matrix, snap.tm, snap.registered) == ('4x4', 3, False)
    assert build_snapshot({'mimo': '2x2'}).tm is None
    assert format_mimo('error 4') == 'error 4'


def test_survey_keeps_lifetime_extrema():
    s = Survey()
    s.start()
    sample = Snapshot(earfcn=1300, rsrp=-65)
    s.observe(sample)
    for _ in range(600):
        s.observe(replace(sample, rsrp=-110))
    row, = s.ranked()
    assert (row.samples, row.median('rsrp'), row.maxima['rsrp']) == (601, -110, -65)
    assert '-65' in s.report() and '-65' in s.csv_text()


def test_most_specific_android_route_wins():
    candidates = [('ethernet', ['0.0.0.0/0']), ('wifi', ['192.168.88.0/24', '192.168.8.0/24'])]
    assert select_local_network('192.168.8.1', candidates) == 'wifi'
    with pytest.raises(OSError):
        select_local_network('192.168.8.1', [('wifi', ['0.0.0.0/0']), ('ethernet', ['0.0.0.0/0'])])
