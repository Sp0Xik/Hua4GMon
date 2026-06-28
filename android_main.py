"""
Hua4GMon — Android-версия (Kivy).

Использует тот же пакет ``core/``, что и десктоп (main.py): вся чистая
логика (разбор сигнала, бандов, PLMN, оценка качества, переводы) —
общая. Здесь только UI-слой на Kivy + сетевой опрос роутера в фоновом
потоке.

Экраны:
    * Подключение — IP/пароль/язык, подсказка по ошибкам, тестовый режим;
    * Монитор — крупные RSRP/SINR/RSRQ/RSSI, стрелка тенденции, общая
      оценка качества, джиттер, краткая сводка по вышке;
    * Информация — полная вышка (band/EARFCN/CA/ширина/PCI/eNodeB/Cell),
      SIM/устройство (IMEI/IMSI/ICCID/номер/серийный/модель/прошивка),
      состояние (трафик/скорости/температура/время сессии/мин-макс);
    * Сеть — Band Lock, переключение антенн, перезагрузка, тест «белых
      списков» (РФ).

Сознательно НЕ перенесено с десктопа: крышный режим, аудио-помощник,
CSV-экспорт.

Сетевые операции — в фоновом потоке; обновление UI только через
@mainthread (Kivy не потокобезопасен). Диалоги — через Popup.

Сборка APK — через Buildozer (см. buildozer.spec).
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import os
import sys
import threading
from typing import Any, Dict, Optional

# --- Android crypto-совместимость (ДО импорта huawei_lte_api) ---
# huawei-lte-api требует pycryptodomex (неймспейс Cryptodome), но у него
# нет рецепта python-for-android, и его нативные .so не грузятся на
# Android. Зато pycryptodome (неймспейс Crypto) рецепт имеет. Код пакетов
# идентичен — перенаправляем Cryptodome.* -> Crypto.*. На десктопе, где
# настоящий Cryptodome есть, алиас не включается.


class _CryptodomeAliasFinder(importlib.abc.MetaPathFinder,
                             importlib.abc.Loader):
    PREFIX = 'Cryptodome'

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.PREFIX or fullname.startswith(self.PREFIX + '.'):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        real_name = 'Crypto' + spec.name[len(self.PREFIX):]
        module = importlib.import_module(real_name)
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module):
        pass


try:
    import Cryptodome  # noqa: F401  (есть на десктопе — pycryptodomex)
except ImportError:
    sys.meta_path.insert(0, _CryptodomeAliasFinder())

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from kivy.app import App
from kivy.clock import mainthread
from kivy.core.text import LabelBase
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
    JITTER_WINDOW,
    LANGUAGES,
    LTEBAND_AUTO_ALL,
    NETBAND_AUTO_MASK,
    NETMODE_AUTO,
    NETMODE_LTE_ONLY,
    PARAM_RANGES,
    PLMN_MAP,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
    WHITELIST_HOSTS_RU,
    analyze_whitelist_results,
    current_language,
    evaluate_signal,
    extract_number,
    first_present,
    format_band_label,
    format_bytes_mb,
    format_rate_mbps,
    is_valid_ip,
    mcs_to_modulation,
    parse_antenna_value,
    parse_cell_id,
    set_language,
    t,
    tcp_reachable,
)

__version__ = "1.2"
APP_NAME = "Hua4GMon"

DYNAMIC_PARAMS = ['rsrp', 'rssi', 'sinr', 'rsrq']

# Шрифт с поддержкой стрелок (↑→↓), ⚠, тире и т.п. Встроенный Roboto в
# Kivy их не содержит — на Android они рисуются «квадратом с крестиком».
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'assets', 'DejaVuSans.ttf')


def _unit(param: str) -> str:
    return "dBm" if param in ('rsrp', 'rssi') else "dB"


def _first_present(data, keys):
    return first_present(data, keys)


def _mcs_label(mcs) -> str:
    """'MCS N (~QAM)' — сырое значение MCS плюс ориентировочный тип."""
    mod = mcs_to_modulation(mcs)
    return f"MCS {mcs}" + (f"  (~{mod})" if mod else "")


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
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0, 1)
    except ValueError:
        return (0.5, 0.5, 0.5, 1)


# =========================================================
# Виджет графика на Kivy canvas (аналог CanvasGraph из десктопа)
# =========================================================

from kivy.uix.widget import Widget  # noqa: E402


class SignalGraph(Widget):
    """Лёгкий линейный график на canvas — без сторонних зависимостей.

    Рисует историю значений выбранного параметра с сеткой и подписью
    последнего значения. Используется и на мониторе, и в полноэкранном
    Popup.
    """
    def __init__(self, **kw):
        super().__init__(**kw)
        self._values: list = []
        self._y_min = -120.0
        self._y_max = -50.0
        self._title = "RSRP"
        self._unit = "dBm"
        self.bind(pos=lambda *a: self._redraw(),
                  size=lambda *a: self._redraw())

    def set_data(self, values, y_min, y_max, title, unit) -> None:
        self._values = list(values)
        self._y_min, self._y_max = float(y_min), float(y_max)
        self._title, self._unit = title, unit
        self._redraw()

    def _redraw(self) -> None:
        from kivy.graphics import Color, Line, Rectangle
        self.canvas.clear()
        w, h = self.width, self.height
        if w < 40 or h < 40:
            return
        pl, pr, pt, pb = 8, 8, 8, 8
        plot_w, plot_h = w - pl - pr, h - pt - pb
        x0, y0 = self.x + pl, self.y + pb
        with self.canvas:
            # фон
            Color(0.1, 0.12, 0.16, 1)
            Rectangle(pos=(self.x, self.y), size=(w, h))
            # сетка (4 линии)
            Color(0.2, 0.23, 0.27, 1)
            for i in range(5):
                gy = y0 + plot_h * i / 4
                Line(points=[x0, gy, x0 + plot_w, gy], width=1)
            if len(self._values) < 2:
                return
            rng = max(self._y_max - self._y_min, 1e-9)
            span = max(len(self._values) - 1, 1)
            pts = []
            for i, v in enumerate(self._values):
                px = x0 + plot_w * i / span
                v_cl = max(self._y_min, min(self._y_max, v))
                py = y0 + plot_h * (v_cl - self._y_min) / rng
                pts.extend([px, py])
            Color(0.0, 0.72, 0.58, 1)
            Line(points=pts, width=max(1.5, h / 110.0))


def _graph_axes(param: str):
    y_min, y_max = PARAM_RANGES.get(param, (-120, 0))
    return y_min, y_max, param.upper(), _unit(param)


# =========================================================
# KV-разметка
# =========================================================

KV = """
#:import dp kivy.metrics.dp

<RoundButton@Button>:
    background_normal: ''
    background_color: 0.04, 0.47, 0.84, 1
    color: 1, 1, 1, 1
    font_size: dp(18)
    size_hint_y: None
    height: dp(52)

<TopButton@Button>:
    background_normal: ''
    color: 1, 1, 1, 1
    font_size: dp(15)
    size_hint_x: None

<SectionCard@BoxLayout>:
    orientation: 'vertical'
    size_hint_y: None
    height: self.minimum_height
    padding: dp(12)
    spacing: dp(8)
    canvas.before:
        Color:
            rgba: 0.12, 0.14, 0.18, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<SectionTitle@Label>:
    font_size: dp(19)
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

<InfoLabel@Label>:
    font_size: dp(16)
    color: 0.82, 0.85, 0.88, 1
    line_height: 1.35
    size_hint_y: None
    height: self.texture_size[1] + dp(8)
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

ScreenManager:
    ConnectionScreen:
    MonitorScreen:
    InfoScreen:
    ToolsScreen:

<ConnectionScreen>:
    name: 'connection'
    ip_input: ip_input
    pw_input: pw_input
    status_lbl: status_lbl
    ScrollView:
        bar_width: dp(8)
        bar_color: 0.45, 0.5, 0.55, 1
        bar_inactive_color: 0.25, 0.28, 0.32, 1
        scroll_type: ['bars', 'content']
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: dp(24)
            spacing: dp(14)
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
                text: root.subtitle
                font_size: dp(15)
                color: 0.7, 0.75, 0.8, 1
                size_hint_y: None
                height: dp(28)
            Widget:
                size_hint_y: None
                height: dp(10)

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

            # Ошибка подключения: многострочная, с переносом — иначе
            # длинный текст уезжал за край экрана.
            Label:
                id: status_lbl
                text: ''
                font_size: dp(14)
                color: 0.9, 0.5, 0.2, 1
                size_hint_y: None
                height: self.texture_size[1]
                text_size: self.width, None
                halign: 'center'
                valign: 'top'

            BoxLayout:
                size_hint_y: None
                height: dp(44)
                spacing: dp(8)
                Button:
                    text: root.lbl_help
                    background_normal: ''
                    background_color: 0.2, 0.35, 0.55, 1
                    color: 1, 1, 1, 1
                    font_size: dp(15)
                    on_release: root.on_help()

            BoxLayout:
                size_hint_y: None
                height: dp(44)
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
                size_hint_y: None
                height: dp(16)
            Button:
                text: root.lbl_demo
                size_hint_y: None
                height: dp(40)
                font_size: dp(13)
                background_normal: ''
                background_color: 0.18, 0.2, 0.24, 1
                color: 0.6, 0.65, 0.7, 1
                on_release: root.on_demo()

<MetricBox@BoxLayout>:
    orientation: 'vertical'
    metric_name: ''
    metric_value: '-'
    metric_status: ''
    metric_peak: ''
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
    Label:
        text: root.metric_peak
        font_size: dp(14)
        color: 0.68, 0.73, 0.78, 1
        size_hint_y: None
        height: dp(20)

<MonitorScreen>:
    name: 'monitor'
    status_lbl: status_lbl
    dir_lbl: dir_lbl
    dir_text_lbl: dir_text_lbl
    jitter_lbl: jitter_lbl
    rsrp_box: rsrp_box
    rssi_box: rssi_box
    sinr_box: sinr_box
    rsrq_box: rsrq_box
    signal_graph: signal_graph
    graph_param: graph_param
    BoxLayout:
        orientation: 'vertical'
        padding: dp(8)
        spacing: dp(8)
        canvas.before:
            Color:
                rgba: 0.07, 0.09, 0.12, 1
            Rectangle:
                pos: self.pos
                size: self.size

        # Топбар — кнопки равной ширины (статус вынесен ниже отдельно)
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(6)
            TopButton:
                text: root.lbl_info
                size_hint_x: 1
                background_color: 0.2, 0.45, 0.4, 1
                on_release: root.on_info()
            TopButton:
                text: root.lbl_tools
                size_hint_x: 1
                background_color: 0.2, 0.35, 0.55, 1
                on_release: root.on_tools()
            TopButton:
                text: root.lbl_disconnect
                size_hint_x: 1
                background_color: 0.6, 0.2, 0.2, 1
                on_release: root.on_disconnect()

        # Статус подключения — отдельная строка, помещается на любом экране
        Label:
            id: status_lbl
            text: ''
            font_size: dp(14)
            bold: True
            color: 0.2, 0.8, 0.4, 1
            size_hint_y: None
            height: dp(24)
            text_size: self.width, None
            halign: 'center'
            valign: 'middle'

        # Тенденция (стрелка) — компактнее: это подсказка, не главное
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: 0.16
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
                font_size: dp(46)
                bold: True
                color: 0.5, 0.5, 0.5, 1
            Label:
                id: dir_text_lbl
                text: root.lbl_collecting
                font_size: dp(13)
                color: 0.6, 0.65, 0.7, 1
                size_hint_y: None
                height: dp(30)
                text_size: self.width, None
                halign: 'center'
                valign: 'middle'

        # Метрики — главное (крупные значения + пики)
        GridLayout:
            cols: 2
            spacing: dp(8)
            size_hint_y: 0.46
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

        # Джиттер — компактная строка
        Label:
            id: jitter_lbl
            text: ''
            font_size: dp(13)
            color: 0.6, 0.65, 0.7, 1
            size_hint_y: None
            height: dp(22)
            text_size: self.width, None
            halign: 'center'

        # Управление графиком
        BoxLayout:
            size_hint_y: None
            height: dp(40)
            spacing: dp(8)
            Spinner:
                id: graph_param
                text: 'rsrp'
                values: ['rsrp', 'sinr', 'rssi', 'rsrq']
                size_hint_x: 0.5
                font_size: dp(15)
                on_text: root.on_graph_param(self.text)
            Button:
                text: root.lbl_fullscreen
                size_hint_x: 0.5
                font_size: dp(14)
                background_normal: ''
                background_color: 0.2, 0.35, 0.55, 1
                color: 1, 1, 1, 1
                on_release: root.on_fullscreen()

        # График — занимает оставшееся место
        SignalGraph:
            id: signal_graph
            size_hint_y: 0.30

<InfoScreen>:
    name: 'info'
    tower_block: tower_block
    sim_block: sim_block
    status_block: status_block
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.07, 0.09, 0.12, 1
            Rectangle:
                pos: self.pos
                size: self.size
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            padding: dp(8), dp(4)
            spacing: dp(8)
            TopButton:
                text: root.lbl_back
                width: dp(110)
                background_color: 0.2, 0.35, 0.55, 1
                on_release: root.on_back()
            Label:
                text: root.lbl_title
                font_size: dp(17)
                bold: True
                color: 1, 1, 1, 1
        ScrollView:
            bar_width: dp(8)
            bar_color: 0.45, 0.5, 0.55, 1
            bar_inactive_color: 0.25, 0.28, 0.32, 1
            scroll_type: ['bars', 'content']
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10)
                spacing: dp(10)
                SectionCard:
                    SectionTitle:
                        text: root.lbl_tower_title
                    InfoLabel:
                        id: tower_block
                        text: ''
                SectionCard:
                    SectionTitle:
                        text: root.lbl_sim_title
                    InfoLabel:
                        id: sim_block
                        text: ''
                SectionCard:
                    SectionTitle:
                        text: root.lbl_status_title
                    InfoLabel:
                        id: status_block
                        text: ''

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
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            padding: dp(8), dp(4)
            spacing: dp(8)
            TopButton:
                text: root.lbl_back
                width: dp(110)
                background_color: 0.2, 0.35, 0.55, 1
                on_release: root.on_back()
            Label:
                text: root.lbl_title
                font_size: dp(17)
                bold: True
                color: 1, 1, 1, 1
        ScrollView:
            bar_width: dp(8)
            bar_color: 0.45, 0.5, 0.55, 1
            bar_inactive_color: 0.25, 0.28, 0.32, 1
            scroll_type: ['bars', 'content']
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10)
                spacing: dp(12)

                SectionCard:
                    SectionTitle:
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

                SectionCard:
                    SectionTitle:
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

                SectionCard:
                    SectionTitle:
                        text: root.lbl_reboot_section
                    ToolButton:
                        text: root.lbl_reboot
                        background_color: 0.6, 0.3, 0.15, 1
                        on_release: root.on_reboot()

                SectionCard:
                    SectionTitle:
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
    lbl_demo = StringProperty("")
    lbl_help = StringProperty("")
    lang_name = StringProperty("")
    lang_values = ListProperty([])

    def on_pre_enter(self, *args):
        self.refresh_texts()

    def refresh_texts(self) -> None:
        self.subtitle = t("Портативный монитор LTE Huawei")
        self.lbl_ip = t("IP адрес:")
        self.lbl_pw = t("Пароль:")
        self.lbl_connect = t("Подключиться")
        self.lbl_lang = t("Язык:")
        self.lbl_demo = t("Тестовый режим (без модема)")
        self.lbl_help = t("Подсказка")
        self.lang_values = list(LANGUAGES.values())
        self.lang_name = LANGUAGES.get(current_language(), "Русский")

    def on_language(self, name: str) -> None:
        code = {v: k for k, v in LANGUAGES.items()}.get(name)
        if code and code != current_language():
            set_language(code)
            self.refresh_texts()

    def on_help(self) -> None:
        App.get_running_app().show_popup(
            t("Подключение и частые ошибки"),
            t("IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 "
              "или 192.168.3.1). Логин: admin, пароль — на наклейке "
              "роутера.\n"
              "\n"
              "Частые ошибки и что делать:\n"
              "• 401 Unauthorized — неверный пароль, либо в веб-морду уже "
              "вошли с другого устройства. Закройте веб-интерфейс роутера "
              "и проверьте пароль.\n"
              "• 108003 / 108006 — превышено число сессий или уже выполнен "
              "вход. Перезагрузите роутер или подождите 1–2 минуты.\n"
              "• 100002 / 100003 — функция не поддерживается этой моделью "
              "или прошивкой. Часть возможностей будет недоступна — это "
              "нормально.\n"
              "• 125002 / 125003 — устарел токен сессии. Переподключитесь.\n"
              "• Таймаут / нет ответа — проверьте, что телефон подключён "
              "к Wi-Fi именно этого роутера и IP введён верно."))

    def on_demo(self) -> None:
        App.get_running_app().start_demo()

    def on_connect(self) -> None:
        app = App.get_running_app()
        ip = self.ip_input.text.strip()
        if not is_valid_ip(ip):
            self.status_lbl.text = t(
                "Неверный IP-адрес: {ip}\nПример: 192.168.8.1").format(ip=ip)
            return
        self.status_lbl.text = t("Подключение...")
        app.connect(ip, self.pw_input.text)


class MonitorScreen(Screen):
    lbl_disconnect = StringProperty("")
    lbl_collecting = StringProperty("")
    lbl_tools = StringProperty("")
    lbl_info = StringProperty("")
    lbl_fullscreen = StringProperty("")

    def on_pre_enter(self, *args):
        self.lbl_disconnect = t("Отключиться")
        self.lbl_collecting = t("Накапливаю данные...")
        self.lbl_tools = t("Сеть")
        self.lbl_info = t("Инфо")
        self.lbl_fullscreen = t("Во весь экран")

    def on_disconnect(self) -> None:
        App.get_running_app().disconnect()

    def on_tools(self) -> None:
        self.manager.current = 'tools'

    def on_info(self) -> None:
        self.manager.current = 'info'

    def on_graph_param(self, param: str) -> None:
        App.get_running_app().graph_param = param
        App.get_running_app().refresh_graph()

    def on_fullscreen(self) -> None:
        App.get_running_app().open_fullscreen_graph()


class InfoScreen(Screen):
    lbl_back = StringProperty("")
    lbl_title = StringProperty("")
    lbl_tower_title = StringProperty("")
    lbl_sim_title = StringProperty("")
    lbl_status_title = StringProperty("")

    def on_pre_enter(self, *args):
        self.lbl_back = t("← Назад")
        self.lbl_title = t("Информация")
        self.lbl_tower_title = t("Информация о станции")
        self.lbl_sim_title = t("SIM / Устройство")
        self.lbl_status_title = t("Состояние")
        App.get_running_app().refresh_info_screen()

    def on_back(self) -> None:
        self.manager.current = 'monitor'


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
        self.lbl_title = t("Сеть")
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
        self.lbl_reboot = t("Перезагрузить роутер")
        self.lbl_whitelist = t("Белые списки (РФ)")
        self.hint_whitelist = t(
            "⚠ Телефон должен быть подключён к Wi-Fi именно этого "
            "роутера — иначе тест измерит чужой канал. Применимо только "
            "для РФ.")
        self.lbl_wl_check = t("Проверить сейчас")
        self.antenna_values = [t(k) for k in ANTENNA_MODES]
        self.antenna_spinner.text = t("Авто")
        self._build_band_checkboxes()

    def _build_band_checkboxes(self) -> None:
        if self._bands_built:
            return
        from kivy.metrics import dp
        from kivy.uix.checkbox import CheckBox
        from kivy.uix.label import Label as KLabel

        row_h = dp(44)
        self.band_vars: Dict[str, Any] = {}
        for band_name in BANDS:
            grid = self.bands_grid
            # В GridLayout с size_hint_y=None детям нужна явная высота,
            # иначе строки схлопываются и накладываются друг на друга.
            cb = CheckBox(size_hint=(None, None), width=dp(40), height=row_h)
            lbl = KLabel(text=t(band_name), color=(0.85, 0.88, 0.9, 1),
                         halign='left', valign='middle',
                         size_hint_y=None, height=row_h)
            lbl.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
            grid.add_widget(cb)
            grid.add_widget(lbl)
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
        self.wl_color = [0.9, 0.5, 0.2, 1]
        self.wl_detail = t("Подождите 1–3 секунды.")
        App.get_running_app().whitelist_check()

    def show_whitelist_result(self, title: str, detail: str, color) -> None:
        self.wl_verdict = t(title)
        self.wl_detail = detail
        self.wl_color = list(color)


# =========================================================
# ПРИЛОЖЕНИЕ
# =========================================================

class Hua4GMonApp(App):
    def build(self):
        # Цвет фона окна — иначе непокрытые области (под коротким контентом,
        # системные отступы) показываются чёрными вместо нашей тёмной темы.
        from kivy.core.window import Window
        Window.clearcolor = (0.07, 0.09, 0.12, 1)
        # Шрифт со стрелками/символами вместо «квадратов с крестиком».
        if os.path.exists(_FONT_PATH):
            LabelBase.register(name='Roboto',
                               fn_regular=_FONT_PATH,
                               fn_bold=_FONT_PATH)
        self.title = f"{APP_NAME} v{__version__}"
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.client: Optional[Client] = None
        self._cached_ip = ""
        self._cached_pw = ""
        self.connected = False
        self.auto_reconnect = True
        self.demo_mode = False
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.dir_history: list = []
        self.peak_values: Dict[str, Any] = {p: '-' for p in DYNAMIC_PARAMS}
        self.values: Dict[str, list] = {p: [] for p in DYNAMIC_PARAMS}
        self.device_info: Dict[str, Any] = {}
        self.last_data: Dict[str, Any] = {}
        self._data_lock = threading.Lock()
        self.graph_param = 'rsrp'
        self._fs_graph = None

        from kivy.factory import Factory
        Factory.register('SignalGraph', cls=SignalGraph)
        self.sm: ScreenManager = Builder.load_string(KV)
        return self.sm

    # ---- Подключение ----

    def _reset_session(self) -> None:
        self.dir_history.clear()
        self.peak_values = {p: '-' for p in DYNAMIC_PARAMS}
        self.values = {p: [] for p in DYNAMIC_PARAMS}
        with self._data_lock:
            self.last_data = {}

    def connect(self, ip: str, password: str) -> None:
        self.demo_mode = False
        self._cached_ip = ip
        self._cached_pw = password
        self._stop_event.clear()
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self._reset_session()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def start_demo(self) -> None:
        """Тестовый режим: демо-данные без реального модема (для эмулятора)."""
        self.demo_mode = True
        self._stop_event.clear()
        self.auto_reconnect = False
        self.connected = True
        self._reset_session()
        self.device_info = {
            'DeviceName': 'B525s-23a (demo)', 'SerialNumber': 'DEMO1234567890',
            'Imei': '860000000000001', 'Imsi': '250010000000001',
            'Iccid': '8970100000000000001', 'Msisdn': '+79000000000',
            'SoftwareVersion': '11.0.1.1(DEMO)',
        }
        self._goto_monitor()
        self._thread = threading.Thread(target=self._demo_worker, daemon=True)
        self._thread.start()

    def _demo_worker(self) -> None:
        import math
        import random
        i = 0
        while not self._stop_event.is_set():
            base_rsrp = -85 + 12 * math.sin(i / 6.0)
            data = {
                'rsrp': round(base_rsrp + random.uniform(-1.5, 1.5), 1),
                'rsrq': round(-9 + random.uniform(-2, 2), 1),
                'sinr': round(12 + 6 * math.sin(i / 8.0)
                              + random.uniform(-1, 1), 1),
                'rssi': round(-62 + random.uniform(-3, 3), 1),
                'plmn': '25001',
                'band': str(random.choice([3, 7, 20])),
                'earfcn': 1300,
                'cell_id': 12345 * 256 + 7,
                'pci': random.randint(1, 503),
                'dlbandwidth': '20MHz',
                'CurrentDownloadRate': random.randint(2_000_000, 9_000_000),
                'CurrentUploadRate': random.randint(200_000, 2_000_000),
                'TotalDownload': 1048576 * (50 + i),
                'TotalUpload': 1048576 * (10 + i // 3),
                'CurrentConnectTime': 60 + i,
                'Temperature': '38',
                'dl_mcs': 26,
                'ul_mcs': 18,
                'tac': '12345',
                'ulbandwidth': '10MHz',
                'txpower': 'PPusch:12dBm PPucch:6dBm',
                'transmode': '2x2 MIMO',
                'CurrentMonthDownload': 1048576 * 1024 * 8,
                'CurrentMonthUpload': 1048576 * 1024,
            }
            enodeb, sector = parse_cell_id(data['cell_id'])
            data['enodeb'] = enodeb
            data['sector'] = sector
            data['aggregation'] = ("Активна" if "+" in data['band']
                                   else "Нет (Single)")
            self._update_ui(data)
            self._set_status(t("ДЕМО"), (0.9, 0.6, 0.2, 1))
            i += 1
            if self._stop_event.wait(1.0):
                break

    def disconnect(self) -> None:
        self.auto_reconnect = False
        self.connected = False
        self.demo_mode = False
        self._stop_event.set()
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
        self.device_info = {}
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
            self.device_info = self.client.device.information() or {}
            self.connected = True
            self._goto_monitor()
        except Exception as e:
            self._show_conn_error(str(e))
            return

        tick = 0
        month_cache: Dict[str, Any] = {}
        while not self._stop_event.is_set():
            client = self.client
            if client is None:
                break
            try:
                sig = client.device.signal()
                plmn = client.net.current_plmn()
                status = client.monitoring.status()
                traffic = client.monitoring.traffic_statistics()
                # Месячная статистика меняется медленно и есть не на всех
                # моделях (USB-стики часто без неё) — опрашиваем редко и
                # молча игнорируем, если endpoint недоступен.
                if tick % 30 == 0:
                    try:
                        ms = client.monitoring.month_statistics()
                        if ms:
                            month_cache = ms
                    except Exception:
                        pass
                tick += 1
                data = {**(sig or {}), **(plmn or {}),
                        **(status or {}), **(traffic or {}), **month_cache}
                data['plmn'] = (plmn or {}).get('Numeric',
                                                data.get('plmn', ''))
                enodeb, sector = parse_cell_id(data.get('cell_id'))
                if enodeb is not None:
                    data['enodeb'] = enodeb
                    data['sector'] = sector
                band_str = str(data.get('band', ''))
                data['aggregation'] = ("Активна"
                                       if ("+" in band_str or "CA" in band_str)
                                       else "Нет (Single)")
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
        snippet = err if len(err) < 160 else err[:160] + "..."
        scr.status_lbl.text = t("Связь с роутером не удалась:\n\n{err}").format(
            err=snippet)

    @mainthread
    def _set_status(self, text: str, color) -> None:
        scr = self.sm.get_screen('monitor')
        scr.status_lbl.text = text
        scr.status_lbl.color = color

    @mainthread
    def _update_ui(self, data: Dict[str, Any]) -> None:
        with self._data_lock:
            self.last_data = dict(data)
        scr = self.sm.get_screen('monitor')
        if not self.demo_mode:
            scr.status_lbl.text = t("Подключено")
            scr.status_lbl.color = (0.2, 0.8, 0.4, 1)

        current_vals: Dict[str, Optional[float]] = {
            p: extract_number(data.get(p)) for p in DYNAMIC_PARAMS}

        box_by_param = {'rsrp': scr.rsrp_box, 'rssi': scr.rssi_box,
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
            if self.peak_values[p] == '-' or val > self.peak_values[p]:
                self.peak_values[p] = val
            box.metric_peak = t("Пик: {v}").format(v=self.peak_values[p])
            self.values[p].append(val)
            if len(self.values[p]) > 100:
                self.values[p].pop(0)

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

        # Джиттер
        if len(self.values['rsrp']) >= JITTER_WINDOW:
            recent = self.values['rsrp'][-JITTER_WINDOW:]
            jitter = max(recent) - min(recent)
            jcol = ('green' if jitter < 3
                    else 'orange' if jitter < 7 else 'red')
            scr.jitter_lbl.text = t(
                "Джиттер: {j:.1f} dB").format(j=jitter)
            scr.jitter_lbl.color = _hex_to_rgba(jcol)

        # График выбранного параметра (на мониторе и в fullscreen, если открыт)
        self._draw_graph(scr.signal_graph)
        if self._fs_graph is not None:
            self._draw_graph(self._fs_graph)

    def _draw_graph(self, widget) -> None:
        param = self.graph_param
        y_min, y_max, title, unit = _graph_axes(param)
        widget.set_data(self.values.get(param, []), y_min, y_max, title, unit)

    @mainthread
    def refresh_graph(self) -> None:
        scr = self.sm.get_screen('monitor')
        self._draw_graph(scr.signal_graph)
        if self._fs_graph is not None:
            self._draw_graph(self._fs_graph)

    @mainthread
    def open_fullscreen_graph(self) -> None:
        from kivy.uix.boxlayout import BoxLayout as BL
        from kivy.uix.button import Button as Btn

        param = self.graph_param
        y_min, y_max, title, unit = _graph_axes(param)
        root = BL(orientation='vertical', spacing=8, padding=8)
        graph = SignalGraph()
        graph.set_data(self.values.get(param, []), y_min, y_max, title, unit)
        # Запоминаем график, чтобы _update_ui обновлял его в реальном
        # времени, пока Popup открыт.
        self._fs_graph = graph
        root.add_widget(graph)
        close = Btn(text=t("← Назад"), size_hint_y=None, height='48dp',
                    background_normal='', background_color=(0.2, 0.35, 0.55, 1))
        popup = Popup(title=f"{title} ({unit})", content=root,
                      size_hint=(0.98, 0.9))

        def _on_dismiss(*a):
            self._fs_graph = None

        popup.bind(on_dismiss=_on_dismiss)
        close.bind(on_release=lambda *a: popup.dismiss())
        root.add_widget(close)
        popup.open()

    @mainthread
    def refresh_info_screen(self) -> None:
        """Заполняет экран Информация из последних данных и device_info."""
        scr = self.sm.get_screen('info')
        with self._data_lock:
            data = dict(self.last_data)

        earfcn_raw = data.get('earfcn', data.get('Earfcn', '-'))
        plmn = str(data.get('plmn', '-'))
        op = ''
        if plmn != '-' and len(plmn) >= 5:
            op = PLMN_MAP.get(plmn, t("Неизвестный оператор"))
        band = format_band_label(data.get('band'), earfcn_raw)
        nd = t("Нет данных")

        def g(key, default='-'):
            v = data.get(key, default)
            return v if v not in (None, '') else default

        # Ширина канала: показываем DL и UL вместе, если есть UL
        ul_bw = g('ulbandwidth', '')
        bw = g('dlbandwidth')
        if ul_bw not in ('', '-', None):
            bw = f"DL {bw} / UL {ul_bw}"

        tower_lines = [
            f"{t('Оператор (PLMN)')}: {plmn}"
            f"{(' (' + op + ')') if op else ''}",
            f"{t('Рабочий Band (LTE)')}: {band}",
            f"{t('EARFCN (канал DL)')}: "
            f"{earfcn_raw if earfcn_raw not in (None, '', '-') else '-'}",
            f"{t('Агрегация (CA)')}: {t(str(g('aggregation')))}",
            f"{t('Ширина канала')}: {bw}",
            f"{t('Сектор антенны (PCI)')}: {g('pci')}",
            f"{t('eNodeB (Вышка)')}: {g('enodeb')}",
            f"{t('Cell (Локальный сектор)')}: {g('sector')}",
        ]
        # TAC (Tracking Area Code) — зона, в которой работает сота
        tac = _first_present(data, ('tac', 'TAC'))
        if tac is not None:
            tower_lines.append(f"{t('TAC (зона)')}: {tac}")
        scr.tower_block.text = "\n".join(tower_lines)

        di = self.device_info or {}

        def d(key):
            v = di.get(key, '')
            return str(v) if v not in (None, '') else nd

        sim_lines = [
            f"{t('IMEI (роутер)')}: {d('Imei')}",
            f"{t('IMSI (SIM)')}: {d('Imsi')}",
            f"{t('ICCID (SIM-карта)')}: {d('Iccid')}",
            f"{t('Номер телефона')}: {d('Msisdn')}",
            f"{t('Серийный номер')}: {d('SerialNumber')}",
            f"{t('Модель')}: {d('DeviceName')}",
            f"{t('Прошивка')}: {d('SoftwareVersion')}",
        ]
        scr.sim_block.text = "\n".join(sim_lines)

        # Состояние
        import datetime
        up = g('CurrentConnectTime', g('ConnectionTime', 0))
        try:
            up_i = int(up)
            uptime = str(datetime.timedelta(seconds=up_i)) if up_i > 0 else '-'
        except (TypeError, ValueError):
            uptime = '-'
        rsrp_v = self.values.get('rsrp', [])
        sinr_v = self.values.get('sinr', [])
        status_lines = [
            f"{t('Время сессии')}: {uptime}",
            f"{t('Температура чипа')}: {g('Temperature', nd)}",
            f"{t('Скорость (Download)')}: "
            f"{format_rate_mbps(g('CurrentDownloadRate', 0))}",
            f"{t('Скорость (Upload)')}: "
            f"{format_rate_mbps(g('CurrentUploadRate', 0))}",
            f"{t('Скачано за сессию')}: "
            f"{format_bytes_mb(g('TotalDownload', 0))}",
            f"{t('Отдано за сессию')}: "
            f"{format_bytes_mb(g('TotalUpload', 0))}",
            f"{t('RSRP мин / макс')}: "
            + (f"{min(rsrp_v):g} / {max(rsrp_v):g} dBm" if rsrp_v else '-'),
            f"{t('SINR мин / макс')}: "
            + (f"{min(sinr_v):g} / {max(sinr_v):g} dB" if sinr_v else '-'),
        ]
        # Модуляция DL/UL — двусторонняя. Показываем обе стороны, если
        # роутер их отдаёт (имена полей варьируются между прошивками).
        # Модуляция передаётся как MCS-индекс — расшифровываем в тип QAM.
        dl = _first_present(data, ('dl_mcs', 'dlmcs', 'dlMcs'))
        ul = _first_present(data, ('ul_mcs', 'ulmcs', 'ulMcs'))
        if dl is not None:
            status_lines.append(
                f"{t('Модуляция DL')}: {_mcs_label(dl)}")
        if ul is not None:
            status_lines.append(
                f"{t('Модуляция UL')}: {_mcs_label(ul)}")
        # Мощность передатчика модема (txpower) — косвенный индикатор
        # качества UL: чем выше, тем сильнее модем «вынужден кричать».
        txp = _first_present(data, ('txpower', 'TxPower', 'tx_power'))
        if txp is not None:
            status_lines.append(f"{t('Мощность передатчика')}: {txp}")
        # Режим MIMO (число потоков)
        mimo = _first_present(data, ('transmode', 'TransMode', 'mimo'))
        if mimo is not None:
            status_lines.append(f"{t('Режим MIMO')}: {mimo}")
        # Месячный трафик (если роутер отдаёт month_statistics)
        m_dl = _first_present(data, ('CurrentMonthDownload',))
        m_ul = _first_present(data, ('CurrentMonthUpload',))
        if m_dl is not None or m_ul is not None:
            status_lines.append(
                f"{t('Трафик за месяц (↓/↑)')}: "
                f"{format_bytes_mb(m_dl or 0)} / {format_bytes_mb(m_ul or 0)}")
        scr.status_block.text = "\n".join(status_lines)

    def _direction(self):
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

    # ---- Инструменты: Band Lock / антенна / reboot / белые списки ----

    def _run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _block_in_demo(self) -> bool:
        if self.demo_mode:
            self.show_popup(
                t("Тестовый режим"),
                t("Операции с роутером недоступны в тестовом режиме."))
            return True
        return False

    def apply_bands(self, selected_names: list) -> None:
        if self._block_in_demo():
            return
        if self.client is None:
            self.show_popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        mask = sum(BANDS[n] for n in selected_names if n in BANDS)
        if mask == 0:
            self.show_popup(t("Внимание"), t("Выберите хотя бы один диапазон!"))
            return
        hex_mask = format(mask, 'X')
        client = self.client

        def task():
            try:
                client.net.set_net_mode(hex_mask, NETBAND_AUTO_MASK,
                                        NETMODE_LTE_ONLY)
                self.show_popup(t("Успех"),
                                t("Band Lock применён (mask: {mask}).").format(
                                    mask=hex_mask))
            except Exception as e:
                self.show_popup(t("Ошибка"),
                                t("Роутер отклонил команду:\n{err}").format(
                                    err=str(e)))
        self._run_bg(task)

    def reset_bands(self) -> None:
        if self._block_in_demo():
            return
        if self.client is None:
            self.show_popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        client = self.client

        def task():
            try:
                client.net.set_net_mode(LTEBAND_AUTO_ALL,
                                        NETBAND_AUTO_MASK, NETMODE_AUTO)
                self.show_popup(t("Успех"), t("Сеть сброшена в AUTO."))
            except Exception as e:
                self.show_popup(t("Ошибка"), str(e))
        self._run_bg(task)

    def apply_antenna(self, label: str) -> None:
        if self._block_in_demo():
            return
        if self.client is None:
            self.show_popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        # Спиннер показывает переведённую метку — вернём русский ключ,
        # по которому работает parse_antenna_value.
        rev = {t(k): k for k in ANTENNA_MODES}
        ant_val = parse_antenna_value(rev.get(label, label))
        if ant_val is None:
            self.show_popup(t("Ошибка"), t("Неизвестный режим антенны."))
            return
        client = self.client

        def task():
            try:
                try:
                    from huawei_lte_api.enums.device import AntennaTypeEnum
                    client.device.set_antenna_settings(AntennaTypeEnum(ant_val))
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
                self.show_popup(t("Успех"),
                                t("Тип антенны изменён: {mode}").format(
                                    mode=label))
            except Exception as e:
                self.show_popup(t("Ошибка"), str(e))
        self._run_bg(task)

    def confirm_reboot(self) -> None:
        if self._block_in_demo():
            return
        if self.client is None:
            self.show_popup(t("Ошибка"), t("Сначала подключитесь к роутеру."))
            return
        from kivy.uix.boxlayout import BoxLayout as BL
        from kivy.uix.button import Button as Btn
        from kivy.uix.label import Label as KLabel

        content = BL(orientation='vertical', spacing=10, padding=10)
        msg = KLabel(text=t("Перезагрузить роутер?\n\nСоединение с интернетом "
                            "прервётся на 1–2 минуты. После загрузки "
                            "переподключитесь вручную."), halign='center')
        msg.bind(size=lambda i, v: setattr(i, 'text_size', v))
        content.add_widget(msg)
        btns = BL(size_hint_y=None, height=48, spacing=10)
        popup = Popup(title=t("Подтверждение"), content=content,
                      size_hint=(0.9, 0.5))
        yes = Btn(text=t("Перезагрузить роутер"),
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
                self.show_popup(t("Перезагрузка"),
                                t("Команда отправлена. Роутер вернётся через "
                                  "1–2 минуты."))
            except Exception as e:
                self.show_popup(t("Ошибка"),
                                t("Не удалось перезагрузить:\n{err}").format(
                                    err=str(e)))
        self._run_bg(task)

    def whitelist_check(self) -> None:
        def task():
            white = [(h, tcp_reachable(h, p)[0]) for h, p in WHITELIST_HOSTS_RU]
            neutral = [(h, tcp_reachable(h, p)[0])
                       for h, p in CONTROL_HOSTS_NEUTRAL]
            title, detail, hexcolor = analyze_whitelist_results(white, neutral)
            self._deliver_whitelist(title, detail, _hex_to_rgba(hexcolor))
        self._run_bg(task)

    @mainthread
    def _deliver_whitelist(self, title, detail, color) -> None:
        self.sm.get_screen('tools').show_whitelist_result(title, detail, color)

    @mainthread
    def show_popup(self, title: str, message: str) -> None:
        from kivy.uix.label import Label as KLabel
        from kivy.uix.scrollview import ScrollView as SV

        lbl = KLabel(text=message, halign='left', valign='top',
                     size_hint_y=None, font_size='14sp')
        lbl.bind(width=lambda i, w: setattr(i, 'text_size', (w, None)),
                 texture_size=lambda i, ts: setattr(i, 'height', ts[1]))
        sv = SV()
        sv.add_widget(lbl)
        Popup(title=title, content=sv, size_hint=(0.92, 0.8)).open()

    def on_stop(self):
        self._stop_event.set()


def main() -> None:
    Hua4GMonApp().run()


if __name__ == "__main__":
    main()
