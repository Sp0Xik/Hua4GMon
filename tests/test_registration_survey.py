"""Reproduce the strong foreign signal with no service; protect survey provenance."""
import csv
import io
import math
from dataclasses import replace

import pytest

from core.demo import DemoApiError, DemoModem, demo_factory
from core.graph import GraphViewport
from core.models import Snapshot, build_snapshot
from core.router import RouterSession
from core.scanner import Survey, records
from core.state import SignalState


@pytest.mark.parametrize('raw, expected', [
    ({}, 'unknown'), ({'ServiceStatus': '9', 'ConnectionStatus': '901'}, 'unknown'),
    ({'ServiceStatus': '0', 'ConnectionStatus': '901'}, 'unregistered'),
    ({'ServiceStatus': '1'}, 'unregistered'), ({'ServiceStatus': '3'}, 'unregistered'),
    ({'ServiceStatus': '2', 'ConnectionStatus': '902'}, 'registered'),
    ({'ConnectionStatus': '901'}, 'registered'), ({'ConnectionStatus': '902'}, 'unknown'),
    ({'ServiceStatus': '2', 'SimStatus': '255'}, 'unregistered'),
    ({'ServiceStatus': '2', '_network_uncertain': True}, 'unknown'),
])
def test_registration_requires_evidence(raw, expected):
    snap = build_snapshot({'rsrp': '-89dBm', 'Numeric': '25035', **raw})
    assert snap.registration == expected
    assert snap.rsrp == -89  # raw observations remain available for the survey
    assert snap.display().rsrp == (-89 if expected == 'registered' else None)


def test_motiv_does_not_improve_working_peak_or_bridge_graph():
    state = SignalState()
    good = Snapshot(registration='registered', rsrp=-110, sinr=4,
                    cell_id=100, earfcn=1300, pci=20, plmn='25002')
    state.ingest(good, 0)
    bad = build_snapshot({'ServiceStatus': '1', 'ConnectionStatus': '902',
                          'Numeric': '25035', 'rsrp': '-89dBm', 'sinr': '20dB',
                          'earfcn': '1276', 'pci': '30', 'cell_id': '200'})
    state.ingest(bad, 1)
    assert state.current('rsrp') is None
    assert state.peaks['rsrp'] == -110
    assert len(state.top_cells()) == 1
    assert not state.top_cells()[0].is_current(bad)
    assert state.log[-1]['rsrp'] is None
    assert state.log[-1]['registration'] == 'unregistered'
    state.ingest(good, 2)
    graph = GraphViewport()
    graph.update(list(state.history['rsrp']), list(state.history_times['rsrp']), 'rsrp', -110)
    assert graph.segments() == []
    assert all(math.isfinite(v) for v in graph.y_range())


@pytest.fixture
def session():
    modem = DemoModem(seed=1)
    value = RouterSession('demo', 'pw', factory=demo_factory(modem))
    value.open()
    yield value
    value.close()


def test_transition_during_poll_is_not_accepted(session):
    states = iter([{'ServiceStatus': '2'}, {'ServiceStatus': '1'}])
    session._client.monitoring.status = lambda: next(states)
    assert build_snapshot(session.fetch()).registration == 'unknown'


def test_conflicting_operators_are_not_serving_network(session):
    session._client.device.signal = lambda: {'rsrp': '-89dBm', 'plmn': '25035'}
    snap = build_snapshot(session.fetch())
    assert not snap.registered
    assert snap.plmn == '25035' and snap.operator == 'Мотив'
    assert not snap.display().plmn


def test_status_failure_cannot_reuse_previous_registration(session):
    assert build_snapshot(session.fetch()).registered
    session._client.monitoring.status = lambda: {}
    assert not build_snapshot(session.fetch()).registered


def test_survey_only_queries_cells_when_enabled(session):
    calls = []
    session._client.net.cell_info = lambda: calls.append(1) or {'cells': []}
    session.fetch()
    assert not calls
    session.survey_enabled = True
    raw = session.fetch()
    assert '_survey_cells' in raw and calls == [1]
    session.fetch()
    assert calls == [1]
    session.survey_enabled = False
    session._next_survey_read = 0
    session.fetch()
    assert calls == [1]


def test_search_restores_timeout_and_does_not_write_settings(session):
    original = session.read_net_mode()
    session._connection.timeout = 4
    session._client.net.plmn_list = lambda: {'Networks': {'Network': {'Numeric': '25035'}}}
    dumps, errors = session.discover_networks()
    assert dumps['plmn_list']
    assert 'cell_info' in errors
    assert session._connection.timeout == 4
    assert session.read_net_mode() == original


def test_unsupported_cells_are_not_retried_each_poll(session):
    calls = []
    def unsupported():
        calls.append(1)
        raise DemoApiError('unsupported', 100002)
    session._client.net.cell_info = unsupported
    session.survey_enabled = True
    assert '_survey_error' in session.fetch()
    session.fetch()
    assert calls == [1]


def test_survey_stop_restart_ranking_and_complete_export():
    survey = Survey()
    raw = Snapshot(registration='registered', plmn='25002', cell_id=100,
                   earfcn=1300, pci=1, sinr=4, rsrp=-110, rsrq=-10)
    survey.observe(raw)
    assert not survey.rows
    survey.start()
    for level in (4, 6, 30):
        survey.observe(replace(raw, sinr=level))
    survey.observe(replace(raw, registration='unregistered', plmn='25035', rsrp=-70, sinr=40))
    assert survey.ranked()[0].median('sinr') == 6
    assert survey.ranked()[0].registered_samples == 3
    survey.stop()
    before = survey.csv_text()
    survey.observe(replace(raw, rsrp=-50))
    assert survey.csv_text() == before
    rows = list(csv.DictReader(io.StringIO(before)))
    assert rows[0]['cell_id'] == '100' and rows[0]['sinr_max'] == '30'
    assert '25035' in survey.report()
    generation = survey.generation
    survey.start()
    assert not survey.rows and survey.generation > generation


def test_discovery_does_not_invent_neighbour_operator_or_registration():
    survey = Survey()
    survey.start()
    survey.accept_discovery({
        'plmn_list': {'Networks': {'Network': [{'Numeric': '25035', 'FullName': 'MOTIV', 'State': '3', 'Rat': '7'}]}},
        'cell_info': {'neighbours': [{'earfcn': '1276', 'pci': '15', 'rsrp': '-89dBm'}]},
    }, {})
    row = survey.ranked()[0]
    assert row.snap.plmn == '' and row.snap.registration == 'unknown'
    assert row.snap.cell_id is None
    assert row.source == 'net/cell-info'
    assert len(survey.operators) == 1
    assert '25035' in survey.csv_text()
    assert 'Cell ID неизвестен' in survey.report()
    survey.accept_cells({'neighbours': [{'earfcn': '1276', 'pci': '15', 'rsrp': '-90dBm'}]})
    assert len(survey.rows) == 1 and row.samples == 2
    assert not survey.messages


def test_unknown_payloads_and_limits_are_explicit():
    survey = Survey()
    survey.start()
    survey.accept_discovery({'cell_info': {'unknown_vendor_field': 'hello'}}, {'plmn_list': 'unsupported'})
    assert not survey.rows and not survey.operators
    assert len(survey.messages) == 3
    for i in range(1002):
        survey.observe(Snapshot(earfcn=i, pci=1))
    assert len(survey.rows) == 1000 and survey.limit_reached
    assert '1000' in survey.report()
    assert list(records({'a': {'b': {}}}, 13)) == []


def test_survey_5g_and_missing_metrics():
    survey = Survey()
    survey.start()
    survey.observe(Snapshot(nci=1000000000, nr_arfcn=600000, nr_pci=0,
                            nr_rsrp=-90, nr_sinr=10, nr_rsrq=-9))
    assert survey.ranked()[0].snap.has_nr
    assert 'NR_RSRP' in survey.report()
    assert 'nr_rsrp_median' in survey.csv_text()
