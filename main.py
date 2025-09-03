import tkinter as tk
from tkinter import messagebox, ttk
import threading
import time
import configparser
import datetime
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

class Hua4GMon:
    def __init__(self, root):
        self.root = root
        self.root.title("Huawei 4G Monitor")
        self.root.configure(bg='white')
        self.root.geometry("800x700")

        self.config = configparser.ConfigParser()
        self.config_file = 'config.ini'
        self.load_config()

        # Обработчик закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Стили для кнопок
        style = ttk.Style()
        style.configure("TButton", font=("Arial", 12), padding=5)
        style.map("TButton", background=[('active', '#4CAF50')])

        # Grid компоновка
        self.root.rowconfigure(3, weight=1)
        self.root.rowconfigure(6, weight=1)
        self.root.columnconfigure(0, weight=1)

        # Контейнер для ввода
        input_frame = tk.Frame(root, bg='white', padx=10, pady=10)
        input_frame.grid(row=0, column=0, sticky='ew')

        tk.Label(input_frame, text="IP роутера:", bg='white', font=("Arial", 12)).pack()
        self.ip_entry = tk.Entry(input_frame, font=("Arial", 12))
        self.ip_entry.insert(0, self.config.get('Settings', 'ip', fallback='192.168.8.1'))
        self.ip_entry.pack()

        tk.Label(input_frame, text="Пароль (логин: admin):", bg='white', font=("Arial", 12)).pack()
        self.password_entry = tk.Entry(input_frame, show="*", font=("Arial", 12))
        self.password_entry.insert(0, self.config.get('Settings', 'password', fallback=''))
        self.password_entry.pack()

        self.remember_var = tk.BooleanVar(value=bool(self.config.get('Settings', 'remember', fallback=False)))
        tk.Checkbutton(input_frame, text="Запомнить данные", variable=self.remember_var, bg='white', font=("Arial", 10)).pack()

        self.connect_button = ttk.Button(input_frame, text="Connect", command=self.connect, style="TButton")
        self.connect_button.pack(pady=5)

        # Выбор частоты обновления
        tk.Label(input_frame, text="Частота обновления (сек):", bg='white', font=("Arial", 12)).pack()
        self.update_interval = tk.StringVar(value='0.5')
        self.interval_combo = ttk.Combobox(input_frame, textvariable=self.update_interval, values=['0.5', '1', '2'], font=("Arial", 12))
        self.interval_combo.pack()

        # Статус подключения
        self.status_label = tk.Label(root, text="Статус: Не подключено", bg='white', fg='red', font=("Arial", 12, "bold"))
        self.status_label.grid(row=1, column=0, sticky='ew')

        # Контейнер для параметров (две колонки)
        self.params_frame = tk.Frame(root, bg='white', padx=10, pady=10)
        self.params_frame.grid(row=2, column=0, sticky='nsew')
        self.param_labels = {}
        self.dynamic_params = ['rssi', 'rsrp', 'rsrq', 'sinr']
        self.static_params = ['cell_id', 'band', 'mode', 'CurrentOperator', 'ConnectionStatus', 'CurrentNetworkType', 'SignalStrength', 'plmn']
        self.params = self.dynamic_params + self.static_params
        self.init_params()

        # Кнопки управления
        button_frame = tk.Frame(root, bg='white', pady=5)
        button_frame.grid(row=3, column=0, sticky='ew')
        button_frame.columnconfigure(0, weight=1)
        button_frame.columnconfigure(2, weight=1)
        self.reset_button = ttk.Button(button_frame, text="Сброс пиков", command=self.reset_peaks, style="TButton")
        self.reset_button.grid(row=0, column=1, sticky='center')
        self.save_log_button = ttk.Button(button_frame, text="Сохранить лог", command=self.save_log, style="TButton")
        self.save_log_button.grid(row=0, column=2, sticky='center', padx=5)

        # Выбор графика
        tk.Label(root, text="Параметр для графика:", bg='white', font=("Arial", 12)).grid(row=4, column=0, sticky='', padx=10)
        self.graph_param = tk.StringVar(value='rsrp')
        self.graph_combo = ttk.Combobox(root, textvariable=self.graph_param, values=self.dynamic_params, font=("Arial", 12), width=20, state='readonly')
        self.graph_combo.grid(row=5, column=0, sticky='', padx=10, pady=(0, 10))
        self.graph_combo.bind("<1>", lambda event: self.graph_combo.event_generate("<Down>"))
        self.graph_combo.bind("<<ComboboxSelected>>", self.reset_graph)

        # Диаграмма
        self.fig, self.ax = plt.subplots(figsize=(8, 4.5))
        self.ax.set_title("Уровень сигнала", fontsize=12, pad=20)
        self.ax.set_xlabel("Время (сек)", fontsize=10)
        self.ax.set_ylabel("Значение", fontsize=10)
        self.ax.grid(True)
        self.ax.set_xlim(0, 10)
        self.ax.set_ylim(-100, 0)  # Начальный диапазон для RSRP
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        canvas_widget = self.canvas.get_tk_widget()
        canvas_widget.grid(row=6, column=0, sticky='nsew')
        canvas_widget.configure(width=700, height=500)
        self.root.update_idletasks()
        self.canvas.draw()
        self.update_graph_initial()

        self.times = []
        self.values = {}
        self.peak_values = {}
        self.connected = False
        self.client = None
        self.connection = None
        self.last_data = {}
        self.start_time = None

    def load_config(self):
        try:
            self.config.read(self.config_file)
        except:
            pass

    def save_config(self):
        if self.remember_var.get():
            if not self.config.has_section('Settings'):
                self.config.add_section('Settings')
            self.config.set('Settings', 'ip', self.ip_entry.get())
            self.config.set('Settings', 'password', self.password_entry.get())
            self.config.set('Settings', 'remember', 'True')
            with open(self.config_file, 'w') as f:
                self.config.write(f)
        else:
            try:
                import os
                os.remove(self.config_file)
            except:
                pass

    def connect(self):
        if self.connected:
            self.connected = False
            self.connect_button.config(text="Connect")
            self.status_label.config(text="Статус: Не подключено", fg='red')
            self.reset_peaks()
            self.reset_graph()
            self.update_params(default=True)
            self.client = None
            self.connection = None
            self.start_time = None
            return

        ip = self.ip_entry.get()
        password = self.password_entry.get()
        url = f"http://admin:{password}@{ip}/"

        try:
            self.connection = Connection(url)
            self.client = Client(self.connection)
            self.fetch_data()
            self.connected = True
            self.connect_button.config(text="Disconnect")
            self.status_label.config(text="Статус: Подключено", fg='green')
            self.save_config()
            self.update_params()
            self.start_time = time.time()
            self.reset_graph()
            threading.Thread(target=self.monitor_loop, daemon=True).start()
            threading.Thread(target=self.keep_alive_loop, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось подключиться: {str(e)}")

    def fetch_data(self):
        if not self.connected or not self.client:
            return self.last_data
        try:
            signal = self.client.device.signal()
            status = self.client.monitoring.status()
            plmn = self.client.net.current_plmn()
            data = {**signal, **status, **plmn}
            self.last_data = data
            return data
        except Exception:
            self.reconnect()
            return self.last_data

    def reconnect(self):
        self.connected = False
        self.client = None
        self.connection = None
        self.status_label.config(text="Статус: Ожидание подключения", fg='orange')
        ip = self.ip_entry.get()
        password = self.password_entry.get()
        url = f"http://admin:{password}@{ip}/"
        try:
            self.connection = Connection(url)
            self.client = Client(self.connection)
            self.connected = True
            self.status_label.config(text="Статус: Подключено", fg='green')
            self.start_time = time.time()
        except Exception:
            self.status_label.config(text="Статус: Ошибка, повторная попытка...", fg='red')
            self.root.after(5000, self.reconnect)

    def keep_alive_loop(self):
        while self.connected:
            try:
                if self.client:
                    self.client.device.information()
            except Exception:
                self.reconnect()
            time.sleep(30)

    def init_params(self):
        left_frame = tk.Frame(self.params_frame, bg='white')
        right_frame = tk.Frame(self.params_frame, bg='white')
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=15)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=15)
        for i, param in enumerate(self.params):
            frame = left_frame if i % 2 == 0 else right_frame
            text = f"{param.upper()}: -" if param in self.static_params else f"{param.upper()}: - (пик: -)"
            label = tk.Label(frame, text=text, bg='white', fg='blue', font=("Arial", 12, "bold"), anchor='w', wraplength=300)
            label.pack(fill=tk.X, pady=2)
            self.param_labels[param] = label

    def get_param_color(self, param, value):
        try:
            val = float(''.join(c for c in str(value) if c.isdigit() or c in ['-', '.']))
            if param == 'rsrp':
                return 'green' if val > -80 else 'orange' if val > -100 else 'red'
            elif param == 'rssi':
                return 'green' if val > -65 else 'orange' if val > -85 else 'red'
            elif param == 'sinr':
                return 'green' if val > 20 else 'orange' if val > 0 else 'red'
            elif param == 'rsrq':
                return 'green' if val > -6 else 'orange' if val > -12 else 'red'
        except:
            pass
        return 'black'

    def get_unit(self, param):
        if param in ['rsrp', 'rssi']:
            return "dBm"
        elif param in ['sinr', 'rsrq']:
            return "dB"
        return ""

    def save_log(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Hua4GMon_log_{timestamp}.txt"
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(f"Huawei 4G Monitor Log - {timestamp}\n")
                f.write(f"Статус: {self.status_label.cget('text')}\n")
                f.write("Параметры:\n")
                for param in self.params:
                    current = self.last_data.get(param, '-')
                    if param in self.dynamic_params:
                        peak = self.peak_values.get(param, '-')
                        f.write(f"{param.upper()}: {current} (пик: {peak})\n")
                    else:
                        f.write(f"{param.upper()}: {current}\n")
            messagebox.showinfo("Успех", f"Лог сохранён в {filename}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить лог: {str(e)}")

    def update_params(self, default=False):
        if default:
            data = {param: '-' for param in self.params}
            self.peak_values = {param: '-' for param in self.dynamic_params}
        else:
            data = self.fetch_data()
            for param in self.dynamic_params:
                if param in data:
                    current = data[param]
                    peak = self.peak_values.get(param, '-')
                    if peak == '-' or self.is_better(current, peak, param):
                        self.peak_values[param] = current
                        peak = current
                        self.param_labels[param].config(fg='green')
                        self.root.after(2000, lambda p=param: self.param_labels[p].config(fg=self.get_param_color(p, self.last_data.get(p, '-'))))
                else:
                    current = '-'
                    peak = self.peak_values.get(param, '-')
            for param in self.static_params:
                current = data.get(param, '-')

        for param in self.params:
            current = data.get(param, '-')
            if param in self.dynamic_params:
                peak = self.peak_values.get(param, '-')
                text = f"{param.upper()}: {current} (пик: {peak})"
                color = self.get_param_color(param, current)
            else:
                text = f"{param.upper()}: {current}"
                color = 'black'
            self.param_labels[param].config(text=text, fg=color if current != '-' else 'black')

        # Обновить график
        if not default and self.start_time is not None:
            param = self.graph_param.get()
            if param in data:
                val_str = data[param]
                try:
                    val = float(''.join(c for c in str(val_str) if c.isdigit() or c in ['-', '.']))
                    if param not in self.values:
                        self.values[param] = []
                    relative_time = time.time() - self.start_time
                    self.times.append(relative_time)
                    self.values[param].append(val)
                    if len(self.times) > 100:
                        self.times.pop(0)
                        self.values[param].pop(0)
                    self.ax.clear()
                    self.ax.plot(self.times, self.values[param], color='blue')
                    self.ax.set_title(f"Уровень сигнала ({param.upper()})", fontsize=12, pad=20)
                    self.ax.set_xlabel("Время (сек)", fontsize=10)
                    self.ax.set_ylabel(f"Значение ({self.get_unit(param)})", fontsize=10)
                    self.ax.grid(True)
                    self.ax.set_xlim(0, max(10, max(self.times) + 1))
                    self.ax.set_ylim(-100, 0)  # Динамический диапазон для RSRP
                    self.fig.tight_layout()
                    self.canvas.draw()
                except ValueError:
                    pass

    def update_graph_initial(self):
        param = self.graph_param.get()
        self.ax.clear()
        self.ax.plot([], [], color='blue')
        self.ax.set_title(f"Уровень сигнала ({param.upper()})", fontsize=12, pad=20)
        self.ax.set_xlabel("Время (сек)", fontsize=10)
        self.ax.set_ylabel(f"Значение ({self.get_unit(param)})", fontsize=10)
        self.ax.grid(True)
        self.ax.set_xlim(0, 10)
        self.ax.set_ylim(-100, 0)
        self.fig.tight_layout()
        self.ax.figure.canvas.draw()  # Принудительное обновление

    def is_better(self, current, peak, param):
        try:
            cur_val = float(''.join(c for c in str(current) if c.isdigit() or c in ['-', '.']))
            peak_val = float(''.join(c for c in str(peak) if c.isdigit() or c in ['-', '.']))
            if param in ['rsrp', 'rssi', 'sinr']:
                return cur_val > peak_val
            elif param == 'rsrq':
                return abs(cur_val) < abs(peak_val)
            return False
        except:
            return False

    def reset_peaks(self):
        self.peak_values = {param: '-' for param in self.dynamic_params}
        self.update_params()

    def reset_graph(self, event=None):
        self.times = []
        self.values = {}
        self.ax.clear()
        self.ax.set_title("Уровень сигнала", fontsize=12, pad=20)
        self.ax.set_xlabel("Время (сек)", fontsize=10)
        self.ax.set_ylabel("Значение", fontsize=10)
        self.ax.grid(True)
        self.ax.set_xlim(0, 10)
        self.ax.set_ylim(-100, 0)
        self.fig.tight_layout()
        self.canvas.draw()

    def on_closing(self):
        self.connected = False
        self.client = None
        self.connection = None
        plt.close(self.fig)
        self.root.destroy()

    def monitor_loop(self):
        while self.connected:
            try:
                self.root.after(0, self.update_params)
                time.sleep(float(self.update_interval.get()))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
                self.connected = False
                self.update_params(default=True)

if __name__ == "__main__":
    root = tk.Tk()
    app = Hua4GMon(root)
    root.mainloop()
