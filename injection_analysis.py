"""Dye delivered past one OPT101 cell during an injection.

Turns a recorded voltage trace into dye concentration with the cell's
calibration (the calibration GUI's summary CSVs), finds the dye pulse and
integrates it:

    dye volume past the cell = Q x integral of c dt

c is the dye fraction (mL of dye per mL of fluid, the units the calibration
uses), so the result is in mL of the same dye used for calibration, and Q is
the volumetric flow through the cell. With the catheter in the OA all the dye
should pass the OA cell, so the result is compared with the volume injected
(recovery R), and the flow that would make the two agree is also reported
(indicator dilution: Q = V_injected / integral of c dt).

The calibration is used as measured rather than as a straight line, because
absorbance against dye fraction is clearly curved for these cells: dye
fraction is read off a monotonic piecewise-linear curve through the
calibration points (replicates averaged, anchored at zero dye = zero
absorbance).

Command line, for a saved trace:
    python injection_analysis.py injection_data/A1_inject_....csv --injected 0.1 --flow 7
"""

import argparse
import csv
import math
import statistics
from pathlib import Path

from calibration_gui import SUMMARY_STEM, read_session

HERE = Path(__file__).resolve().parent
# The A1 (photodiode 1) calibration trials, recorded 26 Nov 2026, 14:07-15:01.
DEFAULT_CAL_DIR = HERE / "photodiode1_calibration"

# Filter settings, shared with the live display in injection_gui.py.
MEDIAN_S = 0.1   # running median: removes spikes (bubbles) shorter than about half of this
SMOOTH_S = 0.25  # then a running mean: smooths noise without shifting the pulse in time


# ---------- calibration ----------

def isotonic_blocks(xs, ys, ws):
    """Pool adjacent violators: merge neighbouring points until absorbance strictly
    increases with dye fraction. Merged points take the weighted mean x and y."""
    blocks = []  # [x_sum_w, y_sum_w, w]
    for x, y, w in zip(xs, ys, ws):
        blocks.append([x * w, y * w, w])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]:
            xw, yw, ww = blocks.pop()
            blocks[-1][0] += xw
            blocks[-1][1] += yw
            blocks[-1][2] += ww
    return [b[0] / b[2] for b in blocks], [b[1] / b[2] for b in blocks]


def scatter_sigma(xs, ys):
    """Calibration scatter in absorbance, without assuming a curve shape: predict each
    interior point from its two neighbours by linear interpolation. Each residual
    contains the point's own error plus about half of each neighbour's, so its
    variance is about 1.5 sigma^2."""
    residuals = []
    for i in range(1, len(xs) - 1):
        w = (xs[i] - xs[i - 1]) / (xs[i + 1] - xs[i - 1])
        residuals.append(ys[i] - (ys[i - 1] + w * (ys[i + 1] - ys[i - 1])))
    if not residuals:
        return None
    return math.sqrt(statistics.fmean(r * r for r in residuals) / 1.5)


class Calibration:
    """Absorbance against dye fraction for one cell, as a monotonic piecewise-linear curve."""

    def __init__(self, fractions, absorbances, sources=()):
        groups = {}
        for x, a in zip(fractions, absorbances):
            groups.setdefault(round(x, 6), []).append(a)
        levels = sorted(groups)
        if len(levels) < 2:
            raise ValueError("The calibration needs dye trials at 2 or more different fractions.")
        means = [statistics.fmean(groups[x]) for x in levels]
        self.points = list(zip(levels, means))       # averaged measurements, for plotting
        self.n_trials = len(fractions)
        self.sources = list(sources)
        self.sigma_A = scatter_sigma(levels, means)  # None with fewer than 3 levels

        xs, ys = isotonic_blocks(levels, means, [len(groups[x]) for x in levels])
        if ys[0] <= 0:
            raise ValueError("The lowest calibration level has no positive absorbance; "
                             "check its I0.")
        self.xs = [0.0] + xs  # zero dye, zero absorbance (absorbance is relative to I0)
        self.As = [0.0] + ys
        self.max_A, self.max_fraction = self.As[-1], self.xs[-1]

    def _segment(self, a):
        """Index i of the segment As[i]..As[i+1] containing absorbance a (clamped to the ends)."""
        for i in range(len(self.As) - 1):
            if a <= self.As[i + 1]:
                return i
        return len(self.As) - 2

    def slope_at(self, a):
        """dA/d(fraction) at absorbance a."""
        i = 0 if a <= 0 else self._segment(a)
        return (self.As[i + 1] - self.As[i]) / (self.xs[i + 1] - self.xs[i])

    def fraction(self, a):
        """Dye fraction for absorbance a, and whether a is above the calibrated range.
        Below zero the first segment is extended, so baseline noise averages out
        instead of being clipped to zero; above the range it is clamped to the top
        level, which underestimates."""
        if a <= 0:
            return a / self.slope_at(0.0), False
        if a > self.max_A:
            return self.max_fraction, True
        i = self._segment(a)
        return self.xs[i] + (a - self.As[i]) / self.slope_at(a), False

    def describe(self):
        text = (f"{self.n_trials} dye trials at {len(self.points)} levels, up to fraction "
                f"{self.max_fraction:.3f} (A = {self.max_A:.3f})")
        if self.sigma_A is not None:
            text += f", scatter σA = {self.sigma_A:.4f}"
        return text


def find_summaries(root=HERE):
    """Every calibration summary CSV under the project folder."""
    return sorted(p for p in root.rglob(f"{SUMMARY_STEM}*.csv") if ".git" not in p.parts)


def default_calibration_paths():
    """The summaries in DEFAULT_CAL_DIR, or every summary in the project if it's missing."""
    return sorted(DEFAULT_CAL_DIR.glob(f"{SUMMARY_STEM}*.csv")) or find_summaries()


def load_calibration(paths, channel):
    """Build the calibration for one channel from the included dye trials in the summaries."""
    fractions, absorbances, sources = [], [], []
    for path in paths:
        trials, _ = read_session(Path(path))
        used = [t for t in trials if t["channel"] == channel and t["A"] is not None and t["included"]]
        if used:
            sources.append(Path(path))
        fractions += [t["fraction"] for t in used]
        absorbances += [t["A"] for t in used]
    if not fractions:
        raise ValueError(f"No {channel} dye trials with an absorbance in the calibration files.")
    return Calibration(fractions, absorbances, sources)


# ---------- filtering ----------

def median_filter(values, k):
    """Centred running median over k samples: removes short spikes such as bubbles."""
    h = k // 2
    out = []
    for i in range(len(values)):
        window = sorted(values[max(0, i - h):i + h + 1])
        out.append(window[len(window) // 2])
    return out


def moving_average(values, k):
    """Centred running mean over k samples. Zero phase, so the pulse doesn't shift in time."""
    if k <= 1:
        return list(values)
    h = k // 2
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    n = len(values)
    return [(prefix[min(n, i + h + 1)] - prefix[max(0, i - h)]) / (min(n, i + h + 1) - max(0, i - h))
            for i in range(n)]


# ---------- analysis ----------

def analyze(times, volts, cal, injected_mL=None, flow_mL_min=None, flow_rel_unc=0.05,
            baseline_s=5.0, median_s=MEDIAN_S, smooth_s=SMOOTH_S, detect_sigma=5.0, min_pulse_s=1.0):
    """Analyse one injection trace. Returns a dict of results and the processed signals.

    The signal is median filtered over median_s (removes bubble spikes shorter than
    about half of that) and then smoothed over smooth_s. The first baseline_s seconds
    (before injecting) give I0 and the baseline noise. The pulse is the stretch around
    the absorbance peak where the smoothed absorbance stays above the baseline noise;
    it must peak above detect_sigma times that noise and last at least min_pulse_s.
    Uncertainties are 1 sigma: calibration scatter and baseline wander are each
    treated as an offset shared by the whole pulse (a systematic error)."""
    n = len(volts)
    if n < 20 or times[-1] <= times[0]:
        raise ValueError("The trace is too short to analyse.")
    rate = (n - 1) / (times[-1] - times[0])
    filtered = median_filter(volts, 2 * round(median_s * rate / 2) + 1)
    smooth = moving_average(filtered, max(1, round(smooth_s * rate)))

    base = [i for i in range(n) if times[i] - times[0] < baseline_s]
    if len(base) < 10 or base[-1] >= n - 10:
        raise ValueError("The baseline window must cover at least 10 samples and end "
                         "well before the trace does.")
    i0 = statistics.fmean(smooth[i] for i in base)
    if i0 <= 0:
        raise ValueError("The baseline voltage is not positive.")
    A = [math.log10(i0 / max(v, 1e-6)) for v in smooth]
    conv = [cal.fraction(a) for a in A]
    frac = [c[0] for c in conv]

    base_A = [A[i] for i in base]
    sigma_base = statistics.stdev(base_A)
    half = len(base_A) // 2
    base_drift = statistics.fmean(base_A[half:]) - statistics.fmean(base_A[:half])

    res = {
        "times": times, "volts": volts, "smooth_V": smooth, "A": A, "fraction": frac,
        "baseline_end": base[-1], "rate_Hz": rate, "I0_V": i0, "sigma_base_A": sigma_base,
        "baseline_drift_A": base_drift, "cal": cal, "injected_mL": injected_mL,
        "flow_mL_min": flow_mL_min, "detected": False, "warnings": [],
    }
    warn = res["warnings"].append
    if abs(base_drift) > max(3 * sigma_base, 0.002):
        warn(f"Baseline drifted by {base_drift:+.4f} A within the baseline window; "
             "I0 may be off. Let the signal settle before recording.")

    first = base[-1] + 1
    peak = max(range(first, n), key=A.__getitem__)
    threshold = max(detect_sigma * sigma_base, 0.002)
    res.update(peak_index=peak, threshold_A=threshold)
    if A[peak] < threshold:
        warn(f"No dye pulse found: peak absorbance {A[peak]:.4f} is below the detection "
             f"threshold {threshold:.4f}.")
        return res

    edge = max(sigma_base, 0.0005)
    start = peak
    while start > first and A[start - 1] > edge:
        start -= 1
    end = peak
    while end < n - 1 and A[end + 1] > edge:
        end += 1
    if times[end] - times[start] < min_pulse_s:
        warn(f"No dye pulse found: the largest rise lasted only {times[end] - times[start]:.2f} s "
             f"at {times[peak]:.1f} s (a bubble or knock?). Dye pulses last longer than "
             f"{min_pulse_s:g} s.")
        return res
    if start == first:
        warn("Dye was already arriving when the baseline window ended. "
             "Wait longer before injecting, or shorten the baseline.")
    if end == n - 1:
        warn("The signal never returned to baseline, so the end of the pulse is cut off "
             "and the dye is underestimated. Record for longer.")

    integral = inv_slope = 0.0
    for i in range(start, end):
        dt = times[i + 1] - times[i]
        integral += 0.5 * (frac[i] + frac[i + 1]) * dt
        inv_slope += 0.5 * (1 / cal.slope_at(A[i]) + 1 / cal.slope_at(A[i + 1])) * dt
    above = sum(1 for i in range(start, end + 1) if conv[i][1]) / (end - start + 1)
    if above > 0:
        warn(f"{above:.0%} of the pulse is above the calibrated range (A > {cal.max_A:.3f}), "
             "where it is clamped, so the dye is underestimated. Dilute the injected dye "
             "or extend the calibration.")

    # integral of sigma_A / slope dt for a fixed absorbance offset sigma_A
    sd_cal = (cal.sigma_A or 0.0) * inv_slope
    sd_base = sigma_base * inv_slope
    sd_integral = math.hypot(sd_cal, sd_base)
    if cal.sigma_A is None:
        warn("The calibration has fewer than 3 levels, so its scatter is unknown and left "
             "out of the uncertainty.")
    res.update(
        detected=True, start_index=start, end_index=end, integral=integral,
        sd_integral=sd_integral, sd_integral_cal=sd_cal, sd_integral_base=sd_base,
        peak_fraction=frac[peak], peak_time=times[peak], above_range=above,
    )
    if integral <= 0:
        warn("The integrated dye is not positive.")
        return res
    rel_integral = sd_integral / integral

    if flow_mL_min:
        q = flow_mL_min / 60
        vol = q * integral
        res.update(dye_mL=vol, sd_dye_mL=vol * math.hypot(flow_rel_unc, rel_integral))
        if injected_mL:
            r = vol / injected_mL
            res.update(recovery=r, sd_recovery=r * math.hypot(flow_rel_unc, rel_integral))
    if injected_mL:
        q_implied = injected_mL / integral * 60
        res.update(implied_flow_mL_min=q_implied, sd_implied_flow=q_implied * rel_integral)
    return res


# ---------- files ----------

def read_trace(path):
    """Times and voltages from a trial CSV with time_s and voltage_V columns."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or "time_s" not in rows[0] or "voltage_V" not in rows[0]:
        raise ValueError(f"{Path(path).name} has no time_s and voltage_V columns.")
    return [float(r["time_s"]) for r in rows], [float(r["voltage_V"]) for r in rows]


def write_processed(path, res):
    """Per-sample signals behind the result, for plotting in the report."""
    start, end = res.get("start_index", -1), res.get("end_index", -2)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "voltage_V", "filtered_V", "absorbance", "dye_fraction", "in_pulse"])
        for i, t in enumerate(res["times"]):
            writer.writerow([f"{t:.6f}", f"{res['volts'][i]:.5f}", f"{res['smooth_V'][i]:.5f}",
                             f"{res['A'][i]:.5f}", f"{res['fraction'][i]:.6f}", int(start <= i <= end)])


def summary_lines(res, warnings=True):
    """The result as short lines of text. Uncertainties are shown as ±2σ."""
    lines = [f"I0 = {res['I0_V']:.5f} V, baseline noise σA = {res['sigma_base_A']:.5f}, "
             f"{res['rate_Hz']:.0f} Hz"]
    if res["detected"]:
        t = res["times"]
        lines.append(f"Pulse {t[res['start_index']]:.1f}-{t[res['end_index']]:.1f} s, peak fraction "
                     f"{res['peak_fraction']:.4f} at {res['peak_time']:.1f} s")
        lines.append(f"∫c dt = {res['integral']:.4f} ± {2 * res['sd_integral']:.4f} s (±2σ; from "
                     f"calibration ± {2 * res['sd_integral_cal']:.4f}, baseline "
                     f"± {2 * res['sd_integral_base']:.4f})")
        if "dye_mL" in res:
            lines.append(f"Dye past cell = {res['dye_mL']:.4f} ± {2 * res['sd_dye_mL']:.4f} mL at "
                         f"{res['flow_mL_min']:g} mL/min")
        if "recovery" in res:
            lines.append(f"Recovery = {res['recovery']:.1%} ± {2 * res['sd_recovery']:.1%} "
                         f"of {res['injected_mL']:g} mL injected")
        if "implied_flow_mL_min" in res:
            lines.append(f"Flow that accounts for all the dye = {res['implied_flow_mL_min']:.2f} "
                         f"± {2 * res['sd_implied_flow']:.2f} mL/min")
    if warnings:
        lines += [f"Warning: {w}" for w in res["warnings"]]
    return lines


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("trace", help="injection trace CSV (time_s, voltage_V)")
    p.add_argument("--channel", help="A0 or A1 (default: from the file name)")
    p.add_argument("--cal", nargs="+",
                   help=f"calibration summary CSVs (default: those in {DEFAULT_CAL_DIR.name}/)")
    p.add_argument("--injected", type=float, help="dye injected, mL")
    p.add_argument("--flow", type=float, help="flow through the cell, mL/min")
    p.add_argument("--flow-unc", type=float, default=5, help="flow uncertainty, %% (default 5)")
    p.add_argument("--baseline", type=float, default=5, help="baseline window, s (default 5)")
    args = p.parse_args()

    channel = args.channel or Path(args.trace).name.split("_")[0]
    cal = load_calibration(args.cal or default_calibration_paths(), channel)
    print(f"Calibration {channel}: {cal.describe()}")
    times, volts = read_trace(args.trace)
    res = analyze(times, volts, cal, args.injected, args.flow, args.flow_unc / 100, args.baseline)
    print("\n".join(summary_lines(res)))


if __name__ == "__main__":
    main()
