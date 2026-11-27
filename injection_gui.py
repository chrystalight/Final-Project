"""Injection test: record a dye injection on one optical cell and work out how
much dye went past it, showing every step of the processing.

Pairs with calibration_logger/calibration_logger.ino, the same firmware as the
calibration GUI. Choose the channel, enter the dye volume injected and the
flow through that outlet, press Record, and inject when prompted (after the
baseline). Four plots follow the processing in order:

  1. raw voltage from the ADC                                  (live)
  2. filtered voltage: running median, then running mean      (live)
  3. absorbance log10(I0 / V), with I0 from the baseline       (live)
  4. dye fraction from the channel's calibration curve         (after recording)

After the recording the dye pulse is integrated (see injection_analysis.py)
and the dye volume, recovery and implied flow are shown with ±2σ
uncertainties.

Each recording is saved to injection_data/ as a raw trace CSV plus a
_processed.csv of the signals behind the result, and every analysis is
appended to injection_log.csv in the same folder. Saved traces can be
analysed again with different inputs.

Requires: pip install pyserial
"""

import csv
import math
import queue
import statistics
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import serial
import serial.tools.list_ports

import injection_analysis as ia
from calibration_gui import BAUD, CHANNELS, PlotCanvas, draw_axes, filename_part

DATA_DIR = ia.HERE / "injection_data"
LOG_NAME = "injection_log.csv"
LOG_FIELDS = [
    "analysed_at", "trace", "channel", "injected_mL", "flow_mL_min", "flow_unc_pct", "baseline_s",
    "I0_V", "sigma_base_A", "pulse_start_s", "pulse_end_s", "peak_fraction", "integral_s",
    "sd_integral_s", "dye_mL", "sd_dye_mL", "recovery", "sd_recovery", "implied_flow_mL_min",
    "sd_implied_flow", "above_range", "calibration_files", "warnings",
]  # sd_ columns are 1σ
MAX_DURATION_S = 300    # the firmware's limit
INJECT_DELAY_S = 0.5    # prompt a little after the baseline window ends
NOMINAL_RATE_HZ = 155.5  # sample rate the firmware achieves, for sizing the live filters

RAW = "#7a7a7a"
FILTERED = "#1f5fbf"
ABSORB = "#7b3fa0"
FRACTION = "#2a7d4b"
BASE_SHADE = "#ececec"
PULSE_SHADE = "#f6d2b3"
LIMIT = "#c0392b"
NOTE = "#555"


def pixel_columns(ts, ys, xmap):
    """Group samples by canvas pixel column: [(px, min, max, mean)], so long traces draw fast
    and short spikes stay visible."""
    cols = []
    for t, y in zip(ts, ys):
        px = int(xmap(t))
        if cols and cols[-1][0] == px:
            c = cols[-1]
            c[1], c[2], c[3], c[4] = min(c[1], y), max(c[2], y), c[3] + y, c[4] + 1
        else:
            cols.append([px, y, y, y, 1])
    return [(px, lo, hi, s / n) for px, lo, hi, s, n in cols]


class TracePlot(PlotCanvas):
    """One processing stage against time. Shows any number of traces, shaded time spans
    (baseline, pulse), dashed reference lines and a filled area under part of a trace."""

    def __init__(self, parent, ylabel, empty_text, min_span):
        self.ylabel, self.empty_text, self.min_span = ylabel, empty_text, min_span
        self.xmax, self.traces, self.spans, self.lines, self.fill = 10.0, [], [], [], None
        super().__init__(parent, width=480, height=200)
        self.note_var = tk.StringVar()
        ttk.Label(self, textvariable=self.note_var, foreground=NOTE).pack(anchor="w", padx=4)

    def show(self, xmax, traces=(), spans=(), lines=(), fill=None, note="", empty_text=None):
        """traces: [(ts, ys, colour)]; spans: [(t0, t1, colour, label)];
        lines: [(y, colour, label)], drawn when inside the traces' range;
        fill: (ts, ys, colour), filled down to zero; note: a line of text under the plot."""
        self.xmax, self.traces, self.spans = max(xmax, 1e-6), list(traces), list(spans)
        self.lines, self.fill = list(lines), fill
        self.note_var.set(note)
        if empty_text is not None:
            self.empty_text = empty_text
        self.draw()

    def draw(self):
        if not self.ready():
            return
        values = [y for _ts, ys, _c in self.traces for y in ys]
        if not values:
            self.message(self.empty_text)
            return
        lo, hi = min(values), max(values)
        if hi - lo < self.min_span:
            mid = (hi + lo) / 2
            lo, hi = mid - self.min_span / 2, mid + self.min_span / 2
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        c = self.canvas
        xmap, ymap = draw_axes(c, 0, self.xmax, lo, hi, "Time (s)", self.ylabel,
                               margins=(70, 12, 10, 36))

        under = []  # shading drawn beneath the grid and traces
        for t0, t1, colour, label in self.spans:
            under.append(c.create_rectangle(xmap(t0), ymap(hi), xmap(min(t1, self.xmax)), ymap(lo),
                                            fill=colour, outline=""))
            if label:
                c.create_text(xmap(t0) + 3, ymap(hi) + 2, text=label, anchor="nw", fill="#777")
        if self.fill and self.fill[0]:
            ts, ys, colour = self.fill
            cols = pixel_columns(ts, ys, xmap)
            base = ymap(min(max(0.0, lo), hi))
            poly = [cols[0][0], base] + [v for px, _l, _h, mean in cols for v in (px, ymap(mean))]
            poly += [cols[-1][0], base]
            under.append(c.create_polygon(*poly, fill=colour, outline=""))
        for item in under:
            c.tag_lower(item)

        for y, colour, label in self.lines:
            if lo <= y <= hi:
                c.create_line(xmap(0), ymap(y), xmap(self.xmax), ymap(y), fill=colour, dash=(5, 3))
                if label:
                    c.create_text(xmap(self.xmax) - 4, ymap(y) - 2, text=label, anchor="se", fill=colour)
        for ts, ys, colour in self.traces:
            points = []
            for px, ylo, yhi, _mean in pixel_columns(ts, ys, xmap):
                points += [px, ymap(ylo), px, ymap(yhi)]
            if len(points) >= 4:
                c.create_line(*points, fill=colour)


class LiveProcessor:
    """The same filters and absorbance as injection_analysis.analyze, computed as samples
    arrive. Centred windows need samples on both sides, so the newest few values are
    provisional and are recomputed as more samples come in. The final result always
    comes from analyze() on the complete trace."""

    def __init__(self, baseline_s):
        self.baseline_s = baseline_s
        self.hm = round(ia.MEDIAN_S * NOMINAL_RATE_HZ / 2)      # median half-window
        self.hs = max(1, round(ia.SMOOTH_S * NOMINAL_RATE_HZ)) // 2  # mean half-window
        self.median, self.smooth, self.A = [], [], []
        self.med_final = self.smooth_final = 0
        self.i0 = None
        self.i0_final = False

    def update(self, times, volts):
        n = len(volts)
        for i in range(self.med_final, n):
            window = sorted(volts[max(0, i - self.hm):i + self.hm + 1])
            value = window[len(window) // 2]
            if i < len(self.median):
                self.median[i] = value
            else:
                self.median.append(value)
        self.med_final = max(0, n - self.hm)

        start = self.smooth_final
        for i in range(start, n):
            window = self.median[max(0, i - self.hs):i + self.hs + 1]
            value = sum(window) / len(window)
            if i < len(self.smooth):
                self.smooth[i] = value
            else:
                self.smooth.append(value)
        self.smooth_final = max(0, n - self.hm - self.hs)

        # I0: mean of the filtered baseline, provisional until the baseline is over.
        if not self.i0_final and n:
            base = [i for i in range(n) if times[i] - times[0] < self.baseline_s]
            done = times[-1] - times[0] >= self.baseline_s and base[-1] < self.smooth_final
            if base:
                self.i0 = statistics.fmean(self.smooth[i] for i in base)
                self.i0_final = done
                start = 0  # I0 changed: recompute every absorbance
        if self.i0 and self.i0 > 0:
            del self.A[start:]
            self.A += [math.log10(self.i0 / max(v, 1e-6)) for v in self.smooth[start:n]]


class InjectionApp:
    def __init__(self, root):
        self.root = root
        root.title("Injection Test")
        self.ser = None
        self.events = queue.Queue()  # (kind, payload) from worker threads
        self.recording = None        # settings of the recording in progress
        self.record_start = None
        self.live_times, self.live_volts = [], []
        self.live = None             # LiveProcessor during a recording
        self.plot_tick = 0
        self.cal_paths = ia.default_calibration_paths()
        self.cal = None
        self.trace = None            # (times, volts, path) of the trace on screen
        self._build_ui()
        self.refresh_ports()
        self.load_calibration()
        self.clear_plots(10)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.poll_events)

    # ---------- UI ----------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}

        top = ttk.Frame(self.root)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        conn = ttk.LabelFrame(top, text="Arduino")
        conn.grid(row=0, column=0, sticky="nsew", **pad)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, width=14, state="readonly")
        self.port_box.grid(row=0, column=0, **pad)
        ttk.Button(conn, text="Refresh", command=self.refresh_ports).grid(row=0, column=1, **pad)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connection)
        self.connect_btn.grid(row=0, column=2, **pad)
        self.conn_status = tk.StringVar(value="Not connected")
        ttk.Label(conn, textvariable=self.conn_status).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        setup = ttk.LabelFrame(top, text="Injection")
        setup.grid(row=0, column=1, sticky="nsew", **pad)
        setup.columnconfigure(7, weight=1)

        ttk.Label(setup, text="Channel").grid(row=0, column=0, sticky="w", **pad)
        self.channel_var = tk.StringVar(value="A1")
        ttk.Combobox(setup, textvariable=self.channel_var, values=list(CHANNELS), width=5,
                     state="readonly").grid(row=0, column=1, sticky="w", **pad)
        self.channel_var.trace_add("write", lambda *_: self.load_calibration())
        self.cal_var = tk.StringVar()
        ttk.Label(setup, textvariable=self.cal_var, wraplength=700, justify="left",
                  foreground=NOTE).grid(row=0, column=2, columnspan=6, sticky="w", **pad)
        ttk.Button(setup, text="Calibration files...", command=self.choose_calibration).grid(
            row=0, column=8, sticky="e", **pad)

        ttk.Label(setup, text="Dye injected (mL)").grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.injected_var = tk.StringVar()
        ttk.Entry(setup, textvariable=self.injected_var, width=8).grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(setup, text="Flow (mL/min)").grid(row=1, column=3, sticky="e", **pad)
        self.flow_var = tk.StringVar()
        ttk.Entry(setup, textvariable=self.flow_var, width=8).grid(row=1, column=4, sticky="w", **pad)
        ttk.Label(setup, text="±").grid(row=1, column=5, sticky="e")
        self.flow_unc_var = tk.StringVar(value="5")
        ttk.Entry(setup, textvariable=self.flow_unc_var, width=4).grid(row=1, column=6, sticky="w", **pad)
        ttk.Label(setup, text="%   (flow can be left blank)", foreground=NOTE).grid(
            row=1, column=7, sticky="w")

        ttk.Label(setup, text="Baseline (s)").grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        self.baseline_var = tk.StringVar(value="5")
        ttk.Spinbox(setup, textvariable=self.baseline_var, from_=2, to=30, increment=1, width=6).grid(
            row=2, column=2, sticky="w", **pad)
        ttk.Label(setup, text="Record for (s)").grid(row=2, column=3, sticky="e", **pad)
        self.duration_var = tk.StringVar(value="60")
        ttk.Spinbox(setup, textvariable=self.duration_var, from_=10, to=MAX_DURATION_S, increment=10,
                    width=6).grid(row=2, column=4, sticky="w", **pad)
        ttk.Label(setup, text="Save to").grid(row=2, column=5, columnspan=2, sticky="e", **pad)
        self.dir_var = tk.StringVar(value=str(DATA_DIR))
        ttk.Entry(setup, textvariable=self.dir_var).grid(row=2, column=7, sticky="ew", **pad)
        ttk.Button(setup, text="Browse", command=self.choose_dir).grid(row=2, column=8, sticky="e", **pad)

        controls = ttk.Frame(self.root)
        controls.grid(row=1, column=0, sticky="ew", padx=6)
        self.record_btn = ttk.Button(controls, text="Record", command=self.start_recording, state="disabled")
        self.record_btn.pack(side="left", padx=4, pady=2)
        self.open_btn = ttk.Button(controls, text="Analyse saved trace...", command=self.choose_trace)
        self.open_btn.pack(side="left", padx=4)
        self.reanalyse_btn = ttk.Button(controls, text="Re-analyse", command=self.analyse, state="disabled")
        self.reanalyse_btn.pack(side="left", padx=4)
        self.prompt_var = tk.StringVar()
        self.prompt = ttk.Label(controls, textvariable=self.prompt_var, font=("TkDefaultFont", 16, "bold"))
        self.prompt.pack(side="left", padx=16)
        self.progress_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.progress_var, foreground=NOTE).pack(side="right", padx=4)

        plots = ttk.Frame(self.root)
        plots.grid(row=2, column=0, sticky="nsew")
        for i in range(2):
            plots.columnconfigure(i, weight=1)
            plots.rowconfigure(i, weight=1)
        specs = [
            ("1  Raw voltage from the ADC", "Voltage (V)", 0.002),
            (f"2  Filtered: {ia.MEDIAN_S:g} s running median (bubbles), then "
             f"{ia.SMOOTH_S:g} s running mean (noise)", "Voltage (V)", 0.002),
            ("3  Absorbance  A = log₁₀(I₀ / V),  I₀ = mean of the filtered baseline",
             "Absorbance", 0.005),
            ("4  Dye fraction from the calibration curve;  shaded area = ∫c dt",
             "Dye fraction", 0.005),
        ]
        self.plots = []
        for i, (title, ylabel, span) in enumerate(specs):
            frame = ttk.LabelFrame(plots, text=title)
            frame.grid(row=i // 2, column=i % 2, sticky="nsew", **pad)
            plot = TracePlot(frame, ylabel, "", span)
            plot.pack(fill="both", expand=True, padx=3, pady=3)
            self.plots.append(plot)

        result = ttk.LabelFrame(self.root, text="Result (±2σ)")
        result.grid(row=3, column=0, sticky="ew", **pad)
        big = ("TkDefaultFont", 14, "bold")
        self.big_vars = [tk.StringVar() for _ in range(3)]
        for i, var in enumerate(self.big_vars):
            ttk.Label(result, textvariable=var, font=big).grid(row=0, column=i, sticky="w", padx=10, pady=3)
        self.detail_var = tk.StringVar()
        ttk.Label(result, textvariable=self.detail_var, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=10)
        self.warning_var = tk.StringVar()
        ttk.Label(result, textvariable=self.warning_var, foreground="#b03a2e", justify="left",
                  wraplength=1100).grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))

        self.root.rowconfigure(2, weight=1)
        self.root.columnconfigure(0, weight=1)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_box["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def choose_dir(self):
        folder = filedialog.askdirectory(initialdir=self.dir_var.get())
        if folder:
            self.dir_var.set(folder)

    # ---------- plots ----------

    def clear_plots(self, duration):
        texts = ["Press Record to start", "The filtered voltage appears here as it records",
                 "Absorbance appears here as it records",
                 "The dye fraction is calculated when the recording ends"]
        for plot, text in zip(self.plots, texts):
            plot.show(duration, empty_text=text)

    def draw_live(self):
        """Plots 1-3 from the samples received so far."""
        n = len(self.live_volts)
        times, volts = self.live_times[:n], self.live_volts[:n]
        live, rec = self.live, self.recording
        live.update(times, volts)
        dur = rec["duration"]
        base = [(0, rec["baseline"], BASE_SHADE, "baseline")]
        raw_plot, filt_plot, abs_plot, _ = self.plots
        raw_plot.show(dur, [(times, volts, RAW)], base)
        i0_line = [(live.i0, LIMIT, f"I₀ = {live.i0:.4f} V" + ("" if live.i0_final else " (so far)"))] \
            if live.i0 else []
        filt_plot.show(dur, [(times, live.smooth, FILTERED)], base, i0_line)
        if live.A:
            abs_plot.show(dur, [(times[:len(live.A)], live.A, ABSORB)], base, [(0.0, NOTE, "")],
                          note="" if live.i0_final else "I₀ is provisional until the baseline ends")

    def draw_result(self, res):
        """All four plots from the finished analysis, with the baseline and pulse marked."""
        t, cal = res["times"], res["cal"]
        dur = t[-1]
        spans = [(t[0], t[res["baseline_end"]], BASE_SHADE, "baseline")]
        pulse_note = ""
        if res["detected"]:
            s, e = res["start_index"], res["end_index"]
            spans.append((t[s], t[e], PULSE_SHADE, "pulse"))
            pulse_note = f"pulse {t[s]:.1f}-{t[e]:.1f} s"
        raw_plot, filt_plot, abs_plot, frac_plot = self.plots
        raw_plot.show(dur, [(t, res["volts"], RAW)], spans, note=f"{len(t)} samples, {res['rate_Hz']:.0f} Hz")
        filt_plot.show(dur, [(t, res["volts"], "#d6d6d6"), (t, res["smooth_V"], FILTERED)], spans,
                       [(res["I0_V"], LIMIT, f"I₀ = {res['I0_V']:.4f} V")], note="grey: raw")
        abs_plot.show(dur, [(t, res["A"], ABSORB)], spans,
                      [(0.0, NOTE, ""),
                       (res["threshold_A"], "#a0521d", f"detection threshold {res['threshold_A']:.4f}"),
                       (cal.max_A, LIMIT, f"top of calibration A = {cal.max_A:.3f}")],
                      note=f"baseline noise σA = {res['sigma_base_A']:.5f}")
        fill = None
        note = "no pulse found"
        if res["detected"]:
            fill = (t[s:e + 1], res["fraction"][s:e + 1], PULSE_SHADE)
            note = f"{pulse_note},  ∫c dt = {res['integral']:.4f} s,  peak {res['peak_fraction']:.4f}"
        frac_plot.show(dur, [(t, res["fraction"], FRACTION)], spans[:1],
                       [(cal.max_fraction, LIMIT, f"top of calibration {cal.max_fraction:.3f}")],
                       fill=fill, note=note)

    # ---------- calibration ----------

    def load_calibration(self):
        channel = self.channel_var.get()
        try:
            self.cal = ia.load_calibration(self.cal_paths, channel)
        except (OSError, ValueError, KeyError) as exc:
            self.cal = None
            self.cal_var.set(f"No calibration for {channel}: {exc}")
            return
        names = ", ".join(self.display_path(p) for p in self.cal.sources)
        self.cal_var.set(f"Calibration {channel}: {self.cal.describe()}. From {names}")

    @staticmethod
    def display_path(path):
        try:
            return str(Path(path).resolve().relative_to(ia.HERE))
        except ValueError:
            return Path(path).name

    def choose_calibration(self):
        paths = filedialog.askopenfilenames(
            title="Calibration summaries to use", initialdir=str(ia.DEFAULT_CAL_DIR),
            filetypes=[("Calibration summary", f"{ia.SUMMARY_STEM}*.csv"), ("CSV files", "*.csv")])
        if paths:
            self.cal_paths = [Path(p) for p in paths]
            self.load_calibration()
            if self.trace:
                self.analyse()

    # ---------- inputs ----------

    def read_inputs(self):
        """The analysis settings, or None after showing what's wrong."""
        def number(var, name, required=False, allow_zero=False):
            text = var.get().strip()
            if not text and not required:
                return None
            try:
                value = float(text)
                if value < 0 or (value == 0 and not allow_zero):
                    raise ValueError
            except ValueError:
                need = "0 or more" if allow_zero else "more than 0"
                raise ValueError(f"{name} must be a number, {need}"
                                 f"{'' if required else ', or left blank'}.") from None
            return value

        try:
            return {
                "injected": number(self.injected_var, "Dye injected"),
                "flow": number(self.flow_var, "Flow"),
                "flow_unc": number(self.flow_unc_var, "Flow uncertainty", required=True, allow_zero=True),
                "baseline": number(self.baseline_var, "Baseline", required=True),
            }
        except ValueError as exc:
            messagebox.showerror("Check the inputs", str(exc))
            return None

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
                if not line and not pinged and time.monotonic() - opened > 3:
                    ser.write(b"PING\n")  # the board didn't reset on open
                    pinged = True
            raise TimeoutError("No response. Is calibration_logger.ino uploaded, and the "
                               "Serial Monitor and the calibration GUI closed?")
        except Exception as exc:
            if ser:
                ser.close()
            self.events.put(("connect_error", str(exc)))

    # ---------- recording ----------

    def start_recording(self):
        inputs = self.read_inputs()
        if inputs is None:
            return
        try:
            duration = float(self.duration_var.get())
            if not inputs["baseline"] + 5 <= duration <= MAX_DURATION_S:
                raise ValueError
        except ValueError:
            messagebox.showerror("Duration", f"Record for at least 5 s longer than the baseline, "
                                             f"up to {MAX_DURATION_S} s.")
            return
        if self.cal is None and not messagebox.askokcancel(
                "No calibration", f"There's no calibration for {self.channel_var.get()}, so the "
                "trace can be recorded and saved but not analysed. Record anyway?"):
            return

        self.recording = {"channel": self.channel_var.get(), "duration": duration,
                          "baseline": inputs["baseline"], "injected_text": self.injected_var.get().strip(),
                          "folder": Path(self.dir_var.get())}
        for widget in (self.record_btn, self.connect_btn, self.open_btn, self.reanalyse_btn):
            widget.config(state="disabled")
        for var in self.big_vars + [self.detail_var, self.warning_var]:
            var.set("")
        self.live_times, self.live_volts = [], []
        self.live = LiveProcessor(inputs["baseline"])
        self.clear_plots(duration)
        self.record_start = time.monotonic()
        threading.Thread(target=self._record_worker,
                         args=(CHANNELS[self.recording["channel"]], duration,
                               self.live_times, self.live_volts), daemon=True).start()

    def _record_worker(self, channel, duration, times, volts):
        try:
            self.ser.reset_input_buffer()
            self.ser.write(f"R {channel} {int(duration * 1000)}\n".encode())
            started, expected = False, None
            deadline = time.monotonic() + duration + 5
            while expected is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("The Arduino stopped responding during the recording.")
                line = self.ser.readline().decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("ERR"):
                    reason = line[4:]
                    if "duration" in reason:
                        reason += (" (the Arduino still has the old 60 s limit: upload "
                                   "calibration_logger.ino again)")
                    raise RuntimeError(f"Arduino: {reason}")
                if line == "START":
                    started = True
                elif line.startswith("END"):
                    expected = int(line.split()[1])
                elif started:
                    t_us, v = line.split(",")
                    t_s, v_V = int(t_us) / 1e6, float(v)
                    times.append(t_s)  # times first, so len(volts) never exceeds len(times)
                    volts.append(v_V)
            if len(volts) != expected:
                raise RuntimeError(f"Received {len(volts)} of {expected} samples; the recording "
                                   "was not saved. Try again.")
            self.events.put(("recording_done", (times, volts)))
        except Exception as exc:
            self.events.put(("recording_error", str(exc)))

    def finish_recording(self, times, volts):
        rec = self.recording
        injected = filename_part(rec["injected_text"]) or "unknown"
        stamp = datetime.now()
        rec["folder"].mkdir(parents=True, exist_ok=True)
        path = rec["folder"] / f"{rec['channel']}_inject_{injected}mLdye_{stamp:%Y%m%d-%H%M%S}.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "voltage_V"])
            writer.writerows((f"{t:.6f}", f"{v:.5f}") for t, v in zip(times, volts))
        self.trace = (times, volts, path)
        self.progress_var.set(f"Saved {path.name}")
        if self.channel_var.get() != rec["channel"]:
            self.channel_var.set(rec["channel"])  # analyse with the recorded cell's calibration
        if self.cal is not None:
            self.analyse()

    def end_recording(self):
        self.recording = None
        self.record_start = None
        self.live = None
        self.prompt_var.set("")
        self.record_btn.config(state="normal" if self.ser else "disabled")
        self.connect_btn.config(state="normal")
        self.open_btn.config(state="normal")
        self.reanalyse_btn.config(state="normal" if self.trace else "disabled")

    # ---------- analysis ----------

    def choose_trace(self):
        path = filedialog.askopenfilename(title="Injection trace to analyse", initialdir=self.dir_var.get(),
                                          filetypes=[("Trace CSV", "*.csv")])
        if path:
            self.open_trace(Path(path))

    def open_trace(self, path):
        try:
            times, volts = ia.read_trace(path)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("Couldn't open the trace", str(exc))
            return
        channel = path.name.split("_")[0]
        if channel in CHANNELS and channel != self.channel_var.get():
            self.channel_var.set(channel)  # use that cell's calibration
        self.trace = (times, volts, path)
        self.reanalyse_btn.config(state="normal")
        self.progress_var.set(f"Opened {path.name}")
        self.analyse()

    def analyse(self):
        if not self.trace:
            return
        inputs = self.read_inputs()
        if inputs is None:
            return
        if self.cal is None:
            messagebox.showerror("No calibration", self.cal_var.get())
            return
        times, volts, path = self.trace
        try:
            res = ia.analyze(times, volts, self.cal, inputs["injected"], inputs["flow"],
                             inputs["flow_unc"] / 100, inputs["baseline"])
        except ValueError as exc:
            messagebox.showerror("Couldn't analyse the trace", str(exc))
            return
        self.draw_result(res)
        self.show_numbers(res)
        try:
            self.save_analysis(res, path, inputs)
        except OSError as exc:
            messagebox.showerror("Couldn't save the analysis", str(exc))

    def show_numbers(self, res):
        big = ["No dye pulse found", "", ""]
        if res["detected"]:
            big[0] = (f"Dye past cell: {res['dye_mL']:.4f} ± {2 * res['sd_dye_mL']:.4f} mL"
                      if "dye_mL" in res else "Enter the flow to get the dye volume")
            if "recovery" in res:
                big[1] = f"Recovery: {res['recovery']:.0%} ± {2 * res['sd_recovery']:.0%}"
            if "implied_flow_mL_min" in res:
                big[2] = (f"Implied flow: {res['implied_flow_mL_min']:.2f} "
                          f"± {2 * res['sd_implied_flow']:.2f} mL/min")
        for var, text in zip(self.big_vars, big):
            var.set(text)
        self.detail_var.set("\n".join(ia.summary_lines(res, warnings=False)))
        self.warning_var.set("\n".join(f"⚠ {w}" for w in res["warnings"]))

    def save_analysis(self, res, trace_path, inputs):
        stem = trace_path.stem.removesuffix("_processed")
        ia.write_processed(trace_path.with_name(f"{stem}_processed.csv"), res)
        log = trace_path.parent / LOG_NAME
        new_log = not log.exists()
        t = res["times"]

        def fmt(key, spec=".6f"):
            return format(res[key], spec) if key in res else ""

        with open(log, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new_log:
                writer.writeheader()
            writer.writerow({
                "analysed_at": datetime.now().isoformat(timespec="seconds"),
                "trace": trace_path.name,
                "channel": self.channel_var.get(),
                "injected_mL": inputs["injected"] or "",
                "flow_mL_min": inputs["flow"] or "",
                "flow_unc_pct": inputs["flow_unc"],
                "baseline_s": inputs["baseline"],
                "I0_V": fmt("I0_V"),
                "sigma_base_A": fmt("sigma_base_A"),
                "pulse_start_s": f"{t[res['start_index']]:.3f}" if res["detected"] else "",
                "pulse_end_s": f"{t[res['end_index']]:.3f}" if res["detected"] else "",
                "peak_fraction": fmt("peak_fraction"),
                "integral_s": fmt("integral"),
                "sd_integral_s": fmt("sd_integral"),
                "dye_mL": fmt("dye_mL"),
                "sd_dye_mL": fmt("sd_dye_mL"),
                "recovery": fmt("recovery", ".4f"),
                "sd_recovery": fmt("sd_recovery", ".4f"),
                "implied_flow_mL_min": fmt("implied_flow_mL_min", ".4f"),
                "sd_implied_flow": fmt("sd_implied_flow", ".4f"),
                "above_range": fmt("above_range", ".3f"),
                "calibration_files": "; ".join(self.display_path(p) for p in res["cal"].sources),
                "warnings": " | ".join(res["warnings"]),
            })

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
            elif kind == "recording_done":
                try:
                    self.finish_recording(*payload)
                except OSError as exc:
                    self.progress_var.set("Save failed")
                    messagebox.showerror("Save failed", str(exc))
                self.end_recording()
            elif kind == "recording_error":
                self.progress_var.set("Recording failed")
                self.end_recording()
                messagebox.showerror("Recording failed", payload)

        if self.record_start is not None:
            rec = self.recording
            elapsed = min(time.monotonic() - self.record_start, rec["duration"])
            inject_at = rec["baseline"] + INJECT_DELAY_S
            if elapsed < inject_at:
                self.prompt_var.set(f"Baseline: don't inject yet ({inject_at - elapsed:.1f} s)")
                self.prompt.config(foreground=NOTE)
            elif elapsed < inject_at + 5:
                self.prompt_var.set("INJECT NOW")
                self.prompt.config(foreground="#d0661c")
            else:
                self.prompt_var.set("Wait for the dye to wash out")
                self.prompt.config(foreground=NOTE)
            self.progress_var.set(f"Recording... {elapsed:.0f} / {rec['duration']:g} s")
            self.plot_tick = (self.plot_tick + 1) % 3  # redraw every third poll, ~7 Hz
            if self.plot_tick == 0 and self.live_volts:
                self.draw_live()

        self.root.after(50, self.poll_events)

    def on_close(self):
        if self.ser:
            self.ser.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("1250x900")
    InjectionApp(root)
    root.mainloop()
