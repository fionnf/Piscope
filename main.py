import tkinter as tk
from tkinter import ttk, messagebox
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np
import threading
import time
import csv
from datetime import datetime
import os
import ctypes

print("Starting PicoScope Spin Frequency Analyzer...")

# Try to import picosdk, fallback to Mock if not available
try:
    from picosdk.ps2000 import ps2000
    from picosdk.functions import adc2mV, assert_pico2000_ok
    PICOSDK_AVAILABLE = True
except (ImportError, Exception):
    PICOSDK_AVAILABLE = False
    print("PicoSDK not found or drivers missing. Running in simulation mode.")


class ScopeInterface:
    def connect(self): raise NotImplementedError
    def disconnect(self): raise NotImplementedError
    def get_data(self): raise NotImplementedError
    def set_timebase(self, index): raise NotImplementedError
    def set_range(self, index): raise NotImplementedError

class MockScope(ScopeInterface):
    def __init__(self):
        self.connected = False
        self.timebase = 1
        self.range_idx = 6

    def connect(self):
        print("Mock Scope Connected")
        self.connected = True
        return True

    def disconnect(self):
        print("Mock Scope Disconnected")
        self.connected = False

    def set_timebase(self, index):
        self.timebase = index
        print(f"Mock Scope Timebase: {index}")

    def set_range(self, index):
        self.range_idx = index
        print(f"Mock Scope Range: {index}")

    def get_data(self):
        # Generate fake sine wave with noise
        t = np.linspace(0, 0.01 * (self.timebase + 1), 1000)
        amplitude = self.range_idx * 0.5 # Fake amplitude scaling
        freq = 50 + np.random.normal(0, 1) # ~50 Hz
        y = amplitude * np.sin(2 * np.pi * freq * t) + 0.1 * np.random.normal(size=len(t))
        return t, y, freq

class RealPicoScope(ScopeInterface):
    def __init__(self):
        self.chandle = ctypes.c_int16()
        self.status = {}
        self.timebase = 8
        self.oversample = 1
        self.max_samples = 2000
        self.range_idx = 6 # Default 5V

    def connect(self):
        if not PICOSDK_AVAILABLE:
            return False

        # Open the unit
        self.status["openunit"] = ps2000.ps2000_open_unit()
        self.chandle = ctypes.c_int16(self.status["openunit"])

        if self.chandle.value > 0:
            print(f"PicoScope Connected. Handle: {self.chandle.value}")
            return True
        else:
            print("Failed to open PicoScope")
            return False

    def disconnect(self):
        if self.chandle.value > 0:
            ps2000.ps2000_close_unit(self.chandle)
            print("PicoScope Closed")

    def set_timebase(self, index):
        self.timebase = index

    def set_range(self, index):
        self.range_idx = index

    def get_data(self):
        # Setup channel A
        # Range: self.range_idx, AC coupled (0)
        ps2000.ps2000_set_channel(self.chandle, 0, 1, 0, self.range_idx)

        # Setup collection
        time_interval_ns = ctypes.c_int32()
        time_units = ctypes.c_int32()
        oversample = ctypes.c_int16(1)

        ps2000.ps2000_get_timebase(self.chandle, self.timebase, self.max_samples,
                                   ctypes.byref(time_interval_ns), ctypes.byref(time_units),
                                   oversample, ctypes.byref(ctypes.c_int32()))

        # Run block
        ps2000.ps2000_run_block(self.chandle, self.max_samples, self.timebase, oversample, ctypes.byref(time_interval_ns))

        # Wait for ready
        while ps2000.ps2000_ready(self.chandle) == 0:
            time.sleep(0.001)

        # Get values
        buffer_a = (ctypes.c_int16 * self.max_samples)()
        overflow = ctypes.c_int16()

        # ps2000_get_values(handle, buffer_a, buffer_b, buffer_c, buffer_d, overflow, no_of_samples)
        ps2000.ps2000_get_values(self.chandle, ctypes.byref(buffer_a), None, None, None, ctypes.byref(overflow), self.max_samples)

        # Convert to mV
        # maxADC for PS2000 is 32767
        cmaxADC = ctypes.c_int16(32767)
        data_mV = np.array(adc2mV(buffer_a, self.range_idx, cmaxADC))

        # Determine frequency (dummy calculation for now based on data)
        # Real calc from FFT
        freq = 0 # Placeholder

        # Time axis
        t = np.linspace(0, self.max_samples * time_interval_ns.value * 1e-9, self.max_samples)

        return t, data_mV, freq


class WaveformApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PicoScope Spin Frequency Analyzer")

        self.scope = None
        self.is_running = False
        self.log_file = None

        self.setup_ui()

        # Initialize scope
        if PICOSDK_AVAILABLE:
            # We need ctypes for real scope
            global ctypes
            import ctypes
            try:
                self.scope = RealPicoScope()
                if not self.scope.connect():
                    print("Could not connect to real scope, falling back to mock.")
                    self.scope = MockScope()
                    self.scope.connect()
            except Exception as e:
                print(f"Error initializing real scope: {e}")
                self.scope = MockScope()
                self.scope.connect()
        else:
            self.scope = MockScope()
            self.scope.connect()

    def setup_ui(self):
        # Control Panel
        control_frame = ttk.LabelFrame(self.root, text="Controls")
        control_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        self.start_btn = ttk.Button(control_frame, text="Start", command=self.start_capture)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(control_frame, text="Stop", command=self.stop_capture, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        # Timebase Control
        ttk.Label(control_frame, text="Timebase:").pack(side=tk.LEFT, padx=(10, 2))
        self.timebase_var = tk.IntVar(value=8)
        self.timebase_spin = ttk.Spinbox(control_frame, from_=0, to=20, textvariable=self.timebase_var, width=3, command=self.update_scope_settings)
        self.timebase_spin.bind('<Return>', lambda e: self.update_scope_settings())
        self.timebase_spin.pack(side=tk.LEFT, padx=2)

        # Range Control
        ttk.Label(control_frame, text="Range:").pack(side=tk.LEFT, padx=(10, 2))
        self.range_map = {"50mV": 1, "100mV": 2, "200mV": 3, "500mV": 4, "1V": 5, "2V": 6, "5V": 7, "10V": 8, "20V": 9}
        self.range_var = tk.StringVar(value="5V")
        self.range_combo = ttk.Combobox(control_frame, textvariable=self.range_var, values=list(self.range_map.keys()), width=7, state="readonly")
        self.range_combo.bind("<<ComboboxSelected>>", lambda e: self.update_scope_settings())
        self.range_combo.pack(side=tk.LEFT, padx=2)

        # Frequency Display
        self.freq_var = tk.StringVar(value="Frequency: --- Hz")
        freq_label = ttk.Label(control_frame, textvariable=self.freq_var, font=("Arial", 14, "bold"))
        freq_label.pack(side=tk.LEFT, padx=20)


        # Logger Status
        self.log_var = tk.StringVar(value="Logging: Stopped")
        log_label = ttk.Label(control_frame, textvariable=self.log_var)
        log_label.pack(side=tk.RIGHT, padx=10)

        # Plot Area
        self.fig, self.ax = plt.subplots(figsize=(8, 4))
        self.ax.set_title("Live Scope View")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Amplitude")
        self.line, = self.ax.plot([], [], lw=2)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def start_capture(self):
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        # Setup logging
        filename = f"spin_freq_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.log_file = open(filename, 'w', newline='')
        self.csv_writer = csv.writer(self.log_file)
        self.csv_writer.writerow(["Timestamp", "Frequency_Hz"])
        self.log_var.set(f"Logging to: {filename}")

        # Start thread
        self.thread = threading.Thread(target=self. update_loop)
        self.thread.daemon = True
        self.thread.start()

    def stop_capture(self):
        self.is_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.log_var.set("Logging: Stopped")

        if self.log_file:
            self.log_file.close()
            self.log_file = None

    def update_scope_settings(self):
        try:
            # Update Timebase
            try:
                tb_val = int(self.timebase_var.get())
                if self.scope:
                  self.scope.set_timebase(tb_val)
            except ValueError:
                pass

            # Update Range
            range_key = self.range_var.get()
            if range_key in self.range_map and self.scope:
                range_idx = self.range_map[range_key]
                self.scope.set_range(range_idx)
        except Exception as e:
            print(f"Error updating settings: {e}")

    def update_loop(self):
        while self.is_running:
            try:
                t, y, freq = self.scope.get_data()

                # If Real Scope, calculate freq here using FFT
                if isinstance(self.scope, RealPicoScope):
                    # Remove DC offset
                    y = y - np.mean(y)
                    # FFT
                    fft_vals = np.fft.rfft(y)
                    fft_freq = np.fft.rfftfreq(len(y), d=(t[1]-t[0]))
                    peak_idx = np.argmax(np.abs(fft_vals))
                    freq = fft_freq[peak_idx]

                # Update UI (thread safe call)
                self.root.after(0, self.update_plot, t, y, freq)

                # Log data
                if self.log_file:
                    self.csv_writer.writerow([datetime.now().isoformat(), freq])

                time.sleep(0.1) # Update rate
            except Exception as e:
                print(f"Error in loop: {e}")
                self.is_running = False

    def update_plot(self, t, y, freq):
        self.line.set_data(t, y)
        self.ax.set_xlim(t[0], t[-1])
        min_y, max_y = np.min(y), np.max(y)
        range_y = max_y - min_y if max_y != min_y else 1.0
        self.ax.set_ylim(min_y - 0.1*range_y, max_y + 0.1*range_y)
        self.canvas.draw()
        self.freq_var.set(f"Frequency: {freq:.2f} Hz")

    def on_close(self):
        self.stop_capture()
        if self.scope:
            self.scope.disconnect()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = WaveformApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
