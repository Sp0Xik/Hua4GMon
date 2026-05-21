import tkinter as tk
from tkinter import messagebox, ttk
import threading
import time
import configparser
import datetime
import re
import base64

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


class Hua4GMon:
    def __init__(self, root):
        self.root = root
        self.root.title("Huawei 4G/5G Monitor")
        self.root.configure(bg='#f5f6fa')
        self.root.minsize(850, 650)

        # Состояния
        self.connected = False
        self.is_monitoring = False  # Флаг для контроля потоков
        self.client = None
        self.connection = None
        self.last_data = {}
        self.start_time = None
        self.previous_value = None

        # Конфиг
        self.config = configparser.ConfigParser()
        self.config_file = 'config.ini'
        self.load_config()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Списки параметров (определяем один раз)
        self.dynamic_params = ['rssi', 'rsrp', 'rsrq', 'sinr']
        self.params_order = [
            'rssi', 'rsrp', 'lac', 'rsrq', 'rrc_state', 'sinr',
            'lte_bandwidth', 'cell_id', 'band', 'mode', 'CurrentOperator',
            'ConnectionStatus', 'CurrentNetworkType', 'SignalStrength', 'plmn'
        ]
        self.params = self.dynamic_params + self.params_order
        self.param_labels = {}
        self.peak_values = {param: '-' for param in self.dynamic_params}

        self.setup_ui()

    # =========================================================
    # ПОСТРОЕНИЕ ИНТЕРФЕЙСА
    # =========================================================
    def setup_ui(self):
        style = ttk.Style()
        style.configure("TButton", font=("Segoe UI", 10), padding=4)
        style.configure("TEntry", padding=4)

        # --- Верхний блок (Настройки) ---
        input_frame = tk.Frame(self.root, bg='#f5f6fa', padx=10, pady=10)
        input_frame.pack(fill=tk.X)

        tk.Label(input_frame, text="IP роутера:", bg='#f5f6fa', font=("Segoe UI", 10)).grid(row=0, column=0, sticky='e', padx=5)
        self.ip_entry = ttk.Entry(input_frame, font=("Segoe UI", 10), width=20)
        self.ip_entry.insert(0, self.config.get('Settings', 'ip', fallback='192.168.8.1'))
        self.ip_entry.grid(row=0, column=1, padx=5, pady=2)

        tk.Label(input_frame, text="Пароль:", bg='#f5f6fa', font=("Segoe UI", 10)).grid(row=0, column=2, sticky='e', padx=5)
        self.password_entry = ttk.Entry(input_frame, show="*", font=("Segoe UI", 10), width=20)
        
        # Декодируем пароль из конфига
        saved_pw_b64 = self.config.get('Settings', 'password', fallback='')
        if saved_pw_b64:
            try:
                self.password_entry.insert(0, base64.b64decode(saved_pw_b64).decode('utf-8'))
            except:
                pass

        self.password_entry.grid(row=0, column=3, padx=5, pady=2)

        self.connect_button = ttk.Button(input_frame, text="Подключиться", command=self.start_connect, width=15)
        self.connect_button.grid(row=0, column=4, padx=10)

        tk.Label(input_frame, text="Обновление (сек):", bg='#f5f6fa', font=("Segoe UI", 10)).grid(row=1, column=0, sticky='e', padx=5)
        self.update_interval = tk.StringVar(value='1')
        self.interval_combo = ttk.Combobox(input_frame, textvariable=self.update_interval, values=['0.5', '1', '2', '5'], state='readonly', width=5)
        self.interval_combo.grid(row=1, column=1, sticky='w', padx=5, pady=5)

        self.status_label = tk.Label(input_frame, text="Статус: Не подключено", bg='#f5f6fa', fg='red', font=("Segoe UI", 10, "bold"))
        self.status_label.grid(row=1, column=2, columnspan=2, pady=5)

        self.progress = ttk.Progressbar(input_frame, mode='indeterminate', length=150)
        self.progress.grid(row=1, column=4, pady=5)

        # --- Блок параметров (Сетка для ровного отображения) ---
        self.params_frame = tk.LabelFrame(self.root, text="Параметры сети", bg='white', font=("Segoe UI", 10, "bold"), padx=10, pady=10)
        self.params_frame.pack(fill=tk.X, padx=10, pady=5)

        for i, param in enumerate(self.params_order):
            row = i // 2
            col = (i % 2) * 2  # 0 и 2 для лейблов, 1 и 3 для значений
            
            title_lbl = tk.Label(self.params_frame, text=f"{param.upper()}:", bg='white', fg='gray', font=("Segoe UI", 9))
            title_lbl.grid(row=row, column=col, sticky='e', padx=(10, 5), pady=2)
            
            val_lbl = tk.Label(self.params_frame, text="-", bg='white', font=("Segoe UI", 10, "bold"), width=25, anchor='w')
            val_lbl.grid(row=row, column=col+1, sticky='w', padx=5, pady=2)
            self.param_labels[param] = val_lbl

        # Тренд и управление
        control_frame = tk.Frame(self.root, bg='#f5f6fa')
        control_frame.pack(fill=tk.X, padx=10, pady=5)

        self.direction_label = tk.Label(control_frame, text="Тренд сигнала: -", bg='#f5f6fa', font=("Segoe UI", 10, "bold"))
        self.direction_label.pack(side=tk.LEFT, padx=5)

        self.save_log_button = ttk.Button(control_frame, text="Сохранить лог", command=self.save_log)
        self.save_log_button.pack(side=tk.RIGHT, padx=5)

        self.reset_button = ttk.Button(control_frame, text="Сброс пиков", command=self.reset_peaks)
        self.reset_button.pack(side=tk.RIGHT, padx=5)

        # --- Блок графика ---
        graph_control_frame = tk.Frame(self.root, bg='#f5f6fa')
        graph_control_frame.pack(pady=5)
        tk.Label(graph_control_frame, text="Параметр для графика:", bg='#f5f6fa').pack(side=tk.LEFT)
        self.graph_param = tk.StringVar(value='rsrp')
        self.graph_combo = ttk.Combobox(graph_control_frame, textvariable=self.graph_param, values=self.dynamic_params, state='readonly', width=10)
        self.graph_combo.pack(side=tk.LEFT, padx=5)
        self.graph_combo.bind("<<ComboboxSelected>>", self.reset_graph)

        self.fig, self.ax = plt.subplots(figsize=(8, 3))
        self.fig.tight_layout(pad=2) # Предотвращает обрезание текста осей
        self.param_ranges = {'rsrp': (-120, -50), 'rssi': (-120, -50), 'rsrq': (-20, 0), 'sinr': (-5, 30)}
        self.times, self.values = [], {}
        self.line, = self.ax.plot([], [], linewidth=2, color='#0078D7')
        
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.setup_graph()

    # =========================================================
    # КОНФИГ И УТИЛИТЫ
    # =========================================================
    def load_config(self):
        try:
            self.config.read(self.config_file)
        except Exception:
            pass

    def save_config(self):
        # Базовая обфускация пароля (чтобы не лежал плейнтекстом)
        pw = self.password_entry.get().encode('utf-8')
        pw_b64 = base64.b64encode(pw).decode('utf-8')
        
        self.config['Settings'] = {
            'ip': self.ip_entry.get(),
            'password': pw_b64
        }
        with open(self.config_file, 'w') as configfile:
            self.config.write(configfile)

    def extract_number(self, value):
        try:
            match = re.search(r'-?\d+\.?\d*', str(value))
            return float(match.group()) if match else None
        except Exception:
            return None

    def get_unit(self, param):
        if param in ['rsrp', 'rssi']: return "dBm"
        if param in ['sinr', 'rsrq']: return "dB"
        return ""

    def get_param_color(self, param, value):
        val = self.extract_number(value)
        if val is None: return 'black'
        if param == 'rsrp': return 'green' if val > -80 else 'orange' if val > -100 else 'red'
        if param == 'rssi': return 'green' if val > -65 else 'orange' if val > -85 else 'red'
        if param == 'sinr': return 'green' if val > 15 else 'orange' if val > 0 else 'red'
        if param == 'rsrq': return 'green' if val > -6 else 'orange' if val > -12 else 'red'
        return 'black'

    def is_better(self, current, peak, param):
        cur_val, peak_val = self.extract_number(current), self.extract_number(peak)
        if cur_val is None or peak_val is None: return False
        if param in ['rsrp', 'rssi', 'sinr']: return cur_val > peak_val
        if param == 'rsrq': return abs(cur_val) < abs(peak_val)
        return False

    # =========================================================
    # ПОДКЛЮЧЕНИЕ / ОТКЛЮЧЕНИЕ
    # =========================================================
    def start_connect(self):
        if self.connected:
            self.disconnect()
            return

        self.progress.start(10)
        self.connect_button.config(state='disabled')
        self.status_label.config(text="Статус: Подключение...", fg='orange')
        
        # Безопасно запускаем инициализацию в фоне
        threading.Thread(target=self.connect_thread, daemon=True).start()

    def connect_thread(self):
        ip = self.ip_entry.get().strip()
        password = self.password_entry.get().strip()
        url = f"http://admin:{password}@{ip}/"

        try:
            connection = Connection(url, timeout=5) # ОБЯЗАТЕЛЕН ТАЙМАУТ
            client = Client(connection)
            client.device.information() # Тестовый запрос

            self.connection = connection
            self.client = client
            self.connected = True
            self.is_monitoring = True
            self.start_time = time.time()

            # Обновляем UI в главном потоке
            self.root.after(0, self.on_connected_success)

            # Запускаем фоновые задачи (теперь под флагом is_monitoring)
            threading.Thread(target=self.monitor_loop, daemon=True).start()
            threading.Thread(target=self.keep_alive_loop, daemon=True).start()

        except Exception as e:
            self.root.after(0, lambda: self.on_connected_fail(str(e)))

    def on_connected_success(self):
        self.progress.stop()
        self.connect_button.config(state='normal', text="Отключиться")
        self.status_label.config(text="Статус: Подключено", fg='green')
        self.reset_graph()

    def on_connected_fail(self, error_msg):
        self.progress.stop()
        self.connect_button.config(state='normal')
        self.status_label.config(text="Статус: Ошибка", fg='red')
        messagebox.showerror("Ошибка подключения", error_msg)

    def disconnect(self):
        self.connected = False
        self.is_monitoring = False # Убивает фоновые потоки!
        self.client = None
        self.connection = None

        self.connect_button.config(text="Подключиться")
        self.status_label.config(text="Статус: Не подключено", fg='red')
        self.direction_label.config(text="Тренд сигнала: -", fg='black')
        self.reset_graph()
        self.reset_peaks()
        self.refresh_ui(default=True)

    # =========================================================
    # ФОНОВЫЕ ПОТОКИ (Сеть не должна блокировать UI)
    # =========================================================
    def monitor_loop(self):
        while self.is_monitoring:
            if not self.connected:
                time.sleep(2)
                continue
            
            try:
                # 1. Сетевой запрос происходит в фоне (UI не зависает)
                signal = self.client.device.signal()
                status = self.client.monitoring.status()
                plmn = self.client.net.current_plmn()

                self.last_data = {**signal, **status, **plmn}
                
                # 2. Только когда данные получены, просим UI обновиться
                self.root.after(0, self.refresh_ui)

            except Exception as e:
                print(f"Network Timeout/Error: {e}")
                self.root.after(0, lambda: self.status_label.config(text="Статус: Потеря связи...", fg='orange'))
            
            # Ждем интервал
            try:
                interval = float(self.update_interval.get())
            except ValueError:
                interval = 1.0
            time.sleep(interval)

    def keep_alive_loop(self):
        while self.is_monitoring:
            try:
                if self.connected and self.client:
                    self.client.device.information()
            except Exception:
                pass
            time.sleep(30) # Раз в 30 секунд обновляем сессию

    # =========================================================
    # ОБНОВЛЕНИЕ ИНТЕРФЕЙСА (Вызывается только через root.after)
    # =========================================================
    def refresh_ui(self, default=False):
        if default:
            data = {param: '-' for param in self.params}
        else:
            data = self.last_data
            self.status_label.config(text="Статус: Подключено", fg='green')

        for param in self.params_order:
            current = data.get(param, '-')
            color = 'black'
            text = str(current)

            if param in self.dynamic_params and current != '-':
                peak = self.peak_values.get(param, '-')
                if peak == '-' or self.is_better(current, peak, param):
                    self.peak_values[param] = current
                    peak = current
                text = f"{current} (Пик: {peak})"
                color = self.get_param_color(param, current)

            self.param_labels[param].config(text=text, fg=color)

        # Вычисление тренда RSRP
        rsrp = data.get('rsrp', '-')
        current_val = self.extract_number(rsrp)
        previous_val = self.extract_number(self.previous_value)

        if current_val is not None and previous_val is not None:
            if current_val > previous_val:
                self.direction_label.config(text="Тренд: Улучшается ↗", fg='green')
            elif current_val < previous_val:
                self.direction_label.config(text="Тренд: Ухудшается ↘", fg='red')
            else:
                self.direction_label.config(text="Тренд: Стабильно →", fg='blue')
        self.previous_value = rsrp

        # Обновление графика
        param = self.graph_param.get()
        graph_val = self.extract_number(data.get(param, '-'))
        if graph_val is not None:
            self.update_graph(graph_val)

    # =========================================================
    # ГРАФИК
    # =========================================================
    def setup_graph(self):
        param = self.graph_param.get()
        self.ax.clear()
        self.line, = self.ax.plot([], [], linewidth=2, color='#0078D7')
        self.ax.set_title(f"Динамика {param.upper()}")
        self.ax.set_ylabel(self.get_unit(param))
        self.ax.grid(True, linestyle='--', alpha=0.7)
        self.ax.set_ylim(*self.param_ranges.get(param, (-120, 0)))
        self.fig.tight_layout(pad=2)
        self.canvas.draw()

    def update_graph(self, value):
        param = self.graph_param.get()
        if self.start_time is None: return

        relative_time = time.time() - self.start_time

        if param not in self.values:
            self.values[param] = []

        self.times.append(relative_time)
        self.values[param].append(value)

        # Ограничиваем массивы до 100 точек
        if len(self.times) > 100:
            self.times.pop(0)
            self.values[param].pop(0)

        self.line.set_data(self.times, self.values[param])

        # Исправлено "скользящее окно" оси X
        min_x = max(0, relative_time - (float(self.update_interval.get()) * 100))
        self.ax.set_xlim(min_x, max(10, relative_time + 1))
        
        self.canvas.draw_idle()

    def reset_graph(self, event=None):
        self.times, self.values = [], {}
        self.setup_graph()

    def reset_peaks(self):
        self.peak_values = {param: '-' for param in self.dynamic_params}
        self.previous_value = None
        self.direction_label.config(text="Тренд сигнала: -", fg='black')

    # =========================================================
    # ЛОГИ И ВЫХОД
    # =========================================================
    def save_log(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Hua4GMon_log_{timestamp}.csv"
        try:
            with open(filename, 'w', encoding='utf-8', newline='') as f:
                f.write("Время,Параметр,Значение,Пик\n")
                for param in self.params_order:
                    current = self.last_data.get(param, '-')
                    peak = self.peak_values.get(param, '-') if param in self.dynamic_params else '-'
                    f.write(f"{timestamp},{param.upper()},{current},{peak}\n")
            messagebox.showinfo("Успех", f"Лог сохранён:\n{filename}")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def on_closing(self):
        self.is_monitoring = False
        self.connected = False
        self.save_config()
        try:
            plt.close(self.fig)
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = Hua4GMon(root)
    root.mainloop()
