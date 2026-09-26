"""Сквозной тест Windows-интерфейса (Tk) в тестовом режиме.

Запускается, если доступен Tk и дисплей (локально: xvfb-run pytest).
Проходит те же действия, что пользователь: подключение, опрос, Band Lock,
AUTO/возврат, антенна, перезагрузка с автопереподключением, смена языка,
крышный режим, CSV, диагностика, белые списки, отключение; а также
запуск: самопроверку --self-test и закрытие заставки.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import types

import pytest

tk = pytest.importorskip("tkinter")
if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    pytest.skip("нет дисплея для Tk", allow_module_level=True)

import core  # noqa: E402
import core.demo as demo_mod  # noqa: E402
import core.router as router_mod  # noqa: E402
import main  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def pump(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def _create_root():
    """Один интерпретатор Tk на весь модуль — как в самой программе.

    На Windows-раннерах GitHub повторное создание Tk() в одном процессе
    изредка падает на чтении собственных скриптов Tcl (TclError
    «couldn't read file …init.tcl: No error»). Приложение создаёт Tk
    один раз, поэтому тесты делают так же; повтор с паузой — на случай
    такого же разового сбоя при единственном создании.
    """
    for attempt in range(3):
        try:
            return tk.Tk()
        except tk.TclError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    raise AssertionError("unreachable")


@pytest.fixture(scope="module")
def shared_app():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(router_mod, "RECONNECT_DELAY_INITIAL", 0.05)
        mp.setattr(router_mod, "RECONNECT_DELAY_MAX", 0.1)
        mp.setattr(demo_mod, "REBOOT_SECONDS", 0.5)
        mp.setattr(demo_mod, "REREGISTER_SECONDS", 0.1)
        mp.setattr(main.messagebox, "askyesno", lambda *a, **k: True)
        mp.setattr(main.messagebox, "showinfo", lambda *a, **k: None)
        mp.setattr(main.messagebox, "showwarning", lambda *a, **k: None)
        core.set_language("ru")
        a = main.Hua4GMon(_create_root())
        yield a
        a.on_closing()
        core.set_language("ru")


@pytest.fixture
def app(shared_app):
    """Общее окно, приведённое к исходному состоянию перед каждым тестом."""
    a = shared_app
    a.disconnect()
    a._close_roof()
    core.set_language("ru")
    if a.lang_var.get() != core.LANGUAGES["ru"]:
        a.rebuild_ui()
    a.reconnect_var.set(True)
    a.update_interval.set('0.5')
    a.graph_param.set('sinr')
    a._on_graph_param()
    a.root.update()
    yield a
    a.disconnect()
    a._close_roof()


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
    assert app.dir_label.cget('text') == "⚠"          # без данных — без указаний
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
    assert time.monotonic() - start < 0.5          # не ждёт ответа роутера (запас на медленный CI)
    assert app.link == 'offline'
    worker.join(5)
    assert not worker.is_alive()


def test_beep_frequency():
    assert main.beep_frequency('rsrp', 0) == 2500
    assert main.beep_frequency('sinr', -10) == 1400
    assert main.beep_frequency('rsrp', -100) == 300


# ---------- запуск: самопроверка и заставка ----------

def test_self_test_of_program(tmp_path):
    """Тот же вызов, что CI делает для собранного .exe — в отдельном процессе."""
    report = tmp_path / "report.txt"
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--self-test", str(report)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = report.read_text(encoding="utf-8")
    assert f"huawei-lte-api {core.library_version()}" in text
    assert "window built" in text and "\nOK in " in text
    assert text.strip() == proc.stdout.strip()


def test_self_test_reports_failure(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("no crypto")
    monkeypatch.setattr(main, "library_self_test", broken)
    report = tmp_path / "report.txt"
    assert main.run_self_test(str(report)) == 1
    text = report.read_text(encoding="utf-8")
    assert "ERROR: RuntimeError: no crypto" in text and "\nFAIL in " in text


def test_self_test_argument():
    assert main.parse_args([]).self_test is None
    assert main.parse_args(["--self-test"]).self_test == ""
    assert main.parse_args(["--self-test", "r.txt"]).self_test == "r.txt"


def test_close_splash(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyi_splash", None)     # не собранный .exe
    main.close_splash()
    calls = []
    monkeypatch.setitem(sys.modules, "pyi_splash",
                        types.SimpleNamespace(close=lambda: calls.append(1)))
    main.close_splash()
    assert calls == [1]

    def gone():
        raise OSError("bootloader closed the socket")
    monkeypatch.setitem(sys.modules, "pyi_splash", types.SimpleNamespace(close=gone))
    main.close_splash()


def test_splash_status(monkeypatch):
    shown = []
    monkeypatch.setitem(sys.modules, "pyi_splash",
                        types.SimpleNamespace(update_text=shown.append))
    main.splash_status("Построение окна…")
    assert shown == ["Построение окна…"]

    def not_started(_text):
        raise RuntimeError("This module is not initialized")
    monkeypatch.setitem(sys.modules, "pyi_splash", types.SimpleNamespace(update_text=not_started))
    main.splash_status("x")
    monkeypatch.setitem(sys.modules, "pyi_splash", None)     # запуск из исходников
    main.splash_status("x")


def test_fmt_num_keeps_integers():
    assert main.fmt_num(1048575) == "1048575"
    assert main.fmt_num(-8.4) == "-8.4" and main.fmt_num(None) == "-"


def test_shortcuts_work_with_russian_layout_and_caps(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "reset_peaks", lambda: calls.append('r'))
    monkeypatch.setattr(app, "_toggle_sound", lambda: calls.append('m'))
    assert app.root.bind_all("<Control-KeyPress>")
    for sym in ("r", "R", "Cyrillic_ka", "Cyrillic_KA", "m", "M",
                "Cyrillic_softsign", "Cyrillic_SOFTSIGN", "c"):
        app._on_ctrl_key(types.SimpleNamespace(keysym=sym, keycode=0))
    assert calls == ['r'] * 4 + ['m'] * 4
    calls.clear()
    # Tk на Windows: keysym — символ раскладки (к, ь), keycode — VK_R / VK_M.
    monkeypatch.setattr(main.sys, "platform", "win32")
    for code, sym in ((0x52, "\u043a"), (0x4D, "\u044c"), (0x43, "c")):
        app._on_ctrl_key(types.SimpleNamespace(keysym=sym, keycode=code))
    assert calls == ['r', 'm']


def test_reboot_only_when_online(app):
    online(app)
    app.link = 'connecting'
    app.worker.auto_reconnect = False
    app.reboot_router()
    assert app.worker.auto_reconnect is False      # флаг не включается без команды
    assert "Сначала подключитесь" in app.net_msg.cget('text')
    app.link = 'online'
    app._busy = True                               # идёт другая команда
    try:
        app.reboot_router()
        assert app.worker.auto_reconnect is False
    finally:
        app._busy = False


def test_roof_window_follows_on_top(app, monkeypatch):
    """Крышное окно получает «Поверх окон» сразу при открытии (Xvfb без
    оконного менеджера не отражает -topmost, поэтому проверяется вызов)."""
    seen = []
    real = app.toggle_on_top
    monkeypatch.setattr(app, "toggle_on_top", lambda: (seen.append(app.roof_win), real()))
    app.toggle_roof_mode()
    assert seen and seen[-1] is app.roof_win is not None


def test_lock_state_survives_language_switch(app):
    online(app)
    assert pump(app, lambda: "AUTO" in app.lock_state_lbl.cget('text'))
    app.lang_var.set("English")
    app._on_language_change()
    try:
        assert app.lock_state_lbl.cget('text') == core.t("Сейчас на модеме: AUTO (все бэнды)")
        assert "AUTO" in app.lock_state_lbl.cget('text')
    finally:
        core.set_language("ru")


def test_band_grid_not_rebuilt_every_poll(app, monkeypatch):
    """Бэнд, которого нет в списке модема, не пересоздаёт сетку на каждом опросе."""
    online(app)
    app.session.supported_bands = [7]
    rebuilds = []
    monkeypatch.setattr(app, "_rebuild_band_grid", rebuilds.append)
    n = len(app.state.log)
    assert pump(app, lambda: len(app.state.log) >= n + 4)
    assert len(rebuilds) <= 1


def test_busy_buttons_stay_disabled_after_language_switch(app):
    online(app)
    app._busy = True
    try:
        app.lang_var.set("English")
        app._on_language_change()
        assert all(str(b.cget('state')) == 'disabled' for b in app._net_buttons)
    finally:
        app._busy = False
        core.set_language("ru")


def test_diagnostics_file_error_is_not_router_error(app, tmp_path, monkeypatch):
    online(app)
    monkeypatch.setattr(main.filedialog, "asksaveasfilename",
                        lambda **k: str(tmp_path / "missing" / "d.json"))
    app.save_diagnostics()
    assert pump(app, lambda: not app._busy)
    text = app.net_msg.cget('text')
    assert "Не удалось записать файл" in text and "Команда не выполнена" not in text
