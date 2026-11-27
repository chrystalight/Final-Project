"""Live plot and capture for the laminar-flow pressure sensor (ADS1115 A0).

1. Upload pressure_stream/pressure_stream.ino to the Mega.
2. Close the other GUIs and the Serial Monitor (only one program can hold the port).
3. python pressure_gui.py

The plot scrolls the last few seconds of the sensor voltage, with the mean and
standard deviation of the last second underneath. "Start recording" captures
every sample until you press "Stop & save", then writes a CSV to pressure_data/
(name = your label + timestamp) and shows that recording's mean and std.
"""

import csv
import math
import queue
import statistics
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import serial
import serial.tools.list_ports

BAUD = 115200
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "pressure_data"
WINDOWS_S = ("5", "10", "30", "60")
STATS_S = 1.0          # window for the live mean/std readout
BUFFER_S = 70          # how much history the plot keeps
POLL_MS = 50

TRACE = "#1f5fbf"
REC_FILL = "#fde2e2"
GRID = "#e4e4e4"


class Reader(threading.Thread):
    """Reads "<t_us>,<volts>" lines and posts (kind, ...) tuples to a queue."""

    def __init__(self, port, out):
        super().__init__(daemon=True)
        self.ser = serial.Serial(port, BAUD, timeout=0.5)  # opening resets the Mega
        self.out = out
        self.stopping = threading.Event()

    def run(self):
        last_us, wraps = None, 0
        while not self.stopping.is_set():
            try:
                line = self.ser.readline()
            except (serial.SerialException, OSError) as exc:
                self.out.put(("error", str(exc)))
                break
            text = line.decode("ascii", "replace").strip()
            if not text:
                continue
            parts = text.split(",")
            if len(parts) == 2:
                try:
                    us, volts = int(parts[0]), float(parts[1])
                except ValueError:
                    continue
                if last_us is not None and us < last_us:
                    wraps += 1  # micros() rolls over every ~71 min
                last_us = us
                self.out.put(("sample", (us + wraps * 2**32) / 1e6, volts))
            else:
                if text == "READY":
                    last_us, wraps = None, 0  # board restarted, clock back at 0
                self.out.put(("status", text))
        try:
            self.ser.close()
        except Exception:
            pass

    def stop(self):
        self.stopping.set()


def nice_ticks(lo, hi, n=5):
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        step = m * mag
        if span / step <= n:
            break
    first = math.ceil(lo / step) * step
    ticks = []
    k = 0
    while first + k * step <= hi + step * 1e-9:
        ticks.append(first + k * step)
        k += 1
    decimals = max(0, -math.floor(math.log10(step)))
    return ticks, decimals


class LivePlot(tk.Canvas):
    MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 70, 14, 12, 38

    def __init__(self, parent):
        super().__init__(parent, background="white", highlightthickness=0,
                         width=800, height=380)

    def draw(self, ts, vs, window_s, rec_start=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        x0, x1 = self.MARGIN_L, w - self.MARGIN_R
        y0, y1 = self.MARGIN_T, h - self.MARGIN_B
        if not ts:
            self.create_text(w / 2, h / 2, text="Waiting for data...", fill="#777",
                             font=("Segoe UI", 12))
            return

        thi = ts[-1]
        tlo = thi - window_s
        start = 0
        while start < len(ts) and ts[start] < tlo:
            start += 1
        ts, vs = ts[start:], vs[start:]

        vlo, vhi = min(vs), max(vs)
        span = max(vhi - vlo, 0.002)  # at least 2 mV so pure noise isn't blown up
        mid = (vhi + vlo) / 2
        vlo, vhi = mid - span * 0.6, mid + span * 0.6

        def xmap(t):
            return x0 + (t - tlo) / window_s * (x1 - x0)

        def ymap(v):
            return y1 - (v - vlo) / (vhi - vlo) * (y1 - y0)

        if rec_start is not None:
            self.create_rectangle(max(x0, xmap(rec_start)), y0, x1, y1,
                                  fill=REC_FILL, outline="")
            self.create_text(max(x0, xmap(rec_start)) + 6, y0 + 4, anchor="nw",
                             text="recording", fill="#b03030", font=("Segoe UI", 9))

        yt, yd = nice_ticks(vlo, vhi)
        for v in yt:
            y = ymap(v)
            self.create_line(x0, y, x1, y, fill=GRID)
            self.create_text(x0 - 6, y, anchor="e", text=f"{v:.{yd}f}", font=("Segoe UI", 9))
        xt, xd = nice_ticks(tlo, thi)
        for t in xt:
            x = xmap(t)
            self.create_line(x, y0, x, y1, fill=GRID)
            self.create_text(x, y1 + 4, anchor="n", text=f"{t:.{xd}f}", font=("Segoe UI", 9))
        self.create_rectangle(x0, y0, x1, y1, outline="#888")
        self.create_text((x0 + x1) / 2, h - 4, anchor="s", text="time since connect (s)",
                         font=("Segoe UI", 9))
        self.create_text(14, (y0 + y1) / 2, text="sensor output (V)", angle=90,
                         font=("Segoe UI", 9))

        # One min/max pair per pixel column, drawn as a single zig-zag line.
        cols = {}
        for t, v in zip(ts, vs):
            px = int(xmap(t))
            lo_hi = cols.get(px)
            if lo_hi is None:
                cols[px] = [v, v]
            else:
                if v < lo_hi[0]:
                    lo_hi[0] = v
                if v > lo_hi[1]:
                    lo_hi[1] = v
        coords = []
        for px in sorted(cols):
            lo, hi = cols[px]
            coords += [px, ymap(lo), px, ymap(hi)]
        if len(coords) >= 4:
            if len(coords) == 4:
                coords += coords[-2:]
            self.create_line(*coords, fill=TRACE)


class PressureApp(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=10)
        self.root = root
        self.queue = queue.Queue()
        self.reader = None
        maxlen = BUFFER_S * 250
        self.ts = deque(maxlen=maxlen)
        self.vs = deque(maxlen=maxlen)
        self.recording = None  # list of (t, v) while recording
        self.rec_start = None

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Port").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=10, state="readonly")
        self.port_box.pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", command=self.refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=(4, 16))
        ttk.Label(top, text="Show last").pack(side="left")
        self.window_var = tk.StringVar(value="10")
        ttk.Combobox(top, textvariable=self.window_var, values=WINDOWS_S, width=4,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(top, text="s").pack(side="left")
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status_var, foreground="#555").pack(side="right")

        self.plot = LivePlot(self)
        self.plot.pack(fill="both", expand=True, pady=8)

        live = ttk.Frame(self)
        live.pack(fill="x")
        self.live_var = tk.StringVar(value="Last 1 s: -")
        ttk.Label(live, textvariable=self.live_var, font=("Consolas", 14)).pack(side="left")
        self.rate_var = tk.StringVar(value="")
        ttk.Label(live, textvariable=self.rate_var, foreground="#555").pack(side="right")

        rec_box = ttk.LabelFrame(self, text="Capture", padding=8)
        rec_box.pack(fill="x", pady=(8, 0))
        rec = ttk.Frame(rec_box)
        rec.pack(fill="x")
        ttk.Label(rec, text="Label (e.g. 10mLmin)").pack(side="left")
        self.label_var = tk.StringVar()
        ttk.Entry(rec, textvariable=self.label_var, width=18).pack(side="left", padx=4)
        self.rec_btn = ttk.Button(rec, text="Start recording", command=self.toggle_record,
                                  state="disabled")
        self.rec_btn.pack(side="left", padx=8)
        self.rec_var = tk.StringVar(value="")
        ttk.Label(rec_box, textvariable=self.rec_var, font=("Consolas", 11)).pack(anchor="w", pady=(6, 0))

        self.refresh_ports()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(POLL_MS, self.poll)

    # --- serial -------------------------------------------------------------
    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        names = [p.device for p in ports]
        self.port_box["values"] = names
        if self.port_var.get() not in names:
            guess = next((p.device for p in ports
                          if any(k in (p.description or "") for k in ("Arduino", "CH340", "USB Serial"))),
                         names[0] if names else "")
            self.port_var.set(guess)

    def toggle_connect(self):
        if self.reader:
            self.disconnect()
            return
        port = self.port_var.get()
        if not port:
            messagebox.showerror("No port", "Plug in the Arduino and press Refresh.")
            return
        try:
            self.reader = Reader(port, self.queue)
        except (serial.SerialException, OSError) as exc:
            messagebox.showerror("Can't open port",
                                 f"{exc}\n\nIs another GUI or the Serial Monitor still open?")
            return
        self.reader.start()
        self.ts.clear()
        self.vs.clear()
        self.connect_btn["text"] = "Disconnect"
        self.status_var.set(f"Connected to {port}, waiting for READY...")

    def disconnect(self):
        if self.recording is not None:
            self.toggle_record()
        if self.reader:
            self.reader.stop()
            self.reader = None
        self.connect_btn["text"] = "Connect"
        self.rec_btn["state"] = "disabled"
        self.status_var.set("Not connected")

    # --- recording ----------------------------------------------------------
    def toggle_record(self):
        if self.recording is None:
            self.recording = []
            self.rec_start = None
            self.rec_btn["text"] = "Stop & save"
            self.rec_var.set("Recording...")
            return
        samples, self.recording, self.rec_start = self.recording, None, None
        self.rec_btn["text"] = "Start recording"
        if len(samples) < 2:
            self.rec_var.set("Nothing recorded")
            return
        path = self.save(samples)
        vs = [v for _, v in samples]
        dur = samples[-1][0] - samples[0][0]
        self.rec_var.set(f"mean {statistics.fmean(vs):.5f} V   std {statistics.stdev(vs) * 1000:.3f} mV"
                         f"   n={len(vs)}   {dur:.1f} s   -> {path.name}")

    def save(self, samples):
        DATA_DIR.mkdir(exist_ok=True)
        label = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.label_var.get().strip())
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = DATA_DIR / (f"pressure_{label}_{stamp}.csv" if label else f"pressure_{stamp}.csv")
        t0 = samples[0][0]
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_s", "voltage_V"])
            for t, v in samples:
                w.writerow([f"{t - t0:.6f}", f"{v:.5f}"])
        return path

    # --- main loop ----------------------------------------------------------
    def poll(self):
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "sample":
                _, t, v = item
                self.ts.append(t)
                self.vs.append(v)
                if self.recording is not None:
                    if self.rec_start is None:
                        self.rec_start = t
                    self.recording.append((t, v))
            elif kind == "status":
                text = item[1]
                if text == "READY":
                    self.ts.clear()
                    self.vs.clear()
                    self.status_var.set(f"Streaming from {self.port_var.get()}")
                    self.rec_btn["state"] = "normal"
                else:
                    self.status_var.set(text)
            elif kind == "error":
                self.disconnect()
                self.status_var.set(f"Serial error: {item[1]}")

        if self.ts:
            tnow = self.ts[-1]
            i = len(self.ts) - 1
            while i > 0 and self.ts[i - 1] >= tnow - STATS_S:
                i -= 1
            recent = list(self.vs)[i:]
            if len(recent) > 1:
                self.live_var.set(f"Last 1 s:  {statistics.fmean(recent):.5f} V   "
                                  f"std {statistics.stdev(recent) * 1000:.3f} mV")
                self.rate_var.set(f"{len(recent) / max(tnow - self.ts[i], 1e-9):.0f} samples/s")
            if self.recording is not None and self.rec_start is not None:
                self.rec_var.set(f"Recording... {tnow - self.rec_start:.1f} s, {len(self.recording)} samples")

        self.plot.draw(list(self.ts), list(self.vs), float(self.window_var.get()), self.rec_start)
        self.after(POLL_MS, self.poll)

    def on_close(self):
        if self.recording is not None and len(self.recording) > 1:
            if messagebox.askyesno("Recording", "Save the recording in progress?"):
                self.toggle_record()
        if self.reader:
            self.reader.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    root.title("Pressure sensor (ADS1115 A0)")
    root.geometry("900x600")
    PressureApp(root).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
