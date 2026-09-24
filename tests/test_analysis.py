"""Тесты RF-аналитики, состояния сессии, Band Lock и классификации ошибок."""
import datetime

import pytest

import core
from core.band_lock import NetModeSettings

# =========================================================
# rf
# =========================================================


@pytest.mark.parametrize("ex, lte, nr, rat", [
    (101, True, False, 'lte'), (1011, True, False, 'lte_ca'),
    (None, True, True, 'nr_nsa'), (None, False, True, 'nr_sa'),
    (41, False, False, '3g'), (0, False, False, 'none'),
    (None, True, False, 'lte'), (None, False, False, ''),
    (777, True, False, 'lte'),
])
def test_classify_rat(ex, lte, nr, rat):
    assert core.classify_rat(ex, lte, nr) == rat


@pytest.mark.parametrize("ex, count, ca", [
    (1011, 1, True), (101, 2, False), (None, 2, True), (None, 1, False),
    (None, 0, None),
])
def test_detect_ca(ex, count, ca):
    assert core.detect_ca(ex, count) is ca


@pytest.mark.parametrize("cqi0, cqi1, status", [
    (11, 10, 'ok'), (12, 0, 'single'), (12, 6, 'imbalance'),
    (3, 0, 'ok'), (None, 5, None), (5, None, None),
])
def test_mimo_status(cqi0, cqi1, status):
    assert core.mimo_status(cqi0, cqi1) == status


@pytest.mark.parametrize("p, status", [
    (22.0, 'limit'), (20.0, 'limit'), (16.0, 'high'), (3.0, 'ok'), (None, None)])
def test_uplink_status(p, status):
    assert core.uplink_status(p) == status


def _advice(**kw):
    base = {'rsrp': -85.0, 'sinr': 18.0, 'rsrq': -8.0, 'jitter': 2.0,
            'mimo': 'ok', 'uplink': 'ok'}
    base.update(kw)
    return core.advice(**base)


def test_advice_quiet_when_good():
    assert _advice() == []


def test_advice_interference():
    tips = _advice(rsrp=-85.0, sinr=2.0)
    assert any("помех" in tip for tip in tips)


def test_advice_weak_but_clean():
    tips = _advice(rsrp=-115.0, sinr=12.0)
    assert any("слабый" in tip for tip in tips)


def test_advice_load_mimo_uplink_jitter():
    tips = _advice(rsrq=-17.0, mimo='single', uplink='limit', jitter=9.0)
    assert len(tips) == 4
    assert any("загружена" in tip for tip in tips)
    assert any("MIMO" in tip for tip in tips)


def test_advice_texts_translated():
    core.set_language("en")
    try:
        for tip in _advice(rsrp=-85.0, sinr=2.0, rsrq=-17.0, mimo='imbalance',
                           uplink='limit', jitter=9.0):
            assert core.t(tip) != tip
    finally:
        core.set_language("ru")


# =========================================================
# health: непрерывность
# =========================================================

def test_curve_score_matches_thresholds():
    assert core.curve_score('rsrp', -80) == 100
    assert core.curve_score('rsrp', -90) == 80
    assert core.curve_score('rsrp', -100) == 50
    assert core.curve_score('sinr', 13) == 75
    assert core.curve_score('rsrp', -200) == 0
    assert core.curve_score('rsrp', None) == 0
    assert core.curve_score('unknown', 5) == 0
    assert core.curve_score('sinr', 40) == 100


def test_health_is_monotonic_and_responsive():
    prev = -1
    for rsrp in range(-125, -70):
        score, _, _ = core.calculate_overall_health(rsrp, 12)
        assert score >= prev
        prev = score
    a, _, _ = core.calculate_overall_health(-97, 10)
    b, _, _ = core.calculate_overall_health(-95, 10)
    assert b > a, "2 дБ улучшения должны быть видны на прогресс-баре"


# =========================================================
# TrendTracker
# =========================================================

def test_trend_collecting_then_up():
    tr = core.TrendTracker(lookback=3)
    states = [tr.push(v) for v in (-100, -99, -98, -96, -94, -92)]
    assert states[0] == core.TREND_COLLECTING
    assert states[-1] == core.TREND_UP
    assert tr.delta is not None and tr.delta > 0


def test_trend_down():
    tr = core.TrendTracker(lookback=3)
    for v in (-80, -81, -83, -85, -87, -90):
        state = tr.push(v)
    assert state == core.TREND_DOWN


def test_trend_ignores_noise():
    """Шум ±1 дБ не должен давать ↑/↓."""
    tr = core.TrendTracker(lookback=3)
    seen = {tr.push(v) for v in (-90, -91, -90, -89, -90, -91, -90, -89, -90, -91)}
    assert core.TREND_UP not in seen and core.TREND_DOWN not in seen


def test_trend_hysteresis_holds_state():
    tr = core.TrendTracker(lookback=3, enter=1.0, exit_=0.5)
    for v in (-100, -98, -96, -94, -92):
        tr.push(v)
    assert tr.state == core.TREND_UP
    tr.push(-92.2)          # Δ уменьшился, но выше порога выхода — держим ↑
    assert tr.state == core.TREND_UP


def test_trend_reverses_direction():
    tr = core.TrendTracker(lookback=3)
    for v in (-100, -97, -94, -91, -88):
        tr.push(v)
    assert tr.state == core.TREND_UP
    for v in (-92, -97, -102, -107, -112):
        tr.push(v)
    assert tr.state == core.TREND_DOWN
    for v in (-105, -98, -91, -84, -77):
        tr.push(v)
    assert tr.state == core.TREND_UP


def test_trend_reset():
    tr = core.TrendTracker()
    for v in (-100, -95, -90, -85, -80):
        tr.push(v)
    tr.reset()
    assert tr.state == core.TREND_COLLECTING and tr.delta is None


# =========================================================
# SignalState
# =========================================================

WALL = datetime.datetime(2026, 9, 24, 12, 0, 0)


def snap(rsrp=-90.0, sinr=10.0, pci=287, earfcn=1300, **kw):
    return core.Snapshot(rsrp=rsrp, sinr=sinr, rsrq=-9.0, rssi=-60.0,
                         pci=pci, earfcn=earfcn, bands=(3,), band_label="B3",
                         plmn="25002", enodeb=100, sector=1, **kw)


def test_state_peaks_and_delta():
    st = core.SignalState()
    st.ingest(snap(rsrp=-95, sinr=8), 1.0, WALL)
    st.ingest(snap(rsrp=-85, sinr=15), 2.0, WALL)
    st.ingest(snap(rsrp=-90, sinr=11), 3.0, WALL)
    assert st.peaks['rsrp'] == -85 and st.peaks['sinr'] == 15
    assert st.delta_to_peak('rsrp') == -5
    assert st.delta_to_peak('sinr') == -4
    assert st.delta_to_peak('nr_sinr') is None
    st.reset_peaks()
    assert st.peaks['rsrp'] is None


def test_state_session_minmax_outlives_history():
    st = core.SignalState(history=3)
    for i, v in enumerate((-70, -100, -90, -91, -92)):
        st.ingest(snap(rsrp=v), float(i), WALL)
    assert list(st.history['rsrp']) == [-90, -91, -92]
    assert st.session_max['rsrp'] == -70
    assert st.session_min['rsrp'] == -100


def test_state_jitter():
    st = core.SignalState()
    for i, v in enumerate((-90, -92, -88, -95, -89)):
        assert st.jitter() is None
        st.ingest(snap(rsrp=v), float(i), WALL)
    assert st.jitter() == 7


def test_state_cell_change_event_resets_trend():
    st = core.SignalState()
    for i, v in enumerate((-12, -10, -8, -6, -4)):
        st.ingest(snap(sinr=v), float(i), WALL)
    assert st.trend == core.TREND_UP
    change = st.ingest(snap(sinr=5, pci=112, earfcn=6300), 10.0, WALL)
    assert change is not None
    assert change.old == core.CellKey(1300, 287)
    assert change.new == core.CellKey(6300, 112)
    assert "PCI 112" in change.new_label
    assert change.at == "12:00:00"
    assert st.trend == core.TREND_COLLECTING
    assert list(st.events) == [change]


def test_state_no_event_for_unknown_cell():
    st = core.SignalState()
    st.ingest(snap(), 1.0, WALL)
    assert st.ingest(snap(pci=None), 2.0, WALL) is None
    assert not st.events


def test_state_top_cells_sorted_by_sinr():
    st = core.SignalState()
    st.ingest(snap(sinr=5, pci=1), 1.0, WALL)
    st.ingest(snap(sinr=15, pci=2), 2.0, WALL)
    st.ingest(snap(sinr=10, pci=3), 3.0, WALL)
    st.ingest(snap(sinr=12, pci=1), 4.0, WALL)
    top = st.top_cells(2)
    assert [c.key.pci for c in top] == [2, 1]
    assert top[1].best_sinr == 12 and top[1].samples == 2


def test_state_log_and_observed_bands():
    st = core.SignalState(log_max=2)
    for i in range(3):
        st.ingest(snap(rsrp=-90 - i), float(i), WALL)
    assert len(st.log) == 2
    assert st.log[-1]['rsrp'] == -92
    assert st.log[-1]['bands'] == "B3"
    assert st.log[-1]['ts'] == "2026-09-24T12:00:00"
    assert st.observed_bands == {3}


def test_state_staleness():
    st = core.SignalState()
    assert st.is_stale(0.0, 1.0)
    assert st.age(0.0) is None
    st.ingest(snap(), 10.0, WALL)
    assert not st.is_stale(12.0, 1.0)          # < 3 с (минимум)
    assert st.is_stale(13.5, 1.0)
    assert not st.is_stale(14.0, 2.0)          # 2.5 × 2 с = 5 с
    assert st.age(12.5) == 2.5


def test_state_trend_param_switch_resets():
    st = core.SignalState(trend_param='sinr')
    for i, v in enumerate((-12, -10, -8, -6, -4)):
        st.ingest(snap(sinr=v), float(i), WALL)
    st.set_trend_param('rsrp')
    assert st.trend == core.TREND_COLLECTING
    assert st.trend_param == 'rsrp'


def test_state_reset():
    st = core.SignalState(history=7, trend_param='rsrp')
    st.ingest(snap(), 1.0, WALL)
    st.reset()
    assert st.last is None and not st.log
    assert st.history['rsrp'].maxlen == 7 and st.trend_param == 'rsrp'


# =========================================================
# Band Lock
# =========================================================

CURRENT = NetModeSettings(lte_band='7FFFFFFFFFFFFFFF', network_band='3FFFFFFF',
                          network_mode='00')
ORIGINAL = NetModeSettings(lte_band='80005', network_band='2000000400380',
                           network_mode='03')


def test_net_mode_from_response():
    s = NetModeSettings.from_response(
        {'LTEBand': '80005', 'NetworkBand': '3FFFFFFF', 'NetworkMode': '03'})
    assert s.lte_mask == 0x80005 and s.network_mode == '03'
    assert NetModeSettings.from_response({'LTEBand': 'zz'}) is None
    assert NetModeSettings.from_response(None) is None


def test_plan_lock_preserves_network_band_and_forces_lte_only():
    plan = core.plan_lock(ORIGINAL, [3, 20], force_lte_only=True)
    assert plan == core.BandLockPlan('80004', '2000000400380', '03')


def test_plan_lock_5g_keeps_mode():
    cur = NetModeSettings('7FFFFFFFFFFFFFFF', '3FFFFFFF', '0803')
    plan = core.plan_lock(cur, [3], force_lte_only=False)
    assert plan.networkmode == '0803'


def test_plan_lock_without_current_uses_safe_defaults():
    plan = core.plan_lock(None, [1], force_lte_only=False)
    assert plan == core.BandLockPlan('1', '3FFFFFFF', '00')


def test_plan_lock_empty_rejected():
    with pytest.raises(ValueError):
        core.plan_lock(CURRENT, [], force_lte_only=True)


def test_plan_auto_restores_original_mode():
    """AUTO не должен включать 2G/3G, если до программы было «только 4G»."""
    cur = NetModeSettings('4', '3FFFFFFF', '03')
    assert core.plan_auto(cur, ORIGINAL) == core.BandLockPlan(
        '7FFFFFFFFFFFFFFF', '3FFFFFFF', '03')
    assert core.plan_auto(cur, None).networkmode == '03'
    assert core.plan_auto(None, None).networkmode == '00'


def test_plan_restore_exact():
    assert core.plan_restore(ORIGINAL) == core.BandLockPlan('80005', '2000000400380', '03')


def test_verify():
    plan = core.BandLockPlan('4', '3FFFFFFF', '03')
    assert core.verify(plan, NetModeSettings('4', '3FFFFFFF', '03'))
    assert not core.verify(plan, NetModeSettings('5', '3FFFFFFF', '03'))
    assert not core.verify(plan, NetModeSettings('4', '3FFFFFFF', '00'))
    assert not core.verify(plan, None)
    auto = core.BandLockPlan('7FFFFFFFFFFFFFFF', '3FFFFFFF', '00')
    assert core.verify(auto, NetModeSettings('20000800C5', '3FFFFFFF', '00'))


def test_locked_bands():
    assert core.locked_bands(NetModeSettings('4', '', '03')) == [3]
    assert core.locked_bands(CURRENT) == []
    assert core.locked_bands(None) is None
    assert core.locked_bands(NetModeSettings('45', '', '00'), [1, 3, 7]) == []


def test_modem_supports_5g():
    assert core.modem_supports_5g(['00', '03', '0803'])
    assert not core.modem_supports_5g(['00', '01', '02', '03'])
    assert not core.modem_supports_5g([])


def test_lockable_bands_ordering():
    assert core.lockable_bands([1, 3, 7, 20], [20, 3]) == [3, 20, 1, 7]
    assert core.lockable_bands([], [28]) == [28, 1, 3, 5, 7, 8, 20, 38, 40, 41]
    assert core.lockable_bands([1, 3], [7]) == [1, 3]


def test_lock_warnings():
    s = core.Snapshot(bands=(3, 1))
    assert core.lock_warnings([3, 1], s) == []
    w = core.lock_warnings([3], s)
    assert len(w) == 1 and "B1" in w[0]
    w = core.lock_warnings([20], s)
    assert len(w) == 1 and "B3" in w[0]
    assert core.lock_warnings([3], None) == []


def test_lock_warnings_english():
    core.set_language("en")
    try:
        w = core.lock_warnings([20], core.Snapshot(bands=(3,)))
        assert "B3" in w[0] and "excluded" in w[0]
    finally:
        core.set_language("ru")


# =========================================================
# errors
# =========================================================

class ApiError(Exception):
    def __init__(self, code):
        super().__init__(f"{code}: x")
        self.code = code


@pytest.mark.parametrize("code, kind", [
    (108006, core.ErrorKind.LOGIN_FATAL), (108007, core.ErrorKind.LOGIN_FATAL),
    (108003, core.ErrorKind.LOGIN_FATAL), (100003, core.ErrorKind.SESSION),
    (125002, core.ErrorKind.SESSION), (100002, core.ErrorKind.NOT_SUPPORTED),
    (100004, core.ErrorKind.BUSY), (100001, core.ErrorKind.OTHER),
])
def test_classify_codes(code, kind):
    assert core.classify_error(ApiError(code)) is kind


def test_classify_network():
    assert core.classify_error(ConnectionError("x")) is core.ErrorKind.NETWORK
    assert core.classify_error(TimeoutError()) is core.ErrorKind.NETWORK
    assert core.classify_error(ValueError("x")) is core.ErrorKind.OTHER


def test_error_code_tolerates_garbage():
    from core.errors import error_code
    bad = ApiError(0)
    bad.code = "not-a-number"
    assert error_code(bad) is None
    assert error_code(ValueError()) is None


def test_classify_real_library_exceptions():
    requests = pytest.importorskip("requests")
    from huawei_lte_api.exceptions import (
        LoginErrorUsernamePasswordWrongException,
        ResponseErrorLoginRequiredException,
    )
    assert core.classify_error(requests.exceptions.ConnectTimeout()) is core.ErrorKind.NETWORK
    assert core.classify_error(requests.exceptions.ConnectionError()) is core.ErrorKind.NETWORK
    assert core.classify_error(
        LoginErrorUsernamePasswordWrongException("x", 108006)) is core.ErrorKind.LOGIN_FATAL
    assert core.classify_error(
        ResponseErrorLoginRequiredException("x", 100003)) is core.ErrorKind.SESSION


def test_humanize():
    assert "пароль" in core.humanize_error(ApiError(108006))
    assert "не отвечает" in core.humanize_error(ConnectionError())
    assert core.humanize_error(ValueError("boom")) == "boom"
    assert core.humanize_error(ValueError()) == "ValueError"
    assert len(core.humanize_error(ValueError("x" * 500))) == 201
    core.set_language("en")
    try:
        assert "password" in core.humanize_error(ApiError(108006)).lower()
        assert "not responding" in core.humanize_error(OSError()).lower()
    finally:
        core.set_language("ru")
