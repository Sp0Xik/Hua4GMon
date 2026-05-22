import tkinter as tk
from tkinter import messagebox, ttk
import threading
import time
import configparser
import datetime
import re
import base64
import webbrowser
import requests

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

class Hua4GMon:
    PLMN_MAP = {
        '25001': 'МТС', '25002': 'МегаФон', '25011': 'Yota',
        '25020': 'Tele2', '25027': 'Летай', '25035': 'Мотив', 
        '25039': 'Ростелеком', '25099': 'Билайн'
    }

    BANDS = {
        'B1 (2100 МГц)': 0x1,
        'B3 (1800 МГц)': 0x4,
        'B7 (2600 МГц)': 0x40,
        'B8 (900 МГц)': 0x80,
        'B20 (800 МГц)': 0x80000,
        'B38 (TDD 2600)': 0x2000000000,
        'B40 (TDD 2300)': 0x8000000000
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Huawei PRO Antenna Tool")
        self.root.geometry("850x700")
        self.root.minsize(800, 650)

        # Состояния
        self.connected = False
        self.is_monitoring = False
        self.client = None
        self.last_data = {}
        self.start_time = None
        self.roof_win = None
        
        # Данные графиков и метрик
        self.dynamic_params = ['rsrp', 'rssi', 'sinr', 'rsrq']
        self.peak_values = {param: '-' for param in self.dynamic_params}
        self.times, self.values = [], {p: [] for p in self.dynamic_params}
        self.line = None

        self.config = configparser.ConfigParser()
        self.config_file = 'config.ini'
        self.load_config()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.setup_ui()

    # =========================================================
    # ПОСТРОЕНИЕ ИНТЕРФЕЙСА
    # =========================================================
    def setup_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tab_monitor = ttk.Frame(self.notebook)
        self.tab_network = ttk.Frame(self.notebook)
        self.tab_tower = ttk.Frame(self.notebook)
        self.tab_status = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_monitor, text="📈 Монитор")
        self.notebook.add(self.tab_network, text="🎛️ Управление сетью")
        self.notebook.add(self.tab_tower, text="🗼 Вышка")
        self.notebook.add(self.tab_status, text="📊 Состояние")
        self.notebook.add(self.tab_settings, text="⚙️ Подключение")

        self.build_settings_tab()
        self.build_monitor_tab()
        self.build_network_tab()
        self.build_tower_tab()
        self.build_status_tab()

        self.apply_view_mode()

    def build_settings_tab(self):
        frame = ttk.LabelFrame(self.tab_settings, text="Параметры роутера", padding=10)
        frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(frame, text="IP адрес:").grid(row=0, column=0, sticky='e', padx=5, pady=5)
        self.ip_entry = ttk.Entry(frame, width=25)
        self.ip_entry.insert(0, self.config.get('Settings', 'ip', fallback='192.168.8.1'))
        self.ip_entry.grid(row=0, column=1, sticky='w', padx=5)

        ttk.Label(frame, text="Пароль:").grid(row=1, column=0, sticky='e', padx=5, pady=5)
        self.password_entry = ttk.Entry(frame, show="*", width=25)
        saved_pw = self.config.get('Settings', 'password', fallback='')
        if saved_pw:
            try: self.password_entry.insert(0, base64.b64decode(saved_pw).decode('utf-8'))
            except: pass
        self.password_entry.grid(row=1, column=1, sticky='w', padx=5)

        ttk.Label(frame, text="Опрос (сек):").grid(row=2, column=0, sticky='e', padx=5, pady=5)
        self.update_interval = tk.StringVar(value='1')
        ttk.Combobox(frame, textvariable=self.update_interval, values=['0.5', '1', '2', '5'], state='readonly', width=5).grid(row=2, column=1, sticky='w', padx=5)

        btn_frame = ttk.Frame(self.tab_settings)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.connect_button = ttk.Button(btn_frame, text="🚀 Подключиться", command=self.start_connect)
        self.connect_button.pack(side=tk.LEFT, padx=5)
        self.status_label = ttk.Label(btn_frame, text="Отключено", foreground='red', font=("", 10, "bold"))
        self.status_label.pack(side=tk.LEFT, padx=15)

    def build_monitor_tab(self):
        self.mode_frame = ttk.Frame(self.tab_monitor, padding=5)
        self.mode_frame.pack(fill=tk.X, padx=10, pady=2)
        
        ttk.Label(self.mode_frame, text="Интерфейс:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=5)
        self.view_mode_var = tk.StringVar(value="Стандартный")
        mode_cb = ttk.Combobox(self.mode_frame, textvariable=self.view_mode_var, values=["Стандартный", "Профессиональный"], state="readonly", width=16)
        mode_cb.pack(side=tk.LEFT, padx=5)
        mode_cb.bind("<<ComboboxSelected>>", self.apply_view_mode)

        self.health_frame = ttk.LabelFrame(self.tab_monitor, text="Общее качество связи для пользователя", padding=10)
        self.health_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.health_progress = ttk.Progressbar(self.health_frame, orient="horizontal", mode="determinate")
        self.health_progress.pack(fill=tk.X, side=tk.TOP, pady=5)
        
        self.health_text_lbl = tk.Label(self.health_frame, text="Подключитесь к роутеру для оценки", font=("Segoe UI", 12, "bold"), fg="gray")
        self.health_text_lbl.pack(side=tk.TOP, pady=2)

        digits_frame = ttk.Frame(self.tab_monitor)
        digits_frame.pack(fill=tk.X, padx=10, pady=5)

        self.lbl_vars = {}
        for i, param in enumerate(self.dynamic_params):
            frame = ttk.LabelFrame(digits_frame, text=param.upper(), padding=5)
            frame.grid(row=0, column=i, padx=5, sticky='nsew')
            digits_frame.columnconfigure(i, weight=1)
            
            val_lbl = tk.Label(frame, text="-", font=("Segoe UI", 20, "bold"), fg='gray')
            val_lbl.pack()
            
            status_lbl = tk.Label(frame, text="Нет данных", font=("Segoe UI", 9, "bold"), fg='gray')
            status_lbl.pack(pady=2)
            
            peak_lbl = tk.Label(frame, text="Пик: -", font=("Segoe UI", 8), fg='gray')
            peak_lbl.pack()
            
            self.lbl_vars[param] = {'val': val_lbl, 'status': status_lbl, 'peak': peak_lbl, 'frame': frame}

        self.tools_frame = ttk.Frame(self.tab_monitor)
        self.tools_frame.pack(fill=tk.X, padx=15, pady=5)
        
        self.jitter_label = ttk.Label(self.tools_frame, text="Джиттер сигнала: -", font=("Segoe UI", 10, "bold"))
        self.jitter_label.pack(side=tk.LEFT)

        self.geiger_var = tk.BooleanVar(value=False)
        geiger_cb = ttk.Checkbutton(self.tools_frame, text="🔊 Аудио-помощник", variable=self.geiger_var)
        geiger_cb.pack(side=tk.RIGHT, padx=10)
        if not HAS_WINSOUND:
            geiger_cb.config(state='disabled', text="🔊 Аудио-помощник (ОС не подд.)")

        ttk.Button(self.tools_frame, text="🖥 Крышный режим", command=self.toggle_roof_mode).pack(side=tk.RIGHT, padx=10)

        self.ctrl_frame = ttk.Frame(self.tab_monitor)
        self.ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(self.ctrl_frame, text="График:").pack(side=tk.LEFT)
        self.graph_param = tk.StringVar(value='rsrp')
        self.graph_cb = ttk.Combobox(self.ctrl_frame, textvariable=self.graph_param, values=self.dynamic_params, state='readonly', width=8)
        self.graph_cb.pack(side=tk.LEFT, padx=5)
        self.graph_cb.bind("<<ComboboxSelected>>", self.reset_graph)

        ttk.Button(self.ctrl_frame, text="Сбросить пики", command=self.reset_peaks).pack(side=tk.RIGHT)

        self.fig, self.ax = plt.subplots(figsize=(8, 2.2))
        self.fig.tight_layout(pad=2)
        self.param_ranges = {'rsrp': (-120, -50), 'rssi': (-110, -50), 'rsrq': (-20, -3), 'sinr': (-5, 30)}
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.tab_monitor)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.setup_graph()

    def build_network_tab(self):
        band_frame = ttk.LabelFrame(self.tab_network, text="Фиксация частот (Band Lock)", padding=10)
        band_frame.pack(fill=tk.X, padx=10, pady=10)

        self.band_checkboxes = {}
        row, col = 0, 0
        for band_name in self.BANDS.keys():
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(band_frame, text=band_name, variable=var)
            cb.grid(row=row, column=col, sticky='w', padx=10, pady=2)
            self.band_checkboxes[band_name] = var
            col += 1
            if col > 2:
                col = 0
                row += 1

        btn_frame = ttk.Frame(band_frame)
        btn_frame.grid(row=row+1, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="Применить Band Lock", command=self.apply_bands).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Сбросить в AUTO", command=self.reset_bands).pack(side=tk.LEFT, padx=5)

        ant_frame = ttk.LabelFrame(self.tab_network, text="Переключение антенн", padding=10)
        ant_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(ant_frame, text="Режим:").pack(side=tk.LEFT, padx=5)
        self.antenna_var = tk.StringVar(value="Auto (0)")
        ant_combo = ttk.Combobox(ant_frame, textvariable=self.antenna_var, values=["Auto (0)", "Внутренняя (1)", "Внешняя (2)", "Смешанная (3)"], state='readonly', width=15)
        ant_combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(ant_frame, text="Применить", command=self.apply_antenna).pack(side=tk.LEFT, padx=5)

    def build_tower_tab(self):
        info_frame = ttk.LabelFrame(self.tab_tower, text="Информация о станции", padding=10)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tower_labels = {}
        fields = [
            ('plmn', 'Оператор (PLMN)'),
            ('band', 'Рабочий Band (LTE)'),
            ('aggregation', 'Агрегация (CA)'),
            ('dlbandwidth', 'Ширина канала (DL)'),
            ('pci', 'Сектор антенны (PCI)'),
            ('enodeb', 'eNodeB (Вышка)'),
            ('sector', 'Cell (Локальный сектор)')
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(info_frame, text=f"{name}:", font=("", 10, "bold")).grid(row=i, column=0, sticky='e', pady=4, padx=5)
            lbl = ttk.Label(info_frame, text="-", font=("", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=4, padx=5)
            self.tower_labels[key] = lbl

        btn_frame = ttk.Frame(self.tab_tower)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text="🗺️ Открыть на CellMapper", command=self.open_cellmapper).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📡 Инфо из InfoCellTowers", command=self.fetch_infocelltowers).pack(side=tk.LEFT, padx=5)

    def build_status_tab(self):
        stat_frame = ttk.LabelFrame(self.tab_status, text="Мониторинг железа и трафика", padding=10)
        stat_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.stat_labels = {}
        fields = [
            ('uptime', 'Время сессии'),
            ('temp', 'Температура чипа'),
            ('dl_rate', 'Текущая скорость (Download)'),
            ('ul_rate', 'Текущая скорость (Upload)'),
            ('total_dl', 'Скачано за сессию'),
            ('total_ul', 'Отдано за сессию')
        ]
        for i, (key, name) in enumerate(fields):
            ttk.Label(stat_frame, text=f"{name}:", font=("", 10, "bold")).grid(row=i, column=0, sticky='e', pady=6, padx=5)
            lbl = ttk.Label(stat_frame, text="-", font=("", 10))
            lbl.grid(row=i, column=1, sticky='w', pady=6, padx=5)
            self.stat_labels[key] = lbl

    # =========================================================
    # РЕЖИМЫ ИНТЕРФЕЙСА (СТАНДАРТНЫЙ / ПРОФЕССИОНАЛЬНЫЙ)
    # =========================================================
    def apply_view_mode(self, event=None):
        mode = self.view_mode_var.get()
        if mode == "Стандартный":
            self.jitter_label.pack_forget()
            self.ctrl_frame.pack_forget()
            self.canvas_widget.pack_forget()
            self.health_frame.pack(fill=tk.X, padx=10, pady=5, before=self.lbl_vars['rsrp']['frame'].master)
            for param in self.dynamic_params:
                self.lbl_vars[param]['peak'].pack_forget()
        else:
            self.health_frame.pack_forget()
            self.jitter_label.pack(side=tk.LEFT)
            self.ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
            self.canvas_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
            for param in self.dynamic_params:
                self.lbl_vars[param]['peak'].pack(side=tk.BOTTOM)

    # =========================================================
    # ЛОГИКА ПОДКЛЮЧЕНИЯ И ОПРОСА СЕТИ
    # =========================================================
    def start_connect(self):
        if self.connected:
            self.disconnect()
            return
        self.connect_button.config(state='disabled')
        self.status_label.config(text="Подключение...", foreground='orange')
        threading.Thread(target=self.connect_thread, daemon=True).start()

    def connect_thread(self):
        # ИСПРАВЛЕНО: Передаем username и password явными аргументами, а не в URL
        ip = self.ip_entry.get().strip()
        password = self.password_entry.get()
        url = f"http://{ip}"
        try:
            self.client = Client(Connection(url, username='admin', password=password, timeout=4))
            self.client.device.information() 
            self.connected = True
            self.is_monitoring = True
            self.start_time = time.time()
            self.root.after(0, self.on_connected_success)
            threading.Thread(target=self.monitor_loop, daemon=True).start()
        except Exception as e:
            self.root.after(0, lambda: self.on_connected_fail(str(e)))

    def on_connected_success(self):
        self.connect_button.config(state='normal', text="⏹ Отключиться")
        self.status_label.config(text="Подключено", foreground='green')
        self.notebook.select(self.tab_monitor)
        self.reset_graph()

    def on_connected_fail(self, error):
        self.connect_button.config(state='normal')
        self.status_label.config(text="Ошибка", foreground='red')
        messagebox.showerror("Ошибка", f"Связь не удалась:\n{error}")

    def disconnect(self):
        self.is_monitoring = False
        self.connected = False
        self.client = None
        self.connect_button.config(text="🚀 Подключиться")
        self.status_label.config(text="Отключено", foreground='red')
        self.health_text_lbl.config(text="Подключитесь к роутеру для оценки", fg="gray")
        self.health_progress.config(value=0)

    def monitor_loop(self):
        while self.is_monitoring:
            try:
                sig = self.client.device.signal()
                plmn = self.client.net.current_plmn()
                status = self.client.monitoring.status()
                traffic = self.client.monitoring.traffic_statistics()
                
                self.last_data = {**sig, **plmn, **status, **traffic}
                
                # ИСПРАВЛЕНО: В plmn ключ называется 'Numeric', а не 'plmn'
                self.last_data['plmn'] = plmn.get('Numeric', self.last_data.get('plmn', ''))

                # ИСПРАВЛЕНО: Поддержка HEX и DEC форматов cell_id
                cell_id = self.last_data.get('cell_id', '')
                if cell_id:
                    try:
                        cid_str = str(cell_id)
                        if cid_str.startswith('0x') or any(c in 'abcdefABCDEF' for c in cid_str):
                            cid = int(cid_str, 16)
                        else:
                            cid = int(cid_str)
                        self.last_data['enodeb'] = cid // 256
                        self.last_data['sector'] = cid % 256
                    except ValueError:
                        pass

                band_str = str(self.last_data.get('band', ''))
                self.last_data['aggregation'] = "Активна" if ("+" in band_str or "CA" in band_str) else "Нет (Single)"

                self.root.after(0, self.refresh_ui)
            except Exception:
                self.root.after(0, lambda: self.status_label.config(text="Таймаут...", foreground='orange'))
            
            time.sleep(float(self.update_interval.get()))

    # =========================================================
    # АНАЛИЗАТОР СИГНАЛА ДЛЯ НОВИЧКОВ (МАТЕМАТИКА)
    # =========================================================
    def evaluate_signal(self, param, val):
        if val is None:
            return "Нет данных", "gray", 0
            
        if param == 'rsrp':
            if val >= -80:   return "Отличный", "#00b894", 100
            if val >= -90:   return "Хороший", "#2ecc71", 80
            if val >= -100:  return "Средний (Удовл.)", "#fdcb6e", 50
            return "Плохой (Обрывы)", "#d63031", 15
            
        if param == 'sinr':
            if val >= 20:    return "Идеально чистый", "#00b894", 100
            if val >= 13:    return "Хорошая чистота", "#2ecc71", 75
            if val >= 0:     return "Много шума", "#fdcb6e", 40
            return "Критичные шумы", "#d63031", 5
            
        if param == 'rssi':
            if val >= -65:   return "Сильный", "#00b894", 100
            if val >= -75:   return "Нормальный", "#2ecc71", 75
            if val >= -85:   return "Слабый", "#fdcb6e", 45
            return "Очень слабый", "#d63031", 10
            
        if param == 'rsrq':
            if val >= -6:    return "Отличный", "#00b894", 100
            if val >= -12:   return "Стабильный", "#2ecc71", 70
            if val >= -15:   return "Потери пакетов", "#fdcb6e", 40
            return "Высокие потери", "#d63031", 10
            
        return "Н/Д", "gray", 0

    def calculate_overall_health(self, rsrp, sinr):
        if rsrp is None or sinr is None: return 0, "Нет данных", "gray"
        
        _, _, r_pct = self.evaluate_signal('rsrp', rsrp)
        _, _, s_pct = self.evaluate_signal('sinr', sinr)
        
        overall = min(r_pct, s_pct) * 0.7 + max(r_pct, s_pct) * 0.3
        overall = int(max(0, min(100, overall)))
        
        if overall >= 85:   return overall, f"Идеальная связь ({overall}%) — отличная скорость, подходит для 4K и игр", "#00b894"
        if overall >= 65:   return overall, f"Хорошая связь ({overall}%) — стабильный интернет, FullHD видео", "#2ecc71"
        
        if overall >= 35:   return overall, f"Умеренное качество ({overall}%) — возможны просадки скорости, крутите антенну", "#fdcb6e"
        return overall, f"Плохая связь ({overall}%) — интернет будет тормозить или отваливаться!", "#d63031"

    # =========================================================
    # ОБНОВЛЕНИЕ ИНТЕРФЕЙСА
    # =========================================================
    def refresh_ui(self):
        if not self.is_monitoring: return
        self.status_label.config(text="Подключено", foreground='green')

        current_vals = {}

        for param in self.dynamic_params:
            val_num = self.extract_number(self.last_data.get(param))
            current_vals[param] = val_num
            
            if val_num is not None:
                status_text, color, _ = self.evaluate_signal(param, val_num)
                
                self.lbl_vars[param]['val'].config(text=f"{val_num} {self.get_unit(param)}", fg=color)
                self.lbl_vars[param]['status'].config(text=status_text.upper(), fg=color)
                
                if self.peak_values[param] == '-' or self.is_better(val_num, self.peak_values[param], param):
                    self.peak_values[param] = val_num
                self.lbl_vars[param]['peak'].config(text=f"Пик: {self.peak_values[param]}")

                self.values[param].append(val_num)
                if len(self.values[param]) > 100: self.values[param].pop(0)

        if self.view_mode_var.get() == "Стандартный":
            score, summary_text, health_color = self.calculate_overall_health(current_vals.get('rsrp'), current_vals.get('sinr'))
            self.health_progress.config(value=score)
            self.health_text_lbl.config(text=summary_text, fg=health_color)

        if self.view_mode_var.get() == "Профессиональный" and len(self.values['rsrp']) >= 5:
            recent_rsrp = self.values['rsrp'][-5:]
            jitter = max(recent_rsrp) - min(recent_rsrp)
            j_color = 'green' if jitter < 3 else 'orange' if jitter < 7 else 'red'
            self.jitter_label.config(text=f"Джиттер (разброс): {jitter:.1f} dB", foreground=j_color)

        if HAS_WINSOUND and self.geiger_var.get():
            sinr = current_vals.get('sinr')
            if sinr is not None:
                freq = max(200, min(3000, int(300 + (sinr + 5) * 60)))
                threading.Thread(target=winsound.Beep, args=(freq, 150), daemon=True).start()

        if self.view_mode_var.get() == "Профессиональный":
            param = self.graph_param.get()
            if len(self.values[param]) > 0:
                self.times.append(time.time() - self.start_time)
                if len(self.times) > 100: self.times.pop(0)
                
                self.line.set_data(self.times, self.values[param])
                self.ax.set_xlim(max(0, self.times[-1] - 100 * float(self.update_interval.get())), self.times[-1] + 1)
                self.canvas.draw_idle()

        if self.roof_win and self.roof_win.winfo_exists():
            r = current_vals.get('rsrp', 0)
            s = current_vals.get('sinr', 0)
            _, r_col, _ = self.evaluate_signal('rsrp', r)
            _, s_col, _ = self.evaluate_signal('sinr', s)
            self.r_lbl_rsrp.config(text=f"RSRP: {r if r else '-'}", fg=r_col)
            self.r_lbl_sinr.config(text=f"SINR: {s if s else '-'}", fg=s_col)

        for key, lbl in self.tower_labels.items():
            val = str(self.last_data.get(key, '-'))
            if key == 'plmn' and val != '-': val = f"{val} ({self.PLMN_MAP.get(val, 'Н/Д')})"
            lbl.config(text=val)

        dl_mbps = (int(self.last_data.get('CurrentDownloadRate', 0)) * 8) / 1000000
        ul_mbps = (int(self.last_data.get('CurrentUploadRate', 0)) * 8) / 1000000
        dl_total_mb = int(self.last_data.get('TotalDownload', 0)) / 1048576
        ul_total_mb = int(self.last_data.get('TotalUpload', 0)) / 1048576
        
        # ИСПРАВЛЕНО: Ключ API называется CurrentConnectTime
        up_sec = int(self.last_data.get('CurrentConnectTime', self.last_data.get('ConnectionTime', 0)))
        uptime_str = str(datetime.timedelta(seconds=up_sec)) if up_sec > 0 else "-"

        self.stat_labels['uptime'].config(text=uptime_str)
        self.stat_labels['temp'].config(text=str(self.last_data.get('Temperature', 'Н/Д')))
        self.stat_labels['dl_rate'].config(text=f"{dl_mbps:.2f} Мбит/с")
        self.stat_labels['ul_rate'].config(text=f"{ul_mbps:.2f} Мбит/с")
        self.stat_labels['total_dl'].config(text=f"{dl_total_mb:.1f} МБ")
        self.stat_labels['total_ul'].config(text=f"{ul_total_mb:.1f} МБ")

    # =========================================================
    # УПРАВЛЕНИЕ СЕТЬЮ (Band Lock & Antenna)
    # =========================================================
    def apply_bands(self):
        if not self.client: return messagebox.showwarning("Ошибка", "Сначала подключитесь к роутеру")
        mask_sum = 0
        for name, var in self.band_checkboxes.items():
            if var.get(): mask_sum += self.BANDS[name]
            
        if mask_sum == 0: return messagebox.showwarning("Внимание", "Выберите хотя бы один диапазон!")
        hex_mask = format(mask_sum, 'X')
        
        def task():
            try:
                # ИСПРАВЛЕНО: Аргументы шли в неверном порядке (lteband, networkband, networkmode)
                self.client.net.set_net_mode(hex_mask, '3FFFFFFF', '03')
                self.root.after(0, lambda: messagebox.showinfo("Успех", f"Band Lock применен (Mask: {hex_mask})."))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Роутер отклонил команду.\n{e}"))
                
        threading.Thread(target=task, daemon=True).start()

    def reset_bands(self):
        if not self.client: return
        def task():
            try:
                # ИСПРАВЛЕНО: Аргументы шли в неверном порядке
                self.client.net.set_net_mode('7FFFFFFFFFFFFFFF', '3FFFFFFF', '00')
                self.root.after(0, lambda: messagebox.showinfo("Успех", "Сеть сброшена в AUTO."))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
        threading.Thread(target=task, daemon=True).start()

    def apply_antenna(self):
        if not self.client: return
        ant_val = int(self.antenna_var.get()[-2])
        def task():
            try:
                # ИСПРАВЛЕНО: Безопасный вызов метода с учетом версий библиотеки и Enum
                try:
                    from huawei_lte_api.enums.device import AntennaTypeEnum
                    self.client.device.set_antenna_settings(AntennaTypeEnum(ant_val))
                except ImportError:
                    if hasattr(self.client.device, 'set_antenna_settings'):
                        self.client.device.set_antenna_settings(ant_val)
                    elif hasattr(self.client.device, 'set_antenna_type'):
                        self.client.device.set_antenna_type(ant_val)
                    else:
                        raise Exception("Метод управления антенной не найден в API")
                
                self.root.after(0, lambda: messagebox.showinfo("Успех", f"Тип антенны изменен на: {ant_val}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
        threading.Thread(target=task, daemon=True).start()

    # =========================================================
    # ОКНА И КАРТЫ
    # =========================================================
    def toggle_roof_mode(self):
        if self.roof_win and self.roof_win.winfo_exists():
            self.roof_win.destroy()
            return
            
        self.roof_win = tk.Toplevel(self.root)
        self.roof_win.attributes('-fullscreen', True)
        self.roof_win.configure(bg='black')
        self.roof_win.bind("<Escape>", lambda e: self.roof_win.destroy())
        
        tk.Label(self.roof_win, text="[ESC] для выхода", font=("Arial", 12), fg='gray', bg='black').pack(pady=20)
        self.r_lbl_rsrp = tk.Label(self.roof_win, text="RSRP: -", font=("Consolas", 100, "bold"), bg='black', fg='white')
        self.r_lbl_rsrp.pack(expand=True)
        self.r_lbl_sinr = tk.Label(self.roof_win, text="SINR: -", font=("Consolas", 100, "bold"), bg='black', fg='white')
        self.r_lbl_sinr.pack(expand=True)

    def open_cellmapper(self):
        plmn = str(self.last_data.get('plmn', ''))
        enodeb = self.last_data.get('enodeb')
        if not plmn or enodeb is None: return messagebox.showwarning("Внимание", "Нет данных о БС")
        webbrowser.open(f"https://www.cellmapper.net/map?MCC={plmn[:3]}&MNC={plmn[3:]}&type=LTE&enodeb={enodeb}")

    def fetch_infocelltowers(self):
        threading.Thread(target=self._infocell_task, daemon=True).start()

    def _infocell_task(self):
        plmn = str(self.last_data.get('plmn', ''))
        enodeb = self.last_data.get('enodeb')
        if not plmn or not enodeb: return self.root.after(0, lambda: messagebox.showwarning("Внимание", "Нет данных о вышке"))
        try:
            resp = requests.get(f"https://infocelltowers.ru/api/v2/cell?mcc={plmn[:3]}&mnc={plmn[3:]}&enodeb={enodeb}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                info = f"Адрес: {data.get('address', 'Нет данных')}\nШирота: {data.get('lat')}, Долгота: {data.get('lon')}"
                self.root.after(0, lambda: messagebox.showinfo("InfoCellTowers", info))
            else:
                self.root.after(0, lambda: messagebox.showwarning("Инфо", f"API код: {resp.status_code}"))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))

    # Утилиты
    def setup_graph(self):
        param = self.graph_param.get()
        self.ax.clear()
        self.line, = self.ax.plot([], [], linewidth=2, color='#0078D7')
        self.ax.set_ylabel(self.get_unit(param))
        self.ax.grid(True, linestyle=':', alpha=0.6)
        self.ax.set_ylim(*self.param_ranges.get(param, (-120, 0)))
        self.canvas.draw()

    def reset_graph(self, event=None):
        self.times, self.values = [], {p: [] for p in self.dynamic_params}
        self.setup_graph()

    def reset_peaks(self):
        self.peak_values = {param: '-' for param in self.dynamic_params}

    def extract_number(self, val):
        try: return float(re.search(r'-?\d+\.?\d*', str(val)).group())
        except: return None

    def get_unit(self, param):
        return "dBm" if param in ['rsrp', 'rssi'] else "dB"

    def is_better(self, cur, peak, param):
        if param in ['rsrq']: return abs(cur) < abs(peak)
        return cur > peak

    def load_config(self):
        try: self.config.read(self.config_file)
        except: pass

    def on_closing(self):
        self.is_monitoring = False
        pw_b64 = base64.b64encode(self.password_entry.get().encode('utf-8')).decode('utf-8')
        self.config['Settings'] = {'ip': self.ip_entry.get(), 'password': pw_b64}
        with open(self.config_file, 'w') as f: self.config.write(f)
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = Hua4GMon(root)
    root.mainloop()
