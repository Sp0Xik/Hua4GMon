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
    assert "PCI" in ids.cells_block.text and "(25002)" in ids.cells_block.text
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


def test_sun_theme_status_colors_readable(app):
    """В «Солнце» цвета статусов читаются на светлой карточке (≥ 4,5:1);
    в тёмной теме остаются исходными."""
    import android_main
    for color in ('#00b894', '#2ecc71', '#fdcb6e', '#d63031', 'gray', 'orange', '#e68033'):
        r, g, b, _ = android_main.hex_to_rgba(color, sun=True)
        sun_hex = "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (r, g, b)))
        assert core.contrast_ratio(sun_hex, core.SUN_CARD_BG) >= 4.5, color
    assert android_main.hex_to_rgba('#fdcb6e') == [0xfd / 255, 0xcb / 255, 0x6e / 255, 1.0]


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


@pytest.mark.parametrize("start", [
    lambda a: a.discover(""),
    lambda a: a.whitelist_check(),
], ids=["discover", "whitelist"])
def test_repeated_tap_does_not_start_second_probe(app, monkeypatch, start):
    """Повторное нажатие, пока идёт поиск/проверка, не запускает вторую
    (результат первой мог бы прийти последним и затереть вторую)."""
    calls = []
    monkeypatch.setattr(app, "_run_bg", lambda work, done, fail: calls.append((done, fail)))
    start(app)
    start(app)
    assert len(calls) == 1
    _done, fail = calls[0]
    fail(OSError("network down"))          # проверка закончилась — можно снова
    start(app)
    assert len(calls) == 2
    calls[1][1](OSError("network down"))


def test_fullscreen_zoom_and_removed_arrow(app):
    from kivy.core.window import Window
    from kivy.uix.popup import Popup
    ensure_online(app)
    assert 'dir_lbl' not in app.sm.get_screen('monitor').ids
    app.open_fullscreen_graph()
    graph = app._fs_graph
    graph.zoom('time', 0.5)
    graph.zoom('level', 0.5)
    seconds, levels = graph.viewport.seconds, graph.viewport.levels
    n = len(app.state.log)
    assert pump(lambda: len(app.state.log) > n)
    assert graph.viewport.seconds == seconds and graph.viewport.levels == levels
    graph.auto()
    assert graph.viewport.seconds is None and graph.viewport.levels is None
    for child in list(Window.children):
        if isinstance(child, Popup):
            child.dismiss()
    assert pump(lambda: app._fs_graph is None)


@pytest.mark.parametrize('name', ['info', 'tools'])
def test_pages_open_at_top_without_poll_reset(app, name):
    ensure_online(app)
    app.go(name)
    screen = app.sm.get_screen(name)
    assert pump(lambda: not app.sm.transition.is_active)
    pump(lambda: False, 0.2)
    scroll = screen.ids.content_scroll
    assert scroll.scroll_y == 1
    scroll.scroll_y = 0.4
    n = len(app.state.log)
    assert pump(lambda: len(app.state.log) > n)
    assert abs(scroll.scroll_y - 0.4) < 0.01
    app.go('monitor')
    assert pump(lambda: not app.sm.transition.is_active)
    app.go(name)
    assert pump(lambda: not app.sm.transition.is_active and scroll.scroll_y == 1)
    app.go('monitor')


def test_new_session_has_no_demo_settings(app, monkeypatch):
    import android_main
    ensure_online(app)
    app.build_band_checkboxes()
    for cb in app.band_vars.values():
        cb.active = True
    app._tools().antenna_text = 'Внешняя'
    app._tools().net_msg = 'demo'
    app.disconnect()
    monkeypatch.setattr(android_main.SessionWorker, 'start', lambda self: None)
    session = core.RouterSession('192.168.8.1', '')
    app._start_session(session)
    assert not any(cb.active for cb in app.band_vars.values())
    assert app._tools().antenna_text == core.t('Не прочитано')
    assert app._tools().net_msg == ''
    assert app.state.last is None
    assert app.sm.get_screen('monitor').ids.primary_box.m_value == '-'
    app.disconnect()


def test_equal_metric_grid_and_centered_actions(app):
    ensure_online(app)
    ids = app.sm.get_screen('monitor').ids
    pump(lambda: False, 0.2)
    cards = [ids.primary_box, ids.secondary_box, ids.small1_box, ids.small2_box]
    assert ids.metrics_grid.cols == 2 and ids.metrics_grid.rows == 2
    assert all(card.size == cards[0].size for card in cards)
    assert all(card.value_size == cards[0].value_size for card in cards)
    app.go('tools')
    assert pump(lambda: not app.sm.transition.is_active)
    from kivy.factory import Factory
    for name in ('connection', 'tools'):
        actions = [w for w in app.sm.get_screen(name).walk()
                   if isinstance(w, Factory.ActionButton)]
        assert actions
        for button in actions:
            assert button.center_x == pytest.approx(button.parent.center_x)
            assert button.halign == 'center' and button.valign == 'middle'
    assert 'не гарантируются' not in app._tools().hint_router
    app.go('monitor')


def test_pinch_through_rotated_popup_and_auto(app):
    from kivy.core.window import Window
    from kivy.tests.common import UnitTestTouch
    from kivy.uix.button import Button
    from kivy.uix.popup import Popup
    ensure_online(app)
    app.worker.pause()
    app.open_fullscreen_graph()
    pump(lambda: False, 0.3)
    graph = app._fs_graph
    popup = next(w for w in Window.children if isinstance(w, Popup))
    graph.set_data([0, 5, 10], 'sinr', 10, False, [0, 50, 100])

    def point(x, y):
        return graph.to_window(graph.x + graph.width * x, graph.y + graph.height * y)

    first = UnitTestTouch(*point(0.4, 0.4))
    second = UnitTestTouch(*point(0.6, 0.6))
    first.touch_down()
    second.touch_down()
    assert len(graph._fingers) == 2
    original = graph.viewport.y_range()
    second.touch_move(*point(0.8, 0.8))
    assert graph.viewport.seconds < 100
    lo, hi = graph.viewport.y_range()
    assert hi - lo < original[1] - original[0]
    zoomed = graph.viewport.seconds
    second.touch_move(*point(0.6, 0.6))
    assert graph.viewport.seconds > zoomed
    first.touch_up()
    second.touch_up()
    assert not graph._fingers
    buttons = [w for w in popup.walk() if isinstance(w, Button)]
    assert {b.text for b in buttons} == {core.t('Авто'), core.t('← Назад')}
    auto = next(b for b in buttons if b.text == core.t('Авто'))
    tap = UnitTestTouch(*auto.to_window(*auto.center))
    tap.touch_down()
    tap.touch_up()
    assert graph.viewport.seconds is None and graph.viewport.levels is None
    first = UnitTestTouch(*point(0.5, 0.5))
    first.touch_down()
    popup.dismiss()
    assert pump(lambda: app._fs_graph is None)
    assert not graph._fingers and not first.grab_list
    first.touch_up()
    app.worker.resume()


def test_foreign_network_hidden_and_survey_copy(app, monkeypatch):
    from kivy.core.clipboard import Clipboard
    from kivy.core.window import Window
    from kivy.uix.popup import Popup
    ensure_online(app)
    app.worker.pause()
    pump(lambda: False, 0.15)
    app.open_survey()
    app._start_survey()
    bad = core.build_snapshot({'ServiceStatus': '1', 'ConnectionStatus': '902',
                               'rsrp': '-89dBm', 'sinr': '20dB', 'Numeric': '25035',
                               'earfcn': '1276', 'pci': '15'})
    peak = app.state.peaks['rsrp']
    app._on_snapshot(bad)
    monitor = app.sm.get_screen('monitor')
    assert 'Нет регистрации' in monitor.ids.status_lbl.text
    assert all(card.m_value == '-' for card in (monitor.ids.primary_box,
               monitor.ids.secondary_box, monitor.ids.small1_box, monitor.ids.small2_box))
    assert app.state.peaks['rsrp'] == peak
    assert 'Мотив' in app._survey_text.text
    copied = []
    monkeypatch.setattr(Clipboard, 'copy', copied.append)
    app._stop_survey()
    app._copy_survey()
    assert '25035' in copied[0]
    for widget in list(Window.children):
        if isinstance(widget, Popup):
            widget.dismiss()
    pump(lambda: False, 0.1)
    app.worker.resume()


@pytest.mark.parametrize('size', [(140, 100), (165, 140), (280, 120)])
@pytest.mark.parametrize('language', ['ru', 'en'])
def test_metric_captions_fit_small_cards(app, size, language):
    from kivy.factory import Factory
    core.set_language(language)
    card = Factory.MetricCard(size_hint=(None, None), size=size)
    card.m_name = 'RSRP'
    card.m_value = '-119.5'
    card.m_status = core.t('Нет данных')
    card.m_peak = core.t('Пик: {v} (Δ {d})').format(v='-109.5', d='-10.25')
    try:
        assert pump(lambda: all(label.text and label.texture_size[0] <= label.width + 1
                               and label.texture_size[1] <= label.height + 1
                               for label in card.children))
        assert card.ids.peak_label.text.replace('\n', ' ') == card.m_peak
        for label in card.children:
            assert label.x >= card.x and label.right <= card.right
            assert label.y >= card.y and label.top <= card.top
    finally:
        core.set_language('ru')


def test_network_summary_only_warns_on_problem(app):
    from dataclasses import replace

    from core.models import Snapshot
    snap = Snapshot(registration='registered', connection=901, data_enabled=True,
                    bands=(3,), band_label='B3', pci=114)
    assert 'В сети' not in app._tower_summary(snap)
    assert 'B3' in app._tower_summary(snap)
    assert 'нет мобильного' in app._tower_summary(replace(snap, connection=902))
    assert 'Нет регистрации' in app._tower_summary(replace(snap, registration='unregistered'))


def test_lte_nr_graphs_main_and_fullscreen(app):
    from kivy.core.window import Window
    from kivy.uix.popup import Popup
    ensure_online(app)
    app.worker.pause()
    pump(lambda: False, 0.15)
    try:
        app.state.reset()
        app.state.trend_param = 'sinr'
        for now in range(3):
            app.state.ingest(core.Snapshot(registration='registered', sinr=5 + now,
                                           nr_sinr=20 + now), float(now))
        app.refresh_graph()
        app.open_fullscreen_graph()
        pump(lambda: False, 0.15)
        for graph in (app.sm.get_screen('monitor').ids.signal_graph, app._fs_graph):
            assert graph.viewport.has_comparison
            assert len(graph.viewport.segments(True)) == 2
            labels = [key[0] for key in graph._tex]
            assert any(label.startswith('LTE: 7') for label in labels)
            assert any(label.startswith('NR: 22') for label in labels)
        app.state.ingest(core.Snapshot(registration='registered', sinr=8), 3.0)
        app.refresh_graph()
        assert app._fs_graph.viewport.latest_value(True) is None
        for theme in ('sun', 'dark'):
            app.apply_theme(theme)
            app.refresh_graph(stale=True)
        app.set_graph_param('nr_rsrq')
        assert app._fs_graph.viewport.param == 'nr_rsrq'
    finally:
        for widget in list(Window.children):
            if isinstance(widget, Popup):
                widget.dismiss()
        pump(lambda: False, 0.1)
        app.set_graph_param('sinr')
        app.worker.resume()
