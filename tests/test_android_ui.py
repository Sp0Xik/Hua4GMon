"""Сквозной тест Android-интерфейса (Kivy) в тестовом режиме.

Запускается, если установлен Kivy и есть дисплей (локально:
xvfb-run pytest). Платформенные вызовы Android (pyjnius) на десктопе
отключаются сами — проверяется вся логика UI поверх core.
"""
import os
import sys
import time

import pytest

if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    pytest.skip("нет дисплея для Kivy", allow_module_level=True)
os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_NO_CONSOLELOG", "1")
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")
pytest.importorskip("kivy")

import core  # noqa: E402
import core.demo as demo_mod  # noqa: E402
import core.router as router_mod  # noqa: E402


@pytest.fixture(scope="module")
def app():
    patches = [(router_mod, "RECONNECT_DELAY_INITIAL", 0.05),
               (router_mod, "RECONNECT_DELAY_MAX", 0.1),
               (demo_mod, "REBOOT_SECONDS", 0.5),
               (demo_mod, "REREGISTER_SECONDS", 0.1)]
    saved = [(m, n, getattr(m, n)) for m, n, _ in patches]
    for m, n, v in patches:
        setattr(m, n, v)
    import android_main
    a = android_main.Hua4GMonApp()
    a._run_prepare()
    a.confirm = lambda title, msg, yes, on_yes: on_yes()   # «Да» в диалогах
    yield a
    a.disconnect()
    a.stop()
    for m, n, v in saved:
        setattr(m, n, v)
    core.set_language("ru")


def pump(cond, timeout=10.0):
    from kivy.base import EventLoop
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        EventLoop.idle()
        if cond():
            return True
        time.sleep(0.02)
    return False


def ensure_online(app):
    if app.link != 'online':
        app.start_demo()
    assert pump(lambda: app.link == 'online' and len(app.state.log) >= 3)


def test_starts_on_connection_screen(app):
    assert app.sm.current == 'connection'


def test_invalid_ip_shows_error(app):
    scr = app.sm.get_screen('connection')
    scr.ids.ip_input.text = "300.1.1.1"
    scr.on_connect()
    assert app.worker is None
    assert "300.1.1.1" in scr.ids.status_lbl.text


def test_monitor_shows_live_data(app):
    ensure_online(app)
    ids = app.sm.get_screen('monitor').ids
    assert app.sm.current == 'monitor'
    assert "PCI" in ids.tower_lbl.text
    assert ids.primary_box.m_name == "SINR" and ids.primary_box.m_value != "-"
    assert ids.primary_box.m_peak.startswith("Пик:")
    assert "ДЕМО" in ids.status_lbl.text


def test_graph_param_reorders_cards(app):
    ensure_online(app)
    ids = app.sm.get_screen('monitor').ids
    app.set_graph_param('rsrp')
    assert [ids.primary_box.m_name, ids.secondary_box.m_name] == ["RSRP", "SINR"]
    app.set_graph_param('sinr')
    assert ids.primary_box.m_name == "SINR"


def test_band_lock_restore_and_antenna(app):
    ensure_online(app)
    modem = app.demo_modem
    app.go('tools')
    assert pump(lambda: "AUTO" in app._tools().lock_state)
    assert sorted(app.band_vars) == sorted(demo_mod.DEMO_SUPPORTED_BANDS)
    app.mark_current_bands()
    assert {b for b, cb in app.band_vars.items() if cb.active} == set(app.state.last.bands)
    for b, cb in app.band_vars.items():
        cb.active = b == 20
    app.apply_bands()
    assert pump(lambda: not app._busy)
    assert modem.lte_mask == 1 << 19 and modem.network_mode == '03'
    app.restore_bands()
    assert pump(lambda: not app._busy)
    assert modem.lte_mask == core.constants.LTE_ALL_MASK and modem.network_mode == '00'
    app.apply_antenna("Внешняя")
    assert pump(lambda: not app._busy)
    assert modem.antenna == 1


def test_back_key_navigation(app):
    from kivy.core.window import Window
    ensure_online(app)
    app.go('info')
    assert app._on_keyboard(Window, 27) is True and app.sm.current == 'monitor'
    assert app._on_keyboard(Window, 13) is False


def test_info_screen_live(app):
    ensure_online(app)
    app.go('info')
    ids = app.sm.get_screen('info').ids
    assert pump(lambda: "MegaFon" in ids.tower_block.text)
    assert "PCI" in ids.cells_block.text
    assert "B636" in ids.sim_block.text
    app.go('monitor')


def test_pause_stops_polling_and_resume_continues(app):
    ensure_online(app)
    app.on_pause()
    time.sleep(0.3)
    pump(lambda: False, 0.3)
    n = len(app.state.log)
    pump(lambda: False, 1.2)
    assert len(app.state.log) <= n + 1
    app.on_resume()
    assert pump(lambda: len(app.state.log) > n + 1)


def test_sun_theme(app):
    app.apply_theme('sun')
    assert app.c_bg == [1, 1, 1, 1]
    app.apply_theme('dark')
    assert app.c_bg[0] < 0.2


def test_reboot_reconnects(app):
    ensure_online(app)
    app.confirm_reboot()
    assert pump(lambda: app.link == 'reconnecting', 10)
    assert pump(lambda: app.link == 'online', 15)


def test_diagnostics_to_clipboard(app, monkeypatch):
    ensure_online(app)
    copied = {}
    from kivy.core import clipboard
    monkeypatch.setattr(clipboard.Clipboard, "copy", lambda text: copied.setdefault('t', text))
    app.copy_diagnostics()
    assert pump(lambda: not app._busy and 't' in copied)
    assert '"app_version"' in copied['t'] and "860000000000001" not in copied['t']


def test_english_texts(app):
    ensure_online(app)
    core.set_language("en")
    try:
        app.refresh_all_texts()
        scr = app.sm.get_screen('monitor')
        assert scr.lbl_menu == "☰ Menu"
        assert pump(lambda: "DEMO" in scr.ids.status_lbl.text)
    finally:
        core.set_language("ru")
        app.refresh_all_texts()


def test_beep_interval():
    import android_main
    assert android_main.beep_interval(None) == 1.5
    assert android_main.beep_interval(0) == pytest.approx(0.12)
    assert android_main.beep_interval(-50) == pytest.approx(1.42)
    assert android_main.hex_to_rgba('#ffffff') == [1.0, 1.0, 1.0, 1.0]
    assert android_main.hex_to_rgba('garbage') == [0.5, 0.5, 0.5, 1.0]


def test_double_back_disconnects(app):
    from kivy.core.window import Window
    ensure_online(app)
    app.go('monitor')
    assert app._on_keyboard(Window, 27) is True and app.link == 'online'
    assert app._on_keyboard(Window, 27) is True
    assert app.link == 'offline' and app.sm.current == 'connection'


def test_beeper_survives_reconnect(app):
    """Во время переподключения звук молчит, но не выключается навсегда."""
    ensure_online(app)
    beeps = []
    app.beeper.beep = beeps.append
    app.sound_on = True
    app.link = 'reconnecting'
    app._beep_tick(0)
    assert not beeps and app._beep_ev is not None
    app.link = 'online'
    app._stop_beeps()
    app._beep_tick(0)
    assert beeps and app._beep_ev is not None
    app.sound_on = False
    app._stop_beeps()


def test_error_line_cleared_after_recovery(app):
    ensure_online(app)
    ev = app.sm.get_screen('monitor').ids.event_lbl
    app._on_status('reconnecting', 1.0, ConnectionError("x"))
    assert ev.text
    app._on_status('connected', None, None)
    expected = app._event_text(app.state.events[-1]) if app.state.events else ""
    assert ev.text == expected


def test_new_session_clears_old_error_line(app):
    ensure_online(app)
    ev = app.sm.get_screen('monitor').ids.event_lbl
    ev.text = "Роутер не отвечает"               # осталось от прошлой сессии
    app._on_connected({})
    expected = app._event_text(app.state.events[-1]) if app.state.events else ""
    assert ev.text == expected


def test_band_labels_follow_theme(app):
    ensure_online(app)
    app.go('tools')
    app.build_band_checkboxes()
    grid = app._tools().ids.bands_grid
    app.apply_theme('sun')
    try:
        labels = [w for w in grid.children if hasattr(w, 'halign')]
        assert labels and all(list(lb.color) == list(app.c_text) for lb in labels)
    finally:
        app.apply_theme('dark')
        app.go('monitor')


def test_bold_font_is_bundled():
    import android_main
    assert os.path.exists(android_main._FONT_BOLD_PATH)
