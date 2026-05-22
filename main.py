from __future__ import annotations

import argparse
import csv
import datetime
import logging
import re
import sys
import threading
import time
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

import requests
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection


__version__ = "1.2"
APP_NAME = "Hua4GMon"

logger = logging.getLogger(APP_NAME)


# =========================================================
# КОНСТАНТЫ
# =========================================================

PLMN_MAP: Dict[str, str] = {
    # Russia (MCC=250)
    '25001': 'МТС', '25002': 'МегаФон', '25011': 'Yota',
    '25020': 'Tele2', '25027': 'Летай', '25035': 'Мотив',
    '25039': 'Ростелеком', '25099': 'Билайн',
    # Belarus (MCC=257)
    '25701': 'A1 BY', '25702': 'MTS BY', '25704': 'life:)',
    # Kazakhstan (MCC=401)
    '40101': 'Beeline KZ', '40102': 'Kcell', '40177': 'Tele2 KZ',
    # Ukraine (MCC=255)
    '25501': 'Vodafone UA', '25502': 'Kyivstar', '25506': 'lifecell',
}

# LTE bitmask values used by Huawei set_net_mode()
BANDS: Dict[str, int] = {
    'B1 (2100 МГц)':   0x1,
    'B3 (1800 МГц)':   0x4,
    'B7 (2600 МГц)':   0x40,
    'B8 (900 МГц)':    0x80,
    'B20 (800 МГц)':   0x80000,
    'B38 (TDD 2600)':  0x2000000000,
    'B40 (TDD 2300)':  0x8000000000,
}

ANTENNA_MODES: Dict[str, int] = {
    "Авто": 0,
    "Внутренняя": 1,
    "Внешняя": 2,
    "Смешанная": 3,
}

# Network mode constants for set_net_mode
NETMODE_LTE_ONLY = '03'
NETMODE_AUTO = '00'
LTEBAND_AUTO_ALL = '7FFFFFFFFFFFFFFF'    # all LTE bands
NETBAND_AUTO_MASK = '3FFFFFFF'           # GSM/WCDMA/LTE auto

# Thresholds: [(min_value, label, color, percent_score), ...]
# Final entry with min_value=None is the catch-all.
SIGNAL_THRESHOLDS: Dict[str, List[Tuple[Optional[float], str, str, int]]] = {
    'rsrp': [(-80,  "Отличный",       "#00b894", 100),
             (-90,  "Хороший",        "#2ecc71", 80),
             (-100, "Средний",        "#fdcb6e", 50),
             (None, "Плохой",         "#d63031", 15)],
    'sinr': [(20,   "Идеальный",      "#00b894", 100),
             (13,   "Хороший",        "#2ecc71", 75),
             (0,    "Шумный",         "#fdcb6e", 40),
             (None, "Критичный",      "#d63031", 5)],
    'rssi': [(-65,  "Сильный",        "#00b894", 100),
             (-75,  "Нормальный",     "#2ecc71", 75),
             (-85,  "Слабый",         "#fdcb6e", 45),
             (None, "Очень слабый",   "#d63031", 10)],
    'rsrq': [(-6,   "Отличный",       "#00b894", 100),
             (-12,  "Стабильный",     "#2ecc71", 70),
             (-15,  "Потери",         "#fdcb6e", 40),
             (None, "Высокие потери", "#d63031", 10)],
}

PARAM_RANGES: Dict[str, Tuple[int, int]] = {
    'rsrp': (-120, -50),
    'rssi': (-110, -50),
    'rsrq': (-20, -3),
    'sinr': (-5, 30),
}

GRAPH_HISTORY = 100
JITTER_WINDOW = 5
SESSION_LOG_MAX = 10800        # ~3 часа при тике 1 с
RECONNECT_DELAY_INITIAL = 2.0
RECONNECT_DELAY_MAX = 30.0
DIRECTION_LOOKBACK = 3         # сколько тиков сравнивать для стрелки

IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')


# =========================================================
# ЧИСТЫЕ ФУНКЦИИ (легко тестируются отдельно)
# =========================================================

def is_valid_ip(s: str) -> bool:
    """Базовая валидация IPv4."""
    if not s or not IP_RE.match(s):
        return False
    return all(0 <= int(p) <= 255 for p in s.split('.'))


def evaluate_signal(param: str,
                    val: Optional[float]) -> Tuple[str, str, int]:
    """Возвращает (текст_статуса, цвет, процент_качества)."""
    if val is None:
        return "Нет данных", "gray", 0
    rules = SIGNAL_THRESHOLDS.get(param)
    if not rules:
        return "Н/Д", "gray", 0
    for threshold, text, color, pct in rules:
        if threshold is None or val >= threshold:
            return text, color, pct
    return "Н/Д", "gray", 0


def calculate_overall_health(rsrp: Optional[float],
                              sinr: Optional[float]
                              ) -> Tuple[int, str, str]:
    """Общая оценка качества связи на основе RSRP и SINR."""
    if rsrp is None or sinr is None:
        return 0, "Нет данных", "gray"
    _, _, r_pct = evaluate_signal('rsrp', rsrp)
    _, _, s_pct = evaluate_signal('sinr', sinr)
    overall = int(min(r_pct, s_pct) * 0.7 + max(r_pct, s_pct) * 0.3)
    overall = max(0, min(100, overall))
    if overall >= 85:
        return overall, f"Идеально ({overall}%) — 4K/онлайн-игры", "#00b894"
    if overall >= 65:
        return overall, f"Хорошо ({overall}%) — стабильный FullHD", "#2ecc71"
    if overall >= 35:
        return overall, f"Умеренно ({overall}%) — крутите антенну", "#fdcb6e"
    return overall, f"Плохо ({overall}%) — будет рваться!", "#d63031"


def extract_number(val: Any) -> Optional[float]:
    """Строгая извлечение числа. Не ведётся на строки вроде 'timeout 0'."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in ('-', 'None', 'N/A', 'NA'):
        return None
    # Допускаем знак, дробную часть и опциональный суффикс (dBm, %, dB и т.п.)
    m = re.fullmatch(r'(-?\d+(?:\.\d+)?)\s*[a-zA-Z%/]*', s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_cell_id(raw: Any) -> Tuple[Optional[int], Optional[int]]:
    """Парсит cell_id из Huawei API. Возвращает (eNodeB_id, sector)."""
    if raw is None or raw == '':
        return None, None
    s = str(raw).strip()
    try:
        if s.lower().startswith('0x'):
            cid = int(s, 16)
        elif any(c in 'abcdefABCDEF' for c in s):
            cid = int(s, 16)
        else:
            cid = int(s)
    except (ValueError, TypeError):
        return None, None
    # Отбрасываем явные "плохие" значения
    if cid <= 0 or cid >= 0xFFFFFFFF:
        return None, None
    if cid > 0x0FFFFFFF:     # > 28 бит — не LTE CID
        return None, None
    return cid // 256, cid % 256


def parse_antenna_value(label: str) -> Optional[int]:
    """Достаёт целочисленный код режима антенны из локализованной метки."""
    base = label.split('(')[0].strip()
    if base in ANTENNA_MODES:
        return ANTENNA_MODES[base]
    m = re.search(r'\((\d+)\)', label)
    if m:
        return int(m.group(1))
    return None


def format_bytes_mb(b: Any) -> str:
    try:
        return f"{int(b) / 1048576:.1f} МБ"
    except (TypeError, ValueError):
        return "-"


def format_rate_mbps(bps: Any) -> str:
    try:
        return f"{int(bps) * 8 / 1_000_000:.2f} Мбит/с"
    except (TypeError, ValueError):
        return "-"


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
        self.start_time: Optional[float] = None
        self.roof_win: Optional[tk.Toplevel] = None

        # Cached credentials (live only in RAM, never written to disk)
        self._cached_ip: str = ""
        self._cached_pw: str = ""

        # ---- Monitoring buffers ----
        self.dynamic_params = ['rsrp', 'rssi', 'sinr', 'rsrq']
        self.peak_values: Dict[str, Any] = {p: '-' for p in self.dynamic_params}
        self.times: List[float] = []
        self.values: Dict[str, List[float]] = {p: [] for p in self.dynamic_params}
        self.session_log: List[Dict[str, Any]] = []
        self.dir_history: List[float] = []

        # ---- Reconnect ----
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL

        # Defaults from CLI
        self.default_ip = default_ip
        self.default_password = default_password

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
            self.top_bar, text="Отключено", foreground='red',
            font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side=tk.LEFT, padx=5)

        self.ontop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.top_bar, text="Поверх окон",
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
        self.notebook.add(self.tab_settings, text="⚙️ Подключение")
        self.notebook.add(self.tab_monitor, text="📈 Монитор")
        self.notebook.add(self.tab_network, text="🎛️ Сеть")
        self.notebook.add(self.tab_tower, text="🗼 Вышка")
        self.notebook.add(self.tab_status, text="📊 Состояние")

        self.build_settings_tab()
        self.build_monitor_tab()
        self.build_network_tab()
        self.build_tower_tab()
        self.build_status_tab()

        self.apply_view_mode()

    def build_settings_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_settings,
                               text="Параметры роутера", padding=10)
        frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(frame, text="IP адрес:").grid(
            row=0, column=0, sticky='e', padx=5, pady=5)
        self.ip_entry = ttk.Entry(frame, width=25)
        self.ip_entry.insert(0, self.default_ip)
        self.ip_entry.grid(row=0, column=1, sticky='w', padx=5)
        self.ip_entry.bind("<Return>", lambda e: self.password_entry.focus())

        ttk.Label(frame, text="Пароль:").grid(
            row=1, column=0, sticky='e', padx=5, pady=5)
        self.password_entry = ttk.Entry(frame, show="*", width=25)
        if self.default_password:
            self.password_entry.insert(0, self.default_password)
        self.password_entry.grid(row=1, column=1, sticky='w', padx=5)
        # Enter в поле пароля — подключиться
        self.password_entry.bind("<Return>", lambda e: self.start_connect())

        ttk.Label(frame, text="Опрос (сек):").grid(
            row=2, column=0, sticky='e', padx=5, pady=5)
        self.update_interval = tk.StringVar(value='1')
        self.update_interval.trace_add('write',
                                       lambda *a: self._sync_interval())
        ttk.Combobox(frame, textvariable=self.update_interval,
                     values=['0.5', '1', '2', '5'],
                     state='readonly', width=5).grid(
            row=2, column=1, sticky='w', padx=5)

        self.reconnect_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Авто-переподключение при обрыве",
                        variable=self.reconnect_var).grid(
            row=3, column=0, columnspan=2, sticky='w', padx=5, pady=5)

        btn_frame = ttk.Frame(self.tab_settings)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        self.connect_button = ttk.Button(
            btn_frame, text="🚀 Подключиться", command=self.start_connect)
        self.connect_button.pack(side=tk.LEFT, padx=5)

        info = ttk.LabelFrame(self.tab_settings, text="Подсказка", padding=10)
        info.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(info, wraplength=780, justify="left", text=(
            "• IP по умолчанию для большинства Huawei: 192.168.8.1\n"
            "  (для B315/B525 — 192.168.1.1 или 192.168.3.1).\n"
            "• Логин по умолчанию: admin, пароль указан на наклейке.\n"
            "• 401 Unauthorized — перезагрузите роутер или проверьте пароль.\n"
            "• Данные на диск НЕ сохраняются — программа полностью портативна."
        )).pack(anchor='w')

    def build_monitor_tab(self) -> None:
        # Переключатель режима
        self.mode_frame = ttk.Frame(self.tab_monitor, padding=5)
        self.mode_frame.pack(fill=tk.X, padx=10, pady=2)
        ttk.Label(self.mode_frame, text="Интерфейс:",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=5)
        self.view_mode_var = tk.StringVar(value="Стандартный")
        mode_cb = ttk.Combobox(
            self.mode_frame, textvariable=self.view_mode_var,
            values=["Стандартный", "Профессиональный"],
            state="readonly", width=16)
        mode_cb.pack(side=tk.LEFT, padx=5)
        mode_cb.bind("<<ComboboxSelected>>", self.apply_view_mode)

        # Здоровье связи (Standard)
        self.health_frame = ttk.LabelFrame(
            self.tab_monitor, text="Общее качество связи", padding=10)
        self.health_progress = ttk.Progressbar(
            self.health_frame, orient="horizontal", mode="determinate")
        self.health_progress.pack(fill=tk.X, side=tk.TOP, pady=5)
        self.health_text_lbl = tk.Label(
            self.health_frame, text="Подключитесь к роутеру",
            font=("Segoe UI", 12, "bold"), fg="gray")
        self.health_text_lbl.pack(side=tk.TOP, pady=2)

        # 4 крупных индикатора
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
            status = tk.Label(f, text="Нет данных",
                              font=("Segoe UI", 9, "bold"), fg='gray')
            status.pack(pady=2)
            peak = tk.Label(f, text="Пик: -",
                            font=("Segoe UI", 8), fg='gray')
            peak.pack()
            self.lbl_vars[param] = {
                'val': val, 'status': status, 'peak': peak, 'frame': f}

        # Индикатор направления (главная фишка для монтажа)
        self.dir_frame = ttk.LabelFrame(
            self.tab_monitor,
            text="Тенденция RSRP (поворачивайте антенну)", padding=8)
        self.dir_label = tk.Label(
            self.dir_frame, text="—",
            font=("Segoe UI", 32, "bold"), fg='gray')
        self.dir_label.pack()
        self.dir_text = tk.Label(
            self.dir_frame, text="Накапливаю данные...",
            font=("Segoe UI", 10), fg='gray')
        self.dir_text.pack()

        # Инструменты (Pro)
        self.tools_frame = ttk.Frame(self.tab_monitor)
        self.jitter_label = ttk.Label(
            self.tools_frame, text="Джиттер: -",
            font=("Segoe UI", 10, "bold"))
        self.geiger_var = tk.BooleanVar(value=False)
        self.geiger_cb = ttk.Checkbutton(
            self.tools_frame, text="🔊 Аудио-помощник",
            variable=self.geiger_var)
        if not HAS_WINSOUND:
            self.geiger_cb.config(state='disabled',
                                  text="🔊 Аудио (ОС не поддерживается)")
        ttk.Button(self.tools_frame, text="🖥 Крышный режим",
                   command=self.toggle_roof_mode).pack(side=tk.RIGHT, padx=5)
        self.geiger_cb.pack(side=tk.RIGHT, padx=5)
        self.jitter_label.pack(side=tk.LEFT)

        # Кнопки графика и экспорта
        self.ctrl_frame = ttk.Frame(self.tab_monitor)
        ttk.Label(self.ctrl_frame, text="График:").pack(side=tk.LEFT)
        self.graph_param = tk.StringVar(value='rsrp')
        self.graph_cb = ttk.Combobox(
            self.ctrl_frame, textvariable=self.graph_param,
            values=self.dynamic_params, state='readonly', width=8)
        self.graph_cb.pack(side=tk.LEFT, padx=5)
        self.graph_cb.bind("<<ComboboxSelected>>", self.reset_graph)
        ttk.Button(self.ctrl_frame, text="Сбросить пики",
                   command=self.reset_peaks).pack(side=tk.RIGHT, padx=5)
        ttk.Button(self.ctrl_frame, text="💾 Экспорт CSV",
                   command=self.export_csv).pack(side=tk.RIGHT, padx=5)

        # График matplotlib
        self.fig, self.ax = plt.subplots(figsize=(8, 2.2))
        self.fig.subplots_adjust(left=0.10, right=0.98,
                                 top=0.95, bottom=0.25)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.tab_monitor)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.setup_graph()

    def build_network_tab(self) -> None:
        band_frame = ttk.LabelFrame(
            self.tab_network, text="Фиксация частот (Band Lock)", padding=10)
        band_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(band_frame, wraplength=800, justify='left', text=(
            "ВНИМАНИЕ: фиксация диапазона может уменьшить покрытие. "
            "Применяйте, чтобы привязаться к лучшей вышке после анализа в "
            "Pro-режиме.")).grid(
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

        btn_frame = ttk.Frame(band_frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="Применить Band Lock",
                   command=self.apply_bands).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Сбросить в AUTO",
                   command=self.reset_bands).pack(side=tk.LEFT, padx=5)

        ant_frame = ttk.LabelFrame(self.tab_network,
                                   text="Переключение антенн", padding=10)
        ant_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(ant_frame, text="Режим:").pack(side=tk.LEFT, padx=5)
        self.antenna_var = tk.StringVar(value="Авто")
        ttk.Combobox(ant_frame, textvariable=self.antenna_var,
                     values=list(ANTENNA_MODES.keys()),
                     state='readonly', width=15).pack(side=tk.LEFT, padx=5)
        ttk.Button(ant_frame, text="Применить",
                   command=self.apply_antenna).pack(side=tk.LEFT, padx=5)

    def build_tower_tab(self) -> None:
        info_frame = ttk.LabelFrame(
            self.tab_tower, text="Информация о станции", padding=10)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tower_labels: Dict[str, ttk.Label] = {}
        fields = [
            ('plmn', 'Оператор (PLMN)'),
            ('band', 'Рабочий Band (LTE)'),
            ('aggregation', 'Агрегация (CA)'),
            ('dlbandwidth', 'Ширина канала (DL)'),
            ('pci', 'Сектор антенны (PCI)'),
            ('enodeb', 'eNodeB (Вышка)'),
            ('sector', 'Cell (Локальный сектор)'),
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(info_frame, text=f"{name}:",
                      font=("", 10, "bold")).grid(
                row=i, column=0, sticky='e', pady=4, padx=5)
            lbl = ttk.Label(info_frame, text="-", font=("", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=4, padx=5)
            self.tower_labels[key] = lbl

        btn_frame = ttk.Frame(self.tab_tower)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text="🗺 Открыть на CellMapper",
                   command=self.open_cellmapper).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📡 Инфо из InfoCellTowers",
                   command=self.fetch_infocelltowers).pack(side=tk.LEFT, padx=5)

    def build_status_tab(self) -> None:
        stat_frame = ttk.LabelFrame(
            self.tab_status, text="Мониторинг железа и трафика",
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
            ttk.Label(stat_frame, text=f"{name}:",
                      font=("", 10, "bold")).grid(
                row=i, column=0, sticky='e', pady=6, padx=5)
            lbl = ttk.Label(stat_frame, text="-", font=("", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=6, padx=5)
            self.stat_labels[key] = lbl

    # =====================================================
    # View modes
    # =====================================================

    def apply_view_mode(self, _event=None) -> None:
        """Прячем всё необязательное, затем показываем нужное для режима."""
        for w in (self.health_frame, self.tools_frame,
                  self.ctrl_frame, self.canvas_widget, self.dir_frame):
            w.pack_forget()

        if self.view_mode_var.get() == "Стандартный":
            self.health_frame.pack(fill=tk.X, padx=10, pady=5,
                                   before=self.digits_frame)
            self.dir_frame.pack(fill=tk.X, padx=10, pady=5)
            for p in self.dynamic_params:
                self.lbl_vars[p]['peak'].pack_forget()
        else:
            self.dir_frame.pack(fill=tk.X, padx=10, pady=5)
            self.tools_frame.pack(fill=tk.X, padx=15, pady=5)
            self.ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
            self.canvas_widget.pack(fill=tk.BOTH, expand=True,
                                    padx=10, pady=5)
            for p in self.dynamic_params:
                self.lbl_vars[p]['peak'].pack(side=tk.BOTTOM)

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
            messagebox.showerror("Ошибка",
                                 f"Неверный IP-адрес: {ip!r}\n"
                                 "Пример: 192.168.8.1")
            return
        self._cached_ip = ip
        self._cached_pw = self.password_entry.get()
        self._sync_interval()
        self.auto_reconnect = self.reconnect_var.get()
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.connect_button.config(state='disabled')
        self.status_label.config(text="Подключение...", foreground='orange')
        threading.Thread(target=self._connect_thread, daemon=True).start()

    def _connect_thread(self) -> None:
        url = f"http://{self._cached_ip}"
        try:
            client = Client(Connection(
                url, username='admin',
                password=self._cached_pw, timeout=4))
            client.device.information()    # верификация
            self.client = client
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
        self.connect_button.config(state='normal', text="⏹ Отключиться")
        self.status_label.config(text="Подключено", foreground='green')
        self.notebook.select(self.tab_monitor)
        self.reset_graph()
        self.session_log.clear()
        self.dir_history.clear()
        self.peak_values = {p: '-' for p in self.dynamic_params}

    def _on_connected_fail(self, error: str) -> None:
        self.connect_button.config(state='normal', text="🚀 Подключиться")
        self.status_label.config(text="Ошибка", foreground='red')
        snippet = error if len(error) < 200 else error[:200] + "..."
        messagebox.showerror("Ошибка подключения",
                             f"Связь с роутером не удалась:\n\n{snippet}")

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
        self.connect_button.config(text="🚀 Подключиться", state='normal')
        if was_connected:
            self.status_label.config(text="Отключено", foreground='red')
            self.health_text_lbl.config(text="Подключитесь к роутеру",
                                        fg="gray")
            self.health_progress.config(value=0)
            self.dir_label.config(text="—", fg='gray')
            self.dir_text.config(text="Нет данных", fg='gray')

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
                    text="Таймаут API...", foreground='orange'))
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
            text=f"Переподключение через {d:.0f}с...", foreground='orange'))
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
                text="Подключено", foreground='green'))
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
        self.status_label.config(text="Подключено", foreground='green')

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
                text=status_text.upper(), fg=color)
            if (self.peak_values[p] == '-' or val_num > self.peak_values[p]):
                self.peak_values[p] = val_num
            self.lbl_vars[p]['peak'].config(text=f"Пик: {self.peak_values[p]}")
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

        # Здоровье связи (Standard)
        if self.view_mode_var.get() == "Стандартный":
            score, summary, color = calculate_overall_health(
                rsrp, current_vals.get('sinr'))
            self.health_progress.config(value=score)
            self.health_text_lbl.config(text=summary, fg=color)

        # Джиттер (Pro)
        if (self.view_mode_var.get() == "Профессиональный"
                and len(self.values['rsrp']) >= JITTER_WINDOW):
            recent = self.values['rsrp'][-JITTER_WINDOW:]
            jitter = max(recent) - min(recent)
            jcol = ('green' if jitter < 3
                    else 'orange' if jitter < 7 else 'red')
            self.jitter_label.config(
                text=f"Джиттер: {jitter:.1f} dB (стабильность сигнала)",
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

        # График (Pro)
        if (self.view_mode_var.get() == "Профессиональный"
                and self.start_time is not None):
            param = self.graph_param.get()
            if self.values[param]:
                self.times.append(time.time() - self.start_time)
                if len(self.times) > GRAPH_HISTORY:
                    self.times.pop(0)
                self.line.set_data(self.times, self.values[param])
                xmax = self.times[-1] + 1
                xmin = max(0, xmax - GRAPH_HISTORY * self._interval_seconds)
                self.ax.set_xlim(xmin, xmax)
                self.canvas.draw_idle()

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

        # Информация о вышке
        for key, lbl in self.tower_labels.items():
            val = str(data.get(key, '-'))
            if key == 'plmn' and val != '-' and len(val) >= 5:
                op = PLMN_MAP.get(val, 'Неизвестный оператор')
                val = f"{val} ({op})"
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
            text=str(data.get('Temperature', 'Н/Д')))
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
            "↑": "Сигнал улучшается — продолжайте в том же направлении",
            "↓": "Сигнал ухудшается — поверните обратно",
            "→": "Сигнал стабилен — зафиксируйте антенну",
            "—": "Накапливаю данные...",
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
            messagebox.showwarning("Ошибка",
                                   "Сначала подключитесь к роутеру.")
            return
        mask = sum(BANDS[n] for n, v in self.band_checkboxes.items()
                   if v.get())
        if mask == 0:
            messagebox.showwarning("Внимание",
                                   "Выберите хотя бы один диапазон!")
            return
        hex_mask = format(mask, 'X')
        client = self.client

        def task():
            try:
                client.net.set_net_mode(hex_mask, NETBAND_AUTO_MASK,
                                        NETMODE_LTE_ONLY)
                self.root.after(0, lambda: messagebox.showinfo(
                    "Успех", f"Band Lock применён (mask: {hex_mask})."))
            except Exception as e:
                logger.exception("Band lock failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    "Ошибка", f"Роутер отклонил команду:\n{err}"))
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
                    "Успех", "Сеть сброшена в AUTO."))
            except Exception as e:
                logger.exception("Reset bands failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    "Ошибка", err))
        threading.Thread(target=task, daemon=True).start()

    def apply_antenna(self) -> None:
        if self.client is None:
            messagebox.showwarning("Ошибка",
                                   "Сначала подключитесь к роутеру.")
            return
        ant_val = parse_antenna_value(self.antenna_var.get())
        if ant_val is None:
            messagebox.showerror("Ошибка", "Неизвестный режим антенны.")
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
                            "(модель роутера может не поддерживать).")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Успех",
                    f"Тип антенны изменён: {self.antenna_var.get()}"))
            except Exception as e:
                logger.exception("Set antenna failed")
                self.root.after(0, lambda err=str(e): messagebox.showerror(
                    "Ошибка", err))
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
                "Внимание",
                "Недостаточно данных о вышке (нужны PLMN и eNodeB).")
            return
        mcc, mnc = plmn[:3], plmn[3:]
        url = (f"https://www.cellmapper.net/map?MCC={mcc}&MNC={mnc}"
               f"&type=LTE&siteid={enodeb}")
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не открыть браузер: {e}")

    def fetch_infocelltowers(self) -> None:
        threading.Thread(target=self._infocell_task, daemon=True).start()

    def _infocell_task(self) -> None:
        with self._data_lock:
            plmn = str(self.last_data.get('plmn', ''))
            enodeb = self.last_data.get('enodeb')
        if len(plmn) < 5 or not enodeb:
            self.root.after(0, lambda: messagebox.showwarning(
                "Внимание", "Нет данных о вышке."))
            return
        try:
            resp = requests.get(
                f"https://infocelltowers.ru/api/v2/cell"
                f"?mcc={plmn[:3]}&mnc={plmn[3:]}&enodeb={enodeb}",
                timeout=5,
                headers={'User-Agent': f'{APP_NAME}/{__version__}'})
            if resp.ok:
                d = resp.json()
                info = (f"Адрес: {d.get('address', 'Нет данных')}\n"
                        f"Широта: {d.get('lat', '-')}, "
                        f"Долгота: {d.get('lon', '-')}")
                self.root.after(0, lambda: messagebox.showinfo(
                    "InfoCellTowers", info))
            else:
                code = resp.status_code
                self.root.after(0, lambda: messagebox.showwarning(
                    "Инфо", f"API вернул код {code}."))
        except requests.RequestException as e:
            self.root.after(0, lambda err=str(e): messagebox.showerror(
                "Ошибка сети", err))
        except Exception as e:
            logger.exception("InfoCellTowers failed")
            self.root.after(0, lambda err=str(e): messagebox.showerror(
                "Ошибка", err))

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
        tk.Label(self.roof_win, text="[ESC] для выхода",
                 font=("Arial", 14), fg='gray',
                 bg='black').pack(pady=12)
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
                "Экспорт",
                "Лог сессии пуст. Подключитесь и подождите, пока "
                "соберутся данные.")
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
                "Экспорт",
                f"Сохранено {len(self.session_log)} записей в:\n{path}")
        except OSError as e:
            messagebox.showerror("Ошибка", f"Не удалось записать файл: {e}")

    # =====================================================
    # MISC HELPERS
    # =====================================================

    def setup_graph(self) -> None:
        param = self.graph_param.get()
        self.ax.clear()
        (self.line,) = self.ax.plot([], [], linewidth=2, color='#0078D7')
        self.ax.set_ylabel(self._unit(param))
        self.ax.grid(True, linestyle=':', alpha=0.6)
        self.ax.set_ylim(*PARAM_RANGES.get(param, (-120, 0)))
        self.canvas.draw()

    def reset_graph(self, _event=None) -> None:
        self.times = []
        self.values = {p: [] for p in self.dynamic_params}
        self.setup_graph()

    def reset_peaks(self) -> None:
        self.peak_values = {p: '-' for p in self.dynamic_params}
        for p in self.dynamic_params:
            self.lbl_vars[p]['peak'].config(text="Пик: -")

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
            plt.close('all')
        except Exception:
            pass
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
