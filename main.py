import tkinter as tk
from tkinter import messagebox, ttk
import threading
import time
import configparser
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

class Hua4GMon:
    def __init__(self, root):
        self.root = root
        self.root.title("Huawei 4G Monitor")
        self.root.configure(bg='white')
        self.root.geometry("600x800")  # Увеличим для больше параметров

        self.config = configparser.ConfigParser()
        self.config_file = 'config.ini'
        self.load_config()

        # Поля ввода
        tk.Label(root, text="IP роутера:", bg='white').pack()
        self.ip_entry = tk.Entry(root)
        self.ip_entry.insert(0, self.config.get('Settings', 'ip', fallback='192.168.8.1'))
        self.ip_entry.pack()

        tk.Label(root, text="Пароль (логин: admin):", bg='white').pack()
        self.password_entry = tk.Entry(root, show="*")
        self.password_entry.insert(0, self.config.get('Settings', 'password', fallback=''))
        self.password_entry.pack()

        self.remember_var = tk.BooleanVar(value=bool(self.config.get('Settings', 'remember', fallback=False)))
        tk.Checkbutton(root, text="Запомнить данные", variable=self.remember_var, bg='white').pack()

        self.connect_button = tk.Button(root, text="Connect", command=self.connect)
        self.connect_button.pack()

        # Выбор графика
        tk.Label(root, text="Параметр для графика:", bg='white').pack()
        self.graph_param = tk.StringVar(value='rsrp')
        self.graph_combo = ttk.Combobox(root, textvariable=self.graph_param, values=['rsrp', 'rssi', 'sinr', 'rsrq'])
        self.graph_combo.pack()
        self.graph_combo.bind("<<ComboboxSelected>>", self.reset_graph)

        # Место для параметров
        self.params_frame = tk.Frame(root, bg='white')
        self.params_frame.pack(fill=tk.BOTH, expand=True)

        # Кнопка сброса пиков
        self.reset_button = tk.Button(root, text="Сброс пиков", command=self.reset_peaks)
        self.reset_button.pack()
        self.reset_button.pack_forget()  # Скрыть до подключения

        # Диаграмма
        self.fig, self.ax = plt.subplots()
        self.ax.set_title("Уровень сигнала")
        self.ax.set_xlabel("Время")
        self.ax.set_ylabel("Значение")
        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.get_tk_widget().pack_forget()  # Скрыть до подключения

        self.times = []
        self.values = {}  # Словарь для значений по параметрам
        self.peak_values = {}  # Пиковые значения
        self.connected = False
        self.client = None

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
            self.canvas.get_tk_widget().pack_forget()
            self.reset_button.pack_forget()
            for widget in self.params_frame.winfo_children():
                widget.destroy()
            return

        ip = self.ip_entry.get()
        password = self.password_entry.get()
        url = f"http://admin:{password}@{ip}/"

        try:
            connection = Connection(url)
            self.client = Client(connection)
            self.fetch_data()  # Первичный fetch
            self.connected = True
            self.connect_button.config(text="Disconnect")
            self.save_config()
            self.show_params()
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            self.reset_button.pack()
            threading.Thread(target=self.monitor_loop, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось подключиться: {str(e)}")

    def fetch_data(self):
        signal = self.client.device.signal()
        status = self.client.monitoring.status()
        plmn = self.client.net.current_plmn()
        return {**signal, **status, **plmn}  # Объединим все данные

    def show_params(self):
        for widget in self.params_frame.winfo_children():
            widget.destroy()

        data = self.fetch_data()
        params = ['rssi', 'rsrp', 'rsrq', 'sinr', 'cell_id', 'band', 'mode', 'CurrentOperator', 'ConnectionStatus', 'CurrentNetworkType', 'SignalStrength', 'plmn']  # Расширенный список
        self.param_labels = {}
        for param in params:
            if param in data:
                current = data[param]
                peak = self.peak_values.get(param, current)
                if self.is_better(current, peak, param):
                    self.peak_values[param] = current
                    peak = current

                label = tk.Label(self.params_frame, text=f"{param.upper()}: {current} (пик: {peak})", bg='white')
                label.pack(anchor='w')
                self.param_labels[param] = label

    def is_better(self, current, peak, param):
        try:
            cur_val = float(''.join(c for c in str(current) if c.isdigit() or c in ['-', '.']))
            peak_val = float(''.join(c for c in str(peak) if c.isdigit() or c in ['-', '.']))
            if param in ['rsrp', 'rssi', 'sinr']:  # Выше лучше
                return cur_val > peak_val
            elif param == 'rsrq':  # Ближе к 0 лучше
                return abs(cur_val) < abs(peak_val)
            return False
        except:
            return False

    def reset_peaks(self):
        self.peak_values = {}
        self.show_params()

    def reset_graph(self, event=None):
        self.times = []
        self.values = {}

    def monitor_loop(self):
        while self.connected:
            try:
                self.root.after(0, lambda: self.update_params())
                time.sleep(0.5)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
                self.connected = False

    def update_params(self):
        self.show_params()
        # Обновить график
        data = self.fetch_data()
        param = self.graph_param.get()
        if param in data:
            val_str = data[param]
            try:
                val = float(''.join(c for c in str(val_str) if c.isdigit() or c in ['-', '.']))
                self.times.append(time.time())
                if param not in self.values:
                    self.values[param] = []
                self.values[param].append(val)
                if len(self.times) > 100:
                    self.times.pop(0)
                    self.values[param].pop(0)
                self.ax.clear()
                self.ax.plot(self.times, self.values[param])
                self.ax.set_title(f"Уровень сигнала ({param.upper()})")
                self.ax.set_xlabel("Время")
                self.ax.set_ylabel("Значение")
                self.canvas.draw()
            except ValueError:
                pass

if __name__ == "__main__":
    root = tk.Tk()
    app = Hua4GMon(root)
    root.mainloop()
