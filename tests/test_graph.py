"""Graph scaling uses sample times, keeps manual bounds and clips lines."""
import pytest

from core import SignalState, build_snapshot
from core.graph import GraphViewport


def test_cell_identity_decimal_hex_and_exports():
    for raw in ('161490247', hex(161490247), '9a02547'):
        snap = build_snapshot({'ServiceStatus': '2', 'cell_id': raw, 'earfcn': '1300', 'pci': '114'})
        assert snap.cell_id == 161490247
        assert snap.enodeb * 256 + snap.sector == snap.cell_id
        state = SignalState()
        state.ingest(snap, 0)
        assert state.log[-1]['cell_id'] == snap.cell_id
        assert state.top_cells()[0].cell_id == snap.cell_id
    for raw in (None, '', 'bad value', '-1', '0', str(1 << 28)):
        assert build_snapshot({'cell_id': raw}).cell_id is None
    assert build_snapshot({'enodeb_id': '630821'}).cell_id is None


def test_times_follow_valid_samples_and_reset():
    state = SignalState(history=2)
    for now, raw in ((1, '3'), (3, None), (5, '4'), (9, '6')):
        state.ingest(build_snapshot({'ServiceStatus': '2', 'sinr': raw}), now)
    assert list(state.history_times['sinr']) == [5, 9]
    assert list(state.history['sinr']) == [4, 6]
    state.reset()
    assert not state.history_times['sinr']


def test_independent_axes_live_update_and_auto():
    graph = GraphViewport()
    graph.update([0, 5, 10], [100, 110, 120], 'sinr', 10)
    graph.zoom('time', 0.5)
    assert graph.x_range() == (110, 120)
    original_y = graph.y_range()
    graph.zoom('level', 0.5)
    manual_y = graph.y_range()
    assert manual_y[1] - manual_y[0] == (original_y[1] - original_y[0]) / 2
    graph.update([0, 5, 10, 25], [100, 110, 120, 130], 'sinr', 25)
    assert graph.x_range() == (120, 130)
    assert graph.y_range() == manual_y
    graph.zoom('time', 2)
    assert graph.x_range() == (110, 130)
    graph.reset()
    assert graph.x_range() == (100, 130)
    assert graph.y_range()[1] >= 25
    graph.zoom('level', 0.5)
    graph.update([-100], [140], 'rsrp', -100)
    assert graph.levels is None and graph.seconds is None


def test_empty_single_and_zoom_limits():
    graph = GraphViewport()
    assert graph.x_range() == (-1, 0)
    assert graph.y_range()[1] > graph.y_range()[0]
    assert graph.segments() == []
    graph.zoom('time', 0.001)
    graph.zoom('level', 1000)
    assert graph.y_range() == (-20, 40)
    graph.update([5], [], 'sinr', None)
    assert graph.x_range() == (-1, 0)
    assert graph.segments() == []
    with pytest.raises(ValueError):
        graph.zoom('time', 0)
    with pytest.raises(ValueError):
        graph.zoom('bad', 2)


def test_clip_crossing_and_invisible_segments():
    graph = GraphViewport()
    graph.update([-20, 20, 20, -20, -20], [0, 1, 2, 3, 4], 'sinr', None)
    graph.levels = (-5, 5)
    segments = graph.segments()
    assert len(segments) == 2
    assert all(0 <= v <= 1 for line in segments for v in line)
    assert segments[0][1] == 0 and segments[0][3] == 1
    graph.seconds = 1
    assert graph.segments() == []
    graph.update([0, 2], [4, 4], 'sinr', None)
    assert len(graph.segments()) == 1


def test_gesture_zoom_anchor_pan_and_reset():
    from core.graph import GestureViewport
    graph = GestureViewport()
    graph.update([0, 5, 10], [0, 50, 100], 'sinr', 10)
    ylo, yhi = graph.y_range()
    graph.transform(0.5, (0.25, 0.75))
    assert graph.x_range() == (12.5, 62.5)
    lo, hi = graph.y_range()
    assert lo + (hi - lo) * 0.75 == ylo + (yhi - ylo) * 0.75
    graph.transform(1, (0.5, 0.5), (0.1, 0.1))
    assert graph.x_range() == (7.5, 57.5)
    graph.update([0, 5, 10], [10, 60, 110], 'sinr', 10)
    assert graph.x_range() == (17.5, 67.5)
    graph.transform(100, (0.5, 0.5))
    assert graph.x_range() == (10, 110)
    assert graph.y_range() == (-20, 40)
    graph.transform(0.001, (0.5, 0.5), (1000, -1000))
    assert graph.x_range() == (10, 11)
    assert graph.y_range() == (39, 40)
    graph.reset()
    assert graph.offset == 0 and graph.seconds is None and graph.levels is None
    assert graph.x_range() == (10, 110)


def test_gesture_empty_data_and_evicted_history():
    from core.graph import GestureViewport
    graph = GestureViewport()
    graph.transform(0.5, (0.5, 0.5))
    assert graph.seconds is None
    with pytest.raises(ValueError):
        graph.transform(0, (0.5, 0.5))
    graph.update([1, 2], [0, 100], 'sinr', None)
    graph.transform(0.5, (0, 0))
    graph.update([1, 2], [100, 120], 'sinr', None)
    assert graph.x_range() == (100, 120)
    graph.update([-100], [130], 'rsrp', None)
    assert graph.offset == 0


@pytest.mark.parametrize('param', ['rsrp', 'rsrq', 'sinr'])
def test_lte_nr_pair_shares_axes_and_survives_nr_gap(param):
    from core.graph import comparison_param
    state = SignalState()
    for now, nr in enumerate((True, True, False, True, True)):
        raw = {'ServiceStatus': '2', 'rsrp': '-100', 'rsrq': '-15', 'sinr': '5'}
        if nr:
            raw.update(nrrsrp='-80', nrrsrq='-8', nrsinr='25')
        state.ingest(build_snapshot(raw), now)
    pair = comparison_param(param)
    graph = GraphViewport()
    graph.update(list(state.history[param]), list(state.history_times[param]), param, None,
                 list(state.history[pair]), list(state.history_times[pair]))
    assert graph.has_comparison
    assert graph.times == graph.comparison_times == [0, 1, 2, 3, 4]
    assert len(graph.segments()) == 4
    assert len(graph.segments(True)) == 2  # never join through missing NR
    low, high = graph.y_range()
    assert low <= min(graph.latest_value(), graph.latest_value(True))
    assert high >= max(graph.latest_value(), graph.latest_value(True))
    assert comparison_param(pair) == param


def test_comparison_uses_actual_timestamps_and_no_stale_endpoint():
    graph = GraphViewport()
    graph.update([0, 5, 10], [0, 5, 10], 'sinr', None, [20, 25], [2, 7])
    assert graph.latest_value(True) is None
    assert graph.last_point(True) is None
    segment = graph.segments(True)[0]
    assert segment[0] == pytest.approx(0.2)
    assert segment[2] == pytest.approx(0.7)
    graph.zoom('level', 0.5)
    manual = graph.y_range()
    graph.update([10, 12], [10, 11], 'sinr', None, [25, float('nan')], [10, 11])
    assert graph.y_range() == manual
    assert graph.latest_value(True) is None
    assert graph.segments(True) == []
    graph.update([-80], [12], 'rssi', None, [-90], [12])
    assert not graph.has_comparison
    assert graph.levels is None


def test_nr_only_and_loss_of_registration_do_not_create_zero_samples():
    state = SignalState()
    for now, service in enumerate(('2', '2', '1')):
        state.ingest(build_snapshot({'ServiceStatus': service, 'nrsinr': '22'}), now)
    graph = GraphViewport()
    graph.update(list(state.history['sinr']), list(state.history_times['sinr']), 'sinr', None,
                 list(state.history['nr_sinr']), list(state.history_times['nr_sinr']))
    assert graph.segments() == []
    assert len(graph.segments(True)) == 1
    assert graph.latest_value() is None and graph.latest_value(True) is None
    assert graph.last_point(True) is None
