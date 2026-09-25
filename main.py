"""
Hua4GMon — Huawei 4G/5G Monitor для Windows (portable, один файл .exe).

Назначение:
    Утилита для монтажников и владельцев роутеров Huawei
    (B315, B525, B535, B628, B636, B818, E3372 и др.): мониторинг
    качества LTE/5G-сигнала и точная настройка направленной антенны.

Особенности:
    * Portable: ничего не сохраняется на диск (ни настроек, ни паролей).
    * Вся логика — в пакете core/ (общая с Android-версией).
    * Опрос и переподключение — в фоновом потоке core.SessionWorker;
      события доставляются в Tk через потокобезопасную очередь.
    * High DPI (100–200 %), корректная работа на нескольких мониторах.
    * Тестовый режим без роутера (симулятор модема).

Запуск:
    python main.py
    python main.py --ip 192.168.1.1 --password admin
    python main.py --demo
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import logging
import platform
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable
from tkinter import filedialog, messagebox, ttk
from typing import Any

from core import (
    ANTENNA_MODES,
    CONTROL_HOSTS_NEUTRAL,
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
    WHITELIST_HOSTS_RU,
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
    default_csv_name,
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
    library_self_test,
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
    write_session_csv,
)

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False


APP_NAME = "Hua4GMon"
LTE_PARAMS = ('sinr', 'rsrp', 'rsrq', 'rssi')
NR_PARAMS = ('nr_sinr', 'nr_rsrp')
PARAM_TITLES = {'rsrp': 'RSRP', 'rssi': 'RSSI', 'sinr': 'SINR', 'rsrq': 'RSRQ',
                'nr_rsrp': 'NR RSRP', 'nr_sinr': 'NR SINR', 'nr_rsrq': 'NR RSRQ'}
TREND_GLYPHS = {TREND_UP: ("↑", "#00b894"), TREND_DOWN: ("↓", "#d63031"),
                TREND_FLAT: ("→", "#fdcb6e")}

logger = logging.getLogger(APP_NAME)


def unit_of(param: str) -> str:
    return "dBm" if param.endswith(('rsrp', 'rssi')) else "dB"


def fmt_num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


# =========================================================
# ИНФРАСТРУКТУРА UI
# =========================================================

def enable_dpi_awareness() -> None:
    """Чёткий текст на 125–200 %: вызывать ДО создания tk.Tk().

    System-DPI-aware (1): Tk 8.6 не умеет перемасштабировать окно при
    переносе между мониторами с разным DPI, поэтому per-monitor режим
    дал бы «прыгающие» размеры. На других мониторах Windows масштабирует
    окно сама.
    Hardware validation required: масштаб 125/150/200 % и два монитора.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        logger.debug("DPI awareness unavailable", exc_info=True)


class UiDispatcher:
    """Потокобезопасная доставка вызовов в главный поток Tk.

    Фоновые потоки кладут вызовы в очередь, главный поток забирает их
    по таймеру. Прямой root.after() из чужого потока Tkinter не
    гарантирует.
    """

    def __init__(self, root: tk.Misc, period_ms: int = 40) -> None:
        self._root = root
        self._queue: queue.SimpleQueue[tuple[Callable[..., Any], tuple]] = queue.SimpleQueue()
        self._period = period_ms
        self._closed = False
        self._after_id = root.after(period_ms, self._drain)

    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        if not self._closed:
            self._queue.put((fn, args))

    def _drain(self) -> None:
        while True:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                logger.exception("UI callback failed")
        if not self._closed:
            self._after_id = self._root.after(self._period, self._drain)

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(tk.TclError):
            self._root.after_cancel(self._after_id)


class Beeper:
    """Один поток для звуковых сигналов: без лавины потоков и очереди звуков."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[int, int]] = queue.Queue(maxsize=1)
        threading.Thread(target=self._run, daemon=True, name="beeper").start()

    def beep(self, freq: int, ms: int) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait((freq, ms))

    def _run(self) -> None:
        while True:
            freq, ms = self._queue.get()
            with contextlib.suppress(RuntimeError):
                winsound.Beep(freq, ms)


def beep_frequency(param: str, delta: float) -> int:
    """Чем ближе к пику — тем выше тон (как парктроник)."""
    per_db = 70 if param.endswith(('rsrp', 'rssi')) else 110
    return max(300, min(2500, int(2500 - abs(delta) * per_db)))


# =========================================================
# ГРАФИК НА tk.Canvas
# =========================================================

class CanvasGraph(tk.Canvas):
    """Лёгкий график без matplotlib: автомасштаб, линия пика, устаревание."""

    def __init__(self, parent: tk.Misc, scale: float, history: int, **kw: Any) -> None:
        super().__init__(parent, bg='white', highlightthickness=1,
                         highlightbackground='#cccccc', **kw)
        self.s = scale
        self.history = history
        self.param = 'sinr'
        self.values: list[float] = []
        self.peak: float | None = None
        self.stale = False
        self.bind("<Configure>", lambda e: self._redraw())

    def set_param(self, param: str) -> None:
        self.param = param
        self.values = []
        self.peak = None
        self._redraw()

    def update_series(self, values: list[float], peak: float | None, stale: bool) -> None:
        self.values = values[-self.history:]
        self.peak = peak
        self.stale = stale
        self._redraw()

    def _y_range(self) -> tuple[float, float]:
        lo_lim, hi_lim = PARAM_RANGES.get(self.param.replace('nr_', ''), (-140, 40))
        pts = list(self.values) + ([self.peak] if self.peak is not None else [])
        if not pts:
            return float(lo_lim), float(hi_lim)
        lo, hi = min(pts), max(pts)
        span = GRAPH_MIN_SPAN.get(self.param.replace('nr_', ''), 10.0)
        if hi - lo < span:
            mid = (hi + lo) / 2
            lo, hi = mid - span / 2, mid + span / 2
        lo = max(lo_lim, 5 * ((lo - 1) // 5))
        hi = min(hi_lim, 5 * -((-(hi + 1)) // 5))
        return float(lo), float(max(hi, lo + 1))

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        s = self.s
        pl, pr, pt, pb = int(48 * s), int(12 * s), int(20 * s), int(22 * s)
        plot_w, plot_h = w - pl - pr, h - pt - pb
        if plot_w <= 20 or plot_h <= 20:
            return
        title = PARAM_TITLES.get(self.param, self.param.upper())
        self.create_text(pl, int(3 * s), anchor='nw', text=f"{title} ({unit_of(self.param)})",
                         font=("Segoe UI", 9, "bold"), fill='#333')
        y_min, y_max = self._y_range()
        rng = y_max - y_min
        for i in range(5):
            y = pt + plot_h * i / 4
            v = y_max - rng * i / 4
            self.create_line(pl, y, w - pr, y, fill='#ececec')
            self.create_text(pl - 4, y, anchor='e', text=f"{v:.0f}",
                             font=("Segoe UI", 8), fill='#666')
        self.create_line(pl, h - pb, w - pr, h - pb, fill='#888')
        self.create_text((pl + w - pr) / 2, h - 2, anchor='s',
                         text=t("последние {n} точек").format(n=self.history),
                         font=("Segoe UI", 8), fill='#888')

        def y_of(v: float) -> float:
            return (h - pb) - plot_h * (max(y_min, min(y_max, v)) - y_min) / rng

        if self.peak is not None:
            py = y_of(self.peak)
            self.create_line(pl, py, w - pr, py, fill='#00b894', dash=(4, 3))
            self.create_text(pl + 4, py - 2, anchor='sw', font=("Segoe UI", 8),
                             fill='#00b894', text=t("пик {v}").format(v=fmt_num(self.peak)))
        if not self.values:
            return
        span = max(self.history - 1, 1)
        pts: list[float] = []
        for i, v in enumerate(self.values):
            pts.extend([pl + plot_w * i / span, y_of(v)])
        color = '#aaaaaa' if self.stale else '#0078D7'
        if len(pts) >= 4:
            self.create_line(*pts, fill=color, width=max(2, int(2 * s)))
        lx, ly = pts[-2], pts[-1]
        r = 3 * s
        self.create_oval(lx - r, ly - r, lx + r, ly + r, fill=color, outline='')
        self.create_text(w - pr - 4, pt, anchor='ne', fill=color,
                         font=("Segoe UI", 9, "bold"),
                         text=f"{fmt_num(self.values[-1])} {unit_of(self.param)}")
        if self.stale:
            self.create_text((pl + w - pr) / 2, pt + plot_h / 2, fill='#d63031',
                             font=("Segoe UI", 12, "bold"), text=t("нет свежих данных"))


# =========================================================
# ОСНОВНОЙ КЛАСС
# =========================================================

class Hua4GMon:
    def __init__(self, root: tk.Tk, default_ip: str = "192.168.8.1",
                 default_password: str = "", demo: bool = False) -> None:
        self.root = root
        self.s = max(1.0, root.winfo_fpixels('1i') / 96.0)
        self.root.title(f"{APP_NAME} v{__version__}")
        self._fit_window(940, 780, 820, 660)
        self.dispatch = UiDispatcher(root)
        self.beeper = Beeper() if HAS_WINSOUND else None

        self.state = SignalState(trend_param='sinr')
        self.session: RouterSession | None = None
        self.worker: SessionWorker | None = None
        self.demo_modem: DemoModem | None = None
        self.link = 'offline'           # offline | connecting | online | reconnecting
        self._interval_seconds = 1.0
        self._busy = False
        self.roof_win: tk.Toplevel | None = None
        self.band_vars: dict[int, tk.BooleanVar] = {}
        self._band_list: list[int] = list(REGION_BANDS)
        self._graph_params: list[str] = list(LTE_PARAMS)
        self._wrap_labels: list[tuple[tk.Widget, int]] = []

        self.default_ip = default_ip
        self.default_password = default_password
        self._saved: dict[str, Any] = {'ontop': False, 'sound': False, 'graph': 'sinr'}

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.bind("<Configure>", self._on_root_resize, add='+')
        self.root.bind_all("<F11>", lambda e: self.toggle_roof_mode())
        self.root.bind_all("<Control-r>", lambda e: self.reset_peaks())
        self.root.bind_all("<Control-m>", lambda e: self._toggle_sound())
        self.setup_ui()
        self._tick_id = self.root.after(500, self._tick)

        if demo:
            self.root.after(200, self.start_demo)
        elif default_password:
            self.root.after(200, self.start_connect)

    # ---------- размеры / DPI ----------

    def px(self, n: float) -> int:
        return int(round(n * self.s))

    def _fit_window(self, w: int, h: int, min_w: int, min_h: int) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(self.px(w), int(sw * 0.95)), min(self.px(h), int(sh * 0.9))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(min(self.px(min_w), w), min(self.px(min_h), h))

    def _wrap(self, widget: tk.Widget, margin: int = 60) -> tk.Widget:
        self._wrap_labels.append((widget, margin))
        return widget

    def _on_root_resize(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self._apply_wrap(event.width)

    def _apply_wrap(self, width: int) -> None:
        """Перенос длинных подписей по ширине окна (любой масштаб DPI)."""
        width = max(200, width)
        for widget, margin in list(self._wrap_labels):
            with contextlib.suppress(tk.TclError):
                widget.configure(wraplength=width - self.px(margin))

    # =====================================================
    # UI BUILD
    # =====================================================

    def setup_ui(self) -> None:
        self._wrap_labels = []
        style = ttk.Style()
        with contextlib.suppress(tk.TclError):
            style.theme_use('clam')
        style.configure("Treeview", rowheight=self.px(22))

        self.top_bar = ttk.Frame(self.root)
        self.top_bar.pack(fill=tk.X, padx=5, pady=2)
        self.status_label = ttk.Label(self.top_bar, text=t("Отключено"), foreground='red',
                                      font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side=tk.LEFT, padx=5)

        self._lang_code_by_name = {name: code for code, name in LANGUAGES.items()}
        self.lang_var = tk.StringVar(value=LANGUAGES.get(current_language(), "Русский"))
        lang_cb = ttk.Combobox(self.top_bar, textvariable=self.lang_var,
                               values=list(LANGUAGES.values()), state='readonly', width=10)
        lang_cb.pack(side=tk.RIGHT, padx=5)
        lang_cb.bind("<<ComboboxSelected>>", self._on_language_change)
        ttk.Label(self.top_bar, text=t("Язык:")).pack(side=tk.RIGHT)
        self.ontop_var = tk.BooleanVar(value=self._saved['ontop'])
        ttk.Checkbutton(self.top_bar, text=t("Поверх окон"), variable=self.ontop_var,
                        command=self.toggle_on_top).pack(side=tk.RIGHT, padx=5)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.tab_settings = ttk.Frame(self.notebook)
        self.tab_monitor = ttk.Frame(self.notebook)
        self.tab_network = ttk.Frame(self.notebook)
        self.tab_tower = ttk.Frame(self.notebook)
        self.tab_status = ttk.Frame(self.notebook)
        self.tab_whitelist = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_settings, text=t("⚙️ Подключение"))
        self.notebook.add(self.tab_monitor, text=t("📈 Монитор"))
        self.notebook.add(self.tab_network, text=t("🎛️ Сеть"))
        self.notebook.add(self.tab_tower, text=t("🗼 Вышка"))
        self.notebook.add(self.tab_status, text=t("📊 Состояние"))
        self.notebook.add(self.tab_whitelist, text=t("🛡 Белые списки (РФ)"))

        self.build_settings_tab()
        self.build_monitor_tab()
        self.build_network_tab()
        self.build_tower_tab()
        self.build_status_tab()
        self.build_whitelist_tab()
        self.root.after_idle(lambda: self._apply_wrap(self.root.winfo_width()))

    # ---------- смена языка ----------

    def _on_language_change(self, _event: Any = None) -> None:
        code = self._lang_code_by_name.get(self.lang_var.get())
        if not code or code == current_language():
            return
        set_language(code)
        self.rebuild_ui()

    def rebuild_ui(self) -> None:
        """Пересоздаёт виджеты на новом языке. Данные живут в self.state."""
        rev_antenna = {t(k): k for k in ANTENNA_MODES}
        snap = {
            'ip': self.ip_entry.get(), 'pw': self.password_entry.get(),
            'interval': self.update_interval.get(), 'reconnect': self.reconnect_var.get(),
            'antenna': rev_antenna.get(self.antenna_var.get()),
            'bands': {b: v.get() for b, v in self.band_vars.items()},
            'tab': self.notebook.index(self.notebook.select()),
        }
        self._saved.update(ontop=self.ontop_var.get(), sound=self.sound_var.get(),
                           graph=self.graph_param.get())
        self.notebook.destroy()
        self.top_bar.destroy()
        self.setup_ui()
        self.ip_entry.delete(0, tk.END)
        self.ip_entry.insert(0, snap['ip'])
        self.password_entry.delete(0, tk.END)
        self.password_entry.insert(0, snap['pw'])
        self.update_interval.set(snap['interval'])
        self.reconnect_var.set(snap['reconnect'])
        if snap['antenna']:
            self.antenna_var.set(t(snap['antenna']))
        for b, val in snap['bands'].items():
            if b in self.band_vars:
                self.band_vars[b].set(val)
        with contextlib.suppress(tk.TclError):
            self.notebook.select(snap['tab'])
        self.toggle_on_top()
        self._render_link_state()
        if self.session is not None and self.session.device_info:
            self._fill_device(self.session.device_info)
        if self.state.last is not None:
            self._render(self.state.last)

    # ---------- вкладка «Подключение» ----------

    def build_settings_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_settings, text=t("Параметры роутера"), padding=10)
        frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(frame, text=t("IP адрес:")).grid(row=0, column=0, sticky='e', padx=5, pady=5)
        self.ip_entry = ttk.Entry(frame, width=25)
        self.ip_entry.insert(0, self.default_ip)
        self.ip_entry.grid(row=0, column=1, sticky='w', padx=5)
        self.ip_entry.bind("<Return>", lambda e: self.password_entry.focus())
        self.find_button = ttk.Button(frame, text=t("🔎 Найти роутер"),
                                      command=self.start_discovery)
        self.find_button.grid(row=0, column=2, sticky='w', padx=5)

        ttk.Label(frame, text=t("Пароль:")).grid(row=1, column=0, sticky='e', padx=5, pady=5)
        self.password_entry = ttk.Entry(frame, show="*", width=25)
        if self.default_password:
            self.password_entry.insert(0, self.default_password)
        self.password_entry.grid(row=1, column=1, sticky='w', padx=5)
        self.password_entry.bind("<Return>", lambda e: self.start_connect())

        ttk.Label(frame, text=t("Опрос (сек):")).grid(row=2, column=0, sticky='e', padx=5, pady=5)
        self.update_interval = tk.StringVar(value='1')
        self.update_interval.trace_add('write', lambda *a: self._sync_interval())
        ttk.Combobox(frame, textvariable=self.update_interval, values=['0.5', '1', '2', '5'],
                     state='readonly', width=5).grid(row=2, column=1, sticky='w', padx=5)

        self.reconnect_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text=t("Авто-переподключение при обрыве"),
                        variable=self.reconnect_var,
                        command=self._sync_reconnect).grid(
            row=3, column=0, columnspan=3, sticky='w', padx=5, pady=5)

        btn_frame = ttk.Frame(self.tab_settings)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        self.connect_button = ttk.Button(btn_frame, text=t("🚀 Подключиться"),
                                         command=self.start_connect)
        self.connect_button.pack(side=tk.LEFT, padx=5)
        self.demo_button = ttk.Button(btn_frame, text=t("🧪 Тестовый режим"),
                                      command=self.start_demo)
        self.demo_button.pack(side=tk.LEFT, padx=5)

        self.conn_msg = self._wrap(tk.Label(self.tab_settings, text="", fg='#d63031',
                                            font=("Segoe UI", 10, "bold"), justify='left'))
        self.conn_msg.pack(fill=tk.X, padx=15, pady=(2, 6), anchor='w')

        info = ttk.LabelFrame(self.tab_settings, text=t("Подключение и частые ошибки"), padding=10)
        info.pack(fill=tk.X, padx=10, pady=5)
        self._wrap(ttk.Label(info, justify="left", text=t(
            "IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 или "
            "192.168.3.1) — кнопка «Найти роутер» проверит их сама. Логин: "
            "admin, пароль — на наклейке роутера.\n\n"
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
            "• Нет ответа — проверьте, что компьютер подключён к Wi-Fi или "
            "USB именно этого роутера и IP введён верно.\n\n"
            "Тестовый режим показывает работу программы без роутера.")), 80).pack(anchor='w')

    # ---------- вкладка «Монитор» ----------

    def build_monitor_tab(self) -> None:
        self.health_frame = ttk.LabelFrame(self.tab_monitor, text=t("Общее качество связи"),
                                           padding=8)
        self.health_frame.pack(fill=tk.X, padx=10, pady=4)
        self.health_progress = ttk.Progressbar(self.health_frame, orient="horizontal",
                                               mode="determinate")
        self.health_progress.pack(fill=tk.X, side=tk.TOP, pady=4)
        self.health_text_lbl = tk.Label(self.health_frame, text=t("Подключитесь к роутеру"),
                                        font=("Segoe UI", 12, "bold"), fg="gray")
        self.health_text_lbl.pack(side=tk.TOP, pady=1)
        self.tower_line = self._wrap(tk.Label(self.health_frame, text="—", fg='#333',
                                              font=("Segoe UI", 11, "bold")), 80)
        self.tower_line.pack(side=tk.TOP)
        self.event_line = self._wrap(tk.Label(self.health_frame, text="", fg='#0078D7',
                                              font=("Segoe UI", 9)), 80)
        self.event_line.pack(side=tk.TOP)

        self.digits_frame = ttk.Frame(self.tab_monitor)
        self.digits_frame.pack(fill=tk.X, padx=10, pady=4)
        self.lbl_vars: dict[str, dict[str, tk.Label]] = {}
        for i, param in enumerate(('rsrp', 'rssi', 'sinr', 'rsrq')):
            f = ttk.LabelFrame(self.digits_frame, text=param.upper(), padding=4)
            f.grid(row=0, column=i, padx=4, sticky='nsew')
            self.digits_frame.columnconfigure(i, weight=1)
            val = tk.Label(f, text="-", font=("Segoe UI", 20, "bold"), fg='gray')
            val.pack()
            status = tk.Label(f, text=t("Нет данных"), font=("Segoe UI", 9, "bold"), fg='gray')
            status.pack(pady=1)
            peak = tk.Label(f, text=t("Пик: -"), font=("Segoe UI", 9), fg='gray')
            peak.pack(side=tk.BOTTOM)
            self.lbl_vars[param] = {'val': val, 'status': status, 'peak': peak}

        self.dir_frame = ttk.LabelFrame(self.tab_monitor, text="", padding=6)
        self.dir_frame.pack(fill=tk.X, padx=10, pady=4)
        self.dir_label = tk.Label(self.dir_frame, text="—", font=("Segoe UI", 30, "bold"),
                                  fg='gray')
        self.dir_label.pack()
        self.dir_text = tk.Label(self.dir_frame, text=t("Накапливаю данные..."),
                                 font=("Segoe UI", 10), fg='gray')
        self.dir_text.pack()
        self.advice_lbl = self._wrap(tk.Label(self.dir_frame, text="", fg='#b35900',
                                              font=("Segoe UI", 9), justify='left'), 80)
        self.advice_lbl.pack(fill=tk.X)

        self.tools_frame = ttk.Frame(self.tab_monitor)
        self.tools_frame.pack(fill=tk.X, padx=15, pady=3)
        self.jitter_label = ttk.Label(self.tools_frame, text=t("Джиттер: -"),
                                      font=("Segoe UI", 10, "bold"))
        self.jitter_label.pack(side=tk.LEFT)
        ttk.Button(self.tools_frame, text=t("🖥 Крышный режим (F11)"),
                   command=self.toggle_roof_mode).pack(side=tk.RIGHT, padx=5)
        self.sound_var = tk.BooleanVar(value=self._saved['sound'] and HAS_WINSOUND)
        sound_cb = ttk.Checkbutton(self.tools_frame, text=t("🔊 Звук (Ctrl+M)"),
                                   variable=self.sound_var)
        if not HAS_WINSOUND:
            sound_cb.config(state='disabled', text=t("🔊 Аудио (ОС не поддерживается)"))
        sound_cb.pack(side=tk.RIGHT, padx=5)

        self.ctrl_frame = ttk.Frame(self.tab_monitor)
        self.ctrl_frame.pack(fill=tk.X, padx=10, pady=3)
        ttk.Label(self.ctrl_frame, text=t("График и стрелка:")).pack(side=tk.LEFT)
        self.graph_param = tk.StringVar(value=self._saved['graph'])
        self.graph_cb = ttk.Combobox(self.ctrl_frame, textvariable=self.graph_param,
                                     values=self._graph_params, state='readonly', width=9)
        self.graph_cb.pack(side=tk.LEFT, padx=5)
        self.graph_cb.bind("<<ComboboxSelected>>", self._on_graph_param)
        ttk.Button(self.ctrl_frame, text=t("Сбросить пики (Ctrl+R)"),
                   command=self.reset_peaks).pack(side=tk.RIGHT, padx=5)
        ttk.Button(self.ctrl_frame, text=t("💾 Экспорт CSV"),
                   command=self.export_csv).pack(side=tk.RIGHT, padx=5)

        self.signal_graph = CanvasGraph(self.tab_monitor, self.s, GRAPH_HISTORY,
                                        height=self.px(170))
        self.signal_graph.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self.signal_graph.set_param(self.graph_param.get())
        self.state.set_trend_param(self.graph_param.get())
        self._render_trend_title()

    # ---------- вкладка «Сеть» ----------

    def build_network_tab(self) -> None:
        self._net_buttons: list[ttk.Button] = []
        band_frame = ttk.LabelFrame(self.tab_network, text=t("Фиксация частот (Band Lock)"),
                                    padding=10)
        band_frame.pack(fill=tk.X, padx=10, pady=8)
        self._wrap(ttk.Label(band_frame, justify='left', text=t(
            "Фиксация бэндов привязывает модем к выбранным частотам. Список "
            "взят из модема; замеченные в эфире бэнды — первыми. Перед "
            "записью программа читает текущие настройки и меняет только "
            "LTE-бэнды; «Вернуть как было» восстановит настройки, "
            "прочитанные при подключении.")), 80).pack(anchor='w', pady=(0, 6))
        self.lock_state_lbl = ttk.Label(band_frame, text=t("Сейчас на модеме: -"),
                                        font=("Segoe UI", 10, "bold"))
        self.lock_state_lbl.pack(anchor='w')
        self.band_grid = ttk.Frame(band_frame)
        self.band_grid.pack(fill=tk.X, pady=4)
        self._rebuild_band_grid(self._band_list)

        btns = ttk.Frame(band_frame)
        btns.pack(fill=tk.X, pady=6)
        for text, cmd in ((t("Отметить текущие"), self.mark_current_bands),
                          (t("Применить Band Lock"), self.apply_bands),
                          (t("Все бэнды (AUTO)"), self.reset_bands),
                          (t("↩ Вернуть как было"), self.restore_bands)):
            b = ttk.Button(btns, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=4)
            if cmd is not self.mark_current_bands:
                self._net_buttons.append(b)

        ant_frame = ttk.LabelFrame(self.tab_network, text=t("Переключение антенн"), padding=10)
        ant_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(ant_frame, text=t("Режим:")).pack(side=tk.LEFT, padx=5)
        self.antenna_var = tk.StringVar(value=t("Авто"))
        ttk.Combobox(ant_frame, textvariable=self.antenna_var,
                     values=[t(k) for k in ANTENNA_MODES], state='readonly',
                     width=15).pack(side=tk.LEFT, padx=5)
        b = ttk.Button(ant_frame, text=t("Применить"), command=self.apply_antenna)
        b.pack(side=tk.LEFT, padx=5)
        self._net_buttons.append(b)

        mgmt = ttk.LabelFrame(self.tab_network, text=t("Управление роутером"), padding=10)
        mgmt.pack(fill=tk.X, padx=10, pady=5)
        self._wrap(ttk.Label(mgmt, justify='left', text=t(
            "«Переподключить связь» выключает и включает мобильные данные: "
            "модем заново выбирает лучшую соту — быстрее перезагрузки. "
            "После перезагрузки программа переподключится сама.")), 80).pack(
            anchor='w', pady=(0, 6))
        for text, cmd in ((t("📶 Переподключить связь"), self.reattach),
                          (t("🔄 Перезагрузить роутер"), self.reboot_router),
                          (t("🧾 Сохранить диагностику"), self.save_diagnostics)):
            b = ttk.Button(mgmt, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=5)
            self._net_buttons.append(b)
        self.net_msg = self._wrap(tk.Label(self.tab_network, text="", fg='#333',
                                           font=("Segoe UI", 10, "bold"), justify='left'))
        self.net_msg.pack(fill=tk.X, padx=15, pady=6, anchor='w')

    def _rebuild_band_grid(self, bands: list[int]) -> None:
        checked = {b for b, v in self.band_vars.items() if v.get()}
        for child in self.band_grid.winfo_children():
            child.destroy()
        self.band_vars = {}
        cols = 4
        for i, band in enumerate(bands):
            var = tk.BooleanVar(value=band in checked)
            ttk.Checkbutton(self.band_grid, text=lte_band_label(band, t("МГц")),
                            variable=var).grid(row=i // cols, column=i % cols,
                                               sticky='w', padx=8, pady=2)
            self.band_vars[band] = var
        for c in range(cols):
            self.band_grid.columnconfigure(c, weight=1)

    # ---------- вкладка «Вышка» ----------

    def build_tower_tab(self) -> None:
        info = ttk.LabelFrame(self.tab_tower, text=t("Информация о станции"), padding=8)
        info.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.tower_labels: dict[str, ttk.Label] = {}
        fields = [
            ('plmn', 'Оператор (PLMN)'), ('rat', 'Технология'),
            ('band', 'Рабочий Band (LTE)'), ('freq', 'Частота DL'),
            ('earfcn', 'EARFCN (канал DL)'), ('aggregation', 'Агрегация (CA)'),
            ('dlbandwidth', 'Ширина канала'), ('pci', 'Сектор антенны (PCI)'),
            ('enodeb', 'eNodeB (Вышка)'), ('sector', 'Cell (Локальный сектор)'),
            ('tac', 'TAC (зона)'), ('nr', '5G NR'),
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(info, text=f"{t(name)}:", font=("Segoe UI", 10, "bold")).grid(
                row=i // 2, column=(i % 2) * 2, sticky='e', pady=2, padx=5)
            lbl = ttk.Label(info, text="-", font=("Segoe UI", 10))
            lbl.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky='w', pady=2, padx=5)
            self.tower_labels[key] = lbl
        info.columnconfigure(1, weight=1)
        info.columnconfigure(3, weight=1)

        cells = ttk.LabelFrame(self.tab_tower, text=t("Лучшие соты за сессию"), padding=6)
        cells.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        cols = ('band', 'pci', 'enb', 'sinr', 'rsrp', 'n')
        heads = (t("Бэнд"), "PCI", "eNB / Cell", t("Лучший SINR"), t("Лучший RSRP"), t("Замеров"))
        self.cells_tree = ttk.Treeview(cells, columns=cols, show='headings', height=5)
        for c, h in zip(cols, heads, strict=True):
            self.cells_tree.heading(c, text=h)
            self.cells_tree.column(c, width=self.px(110), anchor='center')
        self.cells_tree.pack(fill=tk.BOTH, expand=True)

        sim = ttk.LabelFrame(self.tab_tower, text=t("SIM / Устройство"), padding=8)
        sim.pack(fill=tk.X, padx=10, pady=4)
        self.sim_labels: dict[str, ttk.Label] = {}
        sim_fields = [
            ('Imei', 'IMEI (роутер)'), ('Imsi', 'IMSI (SIM)'), ('Iccid', 'ICCID (SIM-карта)'),
            ('Msisdn', 'Номер телефона'), ('SerialNumber', 'Серийный номер'),
            ('DeviceName', 'Модель'), ('SoftwareVersion', 'Прошивка'),
        ]
        for i, (key, name) in enumerate(sim_fields):
            ttk.Label(sim, text=f"{t(name)}:", font=("Segoe UI", 10, "bold")).grid(
                row=i // 2, column=(i % 2) * 2, sticky='e', pady=2, padx=5)
            lbl = ttk.Label(sim, text="-", font=("Consolas", 10))
            lbl.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky='w', pady=2, padx=5)
            self.sim_labels[key] = lbl

        btn_frame = ttk.Frame(self.tab_tower)
        btn_frame.pack(fill=tk.X, padx=10, pady=4)
        ttk.Button(btn_frame, text=t("🗺 Открыть на CellMapper"),
                   command=self.open_cellmapper).pack(side=tk.LEFT, padx=5)

    # ---------- вкладка «Состояние» ----------

    def build_status_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_status, text=t("Мониторинг железа и трафика"), padding=10)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.stat_labels: dict[str, ttk.Label] = {}
        fields = [
            ('uptime', 'Время сессии'), ('temp', 'Температура чипа'),
            ('data', 'Мобильные данные'), ('dl_rate', 'Скорость (Download)'),
            ('ul_rate', 'Скорость (Upload)'), ('total_dl', 'Скачано за сессию'),
            ('total_ul', 'Отдано за сессию'), ('month_traffic', 'Трафик за месяц (↓/↑)'),
            ('mod', 'Модуляция DL / UL'), ('mimo', 'Режим MIMO'),
            ('cqi', 'CQI (потоки)'), ('txpower', 'Мощность передатчика'),
            ('rrc', 'RRC'), ('rsrp_min', 'RSRP мин / макс'), ('sinr_min', 'SINR мин / макс'),
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(frame, text=f"{t(name)}:", font=("Segoe UI", 10, "bold")).grid(
                row=i, column=0, sticky='ne', pady=3, padx=5)
            lbl = ttk.Label(frame, text="-", font=("Segoe UI", 10), justify='left')
            lbl.grid(row=i, column=1, sticky='w', pady=3, padx=5)
            if key == 'mod':
                self._wrap(lbl, 320)
            self.stat_labels[key] = lbl

    # ---------- вкладка «Белые списки» ----------

    def build_whitelist_tab(self) -> None:
        intro = ttk.LabelFrame(self.tab_whitelist, text=t("Перед проверкой"), padding=10)
        intro.pack(fill=tk.X, padx=10, pady=10)
        self._wrap(ttk.Label(intro, justify='left', text=t(
            "⚠ Ноутбук должен быть подключён к Wi-Fi или USB именно этого "
            "роутера — иначе тест измерит чужой канал.\n"
            "• Применимо только для РФ.")), 80).pack(anchor='w')

        ctrl = ttk.Frame(self.tab_whitelist)
        ctrl.pack(fill=tk.X, padx=10, pady=5)
        self.wl_button = ttk.Button(ctrl, text=t("🔍 Проверить сейчас"),
                                    command=self._start_whitelist_check)
        self.wl_button.pack(side=tk.LEFT, padx=5)
        self.wl_progress = ttk.Progressbar(ctrl, orient="horizontal", mode="indeterminate",
                                           length=self.px(200))
        self.wl_progress.pack(side=tk.LEFT, padx=10)

        verdict = ttk.LabelFrame(self.tab_whitelist, text=t("Вердикт"), padding=12)
        verdict.pack(fill=tk.X, padx=10, pady=5)
        self.wl_title = tk.Label(verdict, text=t("Не проверялось"),
                                 font=("Segoe UI", 14, "bold"), fg='gray')
        self.wl_title.pack(anchor='w')
        self.wl_detail = self._wrap(tk.Label(verdict, text="—", font=("Segoe UI", 10),
                                             fg='gray', justify='left'), 80)
        self.wl_detail.pack(anchor='w', pady=(4, 0))

        details = ttk.Frame(self.tab_whitelist)
        details.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.wl_labels: dict[str, tk.Label] = {}
        for title, hosts, side_pad in ((t("✅ В белых списках"), WHITELIST_HOSTS_RU, (0, 5)),
                                       (t("⚪ Нейтральные"), CONTROL_HOSTS_NEUTRAL, (5, 0))):
            box = ttk.LabelFrame(details, text=title, padding=8)
            box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=side_pad)
            for host, port in hosts:
                lbl = tk.Label(box, text=f"{host}:{port} — ⏳ {t('не проверено')}",
                               font=("Consolas", 10), fg='gray', anchor='w')
                lbl.pack(fill=tk.X, padx=4, pady=2)
                self.wl_labels[host] = lbl

    # =====================================================
    # ОБЩИЕ ПОМОЩНИКИ
    # =====================================================

    def toggle_on_top(self) -> None:
        self.root.attributes('-topmost', self.ontop_var.get())

    def _toggle_sound(self) -> None:
        if HAS_WINSOUND:
            self.sound_var.set(not self.sound_var.get())

    def _sync_interval(self) -> None:
        try:
            self._interval_seconds = float(self.update_interval.get())
        except (ValueError, tk.TclError):
            self._interval_seconds = 1.0

    def _sync_reconnect(self) -> None:
        if self.worker is not None:
            self.worker.auto_reconnect = self.reconnect_var.get()

    def _run_bg(self, work: Callable[[], Any], done: Callable[[Any], None],
                fail: Callable[[BaseException], None]) -> None:
        """Выполнить work в фоне, результат/ошибку — в главном потоке."""
        def task() -> None:
            try:
                result = work()
            except Exception as exc:
                logger.exception("Background task failed")
                self.dispatch.post(fail, exc)
            else:
                self.dispatch.post(done, result)
        threading.Thread(target=task, daemon=True).start()

    def _net_action(self, work: Callable[[], Any], done: Callable[[Any], None],
                    busy_text: str) -> None:
        """Команда роутеру: кнопки блокируются до ответа (без двойных записей)."""
        if self.session is None or self.link != 'online':
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        if self._busy:
            return
        self._busy = True
        self._set_net_buttons('disabled')
        self.net_msg.config(text=busy_text, fg='#0078D7')

        def finish(result: Any) -> None:
            self._busy = False
            self._set_net_buttons('normal')
            done(result)

        def failed(exc: BaseException) -> None:
            self._busy = False
            self._set_net_buttons('normal')
            self.net_msg.config(text=t("Роутер отклонил команду: {err}").format(
                err=humanize_error(exc)), fg='#d63031')
        self._run_bg(work, finish, failed)

    def _set_net_buttons(self, state: str) -> None:
        for b in self._net_buttons:
            with contextlib.suppress(tk.TclError):
                b.config(state=state)

    # =====================================================
    # ПОДКЛЮЧЕНИЕ
    # =====================================================

    def start_discovery(self) -> None:
        self.find_button.config(state='disabled')
        self.conn_msg.config(text=t("Ищу роутер…"), fg='#0078D7')
        entered = self.ip_entry.get().strip()
        candidates = ([entered] if is_valid_ip(entered) else []) + list(DISCOVERY_CANDIDATES)

        def done(found: Any) -> None:
            self.find_button.config(state='normal')
            if found is None:
                self.conn_msg.config(text=t(
                    "Роутер не найден. Проверьте подключение к его Wi-Fi/USB."), fg='#d63031')
                return
            self.ip_entry.delete(0, tk.END)
            self.ip_entry.insert(0, found.ip)
            self.conn_msg.config(text=t("Найден роутер {model} на {ip}").format(
                model=found.model or "Huawei", ip=found.ip), fg='#00b894')
            self.password_entry.focus()

        self._run_bg(lambda: discover_router(candidates), done,
                     lambda e: done(None))

    def start_connect(self) -> None:
        if self.worker is not None:
            self.disconnect()
            return
        ip = self.ip_entry.get().strip()
        if not is_valid_ip(ip):
            self.conn_msg.config(text=t("Неверный IP-адрес: {ip}\nПример: 192.168.8.1").format(
                ip=repr(ip)), fg='#d63031')
            return
        self.demo_modem = None
        self._start_session(RouterSession(ip, self.password_entry.get()))

    def start_demo(self) -> None:
        if self.worker is not None:
            self.disconnect()
        self.demo_modem = DemoModem()
        self._start_session(RouterSession("demo", "", factory=demo_factory(self.demo_modem)))

    def _start_session(self, session: RouterSession) -> None:
        self._sync_interval()
        self.state.reset()
        self.session = session
        self.conn_msg.config(text="")
        self.event_line.config(text="")
        for item in self.cells_tree.get_children():
            self.cells_tree.delete(item)
        worker: SessionWorker | None = None

        def mine(fn: Callable[..., None]) -> Callable[..., None]:
            """Событие фонового потока → главный поток, только для текущей сессии."""
            def post(*args: Any) -> None:
                def call() -> None:
                    if worker is self.worker:
                        fn(*args)
                self.dispatch.post(call)
            return post

        worker = SessionWorker(
            session, interval=lambda: self._interval_seconds,
            auto_reconnect=self.reconnect_var.get(),
            on_connected=mine(self._on_connected), on_snapshot=mine(self._on_snapshot),
            on_status=mine(self._on_status), on_fatal=mine(self._on_fatal))
        self.worker = worker
        self.link = 'connecting'
        self._render_link_state()
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
        self._set_net_buttons('normal')
        self._render_link_state()

    def _render_link_state(self) -> None:
        demo = self.demo_modem is not None
        text, color = {
            'offline': (t("Отключено"), 'red'),
            'connecting': (t("Подключение..."), 'orange'),
            'online': (t("🧪 Тестовый режим") if demo else t("Подключено"), 'green'),
            'reconnecting': (t("Нет связи с роутером — переподключаюсь…"), 'orange'),
        }[self.link]
        self.status_label.config(text=text, foreground=color)
        self.connect_button.config(
            text=t("🚀 Подключиться") if self.link == 'offline' else t("⏹ Отключиться"))

    def _on_connected(self, info: dict[str, Any]) -> None:
        self.link = 'online'
        self._render_link_state()
        self.notebook.select(self.tab_monitor)
        self._fill_device(info)
        session = self.session
        if session is not None:
            self._band_list = lockable_bands(session.supported_bands, self.state.observed_bands)
            self._rebuild_band_grid(self._band_list)
        self.load_router_config()

    def _fill_device(self, info: dict[str, Any]) -> None:
        for key, lbl in self.sim_labels.items():
            raw = info.get(key, '')
            lbl.config(text=str(raw) if raw not in (None, '') else t("Н/Д"))

    def _on_status(self, status: str, delay: float | None, exc: BaseException | None) -> None:
        if status == 'reconnecting':
            self.link = 'reconnecting'
            if exc is not None:
                self.conn_msg.config(text=humanize_error(exc), fg='#b35900')
        else:
            self.link = 'online'
            self.conn_msg.config(text="")
        self._render_link_state()

    def _on_fatal(self, exc: BaseException) -> None:
        was_online = self.link in ('online', 'reconnecting')
        self.disconnect()
        self.conn_msg.config(text=humanize_error(exc), fg='#d63031')
        if not was_online:
            self.notebook.select(self.tab_settings)
        else:
            self.status_label.config(text=t("Связь с роутером потеряна"), foreground='red')

    def load_router_config(self) -> None:
        """Читает текущий Band Lock и антенну (только чтение)."""
        session = self.session
        if session is None:
            return

        def done(cfg: Any) -> None:
            if session is not self.session:
                return
            if cfg.locked is None:
                self.lock_state_lbl.config(text=t("Сейчас на модеме: не прочитано"))
            elif not cfg.locked:
                self.lock_state_lbl.config(text=t("Сейчас на модеме: AUTO (все бэнды)"))
                for var in self.band_vars.values():
                    var.set(False)
            else:
                self.lock_state_lbl.config(text=t("Сейчас на модеме: {bands}").format(
                    bands=", ".join(f"B{b}" for b in cfg.locked)))
                extra = [b for b in cfg.locked if b not in self._band_list]
                if extra:
                    self._band_list += extra
                    self._rebuild_band_grid(self._band_list)
                for b, var in self.band_vars.items():
                    var.set(b in cfg.locked)
            if cfg.antenna is not None:
                for label, code in ANTENNA_MODES.items():
                    if code == cfg.antenna:
                        self.antenna_var.set(t(label))

        self._run_bg(session.read_config, done, lambda e: None)

    # =====================================================
    # ДАННЫЕ И ОТРИСОВКА
    # =====================================================

    def _on_snapshot(self, snap: Snapshot) -> None:
        change = self.state.ingest(snap, time.monotonic())
        if change is not None:
            self.event_line.config(text=t("{at} смена соты: {old} → {new}").format(
                at=change.at, old=change.old_label, new=change.new_label))
        if snap.has_nr and 'nr_sinr' not in self._graph_params:
            self._graph_params = list(LTE_PARAMS + NR_PARAMS)
            self.graph_cb.config(values=self._graph_params)
        new_bands = [b for b in snap.bands if b not in self._band_list]
        if new_bands and self.session is not None:
            self._band_list = lockable_bands(self.session.supported_bands,
                                             self.state.observed_bands)
            self._rebuild_band_grid(self._band_list)
        self._render(snap)
        self._beep()

    def _tick(self) -> None:
        """Каждые 0.5 с: признак устаревших данных."""
        try:
            if self.link in ('online', 'reconnecting') and self.state.last is not None:
                stale = self.state.is_stale(time.monotonic(), self._interval_seconds)
                self._render_staleness(stale)
        finally:
            self._tick_id = self.root.after(500, self._tick)

    def _render_staleness(self, stale: bool) -> None:
        param = self.graph_param.get()
        self.signal_graph.update_series(list(self.state.history.get(param, [])),
                                        self.state.peaks.get(param), stale)
        if stale:
            age = self.state.age(time.monotonic()) or 0
            self.health_text_lbl.config(text=t("⚠ Данные устарели — нет ответа {s:.0f} с").format(
                s=age), fg='#d63031')
            for widgets in self.lbl_vars.values():
                widgets['val'].config(fg='#aaaaaa')
        self._render_roof(stale)

    def _render(self, snap: Snapshot) -> None:
        st = self.state
        score, summary, color = calculate_overall_health(snap.rsrp, snap.sinr)
        self.health_progress.config(value=score)
        self.health_text_lbl.config(text=t(summary).format(pct=score), fg=color)
        self.tower_line.config(text=self._tower_summary(snap))

        for param, widgets in self.lbl_vars.items():
            val = snap.metric(param)
            label, col, _ = evaluate_signal(param, val)
            widgets['val'].config(text=fmt_num(val), fg=col)
            widgets['status'].config(text=t(label), fg=col)
            peak, delta = st.peaks.get(param), st.delta_to_peak(param)
            if peak is None or delta is None:
                widgets['peak'].config(text=t("Пик: -"), fg='gray')
            else:
                widgets['peak'].config(
                    text=t("Пик: {v} (Δ {d})").format(v=fmt_num(peak), d=f"{delta:+g}"),
                    fg='#00b894' if delta > -1 else '#555')

        self._render_trend()
        jitter = st.jitter()
        if jitter is not None:
            jcol = 'green' if jitter < 3 else 'orange' if jitter < 7 else 'red'
            self.jitter_label.config(text=t("Джиттер: {j:.1f} dB").format(j=jitter),
                                     foreground=jcol)
        mimo = mimo_status(snap.cqi0, snap.cqi1)
        up = uplink_status(snap.pusch_dbm)
        tips = advice(rsrp=snap.rsrp, sinr=snap.sinr, rsrq=snap.rsrq, jitter=jitter,
                      mimo=mimo, uplink=up)
        if snap.data_enabled is False:
            tips.insert(0, "Мобильные данные на роутере выключены.")
        self.advice_lbl.config(text="\n".join("💡 " + t(tip) for tip in tips[:2]))

        param = self.graph_param.get()
        self.signal_graph.update_series(list(st.history.get(param, [])),
                                        st.peaks.get(param), False)
        self._render_tower(snap)
        self._render_status(snap, mimo, up)
        self._render_roof(False)

    def _tower_summary(self, snap: Snapshot) -> str:
        parts = []
        if self.demo_modem is not None:
            parts.append(t("🧪 ДЕМО · азимут {a}").format(a=demo_angle_hint(self.demo_modem)))
        if snap.bands:
            freq = f" · {snap.freq_mhz:g} {t('МГц')}" if snap.freq_mhz else ""
            parts.append(f"{snap.band_label.replace('МГц', t('МГц'))}{freq}")
        if snap.pci is not None:
            parts.append(f"PCI {snap.pci}")
        if snap.enodeb is not None:
            parts.append(f"eNB {snap.enodeb}/{snap.sector}")
        if snap.rat:
            parts.append(t(RAT_LABELS.get(snap.rat, snap.rat)))
        return " · ".join(parts) if parts else "—"

    def _render_trend_title(self) -> None:
        title = PARAM_TITLES.get(self.state.trend_param, self.state.trend_param.upper())
        self.dir_frame.config(text=t("Тенденция {param} (поворачивайте антенну)").format(
            param=title))

    def _render_trend(self) -> None:
        arrow, color = TREND_GLYPHS.get(self.state.trend, ("—", "gray"))
        text = {
            "↑": t("Сигнал улучшается — продолжайте в том же направлении"),
            "↓": t("Сигнал ухудшается — поверните обратно"),
            "→": t("Сигнал стабилен — зафиксируйте антенну"),
        }.get(arrow, t("Накапливаю данные..."))
        self.dir_label.config(text=arrow, fg=color)
        self.dir_text.config(text=text, fg=color)

    def _render_tower(self, snap: Snapshot) -> None:
        mhz = t('МГц')
        ca_text = {True: t("Активна"), False: t("Нет"), None: "-"}[snap.ca]
        nr = snap.nr_summary() if snap.has_nr else "-"
        dl, ul = snap.dl_bandwidth or '-', snap.ul_bandwidth
        values = {
            'plmn': f"{snap.plmn} ({snap.operator or t('Неизвестный оператор')})"
                    if snap.plmn else '-',
            'rat': t(RAT_LABELS.get(snap.rat, snap.rat)),
            'band': snap.band_label.replace('МГц', mhz),
            'freq': f"{snap.freq_mhz:g} {mhz}" if snap.freq_mhz else '-',
            'earfcn': snap.earfcn_raw or '-',
            'aggregation': ca_text,
            'dlbandwidth': f"DL {dl} / UL {ul}" if ul else dl,
            'pci': fmt_num(snap.pci),
            'enodeb': fmt_num(snap.enodeb) if snap.enodeb is not None else
            (f"NCI {snap.nci}" if snap.nci else '-'),
            'sector': fmt_num(snap.sector),
            'tac': snap.tac or '-',
            'nr': nr,
        }
        for key, lbl in self.tower_labels.items():
            lbl.config(text=values.get(key, '-'))
        if self.notebook.select() == str(self.tab_tower):
            self._render_cells()

    def _render_cells(self) -> None:
        tree = self.cells_tree
        for item in tree.get_children():
            tree.delete(item)
        current = self.state.last.cell if self.state.last else None
        for c in self.state.top_cells():
            mark = "▶ " if c.key == current else ""
            tree.insert('', tk.END, values=(
                mark + c.band_label.replace('МГц', t('МГц')), fmt_num(c.key.pci),
                f"{fmt_num(c.enodeb)}/{fmt_num(c.sector)}", fmt_num(c.best_sinr),
                fmt_num(c.best_rsrp), c.samples))

    def _render_status(self, snap: Snapshot, mimo: str | None, up: str | None) -> None:
        lbl = self.stat_labels
        uptime = str(datetime.timedelta(seconds=snap.connect_time)) \
            if snap.connect_time and snap.connect_time > 0 else "-"
        lbl['uptime'].config(text=uptime)
        lbl['temp'].config(text=snap.temperature or t('Н/Д'))
        lbl['data'].config(text={True: t("включены"), False: t("ВЫКЛЮЧЕНЫ"),
                                 None: "-"}[snap.data_enabled],
                           foreground='#d63031' if snap.data_enabled is False else '')
        lbl['dl_rate'].config(text=format_rate_mbps(snap.dl_rate))
        lbl['ul_rate'].config(text=format_rate_mbps(snap.ul_rate))
        lbl['total_dl'].config(text=format_bytes_mb(snap.total_dl))
        lbl['total_ul'].config(text=format_bytes_mb(snap.total_ul))
        if snap.month_dl is not None or snap.month_ul is not None:
            lbl['month_traffic'].config(text=f"{format_bytes_mb(snap.month_dl or 0)} / "
                                             f"{format_bytes_mb(snap.month_ul or 0)}")
        else:
            lbl['month_traffic'].config(text="-")
        mods = [f"{d} {m}" for d, m in (("DL", format_modulation(snap.dl_mcs)),
                                        ("UL", format_modulation(snap.ul_mcs))) if m]
        lbl['mod'].config(text=" / ".join(mods) if mods else "-")
        lbl['mimo'].config(text=format_mimo(snap.transmode) if snap.transmode else "-")
        cqi = "-"
        if snap.cqi0 is not None:
            cqi = f"{snap.cqi0}" + (f" / {snap.cqi1}" if snap.cqi1 is not None else "")
            if mimo:
                cqi += f" — {t(MIMO_LABELS[mimo])}"
        lbl['cqi'].config(text=cqi)
        tx = snap.txpower or "-"
        if up:
            tx += f" — {t(UPLINK_LABELS[up])}"
        lbl['txpower'].config(text=tx)
        lbl['rrc'].config(text=snap.rrc or "-")
        st = self.state
        for p, key in (('rsrp', 'rsrp_min'), ('sinr', 'sinr_min')):
            lo, hi = st.session_min.get(p), st.session_max.get(p)
            if lo is not None:
                lbl[key].config(text=f"{fmt_num(lo)} / {fmt_num(hi)} {unit_of(p)}")

    def _beep(self) -> None:
        if self.beeper is None or not self.sound_var.get():
            return
        param = self.state.trend_param
        delta = self.state.delta_to_peak(param)
        if delta is not None:
            self.beeper.beep(beep_frequency(param, delta), 80)

    def _on_graph_param(self, _event: Any = None) -> None:
        param = self.graph_param.get()
        self.state.set_trend_param(param)
        self.signal_graph.set_param(param)
        self._render_trend_title()
        self._render_trend()
        self.signal_graph.update_series(list(self.state.history.get(param, [])),
                                        self.state.peaks.get(param), False)

    def reset_peaks(self) -> None:
        self.state.reset_peaks()
        for widgets in self.lbl_vars.values():
            widgets['peak'].config(text=t("Пик: -"), fg='gray')
        if self.state.last is not None:
            self._render(self.state.last)

    # =====================================================
    # СЕТЬ / АНТЕННА / РОУТЕР
    # =====================================================

    def mark_current_bands(self) -> None:
        snap = self.state.last
        if snap is None or not snap.bands:
            self.net_msg.config(text=t("Текущий бэнд ещё не определён."), fg='#d63031')
            return
        missing = [b for b in snap.bands if b not in self.band_vars]
        if missing:
            self._band_list += missing
            self._rebuild_band_grid(self._band_list)
        for b, var in self.band_vars.items():
            var.set(b in snap.bands)
        self.net_msg.config(text=t("Отмечены текущие бэнды: {bands}").format(
            bands=", ".join(f"B{b}" for b in snap.bands)), fg='#0078D7')

    def apply_bands(self) -> None:
        session = self.session
        selected = [b for b, v in self.band_vars.items() if v.get()]
        if session is None or self.link != 'online':
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        if not selected:
            self.net_msg.config(text=t("Выберите хотя бы один диапазон!"), fg='#d63031')
            return
        names = ", ".join(f"B{b}" for b in selected)
        lines = [t("Зафиксировать бэнды: {bands}?").format(bands=names)]
        lines += lock_warnings(selected, self.state.last)
        if not session.supports_5g:
            lines.append(t("Режим сети будет «только 4G»."))
        if not messagebox.askyesno(t("Подтверждение"), "\n\n".join(lines)):
            return
        self._net_action(lambda: session.apply_plan(session.plan_band_lock(selected)),
                         lambda ok: self._after_write(ok, t("Band Lock применён: {bands}.").format(
                             bands=names)), t("Применяю Band Lock…"))

    def reset_bands(self) -> None:
        session = self.session
        if session is None:
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        self._net_action(lambda: session.apply_plan(session.plan_all_bands()),
                         lambda ok: self._after_write(ok, t("Включены все бэнды (AUTO).")),
                         t("Включаю все бэнды…"))

    def restore_bands(self) -> None:
        session = self.session
        if session is None:
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        plan = session.plan_restore_original()
        if plan is None:
            self.net_msg.config(text=t(
                "Исходные настройки не прочитаны — роутер не отдал net-mode."), fg='#d63031')
            return
        self._net_action(lambda: session.apply_plan(plan),
                         lambda ok: self._after_write(ok, t(
                             "Восстановлены настройки, прочитанные при подключении.")),
                         t("Восстанавливаю настройки…"))

    def _after_write(self, verified: bool, success_text: str) -> None:
        if verified:
            self.net_msg.config(text=success_text + " " + t(
                "Модем перерегистрируется в сети (до ~30 с)."), fg='#00b894')
        else:
            self.net_msg.config(text=t(
                "Команда отправлена, но роутер вернул другие настройки — "
                "проверьте строку «Сейчас на модеме»."), fg='#b35900')
        self.load_router_config()

    def apply_antenna(self) -> None:
        session = self.session
        rev = {t(k): k for k in ANTENNA_MODES}
        label = rev.get(self.antenna_var.get(), self.antenna_var.get())
        code = parse_antenna_value(label)
        if session is None:
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        if code is None:
            self.net_msg.config(text=t("Неизвестный режим антенны."), fg='#d63031')
            return

        def work() -> int | None:
            session.set_antenna(code)
            return session.read_antenna()

        def done(read_back: int | None) -> None:
            if read_back == code or read_back is None:
                self.net_msg.config(text=t("Тип антенны изменён: {mode}").format(
                    mode=self.antenna_var.get()), fg='#00b894')
            else:
                self.net_msg.config(text=t(
                    "Роутер принял команду, но сообщает другой режим антенны."), fg='#b35900')
        self._net_action(work, done, t("Переключаю антенну…"))

    def reattach(self) -> None:
        session = self.session
        if session is None:
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        if not messagebox.askyesno(t("Подтверждение"), t(
                "Переподключить мобильную связь?\n\nИнтернет пропадёт примерно "
                "на 5–10 секунд.")):
            return
        self._net_action(session.reattach, lambda ok: self.net_msg.config(
            text=t("Связь переподключена.") if ok else
            t("Не удалось включить мобильные данные — включите их в веб-интерфейсе роутера."),
            fg='#00b894' if ok else '#d63031'), t("Переподключаю связь…"))

    def reboot_router(self) -> None:
        session, worker = self.session, self.worker
        if session is None or worker is None:
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        if not messagebox.askyesno(t("Подтверждение"), t(
                "Перезагрузить роутер?\n\nСоединение с интернетом прервётся "
                "на 1–2 минуты. Программа переподключится автоматически.")):
            return

        def done(_: Any) -> None:
            worker.auto_reconnect = True
            self.net_msg.config(text=t(
                "Роутер перезагружается — переподключусь автоматически."), fg='#0078D7')
        self._net_action(session.reboot, done, t("Отправляю команду перезагрузки…"))

    def save_diagnostics(self) -> None:
        session = self.session
        if session is None or self.link != 'online':
            self.net_msg.config(text=t("Сначала подключитесь к роутеру."), fg='#d63031')
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile=f"hua4gmon-diag-{datetime.datetime.now():%Y%m%d-%H%M%S}.json")
        if not path:
            return

        def work() -> str:
            dumps, errors = session.collect_diagnostics()
            report = build_diagnostics(
                app_version=__version__, library_version=library_version(),
                platform=f"Windows {platform.release()} / Python {platform.python_version()}",
                dumps=dumps, errors=errors)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(diagnostics_json(report))
            return path
        self._net_action(work, lambda p: self.net_msg.config(text=t(
            "Диагностика сохранена: {path}. Личные номера замаскированы.").format(path=p),
            fg='#00b894'), t("Собираю диагностику…"))

    # =====================================================
    # CellMapper / CSV
    # =====================================================

    def open_cellmapper(self) -> None:
        snap = self.state.last
        if snap is None or len(snap.plmn) < 5 or snap.enodeb is None:
            messagebox.showwarning(t("Внимание"),
                                   t("Недостаточно данных о вышке (нужны PLMN и eNodeB)."))
            return
        url = (f"https://www.cellmapper.net/map?MCC={snap.plmn[:3]}&MNC={snap.plmn[3:]}"
               f"&type=LTE&siteid={snap.enodeb}")
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror(t("Ошибка"), t("Не открыть браузер: {e}").format(e=e))

    def export_csv(self) -> None:
        if not self.state.log:
            messagebox.showinfo(t("Экспорт"), t(
                "Лог сессии пуст. Подключитесь и подождите, пока соберутся данные."))
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv"), ("All", "*.*")],
            initialfile=default_csv_name())
        if not path:
            return
        try:
            n = write_session_csv(path, list(self.state.log), current_language())
            messagebox.showinfo(t("Экспорт"), t("Сохранено {n} записей в:\n{path}").format(
                n=n, path=path))
        except OSError as e:
            messagebox.showerror(t("Ошибка"), t("Не удалось записать файл: {e}").format(e=e))

    # =====================================================
    # БЕЛЫЕ СПИСКИ
    # =====================================================

    def _start_whitelist_check(self) -> None:
        self.wl_button.config(state='disabled')
        self.wl_progress.start(10)
        self.wl_title.config(text=t("Проверка…"), fg='orange')
        self.wl_detail.config(text=t("Подождите 1–3 секунды."), fg='gray')
        for lbl in self.wl_labels.values():
            lbl.config(text=lbl.cget('text').split(' — ')[0] + " — ⏳", fg='gray')
        self._run_bg(run_whitelist_check, self._render_whitelist, self._whitelist_failed)

    def _render_whitelist(self, report: Any) -> None:
        self.wl_progress.stop()
        self.wl_button.config(state='normal')
        for r in list(report.white) + list(report.neutral):
            lbl = self.wl_labels.get(r.host)
            if lbl is None:
                continue
            sym, col = ("✅", '#00b894') if r.ok else ("❌", '#d63031')
            lbl.config(text=f"{r.host} — {sym} {r.detail}", fg=col)
        self.wl_title.config(text=report.title, fg=report.color)
        self.wl_detail.config(text=report.detail, fg='#444444')

    def _whitelist_failed(self, exc: BaseException) -> None:
        self.wl_progress.stop()
        self.wl_button.config(state='normal')
        self.wl_title.config(text=t("Ошибка"), fg='#d63031')
        self.wl_detail.config(text=humanize_error(exc), fg='#444444')

    # =====================================================
    # КРЫШНЫЙ РЕЖИМ
    # =====================================================

    def toggle_roof_mode(self) -> None:
        if self.roof_win is not None and self.roof_win.winfo_exists():
            self._close_roof()
            return
        win = tk.Toplevel(self.root)
        # Разворачиваем на том мониторе, где сейчас главное окно.
        win.geometry(f"+{self.root.winfo_rootx()}+{self.root.winfo_rooty()}")
        win.update_idletasks()
        win.attributes('-fullscreen', True)
        win.configure(bg='black')
        win.bind("<Escape>", lambda e: self._close_roof())
        win.protocol("WM_DELETE_WINDOW", self._close_roof)
        self.roof_win = win
        h = max(400, win.winfo_screenheight())
        big, arrow, small = -int(h * 0.13), -int(h * 0.2), -int(h * 0.035)
        tk.Label(win, text=t("[ESC] или F11 — выход"), font=("Segoe UI", small // 2),
                 fg='gray', bg='black').pack(pady=6)
        self.r_tower = tk.Label(win, text="—", justify='right', anchor='ne',
                                font=("Consolas", small), bg='black', fg='#aaaaaa')
        self.r_tower.place(relx=0.99, rely=0.02, anchor='ne')
        self.r_primary = tk.Label(win, text="-", font=("Consolas", big, "bold"),
                                  bg='black', fg='white')
        self.r_primary.pack(expand=True)
        self.r_dir = tk.Label(win, text="—", font=("Consolas", arrow, "bold"),
                              bg='black', fg='gray')
        self.r_dir.pack(expand=True)
        self.r_secondary = tk.Label(win, text="-", font=("Consolas", big, "bold"),
                                    bg='black', fg='white')
        self.r_secondary.pack(expand=True)
        self._render_roof(False)

    def _roof_line(self, param: str) -> tuple[str, str]:
        val = self.state.current(param)
        _, col, _ = evaluate_signal(param.replace('nr_', ''), val)
        delta = self.state.delta_to_peak(param)
        d = f" Δ{delta:+g}" if delta is not None else ""
        return f"{PARAM_TITLES.get(param, param.upper())} {fmt_num(val)}{d}", col

    def _render_roof(self, stale: bool) -> None:
        if self.roof_win is None or not self.roof_win.winfo_exists():
            return
        primary = self.state.trend_param
        secondary = 'rsrp' if primary != 'rsrp' else 'sinr'
        for lbl, param in ((self.r_primary, primary), (self.r_secondary, secondary)):
            text, col = self._roof_line(param)
            lbl.config(text=text, fg='#555555' if stale else col)
        arrow, color = TREND_GLYPHS.get(self.state.trend, ("—", "gray"))
        if stale:
            arrow, color = "⚠", '#d63031'
        self.r_dir.config(text=arrow, fg=color)
        snap = self.state.last
        self.r_tower.config(text=self._tower_summary(snap).replace(" · ", "\n")
                            if snap else "—")

    def _close_roof(self) -> None:
        if self.roof_win is not None and self.roof_win.winfo_exists():
            self.roof_win.destroy()
        self.roof_win = None

    # =====================================================
    # ЗАВЕРШЕНИЕ
    # =====================================================

    def on_closing(self) -> None:
        logger.info("Shutting down")
        worker = self.worker
        self.disconnect()
        self._close_roof()
        self.dispatch.close()
        with contextlib.suppress(tk.TclError):
            self.root.after_cancel(self._tick_id)
        if worker is not None:
            worker.join(timeout=2.0)       # дать отправить logout
        try:
            self.root.quit()
        finally:
            with contextlib.suppress(tk.TclError):
                self.root.destroy()


# =========================================================
# ВХОД
# =========================================================

def splash_status(text: str) -> None:
    """Строка хода запуска на заставке .exe (packaging/windows.spec).

    Первую строку после распаковки пишет packaging/splash_rthook.py.
    ImportError — запуск из исходников; RuntimeError/OSError — загрузчик
    не показал заставку или уже закрыл её.
    """
    with contextlib.suppress(ImportError, RuntimeError, OSError):
        import pyi_splash
        pyi_splash.update_text(text)


def close_splash() -> None:
    """Убирает заставку .exe, когда окно готово. При запуске из исходников
    заставки нет — модуля pyi_splash тоже.
    Hardware validation required: заставка и время запуска на слабом
    ноутбуке с разными антивирусами.
    """
    try:
        import pyi_splash
    except ImportError:
        return
    with contextlib.suppress(OSError):   # загрузчик уже закрыл заставку
        pyi_splash.close()


def run_self_test(report: str) -> int:
    """Проверка программы без роутера: библиотека, криптография, окно.

    CI запускает так собранный .exe; пользователь может приложить отчёт
    к обращению: `Hua4GMon.exe --self-test report.txt`. У оконного .exe
    нет консоли, поэтому отчёт пишется в файл. Код возврата 0 — исправно.
    """
    start = time.perf_counter()
    lines = [f"{APP_NAME} {__version__}, Python {platform.python_version()}"]
    ok = True
    try:
        lines += library_self_test()
        root = tk.Tk()
        root.withdraw()
        app = Hua4GMon(root)
        root.update()
        lines.append(f"Tk {root.tk.call('info', 'patchlevel')}: window built")
        app.on_closing()
    except Exception as exc:
        ok = False
        logger.exception("Self-test failed")
        lines.append(f"ERROR: {type(exc).__name__}: {exc}")
    lines.append(f"{'OK' if ok else 'FAIL'} in {time.perf_counter() - start:.2f} s")
    text = "\n".join(lines)
    print(text)
    if report:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"{APP_NAME} — портативный монитор LTE/5G Huawei.")
    p.add_argument('--ip', default='192.168.8.1', help='IP роутера (по умолчанию 192.168.8.1)')
    p.add_argument('--password', default='', help='Пароль (если указан — автоподключение)')
    p.add_argument('--demo', action='store_true', help='Тестовый режим без роутера')
    p.add_argument('--verbose', '-v', action='store_true', help='Подробный лог в stderr')
    p.add_argument('--self-test', nargs='?', const='', default=None, metavar='ФАЙЛ',
                   help='Проверить программу без роутера и выйти (отчёт — в ФАЙЛ)')
    p.add_argument('--version', action='version', version=f'{APP_NAME} {__version__}')
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                        stream=sys.stderr)
    configure_library_logging()
    enable_dpi_awareness()
    if args.self_test is not None:
        close_splash()
        return run_self_test(args.self_test)
    splash_status(t("Построение окна…"))
    root = tk.Tk()
    app = Hua4GMon(root, default_ip=args.ip, default_password=args.password, demo=args.demo)
    root.after_idle(close_splash)       # окно уже на экране — заставка не нужна
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_closing()
    return 0


if __name__ == "__main__":
    sys.exit(main())
