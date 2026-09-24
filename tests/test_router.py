"""Тесты RouterSession/SessionWorker на симуляторе модема (без сети)."""
import threading
import time

import pytest

import core
import core.router as router_mod
from core.demo import DEMO_SUPPORTED_BANDS, DemoApiError, DemoModem, demo_factory
from core.errors import error_code


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def modem(clock):
    return DemoModem(clock=clock, seed=1)


@pytest.fixture
def session(modem):
    s = core.RouterSession("demo", "pw", factory=demo_factory(modem))
    s.open()
    yield s
    s.close()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(router_mod, "MIN_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(router_mod, "RECONNECT_DELAY_INITIAL", 0.01)
    monkeypatch.setattr(router_mod, "RECONNECT_DELAY_MAX", 0.02)


# =========================================================
# RouterSession
# =========================================================

def test_open_reads_capabilities(session):
    assert session.is_open
    assert session.device_info['DeviceName'].startswith('B636')
    assert session.supported_bands == list(DEMO_SUPPORTED_BANDS)
    assert session.access_modes == ['00', '01', '02', '03']
    assert not session.supports_5g
    assert session.original_net_mode.network_mode == '00'


def test_original_captured_once(session, modem):
    session.apply_plan(session.plan_band_lock([3]))
    session.open()                       # повторный вход (например, после reboot)
    assert session.original_net_mode.lte_band == '7FFFFFFFFFFFFFFF'


def test_fetch_merges_endpoints(session):
    raw = session.fetch()
    snap = core.build_snapshot(raw, session.supported_bands)
    assert snap.rsrp is not None and snap.pci in (287, 112, 45)
    assert raw['Numeric'] == '25002'
    assert raw['dataswitch'] == '1'
    assert 'CurrentNetworkTypeEx' in raw and 'TotalDownload' in raw
    assert 'CurrentMonthDownload' in raw


def _count(obj, name, counter):
    orig = getattr(obj, name)

    def wrapped(*a, **kw):
        counter[name] = counter.get(name, 0) + 1
        return orig(*a, **kw)
    setattr(obj, name, wrapped)


def test_fetch_schedule(session):
    client = session._client
    calls = {}
    _count(client.device, 'signal', calls)
    _count(client.net, 'current_plmn', calls)
    _count(client.monitoring, 'status', calls)
    _count(client.monitoring, 'traffic_statistics', calls)
    _count(client.monitoring, 'month_statistics', calls)
    _count(client.dial_up, 'mobile_dataswitch', calls)
    for _ in range(30):
        session.fetch()
    assert calls['signal'] == 30
    assert calls['status'] == 15 and calls['traffic_statistics'] == 15
    assert calls['current_plmn'] == 2          # тики 0 и 15 (сота не менялась)
    assert calls['mobile_dataswitch'] == 1
    assert calls['month_statistics'] == 1


def test_plmn_refetched_on_cell_change(session, modem, clock):
    client = session._client
    calls = {}
    _count(client.net, 'current_plmn', calls)
    session.fetch()
    session.apply_plan(session.plan_band_lock([20]))   # другая сота
    clock.advance(5)                                    # перерегистрация прошла
    session.fetch()
    assert calls['current_plmn'] == 2


def test_unsupported_endpoint_not_polled_again(session):
    client = session._client
    calls = {'n': 0}

    def month():
        calls['n'] += 1
        raise DemoApiError("No support", 100002)
    client.monitoring.month_statistics = month
    for _ in range(65):
        raw = session.fetch()
    assert calls['n'] == 1
    assert 'CurrentMonthDownload' not in raw


def test_optional_endpoint_network_error_propagates(session):
    def broken():
        raise ConnectionError("link down")
    session._client.monitoring.status = broken
    with pytest.raises(ConnectionError):
        session.fetch()


def test_optional_endpoint_other_error_is_skipped(session):
    def flaky():
        raise DemoApiError("Unknown", 100001)
    session._client.net.current_plmn = flaky
    raw = session.fetch()
    assert 'Numeric' not in raw and raw['rsrp']


def test_fetch_without_session_raises():
    s = core.RouterSession("demo", "pw", factory=demo_factory(DemoModem()))
    with pytest.raises(ConnectionError):
        s.fetch()


def test_open_failure_closes_connection(modem):
    made = []

    def factory(*a):
        conn, client = demo_factory(modem)(*a)
        made.append(client)

        def broken():
            raise DemoApiError("Unknown", 100001)
        client.device.information = broken
        return conn, client
    s = core.RouterSession("demo", "pw", factory=factory)
    with pytest.raises(DemoApiError):
        s.open()
    assert made[0].closed and not s.is_open


def test_capabilities_missing_is_tolerated(modem):
    def factory(*a):
        conn, client = demo_factory(modem)(*a)

        def unsupported():
            raise DemoApiError("No support", 100002)
        client.net.net_mode_list = unsupported
        client.net.net_mode = unsupported
        return conn, client
    s = core.RouterSession("demo", "pw", factory=factory)
    s.open()
    assert s.supported_bands == [] and s.original_net_mode is None
    assert s.plan_restore_original() is None
    assert s.read_config().locked is None
    s.close()


def test_band_lock_applies_and_verifies(session, modem, clock):
    plan = session.plan_band_lock([20])
    assert plan.networkmode == '03' and plan.networkband == '3FFFFFFF'
    assert session.apply_plan(plan) is True
    assert modem.lte_mask == 1 << 19 and modem.network_mode == '03'
    clock.advance(5)
    snap = core.build_snapshot(session.fetch(), session.supported_bands)
    assert snap.bands[0] == 20


def test_all_bands_restores_original_mode(session, modem):
    session.apply_plan(session.plan_band_lock([3]))
    plan = session.plan_all_bands()
    assert plan.networkmode == '00'
    assert session.apply_plan(plan)
    assert modem.network_mode == '00'


def test_restore_original(session, modem):
    session.apply_plan(session.plan_band_lock([1, 3]))
    plan = session.plan_restore_original()
    assert plan.lteband == '7FFFFFFFFFFFFFFF'
    assert session.apply_plan(plan)


def test_network_mode_arg():
    from huawei_lte_api.enums.net import NetworkModeEnum
    assert router_mod._network_mode_arg('03') is NetworkModeEnum.MODE_4G_ONLY
    assert router_mod._network_mode_arg('0803') == '0803'


def test_antenna(session, modem):
    session.set_antenna(0)
    assert modem.antenna == 0
    assert session.read_antenna() == 0
    cfg = session.read_config()
    assert cfg.antenna == 0 and cfg.locked == [] and cfg.net_mode is not None


def test_antenna_getter_fallback(session):
    device = session._client.device

    def unsupported():
        raise DemoApiError("No support", 100002)
    device.get_antenna_settings = unsupported
    device.antenna_type = lambda: {'unrelated': 'x'}
    device.antenna_status = lambda: {'antennatype': '2'}
    assert session.read_antenna() == 2      # ответил третий геттер


def test_reboot_then_session_expires(session, modem, clock):
    old_client = session._client
    session.reboot()
    with pytest.raises(ConnectionError):
        session.fetch()
    clock.advance(core.demo.REBOOT_SECONDS + 1)
    with pytest.raises(DemoApiError) as ei:
        old_client.device.signal()
    assert core.classify_error(ei.value) is core.ErrorKind.SESSION
    session.open()
    assert session.fetch()['rsrp']


def test_reattach(session, modem):
    sleeps = []
    assert session.reattach(pause=3.0, sleep=sleeps.append) is True
    assert modem.data_on is True
    assert sleeps == [3.0]


def test_reattach_failure_is_recovered_on_close(session, modem):
    dial = session._client.dial_up
    real = dial.set_mobile_dataswitch
    failures = {'n': 0}

    def flaky(dataswitch=0):
        if dataswitch == 1 and failures['n'] < 3:
            failures['n'] += 1
            raise ConnectionError("glitch")
        return real(dataswitch)
    dial.set_mobile_dataswitch = flaky
    assert session.reattach(pause=0, sleep=lambda s: None) is False
    assert modem.data_on is False
    session.close()                    # закрытие обязано включить данные
    assert modem.data_on is True


def test_diagnostics_are_read_only(session, modem):
    before = (modem.lte_mask, modem.network_mode, modem.antenna, modem.data_on)
    dumps, errors = session.collect_diagnostics()
    assert 'device_information' in dumps and 'net_net_mode_list' in dumps
    assert 'net_cell_info' in errors
    assert (modem.lte_mask, modem.network_mode, modem.antenna, modem.data_on) == before


def test_close_is_idempotent(session):
    session.close()
    session.close()
    assert not session.is_open


# =========================================================
# SessionWorker
# =========================================================

class Recorder:
    def __init__(self):
        self.connected = threading.Event()
        self.fatal = []
        self.statuses = []
        self.snaps = []
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def kwargs(self):
        return {
            'interval': lambda: 0.01,
            'on_connected': lambda info: self.connected.set(),
            'on_snapshot': self._snap,
            'on_status': lambda st, d, e: self.statuses.append(st),
            'on_fatal': self.fatal.append,
            'on_stopped': self.stopped.set,
        }

    def _snap(self, s):
        with self.lock:
            self.snaps.append(s)

    def wait_snaps(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if len(self.snaps) >= n:
                    return True
            time.sleep(0.01)
        return False

    def wait_status(self, st, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if st in self.statuses:
                return True
            time.sleep(0.01)
        return False


def test_worker_happy_path(fast, modem):
    rec = Recorder()
    s = core.RouterSession("demo", "pw", factory=demo_factory(modem))
    w = core.SessionWorker(s, **rec.kwargs())
    w.start()
    assert rec.connected.wait(5)
    assert rec.wait_snaps(3)
    assert isinstance(rec.snaps[0], core.Snapshot)
    w.stop()
    assert rec.stopped.wait(5)
    w.join(5)
    assert not s.is_open and not rec.fatal


def test_worker_login_fatal(fast):
    def factory(*a):
        raise DemoApiError("Username and Password wrong", 108006)
    rec = Recorder()
    w = core.SessionWorker(core.RouterSession("x", "bad", factory=factory), **rec.kwargs())
    w.start()
    w.join(5)
    assert not rec.connected.is_set()
    assert len(rec.fatal) == 1
    assert core.classify_error(rec.fatal[0]) is core.ErrorKind.LOGIN_FATAL
    assert rec.stopped.is_set()


def test_worker_recovers_after_reboot(fast, modem, clock):
    rec = Recorder()
    s = core.RouterSession("demo", "pw", factory=demo_factory(modem))
    w = core.SessionWorker(s, **rec.kwargs())
    w.start()
    assert rec.wait_snaps(2)
    s.reboot()
    assert rec.wait_status('reconnecting')
    clock.advance(core.demo.REBOOT_SECONDS + 1)
    assert rec.wait_status('connected')
    n = len(rec.snaps)
    assert rec.wait_snaps(n + 2)
    assert not rec.fatal
    w.stop()
    w.join(5)


def test_worker_no_auto_reconnect_reports_fatal(fast, modem):
    rec = Recorder()
    s = core.RouterSession("demo", "pw", factory=demo_factory(modem))
    w = core.SessionWorker(s, auto_reconnect=False, **rec.kwargs())
    w.start()
    assert rec.wait_snaps(1)
    s.reboot()
    w.join(5)
    assert len(rec.fatal) == 1
    assert core.classify_error(rec.fatal[0]) is core.ErrorKind.NETWORK


def test_worker_login_fatal_on_relogin_stops(fast, modem, clock):
    state = {'reject': False}
    base = demo_factory(modem)

    def factory(*a):
        if state['reject']:
            raise DemoApiError("Already login", 108003)
        return base(*a)
    rec = Recorder()
    w = core.SessionWorker(core.RouterSession("demo", "pw", factory=factory), **rec.kwargs())
    w.start()
    assert rec.wait_snaps(1)
    state['reject'] = True
    w.session.reboot()
    clock.advance(core.demo.REBOOT_SECONDS + 1)
    w.join(5)
    assert not w.is_alive()
    assert error_code(rec.fatal[0]) == 108003
    assert core.classify_error(rec.fatal[0]) is core.ErrorKind.LOGIN_FATAL


def test_worker_pause_resume(fast, modem):
    rec = Recorder()
    w = core.SessionWorker(core.RouterSession("demo", "pw", factory=demo_factory(modem)),
                           **rec.kwargs())
    w.start()
    assert rec.wait_snaps(2)
    w.pause()
    time.sleep(0.1)
    n = len(rec.snaps)
    time.sleep(0.2)
    assert len(rec.snaps) <= n + 1
    w.resume()
    assert rec.wait_snaps(n + 3)
    w.stop()
    w.join(5)


def test_worker_stop_while_paused(fast, modem):
    rec = Recorder()
    w = core.SessionWorker(core.RouterSession("demo", "pw", factory=demo_factory(modem)),
                           **rec.kwargs())
    w.start()
    assert rec.wait_snaps(1)
    w.pause()
    w.stop()
    w.join(5)
    assert not w.is_alive() and rec.stopped.is_set()


def test_worker_stop_during_login_closes_session(fast, modem):
    release = threading.Event()
    entered = threading.Event()
    base = demo_factory(modem)
    made = []

    def slow_factory(*a):
        entered.set()
        release.wait(5)
        conn, client = base(*a)
        made.append(client)
        return conn, client
    rec = Recorder()
    w = core.SessionWorker(core.RouterSession("demo", "pw", factory=slow_factory),
                           **rec.kwargs())
    w.start()
    assert entered.wait(5)
    w.stop()                      # пользователь нажал «Отключиться» во время входа
    release.set()
    w.join(5)
    assert not rec.connected.is_set()
    assert made and made[0].closed, "сессия, открытая после stop(), должна быть закрыта"
    assert not rec.fatal


def test_worker_skips_tick_on_snapshot_bug(fast, modem, monkeypatch):
    calls = {'n': 0}
    real = router_mod.build_snapshot

    def flaky(raw, known):
        calls['n'] += 1
        if calls['n'] == 1:
            raise ValueError("parser bug")
        return real(raw, known)
    monkeypatch.setattr(router_mod, "build_snapshot", flaky)
    rec = Recorder()
    w = core.SessionWorker(core.RouterSession("demo", "pw", factory=demo_factory(modem)),
                           **rec.kwargs())
    w.start()
    assert rec.wait_snaps(2)
    assert not rec.fatal and 'reconnecting' not in rec.statuses
    w.stop()
    w.join(5)
