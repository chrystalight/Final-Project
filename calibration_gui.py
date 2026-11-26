"""Calibration logger for the OPT101 optical cells.

Pairs with calibration_logger/calibration_logger.ino. Enter the ADS1115
channel and the volumes of water and dye in the sample, press Record, and the
Arduino streams that channel's voltage for the trial duration, plotted live as
it arrives.

A trial with 0 mL of dye sets that channel's I0 (you can also type I0 in).
Every dye trial after that gets an absorbance A = log10(I0 / I) and is added
to a calibration plot of absorbance against dye fraction, dye / (water + dye),
with a least-squares line, R^2, adjusted R^2 and standard error S.

Each trial is saved to its own CSV (channel and volumes in the filename) and
appended to opt101_calibration_summary.csv.

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
SUMMARY_STEM = "opt101_calibration_summary"
SUMMARY_FIELDS = [
    "recorded_at", "channel", "water_mL", "dye_mL", "dye_fraction",
    "mean_V", "std_V", "n_samples", "sample_rate_Hz", "I0_V", "absorbance", "file",
]
# Columns a summary needs for Load session (older summaries without I0_V/absorbance
# still load: absorbance is recomputed from the 0 mL dye trials in the file).
REQUIRED_COLUMNS = {"channel", "water_mL", "dye_mL", "mean_V", "std_V"}
EXCLUSIONS_NAME = "excluded_trials.txt"  # trial CSVs left out of the fit, one per line


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


def summary_path(data_dir):
    """The summary CSV to append to. If an existing file has different columns
    (from an older version of this GUI), use a numbered file instead of mixing them."""
    for i in range(1, 100):
        path = data_dir / (f"{SUMMARY_STEM}.csv" if i == 1 else f"{SUMMARY_STEM}_{i}.csv")
        if not path.exists():
            return path
        with open(path, newline="") as f:
            if next(csv.reader(f), None) == SUMMARY_FIELDS:
                return path
    raise OSError("Too many summary files with old column layouts in the save folder.")


def linear_fit(xs, ys):
    """Least-squares line y = slope*x + intercept, with R^2, adjusted R^2 and S when n >= 3."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    fit = {"n": n, "slope": slope, "intercept": intercept}
    if n >= 3:
        sse = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        sst = sum((y - my) ** 2 for y in ys)
        r2 = 1 - sse / sst if sst > 0 else 1.0
        fit.update(r2=r2, adj_r2=1 - (1 - r2) * (n - 1) / (n - 2), S=math.sqrt(sse / (n - 2)))
    return fit


def absorbance_of(i0, mean, std):
    """A = log10(I0 / I) and its within-trial SD, sd_I / (I ln 10); (None, None) if undefined."""
    if not i0 or i0 <= 0 or mean <= 0:
        return None, None
    return math.log10(i0 / mean), std / (mean * math.log(10))


def make_trial(file, folder, recorded, channel, water, dye, mean, std, n, i0, excluded=()):
    """One trial as the GUI keeps it, whether just recorded or loaded from a summary."""
    a, sd_a = absorbance_of(i0, mean, std) if dye > 0 else (None, None)
    return {
        "file": file, "folder": folder, "recorded": recorded, "channel": channel,
        "water": water, "dye": dye, "fraction": dye / (water + dye),
        "mean": mean, "std": std, "n": n, "i0": i0, "A": a, "sdA": sd_a,
        "included": file not in excluded,
    }


def read_exclusions(folder):
    try:
        lines = (folder / EXCLUSIONS_NAME).read_text().splitlines()
    except FileNotFoundError:
        return set()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def update_exclusion(folder, file, excluded):
    """Add or remove one trial from the folder's exclusions file."""
    names = read_exclusions(folder)
    if excluded:
        names.add(file)
    else:
        names.discard(file)
    (folder / EXCLUSIONS_NAME).write_text(
        "# Trials left out of the calibration fit (one trial CSV per line)\n"
        + "".join(f"{name}\n" for name in sorted(names)))


def read_session(path):
    """Load the trials in a summary CSV. Returns (trials, latest I0 per channel)."""
    folder = path.parent
    excluded = read_exclusions(folder)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"{path.name} isn't an OPT101 calibration summary "
                             f"(missing columns: {', '.join(sorted(missing))}).")
        rows = list(reader)

    trials, latest_i0 = [], {}
    for row in rows:
        channel, dye, mean = row["channel"], float(row["dye_mL"]), float(row["mean_V"])
        if dye == 0:
            i0 = mean
        elif "I0_V" in columns:
            i0 = float(row["I0_V"]) if row["I0_V"] else None  # the I0 used when recorded
        else:
            i0 = latest_i0.get(channel)  # older summaries: most recent blank before it
        if i0:
            latest_i0[channel] = i0
        trials.append(make_trial(
            row.get("file", ""), folder, row.get("recorded_at", ""), channel,
            float(row["water_mL"]), dye, mean, float(row["std_V"]),
            int(row["n_samples"]) if row.get("n_samples") else 0, i0, excluded))
    return trials, latest_i0


def nice_ticks(lo, hi, target=5):
    """Round tick values covering lo..hi, and the decimals needed to label them."""
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    decimals = max(0, -math.floor(math.log10(step)))
    first = math.ceil(lo / step) * step
    count = int((hi - first) / step + 1e-9) + 1
    return [first + i * step for i in range(count)], decimals


def draw_axes(c, xlo, xhi, ylo, yhi, xlabel, ylabel, margins=(70, 14, 12, 38)):
    """Draw a framed plot area with ticks and axis labels on canvas c.
    Returns (xmap, ymap) converting data values to canvas coordinates."""
    left, right, top, bottom = margins
    w, h = c.winfo_width(), c.winfo_height()
    x0, x1, y0, y1 = left, w - right, top, h - bottom  # y0 is the top edge
    c.create_rectangle(x0, y0, x1, y1, outline="#999")

    def xmap(x):
        return x0 + (x - xlo) / (xhi - xlo) * (x1 - x0)

    def ymap(y):
        return y1 - (y - ylo) / (yhi - ylo) * (y1 - y0)

    ticks, decimals = nice_ticks(xlo, xhi, 6)
    for x in ticks:
        c.create_line(xmap(x), y1, xmap(x), y1 + 4, fill="#666")
        c.create_text(xmap(x), y1 + 6, text=f"{x:.{decimals}f}", anchor="n", fill="#333")
    ticks, decimals = nice_ticks(ylo, yhi, 5)
    for y in ticks:
        c.create_line(x0, ymap(y), x1, ymap(y), fill="#eee")
        c.create_text(x0 - 6, ymap(y), text=f"{y:.{decimals}f}", anchor="e", fill="#333")
    c.create_text((x0 + x1) / 2, h - 2, text=xlabel, anchor="s", fill="#333")
    c.create_text(12, (y0 + y1) / 2, text=ylabel, angle=90, fill="#333")
    return xmap, ymap


class PlotCanvas(ttk.Frame):
    """A resizable white canvas that calls draw() whenever it needs repainting."""

    def __init__(self, parent, width, height):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=width, height=height, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.draw())

    def ready(self):
        """Clear the canvas; return False if it is too small to draw on yet."""
        self.canvas.delete("all")
        return self.canvas.winfo_width() >= 120 and self.canvas.winfo_height() >= 100

    def message(self, text):
        c = self.canvas
        c.create_text(c.winfo_width() / 2, c.winfo_height() / 2, text=text, fill="#888",
                      width=c.winfo_width() - 40, justify="center")

    def draw(self):
        raise NotImplementedError


class LivePlot(PlotCanvas):
    """Voltage-vs-time plot, redrawn as samples arrive."""

    TRACE = "#1f5fbf"
    MEAN = "#d0661c"
    MIN_SPAN_V = 0.002  # never zoom in tighter than 2 mV

    def __init__(self, parent):
        self.times, self.volts = [], []
        self.duration = 10.0
        self.mean = None
        super().__init__(parent, width=520, height=280)

    def reset(self, duration):
        self.times, self.volts, self.duration, self.mean = [], [], duration, None
        self.draw()

    def set_data(self, times, volts, mean=None):
        self.times, self.volts, self.mean = times, volts, mean
        self.draw()

    def draw(self):
        if not self.ready():
            return
        if not self.volts:
            self.message("Press Record to start a trial")
            return
        lo, hi = min(self.volts), max(self.volts)
        if hi - lo < self.MIN_SPAN_V:
            mid = (hi + lo) / 2
            lo, hi = mid - self.MIN_SPAN_V / 2, mid + self.MIN_SPAN_V / 2
        pad = (hi - lo) * 0.08
        xmap, ymap = draw_axes(self.canvas, 0, self.duration, lo - pad, hi + pad,
                               "Time (s)", "Voltage (V)")

        # One min/max pair per pixel column, so spikes stay visible however many samples there are.
        points = []
        col = col_lo = col_hi = None
        for t, v in zip(self.times, self.volts):
            px = int(xmap(min(max(t, 0.0), self.duration)))
            if px != col:
                if col is not None:
                    points += [col, ymap(col_lo), col, ymap(col_hi)]
                col, col_lo, col_hi = px, v, v
            else:
                col_lo, col_hi = min(col_lo, v), max(col_hi, v)
        points += [col, ymap(col_lo), col, ymap(col_hi)]
        self.canvas.create_line(*points, fill=self.TRACE)

        if self.mean is not None:
            y = ymap(self.mean)
            x0, x1 = xmap(0), xmap(self.duration)
            self.canvas.create_line(x0, y, x1, y, fill=self.MEAN, dash=(5, 3), width=2)
            self.canvas.create_text(x1 - 4, y - 3, text=f"mean {self.mean:.5f} V",
                                    anchor="se", fill=self.MEAN)


class CalibrationPlot(PlotCanvas):
    """Absorbance against dye fraction, with +/-2 SD error bars and the fitted line.
    Excluded trials are drawn as hollow grey points and are not part of the fit."""

    POINT = "#1f5fbf"
    LATEST = "#d0661c"
    EXCLUDED = "#9a9a9a"
    FIT = "#2a9d4b"

    def __init__(self, parent):
        self.points, self.fit, self.latest = [], None, None
        super().__init__(parent, width=460, height=280)

    def set_data(self, points, fit, latest=None):
        self.points, self.fit, self.latest = points, fit, latest
        self.draw()

    def draw(self):
        if not self.ready():
            return
        if not self.points:
            self.message("Record a 0 mL dye trial (or type I₀) for this channel, "
                         "then record dye trials to build the calibration")
            return
        xs = [p["fraction"] for p in self.points]
        lows = [p["A"] - 2 * p["sdA"] for p in self.points]
        highs = [p["A"] + 2 * p["sdA"] for p in self.points]
        xhi = max(xs) * 1.1
        ylo, yhi = min(0.0, min(lows)), max(0.0, max(highs))
        if yhi - ylo < 0.01:
            yhi = ylo + 0.01
        pad = (yhi - ylo) * 0.08
        ylo, yhi = ylo - (pad if ylo < 0 else 0), yhi + pad
        c = self.canvas
        xmap, ymap = draw_axes(c, 0, xhi, ylo, yhi, "Dye fraction  dye / (water + dye)",
                               "Absorbance  log₁₀(I₀/I)")
        if ylo < 0:
            c.create_line(xmap(0), ymap(0), xmap(xhi), ymap(0), fill="#bbb")

        if self.fit:
            m, b = self.fit["slope"], self.fit["intercept"]
            c.create_line(xmap(0), ymap(b), xmap(xhi), ymap(m * xhi + b), fill=self.FIT, width=2)

        for p, lo, hi in zip(self.points, lows, highs):
            if not p["included"]:
                colour, fill = self.EXCLUDED, "white"
            else:
                colour = self.LATEST if p is self.latest else self.POINT
                fill = colour
            x, y = xmap(p["fraction"]), ymap(p["A"])
            c.create_line(x, ymap(lo), x, ymap(hi), fill=colour)
            c.create_line(x - 4, ymap(lo), x + 4, ymap(lo), fill=colour)
            c.create_line(x - 4, ymap(hi), x + 4, ymap(hi), fill=colour)
            c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=fill, outline=colour if fill == "white" else "white")


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
        self.trials = []             # every trial shown in the table (see make_trial)
        self.row_trial = {}          # table row id -> trial
        self._build_ui()
        self.refresh_ports()
        self.refresh_calibration()
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
        self.channel_var.trace_add("write", lambda *_: self.refresh_calibration())

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

        ttk.Label(trial, text="I₀ (V)").grid(row=2, column=0, sticky="w", **pad)
        i0_row = ttk.Frame(trial)
        i0_row.grid(row=2, column=1, columnspan=4, sticky="w")
        self.i0_vars = {}
        for i, ch in enumerate(CHANNELS):
            ttk.Label(i0_row, text=ch).grid(row=0, column=2 * i, padx=(6 if i == 0 else 16, 4))
            self.i0_vars[ch] = tk.StringVar()
            ttk.Entry(i0_row, textvariable=self.i0_vars[ch], width=10).grid(row=0, column=2 * i + 1)
        ttk.Label(i0_row, text="set automatically by each 0 mL dye trial, or type a value",
                  foreground="#666").grid(row=0, column=2 * len(CHANNELS), padx=12)

        ttk.Label(trial, text="Duration (s)").grid(row=3, column=0, sticky="w", **pad)
        self.duration_var = tk.StringVar(value="10")
        ttk.Spinbox(trial, textvariable=self.duration_var, from_=1, to=60, increment=1,
                    width=8).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(trial, text="Save to").grid(row=4, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(value=str(DEFAULT_DATA_DIR))
        ttk.Entry(trial, textvariable=self.dir_var, width=40).grid(row=4, column=1, columnspan=3, sticky="ew", **pad)
        ttk.Button(trial, text="Browse", command=self.choose_dir).grid(row=4, column=4, **pad)

        self.record_btn = ttk.Button(trial, text="Record", command=self.start_recording, state="disabled")
        self.record_btn.grid(row=5, column=0, **pad)
        self.progress_var = tk.StringVar()
        ttk.Label(trial, textvariable=self.progress_var).grid(row=5, column=1, columnspan=4, sticky="w", **pad)

        plots = ttk.Frame(self.root)
        plots.grid(row=2, column=0, sticky="nsew")
        plots.columnconfigure(0, weight=1)
        plots.columnconfigure(1, weight=1)
        plots.rowconfigure(0, weight=1)

        live = ttk.LabelFrame(plots, text="Live trace")
        live.grid(row=0, column=0, sticky="nsew", **pad)
        self.plot = LivePlot(live)
        self.plot.pack(fill="both", expand=True, padx=4, pady=4)

        self.cal_frame = ttk.LabelFrame(plots, text="Calibration")
        self.cal_frame.grid(row=0, column=1, sticky="nsew", **pad)
        self.cal_plot = CalibrationPlot(self.cal_frame)
        self.cal_plot.pack(fill="both", expand=True, padx=4, pady=4)
        self.fit_var = tk.StringVar()
        ttk.Label(self.cal_frame, textvariable=self.fit_var).pack(anchor="w", padx=6, pady=(0, 4))

        result = ttk.LabelFrame(self.root, text="Last trial")
        result.grid(row=3, column=0, sticky="ew", **pad)
        big = ("TkDefaultFont", 16, "bold")
        self.mean_var = tk.StringVar(value="Mean: -")
        self.std_var = tk.StringVar(value="Std: -")
        self.abs_var = tk.StringVar()
        self.info_var = tk.StringVar()
        ttk.Label(result, textvariable=self.mean_var, font=big).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(result, textvariable=self.std_var, font=big).grid(row=0, column=1, sticky="w", padx=24)
        ttk.Label(result, textvariable=self.abs_var, font=big).grid(row=0, column=2, sticky="w", padx=24)
        ttk.Label(result, textvariable=self.info_var).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        history = ttk.LabelFrame(self.root, text="Trials")
        history.grid(row=4, column=0, sticky="nsew", **pad)
        buttons = ttk.Frame(history)
        buttons.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.load_btn = ttk.Button(buttons, text="Load session...", command=self.load_session)
        self.load_btn.pack(side="left", padx=4)
        ttk.Button(buttons, text="Include / exclude selected",
                   command=self.toggle_selected).pack(side="left", padx=4)
        ttk.Label(buttons, text="Double-click a dye trial to include or exclude it from the fit",
                  foreground="#666").pack(side="left", padx=12)

        cols = ("use", "time", "channel", "water", "dye", "fraction", "mean", "std", "absorbance", "n")
        headings = ("Fit", "Recorded", "Channel", "Water (mL)", "Dye (mL)", "Dye fraction",
                    "Mean (V)", "Std (mV)", "Absorbance", "N")
        self.table = ttk.Treeview(history, columns=cols, show="headings", height=6)
        for col, heading in zip(cols, headings):
            self.table.heading(col, text=heading)
            self.table.column(col, width=50 if col == "use" else 90, anchor="center")
        self.table.column("time", width=120)
        self.table.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(history, orient="vertical", command=self.table.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.bind("<Double-1>", self.toggle_selected)
        self.root.rowconfigure(2, weight=3)
        self.root.rowconfigure(4, weight=1)
        self.root.columnconfigure(0, weight=1)
        history.rowconfigure(1, weight=1)
        history.columnconfigure(0, weight=1)

    def update_fraction(self, *_):
        """Show the dye fraction for the volumes currently entered."""
        try:
            water = parse_volume(self.water_var.get(), "water", allow_zero=False)
            dye = parse_volume(self.dye_var.get(), "dye", allow_zero=True)
            self.fraction_var.set(f"dye fraction = {dye / (water + dye):.5f}")
        except ValueError:
            self.fraction_var.set("")

    def refresh_calibration(self, latest=None):
        """Redraw the calibration plot and fit for the selected channel."""
        channel = self.channel_var.get()
        points = [tr for tr in self.trials if tr["channel"] == channel and tr["A"] is not None]
        used = [p for p in points if p["included"]]
        n_excluded = len(points) - len(used)
        fit = linear_fit([p["fraction"] for p in used], [p["A"] for p in used])
        self.cal_frame.config(text=f"Calibration: {channel}")
        self.cal_plot.set_data(points, fit, latest)
        excluded_note = f" ({n_excluded} excluded)" if n_excluded else ""
        if not fit:
            self.fit_var.set(f"{len(used)} dye trial(s) in the fit{excluded_note}: need at least "
                             "2 at different fractions to fit a line")
            return
        text = (f"A = {fit['slope']:.4g} × fraction {fit['intercept']:+.4g}    "
                f"n = {fit['n']}{excluded_note}")
        if "S" in fit:
            text += (f"    R² = {fit['r2']:.4f}    adj R² = {fit['adj_r2']:.4f}"
                     f"    S = {fit['S']:.4g}  (±2S = ±{2 * fit['S']:.4g})")
        self.fit_var.set(text)

    @staticmethod
    def table_values(tr):
        if tr["dye"] == 0:
            use, a = "", "I₀"
        elif tr["A"] is None:
            use, a = "", "-"
        else:
            use, a = ("✓" if tr["included"] else "✗"), f"{tr['A']:.4f}"
        recorded = tr["recorded"].replace("T", " ")[5:]  # MM-DD HH:MM:SS
        return (use, recorded, tr["channel"], f"{tr['water']:g}", f"{tr['dye']:g}",
                f"{tr['fraction']:.5f}", f"{tr['mean']:.5f}", f"{tr['std'] * 1000:.3f}", a, tr["n"])

    def add_trial(self, tr):
        """Add a trial to the session and to the top of the table."""
        self.trials.append(tr)
        iid = self.table.insert("", 0, values=self.table_values(tr))
        self.row_trial[iid] = tr

    def toggle_selected(self, *_):
        """Include or exclude the selected dye trials from the fit, and remember it on disk."""
        changed = False
        for iid in self.table.selection():
            tr = self.row_trial[iid]
            if tr["A"] is None:
                continue  # I0 trials and trials without an absorbance aren't in the fit anyway
            try:
                update_exclusion(tr["folder"], tr["file"], excluded=tr["included"])
            except OSError as exc:
                messagebox.showerror("Couldn't save the change", str(exc))
                break
            tr["included"] = not tr["included"]
            self.table.item(iid, values=self.table_values(tr))
            changed = True
        if changed:
            self.refresh_calibration()

    def load_session(self):
        """Replace the table and calibration with the trials in a saved summary CSV."""
        if self.trial is not None:
            messagebox.showinfo("Recording", "Wait for the current trial to finish.")
            return
        path = filedialog.askopenfilename(
            title="Load a calibration summary", initialdir=self.dir_var.get(),
            filetypes=[("Calibration summary", f"{SUMMARY_STEM}*.csv"), ("CSV files", "*.csv")])
        if not path:
            return
        path = Path(path)
        try:
            trials, latest_i0 = read_session(path)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("Couldn't load session", str(exc))
            return
        self.table.delete(*self.table.get_children())
        self.trials, self.row_trial = [], {}
        for tr in trials:
            self.add_trial(tr)
        for ch, i0 in latest_i0.items():
            if ch in self.i0_vars:
                self.i0_vars[ch].set(f"{i0:.6f}")
        self.dir_var.set(str(path.parent))  # new trials are saved alongside the loaded session
        self.refresh_calibration()
        self.progress_var.set(f"Loaded {len(trials)} trials from {path.name}")

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
        i0 = None
        if dye > 0:
            try:
                i0 = float(self.i0_vars[channel].get())
                if i0 <= 0:
                    raise ValueError
            except ValueError:
                if not messagebox.askokcancel(
                        "No I₀",
                        f"There's no valid I₀ for {channel}, so this trial won't get an "
                        "absorbance or appear on the calibration plot.\n\n"
                        "Record a 0 mL dye trial first, or type I₀ in.\n\nRecord anyway?"):
                    return
                i0 = None

        self.trial = {
            "channel": channel,
            "water_text": water_text,
            "dye_text": dye_text,
            "water": water,
            "dye": dye,
            "fraction": dye / (water + dye),
            "i0": i0,
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
        ch = t["channel"]
        n = len(volts)
        mean = statistics.fmean(volts)
        std = statistics.stdev(volts) if n > 1 else 0.0
        rate = (n - 1) / (times[-1] - times[0]) if n > 1 and times[-1] > times[0] else 0.0

        # A 0 mL dye trial becomes this channel's new I0; dye trials use the I0 set when they started.
        i0_used = mean if t["dye"] == 0 else t["i0"]
        absorbance, _ = absorbance_of(i0_used, mean, std) if t["dye"] > 0 else (None, None)

        stamp = datetime.now()
        name = (f"{ch}_{filename_part(t['water_text'])}mLwater_"
                f"{filename_part(t['dye_text'])}mLdye_{stamp:%Y%m%d-%H%M%S}.csv")
        t["data_dir"].mkdir(parents=True, exist_ok=True)
        with open(t["data_dir"] / name, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "voltage_V"])
            writer.writerows((f"{ti:.6f}", f"{v:.5f}") for ti, v in zip(times, volts))

        summary = summary_path(t["data_dir"])
        new_summary = not summary.exists()
        with open(summary, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            if new_summary:
                writer.writeheader()
            writer.writerow({
                "recorded_at": stamp.isoformat(timespec="seconds"),
                "channel": ch,
                "water_mL": t["water"],
                "dye_mL": t["dye"],
                "dye_fraction": f"{t['fraction']:.6f}",
                "mean_V": f"{mean:.6f}",
                "std_V": f"{std:.6f}",
                "n_samples": n,
                "sample_rate_Hz": f"{rate:.1f}",
                "I0_V": f"{i0_used:.6f}" if i0_used else "",
                "absorbance": f"{absorbance:.6f}" if absorbance is not None else "",
                "file": name,
            })

        # Update the display only after both files are safely written.
        self.plot.set_data(times, volts, mean)
        self.mean_var.set(f"Mean: {mean:.5f} V")
        self.std_var.set(f"Std: {std * 1000:.3f} mV")
        self.info_var.set(f"{ch}, {t['water_text']} mL water + {t['dye_text']} mL dye "
                          f"(fraction {t['fraction']:.5f}): {n} samples at {rate:.0f} Hz, saved {name}")
        tr = make_trial(name, t["data_dir"], stamp.isoformat(timespec="seconds"), ch,
                        t["water"], t["dye"], mean, std, n, i0_used)
        self.add_trial(tr)
        if t["dye"] == 0:
            self.i0_vars[ch].set(f"{mean:.6f}")
            self.abs_var.set(f"New I₀ for {ch}")
        elif tr["A"] is not None:
            self.abs_var.set(f"A = {tr['A']:.4f}")
        else:
            self.abs_var.set("No absorbance (no I₀)")
        if self.channel_var.get() != ch:
            self.channel_var.set(ch)  # show the calibration for the channel just recorded
        self.refresh_calibration(latest=tr if tr["A"] is not None else None)

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
