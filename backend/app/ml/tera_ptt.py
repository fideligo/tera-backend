"""
tera_ptt.py — the core Tera pipeline. Raw SCG + PPG in, trend out.

This is the deliverable the whole product rests on. Everything else (the
oscillometric work, the context layer, the OCR) sits around it.

    SCG (accelerometer, chest)  ─┐
                                 ├─> beat pairing ─> per-beat PTT ─> trend
    PPG (camera + torch, finger)─┘        │
                                          └─> quality gate: refuse, don't guess

Design notes
------------
* Beat detection reuses the team's validated approach (band-pass -> Hilbert
  envelope -> adaptive-threshold peaks), the same chain measured at 0.64 bpm
  MAE against ECG on 153 recordings.

* The quality gate extends the team's dual-estimator pattern. On a single
  signal we compare peak-detected HR against spectral HR. Here we get a
  THIRD, stronger check for free: SCG heart rate vs PPG heart rate. If the
  chest and the finger disagree about the heart rate, they are not observing
  the same heartbeats, and PTT is undefined. That is a direct test of the
  simultaneity assumption the whole method depends on.

* PTT SHORTENS as blood pressure RISES (Moens-Korteweg). Sign errors here are
  silent and catastrophic, so the direction is asserted in the tests.

Entry point:
    analyze_session(scg, ppg, fs_scg, fs_ppg, anchor=None) -> dict
"""
from __future__ import annotations

import numpy as np

try:
    from scipy.signal import butter, sosfiltfilt, hilbert, find_peaks
    _SCIPY = True
except ImportError:                                            # pragma: no cover
    _SCIPY = False

    def _fft_band(x, fs, lo, hi):
        n = len(x)
        f = np.fft.rfftfreq(n, 1.0 / fs)
        X = np.fft.rfft(x - x.mean())
        X[(f < lo) | (f > hi)] = 0
        return np.fft.irfft(X, n)

    def hilbert(x):                                            # analytic signal
        n = len(x)
        X = np.fft.fft(x)
        h = np.zeros(n)
        h[0] = 1
        if n % 2 == 0:
            h[n // 2] = 1
            h[1:n // 2] = 2
        else:
            h[1:(n + 1) // 2] = 2
        return np.fft.ifft(X * h)

    def find_peaks(x, distance=1, prominence=None, **_):
        x = np.asarray(x, float)
        cand = np.where((x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:]))[0] + 1
        if prominence is not None:
            cand = cand[x[cand] >= np.median(x) + prominence]
        keep, taken = [], np.zeros(len(x), bool)
        for i in cand[np.argsort(-x[cand])]:
            lo, hi = max(0, i - int(distance)), min(len(x), i + int(distance) + 1)
            if not taken[lo:hi].any():
                keep.append(i)
                taken[i] = True
        return np.array(sorted(keep), dtype=int), {}


# ------------------------------------------------------------------ tunables
PTT_MIN_S, PTT_MAX_S = 0.08, 0.40    # physiological acceptance window
# ---------------------------------------------------------------------------
# HEART-RATE AGREEMENT TOLERANCE
#
# Taken from the standard rather than chosen by us:
#
#   ANSI/AAMI EC13, "Cardiac monitors, heart rate meters, and alarms"
#   (ANSI/AAMI/ISO EC13:2002 (R)2007), which is FDA-recognised and is the
#   accuracy spec wearable-validation studies cite. It permits a readout error
#   of "+/-10% of the input rate or +/-5 bpm, WHICHEVER IS GREATER".
#
# So the tolerance is a function of the rate, not a constant. At a seated
# resting 70 bpm it is 7 bpm; at 100 bpm it is 10. Two earlier versions of this
# file used flat constants (10 bpm within-sensor, 8 bpm across sensors); both
# were too loose at rest and the 8 was simply picked rather than derived.
#
# Two independent sanity checks on the same number:
#
#   Beats. HR = 60*n/duration, so one miscounted beat moves the estimate by
#   60/duration bpm — 2 bpm over a 30 s recording. EC13's 7 bpm at a resting
#   70 bpm therefore allows about 3 miscounted beats, which is the right order:
#   the two streams do not start and stop on the same beat, so one or two beats
#   of edge effect are legitimate and three-plus implies real misdetection.
#
#   The team's own pre-registered rule (analysis_v10) used +/-10 bpm limits of
#   agreement, which is EC13's allowance at 100 bpm. Consistent, at the rate
#   that rule was written for.
#
# Applying a single-device spec to the agreement between two of our own
# estimates is the CONSERVATIVE reading: if each is independently allowed E,
# their difference could reach 2E in the worst case, and we demand E.
EC13_RELATIVE = 0.10                 # +/-10% of the input rate
EC13_FLOOR_BPM = 5.0                 # ...or +/-5 bpm, whichever is greater


def hr_tolerance_bpm(hr_bpm):
    """ANSI/AAMI EC13 readout tolerance at this heart rate."""
    if hr_bpm is None or not np.isfinite(hr_bpm) or hr_bpm <= 0:
        return EC13_FLOOR_BPM
    return float(max(EC13_RELATIVE * hr_bpm, EC13_FLOOR_BPM))
MIN_PAIRS = 12                       # beats needed for a usable median
MIN_PAIR_YIELD = 0.50                # fraction of SCG beats that must pair
# Within-session dispersion ceiling. NOT tuned to taste: it is the 10 ms
# sensing-chain budget the proposal already derives (SCG 0.57 ms + PPG 4-7 ms
# against a 10-50 ms signal).
#
# It is also validated in the posture we actually use. Tera's protocol is
# SEATED, and the PhysioNet PTT dataset's "sit" condition is exactly that: the
# 10 ms ceiling keeps 6 of 8 seated recordings and rejects 13 of 16 walking or
# running ones. The two it rejects are genuinely noisy (SD up to 87 ms) and
# should be refused. A recording noisier than our own error budget is not
# usable, whatever else is true of it.
MAX_PTT_SD_MS = 10.0
TREND_MIN_DELTA_MS = 10.0            # bottom of the clinically meaningful band
SCG_ONSET_FRAC = 0.82                # envelope fraction marking aortic opening


def _bandpass(x, fs, lo, hi, order=4):
    x = np.nan_to_num(np.asarray(x, float))
    if _SCIPY:
        ny = 0.5 * fs
        sos = butter(order, [max(lo / ny, 1e-6), min(hi / ny, 0.999)],
                     btype="band", output="sos")
        return sosfiltfilt(sos, x)
    return _fft_band(x, fs, lo, min(hi, 0.49 * fs))


def _envelope(x, fs, lo, hi):
    xf = _bandpass(x - np.nanmean(x), fs, lo, hi)
    env = np.abs(hilbert(xf))
    k = max(int(0.05 * fs), 1)
    return np.convolve(env, np.ones(k) / k, mode="same")


def _spectral_hr(env, fs):
    """Independent HR estimate from the dominant envelope frequency."""
    e = np.asarray(env, float) - np.mean(env)
    F = np.abs(np.fft.rfft(e))
    f = np.fft.rfftfreq(len(e), 1.0 / fs)
    band = (f >= 0.8) & (f <= 3.0)
    if band.sum() < 2:
        return np.nan
    return 60.0 * f[band][int(np.argmax(F[band]))]


# ------------------------------------------------------------ beat detection
def detect_scg_beats(scg, fs, max_bpm=200):
    """Aortic-opening times from chest SCG, in seconds.

    Returns (beat_times_s, peak_hr, spectral_hr). The existing team pipeline
    returns RR intervals; PTT needs absolute times, so this returns those.
    """
    env = _envelope(scg, fs, 1.0, 25.0)
    if len(env) < fs * 3:
        return np.array([]), np.nan, np.nan
    pk, _ = find_peaks(env, distance=int(fs * 60 / max_bpm),
                       prominence=0.5 * np.std(env))

    # The envelope PEAK sits inside the ejection complex and the smoothing
    # kernel pushes it later still (measured +18 ms against ground truth), so
    # backtrack toward the onset. The threshold fraction was swept on
    # synthetic data with a known answer: 0.20 overshoots to -25 ms, the peak
    # itself is +18 ms, and ONSET_FRAC below lands the PTT bias within a few
    # ms of zero. Re-check this on real recordings; a constant offset is
    # absorbed by cuff calibration, but a drifting one is not.
    onset = []
    back = int(0.12 * fs)
    for p in pk:
        a = max(0, p - back)
        seg = env[a:p + 1]
        if seg.size < 2:
            onset.append(float(p)); continue
        base = seg.min()
        thr = base + SCG_ONSET_FRAC * (env[p] - base)
        above = np.where(seg >= thr)[0]
        if above.size == 0:
            onset.append(float(p)); continue
        i = above[0]
        # linear interpolation between the straddling samples: at 200 Hz one
        # sample is 5 ms, which is a large slice of a 10-50 ms signal
        if i > 0 and seg[i] > seg[i - 1]:
            i = (i - 1) + (thr - seg[i - 1]) / (seg[i] - seg[i - 1])
        onset.append(a + float(i))
    t = np.unique(np.round(np.asarray(onset, float) / float(fs), 6))
    rr = np.diff(t)
    rr = rr[(rr > 60.0 / max_bpm) & (rr < 60.0 / 40.0)]
    hr = 60.0 / np.median(rr) if rr.size else np.nan
    return t, hr, _spectral_hr(env, fs)


def _orient_ppg(x):
    """Return the PPG the right way up.

    A real PPG has a fast systolic upstroke and a slow decay, so the
    DERIVATIVE is positively skewed. Phone cameras often deliver it inverted
    (more blood absorbs more light). Testing skew of the amplitude is the
    wrong test and gives the wrong answer; testing skew of the derivative is
    the right one. This cost a debugging session on Notebook B.
    """
    d = np.gradient(np.asarray(x, float))
    s = np.mean((d - d.mean()) ** 3) / (d.std() ** 3 + 1e-12)
    return -np.asarray(x, float) if s < 0 else np.asarray(x, float)


def detect_ppg_feet(ppg, fs, max_bpm=200):
    """Pulse-foot times at the fingertip, in seconds.

    The foot is the minimum immediately preceding each systolic upstroke.
    The search window is adaptive (a fraction of the observed beat interval)
    so it can never reach back into the previous pulse.
    """
    x = _orient_ppg(ppg)
    f = _bandpass(x, fs, 0.5, min(8.0, 0.45 * fs))
    if len(f) < fs * 3:
        return np.array([]), np.nan, np.nan
    pk, _ = find_peaks(f, distance=int(fs * 60 / max_bpm),
                       prominence=0.3 * np.std(f))
    if len(pk) < 3:
        return np.array([]), np.nan, np.nan

    ibi = float(np.median(np.diff(pk)))
    win = int(min(0.25 * ibi, 0.20 * fs))

    # INTERSECTING TANGENTS, not argmin.
    # At 30 fps one sample is 33 ms, and the foot is a flat minimum, so argmin
    # lands a sample or two early (measured: -39 ms). The intersection of the
    # diastolic baseline with the tangent at maximum upstroke is sub-sample
    # accurate and is the standard fiducial in the PTT literature.
    d = np.gradient(f)
    feet = []
    for p in pk:
        a = max(0, p - win)
        if p - a < 3:
            continue
        seg, dseg = f[a:p + 1], d[a:p + 1]
        i_min = int(np.argmin(seg))
        if p - (a + i_min) < 2:
            continue
        i_slope = i_min + int(np.argmax(dseg[i_min:]))
        slope = dseg[i_slope]
        if slope <= 1e-12:
            feet.append((a + i_min) / float(fs)); continue
        # baseline y = seg[i_min];  tangent y = f[i_slope] + slope*(i - i_slope)
        i_foot = i_slope + (seg[i_min] - seg[i_slope]) / slope
        i_foot = float(np.clip(i_foot, 0, p - a))
        feet.append((a + i_foot) / float(fs))
    feet = np.unique(np.round(np.asarray(feet, float), 6))

    rr = np.diff(feet)
    rr = rr[(rr > 60.0 / max_bpm) & (rr < 60.0 / 40.0)]
    hr = 60.0 / np.median(rr) if rr.size else np.nan
    env = np.abs(hilbert(f))
    return feet, hr, _spectral_hr(env, fs)


# --------------------------------------------------------------- beat pairing
def pair_beats(scg_t, ppg_t, lo=PTT_MIN_S, hi=PTT_MAX_S):
    """Match each SCG beat to the first PPG foot inside the PTT window.

    PTT is defined between two events of THE SAME cardiac cycle. Pairing a
    beat with a foot from a different cycle does not degrade PTT, it invents
    a number. So a beat with no foot in the window is dropped, not stretched.
    """
    scg_t = np.asarray(scg_t, float)
    ppg_t = np.asarray(ppg_t, float)
    if scg_t.size == 0 or ppg_t.size == 0:
        return np.empty((0, 2)), np.array([])

    pairs, used = [], set()
    for ts in scg_t:
        cand = np.where((ppg_t >= ts + lo) & (ppg_t <= ts + hi))[0]
        cand = [i for i in cand if i not in used]
        if cand:
            i = cand[0]                     # earliest foot in the window
            used.add(i)
            pairs.append((ts, ppg_t[i]))
    if not pairs:
        return np.empty((0, 2)), np.array([])
    P = np.array(pairs)
    return P, (P[:, 1] - P[:, 0]) * 1000.0  # ms


def ptt_summary(ptt_ms, n_scg_beats, duration_s=None):
    """duration_s is carried through because the cross-sensor heart-rate
    tolerance is defined in BEATS, and converting beats to bpm needs it."""
    if ptt_ms.size == 0:
        return dict(n=0, median=np.nan, sd=np.nan, iqr=np.nan, yield_=0.0,
                    duration_s=duration_s)
    return dict(
        n=int(ptt_ms.size),
        median=float(np.median(ptt_ms)),
        sd=float(np.std(ptt_ms, ddof=1)) if ptt_ms.size > 1 else np.nan,
        iqr=float(np.percentile(ptt_ms, 75) - np.percentile(ptt_ms, 25)),
        yield_=float(ptt_ms.size / max(n_scg_beats, 1)),
        duration_s=duration_s,
    )


# ---------------------------------------------------------------- quality gate
# Plausible resting heart rate, by posture. Lying down lowers heart rate by
# roughly 10 bpm versus sitting, so a single band would either accept nonsense
# supine or reject healthy athletes. Deliberately generous: this is a sanity
# check against a broken detector, not a clinical range.
HR_PLAUSIBLE = {
    "supine": (38.0, 130.0),
    "seated": (45.0, 140.0),
}


def hr_gate(scg_hr, scg_spec_hr, ppg_hr, ppg_spec_hr, posture="seated",
            duration_s=None):
    """Heart rate is a SEPARATE, easier question than pulse transit time.

    PTT needs both sensors, a shared clock, and beats that pair. Heart rate
    needs one sensor that can count. So a recording can fail the PTT gate and
    still carry a perfectly good heart rate, and refusing to report it would
    be throwing away the one number we are sure of.

    Confidence tiers, best first:
        chest+finger  both sensors independently agree. Two devices, one answer.
        chest         SCG's own dual estimators agree; finger unusable.
        finger        PPG's own dual estimators agree; chest unusable.

    Returns (bpm, source, reason). bpm is None when nothing is trustworthy.
    """
    lo, hi = HR_PLAUSIBLE.get(posture, HR_PLAUSIBLE["seated"])

    def _self_consistent(a, b):
        return (np.isfinite(a) and np.isfinite(b)
                and abs(a - b) <= hr_tolerance_bpm(a) and lo <= a <= hi)

    chest_ok = _self_consistent(scg_hr, scg_spec_hr)
    finger_ok = _self_consistent(ppg_hr, ppg_spec_hr)

    if chest_ok and finger_ok and \
            abs(scg_hr - ppg_hr) <= hr_tolerance_bpm(min(scg_hr, ppg_hr)):
        return float(np.mean([scg_hr, ppg_hr])), "chest+finger", "ok"
    if chest_ok:
        return float(scg_hr), "chest", "ok"
    if finger_ok:
        return float(ppg_hr), "finger", "ok"

    if not (np.isfinite(scg_hr) and np.isfinite(ppg_hr)):
        return None, None, "no usable heartbeat found in either sensor"
    return None, None, (f"heartbeat detection inconsistent (chest {scg_hr:.0f}, "
                        f"finger {ppg_hr:.0f} bpm)")


def quality_gate(scg_hr, scg_spec_hr, ppg_hr, ppg_spec_hr, summ):
    """Refuse rather than guess. Returns (ok, reason).

    Four independent checks, cheapest first. This gates PTT ONLY; heart rate
    has its own, looser gate in hr_gate.
    """
    if summ["n"] < MIN_PAIRS:
        return False, f"only {summ['n']} paired beats, need {MIN_PAIRS}"

    for name, a, b in (("chest", scg_hr, scg_spec_hr), ("finger", ppg_hr, ppg_spec_hr)):
        if not (np.isfinite(a) and np.isfinite(b)):
            return False, f"{name} heart rate could not be estimated"
        if abs(a - b) > hr_tolerance_bpm(a):
            return False, (f"{name} beat detection unreliable "
                           f"({abs(a-b):.0f} bpm disagreement, EC13 limit "
                           f"{hr_tolerance_bpm(a):.0f})")

    # The strongest check, and unique to a two-sensor method: if the chest and
    # the finger disagree about the heart rate, they are not seeing the same
    # heartbeats, so no transit time exists between them. It also catches a
    # failure no physiology check would: if the two streams are not actually
    # simultaneous, the rates drift apart.
    # EC13 at the lower of the two rates: the conservative reading.
    tol = hr_tolerance_bpm(min(scg_hr, ppg_hr))
    if abs(scg_hr - ppg_hr) > tol:
        return False, (f"chest and finger disagree by {abs(scg_hr-ppg_hr):.0f} bpm "
                       f"(limit {tol:.0f}), not the same heartbeats")

    if summ["yield_"] < MIN_PAIR_YIELD:
        return False, f"only {100*summ['yield_']:.0f}% of beats paired"

    if np.isfinite(summ["sd"]) and summ["sd"] > MAX_PTT_SD_MS:
        return False, f"PTT too variable ({summ['sd']:.0f} ms spread), hold still"

    return True, "ok"


# ---------------------------------------------------------------------- trend
def classify_trend(ptt_ms, anchor_ptt_ms, between_session_sd_ms=None,
                   min_delta_ms=TREND_MIN_DELTA_MS):
    """Direction relative to the personal anchor.

    PTT SHORTENS when blood pressure RISES. Getting this backwards is silent,
    so it is asserted in the tests.

    On the threshold: do NOT use the within-session beat-to-beat SD here. That
    is a different variance. Beat-to-beat scatter says how noisy one recording
    was; what matters for a trend is how much the MEDIAN moves between days,
    which also absorbs posture, time of day and finger temperature. Using the
    per-beat SD (typically 4-7 ms, so a 2-sigma threshold of 8-14 ms) would
    silently discard the bottom half of the 10-50 ms band the proposal calls
    clinically meaningful.

    So: a fixed floor at the bottom of that band until enough baseline
    sessions exist to estimate the between-session SD properly, then 2 sigma
    of THAT.
    """
    if anchor_ptt_ms is None or not np.isfinite(anchor_ptt_ms):
        return "no_baseline", None
    d = ptt_ms - anchor_ptt_ms                     # +ve = PTT longer = BP lower
    thr = min_delta_ms
    if between_session_sd_ms is not None and np.isfinite(between_session_sd_ms):
        thr = max(min_delta_ms, 2.0 * between_session_sd_ms)
    if d <= -thr:
        return "rising", d
    if d >= thr:
        return "falling", d
    return "stable", d


# ------------------------------------------------------- optional rhythm check
def rhythm_flag(scg_beat_times, model_path=None):
    """Reuse the team's existing arrhythmia classifier on the same SCG buffer.

    PTT is unreliable in irregular rhythms. Every competitor excludes
    arrhythmia by QUESTIONNAIRE; we can detect it in the measurement itself.
    Returns (is_irregular, probability) or (None, None) if unavailable.
    """
    if model_path is None:
        return None, None
    from pathlib import Path
    if not Path(model_path).exists():
        return None, None
    try:
        import joblib
        bundle = joblib.load(model_path)
    except Exception as e:                                     # noqa: BLE001
        print(f"[tera_ptt] rhythm model not loaded ({type(e).__name__}); "
              "rhythm check skipped")
        return None, None

    # v11 bundles store op_threshold, v12 may not. Defaulting silently to 0.5
    # would NOT match the sensitivity-0.90 operating point the model was
    # validated at, so say so rather than quietly using the wrong threshold.
    model, feats = bundle["model"], bundle["features"]
    if "op_threshold" not in bundle:
        print("[tera_ptt] bundle has no op_threshold; using 0.5, which is NOT "
              "the validated sensitivity-0.90 operating point")
    thr = bundle.get("op_threshold", 0.5)

    rr = np.diff(np.asarray(scg_beat_times, float))
    rr = rr[(rr > 0.2) & (rr < 2.5)]
    if rr.size < 8:
        return None, None
    f = _hrv_features(rr)
    missing = [k for k in feats if k not in f]
    if missing:
        print(f"[tera_ptt] feature mismatch, model expects {missing}; skipped")
        return None, None
    x = np.array([[f[k] for k in feats]], dtype=float)
    try:
        p = float(model.predict_proba(x)[0, 1])
    except Exception as e:                                     # noqa: BLE001
        print(f"[tera_ptt] rhythm inference failed ({type(e).__name__}); skipped")
        return None, None
    return bool(p >= thr), p


def _hrv_features(rr):
    """14-feature contract, identical to jantungsinyal_pipeline."""
    Rms = np.asarray(rr, float) * 1000.0
    d = np.diff(Rms)
    sdnn = Rms.std()
    rmssd = float(np.sqrt((d ** 2).mean())) if d.size else 0.0
    run = lo = 0.0
    for r in rr:
        if r > 0.6:
            run += r; lo = max(lo, run)
        else:
            run = 0.0
    ih = 60.0 / np.clip(rr, 1e-3, None)
    slope = float(np.polyfit(np.arange(len(ih)), ih, 1)[0]) if len(ih) >= 3 else 0.0
    sd1 = float(d.std() / np.sqrt(2)) if d.size else 0.0
    sd2 = float(np.sqrt(max(2 * sdnn ** 2 - sd1 ** 2, 0.0)))
    return dict(zip(
        ["mean_hr", "mean_rr", "sdnn", "rmssd", "rr_cv", "min_rr", "max_rr",
         "pct_long", "long_brady", "hr_slope", "pnn50", "sd1", "sd2", "sd_ratio"],
        [60000.0 / Rms.mean(), Rms.mean(), sdnn, rmssd, sdnn / (Rms.mean() + 1e-8),
         Rms.min(), Rms.max(), float((np.asarray(rr) > 0.6).mean()), lo, slope,
         float((np.abs(d) > 50).mean()) if d.size else 0.0,
         sd1, sd2, sd1 / (sd2 + 1e-8)]))


# ----------------------------------------------------------------- entry point
def analyze_session(scg, ppg, fs_scg, fs_ppg, anchor=None, rhythm_model=None,
                    posture="seated"):
    """One capture -> one result. This is what the backend endpoint calls.

    `anchor` is the stored personal baseline:
        {"ptt_ms": 178.4, "ptt_sd_ms": 5.2, "cuff_sys": 145, "cuff_dia": 92}

    TWO INDEPENDENT OUTPUTS, because they are not equally hard to earn:

        out["hr"]   heart rate. Needs one sensor that can count beats.
                    Survives a failed PTT gate.
        out["ok"]   whether PULSE TRANSIT TIME is trustworthy. Needs both
                    sensors, a shared clock, and beats that pair.

    So `ok == False` with a valid `hr` is a normal, useful outcome: the app
    shows a heart rate and explains why there is no trend. When `ok` is False
    there is NO ptt_ms and NO trend, by design.
    """
    scg_t, scg_hr, scg_spec = detect_scg_beats(scg, fs_scg)
    ppg_t, ppg_hr, ppg_spec = detect_ppg_feet(ppg, fs_ppg)
    pairs, ptt_ms = pair_beats(scg_t, ppg_t)
    dur = max(len(np.asarray(scg)) / float(fs_scg),
              len(np.asarray(ppg)) / float(fs_ppg))
    summ = ptt_summary(ptt_ms, len(scg_t), duration_s=dur)

    hr_bpm, hr_src, hr_reason = hr_gate(scg_hr, scg_spec, ppg_hr, ppg_spec,
                                        posture, duration_s=dur)
    ok, reason = quality_gate(scg_hr, scg_spec, ppg_hr, ppg_spec, summ)

    irregular, p_irr = rhythm_flag(scg_t, rhythm_model)
    if ok and irregular:
        ok, reason = False, "irregular rhythm detected, PTT unreliable this session"

    out = dict(
        ok=ok, reason=reason, posture=posture,
        n_scg_beats=int(len(scg_t)), n_ppg_feet=int(len(ppg_t)),
        n_paired=summ["n"], pair_yield=round(summ["yield_"], 3),
        hr={
            "ok": hr_bpm is not None,
            "bpm": None if hr_bpm is None else round(hr_bpm, 1),
            # chest+finger means two independent sensors agreed. That is a
            # stronger claim than any single-sensor wearable can make.
            "source": hr_src,
            "reason": hr_reason,
            "beats_counted": int(len(scg_t)) if hr_src != "finger"
                             else int(len(ppg_t)),
        },
        hr_chest=None if not np.isfinite(scg_hr) else round(scg_hr, 1),
        hr_finger=None if not np.isfinite(ppg_hr) else round(ppg_hr, 1),
        irregular_rhythm=irregular, p_irregular=p_irr,
        message="BUKAN DIAGNOSIS",
    )
    if not ok:
        out.update(ptt_ms=None, trend=None)
        return out

    trend, delta = classify_trend(
        summ["median"],
        (anchor or {}).get("ptt_ms"),
        (anchor or {}).get("between_session_sd_ms"),   # NOT ptt_sd_ms
    )
    out.update(
        ptt_ms=round(summ["median"], 1),
        ptt_sd_ms=round(summ["sd"], 1) if np.isfinite(summ["sd"]) else None,
        trend=trend,
        delta_ptt_ms=None if delta is None else round(delta, 1),
        anchor_cuff=None if not anchor else
            {"sys": anchor.get("cuff_sys"), "dia": anchor.get("cuff_dia")},
    )
    return out
