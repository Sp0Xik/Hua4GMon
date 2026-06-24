"""
Hua4GMon — Android-версия (Kivy).

Использует тот же пакет ``core/``, что и десктопная версия (main.py):
вся чистая логика (разбор сигнала, бандов, PLMN, оценка качества,
переводы) — общая. Здесь только UI-слой на Kivy + сетевой опрос
роутера в фоновом потоке.

Архитектура (зеркало десктопной):
    * сеть/опрос — в фоновом threading.Thread (requests блокирующий,
      нельзя держать в Kivy Clock главного потока);
    * обновление виджетов — только через @mainthread (Kivy не
      потокобезопасен для UI из сторонних потоков);
    * остановка потока — через threading.Event, как в main.py.

Что есть в этой версии:
    * экран подключения (IP, пароль, язык);
    * экран мониторинга: крупные RSRP/SINR/RSRQ/RSSI с цветовой
      индикацией, стрелка тенденции (↑/→/↓) для наведения антенны,
      общая оценка качества, блок информации о вышке;
    * экран «Инструменты»: Band Lock, переключение антенн, тест
      «белых списков» (РФ) — те же операции, что в десктопной версии.

Сознательно НЕ перенесено:
    * CSV-экспорт — на Android нужен SAF/разрешения, и в полевой
      работе с телефона он малополезен.

Операции записи в роутер (Band Lock, антенна, reboot) и сетевые пробы
выполняются в фоновом потоке, результат показывается через Popup —
прямой аналог messagebox десктопной версии.

Сборка APK — через Buildozer (см. buildozer.spec).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from kivy.app import App
from kivy.clock import mainthread
from kivy.lang import Builder
from kivy.properties import ListProperty, StringProperty
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, ScreenManager

# Общая логика и переводы — тот же пакет, что у десктопа.
from core import (
    ANTENNA_MODES,
    BANDS,
    CONTROL_HOSTS_NEUTRAL,
    DIRECTION_LOOKBACK,
    LANGUAGES,
    LTEBAND_AUTO_ALL,
    NETBAND_AUTO_MASK,
    NETMODE_AUTO,
    NETMODE_LTE_ONLY,
    PLMN_MAP,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
    WHITELIST_HOSTS_RU,
    analyze_whitelist_results,
    calculate_overall_health,
    current_language,
    evaluate_signal,
    extract_number,
    format_band_label,
    is_valid_ip,
    parse_antenna_value,
    parse_cell_id,
    set_language,
    t,
    tcp_reachable,
)

__version__ = "1.2"
APP_NAME = "Hua4GMon"

DYNAMIC_PARAMS = ['rsrp', 'rssi', 'sinr', 'rsrq']


def _unit(param: str) -> str:
    return "dBm" if param in ('rsrp', 'rssi') else "dB"


# =========================================================
# KV-разметка
# =========================================================
# Цвета храним как rgba-кортежи. Большой шрифт на экране монитора —
# чтобы телефон можно было положить у антенны и видеть издалека.

KV = """
#:import dp kivy.metrics.dp

<RoundButton@Button>:
    background_normal: ''
    background_color: 0.04, 0.47, 0.84, 1
    color: 1, 1, 1, 1
    font_size: dp(18)
    size_hint_y: None
    height: dp(52)

ScreenManager:
    ConnectionScreen:
    MonitorScreen:
    ToolsScreen:

<ConnectionScreen>:
    name: 'connection'
    ip_input: ip_input
    pw_input: pw_input
    status_lbl: status_lbl
    BoxLayout:
        orientation: 'vertical'
        padding: dp(24)
        spacing: dp(16)
        canvas.before:
            Color:
                rgba: 0.07, 0.09, 0.12, 1
            Rectangle:
                pos: self.pos
                size: self.size

        Widget:
            size_hint_y: None
            height: dp(20)

        Label:
            text: 'Hua4GMon'
            font_size: dp(38)
            bold: True
            color: 1, 1, 1, 1
            size_hint_y: None
            height: dp(56)

        Label:
            id: subtitle_lbl
            text: root.subtitle
            font_size: dp(15)
            color: 0.7, 0.75, 0.8, 1
            size_hint_y: None
            height: dp(28)

        Widget:
            size_hint_y: None
            height: dp(20)

        Label:
            text: root.lbl_ip
            color: 0.85, 0.88, 0.9, 1
            font_size: dp(15)
            halign: 'left'
            size_hint_y: None
            height: dp(24)
            text_size: self.width, None

        TextInput:
            id: ip_input
            text: '192.168.8.1'
            multiline: False
            font_size: dp(18)
            size_hint_y: None
            height: dp(48)

        Label:
            text: root.lbl_pw
            color: 0.85, 0.88, 0.9, 1
            font_size: dp(15)
            halign: 'left'
            size_hint_y: None
            height: dp(24)
            text_size: self.width, None

        TextInput:
            id: pw_input
            password: True
            multiline: False
            font_size: dp(18)
            size_hint_y: None
            height: dp(48)

        RoundButton:
            text: root.lbl_connect
            on_release: root.on_connect()

        Label:
            id: status_lbl
            text: ''
            font_size: dp(15)
            color: 0.9, 0.5, 0.2, 1
            size_hint_y: None
            height: dp(28)

        BoxLayout:
            size_hint_y: None
            height: dp(40)
            spacing: dp(8)
            Label:
                text: root.lbl_lang
                color: 0.7, 0.75, 0.8, 1
                font_size: dp(14)
                size_hint_x: None
                width: dp(80)
            Spinner:
                id: lang_spinner
                text: root.lang_name
                values: root.lang_values
                font_size: dp(14)
                on_text: root.on_language(self.text)

        Widget:

<MetricBox@BoxLayout>:
    orientation: 'vertical'
    metric_name: ''
    metric_value: '-'
    metric_status: ''
    metric_color: 0.5, 0.5, 0.5, 1
    canvas.before:
        Color:
            rgba: 0.12, 0.14, 0.18, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(10)]
    padding: dp(8)
    spacing: dp(2)
    Label:
        text: root.metric_name
        font_size: dp(14)
        color: 0.6, 0.65, 0.7, 1
        size_hint_y: None
        height: dp(20)
    Label:
        text: root.metric_value
        font_size: dp(34)
        bold: True
        color: root.metric_color
    Label:
        text: root.metric_status
        font_size: dp(13)
        color: root.metric_color
        size_hint_y: None
        height: dp(18)

<MonitorScreen>:
    name: 'monitor'
    status_lbl: status_lbl
    health_lbl: health_lbl
    dir_lbl: dir_lbl
    dir_text_lbl: dir_text_lbl
    rsrp_box: rsrp_box
    rssi_box: rssi_box
    sinr_box: sinr_box
    rsrq_box: rsrq_box
    tower_lbl: tower_lbl
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.07, 0.09, 0.12, 1
            Rectangle:
                pos: self.pos
                size: self.size

        # Верхняя панель: статус + кнопка отключения
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            padding: dp(8), dp(4)
            spacing: dp(8)
            Label:
                id: status_lbl
                text: ''
                font_size: dp(15)
                bold: True
                color: 0.2, 0.8, 0.4, 1
            Button:
                text: root.lbl_tools
                size_hint_x: None
                width: dp(120)
                background_normal: ''
                background_color: 0.2, 0.35, 0.55, 1
                color: 1, 1, 1, 1
                on_release: root.on_tools()
            Button:
                text: root.lbl_disconnect
                size_hint_x: None
                width: dp(130)
                background_normal: ''
                background_color: 0.6, 0.2, 0.2, 1
                color: 1, 1, 1, 1
                on_release: root.on_disconnect()

        ScrollView:
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10)
                spacing: dp(10)

                # Общая оценка качества
                Label:
                    id: health_lbl
                    text: ''
                    font_size: dp(17)
                    bold: True
                    color: 0.5, 0.5, 0.5, 1
                    size_hint_y: None
                    height: dp(40)
                    text_size: self.width, None
                    halign: 'center'
                    valign: 'middle'

                # Стрелка тенденции — главный инструмент наведения
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(150)
                    canvas.before:
                        Color:
                            rgba: 0.1, 0.12, 0.16, 1
                        RoundedRectangle:
                            pos: self.pos
                            size: self.size
                            radius: [dp(12)]
                    Label:
                        id: dir_lbl
                        text: '—'
                        font_size: dp(80)
                        bold: True
                        color: 0.5, 0.5, 0.5, 1
                    Label:
                        id: dir_text_lbl
                        text: root.lbl_collecting
                        font_size: dp(14)
                        color: 0.6, 0.65, 0.7, 1
                        size_hint_y: None
                        height: dp(24)
                        text_size: self.width, None
                        halign: 'center'

                # 2x2 крупные метрики
                GridLayout:
                    cols: 2
                    spacing: dp(10)
                    size_hint_y: None
                    height: dp(220)
                    MetricBox:
                        id: rsrp_box
                        metric_name: 'RSRP'
                    MetricBox:
                        id: sinr_box
                        metric_name: 'SINR'
                    MetricBox:
                        id: rssi_box
                        metric_name: 'RSSI'
                    MetricBox:
                        id: rsrq_box
                        metric_name: 'RSRQ'

                # Информация о вышке
                Label:
                    id: tower_lbl
                    text: ''
                    font_size: dp(14)
                    color: 0.8, 0.83, 0.86, 1
                    size_hint_y: None
                    height: self.texture_size[1] + dp(10)
                    text_size: self.width, None
                    halign: 'left'
                    valign: 'top'

<SectionLabel@Label>:
    font_size: dp(17)
    bold: True
    color: 1, 1, 1, 1
    size_hint_y: None
    height: dp(34)
    text_size: self.width, None
    halign: 'left'
    valign: 'middle'

<HintLabel@Label>:
    font_size: dp(13)
    color: 0.65, 0.7, 0.75, 1
    size_hint_y: None
    height: self.texture_size[1] + dp(6)
    text_size: self.width, None
    halign: 'left'
    valign: 'top'

<ToolButton@Button>:
    background_normal: ''
    background_color: 0.04, 0.47, 0.84, 1
    color: 1, 1, 1, 1
    font_size: dp(16)
    size_hint_y: None
    height: dp(48)

<ToolsScreen>:
    name: 'tools'
    bands_grid: bands_grid
    antenna_spinner: antenna_spinner
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.07, 0.09, 0.12, 1
            Rectangle:
                pos: self.pos
                size: self.size

        # Верхняя панель: назад
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            padding: dp(8), dp(4)
            spacing: dp(8)
            Button:
                text: root.lbl_back
                size_hint_x: None
                width: dp(130)
                background_normal: ''
                background_color: 0.2, 0.35, 0.55, 1
                color: 1, 1, 1, 1
                on_release: root.on_back()
            Label:
                text: root.lbl_title
                font_size: dp(17)
                bold: True
                color: 1, 1, 1, 1

        ScrollView:
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(12)
                spacing: dp(8)

                # --- Band Lock ---
                SectionLabel:
                    text: root.lbl_bandlock
                HintLabel:
                    text: root.hint_bandlock
                GridLayout:
                    id: bands_grid
                    cols: 2
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(4)
                ToolButton:
                    text: root.lbl_apply_bands
                    on_release: root.on_apply_bands()
                ToolButton:
                    text: root.lbl_reset_auto
                    background_color: 0.3, 0.4, 0.5, 1
                    on_release: root.on_reset_bands()

                Widget:
                    size_hint_y: None
                    height: dp(8)

                # --- Антенна ---
                SectionLabel:
                    text: root.lbl_antenna
                Spinner:
                    id: antenna_spinner
                    text: 'Авто'
                    values: root.antenna_values
                    font_size: dp(16)
                    size_hint_y: None
                    height: dp(48)
                ToolButton:
                    text: root.lbl_apply_antenna
                    on_release: root.on_apply_antenna()

                Widget:
                    size_hint_y: None
                    height: dp(8)

                # --- Перезагрузка ---
                SectionLabel:
                    text: root.lbl_reboot_section
                ToolButton:
                    text: root.lbl_reboot
                    background_color: 0.6, 0.3, 0.15, 1
                    on_release: root.on_reboot()

                Widget:
                    size_hint_y: None
                    height: dp(8)

                # --- Белые списки (РФ) ---
                SectionLabel:
                    text: root.lbl_whitelist
                HintLabel:
                    text: root.hint_whitelist
                ToolButton:
                    text: root.lbl_wl_check
                    on_release: root.on_whitelist_check()
                Label:
                    text: root.wl_verdict
                    font_size: dp(16)
                    bold: True
                    color: root.wl_color
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                    halign: 'left'
                    valign: 'top'
                Label:
                    text: root.wl_detail
                    font_size: dp(13)
                    color: 0.8, 0.83, 0.86, 1
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                    halign: 'left'
                    valign: 'top'
"""


# =========================================================
# ЭКРАНЫ
# =========================================================

class ConnectionScreen(Screen):
    subtitle = StringProperty("")
    lbl_ip = StringProperty("")
    lbl_pw = StringProperty("")
    lbl_connect = StringProperty("")
    lbl_lang = StringProperty("")
    lang_name = StringProperty("")
    lang_values = ListProperty([])

    def on_pre_enter(self, *args):
        self.refresh_texts()

    def refresh_texts(self) -> None:
        self.subtitle = t("Портативный монитор LTE Huawei")
        self.lbl_ip = t("IP адрес:")
        self.lbl_pw = t("Пароль:")
        self.lbl_connect = t("🚀 Подключиться")
        self.lbl_lang = t("Язык:")
        self.lang_values = list(LANGUAGES.values())
        self.lang_name = LANGUAGES.get(current_language(), "Русский")

    def on_language(self, name: str) -> None:
        code = {v: k for k, v in LANGUAGES.items()}.get(name)
        if code and code != current_language():
            set_language(code)
            self.refresh_texts()

    def on_connect(self) -> None:
        app = App.get_running_app()
        ip = self.ip_input.text.strip()
        if not is_valid_ip(ip):
            self.status_lbl.text = t("Неверный IP-адрес: {ip}\n"
                                     "Пример: 192.168.8.1").format(
                ip=ip).replace("\n", " ")
            return
        self.status_lbl.text = t("Подключение...")
        app.connect(ip, self.pw_input.text)


class MonitorScreen(Screen):
    lbl_disconnect = StringProperty("")
    lbl_collecting = StringProperty("")
    lbl_tools = StringProperty("")

    def on_pre_enter(self, *args):
        self.lbl_disconnect = t("⏹ Отключиться")
        self.lbl_collecting = t("Накапливаю данные...")
        self.lbl_tools = t("🎛️ Сеть")

    def on_disconnect(self) -> None:
        App.get_running_app().disconnect()

    def on_tools(self) -> None:
        self.manager.current = 'tools'


class ToolsScreen(Screen):
    lbl_back = StringProperty("")
    lbl_title = StringProperty("")
    lbl_bandlock = StringProperty("")
    hint_bandlock = StringProperty("")
    lbl_apply_bands = StringProperty("")
    lbl_reset_auto = StringProperty("")
    lbl_antenna = StringProperty("")
    lbl_apply_antenna = StringProperty("")
    lbl_reboot_section = StringProperty("")
    lbl_reboot = StringProperty("")
    lbl_whitelist = StringProperty("")
    hint_whitelist = StringProperty("")
    lbl_wl_check = StringProperty("")
    wl_verdict = StringProperty("")
    wl_detail = StringProperty("")
    wl_color = ListProperty([0.5, 0.5, 0.5, 1])
    antenna_values = ListProperty([])

    _bands_built = False

    def on_pre_enter(self, *args):
        self.lbl_back = t("← Назад")
        self.lbl_title = t("🎛️ Сеть")
        self.lbl_bandlock = t("Фиксация частот (Band Lock)")
        self.hint_bandlock = t(
            "ВНИМАНИЕ: фиксация диапазона может уменьшить покрытие. "
            "Применяйте, чтобы привязаться к лучшей вышке — сначала "
            "определите рабочий band на экране монитора.")
        self.lbl_apply_bands = t("Применить Band Lock")
        self.lbl_reset_auto = t("Сбросить в AUTO")
        self.lbl_antenna = t("Переключение антенн")
        self.lbl_apply_antenna = t("Применить")
        self.lbl_reboot_section = t("Управление роутером")
        self.lbl_reboot = t("🔄 Перезагрузить роутер")
        self.lbl_whitelist = t("🛡 Белые списки (РФ)")
        self.hint_whitelist = t(
            "⚠ Телефон должен быть подключён к Wi-Fi именно этого "
            "роутера — иначе тест измерит чужой канал. Применимо только "
            "для РФ. Проверка ничего не меняет в роутере.")
        self.lbl_wl_check = t("🔍 Проверить сейчас")
        # Значения антенны — ключи ANTENNA_MODES (русские), не переводим
        self.antenna_values = list(ANTENNA_MODES.keys())
        self._build_band_checkboxes()

    def _build_band_checkboxes(self) -> None:
        """Строит чекбоксы бандов один раз (ленивая инициализация)."""
        if self._bands_built:
            return
        from kivy.uix.checkbox import CheckBox
        from kivy.uix.label import Label as KLabel

        self.band_vars: Dict[str, CheckBox] = {}
        for band_name in BANDS:
            row = self.bands_grid
            cb = CheckBox(size_hint_x=None, width=40)
            lbl = KLabel(text=band_name, color=(0.85, 0.88, 0.9, 1),
                         halign='left', valign='middle')
            lbl.bind(size=lambda inst, val: setattr(
                inst, 'text_size', val))
            row.add_widget(cb)
            row.add_widget(lbl)
            self.band_vars[band_name] = cb
        self._bands_built = True

    def on_back(self) -> None:
        self.manager.current = 'monitor'

    def on_apply_bands(self) -> None:
        selected = [n for n, cb in self.band_vars.items() if cb.active]
        App.get_running_app().apply_bands(selected)

    def on_reset_bands(self) -> None:
        App.get_running_app().reset_bands()

    def on_apply_antenna(self) -> None:
        App.get_running_app().apply_antenna(self.antenna_spinner.text)

    def on_reboot(self) -> None:
        App.get_running_app().confirm_reboot()

    def on_whitelist_check(self) -> None:
        self.wl_verdict = t("Проверка…")
        self.wl_color = (0.9, 0.5, 0.2, 1)
        self.wl_detail = t("Подождите 1–3 секунды.")
        App.get_running_app().whitelist_check()

    def show_whitelist_result(self, title: str, detail: str, color) -> None:
        self.wl_verdict = t(title)
        self.wl_detail = detail
        self.wl_color = color


# =========================================================
# ПРИЛОЖЕНИЕ
# =========================================================

class Hua4GMonApp(App):
    def build(self):
        self.title = f"{APP_NAME} v{__version__}"
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.client: Optional[Client] = None
        self._cached_ip = ""
        self._cached_pw = ""
        self.connected = False
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.dir_history: list[float] = []
        self.peak_values: Dict[str, Any] = {p: '-' for p in DYNAMIC_PARAMS}

        self.sm: ScreenManager = Builder.load_string(KV)
        return self.sm

    # ---- Подключение ----

    def connect(self, ip: str, password: str) -> None:
        self._cached_ip = ip
        self._cached_pw = password
        self._stop_event.clear()
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.dir_history.clear()
        self.peak_values = {p: '-' for p in DYNAMIC_PARAMS}
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self.auto_reconnect = False
        self.connected = False
        self._stop_event.set()
        # join только если вызвано НЕ из самого monitor-потока
        if (self._thread and self._thread.is_alive()
                and threading.current_thread() is not self._thread):
            self._thread.join(timeout=3.0)
        self._thread = None
        if self.client is not None:
            try:
                self.client.user.logout()
            except Exception:
                pass
            self.client = None
        # Смена экрана — только в главном потоке (disconnect может быть
        # вызван из фонового потока, напр. после reboot).
        self._goto_connection()

    @mainthread
    def _goto_connection(self) -> None:
        self.sm.current = 'connection'

    # ---- Фоновый поток опроса ----

    def _worker(self) -> None:
        try:
            self.client = Client(Connection(
                f"http://{self._cached_ip}", username='admin',
                password=self._cached_pw, timeout=4))
            self.client.device.information()
            self.connected = True
            self._goto_monitor()
        except Exception as e:
            self._show_conn_error(str(e))
            return

        while not self._stop_event.is_set():
            client = self.client
            if client is None:
                break
            try:
                sig = client.device.signal()
                plmn = client.net.current_plmn()
                data: Dict[str, Any] = {**(sig or {}), **(plmn or {})}
                data['plmn'] = (plmn or {}).get('Numeric',
                                                data.get('plmn', ''))
                enodeb, sector = parse_cell_id(data.get('cell_id'))
                if enodeb is not None:
                    data['enodeb'] = enodeb
                    data['sector'] = sector
                self._update_ui(data)
                self.reconnect_delay = RECONNECT_DELAY_INITIAL
            except Exception:
                if self.auto_reconnect and not self._stop_event.is_set():
                    self._try_reconnect()
                else:
                    break
            if self._stop_event.wait(1.0):
                break

    def _try_reconnect(self) -> None:
        delay = min(self.reconnect_delay, RECONNECT_DELAY_MAX)
        self._set_status(t("Переподключение через {d:.0f}с...").format(
            d=delay), (0.9, 0.5, 0.2, 1))
        if self._stop_event.wait(delay):
            return
        try:
            self.client = Client(Connection(
                f"http://{self._cached_ip}", username='admin',
                password=self._cached_pw, timeout=4))
            self.client.device.information()
            self.reconnect_delay = RECONNECT_DELAY_INITIAL
            self._set_status(t("Подключено"), (0.2, 0.8, 0.4, 1))
        except Exception:
            self.reconnect_delay = min(self.reconnect_delay * 2,
                                       RECONNECT_DELAY_MAX)

    # ---- Обновление UI (главный поток Kivy) ----

    @mainthread
    def _goto_monitor(self) -> None:
        self.sm.current = 'monitor'
        self._set_status(t("Подключено"), (0.2, 0.8, 0.4, 1))

    @mainthread
    def _show_conn_error(self, err: str) -> None:
        scr = self.sm.get_screen('connection')
        snippet = err if len(err) < 80 else err[:80] + "..."
        scr.status_lbl.text = t("Связь с роутером не удалась:\n\n{err}").format(
            err=snippet).replace("\n", " ")

    @mainthread
    def _set_status(self, text: str, color) -> None:
        scr = self.sm.get_screen('monitor')
        scr.status_lbl.text = text
        scr.status_lbl.color = color

    @mainthread
    def _update_ui(self, data: Dict[str, Any]) -> None:
        scr = self.sm.get_screen('monitor')
        scr.status_lbl.text = t("Подключено")
        scr.status_lbl.color = (0.2, 0.8, 0.4, 1)

        current_vals: Dict[str, Optional[float]] = {
            p: extract_number(data.get(p)) for p in DYNAMIC_PARAMS}

        box_by_param = {
            'rsrp': scr.rsrp_box, 'rssi': scr.rssi_box,
            'sinr': scr.sinr_box, 'rsrq': scr.rsrq_box}
        for p in DYNAMIC_PARAMS:
            val = current_vals[p]
            box = box_by_param[p]
            if val is None:
                box.metric_value = '-'
                box.metric_status = t("Нет данных")
                box.metric_color = (0.5, 0.5, 0.5, 1)
                continue
            status_text, hexcolor, _ = evaluate_signal(p, val)
            box.metric_value = f"{val:g}"
            box.metric_status = t(status_text)
            box.metric_color = _hex_to_rgba(hexcolor)

        # Стрелка тенденции (по RSRP)
        rsrp = current_vals.get('rsrp')
        if rsrp is not None:
            self.dir_history.append(rsrp)
            if len(self.dir_history) > DIRECTION_LOOKBACK * 2:
                self.dir_history.pop(0)
            arrow, hexcolor, text = self._direction()
            scr.dir_lbl.text = arrow
            scr.dir_lbl.color = _hex_to_rgba(hexcolor)
            scr.dir_text_lbl.text = text

        # Общая оценка
        score, summary, hexcolor = calculate_overall_health(
            rsrp, current_vals.get('sinr'))
        scr.health_lbl.text = t(summary).format(pct=score)
        scr.health_lbl.color = _hex_to_rgba(hexcolor)

        # Вышка
        plmn = str(data.get('plmn', '-'))
        op = ''
        if plmn != '-' and len(plmn) >= 5:
            op = PLMN_MAP.get(plmn, t("Неизвестный оператор"))
        band = format_band_label(data.get('band'),
                                 data.get('earfcn', data.get('Earfcn', '-')))
        lines = [
            f"{t('Оператор (PLMN)')}: {plmn} {('(' + op + ')') if op else ''}",
            f"{t('Рабочий Band (LTE)')}: {band}",
            f"{t('eNodeB (Вышка)')}: {data.get('enodeb', '-')}",
            f"{t('Cell (Локальный сектор)')}: {data.get('sector', '-')}",
            f"{t('Сектор антенны (PCI)')}: {data.get('pci', '-')}",
        ]
        scr.tower_lbl.text = "\n".join(lines)

    def _direction(self):
        """Стрелка тенденции RSRP — та же логика, что в десктопе."""
        if len(self.dir_history) < DIRECTION_LOOKBACK * 2:
            return "—", "#888888", t("Накапливаю данные...")
        recent = self.dir_history[-DIRECTION_LOOKBACK:]
        older = self.dir_history[-DIRECTION_LOOKBACK * 2:-DIRECTION_LOOKBACK]
        delta = (sum(recent) / len(recent)) - (sum(older) / len(older))
        if delta >= 1.0:
            return ("↑", "#00b894",
                    t("Сигнал улучшается — продолжайте в том же направлении"))
        if delta <= -1.0:
            return ("↓", "#d63031",
                    t("Сигнал ухудшается — поверните обратно"))
        return ("→", "#fdcb6e",
                t("Сигнал стабилен — зафиксируйте антенну"))

    def on_stop(self):
        self._stop_event.set()

    # =====================================================
    # ИНСТРУМЕНТЫ: Band Lock / антенна / reboot / белые списки
    # =====================================================
    # Все операции записи в роутер идут в фоновом потоке (как в десктопе),
    # результат показывается через Popup в главном потоке.

    def _run_bg(self, fn) -> None:
        """Запускает блокирующую операцию с роутером в фоновом потоке."""
        threading.Thread(target=fn, daemon=True).start()

    def apply_bands(self, selected_names: list) -> None:
        if self.client is None:
            self._popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        mask = sum(BANDS[n] for n in selected_names if n in BANDS)
        if mask == 0:
            self._popup(t("Внимание"), t("Выберите хотя бы один диапазон!"))
            return
        hex_mask = format(mask, 'X')
        client = self.client

        def task():
            try:
                client.net.set_net_mode(hex_mask, NETBAND_AUTO_MASK,
                                        NETMODE_LTE_ONLY)
                self._popup(t("Успех"),
                            t("Band Lock применён (mask: {mask}).").format(
                                mask=hex_mask))
            except Exception as e:
                self._popup(t("Ошибка"),
                            t("Роутер отклонил команду:\n{err}").format(
                                err=str(e)))
        self._run_bg(task)

    def reset_bands(self) -> None:
        if self.client is None:
            self._popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        client = self.client

        def task():
            try:
                client.net.set_net_mode(LTEBAND_AUTO_ALL,
                                        NETBAND_AUTO_MASK, NETMODE_AUTO)
                self._popup(t("Успех"), t("Сеть сброшена в AUTO."))
            except Exception as e:
                self._popup(t("Ошибка"), str(e))
        self._run_bg(task)

    def apply_antenna(self, label: str) -> None:
        if self.client is None:
            self._popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        ant_val = parse_antenna_value(label)
        if ant_val is None:
            self._popup(t("Ошибка"), t("Неизвестный режим антенны."))
            return
        client = self.client

        def task():
            try:
                try:
                    from huawei_lte_api.enums.device import AntennaTypeEnum
                    client.device.set_antenna_settings(
                        AntennaTypeEnum(ant_val))
                except ImportError:
                    if hasattr(client.device, 'set_antenna_settings'):
                        client.device.set_antenna_settings(ant_val)
                    elif hasattr(client.device, 'set_antenna_type'):
                        client.device.set_antenna_type(ant_val)
                    else:
                        raise RuntimeError(
                            "API для управления антенной не найдено "
                            "(модель роутера может не поддерживать)."
                        ) from None
                self._popup(t("Успех"),
                            t("Тип антенны изменён: {mode}").format(
                                mode=label))
            except Exception as e:
                self._popup(t("Ошибка"), str(e))
        self._run_bg(task)

    def confirm_reboot(self) -> None:
        if self.client is None:
            self._popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        # Подтверждение перед перезагрузкой
        from kivy.uix.boxlayout import BoxLayout as BL
        from kivy.uix.button import Button as Btn
        from kivy.uix.label import Label as KLabel

        content = BL(orientation='vertical', spacing=10, padding=10)
        content.add_widget(KLabel(
            text=t("Перезагрузить роутер?\n\nСоединение с интернетом "
                   "прервётся на 1–2 минуты. После загрузки "
                   "переподключитесь вручную."),
            halign='center'))
        btns = BL(size_hint_y=None, height=48, spacing=10)
        popup = Popup(title=t("Подтверждение"), content=content,
                      size_hint=(0.9, 0.5))
        yes = Btn(text=t("🔄 Перезагрузить роутер"),
                  background_color=(0.6, 0.3, 0.15, 1))
        no = Btn(text=t("← Назад"))
        yes.bind(on_release=lambda *a: (popup.dismiss(), self._do_reboot()))
        no.bind(on_release=lambda *a: popup.dismiss())
        btns.add_widget(no)
        btns.add_widget(yes)
        content.add_widget(btns)
        popup.open()

    def _do_reboot(self) -> None:
        client = self.client
        if client is None:
            return

        def task():
            try:
                client.device.reboot()
                self.disconnect()
                self._popup(t("Перезагрузка"),
                            t("Команда отправлена. Роутер вернётся через "
                              "1–2 минуты."))
            except Exception as e:
                self._popup(t("Ошибка"),
                            t("Не удалось перезагрузить:\n{err}").format(
                                err=str(e)))
        self._run_bg(task)

    def whitelist_check(self) -> None:
        """Тест белых списков (РФ) — TCP-пробы в фоновом потоке."""
        def task():
            white_results = []
            for host, port in WHITELIST_HOSTS_RU:
                ok, _ = tcp_reachable(host, port)
                white_results.append((host, ok))
            neutral_results = []
            for host, port in CONTROL_HOSTS_NEUTRAL:
                ok, _ = tcp_reachable(host, port)
                neutral_results.append((host, ok))
            title, detail, hexcolor = analyze_whitelist_results(
                white_results, neutral_results)
            self._deliver_whitelist(title, detail, _hex_to_rgba(hexcolor))
        self._run_bg(task)

    @mainthread
    def _deliver_whitelist(self, title, detail, color) -> None:
        scr = self.sm.get_screen('tools')
        scr.show_whitelist_result(title, detail, color)

    @mainthread
    def _popup(self, title: str, message: str) -> None:
        from kivy.uix.label import Label as KLabel
        lbl = KLabel(text=message, halign='center', valign='middle')
        lbl.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
        Popup(title=title, content=lbl, size_hint=(0.9, 0.5)).open()


def _hex_to_rgba(hexcolor: str):
    """'#00b894' или 'gray' → (r, g, b, 1) для Kivy."""
    named = {
        'gray': (0.5, 0.5, 0.5, 1),
        'green': (0.2, 0.8, 0.4, 1),
        'orange': (0.9, 0.5, 0.2, 1),
        'red': (0.85, 0.2, 0.2, 1),
    }
    if hexcolor in named:
        return named[hexcolor]
    h = hexcolor.lstrip('#')
    if len(h) != 6:
        return (0.5, 0.5, 0.5, 1)
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
        return (r, g, b, 1)
    except ValueError:
        return (0.5, 0.5, 0.5, 1)


def main() -> None:
    Hua4GMonApp().run()


if __name__ == "__main__":
    main()
