```python
import tkinter as tk
from tkinter import messagebox, ttk
import threading
import time
import configparser
import datetime
import re

from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


class Hua4GMon:
    def __init__(self, root):
        self.root = root
        self.root.title("Huawei 4G Monitor")
        self.root.configure(bg='white')
        self.root.minsize(800, 600)

        self.connected = False
        self.client = None
        self.connection = None
        self.last_data = {}
        self.start_time = None
        self.previous_value = None

        self.monitor_thread = None
        self.keepalive_thread = None

        self.config = configparser.ConfigParser()
        self.config_file = 'config.ini'
        self.load_config()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # =========================
        # STYLES
        # =========================
        style = ttk.Style()

        style.configure(
            "TButton",
            font=("Arial", 10),
            padding=2
        )

        style.configure(
            "TEntry",
            fieldbackground="#f0f0f0",
            foreground="black"
        )

        # =========================
        # INPUT FRAME
        # =========================
        input_frame = tk.Frame(root, bg='white', padx=2, pady=0)
        input_frame.pack(fill=tk.X)

        tk.Label(
            input_frame,
            text="IP роутера:",
            bg='white',
            font=("Arial", 10)
        ).pack(anchor='center', pady=1)

        self.ip_entry = ttk.Entry(
            input_frame,
            font=("Arial", 10),
            width=20
        )

        self.ip_entry.insert(
            0,
            self.config.get('Settings', 'ip', fallback='192.168.8.1')
        )

        self.ip_entry.pack(anchor='center', pady=1)

        tk.Label(
            input_frame,
            text="Пароль (логин: admin):",
            bg='white',
            font=("Arial", 10)
        ).pack(anchor='center', pady=1)

        self.password_entry = ttk.Entry(
            input_frame,
            show="*",
            font=("Arial", 10),
            width=20
        )

        self.password_entry.insert(
            0,
            self.config.get('Settings', 'password', fallback='')
        )

        self.password_entry.pack(anchor='center', pady=1)

        self.connect_button = ttk.Button(
            input_frame,
            text="Connect",
            command=self.start_connect,
            width=15
        )

        self.connect_button.pack(anchor='center', pady=2)

        # =========================
        # PROGRESS
        # =========================
        progress_frame = tk.Frame(input_frame, bg='white')
        progress_frame.pack(anchor='center', pady=1)

        self.progress = ttk.Progressbar(
            progress_frame,
            mode='indeterminate',
            length=100
        )

        self.progress.pack(anchor='center')

        self.progress_label = tk.Label(
            progress_frame,
            text="",
            bg='white',
            font=("Arial", 8)
        )

        self.progress_label.pack(anchor='center')

        # =========================
        # STATUS
        # =========================
        self.status_label = tk.Label(
            input_frame,
            text="Статус: Не подключено",
            bg='white',
            fg='red',
            font=("Arial", 10, "bold")
        )

        self.status_label.pack(anchor='center', pady=2)

        # =========================
        # UPDATE INTERVAL
        # =========================
        tk.Label(
            input_frame,
            text="Частота обновления (сек):",
            bg='white',
            font=("Arial", 10)
        ).pack(anchor='center', pady=1)

        self.update_interval = tk.StringVar(value='0.5')

        self.interval_combo = ttk.Combobox(
            input_frame,
            textvariable=self.update_interval,
            values=['0.5', '1', '2'],
            font=("Arial", 10),
            width=5,
            state='readonly'
        )

        self.interval_combo.pack(anchor='center', pady=1)

        # =========================
        # PARAMS FRAME
        # =========================
        self.params_frame = tk.Frame(root, bg='white')
        self.params_frame.pack(fill=tk.X)

        self.left_frame = tk.Frame(self.params_frame, bg='white')
        self.right_frame = tk.Frame(self.params_frame, bg='white')

        self.left_frame.pack(
            side=tk.LEFT,
            padx=5,
            fill=tk.BOTH,
            expand=True
        )

        self.right_frame.pack(
            side=tk.RIGHT,
            padx=5,
            fill=tk.BOTH,
            expand=True
        )

        self.dynamic_params = ['rssi', 'rsrp', 'rsrq', 'sinr']

        self.static_params = [
            'cell_id',
            'band',
            'mode',
            'CurrentOperator',
            'ConnectionStatus',
            'CurrentNetworkType',
            'SignalStrength',
            'plmn',
            'lac',
            'rrc_state',
            'lte_bandwidth'
        ]

        self.params = self.dynamic_params + self.static_params

        self.param_labels = {}

        self.init_params()

        # =========================
        # DIRECTION LABEL
        # =========================
        self.direction_label = tk.Label(
            root,
            text="Направление: -",
            bg='white',
            fg='black',
            font=("Arial", 10)
        )

        self.direction_label.pack(pady=2)

        # =========================
        # BUTTONS
        # =========================
        button_frame = tk.Frame(root, bg='white')
        button_frame.pack(anchor='center')

        self.reset_button = ttk.Button(
            button_frame,
            text="Сброс пиков",
            command=self.reset_peaks,
            width=15
        )

        self.reset_button.pack(side=tk.LEFT, padx=2)

        self.save_log_button = ttk.Button(
            button_frame,
            text="Сохранить лог",
            command=self.save_log,
            width=15
        )

        self.save_log_button.pack(side=tk.LEFT, padx=2)

        # =========================
        # GRAPH PARAM
        # =========================
        tk.Label(
            root,
            text="Параметр для графика:",
            bg='white',
            font=("Arial", 10)
        ).pack(anchor='center')

        self.graph_param = tk.StringVar(value='rsrp')

        self.graph_combo = ttk.Combobox(
            root,
            textvariable=self.graph_param,
            values=self.dynamic_params,
            state='readonly',
            width=20
        )

        self.graph_combo.pack(anchor='center', pady=2)
        self.graph_combo.bind("<<ComboboxSelected>>", self.reset_graph)

        # =========================
        # GRAPH
        # =========================
        self.fig, self.ax = plt.subplots(figsize=(8, 2))

        self.param_ranges = {
            'rsrp': (-120, -50),
            'rssi': (-120, -50),
            'rsrq': (-20, 0),
            'sinr': (-5, 30)
        }

        self.times = []
        self.values = {}

        self.line, = self.ax.plot([], [])

        self.setup_graph()

        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.canvas.draw()

        self.peak_values = {
            param: '-'
            for param in self.dynamic_params
        }

    # =========================================================
    # CONFIG
    # =========================================================
    def load_config(self):
        try:
            self.config.read(self.config_file)
        except Exception as e:
            print("Config load error:", e)

    def save_config(self):
        try:
            self.config['Settings'] = {
                'ip': self.ip_entry.get(),
                'password': self.password_entry.get()
            }

            with open(self.config_file, 'w') as configfile:
                self.config.write(configfile)

        except Exception as e:
            print("Config save error:", e)

    # =========================================================
    # UTILS
    # =========================================================
    def extract_number(self, value):
        try:
            match = re.search(r'-?\d+\.?\d*', str(value))
            return float(match.group()) if match else None
        except Exception:
            return None

    def get_unit(self, param):
        if param in ['rsrp', 'rssi']:
            return "dBm"

        if param in ['sinr', 'rsrq']:
            return "dB"

        return ""

    def get_param_color(self, param, value):
        val = self.extract_number(value)

        if val is None:
            return 'black'

        if param == 'rsrp':
            return 'green' if val > -80 else 'orange' if val > -100 else 'red'

        if param == 'rssi':
            return 'green' if val > -65 else 'orange' if val > -85 else 'red'

        if param == 'sinr':
            return 'green' if val > 20 else 'orange' if val > 0 else 'red'

        if param == 'rsrq':
            return 'green' if val > -6 else 'orange' if val > -12 else 'red'

        return 'black'

    def is_better(self, current, peak, param):
        cur_val = self.extract_number(current)
        peak_val = self.extract_number(peak)

        if cur_val is None or peak_val is None:
            return False

        if param in ['rsrp', 'rssi', 'sinr']:
            return cur_val > peak_val

        if param == 'rsrq':
            return abs(cur_val) < abs(peak_val)

        return False

    # =========================================================
    # GRAPH
    # =========================================================
    def setup_graph(self):
        param = self.graph_param.get()

        self.ax.clear()

        self.line, = self.ax.plot([], [])

        self.ax.set_title(f"Уровень сигнала ({param.upper()})")
        self.ax.set_xlabel("Время (сек)")
        self.ax.set_ylabel(f"Значение ({self.get_unit(param)})")

        self.ax.grid(True)

        self.ax.set_xlim(0, 10)
        self.ax.set_ylim(*self.param_ranges[param])

    def update_graph(self, value):
        param = self.graph_param.get()

        if self.start_time is None:
            return

        relative_time = time.time() - self.start_time

        if param not in self.values:
            self.values[param] = []

        self.times.append(relative_time)
        self.values[param].append(value)

        if len(self.times) > 100:
            self.times.pop(0)

            if self.values[param]:
                self.values[param].pop(0)

        self.line.set_data(self.times, self.values[param])

        self.ax.set_xlim(0, max(10, relative_time + 1))
        self.ax.relim()
        self.ax.autoscale_view(True, True, True)

        self.canvas.draw_idle()

    def reset_graph(self, event=None):
        self.times = []
        self.values = {}

        self.setup_graph()

        self.canvas.draw_idle()

    # =========================================================
    # PARAMS
    # =========================================================
    def init_params(self):
        params_order = [
            'rssi',
            'rsrp',
            'lac',
            'rsrq',
            'rrc_state',
            'sinr',
            'lte_bandwidth',
            'cell_id',
            'band',
            'mode',
            'CurrentOperator',
            'ConnectionStatus',
            'CurrentNetworkType',
            'SignalStrength',
            'plmn'
        ]

        for i, param in enumerate(params_order):

            frame = (
                self.left_frame
                if i < len(params_order) // 2
                else self.right_frame
            )

            label = tk.Label(
                frame,
                text=f"{param.upper()}: -",
                bg='white',
                fg='blue',
                font=("Arial", 10, "bold"),
                anchor='w'
            )

            label.pack(fill=tk.X, pady=1)

            self.param_labels[param] = label

    # =========================================================
    # CONNECT
    # =========================================================
    def start_connect(self):

        if self.connected:
            self.disconnect()
            return

        self.progress.start(10)

        self.progress_label.config(text="Подключение...")

        self.connect_button.config(state='disabled')

        threading.Thread(
            target=self.connect_thread,
            daemon=True
        ).start()

    def connect_thread(self):

        ip = self.ip_entry.get().strip()
        password = self.password_entry.get().strip()

        url = f"http://admin:{password}@{ip}/"

        try:
            connection = Connection(url)
            client = Client(connection)

            # test request
            client.device.information()

            self.connection = connection
            self.client = client

            self.connected = True

            self.fetch_data()

            self.start_time = time.time()

            self.root.after(0, self.on_connected)

            self.monitor_thread = threading.Thread(
                target=self.monitor_loop,
                daemon=True
            )

            self.monitor_thread.start()

            self.keepalive_thread = threading.Thread(
                target=self.keep_alive_loop,
                daemon=True
            )

            self.keepalive_thread.start()

        except Exception as e:

            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Ошибка подключения",
                    str(e)
                )
            )

        finally:

            self.root.after(0, self.progress.stop)

            self.root.after(
                0,
                lambda: self.progress_label.config(text="")
            )

            self.root.after(
                0,
                lambda: self.connect_button.config(state='normal')
            )

    def on_connected(self):

        self.connect_button.config(text="Disconnect")

        self.status_label.config(
            text="Статус: Подключено",
            fg='green'
        )

        self.reset_graph()

        self.update_params()

    def disconnect(self):

        self.connected = False

        self.client = None
        self.connection = None

        self.connect_button.config(text="Connect")

        self.status_label.config(
            text="Статус: Не подключено",
            fg='red'
        )

        self.direction_label.config(
            text="Направление: -",
            fg='black'
        )

        self.reset_graph()
        self.reset_peaks()

        self.update_params(default=True)

    # =========================================================
    # DATA
    # =========================================================
    def fetch_data(self):

        if not self.client:
            return self.last_data

        try:

            signal = self.client.device.signal()

            status = self.client.monitoring.status()

            plmn = self.client.net.current_plmn()

            data = {
                **signal,
                **status,
                **plmn
            }

            self.last_data = data

            return data

        except Exception as e:

            print("Fetch error:", e)

            self.connected = False

            return self.last_data

    # =========================================================
    # RECONNECT
    # =========================================================
    def reconnect(self):

        if self.connected:
            return

        self.root.after(
            0,
            lambda: self.status_label.config(
                text="Статус: Переподключение...",
                fg='orange'
            )
        )

        ip = self.ip_entry.get().strip()
        password = self.password_entry.get().strip()

        url = f"http://admin:{password}@{ip}/"

        try:

            connection = Connection(url)
            client = Client(connection)

            client.device.information()

            self.connection = connection
            self.client = client

            self.connected = True

            self.start_time = time.time()

            self.root.after(
                0,
                lambda: self.status_label.config(
                    text="Статус: Подключено",
                    fg='green'
                )
            )

        except Exception as e:

            print("Reconnect error:", e)

            self.root.after(
                0,
                lambda: self.status_label.config(
                    text="Статус: Ошибка подключения",
                    fg='red'
                )
            )

    # =========================================================
    # UPDATE PARAMS
    # =========================================================
    def update_params(self, default=False):

        if default:

            data = {
                param: '-'
                for param in self.params
            }

        else:

            data = self.fetch_data()

        params_order = [
            'rssi',
            'rsrp',
            'lac',
            'rsrq',
            'rrc_state',
            'sinr',
            'lte_bandwidth',
            'cell_id',
            'band',
            'mode',
            'CurrentOperator',
            'ConnectionStatus',
            'CurrentNetworkType',
            'SignalStrength',
            'plmn'
        ]

        for param in params_order:

            current = data.get(param, '-')

            if param in self.dynamic_params:

                peak = self.peak_values.get(param, '-')

                if (
                    current != '-'
                    and (
                        peak == '-'
                        or self.is_better(current, peak, param)
                    )
                ):
                    self.peak_values[param] = current
                    peak = current

                text = f"{param.upper()}: {current} (пик: {peak})"

                color = self.get_param_color(param, current)

            else:

                text = f"{param.upper()}: {current}"

                color = 'black'

            self.param_labels[param].config(
                text=text,
                fg=color
            )

        # direction
        rsrp = data.get('rsrp', '-')

        current_val = self.extract_number(rsrp)

        previous_val = self.extract_number(self.previous_value)

        if current_val is not None and previous_val is not None:

            if current_val > previous_val:

                self.direction_label.config(
                    text="Направление: Сигнал улучшается",
                    fg='green'
                )

            elif current_val < previous_val:

                self.direction_label.config(
                    text="Направление: Сигнал ухудшается",
                    fg='red'
                )

            else:

                self.direction_label.config(
                    text="Направление: Сигнал стабилен",
                    fg='black'
                )

        self.previous_value = rsrp

        # graph
        param = self.graph_param.get()

        value = self.extract_number(data.get(param, '-'))

        if value is not None:
            self.update_graph(value)

    # =========================================================
    # THREADS
    # =========================================================
    def monitor_loop(self):

        while True:

            if not self.connected:

                self.reconnect()

                time.sleep(5)

                continue

            try:

                self.root.after(0, self.update_params)

                interval = float(self.update_interval.get())

                time.sleep(interval)

            except Exception as e:

                print("Monitor error:", e)

                time.sleep(2)

    def keep_alive_loop(self):

        while True:

            try:

                if self.connected and self.client:

                    self.client.device.information()

            except Exception as e:

                print("KeepAlive error:", e)

                self.connected = False

            time.sleep(30)

    # =========================================================
    # PEAKS
    # =========================================================
    def reset_peaks(self):

        self.peak_values = {
            param: '-'
            for param in self.dynamic_params
        }

        self.previous_value = None

        self.direction_label.config(
            text="Направление: -",
            fg='black'
        )

    # =========================================================
    # LOG
    # =========================================================
    def save_log(self):

        timestamp = datetime.datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        filename = f"Hua4GMon_log_{timestamp}.csv"

        try:

            with open(
                filename,
                'w',
                encoding='utf-8',
                newline=''
            ) as f:

                f.write("Время,Параметр,Значение,Пик\n")

                for param in self.params:

                    current = self.last_data.get(param, '-')

                    peak = (
                        self.peak_values.get(param, '-')
                        if param in self.dynamic_params
                        else '-'
                    )

                    f.write(
                        f"{timestamp},"
                        f"{param.upper()},"
                        f"{current},"
                        f"{peak}\n"
                    )

            messagebox.showinfo(
                "Успех",
                f"Лог сохранён:\n{filename}"
            )

        except Exception as e:

            messagebox.showerror(
                "Ошибка",
                str(e)
            )

    # =========================================================
    # CLOSE
    # =========================================================
    def on_closing(self):

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
```
