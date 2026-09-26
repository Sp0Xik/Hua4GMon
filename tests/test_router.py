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


def _busy_net_mode(session):
    """Следующее чтение net-mode ответит «роутер занят» (100004)."""
    net = session._client.net
    real = net.net_mode
    calls = {'n': 0}

    def busy_once():
        calls['n'] += 1
        if calls['n'] == 1:
            raise DemoApiError("Busy", 100004)
        return real()
    net.net_mode = busy_once


def test_band_lock_keeps_2g3g_when_read_fails(session, modem):
    """Не прочитали настройки перед записью — берутся исходные, а не значения
    по умолчанию: NetworkBand (2G/3G) модема не меняется."""
    modem.network_band = '2000000400380'
    session.original_net_mode = session.read_net_mode()
    _busy_net_mode(session)
    plan = session.plan_band_lock([20])
    assert plan.networkband == '2000000400380'
    assert session.apply_plan(plan) is True
    assert modem.network_band == '2000000400380' and modem.lte_mask == 1 << 19
    _busy_net_mode(session)
    assert session.plan_all_bands().networkband == '2000000400380'


def test_band_lock_refused_without_any_settings(session, modem):
    session.original_net_mode = None
    _busy_net_mode(session)
    writes = []
    session._client.net.set_net_mode = lambda *a: writes.append(a)
    with pytest.raises(core.NetModeUnavailable):
        session.plan_band_lock([20])
    _busy_net_mode(session)
    with pytest.raises(core.NetModeUnavailable):
        session.plan_all_bands()
    assert not writes
    assert "модем не изменён" in core.humanize_error(core.NetModeUnavailable())


@pytest.mark.parametrize("busy", [1, 2])
def test_band_lock_keeps_5g_mode_when_modes_unread(modem, busy):
    """net-mode-list не прочитан при подключении (busy=1) или и перед
    записью (busy=2): 5G-модем не переводится в «только 4G»."""
    left = {'busy': busy}

    def factory(*a):
        conn, client = demo_factory(modem)(*a)
        real = client.net.net_mode_list

        def mode_list():
            if left['busy']:
                left['busy'] -= 1
                raise DemoApiError("Busy", 100004)
            return {**real(), 'AccessList': {'Access': ['00', '03', '0803']}}
        client.net.net_mode_list = mode_list
        return conn, client
    modem.network_mode = '0803'
    s = core.RouterSession("demo", "pw", factory=factory)
    s.open()
    assert not s.locks_lte_only
    plan = s.plan_band_lock([3])
    assert plan.networkmode == '0803'
    assert s.apply_plan(plan) and modem.network_mode == '0803'
    assert s.supports_5g is (busy == 1)
    s.close()


def test_band_lock_4g_modem_with_modes_unread_at_connect(modem):
    """4G-модем, занятый при подключении: режимы перечитываются перед
    записью, и Band Lock, как обычно, включает «только 4G»."""
    busy = {'n': 1}

    def factory(*a):
        conn, client = demo_factory(modem)(*a)
        real = client.net.net_mode_list

        def mode_list():
            if busy['n']:
                busy['n'] -= 1
                raise DemoApiError("Busy", 100004)
            return real()
        client.net.net_mode_list = mode_list
        return conn, client
    s = core.RouterSession("demo", "pw", factory=factory)
    s.open()
    assert not s.caps_known and not s.locks_lte_only
    assert s.plan_band_lock([3]).networkmode == '03'
    assert s.caps_known and s.locks_lte_only
    s.close()


def test_original_captured_before_first_write(session, modem):
    session.original_net_mode = None            # при подключении роутер был занят
    session.apply_plan(session.plan_band_lock([3]))
    assert session.original_net_mode.lte_band == '7FFFFFFFFFFFFFFF'
    assert session.plan_restore_original().lteband == '7FFFFFFFFFFFFFFF'


def test_write_without_read_back_is_unconfirmed(session, modem):
    plan = session.plan_band_lock([20])

    def timeout():
        raise TimeoutError("read-back")
    session._client.net.net_mode = timeout
    assert session.apply_plan(plan) is False     # «не подтверждено», не исключение
    assert modem.lte_mask == 1 << 19


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


def test_reattach_lost_response_still_restores_data(session, modem):
    """Роутер выключил данные, но ответ потерялся — close() всё равно включит."""
    dial = session._client.dial_up
    real = dial.set_mobile_dataswitch

    def lost(dataswitch=0):
        real(dataswitch)
        if dataswitch == 0:
            raise TimeoutError("response lost")
        return "OK"
    dial.set_mobile_dataswitch = lost
    with pytest.raises(TimeoutError):
        session.reattach(pause=0, sleep=lambda s: None)
    assert modem.data_on is False
    session.close()
    assert modem.data_on is True


def test_relogin_restores_mobile_data(session, modem):
    """Старая сессия не смогла включить данные — включает новая после входа."""
    session._client.dial_up.set_mobile_dataswitch(0)
    session._data_off_pending = True

    def dead(dataswitch=0):
        raise ConnectionError("old session")
    session._client.dial_up.set_mobile_dataswitch = dead
    session.open()
    assert modem.data_on is True and not session._data_off_pending


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


def test_worker_relogin_without_data_is_not_connected(fast):
    """Вход удаётся, а опрос нет: статус «переподключаюсь», без мигания
    «подключено» и без повторного входа на каждой секунде."""
    class Expired:
        supported_bands: list[int] = []
        opens = 0

        def open(self):
            Expired.opens += 1
            return {}

        def close(self):
            pass

        def fetch(self):
            raise DemoApiError("expired", 100003)

    rec = Recorder()
    delays = []
    kwargs = rec.kwargs()
    kwargs['on_status'] = lambda st, d, e: (rec.statuses.append(st), delays.append(d))
    w = core.SessionWorker(Expired(), **kwargs)
    w.start()
    time.sleep(0.3)
    w.stop()
    w.join(2)
    assert rec.statuses and set(rec.statuses) == {'reconnecting'}
    # Пауза перед входом растёт до максимума, успешный вход её не сбрасывает.
    assert Expired.opens >= 2
    assert delays == sorted(delays) and delays[-1] == router_mod.RECONNECT_DELAY_MAX


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
