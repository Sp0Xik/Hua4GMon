"""
Hua4GMon — Android-версия (Kivy).

Использует тот же пакет ``core/``, что и Windows-версия (main.py):
сессия с роутером (RouterSession/SessionWorker), разбор данных
(Snapshot), состояние (SignalState), Band Lock, RF-аналитика, переводы.
Здесь только UI-слой на Kivy.

Экраны:
    * Подключение — автопоиск роутера, пароль, язык, подсказка, тестовый режим;
    * Монитор — крупная выбранная метрика со стрелкой и «Δ до пика»,
      RSRP/SINR/RSRQ/RSSI, строка вышки, события смены соты, советы,
      график; нижняя панель: звук-парктроник, сброс пиков, меню;
    * Информация — вышка, лучшие соты сессии, SIM/устройство, состояние
      (обновляется в реальном времени);
    * Сеть — Band Lock, антенна, перезагрузка, переподключение связи,
      диагностика, белые списки (РФ).

Жизненный цикл: на паузе опрос приостанавливается, экран не гаснет
только во время мониторинга, «Назад» ведёт к монитору.
Сборка APK — Buildozer (buildozer.spec).
"""
from __future__ import annotations

import datetime
import importlib
import importlib.abc
import importlib.util
import logging
import os
import platform as py_platform
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

# --- Android crypto-совместимость (ДО первого обращения к huawei_lte_api) ---
# huawei-lte-api требует pycryptodomex (неймспейс Cryptodome), но у него
# нет рецепта python-for-android, и его нативные .so не грузятся на
# Android. Зато pycryptodome (неймспейс Crypto) рецепт имеет. Код пакетов
# идентичен — перенаправляем Cryptodome.* -> Crypto.*. На десктопе, где
# настоящий Cryptodome есть, алиас не включается.


class _CryptodomeAliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
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

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty, ListProperty, StringProperty
from kivy.uix.modalview import ModalView
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, ScreenManager
from kivy.uix.widget import Widget
from kivy.utils import escape_markup, platform

from core import (
    ANTENNA_MODES,
    DISCOVERY_CANDIDATES,
    GRAPH_HISTORY,
    GRAPH_MIN_SPAN,
    LANGUAGES,
    MIMO_LABELS,
    PARAM_RANGES,
    RAT_LABELS,
    REGION_BANDS,
    TREND_DOWN,
    TREND_FLAT,
    TREND_UP,
    UPLINK_LABELS,
    DemoModem,
    RouterSession,
    SessionWorker,
    SignalState,
    Snapshot,
    __version__,
    advice,
    build_diagnostics,
    calculate_overall_health,
    configure_library_logging,
    current_language,
    demo_angle_hint,
    demo_factory,
    diagnostics_json,
    discover_router,
    evaluate_signal,
    format_bytes_mb,
    format_mimo,
    format_modulation,
    format_rate_mbps,
    humanize_error,
    is_valid_ip,
    library_version,
    lock_warnings,
    lockable_bands,
    lte_band_label,
    mimo_status,
    parse_antenna_value,
    run_whitelist_check,
    set_language,
    t,
    uplink_status,
)

APP_NAME = "Hua4GMon"

# Логи видны в logcat (adb logcat | grep python).
logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
configure_library_logging()
logger = logging.getLogger(APP_NAME)

LTE_PARAMS = ['sinr', 'rsrp', 'rsrq', 'rssi']
NR_PARAMS = ['nr_sinr', 'nr_rsrp']
PARAM_TITLES = {'rsrp': 'RSRP', 'rssi': 'RSSI', 'sinr': 'SINR', 'rsrq': 'RSRQ',
                'nr_rsrp': 'NR RSRP', 'nr_sinr': 'NR SINR', 'nr_rsrq': 'NR RSRQ'}
TREND_GLYPHS = {TREND_UP: "↑", TREND_DOWN: "↓", TREND_FLAT: "→"}

# Шрифт со стрелками (↑→↓), ⚠, Δ: встроенный Roboto в Kivy их не содержит.
# Kivy не подменяет недостающие символы другим шрифтом, а цветных эмодзи
# (🔊 🧪 ✅ …) в DejaVu Sans нет — они рисуются квадратом. Все подписи
# Android используют только символы этого шрифта; это проверяет
# tests/test_project.py::test_android_text_fits_bundled_font.
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
_FONT_PATH = os.path.join(_ASSETS, 'DejaVuSans.ttf')
# Жирное начертание — для крупных значений и заголовков (читаемость на солнце).
_FONT_BOLD_PATH = os.path.join(_ASSETS, 'DejaVuSans-Bold.ttf')

THEMES: dict[str, dict[str, list[float]]] = {
    'dark': {
        'c_bg': [0.07, 0.09, 0.12, 1], 'c_card': [0.12, 0.14, 0.18, 1],
        'c_text': [0.92, 0.94, 0.96, 1], 'c_sub': [0.62, 0.67, 0.72, 1],
        'c_accent': [0.04, 0.47, 0.84, 1], 'c_button2': [0.2, 0.24, 0.3, 1],
        'c_graph_bg': [0.1, 0.12, 0.16, 1], 'c_grid': [0.2, 0.23, 0.27, 1],
    },
    # «Солнце»: максимальный контраст для экрана под прямым солнцем.
    'sun': {
        'c_bg': [1, 1, 1, 1], 'c_card': [0.92, 0.93, 0.94, 1],
        'c_text': [0, 0, 0, 1], 'c_sub': [0.2, 0.2, 0.2, 1],
        'c_accent': [0.0, 0.3, 0.7, 1], 'c_button2': [0.8, 0.82, 0.85, 1],
        'c_graph_bg': [1, 1, 1, 1], 'c_grid': [0.8, 0.8, 0.8, 1],
    },
}


def unit_of(param: str) -> str:
    return "dBm" if param.endswith(('rsrp', 'rssi')) else "dB"


def fmt_num(value: float | None) -> str:
    """Число для подписи: целые — как есть (eNB 1048575, а не 1.04858e+06)."""
    if value is None:
        return "-"
    return str(value) if isinstance(value, int) else f"{value:g}"


def hex_to_rgba(color: str, sun: bool = False) -> list[float]:
    """'#00b894' или 'gray' → RGBA. В режиме «Солнце» цвета темнее."""
    named = {'gray': '#808080', 'green': '#33cc66', 'orange': '#e68033', 'red': '#d93333'}
    h = named.get(color, color).lstrip('#')
    try:
        rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except (ValueError, IndexError):
        rgb = [0.5, 0.5, 0.5]
    if sun:
        rgb = [c * 0.7 for c in rgb]
    return [*rgb, 1.0]


def beep_interval(delta: float | None) -> float:
    """Пауза между сигналами парктроника: у пика — часто, далеко — редко."""
    if delta is None:
        return 1.5
    return 0.12 + min(1.3, abs(delta) * 0.12)


# =========================================================
# Платформенные сервисы Android (pyjnius)
# =========================================================

def set_keep_screen_on(on: bool) -> None:
    """Экран не гаснет, пока идёт мониторинг (FLAG_KEEP_SCREEN_ON).

    Флаг окна не требует разрешения WAKE_LOCK и снимается системой, когда
    приложение уходит в фон. Hardware validation required.
    """
    if platform != 'android':
        return
    try:
        from android.runnable import run_on_ui_thread
        from jnius import autoclass
    except ImportError:
        logger.warning("pyjnius/android недоступны — keep-screen-on пропущен")
        return

    @run_on_ui_thread
    def apply() -> None:
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        flag = autoclass('android.view.WindowManager$LayoutParams').FLAG_KEEP_SCREEN_ON
        window = activity.getWindow()
        if on:
            window.addFlags(flag)
        else:
            window.clearFlags(flag)
    apply()


class ToneBeeper:
    """Короткие сигналы через android.media.ToneGenerator.

    Hardware validation required: громкость и задержка на устройстве.
    """

    def __init__(self) -> None:
        self._tone: Any = None
        self._tone_id = 0
        if platform != 'android':
            return
        try:
            from jnius import autoclass
            tone_cls = autoclass('android.media.ToneGenerator')
            audio = autoclass('android.media.AudioManager')
            self._tone = tone_cls(audio.STREAM_MUSIC, 80)
            self._tone_id = tone_cls.TONE_PROP_BEEP
        except Exception:
            logger.warning("ToneGenerator недоступен", exc_info=True)

    @property
    def available(self) -> bool:
        return self._tone is not None

    def beep(self, ms: int = 60) -> None:
        if self._tone is not None:
            self._tone.startTone(self._tone_id, ms)

    def release(self) -> None:
        if self._tone is not None:
            self._tone.release()
            self._tone = None


# =========================================================
# График на Kivy canvas
# =========================================================

class SignalGraph(Widget):
    """Линейный график: автомасштаб, линия пика, признак устаревания.

    Размеры — в dp/sp; текстуры подписей кэшируются (подписи оси почти
    не меняются, а создание текстуры — самая дорогая часть отрисовки).
    """

    _TEX_CACHE_MAX = 96

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._values: list[float] = []
        self._peak: float | None = None
        self._param = 'sinr'
        self._stale = False
        self._tex: dict[tuple, Any] = {}
        self.bind(pos=lambda *a: self._redraw(), size=lambda *a: self._redraw())

    def set_data(self, values: list[float], param: str, peak: float | None,
                 stale: bool) -> None:
        self._values = list(values)
        self._param = param
        self._peak = peak
        self._stale = stale
        self._redraw()

    def _texture(self, text: str, size: float, color: tuple, bold: bool = False) -> Any:
        key = (text, size, color, bold)
        tex = self._tex.get(key)
        if tex is None:
            from kivy.core.text import Label as CoreLabel
            lbl = CoreLabel(text=text, font_size=size, bold=bold, color=color)
            lbl.refresh()
            tex = lbl.texture
            if len(self._tex) >= self._TEX_CACHE_MAX:
                self._tex.clear()
            self._tex[key] = tex
        return tex

    def _y_range(self) -> tuple[float, float]:
        base = self._param.replace('nr_', '')
        lo_lim, hi_lim = PARAM_RANGES.get(base, (-140, 40))
        pts = self._values + ([self._peak] if self._peak is not None else [])
        if not pts:
            return float(lo_lim), float(hi_lim)
        lo, hi = min(pts), max(pts)
        span = GRAPH_MIN_SPAN.get(base, 10.0)
        if hi - lo < span:
            mid = (hi + lo) / 2
            lo, hi = mid - span / 2, mid + span / 2
        lo = max(lo_lim, 5 * ((lo - 1) // 5))
        hi = min(hi_lim, 5 * -((-(hi + 1)) // 5))
        return float(lo), float(max(hi, lo + 1))

    def _redraw(self) -> None:
        from kivy.graphics import Color, Ellipse, Line, Rectangle
        app = App.get_running_app()
        theme = THEMES[app.theme_name] if app else THEMES['dark']
        text_col = tuple(theme['c_sub'])
        self.canvas.clear()
        w, h = self.width, self.height
        pl, pr, pt, pb = dp(44), dp(10), dp(22), dp(20)
        plot_w, plot_h = w - pl - pr, h - pt - pb
        if plot_w <= dp(20) or plot_h <= dp(20):
            return
        x0, y0 = self.x + pl, self.y + pb
        y_min, y_max = self._y_range()
        rng = y_max - y_min

        def y_of(v: float) -> float:
            return y0 + plot_h * (max(y_min, min(y_max, v)) - y_min) / rng

        with self.canvas:
            Color(*theme['c_graph_bg'])
            Rectangle(pos=self.pos, size=self.size)
            for i in range(5):
                gy = y0 + plot_h * i / 4
                Color(*theme['c_grid'])
                Line(points=[x0, gy, x0 + plot_w, gy], width=1)
                tex = self._texture(f"{y_min + rng * i / 4:.0f}", sp(11), text_col)
                Color(1, 1, 1, 1)
                Rectangle(texture=tex, pos=(x0 - tex.width - dp(4), gy - tex.height / 2),
                          size=tex.size)
            title = PARAM_TITLES.get(self._param, self._param.upper())
            tex = self._texture(f"{title} ({unit_of(self._param)})", sp(13),
                                tuple(theme['c_text']), True)
            Color(1, 1, 1, 1)
            Rectangle(texture=tex, pos=(x0, self.y + h - pt + dp(3)), size=tex.size)
            tex = self._texture(t("последние {n} точек").format(n=GRAPH_HISTORY),
                                sp(10), text_col)
            Rectangle(texture=tex, pos=(x0 + (plot_w - tex.width) / 2, self.y + dp(3)),
                      size=tex.size)

            if self._peak is not None:
                py = y_of(self._peak)
                Color(0.0, 0.72, 0.58, 1)
                Line(points=[x0, py, x0 + plot_w, py], width=1, dash_length=dp(4),
                     dash_offset=dp(3))
            if len(self._values) < 2:
                return
            span = max(GRAPH_HISTORY - 1, 1)
            pts: list[float] = []
            for i, v in enumerate(self._values[-GRAPH_HISTORY:]):
                pts.extend([x0 + plot_w * i / span, y_of(v)])
            line_col = (0.6, 0.6, 0.6, 1) if self._stale else (0.0, 0.62, 0.9, 1)
            Color(*line_col)
            Line(points=pts, width=dp(1.5))
            lx, ly = pts[-2], pts[-1]
            r = dp(4)
            Ellipse(pos=(lx - r, ly - r), size=(2 * r, 2 * r))
            label = t("нет свежих данных") if self._stale else \
                f"{fmt_num(self._values[-1])} {unit_of(self._param)}"
            tex = self._texture(label, sp(13), line_col, True)
            Color(1, 1, 1, 1)
            Rectangle(texture=tex, pos=(self.x + w - pr - tex.width, self.y + h - pt + dp(3)),
                      size=tex.size)


class RotatedBox(Widget):
    """Показывает вложенный layout повёрнутым на 90° (альбомно).

    Scatter, а не голый Rotate по canvas: Scatter применяет ту же
    трансформацию к касаниям, поэтому кнопка внутри остаётся кликабельной.
    """

    def __init__(self, inner: Widget, **kw: Any) -> None:
        super().__init__(**kw)
        from kivy.uix.scatter import Scatter
        self._inner = inner
        inner.size_hint = (None, None)
        self._scatter = Scatter(do_rotation=False, do_scale=False,
                                do_translation=False, size_hint=(None, None))
        self._scatter.add_widget(inner)
        self.add_widget(self._scatter)
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *a: Any) -> None:
        land = (self.height, self.width)
        self._scatter.size = land
        self._inner.size = land
        self._scatter.rotation = 90
        self._scatter.center = self.center


# =========================================================
# KV-разметка
# =========================================================

KV = """
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp

<ActionButton@Button>:
    background_normal: ''
    background_color: app.c_accent
    color: 1, 1, 1, 1
    font_size: sp(16)
    size_hint_y: None
    height: dp(52)

<FlatButton@Button>:
    background_normal: ''
    background_color: app.c_button2
    color: app.c_text
    font_size: sp(16)

<SectionCard@BoxLayout>:
    orientation: 'vertical'
    size_hint_y: None
    height: self.minimum_height
    padding: dp(12)
    spacing: dp(8)
    canvas.before:
        Color:
            rgba: app.c_card
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<SectionTitle@Label>:
    font_size: sp(19)
    bold: True
    color: app.c_text
    size_hint_y: None
    height: dp(34)
    text_size: self.width, None
    halign: 'left'
    valign: 'middle'

<AutoLabel@Label>:
    font_size: sp(15)
    color: app.c_text
    size_hint_y: None
    height: self.texture_size[1] + dp(6)
    text_size: self.width, None
    halign: 'left'
    valign: 'top'

<HintLabel@AutoLabel>:
    font_size: sp(13)
    color: app.c_sub

<InfoLabel@AutoLabel>:
    font_size: sp(16)
    line_height: 1.3
    markup: True

<MetricCard@BoxLayout>:
    orientation: 'vertical'
    m_name: ''
    m_value: '-'
    m_status: ''
    m_peak: ''
    m_color: 0.5, 0.5, 0.5, 1
    value_size: sp(34)
    padding: dp(6)
    spacing: dp(1)
    canvas.before:
        Color:
            rgba: app.c_card
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(10)]
    Label:
        text: root.m_name
        font_size: sp(14)
        color: app.c_sub
        size_hint_y: None
        height: dp(18)
    Label:
        text: root.m_value
        font_size: root.value_size
        bold: True
        color: root.m_color
    Label:
        text: root.m_status
        font_size: sp(13)
        color: root.m_color
        size_hint_y: None
        height: dp(18)
    Label:
        text: root.m_peak
        font_size: sp(14)
        color: app.c_sub
        size_hint_y: None
        height: dp(20) if self.text else 0

<Header@BoxLayout>:
    title: ''
    size_hint_y: None
    height: dp(52)
    padding: dp(8), dp(4)
    spacing: dp(8)
    FlatButton:
        text: app.lbl_back
        size_hint_x: None
        width: dp(110)
        on_release: app.go('monitor')
    Label:
        text: root.title
        font_size: sp(18)
        bold: True
        color: app.c_text

ScreenManager:
    ConnectionScreen:
    MonitorScreen:
    InfoScreen:
    ToolsScreen:

<ConnectionScreen>:
    name: 'connection'
    canvas.before:
        Color:
            rgba: app.c_bg
        Rectangle:
            pos: self.pos
            size: self.size
    ScrollView:
        bar_width: dp(8)
        scroll_type: ['bars', 'content']
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: dp(24)
            spacing: dp(12)
            Label:
                text: 'Hua4GMon'
                font_size: sp(38)
                bold: True
                color: app.c_text
                size_hint_y: None
                height: dp(64)
            Label:
                text: root.subtitle
                font_size: sp(15)
                color: app.c_sub
                size_hint_y: None
                height: dp(26)
            AutoLabel:
                text: root.lbl_ip
            BoxLayout:
                size_hint_y: None
                height: dp(50)
                spacing: dp(8)
                TextInput:
                    id: ip_input
                    text: '192.168.8.1'
                    multiline: False
                    font_size: sp(18)
                FlatButton:
                    text: root.lbl_find
                    size_hint_x: 0.45
                    font_size: sp(15)
                    on_release: root.on_find()
            AutoLabel:
                text: root.lbl_pw
            TextInput:
                id: pw_input
                password: True
                multiline: False
                font_size: sp(18)
                size_hint_y: None
                height: dp(50)
                on_text_validate: root.on_connect()
            ActionButton:
                text: root.lbl_connect
                on_release: root.on_connect()
            AutoLabel:
                id: status_lbl
                text: ''
                font_size: sp(15)
                color: root.status_color
                halign: 'center'
            FlatButton:
                text: root.lbl_demo
                size_hint_y: None
                height: dp(48)
                on_release: app.start_demo()
            FlatButton:
                text: root.lbl_help
                size_hint_y: None
                height: dp(44)
                on_release: root.on_help()
            BoxLayout:
                size_hint_y: None
                height: dp(44)
                spacing: dp(8)
                Label:
                    text: root.lbl_lang
                    color: app.c_sub
                    font_size: sp(14)
                    size_hint_x: None
                    width: dp(80)
                Spinner:
                    id: lang_spinner
                    text: root.lang_name
                    values: root.lang_values
                    font_size: sp(14)
                    on_text: root.on_language(self.text)

<MonitorScreen>:
    name: 'monitor'
    canvas.before:
        Color:
            rgba: app.c_bg
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        padding: dp(8)
        spacing: dp(6)
        Label:
            id: status_lbl
            text: ''
            font_size: sp(15)
            bold: True
            color: root.status_color
            size_hint_y: None
            height: self.texture_size[1] + dp(2)
            text_size: self.width, None
            halign: 'center'
        Label:
            id: tower_lbl
            text: '—'
            font_size: sp(15)
            bold: True
            color: app.c_text
            size_hint_y: None
            height: self.texture_size[1] + dp(2)
            text_size: self.width, None
            halign: 'center'
        Label:
            id: event_lbl
            text: ''
            font_size: sp(13)
            color: app.c_sub
            size_hint_y: None
            height: self.texture_size[1] if self.text else 0
            text_size: self.width, None
            halign: 'center'
        BoxLayout:
            size_hint_y: 0.30
            spacing: dp(6)
            MetricCard:
                id: primary_box
                size_hint_x: 0.66
                value_size: sp(54)
            BoxLayout:
                orientation: 'vertical'
                size_hint_x: 0.34
                canvas.before:
                    Color:
                        rgba: app.c_card
                    RoundedRectangle:
                        pos: self.pos
                        size: self.size
                        radius: [dp(10)]
                Label:
                    id: dir_lbl
                    text: '—'
                    font_size: sp(72)
                    bold: True
                    color: 0.5, 0.5, 0.5, 1
                Label:
                    id: dir_text_lbl
                    text: ''
                    font_size: sp(12)
                    color: app.c_sub
                    size_hint_y: None
                    height: dp(44)
                    text_size: self.width - dp(8), None
                    halign: 'center'
                    valign: 'middle'
        GridLayout:
            cols: 3
            spacing: dp(6)
            size_hint_y: 0.17
            MetricCard:
                id: secondary_box
                value_size: sp(30)
            MetricCard:
                id: small1_box
                value_size: sp(24)
            MetricCard:
                id: small2_box
                value_size: sp(24)
        Label:
            id: advice_lbl
            text: ''
            font_size: sp(13)
            color: root.advice_color
            size_hint_y: None
            height: self.texture_size[1] if self.text else 0
            text_size: self.width, None
            halign: 'left'
        BoxLayout:
            size_hint_y: None
            height: dp(42)
            spacing: dp(6)
            Spinner:
                id: graph_param
                text: 'sinr'
                values: root.graph_values
                size_hint_x: 0.5
                font_size: sp(15)
                on_text: app.set_graph_param(self.text)
            FlatButton:
                text: root.lbl_fullscreen
                size_hint_x: 0.5
                font_size: sp(14)
                on_release: app.open_fullscreen_graph()
        SignalGraph:
            id: signal_graph
            size_hint_y: 0.26
        BoxLayout:
            size_hint_y: None
            height: dp(56)
            spacing: dp(6)
            FlatButton:
                text: root.lbl_sound
                background_color: app.c_accent if app.sound_on else app.c_button2
                color: (1, 1, 1, 1) if app.sound_on else app.c_text
                on_release: app.toggle_sound()
            FlatButton:
                text: root.lbl_peaks
                on_release: app.reset_peaks()
            FlatButton:
                text: root.lbl_menu
                on_release: app.open_menu()

<InfoScreen>:
    name: 'info'
    canvas.before:
        Color:
            rgba: app.c_bg
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        Header:
            title: root.lbl_title
        ScrollView:
            bar_width: dp(8)
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
                        text: root.lbl_cells_title
                    InfoLabel:
                        id: cells_block
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
    canvas.before:
        Color:
            rgba: app.c_bg
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        Header:
            title: root.lbl_title
        ScrollView:
            bar_width: dp(8)
            scroll_type: ['bars', 'content']
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10)
                spacing: dp(12)
                AutoLabel:
                    text: root.net_msg
                    color: root.net_msg_color
                    bold: True
                    height: (self.texture_size[1] + dp(6)) if self.text else 0
                SectionCard:
                    SectionTitle:
                        text: root.lbl_bandlock
                    HintLabel:
                        text: root.hint_bandlock
                    AutoLabel:
                        text: root.lock_state
                        bold: True
                    GridLayout:
                        id: bands_grid
                        cols: 2
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: dp(4)
                    FlatButton:
                        text: root.lbl_mark_current
                        size_hint_y: None
                        height: dp(48)
                        on_release: app.mark_current_bands()
                    ActionButton:
                        text: root.lbl_apply_bands
                        on_release: app.apply_bands()
                    BoxLayout:
                        size_hint_y: None
                        height: dp(52)
                        spacing: dp(8)
                        FlatButton:
                            text: root.lbl_reset_auto
                            on_release: app.reset_bands()
                        FlatButton:
                            text: root.lbl_restore
                            on_release: app.restore_bands()
                SectionCard:
                    SectionTitle:
                        text: root.lbl_antenna
                    Spinner:
                        id: antenna_spinner
                        text: root.antenna_text
                        values: root.antenna_values
                        font_size: sp(16)
                        size_hint_y: None
                        height: dp(48)
                    ActionButton:
                        text: root.lbl_apply_antenna
                        on_release: app.apply_antenna(antenna_spinner.text)
                SectionCard:
                    SectionTitle:
                        text: root.lbl_router
                    HintLabel:
                        text: root.hint_router
                    FlatButton:
                        text: root.lbl_reattach
                        size_hint_y: None
                        height: dp(52)
                        on_release: app.confirm_reattach()
                    FlatButton:
                        text: root.lbl_reboot
                        size_hint_y: None
                        height: dp(52)
                        on_release: app.confirm_reboot()
                    FlatButton:
                        text: root.lbl_diag
                        size_hint_y: None
                        height: dp(52)
                        on_release: app.copy_diagnostics()
                SectionCard:
                    SectionTitle:
                        text: root.lbl_whitelist
                    HintLabel:
                        text: root.hint_whitelist
                    ActionButton:
                        text: root.lbl_wl_check
                        on_release: app.whitelist_check()
                    AutoLabel:
                        text: root.wl_verdict
                        font_size: sp(16)
                        bold: True
                        color: root.wl_color
                    AutoLabel:
                        text: root.wl_detail
                        font_size: sp(13)
"""


# =========================================================
# ЭКРАНЫ
# =========================================================

class ConnectionScreen(Screen):
    subtitle = StringProperty("")
    lbl_ip = StringProperty("")
    lbl_pw = StringProperty("")
    lbl_find = StringProperty("")
    lbl_connect = StringProperty("")
    lbl_lang = StringProperty("")
    lbl_demo = StringProperty("")
    lbl_help = StringProperty("")
    lang_name = StringProperty("")
    lang_values = ListProperty([])
    status_color = ListProperty([0.9, 0.5, 0.2, 1])

    def on_pre_enter(self, *args: Any) -> None:
        self.refresh_texts()

    def refresh_texts(self) -> None:
        self.subtitle = t("Портативный монитор LTE/5G Huawei")
        self.lbl_ip = t("IP адрес:")
        self.lbl_pw = t("Пароль:")
        self.lbl_find = t("Найти")
        self.lbl_connect = t("Подключиться")
        self.lbl_lang = t("Язык:")
        self.lbl_demo = t("Тестовый режим (без модема)")
        self.lbl_help = t("Подсказка")
        self.lang_values = list(LANGUAGES.values())
        self.lang_name = LANGUAGES.get(current_language(), "Русский")

    def set_status(self, text: str, color: list[float]) -> None:
        lbl = self.ids.get('status_lbl')
        if lbl is not None:
            lbl.text = text
        self.status_color = color

    def on_language(self, name: str) -> None:
        code = {v: k for k, v in LANGUAGES.items()}.get(name)
        if code and code != current_language():
            set_language(code)
            App.get_running_app().refresh_all_texts()

    def on_help(self) -> None:
        App.get_running_app().show_popup(t("Подключение и частые ошибки"), t(
            "IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 или "
            "192.168.3.1) — кнопка «Найти» проверит их сама. Логин: admin, "
            "пароль — на наклейке роутера.\n\n"
            "Частые ошибки и что делать:\n"
            "• 108006 — неверный логин или пароль.\n"
            "• 108007 — слишком много неудачных попыток: роутер временно "
            "заблокировал вход. Подождите несколько минут.\n"
            "• 108003 — уже выполнен вход с другого устройства. Закройте "
            "веб-интерфейс роутера.\n"
            "• 100002 — функция не поддерживается этой моделью или "
            "прошивкой. Часть возможностей будет недоступна — это нормально.\n"
            "• 100003 / 125002 / 125003 — истекла сессия. Программа войдёт "
            "заново сама.\n"
            "• Нет ответа — проверьте, что телефон подключён к Wi-Fi именно "
            "этого роутера и IP введён верно.\n\n"
            "Тестовый режим показывает работу программы без роутера."))

    def on_find(self) -> None:
        ip_input = self.ids.get('ip_input')
        entered = ip_input.text.strip() if ip_input is not None else ''
        App.get_running_app().discover(entered)

    def on_connect(self) -> None:
        ip_input, pw_input = self.ids.get('ip_input'), self.ids.get('pw_input')
        if ip_input is None or pw_input is None:
            return
        ip = ip_input.text.strip()
        if not is_valid_ip(ip):
            self.set_status(t("Неверный IP-адрес: {ip}\nПример: 192.168.8.1").format(ip=ip),
                            [0.9, 0.3, 0.3, 1])
            return
        App.get_running_app().connect(ip, pw_input.text)


class MonitorScreen(Screen):
    lbl_fullscreen = StringProperty("")
    lbl_sound = StringProperty("")
    lbl_peaks = StringProperty("")
    lbl_menu = StringProperty("")
    graph_values = ListProperty(list(LTE_PARAMS))
    status_color = ListProperty([0.2, 0.8, 0.4, 1])
    advice_color = ListProperty([0.95, 0.62, 0.25, 1])

    def on_pre_enter(self, *args: Any) -> None:
        self.refresh_texts()

    def refresh_texts(self) -> None:
        self.lbl_fullscreen = t("Во весь экран")
        self.lbl_sound = t("♪ Звук")
        self.lbl_peaks = t("⟲ Пики")
        self.lbl_menu = t("☰ Меню")


class InfoScreen(Screen):
    lbl_title = StringProperty("")
    lbl_tower_title = StringProperty("")
    lbl_cells_title = StringProperty("")
    lbl_sim_title = StringProperty("")
    lbl_status_title = StringProperty("")

    def on_pre_enter(self, *args: Any) -> None:
        self.refresh_texts()
        App.get_running_app().refresh_info_screen()

    def refresh_texts(self) -> None:
        self.lbl_title = t("Информация")
        self.lbl_tower_title = t("Информация о станции")
        self.lbl_cells_title = t("Лучшие соты за сессию")
        self.lbl_sim_title = t("SIM / Устройство")
        self.lbl_status_title = t("Состояние")


class ToolsScreen(Screen):
    lbl_title = StringProperty("")
    lbl_bandlock = StringProperty("")
    hint_bandlock = StringProperty("")
    lock_state = StringProperty("")
    lbl_mark_current = StringProperty("")
    lbl_apply_bands = StringProperty("")
    lbl_reset_auto = StringProperty("")
    lbl_restore = StringProperty("")
    lbl_antenna = StringProperty("")
    lbl_apply_antenna = StringProperty("")
    antenna_text = StringProperty("")
    antenna_values = ListProperty([])
    lbl_router = StringProperty("")
    hint_router = StringProperty("")
    lbl_reattach = StringProperty("")
    lbl_reboot = StringProperty("")
    lbl_diag = StringProperty("")
    lbl_whitelist = StringProperty("")
    hint_whitelist = StringProperty("")
    lbl_wl_check = StringProperty("")
    wl_verdict = StringProperty("")
    wl_detail = StringProperty("")
    wl_color = ListProperty([0.5, 0.5, 0.5, 1])
    net_msg = StringProperty("")
    net_msg_color = ListProperty([0.5, 0.5, 0.5, 1])

    def on_pre_enter(self, *args: Any) -> None:
        self.refresh_texts()
        app = App.get_running_app()
        app.build_band_checkboxes()
        app.load_router_config()

    def refresh_texts(self) -> None:
        self.lbl_title = t("Сеть")
        self.lbl_bandlock = t("Фиксация частот (Band Lock)")
        self.hint_bandlock = t(
            "Список взят из модема; замеченные в эфире бэнды — первыми. "
            "Меняются только LTE-бэнды; «Как было» восстановит настройки, "
            "прочитанные при подключении.")
        self.lbl_mark_current = t("Отметить текущие")
        self.lbl_apply_bands = t("Применить Band Lock")
        self.lbl_reset_auto = t("Все (AUTO)")
        self.lbl_restore = t("↩ Как было")
        self.lbl_antenna = t("Переключение антенн")
        self.lbl_apply_antenna = t("Применить")
        self.antenna_values = [t(k) for k in ANTENNA_MODES]
        if not self.antenna_text:
            self.antenna_text = t("Авто")
        self.lbl_router = t("Управление роутером")
        self.hint_router = t(
            "«Переподключить связь» заставляет модем заново выбрать лучшую "
            "соту — быстрее перезагрузки. После перезагрузки программа "
            "переподключится сама.")
        self.lbl_reattach = t("Переподключить связь")
        self.lbl_reboot = t("Перезагрузить роутер")
        self.lbl_diag = t("Скопировать диагностику")
        self.lbl_whitelist = t("Белые списки (РФ)")
        self.hint_whitelist = t(
            "⚠ Телефон должен быть подключён к Wi-Fi именно этого "
            "роутера — иначе тест измерит чужой канал. Применимо только для РФ.")
        self.lbl_wl_check = t("Проверить сейчас")


# =========================================================
# ПРИЛОЖЕНИЕ
# =========================================================

class Hua4GMonApp(App):
    theme_name = StringProperty('dark')
    c_bg = ListProperty(THEMES['dark']['c_bg'])
    c_card = ListProperty(THEMES['dark']['c_card'])
    c_text = ListProperty(THEMES['dark']['c_text'])
    c_sub = ListProperty(THEMES['dark']['c_sub'])
    c_accent = ListProperty(THEMES['dark']['c_accent'])
    c_button2 = ListProperty(THEMES['dark']['c_button2'])
    sound_on = BooleanProperty(False)
    lbl_back = StringProperty("")

    # ---------- запуск и жизненный цикл ----------

    def build(self) -> ScreenManager:
        from kivy.core.window import Window
        Window.clearcolor = tuple(self.c_bg)
        Window.softinput_mode = 'below_target'
        Window.bind(on_keyboard=self._on_keyboard)
        if os.path.exists(_FONT_PATH):
            bold = _FONT_BOLD_PATH if os.path.exists(_FONT_BOLD_PATH) else _FONT_PATH
            LabelBase.register(name='Roboto', fn_regular=_FONT_PATH, fn_bold=bold)
        self.title = f"{APP_NAME} v{__version__}"
        # Журнал сессии нужен только для экспорта CSV, которого на Android нет.
        self.state = SignalState(trend_param='sinr', log_max=GRAPH_HISTORY)
        self.session: RouterSession | None = None
        self.worker: SessionWorker | None = None
        self.demo_modem: DemoModem | None = None
        self.link = 'offline'
        self.interval = 1.0
        self.band_vars: dict[int, Any] = {}
        self._band_list: list[int] = list(REGION_BANDS)
        self._bands_shown: list[int] = []
        self._busy = False
        self._fs_graph: SignalGraph | None = None
        self._last_back = 0.0
        self._beep_ev: Any = None
        self.beeper = ToneBeeper()
        self.lbl_back = t("← Назад")
        self.sm: ScreenManager = Builder.load_string(KV)
        Clock.schedule_interval(self._tick, 0.5)
        return self.sm

    def on_pause(self) -> bool:
        """Свернули приложение: опрос на паузу, звук выключаем."""
        if self.worker is not None:
            self.worker.pause()
        self._stop_beeps()
        return True

    def on_resume(self) -> None:
        if self.worker is not None:
            self.worker.resume()
        if self.sound_on:
            self._schedule_beep()

    def on_stop(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.stop()
            worker.join(timeout=2.0)       # дать отправить logout
        self.beeper.release()
        set_keep_screen_on(False)

    # ---------- тема, тексты, навигация ----------

    def apply_theme(self, name: str) -> None:
        from kivy.core.window import Window
        self.theme_name = name
        for key, value in THEMES[name].items():
            if hasattr(self, key):
                setattr(self, key, value)
        Window.clearcolor = tuple(self.c_bg)
        self._bands_shown = []          # подписи бэндов — заново, в цвете темы
        self.build_band_checkboxes()
        if self.state.last is not None:
            self._render(self.state.last)
        else:
            self.refresh_graph()

    def refresh_all_texts(self) -> None:
        self.lbl_back = t("← Назад")
        for name in ('connection', 'monitor', 'info', 'tools'):
            self.sm.get_screen(name).refresh_texts()
        self._render_link_state()
        if self.state.last is not None:
            self._render(self.state.last)

    def go(self, screen: str) -> None:
        self.sm.current = screen

    def _on_keyboard(self, window: Any, key: int, *args: Any) -> bool:
        """Системная «Назад» (Esc на десктопе).

        Hardware validation required: жест «Назад» Android 13+ на устройстве.
        """
        if key != 27:
            return False
        if any(isinstance(w, ModalView) for w in window.children):
            return False            # открытый диалог закроется сам
        current = self.sm.current
        if current in ('info', 'tools'):
            self.sm.current = 'monitor'
            return True
        if current == 'monitor':
            now = time.monotonic()
            if now - self._last_back < 2.0:
                self.disconnect()
            else:
                self._last_back = now
                self.toast(t("Нажмите «Назад» ещё раз, чтобы отключиться"))
            return True
        return False                # экран подключения — стандартный выход

    # ---------- подключение ----------

    def _conn_screen(self) -> ConnectionScreen:
        return self.sm.get_screen('connection')

    def discover(self, entered: str) -> None:
        scr = self._conn_screen()
        scr.set_status(t("Ищу роутер…"), [0.3, 0.6, 0.95, 1])
        candidates = ([entered] if is_valid_ip(entered) else []) + list(DISCOVERY_CANDIDATES)

        def done(found: Any) -> None:
            if found is None:
                scr.set_status(t("Роутер не найден. Проверьте подключение к его Wi-Fi/USB."),
                               [0.9, 0.3, 0.3, 1])
                return
            ip_input = scr.ids.get('ip_input')
            if ip_input is not None:
                ip_input.text = found.ip
            scr.set_status(t("Найден роутер {model} на {ip}").format(
                model=found.model or "Huawei", ip=found.ip), [0.2, 0.75, 0.45, 1])
        self._run_bg(lambda: discover_router(candidates), done, lambda e: done(None))

    def connect(self, ip: str, password: str) -> None:
        if self.worker is not None:
            return
        self.demo_modem = None
        self._conn_screen().set_status(t("Подключение..."), [0.9, 0.6, 0.2, 1])
        self._start_session(RouterSession(ip, password))

    def start_demo(self) -> None:
        if self.worker is not None:
            return
        self.demo_modem = DemoModem()
        self._start_session(RouterSession("demo", "", factory=demo_factory(self.demo_modem)))

    def _start_session(self, session: RouterSession) -> None:
        self.state.reset()
        self.session = session
        worker: SessionWorker | None = None

        def mine(fn: Callable[..., None]) -> Callable[..., None]:
            """Событие фонового потока → главный поток, только для текущей сессии."""
            def post(*args: Any) -> None:
                def call(_dt: float) -> None:
                    if worker is self.worker:
                        fn(*args)
                Clock.schedule_once(call)
            return post

        worker = SessionWorker(
            session, interval=lambda: self.interval,
            on_connected=mine(self._on_connected), on_snapshot=mine(self._on_snapshot),
            on_status=mine(self._on_status), on_fatal=mine(self._on_fatal))
        self.worker = worker
        self.link = 'connecting'
        worker.start()

    def disconnect(self) -> None:
        """Мгновенно для UI: выход из роутера выполняет фоновый поток."""
        if self.worker is not None:
            self.worker.stop()
        self.worker = None
        self.session = None
        self.demo_modem = None
        self.link = 'offline'
        self._busy = False
        self._stop_beeps()
        set_keep_screen_on(False)
        self._conn_screen().set_status("", [0.5, 0.5, 0.5, 1])
        self.sm.current = 'connection'

    def _on_connected(self, info: dict[str, Any]) -> None:
        self.link = 'online'
        session = self.session
        if session is not None:
            self._band_list = lockable_bands(session.supported_bands, self.state.observed_bands)
        self._conn_screen().set_status("", [0.5, 0.5, 0.5, 1])
        self.sm.current = 'monitor'
        set_keep_screen_on(True)
        self._render_link_state()
        self._show_last_event()         # не текст ошибки прошлой сессии
        if self.sound_on:
            self._schedule_beep()

    def _show_last_event(self) -> None:
        ev = self.sm.get_screen('monitor').ids.get('event_lbl')
        if ev is not None:
            ev.text = self._event_text(self.state.events[-1]) if self.state.events else ""

    def _on_status(self, status: str, delay: float | None, exc: BaseException | None) -> None:
        recovered = self.link == 'reconnecting' and status != 'reconnecting'
        self.link = 'reconnecting' if status == 'reconnecting' else 'online'
        self._render_link_state(humanize_error(exc) if exc is not None else "")
        if recovered:
            # Строка событий показывала ошибку связи — возвращаем последнее событие.
            self._show_last_event()

    def _on_fatal(self, exc: BaseException) -> None:
        self.disconnect()
        self._conn_screen().set_status(humanize_error(exc), [0.9, 0.3, 0.3, 1])

    @staticmethod
    def _event_text(change: Any) -> str:
        return t("{at} смена соты: {old} → {new}").format(
            at=change.at, old=change.old_label, new=change.new_label)

    def _render_link_state(self, detail: str = "") -> None:
        scr = self.sm.get_screen('monitor')
        lbl = scr.ids.get('status_lbl')
        if lbl is None:
            return
        sun = self.theme_name == 'sun'
        if self.link == 'reconnecting':
            lbl.text = t("Нет связи с роутером — переподключаюсь…")
            scr.status_color = hex_to_rgba('#e68033', sun)
            ev = scr.ids.get('event_lbl')
            if detail and ev is not None:
                ev.text = detail
        elif self.demo_modem is not None:
            lbl.text = t("Тестовый режим")
            scr.status_color = hex_to_rgba('#e68033', sun)

    # ---------- данные ----------

    def _on_snapshot(self, snap: Snapshot) -> None:
        change = self.state.ingest(snap, time.monotonic())
        scr = self.sm.get_screen('monitor')
        if change is not None:
            ev = scr.ids.get('event_lbl')
            if ev is not None:
                ev.text = self._event_text(change)
        if snap.has_nr and 'nr_sinr' not in scr.graph_values:
            scr.graph_values = LTE_PARAMS + NR_PARAMS
        if any(b not in self._band_list for b in snap.bands) and self.session is not None:
            self._band_list = lockable_bands(self.session.supported_bands,
                                             self.state.observed_bands)
        self._render(snap)

    def _tick(self, _dt: float) -> None:
        """Каждые 0.5 с: признак устаревших данных."""
        if self.link not in ('online', 'reconnecting') or self.state.last is None:
            return
        now = time.monotonic()
        if self.state.is_stale(now, self.interval):
            scr = self.sm.get_screen('monitor')
            lbl = scr.ids.get('status_lbl')
            if lbl is not None:
                lbl.text = t("⚠ Данные устарели — нет ответа {s:.0f} с").format(
                    s=self.state.age(now) or 0)
                scr.status_color = hex_to_rgba('#d63031', self.theme_name == 'sun')
            self.refresh_graph(stale=True)

    def _metric_card(self, card: Any, param: str) -> None:
        val = self.state.current(param)
        label, color, _ = evaluate_signal(param.replace('nr_', ''), val)
        card.m_name = PARAM_TITLES.get(param, param.upper())
        card.m_value = fmt_num(val)
        card.m_status = t(label)
        card.m_color = hex_to_rgba(color, self.theme_name == 'sun')
        peak, delta = self.state.peaks.get(param), self.state.delta_to_peak(param)
        card.m_peak = "" if peak is None or delta is None else \
            t("Пик: {v} (Δ {d})").format(v=fmt_num(peak), d=f"{delta:+g}")

    def _layout_params(self) -> list[str]:
        """Крупно — выбранная метрика, рядом RSRP (или SINR), мелко — остальные."""
        primary = self.state.trend_param
        secondary = 'rsrp' if primary not in ('rsrp', 'nr_rsrp') else 'sinr'
        rest = [p for p in LTE_PARAMS if p not in (primary, secondary)][:2]
        return [primary, secondary, *rest]

    def _render(self, snap: Snapshot) -> None:
        scr = self.sm.get_screen('monitor')
        ids = scr.ids
        if not ids:
            return
        sun = self.theme_name == 'sun'
        score, summary, color = calculate_overall_health(snap.rsrp, snap.sinr)
        health = t(summary).format(pct=score)
        if self.demo_modem is not None:
            health = t("ДЕМО · азимут {a}").format(
                a=demo_angle_hint(self.demo_modem)) + " · " + health
        ids.status_lbl.text = health
        scr.status_color = hex_to_rgba(color, sun)
        ids.tower_lbl.text = self._tower_summary(snap)
        cards = (ids.primary_box, ids.secondary_box, ids.small1_box, ids.small2_box)
        for card, param in zip(cards, self._layout_params(), strict=True):
            self._metric_card(card, param)

        glyph = TREND_GLYPHS.get(self.state.trend, "—")
        ids.dir_lbl.text = glyph
        ids.dir_lbl.color = hex_to_rgba(
            {"↑": '#00b894', "↓": '#d63031', "→": '#fdcb6e'}.get(glyph, 'gray'), sun)
        ids.dir_text_lbl.text = {
            "↑": t("Лучше — продолжайте"), "↓": t("Хуже — поверните обратно"),
            "→": t("Стабильно — фиксируйте"),
        }.get(glyph, t("Накапливаю данные..."))

        mimo = mimo_status(snap.cqi0, snap.cqi1)
        up = uplink_status(snap.pusch_dbm)
        tips = advice(rsrp=snap.rsrp, sinr=snap.sinr, rsrq=snap.rsrq,
                      jitter=self.state.jitter(), mimo=mimo, uplink=up)
        if snap.data_enabled is False:
            tips.insert(0, "Мобильные данные на роутере выключены.")
        ids.advice_lbl.text = "\n".join("• " + t(tip) for tip in tips[:2])
        scr.advice_color = hex_to_rgba('#e68033', sun)
        self.refresh_graph()
        if self.sm.current == 'info':
            self.refresh_info_screen()

    def _tower_summary(self, snap: Snapshot) -> str:
        parts = []
        if snap.bands:
            freq = f" · {snap.freq_mhz:g} {t('МГц')}" if snap.freq_mhz else ""
            parts.append(f"B{snap.bands[0]}{freq}")
            if len(snap.bands) > 1:
                parts.append("CA " + "+".join(f"B{b}" for b in snap.bands[1:]))
        if snap.pci is not None:
            parts.append(f"PCI {snap.pci}")
        if snap.rat:
            parts.append(t(RAT_LABELS.get(snap.rat, snap.rat)))
        return " · ".join(parts) if parts else "—"

    def set_graph_param(self, param: str) -> None:
        self.state.set_trend_param(param)
        if self.state.last is not None:
            self._render(self.state.last)
        else:
            self.refresh_graph()

    def refresh_graph(self, stale: bool = False) -> None:
        param = self.state.trend_param
        values = list(self.state.history.get(param, []))
        peak = self.state.peaks.get(param)
        graph = self.sm.get_screen('monitor').ids.get('signal_graph')
        if graph is not None:
            graph.set_data(values, param, peak, stale)
        if self._fs_graph is not None:
            self._fs_graph.set_data(values, param, peak, stale)

    def reset_peaks(self) -> None:
        self.state.reset_peaks()
        if self.state.last is not None:
            self._render(self.state.last)
        self.toast(t("Пики сброшены"))

    # ---------- звук-парктроник ----------

    def toggle_sound(self) -> None:
        if not self.beeper.available:
            self.toast(t("Звук недоступен на этом устройстве"))
            return
        self.sound_on = not self.sound_on
        if self.sound_on:
            self._schedule_beep()
        else:
            self._stop_beeps()

    def _schedule_beep(self) -> None:
        self._stop_beeps()
        delta = self.state.delta_to_peak(self.state.trend_param)
        self._beep_ev = Clock.schedule_once(self._beep_tick, beep_interval(delta))

    def _beep_tick(self, _dt: float) -> None:
        self._beep_ev = None
        if not self.sound_on or self.link == 'offline':
            return
        # Во время переподключения молчим, но цикл не прерываем — иначе звук
        # не вернулся бы после перезагрузки роутера или обрыва Wi-Fi.
        if self.link == 'online' and not self.state.is_stale(time.monotonic(), self.interval):
            self.beeper.beep(50)
        self._schedule_beep()

    def _stop_beeps(self) -> None:
        if self._beep_ev is not None:
            self._beep_ev.cancel()
            self._beep_ev = None

    # ---------- меню и диалоги ----------

    def open_menu(self) -> None:
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        box = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
        popup = Popup(title=t("Меню"), content=box, size_hint=(0.85, None), height=dp(380))
        sun = self.theme_name == 'sun'
        items = [
            (t("ℹ Информация"), lambda: self.go('info')),
            (t("⚙ Сеть"), lambda: self.go('tools')),
            (t("☀ Солнце: выкл") if sun else t("☀ Солнце: вкл"),
             lambda: self.apply_theme('dark' if sun else 'sun')),
            (t("⏏ Отключиться"), self.confirm_disconnect),
        ]
        for text, action in items:
            btn = Button(text=text, font_size=sp(17), size_hint_y=None, height=dp(56))
            btn.bind(on_release=lambda _b, a=action: (popup.dismiss(), a()))
            box.add_widget(btn)
        popup.open()

    def confirm(self, title: str, message: str, yes_text: str,
                on_yes: Callable[[], None]) -> None:
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(10))
        msg = Label(text=message, halign='center', valign='middle', font_size=sp(15))
        msg.bind(size=lambda i, v: setattr(i, 'text_size', v))
        content.add_widget(msg)
        btns = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(10))
        popup = Popup(title=title, content=content, size_hint=(0.92, 0.55))
        no = Button(text=t("← Назад"), font_size=sp(16))
        yes = Button(text=yes_text, font_size=sp(16), background_color=(0.8, 0.4, 0.15, 1))
        no.bind(on_release=lambda *a: popup.dismiss())
        yes.bind(on_release=lambda *a: (popup.dismiss(), on_yes()))
        btns.add_widget(no)
        btns.add_widget(yes)
        content.add_widget(btns)
        popup.open()

    def confirm_disconnect(self) -> None:
        self.confirm(t("Подтверждение"), t("Отключиться от роутера?"),
                     t("Отключиться"), self.disconnect)

    def show_popup(self, title: str, message: str) -> None:
        from kivy.uix.label import Label
        from kivy.uix.scrollview import ScrollView
        lbl = Label(text=message, halign='left', valign='top', size_hint_y=None,
                    font_size=sp(14))
        lbl.bind(width=lambda i, w: setattr(i, 'text_size', (w, None)),
                 texture_size=lambda i, ts: setattr(i, 'height', ts[1]))
        sv = ScrollView()
        sv.add_widget(lbl)
        Popup(title=title, content=sv, size_hint=(0.92, 0.8)).open()

    def toast(self, text: str, seconds: float = 2.0) -> None:
        from kivy.core.window import Window
        from kivy.graphics import Color, RoundedRectangle
        from kivy.uix.label import Label
        lbl = Label(text=text, font_size=sp(15), size_hint=(None, None), color=(1, 1, 1, 1))
        lbl.texture_update()
        lbl.size = (lbl.texture_size[0] + dp(24), lbl.texture_size[1] + dp(16))
        lbl.pos = ((Window.width - lbl.width) / 2, dp(80))
        with lbl.canvas.before:
            Color(0, 0, 0, 0.8)
            RoundedRectangle(pos=lbl.pos, size=lbl.size, radius=[dp(8)])
        Window.add_widget(lbl)
        Clock.schedule_once(lambda dt: Window.remove_widget(lbl), seconds)

    def open_fullscreen_graph(self) -> None:
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        inner = BoxLayout(orientation='vertical', spacing=dp(6), padding=dp(8))
        graph = SignalGraph()
        close = Button(text=t("← Назад"), size_hint_y=None, height=dp(48))
        inner.add_widget(graph)
        inner.add_widget(close)
        self._fs_graph = graph
        popup = Popup(title='', separator_height=0, content=RotatedBox(inner),
                      size_hint=(0.98, 0.96))
        popup.bind(on_dismiss=lambda *a: setattr(self, '_fs_graph', None))
        close.bind(on_release=lambda *a: popup.dismiss())
        popup.open()
        self.refresh_graph()

    # ---------- экран «Информация» ----------

    def refresh_info_screen(self) -> None:
        ids = self.sm.get_screen('info').ids
        if not ids:
            return
        snap = self.state.last or Snapshot()
        nd = t("Нет данных")
        mhz = t('МГц')

        def row(name: str, value: Any) -> str:
            # Строки от роутера — без разметки Kivy: «[» и «]» в них не теги.
            text = escape_markup(str(value)) if value not in (None, '', '-') else nd
            return f"[b]{t(name)}:[/b] {text}"

        ca = {True: t("Активна"), False: t("Нет"), None: None}[snap.ca]
        nr = snap.nr_summary() if snap.has_nr else None
        dl, ul = snap.dl_bandwidth, snap.ul_bandwidth
        tower = [
            row('Оператор (PLMN)', f"{snap.plmn} ({snap.operator or t('Неизвестный оператор')})"
                if snap.plmn else None),
            row('Технология', t(RAT_LABELS.get(snap.rat, snap.rat)) if snap.rat else None),
            row('Рабочий Band (LTE)', snap.band_label.replace('МГц', mhz)),
            row('Частота DL', f"{snap.freq_mhz:g} {mhz}" if snap.freq_mhz else None),
            row('EARFCN (канал DL)', snap.earfcn_raw),
            row('Агрегация (CA)', ca),
            row('Ширина канала', f"DL {dl} / UL {ul}" if ul else dl),
            row('Сектор антенны (PCI)', fmt_num(snap.pci)),
            row('eNodeB (Вышка)', fmt_num(snap.enodeb) if snap.enodeb is not None else
                (f"NCI {snap.nci}" if snap.nci else None)),
            row('Cell (Локальный сектор)', fmt_num(snap.sector)),
            row('TAC (зона)', snap.tac),
            row('5G NR', nr),
        ]
        current = snap.cell
        cells = [
            f"{'▶ ' if c.key == current else ''}{c.band_label.replace('МГц', mhz)}"
            f" · PCI {fmt_num(c.key.pci)} · SINR {fmt_num(c.best_sinr)}"
            f" · RSRP {fmt_num(c.best_rsrp)}"
            for c in self.state.top_cells()]
        info = self.session.device_info if self.session is not None else {}
        sim = [row(name, info.get(key)) for key, name in (
            ('Imei', 'IMEI (роутер)'), ('Imsi', 'IMSI (SIM)'), ('Iccid', 'ICCID (SIM-карта)'),
            ('Msisdn', 'Номер телефона'), ('SerialNumber', 'Серийный номер'),
            ('DeviceName', 'Модель'), ('SoftwareVersion', 'Прошивка'))]
        mimo = mimo_status(snap.cqi0, snap.cqi1)
        up = uplink_status(snap.pusch_dbm)
        mods = [f"{d} {m}" for d, m in (("DL", format_modulation(snap.dl_mcs)),
                                        ("UL", format_modulation(snap.ul_mcs, uplink=True))) if m]
        cqi = None
        if snap.cqi0 is not None:
            cqi = f"{snap.cqi0}" + (f" / {snap.cqi1}" if snap.cqi1 is not None else "") + \
                (f" — {t(MIMO_LABELS[mimo])}" if mimo else "")
        tx = (snap.txpower + (f" — {t(UPLINK_LABELS[up])}" if up else "")) or None
        st = self.state

        def minmax(p: str) -> str | None:
            lo, hi = st.session_min.get(p), st.session_max.get(p)
            return None if lo is None else f"{fmt_num(lo)} / {fmt_num(hi)} {unit_of(p)}"

        month = None
        if snap.month_dl is not None or snap.month_ul is not None:
            month = f"{format_bytes_mb(snap.month_dl or 0)} / {format_bytes_mb(snap.month_ul or 0)}"
        status = [
            row('Время сессии', str(datetime.timedelta(seconds=snap.connect_time))
                if snap.connect_time else None),
            row('Температура чипа', snap.temperature),
            row('Мобильные данные', {True: t("включены"), False: t("ВЫКЛЮЧЕНЫ"),
                                     None: None}[snap.data_enabled]),
            row('Скорость (Download)', format_rate_mbps(snap.dl_rate)),
            row('Скорость (Upload)', format_rate_mbps(snap.ul_rate)),
            row('Скачано за сессию', format_bytes_mb(snap.total_dl)),
            row('Отдано за сессию', format_bytes_mb(snap.total_ul)),
            row('Трафик за месяц (↓/↑)', month),
            row('Модуляция DL / UL', " / ".join(mods) if mods else None),
            row('Режим MIMO', format_mimo(snap.transmode) if snap.transmode else None),
            row('CQI (потоки)', cqi),
            row('Мощность передатчика', tx),
            row('RRC', snap.rrc),
            row('RSRP мин / макс', minmax('rsrp')),
            row('SINR мин / макс', minmax('sinr')),
        ]
        for key, lines in (('tower_block', tower), ('cells_block', cells or [nd]),
                           ('sim_block', sim), ('status_block', status)):
            ids[key].text = "\n".join(lines)

    # ---------- экран «Сеть» ----------

    def _tools(self) -> ToolsScreen:
        return self.sm.get_screen('tools')

    def _net_msg(self, text: str, color: str) -> None:
        tools = self._tools()
        tools.net_msg = text
        tools.net_msg_color = hex_to_rgba(color, self.theme_name == 'sun')

    def build_band_checkboxes(self) -> None:
        grid = self._tools().ids.get('bands_grid')
        if grid is None or self._bands_shown == self._band_list:
            return
        from kivy.uix.checkbox import CheckBox
        from kivy.uix.label import Label
        checked = {b for b, cb in self.band_vars.items() if cb.active}
        grid.clear_widgets()
        self.band_vars = {}
        row_h = dp(44)
        for band in self._band_list:
            cb = CheckBox(size_hint=(None, None), width=dp(40), height=row_h,
                          active=band in checked)
            lbl = Label(text=lte_band_label(band, t("МГц")), color=self.c_text,
                        halign='left', valign='middle', size_hint_y=None, height=row_h)
            lbl.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
            grid.add_widget(cb)
            grid.add_widget(lbl)
            self.band_vars[band] = cb
        self._bands_shown = list(self._band_list)

    def _run_bg(self, work: Callable[[], Any], done: Callable[[Any], None],
                fail: Callable[[BaseException], None]) -> None:
        """Выполнить work в фоне, результат/ошибку — в главном потоке."""
        def task() -> None:
            try:
                result = work()
            except Exception as exc:
                logger.exception("Background task failed")
                Clock.schedule_once(lambda dt, err=exc: fail(err))
            else:
                Clock.schedule_once(lambda dt: done(result))
        threading.Thread(target=task, daemon=True).start()

    def _net_action(self, work: Callable[[], Any], done: Callable[[Any], None],
                    busy_text: str) -> None:
        """Команда роутеру: повторное нажатие до ответа игнорируется."""
        if self.session is None or self.link != 'online':
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return
        if self._busy:
            return
        self._busy = True
        self._net_msg(busy_text, '#3399ff')
        session = self.session

        def finish(result: Any) -> None:
            if session is not self.session:
                return          # ответ прошлой сессии: её состояние уже сброшено
            self._busy = False
            done(result)

        def failed(exc: BaseException) -> None:
            if session is not self.session:
                return
            self._busy = False
            self._net_msg(t("Команда не выполнена: {err}").format(
                err=humanize_error(exc)), '#d63031')
        self._run_bg(work, finish, failed)

    def load_router_config(self) -> None:
        """Читает текущий Band Lock и антенну (только чтение)."""
        session = self.session
        if session is None:
            return
        unread = t("Сейчас на модеме: не прочитано")
        # Пока не прочитано — не показываем состояние прошлой сессии.
        self._tools().lock_state = unread

        def done(cfg: Any) -> None:
            if session is not self.session:
                return
            tools = self._tools()
            if cfg.locked is None:
                tools.lock_state = t("Сейчас на модеме: не прочитано")
            elif not cfg.locked:
                tools.lock_state = t("Сейчас на модеме: AUTO (все бэнды)")
                for cb in self.band_vars.values():
                    cb.active = False
            else:
                tools.lock_state = t("Сейчас на модеме: {bands}").format(
                    bands=", ".join(f"B{b}" for b in cfg.locked))
                extra = [b for b in cfg.locked if b not in self._band_list]
                if extra:
                    self._band_list += extra
                    self.build_band_checkboxes()
                for b, cb in self.band_vars.items():
                    cb.active = b in cfg.locked
            if cfg.antenna is not None:
                for label, code in ANTENNA_MODES.items():
                    if code == cfg.antenna:
                        tools.antenna_text = t(label)
        def failed(_exc: BaseException) -> None:
            if session is self.session:
                self._tools().lock_state = unread
        self._run_bg(session.read_config, done, failed)

    def mark_current_bands(self) -> None:
        snap = self.state.last
        if snap is None or not snap.bands:
            self._net_msg(t("Текущий бэнд ещё не определён."), '#d63031')
            return
        missing = [b for b in snap.bands if b not in self._band_list]
        if missing:
            self._band_list += missing
        self.build_band_checkboxes()
        for b, cb in self.band_vars.items():
            cb.active = b in snap.bands
        self._net_msg(t("Отмечены текущие бэнды: {bands}").format(
            bands=", ".join(f"B{b}" for b in snap.bands)), '#3399ff')

    def apply_bands(self) -> None:
        session = self.session
        selected = [b for b, cb in self.band_vars.items() if cb.active]
        if session is None or self.link != 'online':
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return
        if not selected:
            self._net_msg(t("Выберите хотя бы один диапазон!"), '#d63031')
            return
        names = ", ".join(f"B{b}" for b in selected)
        lines = [t("Зафиксировать бэнды: {bands}?").format(bands=names)]
        lines += lock_warnings(selected, self.state.last)
        if session.locks_lte_only:
            lines.append(t("Режим сети будет «только 4G»."))
        elif not session.caps_known:     # режимы уточнятся перед записью
            lines.append(t("Если у модема нет 5G, режим сети будет «только 4G»."))

        def go() -> None:
            self._net_action(lambda: session.apply_plan(session.plan_band_lock(selected)),
                             lambda ok: self._after_write(ok, t(
                                 "Band Lock применён: {bands}.").format(bands=names)),
                             t("Применяю Band Lock…"))
        self.confirm(t("Подтверждение"), "\n\n".join(lines), t("Применить"), go)

    def reset_bands(self) -> None:
        session = self.session
        if session is None:
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return
        self._net_action(lambda: session.apply_plan(session.plan_all_bands()),
                         lambda ok: self._after_write(ok, t("Включены все бэнды (AUTO).")),
                         t("Включаю все бэнды…"))

    def restore_bands(self) -> None:
        session = self.session
        if session is None:
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return
        plan = session.plan_restore_original()
        if plan is None:
            self._net_msg(t("Исходные настройки не прочитаны — роутер не отдал net-mode."),
                          '#d63031')
            return
        self._net_action(lambda: session.apply_plan(plan),
                         lambda ok: self._after_write(ok, t(
                             "Восстановлены настройки, прочитанные при подключении.")),
                         t("Восстанавливаю настройки…"))

    def _after_write(self, verified: bool, success_text: str) -> None:
        if verified:
            self._net_msg(success_text + " " + t(
                "Модем перерегистрируется в сети (до ~30 с)."), '#00b894')
        else:
            self._net_msg(t("Команда отправлена, но роутер не подтвердил запись — "
                            "проверьте строку «Сейчас на модеме»."), '#e68033')
        self.load_router_config()

    def apply_antenna(self, label: str) -> None:
        session = self.session
        rev = {t(k): k for k in ANTENNA_MODES}
        code = parse_antenna_value(rev.get(label, label))
        if session is None:
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return
        if code is None:
            self._net_msg(t("Неизвестный режим антенны."), '#d63031')
            return

        def work() -> int | None:
            session.set_antenna(code)
            return session.read_antenna()

        def done(read_back: int | None) -> None:
            if read_back in (code, None):
                self._net_msg(t("Тип антенны изменён: {mode}").format(mode=label), '#00b894')
            else:
                self._net_msg(t("Роутер принял команду, но сообщает другой режим антенны."),
                              '#e68033')
        self._net_action(work, done, t("Переключаю антенну…"))

    def confirm_reattach(self) -> None:
        session = self.session
        if session is None:
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return

        def done(ok: bool) -> None:
            if ok:
                self._net_msg(t("Связь переподключена."), '#00b894')
            else:
                self._net_msg(t("Не удалось включить мобильные данные — включите их в "
                                "веб-интерфейсе роутера."), '#d63031')
        self.confirm(t("Подтверждение"), t(
            "Переподключить мобильную связь?\n\nИнтернет пропадёт примерно "
            "на 5–10 секунд."), t("Переподключить"),
            lambda: self._net_action(session.reattach, done, t("Переподключаю связь…")))

    def confirm_reboot(self) -> None:
        session, worker = self.session, self.worker
        if session is None or worker is None:
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return

        def done(_: Any) -> None:
            self._net_msg(t("Роутер перезагружается — переподключусь автоматически."),
                          '#3399ff')
        self.confirm(t("Подтверждение"), t(
            "Перезагрузить роутер?\n\nСоединение с интернетом прервётся "
            "на 1–2 минуты. Программа переподключится автоматически."),
            t("Перезагрузить роутер"),
            lambda: self._net_action(session.reboot, done, t("Отправляю команду перезагрузки…")))

    def copy_diagnostics(self) -> None:
        """Диагностика (JSON) в буфер обмена: удобно вставить в issue/мессенджер.

        Hardware validation required: вставка из буфера на Android 12+.
        """
        session = self.session
        if session is None:
            self._net_msg(t("Сначала подключитесь к роутеру."), '#d63031')
            return

        def work() -> str:
            dumps, errors = session.collect_diagnostics()
            report = build_diagnostics(
                app_version=__version__, library_version=library_version(),
                platform=f"{platform} / Python {py_platform.python_version()}",
                dumps=dumps, errors=errors)
            return diagnostics_json(report)

        def done(text: str) -> None:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(text)
            self._net_msg(t("Диагностика скопирована в буфер обмена. Личные номера "
                            "замаскированы."), '#00b894')
        self._net_action(work, done, t("Собираю диагностику…"))

    def whitelist_check(self) -> None:
        tools = self._tools()
        tools.wl_verdict = t("Проверка…")
        tools.wl_color = hex_to_rgba('#e68033', self.theme_name == 'sun')
        tools.wl_detail = t("Проверка занимает несколько секунд.")

        def done(report: Any) -> None:
            tools.wl_verdict = report.title
            tools.wl_color = hex_to_rgba(report.color, self.theme_name == 'sun')
            lines = [report.detail, ""]
            for title, results in ((t("В белых списках:"), report.white),
                                   (t("Нейтральные:"), report.neutral)):
                lines.append(title)
                lines += [f"{'✔' if r.ok else '✘'} {r.host} — {r.detail}" for r in results]
            tools.wl_detail = "\n".join(lines)

        def failed(exc: BaseException) -> None:
            tools.wl_verdict = t("Ошибка")
            tools.wl_detail = humanize_error(exc)
        self._run_bg(run_whitelist_check, done, failed)


def main() -> None:
    Hua4GMonApp().run()


if __name__ == "__main__":
    main()
