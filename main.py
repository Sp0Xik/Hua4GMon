"""
Hua4GMon — Huawei 4G Monitor (portable, single-file).

Назначение:
    Утилита для монтажников и владельцев роутеров Huawei
    (E3372, B315, B525, B535, B628, B818 и др.) для мониторинга качества
    LTE-сигнала, ручной настройки антенны и фиксации параметров БС.

Особенности этой версии:
    * Полностью portable: НИЧЕГО не сохраняется на диск
      (нет config.ini, нет паролей в файлах, нет логов на диск).
    * Один исполняемый файл.
    * График построен на голом tk.Canvas — нет matplotlib (~30 МБ
      экономии в .exe, быстрее запуск).
    * Авто-переподключение при обрыве связи с роутером.
    * Индикатор направления (↑↓→) — показывает, улучшается ли сигнал
      при повороте антенны.
    * Распознавание LTE-бандов и EARFCN с привязкой к частоте.
    * Проверка «белых списков» на БС (для РФ) через TCP-сокеты.
    * Экспорт сессии в CSV для отчётов клиенту (по запросу).
    * CLI-аргументы для быстрого запуска (--ip, --password).

Запуск:
    python main.py
    python main.py --ip 192.168.1.1 --password admin

Сборка portable .exe (Windows):
    pip install pyinstaller
    pyinstaller --onefile --windowed --name Hua4GMon main.py

Зависимости:
    huawei-lte-api>=1.10
"""

from __future__ import annotations

import argparse
import csv
import datetime
import logging
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection

# Чистая логика лежит в пакете core/ — она общая для Windows-версии
# (этот файл, через Tkinter) и будущей Android-версии (на Kivy).
# Никакой Tk-зависимости в core/ нет: модули можно импортировать
# из любого Python-окружения, включая python-for-android.
from core import (
    ANTENNA_MODES,
    BANDS,
    CONTROL_HOSTS_NEUTRAL,
    DIRECTION_LOOKBACK,
    GRAPH_HISTORY,
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
    SESSION_LOG_MAX,
    WHITELIST_HOSTS_RU,
    analyze_whitelist_results,
    calculate_overall_health,
    current_language,
    earfcn_to_band,  # noqa: F401  (доступно для отладки/расширений)
    evaluate_signal,
    extract_number,
    format_band_label,
    format_bytes_mb,
    format_rate_mbps,
    is_valid_ip,
    parse_antenna_value,
    parse_cell_id,
    set_language,
    t,
    tcp_reachable,
)

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False


__version__ = "1.2"
APP_NAME = "Hua4GMon"

logger = logging.getLogger(APP_NAME)


# =========================================================
# КОНСТАНТЫ и ЧИСТЫЕ ФУНКЦИИ — перенесены в пакет core/
# =========================================================
# Всё что раньше было здесь (PLMN_MAP, BANDS, EARFCN_RANGES,
# evaluate_signal, format_band_label, parse_cell_id и т.д.)
# теперь импортируется выше из `core`.
# Причина переноса: эти модули не зависят от Tkinter и переиспользуются
# в будущей Android-версии (android_main.py на Kivy).



# =========================================================
# ГРАФИК НА tk.Canvas (замена matplotlib)
# =========================================================

class CanvasGraph(tk.Canvas):
    """Лёгкий график-линия на голом tk.Canvas (без matplotlib).

    Поддерживает:
      * автоматический ресайз;
      * настраиваемый диапазон оси Y и подпись;
      * сглаженное добавление точек с авто-обрезкой истории;
      * маркер последнего значения с числовой подписью.
    """

    PADDING = (45, 12, 18, 22)   # left, right, top, bottom (px)

    def __init__(self, parent: tk.Misc, history: int = 100, **kw):
        super().__init__(parent, bg='white', highlightthickness=1,
                         highlightbackground='#cccccc', **kw)
        self.history = history
        self.values: List[float] = []
        self.y_min = -120.0
        self.y_max = -50.0
        self.unit = "dBm"
        self.title = "RSRP"
        self.bind("<Configure>", lambda e: self._redraw())

    def configure_axes(self, y_min: float, y_max: float,
                       unit: str, title: str) -> None:
        self.y_min, self.y_max = float(y_min), float(y_max)
        self.unit, self.title = unit, title
        self.values.clear()
        self._redraw()

    def push(self, val: float) -> None:
        self.values.append(float(val))
        if len(self.values) > self.history:
            self.values.pop(0)
        self._redraw()

    def clear(self) -> None:
        self.values.clear()
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 50:
            return
        pl, pr, pt, pb = self.PADDING
        plot_w, plot_h = w - pl - pr, h - pt - pb
        if plot_w <= 0 or plot_h <= 0:
            return

        # Заголовок (верх-лево)
        self.create_text(pl, 3, anchor='nw',
                         text=f"{self.title} ({self.unit})",
                         font=("Segoe UI", 9, "bold"), fill='#333')

        # Сетка + подписи оси Y (5 уровней)
        for i in range(5):
            y = pt + plot_h * i / 4
            v = self.y_max - (self.y_max - self.y_min) * i / 4
            self.create_line(pl, y, w - pr, y, fill='#ececec')
            self.create_text(pl - 3, y, anchor='e', text=f"{v:g}",
                             font=("", 8), fill='#666')

        # Базовая линия X
        self.create_line(pl, h - pb, w - pr, h - pb, fill='#888')
        self.create_text((pl + w - pr) / 2, h - 3, anchor='s',
                         text=f"последние {self.history} точек",
                         font=("", 8), fill='#888')

        if not self.values:
            return

        # Точки
        span = max(self.history - 1, 1)
        rng = max(self.y_max - self.y_min, 1e-9)
        pts: List[float] = []
        for i, v in enumerate(self.values):
            x = pl + plot_w * i / span
            v_cl = max(self.y_min, min(self.y_max, v))
            y = (h - pb) - plot_h * (v_cl - self.y_min) / rng
            pts.extend([x, y])

        if len(pts) >= 4:
            self.create_line(*pts, fill='#0078D7', width=2)
        # Маркер последнего значения
        lx, ly = pts[-2], pts[-1]
        self.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                         fill='#0078D7', outline='')
        self.create_text(w - pr - 5, pt + 4, anchor='ne',
                         text=f"{self.values[-1]:g} {self.unit}",
                         font=("Segoe UI", 9, "bold"), fill='#0078D7')


# =========================================================
# ОСНОВНОЙ КЛАСС
# =========================================================

class Hua4GMon:
    def __init__(self, root: tk.Tk, default_ip: str = "192.168.8.1",
                 default_password: str = ""):
        self.root = root
        self.root.title(f"{APP_NAME} v{__version__}")
        self.root.geometry("900x720")
        self.root.minsize(820, 650)

        # ---- Thread sync primitives ----
        self._stop_event = threading.Event()
        self._data_lock = threading.Lock()
        self.monitor_thread: Optional[threading.Thread] = None
        self._interval_seconds: float = 1.0

        # ---- Connection state ----
        self.connected = False
        self.is_monitoring = False
        self.client: Optional[Client] = None
        self.last_data: Dict[str, Any] = {}
        self.device_info: Dict[str, Any] = {}
        self.start_time: Optional[float] = None
        self.roof_win: Optional[tk.Toplevel] = None

        # Cached credentials (live only in RAM, never written to disk)
        self._cached_ip: str = ""
        self._cached_pw: str = ""

        # ---- Monitoring buffers ----
        self.dynamic_params = ['rsrp', 'rssi', 'sinr', 'rsrq']
        self.peak_values: Dict[str, Any] = {p: '-' for p in self.dynamic_params}
        self.values: Dict[str, List[float]] = {p: [] for p in self.dynamic_params}
        self.session_log: List[Dict[str, Any]] = []
        self.dir_history: List[float] = []

        # ---- Reconnect ----
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL

        # Defaults from CLI
        self.default_ip = default_ip
        self.default_password = default_password

        # Состояние, которое должно пережить пересоздание UI при смене языка
        self._saved_ontop = False

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.setup_ui()

        if default_password:
            # CLI: автоподключение
            self.root.after(200, self.start_connect)

    # =====================================================
    # UI BUILD
    # =====================================================

    def setup_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        # Верхняя строка статуса
        self.top_bar = ttk.Frame(self.root)
        self.top_bar.pack(fill=tk.X, padx=5, pady=2)
        self.status_label = ttk.Label(
            self.top_bar, text=t("Отключено"), foreground='red',
            font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side=tk.LEFT, padx=5)

        # Переключатель языка (справа). Меняет язык на лету.
        self._lang_code_by_name = {name: code
                                   for code, name in LANGUAGES.items()}
        self.lang_var = tk.StringVar(
            value=LANGUAGES.get(current_language(), "Русский"))
        lang_cb = ttk.Combobox(
            self.top_bar, textvariable=self.lang_var,
            values=list(LANGUAGES.values()),
            state='readonly', width=10)
        lang_cb.pack(side=tk.RIGHT, padx=5)
        lang_cb.bind("<<ComboboxSelected>>", self._on_language_change)
        ttk.Label(self.top_bar, text=t("Язык:")).pack(side=tk.RIGHT)

        self.ontop_var = tk.BooleanVar(value=self._saved_ontop)
        ttk.Checkbutton(self.top_bar, text=t("Поверх окон"),
                        variable=self.ontop_var,
                        command=self.toggle_on_top).pack(side=tk.RIGHT, padx=5)

        # Вкладки
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

    # =====================================================
    # СМЕНА ЯЗЫКА (пересоздание UI)
    # =====================================================

    def _on_language_change(self, _event=None) -> None:
        """Пользователь выбрал язык в combobox — применяем и пересоздаём UI."""
        code = self._lang_code_by_name.get(self.lang_var.get())
        if not code or code == current_language():
            return
        set_language(code)
        self.rebuild_ui()

    def rebuild_ui(self) -> None:
        """Пересоздаёт верхнюю панель и вкладки на текущем языке.

        Выполняется в главном потоке Tk (по клику), как и refresh_ui
        (через root.after), поэтому гонок за ссылками на виджеты нет.
        Накопленные данные (пики, лог сессии, история) живут в self и
        переживают пересоздание; пересоздаются только виджеты.
        """
        # 1. Снимок пользовательского ввода/выбора
        snap = {
            'ip': self.ip_entry.get(),
            'pw': self.password_entry.get(),
            'interval': self.update_interval.get(),
            'reconnect': self.reconnect_var.get(),
            'ontop': self.ontop_var.get(),
            'graph_param': self.graph_param.get(),
            'antenna': self.antenna_var.get(),
            'bands': {n: v.get() for n, v in self.band_checkboxes.items()},
            'tab': self.notebook.index(self.notebook.select()),
        }
        self._saved_ontop = snap['ontop']

        # 2. Снести старые виджеты верхнего уровня
        self.notebook.destroy()
        self.top_bar.destroy()

        # 3. Построить заново на новом языке
        self.setup_ui()

        # 4. Восстановить ввод/выбор
        self.ip_entry.delete(0, tk.END)
        self.ip_entry.insert(0, snap['ip'])
        self.password_entry.delete(0, tk.END)
        self.password_entry.insert(0, snap['pw'])
        self.update_interval.set(snap['interval'])
        self.reconnect_var.set(snap['reconnect'])
        self.graph_param.set(snap['graph_param'])
        # antenna_var хранит локализованную метку — переустановим по индексу
        ant_keys = list(ANTENNA_MODES.keys())
        if snap['antenna'] in ant_keys:
            self.antenna_var.set(snap['antenna'])
        for name, val in snap['bands'].items():
            if name in self.band_checkboxes:
                self.band_checkboxes[name].set(val)
        try:
            self.notebook.select(snap['tab'])
        except tk.TclError:
            pass
        if snap['ontop']:
            self.toggle_on_top()

        # 5. Восстановить визуальное состояние подключения
        if self.connected:
            self.connect_button.config(text=t("⏹ Отключиться"))
            self.status_label.config(text=t("Подключено"), foreground='green')
            for key, lbl in self.sim_labels.items():
                raw = self.device_info.get(key, '')
                lbl.config(text=str(raw) if raw not in (None, '')
                           else t("Нет данных"))

    def build_settings_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_settings,
                               text=t("Параметры роутера"), padding=10)
        frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(frame, text=t("IP адрес:")).grid(
            row=0, column=0, sticky='e', padx=5, pady=5)
        self.ip_entry = ttk.Entry(frame, width=25)
        self.ip_entry.insert(0, self.default_ip)
        self.ip_entry.grid(row=0, column=1, sticky='w', padx=5)
        self.ip_entry.bind("<Return>", lambda e: self.password_entry.focus())

        ttk.Label(frame, text=t("Пароль:")).grid(
            row=1, column=0, sticky='e', padx=5, pady=5)
        self.password_entry = ttk.Entry(frame, show="*", width=25)
        if self.default_password:
            self.password_entry.insert(0, self.default_password)
        self.password_entry.grid(row=1, column=1, sticky='w', padx=5)
        # Enter в поле пароля — подключиться
        self.password_entry.bind("<Return>", lambda e: self.start_connect())

        ttk.Label(frame, text=t("Опрос (сек):")).grid(
            row=2, column=0, sticky='e', padx=5, pady=5)
        self.update_interval = tk.StringVar(value='1')
        self.update_interval.trace_add('write',
                                       lambda *a: self._sync_interval())
        ttk.Combobox(frame, textvariable=self.update_interval,
                     values=['0.5', '1', '2', '5'],
                     state='readonly', width=5).grid(
            row=2, column=1, sticky='w', padx=5)

        self.reconnect_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text=t("Авто-переподключение при обрыве"),
                        variable=self.reconnect_var).grid(
            row=3, column=0, columnspan=2, sticky='w', padx=5, pady=5)

        btn_frame = ttk.Frame(self.tab_settings)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        self.connect_button = ttk.Button(
            btn_frame, text=t("🚀 Подключиться"), command=self.start_connect)
        self.connect_button.pack(side=tk.LEFT, padx=5)

        info = ttk.LabelFrame(self.tab_settings,
                              text=t("Подключение и частые ошибки"),
                              padding=10)
        info.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(info, wraplength=820, justify="left", text=t(
            "IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 "
            "или 192.168.3.1). Логин: admin, пароль — на наклейке роутера.\n"
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
            "• Таймаут / нет ответа — проверьте, что ноутбук подключён "
            "к Wi-Fi или USB именно этого роутера и IP введён верно."
        )).pack(anchor='w')

    def build_monitor_tab(self) -> None:
        # Здоровье связи — всегда видна сверху
        self.health_frame = ttk.LabelFrame(
            self.tab_monitor, text=t("Общее качество связи"), padding=10)
        self.health_frame.pack(fill=tk.X, padx=10, pady=5)
        self.health_progress = ttk.Progressbar(
            self.health_frame, orient="horizontal", mode="determinate")
        self.health_progress.pack(fill=tk.X, side=tk.TOP, pady=5)
        self.health_text_lbl = tk.Label(
            self.health_frame, text=t("Подключитесь к роутеру"),
            font=("Segoe UI", 12, "bold"), fg="gray")
        self.health_text_lbl.pack(side=tk.TOP, pady=2)

        # 4 крупных индикатора (с пиком всегда)
        self.digits_frame = ttk.Frame(self.tab_monitor)
        self.digits_frame.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_vars: Dict[str, Dict[str, Any]] = {}
        for i, param in enumerate(self.dynamic_params):
            f = ttk.LabelFrame(self.digits_frame, text=param.upper(),
                               padding=5)
            f.grid(row=0, column=i, padx=5, sticky='nsew')
            self.digits_frame.columnconfigure(i, weight=1)
            val = tk.Label(f, text="-",
                           font=("Segoe UI", 20, "bold"), fg='gray')
            val.pack()
            status = tk.Label(f, text=t("Нет данных"),
                              font=("Segoe UI", 9, "bold"), fg='gray')
            status.pack(pady=2)
            peak = tk.Label(f, text=t("Пик: -"),
                            font=("Segoe UI", 8), fg='gray')
            peak.pack(side=tk.BOTTOM)
            self.lbl_vars[param] = {
                'val': val, 'status': status, 'peak': peak, 'frame': f}

        # Индикатор направления (главная фишка для монтажа)
        self.dir_frame = ttk.LabelFrame(
            self.tab_monitor,
            text=t("Тенденция RSRP (поворачивайте антенну)"), padding=8)
        self.dir_frame.pack(fill=tk.X, padx=10, pady=5)
        self.dir_label = tk.Label(
            self.dir_frame, text="—",
            font=("Segoe UI", 32, "bold"), fg='gray')
        self.dir_label.pack()
        self.dir_text = tk.Label(
            self.dir_frame, text=t("Накапливаю данные..."),
            font=("Segoe UI", 10), fg='gray')
        self.dir_text.pack()

        # Инструменты: джиттер, аудио-помощник, крышный режим
        self.tools_frame = ttk.Frame(self.tab_monitor)
        self.tools_frame.pack(fill=tk.X, padx=15, pady=5)
        self.jitter_label = ttk.Label(
            self.tools_frame, text=t("Джиттер: -"),
            font=("Segoe UI", 10, "bold"))
        self.geiger_var = tk.BooleanVar(value=False)
        self.geiger_cb = ttk.Checkbutton(
            self.tools_frame, text=t("🔊 Аудио-помощник"),
            variable=self.geiger_var)
        if not HAS_WINSOUND:
            self.geiger_cb.config(state='disabled',
                                  text=t("🔊 Аудио (ОС не поддерживается)"))
        ttk.Button(self.tools_frame, text=t("🖥 Крышный режим"),
                   command=self.toggle_roof_mode).pack(side=tk.RIGHT, padx=5)
        self.geiger_cb.pack(side=tk.RIGHT, padx=5)
        self.jitter_label.pack(side=tk.LEFT)

        # Управление графиком + экспорт
        self.ctrl_frame = ttk.Frame(self.tab_monitor)
        self.ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(self.ctrl_frame, text=t("График:")).pack(side=tk.LEFT)
        self.graph_param = tk.StringVar(value='rsrp')
        self.graph_cb = ttk.Combobox(
            self.ctrl_frame, textvariable=self.graph_param,
            values=self.dynamic_params, state='readonly', width=8)
        self.graph_cb.pack(side=tk.LEFT, padx=5)
        self.graph_cb.bind("<<ComboboxSelected>>", self.reset_graph)
        ttk.Button(self.ctrl_frame, text=t("Сбросить пики"),
                   command=self.reset_peaks).pack(side=tk.RIGHT, padx=5)
        ttk.Button(self.ctrl_frame, text=t("💾 Экспорт CSV"),
                   command=self.export_csv).pack(side=tk.RIGHT, padx=5)

        # График на голом tk.Canvas (без matplotlib)
        self.signal_graph = CanvasGraph(
            self.tab_monitor, history=GRAPH_HISTORY, height=180)
        self.signal_graph.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.setup_graph()

    def build_network_tab(self) -> None:
        band_frame = ttk.LabelFrame(
            self.tab_network, text=t("Фиксация частот (Band Lock)"),
            padding=10)
        band_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(band_frame, wraplength=800, justify='left', text=t(
            "ВНИМАНИЕ: фиксация диапазона может уменьшить покрытие. "
            "Применяйте, чтобы привязаться к лучшей вышке — сначала "
            "определите рабочий band на вкладке «Вышка».")).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))

        self.band_checkboxes: Dict[str, tk.BooleanVar] = {}
        row, col = 1, 0
        for band_name in BANDS:
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(band_frame, text=band_name,
                            variable=var).grid(
                row=row, column=col, sticky='w', padx=10, pady=2)
            self.band_checkboxes[band_name] = var
            col += 1
            if col > 2:
                col = 0
                row += 1
        # Равный вес трёх колонок — иначе они сжимаются по содержимому,
        # и кнопки ниже (columnspan=3) центрируются не по центру рамки.
        for c in range(3):
            band_frame.columnconfigure(c, weight=1)

        btn_frame = ttk.Frame(band_frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text=t("Применить Band Lock"),
                   command=self.apply_bands).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=t("Сбросить в AUTO"),
                   command=self.reset_bands).pack(side=tk.LEFT, padx=5)

        ant_frame = ttk.LabelFrame(self.tab_network,
                                   text=t("Переключение антенн"), padding=10)
        ant_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(ant_frame, text=t("Режим:")).pack(side=tk.LEFT, padx=5)
        # Значения антенны — ключи ANTENNA_MODES (русские), по ним работает
        # parse_antenna_value; их не переводим, чтобы не ломать логику.
        self.antenna_var = tk.StringVar(value="Авто")
        ttk.Combobox(ant_frame, textvariable=self.antenna_var,
                     values=list(ANTENNA_MODES.keys()),
                     state='readonly', width=15).pack(side=tk.LEFT, padx=5)
        ttk.Button(ant_frame, text=t("Применить"),
                   command=self.apply_antenna).pack(side=tk.LEFT, padx=5)

        # Управление роутером
        mgmt_frame = ttk.LabelFrame(self.tab_network,
                                    text=t("Управление роутером"), padding=10)
        mgmt_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(mgmt_frame, wraplength=820, justify='left', text=t(
            "Перезагрузка иногда нужна после Band Lock, переключения "
            "антенн или при «зависании» сетевой части. Через 1–2 минуты "
            "переподключитесь вручную.")).pack(anchor='w', pady=(0, 6))
        ttk.Button(mgmt_frame, text=t("🔄 Перезагрузить роутер"),
                   command=self.reboot_router).pack(side=tk.LEFT, padx=5)

    def build_tower_tab(self) -> None:
        info_frame = ttk.LabelFrame(
            self.tab_tower, text=t("Информация о станции"), padding=10)
        info_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        self.tower_labels: Dict[str, ttk.Label] = {}
        fields = [
            ('plmn', 'Оператор (PLMN)'),
            ('band', 'Рабочий Band (LTE)'),
            ('earfcn', 'EARFCN (канал DL)'),
            ('aggregation', 'Агрегация (CA)'),
            ('dlbandwidth', 'Ширина канала (DL)'),
            ('pci', 'Сектор антенны (PCI)'),
            ('enodeb', 'eNodeB (Вышка)'),
            ('sector', 'Cell (Локальный сектор)'),
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(info_frame, text=f"{t(name)}:",
                      font=("", 10, "bold")).grid(
                row=i, column=0, sticky='e', pady=4, padx=5)
            lbl = ttk.Label(info_frame, text="-", font=("", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=4, padx=5)
            self.tower_labels[key] = lbl

        # SIM / Устройство — статическая инфа, заполняется при подключении
        sim_frame = ttk.LabelFrame(
            self.tab_tower, text=t("SIM / Устройство"), padding=10)
        sim_frame.pack(fill=tk.X, padx=10, pady=5)
        self.sim_labels: Dict[str, ttk.Label] = {}
        sim_fields = [
            ('Imei', 'IMEI (роутер)'),
            ('Imsi', 'IMSI (SIM)'),
            ('Iccid', 'ICCID (SIM-карта)'),
            ('Msisdn', 'Номер телефона'),
            ('SerialNumber', 'Серийный номер'),
            ('DeviceName', 'Модель'),
            ('SoftwareVersion', 'Прошивка'),
        ]
        for i, (key, name) in enumerate(sim_fields):
            ttk.Label(sim_frame, text=f"{t(name)}:",
                      font=("", 10, "bold")).grid(
                row=i, column=0, sticky='e', pady=3, padx=5)
            lbl = ttk.Label(sim_frame, text="-",
                             font=("Consolas", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=3, padx=5)
            self.sim_labels[key] = lbl

        btn_frame = ttk.Frame(self.tab_tower)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text=t("🗺 Открыть на CellMapper"),
                   command=self.open_cellmapper).pack(side=tk.LEFT, padx=5)

    def build_status_tab(self) -> None:
        stat_frame = ttk.LabelFrame(
            self.tab_status, text=t("Мониторинг железа и трафика"),
            padding=10)
        stat_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.stat_labels: Dict[str, ttk.Label] = {}
        fields = [
            ('uptime', 'Время сессии'),
            ('temp', 'Температура чипа'),
            ('dl_rate', 'Скорость (Download)'),
            ('ul_rate', 'Скорость (Upload)'),
            ('total_dl', 'Скачано за сессию'),
            ('total_ul', 'Отдано за сессию'),
            ('rsrp_min', 'RSRP мин / макс'),
            ('sinr_min', 'SINR мин / макс'),
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(stat_frame, text=f"{t(name)}:",
                      font=("", 10, "bold")).grid(
                row=i, column=0, sticky='e', pady=6, padx=5)
            lbl = ttk.Label(stat_frame, text="-", font=("", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=6, padx=5)
            self.stat_labels[key] = lbl

    def build_whitelist_tab(self) -> None:
        intro = ttk.LabelFrame(self.tab_whitelist,
                               text=t("Перед проверкой"), padding=10)
        intro.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(intro, wraplength=820, justify='left', text=t(
            "⚠ Ноутбук должен быть подключён к Wi-Fi или USB именно этого "
            "роутера — иначе тест измерит чужой канал.\n"
            "• Применимо только для РФ.\n"
            "• Проверка занимает 1–3 секунды и ничего не меняет в роутере."
        )).pack(anchor='w')

        # Кнопка + бегущая строка статуса
        ctrl = ttk.Frame(self.tab_whitelist)
        ctrl.pack(fill=tk.X, padx=10, pady=5)
        self.wl_button = ttk.Button(
            ctrl, text=t("🔍 Проверить сейчас"),
            command=self._start_whitelist_check)
        self.wl_button.pack(side=tk.LEFT, padx=5)
        self.wl_progress = ttk.Progressbar(
            ctrl, orient="horizontal", mode="indeterminate", length=200)
        self.wl_progress.pack(side=tk.LEFT, padx=10)

        # Большой статус-вердикт
        verdict_frame = ttk.LabelFrame(
            self.tab_whitelist, text=t("Вердикт"), padding=12)
        verdict_frame.pack(fill=tk.X, padx=10, pady=5)
        self.wl_title = tk.Label(verdict_frame, text=t("Не проверялось"),
                                  font=("Segoe UI", 14, "bold"), fg='gray')
        self.wl_title.pack(anchor='w')
        self.wl_detail = tk.Label(verdict_frame, text="—",
                                   font=("Segoe UI", 10),
                                   fg='gray', wraplength=820, justify='left')
        self.wl_detail.pack(anchor='w', pady=(4, 0))

        # Детализированные результаты по хостам
        details = ttk.Frame(self.tab_whitelist)
        details.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        white_frame = ttk.LabelFrame(details, text=t("✅ В белых списках"),
                                      padding=8)
        white_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                         padx=(0, 5))
        self.wl_white_labels: Dict[str, tk.Label] = {}
        for host, port in WHITELIST_HOSTS_RU:
            lbl = tk.Label(white_frame,
                           text=f"{host}:{port} — ⏳ {t('не проверено')}",
                           font=("Consolas", 10), fg='gray', anchor='w')
            lbl.pack(fill=tk.X, padx=4, pady=2)
            self.wl_white_labels[host] = lbl

        neut_frame = ttk.LabelFrame(details, text=t("⚪ Нейтральные"),
                                     padding=8)
        neut_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                        padx=(5, 0))
        self.wl_neut_labels: Dict[str, tk.Label] = {}
        for host, port in CONTROL_HOSTS_NEUTRAL:
            lbl = tk.Label(neut_frame,
                           text=f"{host}:{port} — ⏳ {t('не проверено')}",
                           font=("Consolas", 10), fg='gray', anchor='w')
            lbl.pack(fill=tk.X, padx=4, pady=2)
            self.wl_neut_labels[host] = lbl

    def _start_whitelist_check(self) -> None:
        self.wl_button.config(state='disabled')
        self.wl_progress.start(10)
        self.wl_title.config(text=t("Проверка…"), fg='orange')
        self.wl_detail.config(text=t("Подождите 1–3 секунды."), fg='gray')
        for lbl in (list(self.wl_white_labels.values())
                    + list(self.wl_neut_labels.values())):
            lbl.config(text=lbl.cget('text').split(' — ')[0] + " — ⏳",
                       fg='gray')
        threading.Thread(target=self._whitelist_task, daemon=True).start()

    def _whitelist_task(self) -> None:
        """В фоне опрашивает все цели и шлёт результаты обратно в UI."""
        white_results: List[Tuple[str, bool]] = []
        white_details: Dict[str, str] = {}
        for host, port in WHITELIST_HOSTS_RU:
            ok, detail = tcp_reachable(host, port)
            white_results.append((host, ok))
            white_details[host] = detail

        neutral_results: List[Tuple[str, bool]] = []
        neutral_details: Dict[str, str] = {}
        for host, port in CONTROL_HOSTS_NEUTRAL:
            ok, detail = tcp_reachable(host, port)
            neutral_results.append((host, ok))
            neutral_details[host] = detail

        self.root.after(0, lambda: self._render_whitelist_results(
            white_results, white_details,
            neutral_results, neutral_details))

    def _render_whitelist_results(
            self,
            white_results: List[Tuple[str, bool]],
            white_details: Dict[str, str],
            neutral_results: List[Tuple[str, bool]],
            neutral_details: Dict[str, str]) -> None:
        self.wl_progress.stop()
        self.wl_button.config(state='normal')

        for host, ok in white_results:
            lbl = self.wl_white_labels[host]
            sym, col = ("✅", '#00b894') if ok else ("❌", '#d63031')
            lbl.config(text=f"{host} — {sym} {white_details[host]}", fg=col)
        for host, ok in neutral_results:
            lbl = self.wl_neut_labels[host]
            sym, col = ("✅", '#00b894') if ok else ("❌", '#d63031')
            lbl.config(text=f"{host} — {sym} {neutral_details[host]}", fg=col)

        title, detail, color = analyze_whitelist_results(
            white_results, neutral_results)
        self.wl_title.config(text=t(title), fg=color)
        self.wl_detail.config(text=detail, fg='#444444')

    # =====================================================
    # Misc helpers
    # =====================================================

    def toggle_on_top(self) -> None:
        self.root.attributes('-topmost', self.ontop_var.get())

    def _sync_interval(self) -> None:
        try:
            self._interval_seconds = float(self.update_interval.get())
        except (ValueError, tk.TclError):
            self._interval_seconds = 1.0

    # =====================================================
    # CONNECTION
    # =====================================================

    def start_connect(self) -> None:
        if self.connected:
            self.disconnect()
            return
        ip = self.ip_entry.get().strip()
        if not is_valid_ip(ip):
            messagebox.showerror(
                t("Ошибка"),
                t("Неверный IP-адрес: {ip}\nПример: 192.168.8.1").format(
                    ip=repr(ip)))
            return
        self._cached_ip = ip
        self._cached_pw = self.password_entry.get()
        self._sync_interval()
        self.auto_reconnect = self.reconnect_var.get()
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.connect_button.config(state='disabled')
        self.status_label.config(text=t("Подключение..."), foreground='orange')
        threading.Thread(target=self._connect_thread, daemon=True).start()

    def _connect_thread(self) -> None:
        url = f"http://{self._cached_ip}"
        try:
            client = Client(Connection(
                url, username='admin',
                password=self._cached_pw, timeout=4))
            info = client.device.information() or {}    # верификация + кеш
            self.client = client
            self.device_info = info
            self.connected = True
            self.is_monitoring = True
            self.start_time = time.time()
            self._stop_event.clear()
            self.root.after(0, self._on_connected_success)
            self.monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
        except Exception as e:
            logger.exception("Connect failed")
            self.root.after(0, lambda err=str(e): self._on_connected_fail(err))

    def _on_connected_success(self) -> None:
        self.connect_button.config(state='normal', text=t("⏹ Отключиться"))
        self.status_label.config(text=t("Подключено"), foreground='green')
        self.notebook.select(self.tab_monitor)
        self.reset_graph()
        self.session_log.clear()
        self.dir_history.clear()
        self.peak_values = {p: '-' for p in self.dynamic_params}
        # Заполняем SIM/Device-лейблы из закешированного device.information()
        for key, lbl in self.sim_labels.items():
            raw = self.device_info.get(key, '')
            lbl.config(text=str(raw) if raw not in (None, '') else t("Н/Д"))

    def _on_connected_fail(self, error: str) -> None:
        self.connect_button.config(state='normal', text=t("🚀 Подключиться"))
        self.status_label.config(text=t("Ошибка"), foreground='red')
        snippet = error if len(error) < 200 else error[:200] + "..."
        messagebox.showerror(
            t("Ошибка подключения"),
            t("Связь с роутером не удалась:\n\n{err}").format(err=snippet))

    def disconnect(self) -> None:
        """Корректная остановка: сначала глушим поток, потом обнуляем клиент."""
        was_connected = self.connected
        self.is_monitoring = False
        self.connected = False
        self.auto_reconnect = False
        self._stop_event.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            wait = self._interval_seconds + 2.0
            self.monitor_thread.join(timeout=wait)
        self.monitor_thread = None
        if self.client is not None:
            try:
                self.client.user.logout()
            except Exception:
                logger.debug("Logout failed (ignored)", exc_info=True)
            self.client = None
        self.device_info = {}
        self.connect_button.config(text=t("🚀 Подключиться"), state='normal')
        if was_connected:
            self.status_label.config(text=t("Отключено"), foreground='red')
            self.health_text_lbl.config(text=t("Подключитесь к роутеру"),
                                        fg="gray")
            self.health_progress.config(value=0)
            self.dir_label.config(text="—", fg='gray')
            self.dir_text.config(text=t("Нет данных"), fg='gray')
            for lbl in self.sim_labels.values():
                lbl.config(text="-")

    # =====================================================
    # MONITOR LOOP (фоновый поток)
    # =====================================================

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            client = self.client
            if client is None:
                break
            try:
                sig = client.device.signal()
                plmn = client.net.current_plmn()
                status = client.monitoring.status()
                traffic = client.monitoring.traffic_statistics()
                data = {**(sig or {}), **(plmn or {}),
                        **(status or {}), **(traffic or {})}
                data['plmn'] = (plmn or {}).get(
                    'Numeric', data.get('plmn', ''))

                enodeb, sector = parse_cell_id(data.get('cell_id'))
                if enodeb is not None:
                    data['enodeb'] = enodeb
                    data['sector'] = sector

                band_str = str(data.get('band', ''))
                data['aggregation'] = ("Активна"
                                       if ("+" in band_str
                                           or "CA" in band_str)
                                       else "Нет (Single)")

                with self._data_lock:
                    self.last_data = data
                self.root.after(0, self.refresh_ui)
                # Удачный тик — сбрасываем backoff
                self.reconnect_delay = RECONNECT_DELAY_INITIAL
            except Exception as e:
                logger.warning("Monitor tick failed: %s", e)
                self.root.after(0, lambda: self.status_label.config(
                    text=t("Таймаут API..."), foreground='orange'))
                if self.auto_reconnect and not self._stop_event.is_set():
                    self._try_reconnect()
                else:
                    break

            if self._stop_event.wait(self._interval_seconds):
                break

    def _try_reconnect(self) -> None:
        """Одна попытка переподключения с экспоненциальным backoff."""
        if self._stop_event.is_set():
            return
        delay = min(self.reconnect_delay, RECONNECT_DELAY_MAX)
        self.root.after(0, lambda d=delay: self.status_label.config(
            text=t("Переподключение через {d:.0f}с...").format(d=d),
            foreground='orange'))
        if self._stop_event.wait(delay):
            return
        try:
            new_client = Client(Connection(
                f"http://{self._cached_ip}", username='admin',
                password=self._cached_pw, timeout=4))
            new_client.device.information()
            self.client = new_client
            self.reconnect_delay = RECONNECT_DELAY_INITIAL
            self.root.after(0, lambda: self.status_label.config(
                text=t("Подключено"), foreground='green'))
        except Exception as e:
            logger.warning("Reconnect failed: %s", e)
            self.reconnect_delay = min(self.reconnect_delay * 2,
                                       RECONNECT_DELAY_MAX)

    # =====================================================
    # UI REFRESH (главный поток, через root.after)
    # =====================================================

    def refresh_ui(self) -> None:
        if not self.is_monitoring:
            return
        self.status_label.config(text=t("Подключено"), foreground='green')

        with self._data_lock:
            data = dict(self.last_data)

        current_vals: Dict[str, Optional[float]] = {
            p: extract_number(data.get(p)) for p in self.dynamic_params
        }

        for p in self.dynamic_params:
            val_num = current_vals[p]
            if val_num is None:
                continue
            status_text, color, _ = evaluate_signal(p, val_num)
            self.lbl_vars[p]['val'].config(
                text=f"{val_num:g} {self._unit(p)}", fg=color)
            self.lbl_vars[p]['status'].config(
                text=t(status_text).upper(), fg=color)
            if (self.peak_values[p] == '-' or val_num > self.peak_values[p]):
                self.peak_values[p] = val_num
            self.lbl_vars[p]['peak'].config(
                text=t("Пик: {v}").format(v=self.peak_values[p]))
            self.values[p].append(val_num)
            if len(self.values[p]) > GRAPH_HISTORY:
                self.values[p].pop(0)

        # Индикатор направления (по RSRP)
        rsrp = current_vals.get('rsrp')
        if rsrp is not None:
            self.dir_history.append(rsrp)
            if len(self.dir_history) > DIRECTION_LOOKBACK * 2:
                self.dir_history.pop(0)
            self._update_direction()

        # Здоровье связи — всегда обновляется. summary — шаблон-ключ с {pct}.
        score, summary, color = calculate_overall_health(
            rsrp, current_vals.get('sinr'))
        self.health_progress.config(value=score)
        self.health_text_lbl.config(text=t(summary).format(pct=score),
                                    fg=color)

        # Джиттер — всегда обновляется
        if len(self.values['rsrp']) >= JITTER_WINDOW:
            recent = self.values['rsrp'][-JITTER_WINDOW:]
            jitter = max(recent) - min(recent)
            jcol = ('green' if jitter < 3
                    else 'orange' if jitter < 7 else 'red')
            self.jitter_label.config(
                text=t("Джиттер: {j:.1f} dB (стабильность сигнала)").format(
                    j=jitter),
                foreground=jcol)

        # Аудио-помощник: частота зависит от близости к ПИКУ RSRP
        if HAS_WINSOUND and self.geiger_var.get() and rsrp is not None:
            best = self.peak_values['rsrp']
            if isinstance(best, (int, float)):
                # 0..30 dB ниже пика → 2500..300 Гц (чем ближе к пику — выше)
                delta = max(0.0, best - rsrp)
                freq = max(300, min(2500, int(2500 - delta * 70)))
                threading.Thread(target=winsound.Beep,
                                 args=(freq, 80), daemon=True).start()

        # График — толкаем последнее значение всегда
        if self.start_time is not None:
            param = self.graph_param.get()
            val_now = current_vals.get(param)
            if val_now is not None:
                self.signal_graph.push(val_now)

        # Зеркало в Roof Mode
        if self.roof_win is not None and self.roof_win.winfo_exists():
            r = rsrp
            s = current_vals.get('sinr')
            _, r_col, _ = evaluate_signal('rsrp', r)
            _, s_col, _ = evaluate_signal('sinr', s)
            self.r_lbl_rsrp.config(
                text=f"RSRP: {r if r is not None else '-'}", fg=r_col)
            self.r_lbl_sinr.config(
                text=f"SINR: {s if s is not None else '-'}", fg=s_col)
            arrow, color = self._direction_glyph()
            self.r_dir.config(text=arrow, fg=color)
            # Лаконичная сводка по вышке в углу
            enb = data.get('enodeb', '-')
            sec = data.get('sector', '-')
            band_short = format_band_label(
                data.get('band'),
                data.get('earfcn', data.get('Earfcn', '-')))
            self.r_tower.config(
                text=f"eNodeB: {enb}\nCell: {sec}\n{band_short}")

        # Информация о вышке
        earfcn_raw = data.get('earfcn', data.get('Earfcn', '-'))
        for key, lbl in self.tower_labels.items():
            if key == 'plmn':
                val = str(data.get('plmn', '-'))
                if val != '-' and len(val) >= 5:
                    op = PLMN_MAP.get(val, t("Неизвестный оператор"))
                    val = f"{val} ({op})"
            elif key == 'band':
                val = format_band_label(data.get('band'), earfcn_raw)
            elif key == 'earfcn':
                val = (str(earfcn_raw)
                       if earfcn_raw not in (None, '', '-') else '-')
            elif key == 'aggregation':
                # Значение приходит русским из monitor loop — переводим
                val = t(str(data.get(key, '-')))
            else:
                val = str(data.get(key, '-'))
            lbl.config(text=val)

        # Статистика
        self.stat_labels['dl_rate'].config(
            text=format_rate_mbps(data.get('CurrentDownloadRate', 0)))
        self.stat_labels['ul_rate'].config(
            text=format_rate_mbps(data.get('CurrentUploadRate', 0)))
        self.stat_labels['total_dl'].config(
            text=format_bytes_mb(data.get('TotalDownload', 0)))
        self.stat_labels['total_ul'].config(
            text=format_bytes_mb(data.get('TotalUpload', 0)))
        up_sec = data.get('CurrentConnectTime',
                          data.get('ConnectionTime', 0))
        try:
            up_sec_int = int(up_sec)
            uptime_str = (str(datetime.timedelta(seconds=up_sec_int))
                          if up_sec_int > 0 else "-")
        except (TypeError, ValueError):
            uptime_str = "-"
        self.stat_labels['uptime'].config(text=uptime_str)
        self.stat_labels['temp'].config(
            text=str(data.get('Temperature', t('Н/Д'))))
        for p, lbl_key in (('rsrp', 'rsrp_min'), ('sinr', 'sinr_min')):
            vals = self.values[p]
            if vals:
                self.stat_labels[lbl_key].config(
                    text=f"{min(vals):g} / {max(vals):g} {self._unit(p)}")

        # Лог сессии (в RAM, для экспорта в CSV)
        if len(self.session_log) < SESSION_LOG_MAX:
            self.session_log.append({
                'ts': datetime.datetime.now().isoformat(timespec='seconds'),
                **{p: current_vals.get(p) for p in self.dynamic_params},
                'plmn': data.get('plmn', ''),
                'enodeb': data.get('enodeb', ''),
                'sector': data.get('sector', ''),
                'band': data.get('band', ''),
                'pci': data.get('pci', ''),
            })

    def _update_direction(self) -> None:
        arrow, color = self._direction_glyph()
        text = {
            "↑": t("Сигнал улучшается — продолжайте в том же направлении"),
            "↓": t("Сигнал ухудшается — поверните обратно"),
            "→": t("Сигнал стабилен — зафиксируйте антенну"),
            "—": t("Накапливаю данные..."),
        }.get(arrow, "")
        self.dir_label.config(text=arrow, fg=color)
        self.dir_text.config(text=text, fg=color)

    def _direction_glyph(self) -> Tuple[str, str]:
        if len(self.dir_history) < DIRECTION_LOOKBACK * 2:
            return "—", "gray"
        recent = self.dir_history[-DIRECTION_LOOKBACK:]
        older = self.dir_history[-DIRECTION_LOOKBACK * 2:-DIRECTION_LOOKBACK]
        delta = (sum(recent) / len(recent)) - (sum(older) / len(older))
        if delta >= 1.0:
            return "↑", "#00b894"
        if delta <= -1.0:
            return "↓", "#d63031"
        return "→", "#fdcb6e"

    # =====================================================
    # NETWORK / ANTENNA
    # =====================================================

    def apply_bands(self) -> None:
        if self.client is None:
            messagebox.showwarning(t("Ошибка"),
                                   t("Сначала подключитесь к роутеру."))
            return
        mask = sum(BANDS[n] for n, v in self.band_checkboxes.items()
                   if v.get())
        if mask == 0:
            messagebox.showwarning(t("Внимание"),
                                   t("Выберите хотя бы один диапазон!"))
            return
        hex_mask = format(mask, 'X')
        client = self.client

        def task():
            try:
                client.net.set_net_mode(hex_mask, NETBAND_AUTO_MASK,
                                        NETMODE_LTE_ONLY)
                self.root.after(0, lambda: messagebox.showinfo(
                    t("Успех"),
                    t("Band Lock применён (mask: {mask}).").format(
                        mask=hex_mask)))
            except Exception as e:
                logger.exception("Band lock failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    t("Ошибка"),
                    t("Роутер отклонил команду:\n{err}").format(err=err)))
        threading.Thread(target=task, daemon=True).start()

    def reset_bands(self) -> None:
        if self.client is None:
            return
        client = self.client

        def task():
            try:
                client.net.set_net_mode(LTEBAND_AUTO_ALL,
                                        NETBAND_AUTO_MASK, NETMODE_AUTO)
                self.root.after(0, lambda: messagebox.showinfo(
                    t("Успех"), t("Сеть сброшена в AUTO.")))
            except Exception as e:
                logger.exception("Reset bands failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    t("Ошибка"), err))
        threading.Thread(target=task, daemon=True).start()

    def apply_antenna(self) -> None:
        if self.client is None:
            messagebox.showwarning(t("Ошибка"),
                                   t("Сначала подключитесь к роутеру."))
            return
        ant_val = parse_antenna_value(self.antenna_var.get())
        if ant_val is None:
            messagebox.showerror(t("Ошибка"), t("Неизвестный режим антенны."))
            return
        client = self.client

        def task():
            try:
                # Сначала пытаемся через enum (новый API)
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
                self.root.after(0, lambda: messagebox.showinfo(
                    t("Успех"),
                    t("Тип антенны изменён: {mode}").format(
                        mode=self.antenna_var.get())))
            except Exception as e:
                logger.exception("Set antenna failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    t("Ошибка"), err))
        threading.Thread(target=task, daemon=True).start()

    def reboot_router(self) -> None:
        if self.client is None:
            messagebox.showwarning(t("Ошибка"),
                                   t("Сначала подключитесь к роутеру."))
            return
        if not messagebox.askyesno(
                t("Подтверждение"),
                t("Перезагрузить роутер?\n\nСоединение с интернетом "
                  "прервётся на 1–2 минуты. После загрузки переподключитесь "
                  "вручную.")):
            return
        client = self.client

        def task():
            try:
                client.device.reboot()
                # Роутер всё равно сейчас уйдёт — рвём соединение со стороны UI
                self.root.after(0, self.disconnect)
                self.root.after(100, lambda: messagebox.showinfo(
                    t("Перезагрузка"),
                    t("Команда отправлена. Роутер вернётся через "
                      "1–2 минуты.")))
            except Exception as e:
                logger.exception("Reboot failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    t("Ошибка"),
                    t("Не удалось перезагрузить:\n{err}").format(err=err)))
        threading.Thread(target=task, daemon=True).start()

    # =====================================================
    # EXTERNAL LOOKUPS
    # =====================================================

    def open_cellmapper(self) -> None:
        with self._data_lock:
            plmn = str(self.last_data.get('plmn', ''))
            enodeb = self.last_data.get('enodeb')
        if len(plmn) < 5 or enodeb is None:
            messagebox.showwarning(
                t("Внимание"),
                t("Недостаточно данных о вышке (нужны PLMN и eNodeB)."))
            return
        mcc, mnc = plmn[:3], plmn[3:]
        url = (f"https://www.cellmapper.net/map?MCC={mcc}&MNC={mnc}"
               f"&type=LTE&siteid={enodeb}")
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror(
                t("Ошибка"), t("Не открыть браузер: {e}").format(e=e))

    # =====================================================
    # ROOF MODE (полноэкранный)
    # =====================================================

    def toggle_roof_mode(self) -> None:
        if self.roof_win is not None and self.roof_win.winfo_exists():
            self._close_roof()
            return
        self.roof_win = tk.Toplevel(self.root)
        self.roof_win.attributes('-fullscreen', True)
        self.roof_win.configure(bg='black')
        self.roof_win.bind("<Escape>", lambda e: self._close_roof())
        self.roof_win.protocol("WM_DELETE_WINDOW", self._close_roof)
        tk.Label(self.roof_win, text=t("[ESC] для выхода"),
                 font=("Arial", 14), fg='gray',
                 bg='black').pack(pady=12)

        # Лаконичная инфа о вышке в правом верхнем углу:
        # eNodeB / сота / band — чтобы монтажник видел, к чему привязан,
        # не отвлекаясь от крупных цифр RSRP/SINR. До подключения —
        # заглушка с прочерками, чтобы было видно, что панель существует.
        self.r_tower = tk.Label(
            self.roof_win,
            text=f"{t('eNodeB (Вышка)')}: —\n"
                 f"{t('Cell (Локальный сектор)')}: —\n—",
            justify='right', anchor='ne',
            font=("Consolas", 16), bg='black', fg='#888')
        self.r_tower.place(relx=0.99, rely=0.02, anchor='ne')

        self.r_lbl_rsrp = tk.Label(
            self.roof_win, text="RSRP: -",
            font=("Consolas", 90, "bold"), bg='black', fg='white')
        self.r_lbl_rsrp.pack(expand=True)
        self.r_dir = tk.Label(
            self.roof_win, text="—",
            font=("Consolas", 140, "bold"), bg='black', fg='gray')
        self.r_dir.pack(expand=True)
        self.r_lbl_sinr = tk.Label(
            self.roof_win, text="SINR: -",
            font=("Consolas", 90, "bold"), bg='black', fg='white')
        self.r_lbl_sinr.pack(expand=True)

    def _close_roof(self) -> None:
        if self.roof_win is not None and self.roof_win.winfo_exists():
            self.roof_win.destroy()
        self.roof_win = None

    # =====================================================
    # CSV EXPORT
    # =====================================================

    def export_csv(self) -> None:
        if not self.session_log:
            messagebox.showinfo(
                t("Экспорт"),
                t("Лог сессии пуст. Подключитесь и подождите, пока "
                  "соберутся данные."))
            return
        default = f"hua4gmon-{datetime.datetime.now():%Y%m%d-%H%M%S}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All", "*.*")],
            initialfile=default)
        if not path:
            return
        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                fields = list(self.session_log[0].keys())
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(self.session_log)
            messagebox.showinfo(
                t("Экспорт"),
                t("Сохранено {n} записей в:\n{path}").format(
                    n=len(self.session_log), path=path))
        except OSError as e:
            messagebox.showerror(
                t("Ошибка"),
                t("Не удалось записать файл: {e}").format(e=e))

    # =====================================================
    # MISC HELPERS
    # =====================================================

    def setup_graph(self) -> None:
        param = self.graph_param.get()
        y_min, y_max = PARAM_RANGES.get(param, (-120, 0))
        self.signal_graph.configure_axes(
            y_min=y_min, y_max=y_max,
            unit=self._unit(param), title=param.upper())

    def reset_graph(self, _event=None) -> None:
        self.values = {p: [] for p in self.dynamic_params}
        self.setup_graph()

    def reset_peaks(self) -> None:
        self.peak_values = {p: '-' for p in self.dynamic_params}
        for p in self.dynamic_params:
            self.lbl_vars[p]['peak'].config(text=t("Пик: -"))

    @staticmethod
    def _unit(param: str) -> str:
        return "dBm" if param in ('rsrp', 'rssi') else "dB"

    # =====================================================
    # SHUTDOWN
    # =====================================================

    def on_closing(self) -> None:
        logger.info("Shutting down")
        self.disconnect()
        self._close_roof()
        try:
            self.root.quit()
        finally:
            try:
                self.root.destroy()
            except tk.TclError:
                pass


# =========================================================
# ВХОД
# =========================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=f"{APP_NAME} — портативный монитор LTE Huawei.")
    p.add_argument('--ip', default='192.168.8.1',
                   help='IP роутера (по умолчанию 192.168.8.1)')
    p.add_argument('--password', default='',
                   help='Пароль (если указан — автоподключение)')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Подробный лог в stderr')
    p.add_argument('--version', action='version',
                   version=f'{APP_NAME} {__version__}')
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        stream=sys.stderr)
    root = tk.Tk()
    app = Hua4GMon(root,
                   default_ip=args.ip,
                   default_password=args.password)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_closing()


if __name__ == "__main__":
    main()
