"""
contract.py — everything the app is promised, with no web framework attached.

api.py is a thin FastAPI wrapper over this. The logic lives here so that:
  - it can be tested without installing a server,
  - the team can call it directly from Python if the HTTP layer is a distraction
    on demo day,
  - swapping FastAPI for anything else touches one file and breaks no promise.

Two endpoints' worth of behaviour:
    calibrate(cap, cuff_sys, cuff_dia) -> anchor to store on the phone
    session(cap)                       -> a trend, or a refusal with a reason

Both are stateless. The app owns the anchor and sends it back every time, so
nothing about a patient survives a request. That is the PDP Law 27/2022 story
in the proposal, implemented rather than asserted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

import tera_ptt as T


# Tera's protocol posture. Seated: upright, back supported, phone screen-down
# on the sternum, index finger of the same hand over the rear camera and torch.
# Seated matches AlwaysBP's ISO-validated protocol and the guideline posture
# for the cuff reading the anchor is paired with.
DEFAULT_POSTURE = "seated"
KNOWN_POSTURES = ("supine", "seated")

# How far apart the cuff reading and the calibration capture may be.
# Five minutes is the rest interval clinical BP protocols already use between
# repeat readings, so within it the person is in the same physiological state.
# Beyond it we are pairing a PTT with a blood pressure that was true at some
# other moment, which is the one thing calibration must not do.
MAX_CUFF_AGE_S = 300.0


@dataclass
class Capture:
    """One 30-second seated capture.

    Send raw samples. Do not pre-filter on the phone: the pipeline runs its own
    quality checks on the unfiltered signal, and a filter we cannot see is a bug
    we cannot find.

    Send t_scg and t_ppg. If you do, fs_scg and fs_ppg are ignored and the true
    rates are derived here. A phone that requests 200 Hz and receives 96 Hz will
    otherwise report 200, and every timing number downstream inherits that lie.
    """
    scg: List[float]                       # chest-normal axis (Z, phone flat)
    ppg: List[float]                       # camera luma/red mean, finger
    t_scg: Optional[List[float]] = None    # seconds, SHARED clock with t_ppg
    t_ppg: Optional[List[float]] = None
    scg_x: Optional[List[float]] = None    # the other two axes, optional
    scg_y: Optional[List[float]] = None
    fs_scg: Optional[float] = None         # fallback only
    fs_ppg: Optional[float] = None
    posture: str = DEFAULT_POSTURE
    anchor: Optional[dict] = None
    rhythm_model: Optional[str] = None


class RateError(ValueError):
    """The capture's timing is unusable. Not a modelling failure, an input one."""


def posture_mismatch(cap: "Capture") -> Optional[str]:
    """Refuse a session recorded in a different posture from its anchor.

    Posture changes PTT on its own. Lying down alters venous return, vascular
    tone and the hydrostatic column between heart and fingertip, and those
    shifts are the same size as the blood-pressure signal we are looking for.
    Comparing a supine session against a seated anchor therefore produces a
    confident number that means nothing, which is worse than a refusal.

    Cheap to check, and impossible to notice afterwards if we do not.
    """
    if cap.posture not in KNOWN_POSTURES:
        return (f"unknown posture '{cap.posture}', expected one of "
                f"{', '.join(KNOWN_POSTURES)}")
    if not cap.anchor:
        return None
    a = cap.anchor.get("posture")
    if a and a != cap.posture:
        return (f"this check was recorded {cap.posture} but the calibration was "
                f"{a}. Posture shifts pulse timing independently of blood "
                f"pressure, so the two are not comparable. Recalibrate, or "
                f"repeat this check {a}.")
    return None


# --------------------------------------------------------------------- timing
def derive_rate(t, fallback, name):
    """The TRUE rate, from timestamps. Never the requested one."""
    if t is None:
        if not fallback:
            raise RateError(f"{name}: send timestamps or a sampling rate")
        return float(fallback), None
    a = np.asarray(t, float)
    a = np.sort(a[np.isfinite(a)])
    if a.size < 10:
        raise RateError(f"{name}: too few timestamps")
    dt = np.diff(a)
    dt = dt[dt > 0]
    if dt.size == 0:
        raise RateError(f"{name}: timestamps do not advance")
    fs = 1.0 / float(np.median(dt))
    jitter = float(np.std(dt) / np.median(dt))
    if not (5.0 <= fs <= 2000.0):
        raise RateError(f"{name}: derived {fs:.1f} Hz, outside 5-2000 Hz")
    return fs, jitter


def resolve_timing(cap: Capture):
    """Rates, plus a report the app can show the user without interpreting."""
    fs_s, jit_s = derive_rate(cap.t_scg, cap.fs_scg, "scg")
    fs_p, jit_p = derive_rate(cap.t_ppg, cap.fs_ppg, "ppg")
    timing = {
        "fs_scg": round(fs_s, 1), "fs_ppg": round(fs_p, 1),
        "derived_from_timestamps": cap.t_scg is not None,
        "jitter_scg": round(jit_s, 3) if jit_s is not None else None,
        "jitter_ppg": round(jit_p, 3) if jit_p is not None else None,
        "warnings": [],
    }
    if fs_s < 100:
        timing["warnings"].append(
            f"accelerometer at {fs_s:.0f} Hz. Below 100 Hz the timing precision "
            "claim in the proposal does not hold.")
    for j, n in ((jit_s, "accelerometer"), (jit_p, "camera")):
        if j is not None and j > 0.25:
            timing["warnings"].append(
                f"{n} sample spacing varies by {100*j:.0f}%. Something is "
                "stealing time from the capture thread.")
    return fs_s, fs_p, timing


# ----------------------------------------------------------------- axis choice
def run_best_axis(cap: Capture, fs_s, fs_p, anchor):
    """Try each axis the phone sent and keep the best.

    The aortic-valve signature sits on the axis normal to the chest wall. Which
    physical axis that is depends on how the user held the phone, and telling
    them to hold it flat does not make them hold it flat. Trying all three costs
    milliseconds and removes an entire class of silent failure.

    Best = passes the gate with the lowest PTT variability. If none pass, the
    primary axis is returned, so the refusal reason stays honest about the axis
    we were told to use.
    """
    axes = [("z", cap.scg)]
    if cap.scg_x is not None:
        axes.append(("x", cap.scg_x))
    if cap.scg_y is not None:
        axes.append(("y", cap.scg_y))

    ppg = np.asarray(cap.ppg, float)
    primary = best = hr_fallback = None
    for name, raw in axes:
        r = T.analyze_session(np.asarray(raw, float), ppg, fs_s, fs_p,
                              anchor=anchor, rhythm_model=cap.rhythm_model,
                              posture=cap.posture)
        r["axis"] = name
        if primary is None:
            primary = r
        if r["ok"] and (best is None or r["ptt_sd_ms"] < best["ptt_sd_ms"]):
            best = r
        # Keep the best HR seen on ANY axis. A recording can fail the PTT gate
        # on every axis and still have counted the heartbeat cleanly on one of
        # them, and that number is worth returning.
        if r["hr"]["ok"] and (hr_fallback is None
                              or r["hr"]["source"] == "chest+finger"):
            hr_fallback = r["hr"]
    out = best or primary
    if not out["hr"]["ok"] and hr_fallback is not None:
        out["hr"] = hr_fallback
    out["axes_tried"] = [n for n, _ in axes]
    return out


# --------------------------------------------------------------------- display
_TREND_TEXT = {
    "rising":      ("Tren naik", "Tekanan darah cenderung naik dibanding awal episode."),
    "falling":     ("Tren turun", "Tekanan darah cenderung turun dibanding awal episode."),
    "stable":      ("Stabil", "Belum ada perubahan berarti dibanding awal episode."),
    "no_baseline": ("Belum ada acuan", "Lakukan kalibrasi dengan tensimeter dulu."),
}


_HR_SOURCE_TEXT = {
    "chest+finger": "Diukur dua sensor sekaligus, hasilnya cocok",
    "chest": "Diukur dari detak di dada",
    "finger": "Diukur dari denyut di jari",
}


def _hr_block(r: dict) -> dict:
    """Heart rate, ready to render. Present even when the trend is not.

    This is the number the app can almost always show. It is measured, not
    estimated, so unlike the trend it may be displayed as a plain figure.
    """
    hr = r.get("hr") or {}
    if not hr.get("ok"):
        return {"show": False, "bpm": None, "label": "Detak jantung",
                "detail": hr.get("reason") or "Tidak terbaca", "source": None}
    return {
        "show": True,
        "bpm": hr["bpm"],
        "label": "Detak jantung",
        "unit": "bpm",
        "source": hr["source"],
        "detail": _HR_SOURCE_TEXT.get(hr["source"], ""),
        # The one honest bragging right: two independent sensors agreeing is
        # something a single-sensor wearable cannot offer.
        "dual_confirmed": hr["source"] == "chest+finger",
        "beats_counted": hr.get("beats_counted"),
    }


def display_for(r: dict, referenced: bool = True) -> dict:
    """Everything the UI needs, already decided here.

    Four rules the app must not override:
      1. A rejected session shows NO trend number.
      2. A trend is never rendered in the visual language of a cuff reading.
      3. A trend from an UNREFERENCED baseline says so, every time. Otherwise
         a user reads "stable" as "my blood pressure is fine", when all it
         means is "unchanged from a starting point nobody measured".
      4. Heart rate is MEASURED, not estimated, so it may be shown as a plain
         number even when the trend is refused. `heart_rate.show` decides.
    """
    hr = _hr_block(r)
    if not r["ok"]:
        return {
            "headline": ("Detak jantung terbaca" if hr["show"]
                         else "Belum bisa dibaca"),
            "detail": (f"Tren tekanan darah belum bisa dihitung: {r['reason']}"
                       if hr["show"] else r["reason"]),
            "action": "Ulangi pengukuran",
            "show_value": False,
            "heart_rate": hr,
        }
    head, detail = _TREND_TEXT.get(r["trend"], ("Hasil", ""))
    if not referenced and r["trend"] != "no_baseline":
        detail += (" Titik awal Anda belum diukur dengan tensimeter, jadi ini "
                   "perubahan dari titik yang belum diketahui.")
    return {
        "headline": head,
        "detail": detail,
        "action": ("Konfirmasi dengan tensimeter" if r["trend"] == "rising"
                   else ("Kalibrasi dengan tensimeter saat ada kesempatan"
                         if not referenced else None)),
        "show_value": False,          # never an mmHg number from an estimate
        "referenced": referenced,
        "heart_rate": hr,
        "secondary": f"PTT {r['ptt_ms']} ms · {r['n_paired']} denyut",
        "disclaimer": "BUKAN DIAGNOSIS",
    }


def _refusal(reason: str, extra=None) -> dict:
    out = {"ok": False, "reason": reason, "timing": None,
           "display": {"headline": "Rekaman tidak valid", "detail": reason,
                       "action": "Ulangi pengukuran", "show_value": False}}
    if extra:
        out.update(extra)
    return out


# ------------------------------------------------------------------ endpoints
def session(cap: Capture) -> dict:
    """One interim check. A trend, or a refusal with a reason. Never a pressure."""
    bad = posture_mismatch(cap)
    if bad:
        return _refusal(bad, {"posture": cap.posture})
    try:
        fs_s, fs_p, timing = resolve_timing(cap)
    except RateError as e:
        return _refusal(str(e), {"posture": cap.posture})
    r = run_best_axis(cap, fs_s, fs_p, cap.anchor)
    r["timing"] = timing
    r["posture"] = cap.posture
    # An anchor with no cuff behind it still produces a valid trend, but the
    # wording has to change. Absent flag defaults to True so pre-existing
    # callers keep their old behaviour.
    referenced = True if not cap.anchor else cap.anchor.get("referenced", True)
    r["referenced"] = referenced
    r["display"] = display_for(r, referenced=referenced)
    return r


def start_baseline(cap: Capture, reported_sys: Optional[int] = None,
                   reported_dia: Optional[int] = None) -> dict:
    """Create an UNREFERENCED anchor, so a user with no cuff is not locked out.

    The population with undiagnosed hypertension is also the population least
    likely to own a tensimeter. Requiring a cuff before the app does anything
    excludes exactly the people early warning is for.

    So: record today, store this PTT as the baseline, and report change against
    it. That is honest and immediately useful ("your pulse timing has shifted
    since you started"). What it cannot say is where the person started from.

    reported_sys / reported_dia are a remembered reading, e.g. from a clinic
    visit last week. They are stored for TRIAGE ONLY and never enter an
    equation. A remembered number cannot calibrate anything, because the
    recording that would have paired with it does not exist; and a clinic
    reading is systematically higher than the home state we measure in, so
    using it would bias every later estimate in one direction. But it does
    tell us this person is probably hypertensive, which is worth knowing when
    deciding how hard to push them toward a cuff.

    Upgrade later with calibrate(), then reanchor() the stored history.
    """
    bad = posture_mismatch(cap)
    if bad:
        return _refusal(bad, {"anchor": None, "posture": cap.posture})
    try:
        fs_s, fs_p, timing = resolve_timing(cap)
    except RateError as e:
        return _refusal(str(e), {"anchor": None, "posture": cap.posture})

    r = run_best_axis(cap, fs_s, fs_p, None)
    if not r["ok"]:
        return {"ok": False, "reason": r["reason"], "anchor": None,
                "timing": timing, "posture": cap.posture,
                "display": {"headline": "Rekaman belum bisa dipakai",
                            "detail": r["reason"],
                            "action": "Ulangi pengukuran", "show_value": False}}

    triage = None
    if reported_sys and reported_dia:
        triage = ("elevated" if reported_sys >= 140 or reported_dia >= 90
                  else "normal")

    return {
        "ok": True, "timing": timing, "axis": r["axis"], "posture": cap.posture,
        "anchor": {
            "ptt_ms": r["ptt_ms"], "ptt_sd_ms": r["ptt_sd_ms"],
            "cuff_sys": None, "cuff_dia": None,
            "posture": cap.posture, "simultaneous": False,
            # The load-bearing flag. False means every trend from this anchor
            # is a change from an UNKNOWN starting point.
            "referenced": False,
            # Remembered, unverified, never used in arithmetic.
            "reported_sys": reported_sys, "reported_dia": reported_dia,
            "triage": triage,
        },
        "display": {
            "headline": "Titik awal tersimpan",
            "detail": "Tera akan melaporkan PERUBAHAN dari titik ini. "
                      "Tanpa tensimeter, Tera belum tahu tekanan darah awal "
                      "Anda.",
            "action": "Kalibrasi dengan tensimeter saat ada kesempatan"
                      + (" (riwayat Anda menunjukkan tekanan tinggi)"
                         if triage == "elevated" else ""),
            "show_value": False,
        },
    }


def reanchor(history_ptt_ms: List[float], new_anchor: dict,
             between_session_sd_ms: Optional[float] = None) -> dict:
    """Re-read stored history against a real anchor obtained later.

    The point of deferred calibration: nothing recorded before the cuff is
    wasted. Stored PTT values are just numbers, so once a referenced anchor
    exists every past session can be re-classified against it. Three weeks of
    checks become meaningful the moment the user reaches a clinic.
    """
    a = new_anchor.get("ptt_ms")
    out = []
    for p in history_ptt_ms:
        trend, delta = T.classify_trend(p, a,
                                        between_session_sd_ms=between_session_sd_ms)
        out.append({"ptt_ms": p, "trend": trend, "delta_ptt_ms": delta})
    return {
        "ok": True,
        "n_reinterpreted": len(out),
        "anchor_ptt_ms": a,
        "referenced": bool(new_anchor.get("cuff_sys")),
        "sessions": out,
    }


def calibrate(cap: Capture, cuff_sys: int, cuff_dia: int,
              cuff_age_s: Optional[float] = None) -> dict:
    """Pair one capture with one SIMULTANEOUS cuff reading to create the anchor.

    The valuable half of this call is the capture, not the two numbers. The
    anchor is its ptt_ms, and every later session is a comparison against that.
    cuff_sys and cuff_dia never enter an equation anywhere in this codebase;
    they are stored so a clinician can see what state the baseline was taken in.

    Which makes simultaneity the whole requirement. A cuff reading the user
    remembers from last week, attached to a recording made today, does not
    calibrate anything: it labels today's pulse timing with last week's blood
    pressure, and the difference between those two is exactly the signal we
    exist to detect. Better to refuse than to anchor to a number that was true
    at a moment we did not measure.

    cuff_age_s is seconds between the cuff reading and this capture. Send it.
    If it is absent we accept, because refusing on a missing field would break
    every existing caller, but the response says the claim is unverified.
    """
    bad = posture_mismatch(cap)
    if bad:
        return _refusal(bad, {"anchor": None, "posture": cap.posture})

    stale = None
    if cuff_age_s is not None:
        if cuff_age_s < 0:
            stale = "the cuff reading is timestamped after the recording"
        elif cuff_age_s > MAX_CUFF_AGE_S:
            stale = (f"the cuff reading is {cuff_age_s/60:.0f} minutes older "
                     f"than this recording. Calibration needs both measured "
                     f"together, within {MAX_CUFF_AGE_S/60:.0f} minutes. "
                     f"Take a cuff reading now and record again.")
    if stale:
        return _refusal(stale, {"anchor": None, "posture": cap.posture})

    try:
        fs_s, fs_p, timing = resolve_timing(cap)
    except RateError as e:
        return _refusal(str(e), {"anchor": None, "posture": cap.posture})

    r = run_best_axis(cap, fs_s, fs_p, None)
    if not r["ok"]:
        return {"ok": False, "reason": r["reason"], "anchor": None,
                "timing": timing, "posture": cap.posture,
                "display": {"headline": "Rekaman belum bisa dipakai",
                            "detail": r["reason"],
                            "action": "Ulangi pengukuran", "show_value": False}}
    return {
        "ok": True,
        "timing": timing,
        "axis": r["axis"],
        "posture": cap.posture,
        "anchor": {
            "ptt_ms": r["ptt_ms"], "ptt_sd_ms": r["ptt_sd_ms"],
            "cuff_sys": int(cuff_sys), "cuff_dia": int(cuff_dia),
            # Stamped so a later session cannot silently compare against an
            # anchor recorded in a different position.
            "posture": cap.posture,
            # False means nobody confirmed the cuff reading was taken with this
            # recording. The anchor still works; we just cannot claim it was
            # simultaneous, and a clinician reading the summary deserves to know.
            "simultaneous": cuff_age_s is not None,
            # This anchor has a real, paired pressure behind it.
            "referenced": True,
        },
        "display": {
            "headline": "Kalibrasi tersimpan",
            "detail": f"{cuff_sys}/{cuff_dia} mmHg dipasangkan dengan "
                      f"PTT {r['ptt_ms']} ms",
            "action": None if cuff_age_s is not None else
                      "Pastikan tensimeter diukur bersamaan dengan rekaman ini",
            "show_value": False,
        },
    }


def health() -> dict:
    return {"ok": True, "scipy": T._SCIPY, "version": "0.2.0"}
