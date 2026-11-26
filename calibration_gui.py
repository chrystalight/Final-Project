"""Calibration logger for the OPT101 optical cells.

Pairs with calibration_logger/calibration_logger.ino. Enter the ADS1115
channel and the volumes of water and dye in the sample, press Record, and the
Arduino streams that channel's voltage for the trial duration, plotted live
as it arrives. Each trial is
saved to its own CSV (channel and volumes in the filename) and appended to
opt101_calibration_summary.csv with the dye fraction dye / (water + dye), and
the GUI shows the trial's mean and standard deviation.

Requires: pip install pyserial
"""

import csv
import math
import queue
import re
import statistics
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import serial
import serial.tools.list_ports

BAUD = 115200
CHANNELS = {"A0": 0, "A1": 1}  # label -> ADS1115 input
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "calibration_data"
SUMMARY_NAME = "opt101_calibration_summary.csv"
SUMMARY_FIELDS = [
    "recorded_at", "channel", "water_mL", "dye_mL", "dye_fraction",
    "mean_V", "std_V", "n_samples", "sample_rate_Hz", "file",
]


def filename_part(text):
    """Make user text safe for a filename."""
    return re.sub(r"[^A-Za-z0-9.\-]", "", text.strip())


def parse_volume(text, name, allow_zero):
    """Parse a volume in mL, raising ValueError with a readable message."""
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"Enter the {name} volume as a number of mL.") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"The {name} volume must be {'0 or more' if allow_zero else 'more than 0'} mL.")
    return value


def nice_ticks(lo, hi, target=5):
    """Round tick values covering lo..hi, and the decimals needed to label them."""
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    decimals = max(0, -math.floor(math.log10(step)))
    first = math.ceil(lo / step) * step
    count = int((hi - first) / step + 1e-9) + 1
    return [first + i * step for i in range(count)], decimals


class LivePlot(ttk.Frame):
    """Voltage-vs-time plot drawn on a Tk canvas, redrawn as samples arrive."""

    LEFT, RIGHT, TOP, BOTTOM = 70, 14, 12, 38
    TRACE = "#1f5fbf"
    MEAN = "#d0661c"
    MIN_SPAN_V = 0.002  # never zoom in tighter than 2 mV

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=700, height=260, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.times, self.volts = [], []
        self.duration = 10.0
        self.mean = None

    def reset(self, duration):
        self.times, self.volts, self.duration, self.mean = [], [], duration, None
        self.draw()

    def set_data(self, times, volts, mean=None):
        self.times, self.volts, self.mean = times, volts, mean
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        x0, x1 = self.LEFT, w - self.RIGHT
        y0, y1 = self.TOP, h - self.BOTTOM  # y0 is the top edge
        c.create_rectangle(x0, y0, x1, y1, outline="#999")

        def xmap(t):
            return x0 + min(max(t / self.duration, 0.0), 1.0) * (x1 - x0)

        ticks, decimals = nice_ticks(0, self.duration, 6)
        for t in ticks:
            x = xmap(t)
            c.create_line(x, y1, x, y1 + 4, fill="#666")
            c.create_text(x, y1 + 6, text=f"{t:.{decimals}f}", anchor="n", fill="#333")
        c.create_text((x0 + x1) / 2, h - 2, text="Time (s)", anchor="s", fill="#333")

        if not self.volts:
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="Press Record to start a trial", fill="#888")
            return

        lo, hi = min(self.volts), max(self.volts)
        if hi - lo < self.MIN_SPAN_V:
            mid = (hi + lo) / 2
            lo, hi = mid - self.MIN_SPAN_V / 2, mid + self.MIN_SPAN_V / 2
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad

        def ymap(v):
            return y1 - (v - lo) / (hi - lo) * (y1 - y0)

        ticks, decimals = nice_ticks(lo, hi, 5)
        for v in ticks:
            y = ymap(v)
            c.create_line(x0, y, x1, y, fill="#eee")
            c.create_text(x0 - 6, y, text=f"{v:.{decimals}f}", anchor="e", fill="#333")
        c.create_text(12, (y0 + y1) / 2, text="Voltage (V)", angle=90, fill="#333")

        # One min/max pair per pixel column, so spikes stay visible however many samples there are.
        points = []
        col = col_lo = col_hi = None
        for t, v in zip(self.times, self.volts):
            px = int(xmap(t))
            if px != col:
                if col is not None:
                    points += [col, ymap(col_lo), col, ymap(col_hi)]
                col, col_lo, col_hi = px, v, v
            else:
                col_lo, col_hi = min(col_lo, v), max(col_hi, v)
        points += [col, ymap(col_lo), col, ymap(col_hi)]
        c.create_line(*points, fill=self.TRACE)

        if self.mean is not None:
            y = ymap(self.mean)
            c.create_line(x0, y, x1, y, fill=self.MEAN, dash=(5, 3), width=2)
            c.create_text(x1 - 4, y - 3, text=f"mean {self.mean:.5f} V", anchor="se", fill=self.MEAN)


class CalibrationApp:
    def __init__(self, root):
        self.root = root
        root.title("Optical Cell Calibration")
        self.ser = None
        self.events = queue.Queue()  # (kind, payload) from worker threads
        self.trial = None            # settings of the trial being recorded
        self.record_start = None
        self.live_times, self.live_volts = [], []
        self.plot_tick = 0
        self._build_ui()
        self.refresh_ports()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.poll_events)

    # ---------- UI ----------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        conn = ttk.LabelFrame(self.root, text="Arduino")
        conn.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Label(conn, text="Port").grid(row=0, column=0, **pad)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, width=28, state="readonly")
        self.port_box.grid(row=0, column=1, **pad)
        self.refresh_btn = ttk.Button(conn, text="Refresh", command=self.refresh_ports)
        self.refresh_btn.grid(row=0, column=2, **pad)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connection)
        self.connect_btn.grid(row=0, column=3, **pad)
        self.conn_status = tk.StringVar(value="Not connected")
        ttk.Label(conn, textvariable=self.conn_status).grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        trial = ttk.LabelFrame(self.root, text="Trial")
        trial.grid(row=1, column=0, sticky="ew", **pad)

        ttk.Label(trial, text="Channel").grid(row=0, column=0, sticky="w", **pad)
        self.channel_var = tk.StringVar(value="A0")
        ttk.Combobox(trial, textvariable=self.channel_var, values=list(CHANNELS),
                     width=8, state="readonly").grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(trial, text="Water (mL)").grid(row=1, column=0, sticky="w", **pad)
        self.water_var = tk.StringVar()
        ttk.Entry(trial, textvariable=self.water_var, width=12).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(trial, text="Dye (mL)").grid(row=1, column=2, sticky="w", **pad)
        self.dye_var = tk.StringVar(value="0")
        ttk.Entry(trial, textvariable=self.dye_var, width=10).grid(row=1, column=3, sticky="w", **pad)
        self.fraction_var = tk.StringVar()
        ttk.Label(trial, textvariable=self.fraction_var).grid(row=1, column=4, sticky="w", **pad)
        self.water_var.trace_add("write", self.update_fraction)
        self.dye_var.trace_add("write", self.update_fraction)

        ttk.Label(trial, text="Duration (s)").grid(row=2, column=0, sticky="w", **pad)
        self.duration_var = tk.StringVar(value="10")
        ttk.Spinbox(trial, textvariable=self.duration_var, from_=1, to=60, increment=1,
                    width=8).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(trial, text="Save to").grid(row=3, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(value=str(DEFAULT_DATA_DIR))
        ttk.Entry(trial, textvariable=self.dir_var, width=40).grid(row=3, column=1, columnspan=3, sticky="ew", **pad)
        ttk.Button(trial, text="Browse", command=self.choose_dir).grid(row=3, column=4, **pad)

        self.record_btn = ttk.Button(trial, text="Record", command=self.start_recording, state="disabled")
        self.record_btn.grid(row=4, column=0, **pad)
        self.progress_var = tk.StringVar()
        ttk.Label(trial, textvariable=self.progress_var).grid(row=4, column=1, columnspan=4, sticky="w", **pad)

        live = ttk.LabelFrame(self.root, text="Live trace")
        live.grid(row=2, column=0, sticky="nsew", **pad)
        self.plot = LivePlot(live)
        self.plot.pack(fill="both", expand=True, padx=4, pady=4)

        result = ttk.LabelFrame(self.root, text="Last trial")
        result.grid(row=3, column=0, sticky="ew", **pad)
        big = ("TkDefaultFont", 16, "bold")
        self.mean_var = tk.StringVar(value="Mean: -")
        self.std_var = tk.StringVar(value="Std: -")
        self.info_var = tk.StringVar()
        ttk.Label(result, textvariable=self.mean_var, font=big).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(result, textvariable=self.std_var, font=big).grid(row=0, column=1, sticky="w", padx=24)
        ttk.Label(result, textvariable=self.info_var).grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        history = ttk.LabelFrame(self.root, text="This session")
        history.grid(row=4, column=0, sticky="nsew", **pad)
        cols = ("time", "channel", "water", "dye", "fraction", "mean", "std", "n")
        headings = ("Time", "Channel", "Water (mL)", "Dye (mL)", "Dye fraction",
                    "Mean (V)", "Std (mV)", "N")
        self.table = ttk.Treeview(history, columns=cols, show="headings", height=5)
        for col, heading in zip(cols, headings):
            self.table.heading(col, text=heading)
            self.table.column(col, width=95, anchor="center")
        self.table.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(2, weight=3)
        self.root.rowconfigure(4, weight=1)
        self.root.columnconfigure(0, weight=1)
        history.rowconfigure(0, weight=1)
        history.columnconfigure(0, weight=1)

    def update_fraction(self, *_):
        """Show the dye fraction for the volumes currently entered."""
        try:
            water = parse_volume(self.water_var.get(), "water", allow_zero=False)
            dye = parse_volume(self.dye_var.get(), "dye", allow_zero=True)
            self.fraction_var.set(f"dye fraction = {dye / (water + dye):.5f}")
        except ValueError:
            self.fraction_var.set("")

    def choose_dir(self):
        folder = filedialog.askdirectory(initialdir=self.dir_var.get())
        if folder:
            self.dir_var.set(folder)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_box["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    # ---------- connection ----------

    def toggle_connection(self):
        if self.ser:
            self.ser.close()
            self.ser = None
            self.conn_status.set("Not connected")
            self.connect_btn.config(text="Connect")
            self.record_btn.config(state="disabled")
            return
        port = self.port_var.get()
        if not port:
            messagebox.showerror("No port", "Select the Arduino's serial port.")
            return
        self.connect_btn.config(state="disabled")
        self.conn_status.set(f"Connecting to {port} (the board resets, ~2 s)...")
        threading.Thread(target=self._connect_worker, args=(port,), daemon=True).start()

    def _connect_worker(self, port):
        ser = None
        try:
            ser = serial.Serial(port, BAUD, timeout=0.5)
            opened = time.monotonic()
            pinged = False
            while time.monotonic() - opened < 8:
                line = ser.readline().decode(errors="replace").strip()
                if line == "READY":
                    self.events.put(("connected", ser))
                    return
                if line.startswith("ERR"):
                    raise RuntimeError(line[4:])
                # If the board didn't reset on open, ask it directly.
                if not line and not pinged and time.monotonic() - opened > 3:
                    ser.write(b"PING\n")
                    pinged = True
            raise TimeoutError("No response. Is calibration_logger.ino uploaded, "
                               "and the Serial Monitor/Plotter closed?")
        except Exception as exc:
            if ser:
                ser.close()
            self.events.put(("connect_error", str(exc)))

    # ---------- recording ----------

    def start_recording(self):
        water_text = self.water_var.get().strip()
        dye_text = self.dye_var.get().strip()
        try:
            water = parse_volume(water_text, "water", allow_zero=False)
            dye = parse_volume(dye_text, "dye", allow_zero=True)
        except ValueError as exc:
            messagebox.showerror("Volumes", str(exc))
            return
        try:
            duration = float(self.duration_var.get())
            if not 1 <= duration <= 60:
                raise ValueError
        except ValueError:
            messagebox.showerror("Duration", "Duration must be a number of seconds from 1 to 60.")
            return

        channel = self.channel_var.get()
        self.trial = {
            "channel": channel,
            "water_text": water_text,
            "dye_text": dye_text,
            "water": water,
            "dye": dye,
            "fraction": dye / (water + dye),
            "duration": duration,
            "data_dir": Path(self.dir_var.get()),
        }
        self.record_btn.config(state="disabled")
        self.connect_btn.config(state="disabled")
        # The worker appends samples to these lists; poll_events plots them as they grow.
        self.live_times, self.live_volts = [], []
        self.plot.reset(duration)
        self.record_start = time.monotonic()
        threading.Thread(target=self._record_worker,
                         args=(CHANNELS[channel], duration, self.live_times, self.live_volts),
                         daemon=True).start()

    def _record_worker(self, channel, duration, times, volts):
        try:
            self.ser.reset_input_buffer()
            self.ser.write(f"R {channel} {int(duration * 1000)}\n".encode())
            started = False
            expected = None
            deadline = time.monotonic() + duration + 5
            while expected is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("Arduino stopped responding during the trial.")
                line = self.ser.readline().decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("ERR"):
                    raise RuntimeError(f"Arduino: {line[4:]}")
                if line == "START":
                    started = True
                elif line.startswith("END"):
                    expected = int(line.split()[1])
                elif started:
                    t_us, v = line.split(",")
                    t_s, v_V = int(t_us) / 1e6, float(v)
                    times.append(t_s)   # times first, so len(volts) never exceeds len(times)
                    volts.append(v_V)
            if len(volts) != expected:
                raise RuntimeError(f"Received {len(volts)} of {expected} samples; "
                                   "the trial was not saved. Try again.")
            self.events.put(("trial_done", (times, volts)))
        except Exception as exc:
            self.events.put(("trial_error", str(exc)))

    def finish_trial(self, times, volts):
        t = self.trial
        n = len(volts)
        mean = statistics.fmean(volts)
        std = statistics.stdev(volts) if n > 1 else 0.0
        rate = (n - 1) / (times[-1] - times[0]) if n > 1 and times[-1] > times[0] else 0.0

        stamp = datetime.now()
        name = (f"{t['channel']}_{filename_part(t['water_text'])}mLwater_"
                f"{filename_part(t['dye_text'])}mLdye_{stamp:%Y%m%d-%H%M%S}.csv")
        t["data_dir"].mkdir(parents=True, exist_ok=True)
        path = t["data_dir"] / name
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "voltage_V"])
            writer.writerows((f"{ti:.6f}", f"{v:.5f}") for ti, v in zip(times, volts))

        summary = t["data_dir"] / SUMMARY_NAME
        new_summary = not summary.exists()
        with open(summary, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            if new_summary:
                writer.writeheader()
            writer.writerow({
                "recorded_at": stamp.isoformat(timespec="seconds"),
                "channel": t["channel"],
                "water_mL": t["water"],
                "dye_mL": t["dye"],
                "dye_fraction": f"{t['fraction']:.6f}",
                "mean_V": f"{mean:.6f}",
                "std_V": f"{std:.6f}",
                "n_samples": n,
                "sample_rate_Hz": f"{rate:.1f}",
                "file": name,
            })

        self.plot.set_data(times, volts, mean)
        self.mean_var.set(f"Mean: {mean:.5f} V")
        self.std_var.set(f"Std: {std * 1000:.3f} mV")
        self.info_var.set(f"{t['channel']}, {t['water_text']} mL water + {t['dye_text']} mL dye "
                          f"(fraction {t['fraction']:.5f}): {n} samples at {rate:.0f} Hz, saved {name}")
        self.table.insert("", 0, values=(
            f"{stamp:%H:%M:%S}", t["channel"], t["water_text"], t["dye_text"],
            f"{t['fraction']:.5f}", f"{mean:.5f}", f"{std * 1000:.3f}", n,
        ))

    def end_trial(self):
        self.trial = None
        self.record_start = None
        self.record_btn.config(state="normal")
        self.connect_btn.config(state="normal")

    # ---------- event loop ----------

    def poll_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                self.ser = payload
                self.conn_status.set(f"Connected to {self.ser.port}")
                self.connect_btn.config(text="Disconnect", state="normal")
                self.record_btn.config(state="normal")
            elif kind == "connect_error":
                self.conn_status.set("Not connected")
                self.connect_btn.config(state="normal")
                messagebox.showerror("Connection failed", payload)
            elif kind == "trial_done":
                try:
                    self.finish_trial(*payload)
                    self.progress_var.set("Done")
                except OSError as exc:
                    self.progress_var.set("Save failed")
                    messagebox.showerror("Save failed", str(exc))
                self.end_trial()
            elif kind == "trial_error":
                self.progress_var.set("Trial failed")
                self.end_trial()
                messagebox.showerror("Trial failed", payload)

        if self.record_start is not None:
            elapsed = min(time.monotonic() - self.record_start, self.trial["duration"])
            self.progress_var.set(f"Recording... {elapsed:.1f} / {self.trial['duration']:g} s")
            self.plot_tick = (self.plot_tick + 1) % 2  # redraw every other poll, ~10 Hz
            if self.plot_tick == 0:
                n = len(self.live_volts)
                self.plot.set_data(self.live_times[:n], self.live_volts[:n])

        self.root.after(50, self.poll_events)

    def on_close(self):
        if self.ser:
            self.ser.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    CalibrationApp(root)
    root.mainloop()
