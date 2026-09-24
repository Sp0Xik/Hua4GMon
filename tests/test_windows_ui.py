"""Сквозной тест Windows-интерфейса (Tk) в тестовом режиме.

Запускается, если доступен Tk и дисплей (локально: xvfb-run pytest).
Проходит те же действия, что пользователь: подключение, опрос, Band Lock,
AUTO/возврат, антенна, перезагрузка с автопереподключением, смена языка,
крышный режим, CSV, диагностика, белые списки, отключение.
"""
import json
import os
import sys
import time

import pytest

tk = pytest.importorskip("tkinter")
if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    pytest.skip("нет дисплея для Tk", allow_module_level=True)

import core  # noqa: E402
import core.demo as demo_mod  # noqa: E402
import core.router as router_mod  # noqa: E402
import main  # noqa: E402


def pump(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(router_mod, "RECONNECT_DELAY_INITIAL", 0.05)
    monkeypatch.setattr(router_mod, "RECONNECT_DELAY_MAX", 0.1)
    monkeypatch.setattr(demo_mod, "REBOOT_SECONDS", 0.5)
    monkeypatch.setattr(demo_mod, "REREGISTER_SECONDS", 0.1)
    monkeypatch.setattr(main.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(main.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(main.messagebox, "showwarning", lambda *a, **k: None)
    core.set_language("ru")
    root = tk.Tk()
    a = main.Hua4GMon(root)
    a.update_interval.set('0.5')
    yield a
    a.on_closing()
    core.set_language("ru")


def online(app):
    app.start_demo()
    assert pump(app, lambda: app.link == 'online' and len(app.state.log) >= 2)


def test_demo_monitoring(app):
    online(app)
    assert "PCI" in app.tower_line.cget('text')
    assert app.health_progress.cget('value') > 0
    assert app.lbl_vars['rsrp']['val'].cget('text') != "-"
    assert "SINR" in app.dir_frame.cget('text')
    app.graph_param.set('rsrp')
    app._on_graph_param()
    assert "RSRP" in app.dir_frame.cget('text')
    assert app.state.trend_param == 'rsrp'
    app.reset_peaks()
    assert app.state.peaks['rsrp'] is None
    assert sorted(app.band_vars) == sorted(set(demo_mod.DEMO_SUPPORTED_BANDS))


def test_band_lock_flow(app):
    online(app)
    modem = app.demo_modem
    app.mark_current_bands()
    assert {b for b, v in app.band_vars.items() if v.get()} == set(app.state.last.bands)
    for b, v in app.band_vars.items():
        v.set(b == 20)
    app.apply_bands()
    assert pump(app, lambda: not app._busy)
    assert modem.lte_mask == 1 << 19 and modem.network_mode == '03'
    assert pump(app, lambda: "B20" in app.lock_state_lbl.cget('text'))
    app.restore_bands()
    assert pump(app, lambda: not app._busy)
    assert modem.lte_mask == core.constants.LTE_ALL_MASK and modem.network_mode == '00'
    assert pump(app, lambda: "AUTO" in app.lock_state_lbl.cget('text'))


def test_antenna_and_reattach(app):
    online(app)
    app.antenna_var.set("Внешняя")
    app.apply_antenna()
    assert pump(app, lambda: not app._busy)
    assert app.demo_modem.antenna == 1
    assert "Внешняя" in app.net_msg.cget('text')
    app.reattach()
    assert pump(app, lambda: not app._busy, timeout=15)
    assert app.demo_modem.data_on


def test_reboot_reconnects_automatically(app):
    app.reconnect_var.set(False)
    online(app)
    app.reboot_router()
    assert pump(app, lambda: app.link == 'reconnecting', timeout=10)
    assert pump(app, lambda: app.link == 'online', timeout=15)
    n = len(app.state.log)
    assert pump(app, lambda: len(app.state.log) > n)


def test_staleness_is_shown(app):
    online(app)
    app.worker.pause()
    assert pump(app, lambda: "⚠" in app.health_text_lbl.cget('text'), timeout=8)
    app.worker.resume()


def test_language_switch_keeps_state(app):
    online(app)
    app.lang_var.set("English")
    app._on_language_change()
    try:
        assert app.status_label.cget('text') == "🧪 Test mode"
        assert app.tower_line.cget('text') != "—"
        assert "trend" in app.dir_frame.cget("text").lower()
        assert pump(app, lambda: len(app.state.log) >= 3)
    finally:
        core.set_language("ru")


def test_roof_mode(app):
    online(app)
    app.toggle_roof_mode()
    assert app.roof_win is not None
    assert pump(app, lambda: "SINR" in app.r_primary.cget('text'))
    app.toggle_roof_mode()
    assert app.roof_win is None


def test_exports(app, tmp_path, monkeypatch):
    online(app)
    csv_path = tmp_path / "s.csv"
    diag_path = tmp_path / "d.json"
    monkeypatch.setattr(main.filedialog, "asksaveasfilename",
                        lambda **k: str(csv_path if k['defaultextension'] == '.csv' else diag_path))
    app.export_csv()
    assert csv_path.read_text(encoding='utf-8-sig').startswith("ts;rsrp")
    app.save_diagnostics()
    assert pump(app, lambda: not app._busy and diag_path.exists())
    report = json.loads(diag_path.read_text(encoding='utf-8'))
    assert report['app_version'] == core.__version__
    assert '860000000000001' not in diag_path.read_text(encoding='utf-8')


def test_whitelist_render(app, monkeypatch):
    report = core.WhitelistReport(
        white=[core.ProbeResult("gosuslugi.ru", 443, True, True, "OK")],
        neutral=[core.ProbeResult("example.com", 443, True, False, "TLS")],
        title="⚠ Вероятна фильтрация (белые списки)", detail="d", color="#d63031")
    monkeypatch.setattr(main, "run_whitelist_check", lambda: report)
    app._start_whitelist_check()
    assert pump(app, lambda: app.wl_title.cget('text') == report.title)
    assert "❌" in app.wl_labels["example.com"].cget('text')


def test_invalid_ip_and_discovery(app, monkeypatch):
    app.ip_entry.delete(0, tk.END)
    app.ip_entry.insert(0, "999.1.1.1")
    app.start_connect()
    assert app.worker is None and "999.1.1.1" in app.conn_msg.cget('text')
    monkeypatch.setattr(main, "discover_router",
                        lambda c: core.FoundRouter("192.168.3.1", "B535"))
    app.start_discovery()
    assert pump(app, lambda: app.ip_entry.get() == "192.168.3.1")
    assert "B535" in app.conn_msg.cget('text')


def test_disconnect_is_immediate(app):
    online(app)
    worker = app.worker
    start = time.monotonic()
    app.disconnect()
    assert time.monotonic() - start < 0.2
    assert app.link == 'offline'
    worker.join(5)
    assert not worker.is_alive()


def test_beep_frequency():
    assert main.beep_frequency('rsrp', 0) == 2500
    assert main.beep_frequency('sinr', -10) == 1400
    assert main.beep_frequency('rsrp', -100) == 300
