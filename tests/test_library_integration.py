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
