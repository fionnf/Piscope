import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import matplotlib
matplotlib.use('TkAgg') # Explicitly set backend for RPi
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np
import threading
import time
import csv
from datetime import datetime
import os
import ctypes
import signal
import sys

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
    def set_trigger(self, threshold_mv, direction): raise NotImplementedError

class MockScope(ScopeInterface):
    def __init__(self):
        self.connected = False
        self.timebase = 1
        self.range_idx = 6
        self.trigger_threshold = 0
        self.trigger_direction = 0

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

    def set_trigger(self, threshold_mv, direction):
        self.trigger_threshold = threshold_mv
        self.trigger_direction = direction
        print(f"Mock Scope Trigger: {threshold_mv} mV, Dir: {direction}")

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
        self.trigger_mv = 0
        self.trigger_direction = 0 # 0=Rising, 1=Falling

    def connect(self):
        if not PICOSDK_AVAILABLE:
            return False

        print("RealPicoScope: Calling ps2000_open_unit...", flush=True)
        try:
            # Open the unit
            self.status["openunit"] = ps2000.ps2000_open_unit()
            print(f"RealPicoScope: ps2000_open_unit returned {self.status['openunit']}", flush=True)
        except Exception as e:
            print(f"RealPicoScope: Exception during open_unit: {e}", flush=True)
            return False

        self.chandle = ctypes.c_int16(self.status["openunit"])

        if self.chandle.value > 0:
            print(f"PicoScope Connected. Handle: {self.chandle.value}", flush=True)
            return True
        else:
            print("Failed to open PicoScope (Handle <= 0)", flush=True)
            return False

    def disconnect(self):
        if self.chandle.value > 0:
            ps2000.ps2000_close_unit(self.chandle)
            print("PicoScope Closed")

    def set_timebase(self, index):
        self.timebase = index

    def set_range(self, index):
        self.range_idx = index

    def set_trigger(self, threshold_mv, direction):
        self.trigger_mv = threshold_mv
        self.trigger_direction = direction

    def get_data(self):
        # Setup channel A
        # Range: self.range_idx, AC coupled (0)
        ps2000.ps2000_set_channel(self.chandle, 0, 1, 0, self.range_idx)

        # Setup Trigger
        # Direction: 0=Rising, 1=Falling
        # Threshold needs to be converted to ADC counts
        # maxADC = 32767. voltage range depends on range_idx.
        # Simple approximation: threshold_adc = (threshold_mv / range_mv) * 32767
        range_mv = 20000 # default fallback

        # Get range in mV from index (hardcoded mapping matching the UI)
        # 1: 50, 2: 100, 3: 200, 4: 500, 5: 1000, 6: 2000, 7: 5000, 8: 10000, 9: 20000
        ranges_mv = {1: 50, 2: 100, 3: 200, 4: 500, 5: 1000, 6: 2000, 7: 5000, 8: 10000, 9: 20000}
        if self.range_idx in ranges_mv:
            range_mv = ranges_mv[self.range_idx]

        threshold_adc = int((self.trigger_mv / range_mv) * 32767)
        # Clip to valid range
        threshold_adc = max(-32767, min(32767, threshold_adc))

        # ps2000_set_trigger(handle, source, threshold, direction, delay, auto_trigger_ms)
        # source=0 (ChanA), delay=0, auto_trigger_ms=1000 (wait 1s then auto trigger if no event)
        # Increased to 1000ms to support lower frequencies (down to ~1Hz)
        ps2000.ps2000_set_trigger(self.chandle, 0, threshold_adc, self.trigger_direction, 0, ctypes.c_int16(1000))

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
        self.root.geometry("1000x700")

        self.scope = None
        self.is_running = False
        self.log_file = None

        self.setup_ui()

        # Start scope initialization in a background thread
        self.log_var.set("Status: Connecting to Scope...")
        self.start_btn.config(state=tk.DISABLED)
        threading.Thread(target=self.async_init_scope, daemon=True).start()

    def async_init_scope(self):
        print("Attempting to connect to scope in background...", flush=True)
        scope_to_use = None

        if PICOSDK_AVAILABLE:
            try:
                real_scope = RealPicoScope()
                if real_scope.connect():
                    print("Connected to RealPicoScope.", flush=True)
                    scope_to_use = real_scope
                else:
                    print("Could not connect to real scope, falling back to mock.", flush=True)
            except Exception as e:
                print(f"Error initializing real scope: {e}", flush=True)

        if scope_to_use is None:
            print("Using Mock Scope.", flush=True)
            scope_to_use = MockScope()
            scope_to_use.connect()

        self.scope = scope_to_use

        # Update UI in main thread
        def on_connected():
            if not self.root: return
            self.log_var.set("Status: Ready")
            self.start_btn.config(state=tk.NORMAL)
            print("Scope initialized and ready.", flush=True)

        try:
            self.root.after(0, on_connected)
        except:
            pass


    def setup_ui(self):
        print("Setting up UI...", flush=True)
        # Control Panel
        control_frame = ttk.LabelFrame(self.root, text="Controls")
        control_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        # --- Channel / Scope Controls ---
        scope_frame = ttk.Frame(control_frame)
        scope_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

        self.start_btn = ttk.Button(scope_frame, text="Start Scope", command=self.start_scope)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(scope_frame, text="Stop Scope", command=self.stop_scope, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        # Timebase Control
        ttk.Label(scope_frame, text="Timebase:").pack(side=tk.LEFT, padx=(10, 2))
        self.timebase_var = tk.IntVar(value=8)
        self.timebase_spin = ttk.Spinbox(scope_frame, from_=0, to=20, textvariable=self.timebase_var, width=3, command=self.update_scope_settings)
        self.timebase_spin.bind('<Return>', lambda e: self.update_scope_settings())
        self.timebase_spin.pack(side=tk.LEFT, padx=2)

        # Range Control
        ttk.Label(scope_frame, text="Range:").pack(side=tk.LEFT, padx=(10, 2))
        self.range_map = {"50mV": 1, "100mV": 2, "200mV": 3, "500mV": 4, "1V": 5, "2V": 6, "5V": 7, "10V": 8, "20V": 9}
        self.range_var = tk.StringVar(value="5V")
        self.range_combo = ttk.Combobox(scope_frame, textvariable=self.range_var, values=list(self.range_map.keys()), width=7, state="readonly")
        self.range_combo.bind("<<ComboboxSelected>>", lambda e: self.update_scope_settings())
        self.range_combo.pack(side=tk.LEFT, padx=2)

        # --- Trigger Controls ---
        trigger_frame = ttk.LabelFrame(control_frame, text="Trigger")
        trigger_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

        ttk.Label(trigger_frame, text="Thresh(mV):").pack(side=tk.LEFT, padx=2)
        self.trig_thresh_var = tk.IntVar(value=0)
        trig_thresh_spin = ttk.Spinbox(trigger_frame, from_=-20000, to=20000, textvariable=self.trig_thresh_var, width=6, command=self.update_scope_settings)
        trig_thresh_spin.bind('<Return>', lambda e: self.update_scope_settings())
        trig_thresh_spin.pack(side=tk.LEFT, padx=2)

        ttk.Label(trigger_frame, text="Dir:").pack(side=tk.LEFT, padx=2)
        self.trig_dir_var = tk.StringVar(value="Rising")
        trig_dir_combo = ttk.Combobox(trigger_frame, textvariable=self.trig_dir_var, values=["Rising", "Falling"], width=7, state="readonly")
        trig_dir_combo.bind("<<ComboboxSelected>>", lambda e: self.update_scope_settings())
        trig_dir_combo.pack(side=tk.LEFT, padx=2)

        # --- File Settings ---
        file_frame = ttk.Frame(control_frame)
        file_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

        ttk.Label(file_frame, text="File Prefix:").pack(side=tk.LEFT, padx=2)
        self.filename_prefix = tk.StringVar(value="spin_log")
        ttk.Entry(file_frame, textvariable=self.filename_prefix, width=15).pack(side=tk.LEFT, padx=2)

        ttk.Button(file_frame, text="Set Folder...", command=self.choose_directory).pack(side=tk.LEFT, padx=5)
        self.save_dir_var = tk.StringVar(value=os.getcwd())
        # Display folder path (truncated if too long in UI)
        self.lbl_save_dir = ttk.Label(file_frame, textvariable=self.save_dir_var, font=("Arial", 8), foreground="gray")
        self.lbl_save_dir.pack(side=tk.LEFT, padx=2)

        # --- Frequency & Logging Controls ---
        freq_log_frame = ttk.Frame(control_frame)
        freq_log_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

        # Frequency settings
        ttk.Label(freq_log_frame, text="Freq Range(Hz):").pack(side=tk.LEFT, padx=2)
        self.freq_min_var = tk.IntVar(value=0)
        self.freq_max_var = tk.IntVar(value=1000)
        ttk.Spinbox(freq_log_frame, from_=0, to=10000, textvariable=self.freq_min_var, width=5).pack(side=tk.LEFT, padx=1)
        ttk.Label(freq_log_frame, text="-").pack(side=tk.LEFT)
        ttk.Spinbox(freq_log_frame, from_=0, to=10000, textvariable=self.freq_max_var, width=5).pack(side=tk.LEFT, padx=1)

        # Logging Buttons
        self.log_start_btn = ttk.Button(freq_log_frame, text="Start Log", command=self.start_logging)
        self.log_start_btn.pack(side=tk.LEFT, padx=(20, 5))

        self.log_stop_btn = ttk.Button(freq_log_frame, text="Stop Log", command=self.stop_logging, state=tk.DISABLED)
        self.log_stop_btn.pack(side=tk.LEFT, padx=5)

        # Logger Status
        self.log_var = tk.StringVar(value="Logging: Stopped")
        log_label = ttk.Label(freq_log_frame, textvariable=self.log_var)
        log_label.pack(side=tk.LEFT, padx=10)

        # Frequency Display (Main)
        self.freq_var = tk.StringVar(value="Frequency: --- Hz")
        freq_label = ttk.Label(control_frame, textvariable=self.freq_var, font=("Arial", 14, "bold"))
        freq_label.pack(side=tk.TOP, pady=5)

        # Plot Area
        self.fig, self.ax = plt.subplots(figsize=(8, 4))
        self.ax.set_title("Live Scope View")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Amplitude")
        self.line, = self.ax.plot([], [], lw=2)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        print("UI Setup complete.", flush=True)

    def start_scope(self):
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        # Start thread
        self.thread = threading.Thread(target=self. update_loop)
        self.thread.daemon = True
        self.thread.start()

    def stop_scope(self):
        self.is_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def start_logging(self):
        if not self.is_running:
             messagebox.showwarning("Warning", "Start Scope first!")
             return

        # Get base name and directory
        prefix = self.filename_prefix.get().strip()
        if not prefix: prefix = "spin_log"

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{prefix}_{timestamp}.csv"

        directory = self.save_dir_var.get()
        if not os.path.isdir(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Invalid directory: {e}")
                return

        full_path = os.path.join(directory, filename)

        try:
            self.log_file = open(full_path, 'w', newline='')
            self.csv_writer = csv.writer(self.log_file)
            self.csv_writer.writerow(["Timestamp", "Frequency_Hz"])

            # Show just filename in status to save space, or maybe full path in tooltip?
            self.log_var.set(f"Logging: {filename}")
            self.log_start_btn.config(state=tk.DISABLED)
            self.log_stop_btn.config(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open log file: {e}")

    def choose_directory(self):
        folder = filedialog.askdirectory(initialdir=self.save_dir_var.get())
        if folder:
            self.save_dir_var.set(folder)

    def stop_logging(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.log_var.set("Status: Ready (Logging Stopped)")
        self.log_start_btn.config(state=tk.NORMAL)
        self.log_stop_btn.config(state=tk.DISABLED)

    # Replaced start_capture and stop_capture with above methods

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

            # Update Trigger
            try:
                trig_thresh = int(self.trig_thresh_var.get())
                trig_dir_str = self.trig_dir_var.get()
                trig_dir = 0 if trig_dir_str == "Rising" else 1
                if self.scope:
                    self.scope.set_trigger(trig_thresh, trig_dir)
            except ValueError:
                pass

        except Exception as e:
            print(f"Error updating settings: {e}")

    def update_loop(self):
        while self.is_running:
            try:
                t, y, freq = self.scope.get_data()

                # Calculate freq here using FFT with range limits
                # Remove DC offset
                y_ac = y - np.mean(y)

                # FFT
                fft_vals = np.fft.rfft(y_ac)
                fft_freqs = np.fft.rfftfreq(len(y_ac), d=(t[1]-t[0]))
                fft_mags = np.abs(fft_vals)

                # Filter by frequency range
                try:
                    min_f = self.freq_min_var.get()
                    max_f = self.freq_max_var.get()
                except:
                    min_f, max_f = 0, 10000

                # Create mask
                mask = (fft_freqs >= min_f) & (fft_freqs <= max_f)

                if np.any(mask):
                    # Zero out out-of-range frequencies
                    masked_mags = fft_mags.copy()
                    masked_mags[~mask] = 0

                    peak_idx = np.argmax(masked_mags)

                    # Optional: Threshold check (e.g., must be > X signal strength)
                    # For now just take max in range
                    freq = fft_freqs[peak_idx]
                else:
                    freq = 0

                # Update UI (thread safe call)
                try:
                    self.root.after(0, self.update_plot, t, y, freq)
                except Exception:
                    pass # Window likely destroyed, ignore


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
        self.stop_logging()
        self.stop_scope()
        if self.scope:
            self.scope.disconnect()
        self.root.destroy()
        sys.exit(0) # Force exit

if __name__ == "__main__":
    print("Creating Tkinter root...", flush=True)
    root = tk.Tk()

    app = WaveformApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # Fix for macOS window visibility - Defer to after mainloop starts
    def focus_window():
        root.update_idletasks() # Ensure geometry handles pending resize events
        root.deiconify()
        root.lift()
        root.focus_force()
    root.after(100, focus_window)

    # Allow Ctrl+C to interrupt the mainloop
    signal.signal(signal.SIGINT, lambda sig, frame: app.on_close())
    # Periodically wake up the loop to process signals
    def check_signals():
        root.after(200, check_signals)
    root.after(200, check_signals)

    print("Entering mainloop...", flush=True)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_close()
