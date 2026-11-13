"""Интеграция с настоящей huawei-lte-api 2.x через поддельный HTTP-роутер.

Проверяет миграцию на 2.0.1 без железа: вход (SHA256 + CSRF), опрос,
запись net-mode с повторным чтением, enum-аргументы, перезагрузка через
set_control, выход, защиту от блокировки входа и утечки пароля в лог.
"""
import logging
import threading
import time

import pytest

pytest.importorskip("huawei_lte_api")

import core  # noqa: E402
import core.router as router_mod  # noqa: E402
from tests.fake_huawei import PASSWORD, FakeRouter  # noqa: E402


@pytest.fixture
def router():
    with FakeRouter() as fr:
        yield fr


def test_library_version_pinned():
    assert core.library_version() == "2.0.1"


def test_full_flow(router):
    s = core.RouterSession(router.address, PASSWORD, timeout=3)
    info = s.open()
    assert info['DeviceName'] == 'B636-336'
    assert s.supported_bands == [1, 3, 7, 20]
    assert s.original_net_mode.lte_band == '7FFFFFFFFFFFFFFF'

    snap = core.build_snapshot(s.fetch(), s.supported_bands)
    assert (snap.rsrp, snap.sinr, snap.pci) == (-88.0, 14.0, 287)
    assert snap.operator == 'MegaFon' and snap.freq_mhz == 1815.0
    assert snap.rat == 'lte' and snap.ca is False and snap.data_enabled is True

    plan = s.plan_band_lock([3, 20])
    assert s.apply_plan(plan) is True
    assert router.state.lte_band == '80004'
    assert router.state.network_mode == '03'
    assert router.state.network_band == '3FFFFFFF'      # 2G/3G не тронуты

    assert s.apply_plan(s.plan_all_bands()) is True
    assert router.state.network_mode == '00'

    s.set_antenna(1)
    assert router.state.antenna == 1 and s.read_antenna() == 1

    assert s.reattach(pause=0.01) is True
    assert router.state.dataswitch == 1

    dumps, errors = s.collect_diagnostics()
    assert 'device_signal' in dumps
    s.close()
    assert router.state.logouts == 1


def test_month_statistics_unsupported_is_tolerated(router):
    s = core.RouterSession(router.address, PASSWORD, timeout=3)
    s.open()
    for _ in range(3):
        raw = s.fetch()
    assert 'CurrentMonthDownload' not in raw
    assert router.state.gets.count('/api/monitoring/month_statistics') == 1
    s.close()


def test_reboot_uses_set_control_and_relogin(router):
    s = core.RouterSession(router.address, PASSWORD, timeout=3)
    s.open()
    s.reboot()
    assert router.state.reboots == 1
    assert ('/api/device/control', {'Control': '1'}) in router.state.posts
    with pytest.raises(Exception) as ei:
        s.fetch()
    assert core.classify_error(ei.value) is core.ErrorKind.SESSION
    s.open()
    assert s.fetch()['rsrp'] == '-88dBm'
    s.close()


def test_wrong_password_single_attempt(router, monkeypatch):
    """Неверный пароль: ровно одна попытка входа — без блокировки роутера."""
    monkeypatch.setattr(router_mod, "RECONNECT_DELAY_INITIAL", 0.01)
    fatal = []
    done = threading.Event()
    w = core.SessionWorker(
        core.RouterSession(router.address, "wrong", timeout=3),
        interval=lambda: 0.01, on_connected=lambda i: None,
        on_snapshot=lambda s: None, on_status=lambda *a: None,
        on_fatal=fatal.append, on_stopped=done.set)
    w.start()
    assert done.wait(10)
    assert len(fatal) == 1
    assert core.classify_error(fatal[0]) is core.ErrorKind.LOGIN_FATAL
    assert "пароль" in core.humanize_error(fatal[0])
    logins = [p for p, _ in router.state.posts if p == '/api/user/login']
    assert len(logins) == 1


def test_worker_real_library_polls_and_logs_out(router, monkeypatch):
    monkeypatch.setattr(router_mod, "MIN_POLL_INTERVAL", 0.01)
    snaps = []
    done = threading.Event()
    w = core.SessionWorker(
        core.RouterSession(router.address, PASSWORD, timeout=3),
        interval=lambda: 0.01, on_connected=lambda i: None,
        on_snapshot=snaps.append, on_status=lambda *a: None,
        on_fatal=lambda e: None, on_stopped=done.set)
    w.start()
    end = time.monotonic() + 10
    while len(snaps) < 3 and time.monotonic() < end:
        time.sleep(0.02)
    assert len(snaps) >= 3
    w.stop()
    assert done.wait(10)
    assert router.state.logouts == 1


def test_password_not_logged(router, caplog):
    core.configure_library_logging()
    caplog.set_level(logging.DEBUG)
    s = core.RouterSession(router.address, PASSWORD, timeout=3)
    s.open()
    s.fetch()
    s.close()
    assert PASSWORD not in caplog.text


# ---------- автопоиск ----------

def test_probe_finds_fake_router(router):
    found = core.probe_huawei('127.0.0.1', router.port, timeout=2)
    assert found == core.FoundRouter('127.0.0.1', 'B636-336')


def test_discover_prefers_candidate_order(router):
    found = core.discover_router(['127.0.0.2', '127.0.0.1'], port=router.port, timeout=0.5)
    assert found is not None and found.ip == '127.0.0.1'
    assert core.discover_router([], port=router.port) is None


# ---------- самопроверка сборки (Hua4GMon.exe --self-test) ----------

def test_library_self_test_passes():
    assert core.library_self_test() == [
        f"huawei-lte-api {core.library_version()}", "XML: OK", "RSA PKCS#1 v1.5 + OAEP: OK"]


def test_library_self_test_detects_broken_rsa(monkeypatch):
    from huawei_lte_api.Tools import Tools
    monkeypatch.setattr(Tools, "rsa_encrypt", staticmethod(lambda *a: b"0" * 512))
    with pytest.raises(RuntimeError, match="PKCS#1 v1.5"):
        core.library_self_test()


def test_library_self_test_detects_broken_xml(monkeypatch):
    import xmltodict
    monkeypatch.setattr(xmltodict, "parse", lambda text: {})
    with pytest.raises(RuntimeError, match="xmltodict"):
        core.library_self_test()


@pytest.mark.parametrize('status', [401, 403, 500, 503])
def test_get_http_failure_is_not_an_empty_measurement(router, status):
    import requests
    s = core.RouterSession(router.address, PASSWORD, timeout=3)
    s.open()
    try:
        router.state.http_errors['GET', '/api/device/signal'] = status
        with pytest.raises(requests.HTTPError) as error:
            s.fetch()
        assert error.value.response.status_code == status
        assert core.classify_error(error.value) is core.ErrorKind.NETWORK
        del router.state.http_errors['GET', '/api/device/signal']
        assert s.fetch()['rsrp'] == '-88dBm'
        assert router.state.logins == 1
    finally:
        s.close()


def test_commands_are_not_automatically_repeated_on_http_failure(router):
    import requests
    s = core.RouterSession(router.address, PASSWORD, timeout=3)
    s.open()
    try:
        router.state.http_errors['POST', '/api/device/control'] = 503
        with pytest.raises(requests.HTTPError):
            s.reboot()
        assert sum(path == '/api/device/control' for path, _ in router.state.posts) == 1
    finally:
        s.close()


def test_router_ignores_environment_proxy(router, monkeypatch):
    for key in ('HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.setenv(key, 'http://127.0.0.1:1')
    for key in ('NO_PROXY', 'no_proxy'):
        monkeypatch.setenv(key, '')
    s = core.RouterSession(router.address, PASSWORD, timeout=1)
    try:
        assert s.open()['DeviceName'] == 'B636-336'
        assert s.fetch()['rsrp'] == '-88dBm'
    finally:
        s.close()
    assert router.state.logouts == 1


@pytest.mark.parametrize('failure', ['initial_http', 'password', 'logout', 'none'])
def test_owned_http_session_is_closed_on_success_and_failure(router, monkeypatch, failure):
    import requests
    from huawei_lte_api.exceptions import LoginErrorUsernamePasswordWrongException
    closed = []
    original_close = requests.Session.close

    def record_close(transport):
        closed.append(transport)
        original_close(transport)

    monkeypatch.setattr(requests.Session, 'close', record_close)
    if failure == 'initial_http':
        router.state.http_errors['GET', '/'] = 503
    password = 'wrong' if failure == 'password' else PASSWORD
    s = core.RouterSession(router.address, password, timeout=3)
    if failure in ('initial_http', 'password'):
        expected = requests.HTTPError if failure == 'initial_http' else LoginErrorUsernamePasswordWrongException
        with pytest.raises(expected):
            s.open()
        assert not s.is_open
    else:
        s.open()
        if failure == 'logout':
            router.state.http_errors['POST', '/api/user/logout'] = 503
        s.close()
        assert not s.is_open
    assert len(closed) == 1


def test_http_error_response_is_released(router, monkeypatch):
    import requests
    closed = []
    original_close = requests.Response.close

    def record_close(response):
        closed.append(response.status_code)
        original_close(response)

    monkeypatch.setattr(requests.Response, 'close', record_close)
    router.state.http_errors['GET', '/'] = 503
    with pytest.raises(requests.HTTPError):
        core.RouterSession(router.address, PASSWORD, timeout=3).open()
    assert 503 in closed
