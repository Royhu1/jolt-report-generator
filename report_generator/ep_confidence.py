"""
report_generator.ep_confidence
==============================
Per-row confidence grading for the reported Energy Performance (EP).

Why this module exists
---------------------
``Energy Performance (kWh/km)`` is a ratio of two independently-anchored
quantities — a battery/counter energy difference and an odometer difference — each
attributed to a trip window detected from the speed signal. When the telematics
counters are sampled sparsely relative to the trips, or the SOC channel is coarsely
quantised, the ratio can be badly wrong **while still looking like a plausible
number**. Three such mechanisms are documented on the JOLT fleet:

1. **Energy double counting on merge.** Adjacent discharge segments that fall
   inside the *same* sparse counter interval each take the whole interval's energy
   (both use ``_nearest_before(start)`` / ``_nearest_after(end)``, which resolve to
   the same pair of readings). ``merge_discharge_by_mass`` then sums the parts, so
   the merged row reports 2–4× the energy the counter actually measured
   (N88GNW, AV24LXJ). ``_enforce_anchor_ordering`` only repairs the *pairwise*
   overlap between two surviving neighbours, not an overlap consumed inside a merge.
2. **Attribution window wider than the trip.** A counter anchor can sit far outside
   the trip boundary, so the interval between the two anchors contains vehicle
   movement that did not belong to the trip — contaminating the energy, the
   distance, or both (the distance form is a stale odometer anchor: the nearest
   valid reading sits far outside the trip, so the distance spans the gap).
3. **Insufficient SOC resolution.** When a trip's energy comes from
   ``ΔSOC × capacity`` and ΔSOC spans only one or two quantisation steps of the SOC
   channel, the energy estimate carries a relative error of tens of percent
   (LN25NKE, whose SOC channel steps in 0.4 % and whose median trip spans ~6 steps).
4. **SOC discontinuity.** Separately from its resolution, the SOC signal can *step*:
   LN25NKE's worst rows come from a 12-percentage-point drop between two samples 25 s
   apart during a yard shunt, which turns 0.3 km of manoeuvring into 55 kWh and an EP
   of 176 kWh/km. The pack did not discharge; the signal re-initialised.

Mechanisms 1 and 2 are logic/data-coverage defects and can be fixed in the
pipeline; mechanisms 3 and 4 are properties of the source signal and cannot. Either way the
report must not present a number of unknown quality as if it were measured, so every
trip row carries a **grade** and the **evidence behind it**:

    ``EP Confidence``         → ``good`` | ``caution`` | ``poor``
    ``EP Confidence Reason``  → the triggered check codes with their measured values

Design
------
* **Provenance, not plausibility, drives the grade.** Each check names a *mechanism*
  by which the number could be wrong and measures how strongly it applies. A single
  outcome-plausibility backstop (:data:`CODE_EP_RANGE`) exists for the
  "wrong for a reason we did not model" case, and is deliberately the weakest check.
* **The grade is the worst check.** Confidence is a floor, not an average: one
  decisive defect is not offset by other checks passing.
* **Grade ≠ provenance.** A well-resolved SOC-derived energy can be ``good``. The
  ``Energy Source`` column already records provenance, so the grade would double-
  penalise it. What this column measures is *resolution and internal consistency*.
* **One rule engine, evaluated twice.** :func:`assess_ep_confidence` is the only
  place a grade is decided. ``_seg_to_row`` calls it while building the row, so any
  direct caller of the row builder gets a grade; :func:`regrade_rows` calls it again
  from ``_finalize_rows`` once the effective-capacity post-processing has settled
  the final energy source (it can rewrite a counter energy to ``soc_fallback``,
  changing which checks apply) and an independent reference capacity exists — and
  that pass wins. Thresholds live in this module's constants, so a single edit
  re-grades the whole fleet and shows up as one reviewable diff.

Reading the diagnostics requires the counter anchors, which do not survive into the
row tuple, so the measurement step runs in the segmentation layer
(:func:`attach_ep_audits`, called from ``run_segment_detection`` after the anchors
are final) and travels on the segment dict under the public ``ep_audit`` key.
"""

from __future__ import annotations

import logging
from typing import TypeGuard

import numpy as np
import pandas as pd

# ``columns`` is the only package-internal import, and it imports nothing
# package-internal itself, so this module can be depended on from either the
# segmentation layer or the report-builder layer without a cycle. Raw-telematics
# column names are supplied by the caller rather than imported, so there is no
# second copy of a column-name constant to drift.
from .columns import _row_col_index, is_trip_leg

logger = logging.getLogger(__name__)

# ── Grades ──────────────────────────────────────────────────────────────────
# Lower-case, matching the existing enum-valued report columns (``Energy Source``).
CONF_GOOD = "good"
CONF_CAUTION = "caution"
CONF_POOR = "poor"

# Ordered worst-last so ``max`` over severities picks the grade.
_GRADES = (CONF_GOOD, CONF_CAUTION, CONF_POOR)
SEV_GOOD, SEV_CAUTION, SEV_POOR = 0, 1, 2

# ── Check codes (written into ``EP Confidence Reason``) ─────────────────────
CODE_DUP_ENERGY = "DUP_ENERGY"  # reported energy exceeds what the counter measured
CODE_SPLIT_ALLOC = "SPLIT_ALLOC"  # energy is a modelled share of a counter interval
CODE_ENERGY_WINDOW = "ENERGY_WINDOW"  # energy window contains out-of-trip movement
CODE_IDLE_WINDOW = "IDLE_WINDOW"  # energy window contains out-of-trip parked time
CODE_DIST_WINDOW = "DIST_WINDOW"  # distance window contains out-of-trip movement
CODE_DIST_EXTRAP = "DIST_EXTRAP"  # no odometer sample inside the trip window
CODE_SOC_RES = "SOC_RES"  # SOC-derived energy spans too few SOC steps
CODE_SOC_STEP = "SOC_STEP"  # the SOC change is one implausible discontinuity
CODE_CAP_INCONS = "CAP_INCONS"  # counter energy and ΔSOC×capacity disagree
CODE_SHORT_DIST = "SHORT_DIST"  # trip too short for EP to mean anything
CODE_SPEED = "SPEED"  # elapsed average speed physically impossible
CODE_EP_RANGE = "EP_RANGE"  # EP outside the plausible band (backstop)

# Reporting order within one severity: the most decisive, most specific mechanism
# first, so the reason string leads with the finding a reader should act on and ends
# with the "something is off but we did not model why" backstop. Without this the
# codes would come out alphabetically and a row could open with EP_RANGE while the
# actual diagnosis sat at the end.
_CODE_ORDER = (
    CODE_DUP_ENERGY,
    CODE_SOC_STEP,
    CODE_DIST_EXTRAP,
    CODE_ENERGY_WINDOW,
    CODE_IDLE_WINDOW,
    CODE_DIST_WINDOW,
    CODE_SOC_RES,
    CODE_CAP_INCONS,
    CODE_SPLIT_ALLOC,
    CODE_SHORT_DIST,
    CODE_SPEED,
    CODE_EP_RANGE,
)

# ── Thresholds ──────────────────────────────────────────────────────────────
# Calibrated against a fleet-wide replay of the 3.3.0 report database; the
# calibration evidence is recorded in the versions.md entry for this release.

# (1) Double counting. ``dup_ratio`` = |reported energy| / counter energy over the
# same anchor span. Exactly 1.0 when the row's energy IS the counter difference;
# ≈ 2/3/4 when that many merged parts each claimed the same sparse interval.
# The caution band starts just above the rounding noise of the anchor arithmetic.
DUP_RATIO_CAUTION = 1.05
DUP_RATIO_POOR = 1.30

# (2) Split allocation. Below 1.0 the row holds a *share* of a counter interval,
# apportioned by ΔSOC when ``split_discharge_by_mass`` cut a trip: a model, not a
# measurement. Only a substantial shortfall is worth flagging — small deviations are
# the ordinary consequence of anchors sitting slightly outside the trip.
SPLIT_RATIO_CAUTION = 0.80

# (3)/(4) Attribution-window contamination: the share of a reported quantity that
# was accumulated inside the counter's anchor window but outside the trip window,
# so it never belonged to this trip. Both the driving and the parked flavour are
# expressed on the same scale — a fraction of the row's own reported value — so one
# threshold pair reads the same way for either. 0.10 is where the contamination
# exceeds EP's own useful precision; beyond 0.30 the number is substantially
# somebody else's journey.
WINDOW_OUTSIDE_FRAC_CAUTION = 0.10
WINDOW_OUTSIDE_FRAC_POOR = 0.30

# Auxiliary power (kW) assumed when converting out-of-trip *parked* time inside the
# energy window into the energy it contributed. An order-of-magnitude figure for a
# battery HGV's standing load (HVAC, cab and low-voltage systems) — used only to
# decide whether a long anchor overshoot is material, never to alter a reported
# energy. Deliberately on the low side, so this check under-claims rather than
# over-claims.
AUX_POWER_KW = 3.0

# (5) SOC resolution: one quantisation step as a fraction of the trip's |ΔSOC|.
# 0.10 ≈ ±10 % on the energy; 0.25 means ΔSOC spans only four steps or fewer.
SOC_RES_CAUTION = 0.10
SOC_RES_POOR = 0.25

# (5b) SOC discontinuity. A trip's SOC change is only energy if the SOC actually
# fell as the vehicle drove. A single sample-to-sample step far faster than any
# real discharge is the signal re-initialising (a stale reading catching up, or the
# BMS re-estimating), and when the trip's whole SOC change rests on that one step,
# the derived energy is an artefact of the signal rather than a measurement.
# Expressed as a rate so it is independent of pack size: the fastest genuine
# discharge on this fleet is around 90 %/h (a ~400 kW draw on a ~460 kWh pack), so
# 300 %/h is comfortably beyond anything physical while staying far from real data.
SOC_STEP_RATE_MAX_PCT_PER_H = 300.0
# …and only when the step actually carries the trip's SOC change. Requiring a
# substantial share as well means a transient spike that cancels out — which never
# reaches the reported ΔSOC anyway — cannot raise a finding on a sound row.
SOC_STEP_SHARE_CAUTION = 0.25
SOC_STEP_SHARE_POOR = 0.50

# (6) Counter-vs-SOC cross-check: relative gap between the energy the counter
# reports and ΔSOC × reference capacity. Only meaningful when ΔSOC is a fair
# referee, otherwise the gap says more about the SOC channel than about the
# counter — so the check is gated twice: the change must be at least
# ``CAP_CROSSCHECK_MIN_DSOC_PCT`` in absolute terms (the same 10 % judgement the
# capacity module's SOC-energy fallback already makes, for the same reason: it
# bounds the quantisation error to roughly ±5 % on a 1 % channel), and, where the
# channel's quantum is known, must also clear ``SOC_RES_CAUTION``. The absolute
# gate is what lets the cross-check run on a row whose diagnostics were never
# measured; the quantum gate is what stops it running on a coarse channel.
CAP_CROSSCHECK_MIN_DSOC_PCT = 10.0
CAP_DEV_CAUTION = 0.30
CAP_DEV_POOR = 0.60

# (7) Distance floor. Over a very short trip, EP is dominated by auxiliary load and
# by the fixed absolute error of the anchoring, and stops describing traction
# energy at all. In the 3.3.0 replay, 95 % of sub-kilometre trips already exceed
# 3 kWh/km — a rate no fleet-wide mechanism explains.
SHORT_DIST_CAUTION_KM = 3.0
SHORT_DIST_POOR_KM = 1.0

# (8) Elapsed average speed. A UK HGV is limiter-bound near 90 km/h, so a higher
# elapsed average means the distance or the window is wrong, not that the vehicle
# was fast.
SPEED_CAUTION_KMH = 95.0
SPEED_POOR_KMH = 105.0

# (9) Plausibility backstop for a 40 t class battery HGV, in kWh/km.
EP_PLAUSIBLE_CAUTION = (0.15, 4.0)
EP_PLAUSIBLE_POOR = (0.08, 8.0)

# Energy sources whose energy is ΔSOC × capacity rather than a counter difference.
_SOC_DERIVED_SOURCES = frozenset({"soc_estimate", "soc_fallback"})
_COUNTER_SOURCES = frozenset({"total_energy", "moving_energy"})

# Minimum number of non-zero SOC steps a leg must show before its quantum estimate
# is trusted; below this the SOC-resolution check is skipped rather than guessed.
_MIN_SOC_DIFFS = 20
# Reciprocal of the resolution the SOC grid is searched on (1/0.001 %), which also
# absorbs the float noise of a CSV round-trip.
_SOC_GRID_SCALE = 1000
# Ceiling on a believable SOC quantum (%). Above this the estimate is treated as
# unknown rather than acted on: a spuriously large quantum would inflate every
# SOC-resolution finding on that leg.
_SOC_QUANTUM_MAX_PCT = 5.0


def _finite(v) -> TypeGuard[float]:
    """True if ``v`` is a real, finite number.

    Declared as a ``TypeGuard`` so that a guarded ``float(...)`` on an audit value
    (typed ``Any | None`` coming out of a dict) type-checks without a cast at every
    call site.
    """
    if v is None:
        return False
    try:
        return bool(np.isfinite(float(v)))
    except (TypeError, ValueError):
        return False


# =============================================================================
# Measurement — runs in the segmentation layer, where the counter anchors live
# =============================================================================
def soc_quantum_pct(soc: pd.Series) -> float:
    """Estimate the SOC channel's quantisation step (%) from one leg's samples.

    A quantised channel only ever reports values on a fixed grid, so every
    observed change is a whole number of grid steps and the **greatest common
    divisor of the changes** is the step itself — exactly, and independently of how
    densely the leg happens to be sampled. (A percentile or modal estimate needs a
    single-step change to actually appear in the sample, which a sparsely-sampled
    leg may never show; it then over-estimates the step and would inflate every
    SOC-resolution finding on that leg.) The gcd is taken over the differences
    rather than the values so a grid offset cannot distort it.

    Returns NaN when the leg carries too few transitions to tell, or when the
    result is not a believable quantum — the caller then skips the SOC-resolution
    check rather than acting on a guess. A single off-grid reading can only drag the
    estimate *down*, which makes the check more lenient, never falsely harsh.
    """
    v = pd.to_numeric(soc, errors="coerce").dropna()
    if len(v) < 2:
        return float("nan")
    d = v.diff().abs()
    d = d[d > 1e-9]
    if len(d) < _MIN_SOC_DIFFS:
        return float("nan")
    steps = np.rint(d.to_numpy(dtype=float) * _SOC_GRID_SCALE).astype(np.int64)
    steps = steps[steps > 0]
    if steps.size == 0:
        return float("nan")
    quantum = float(np.gcd.reduce(steps)) / _SOC_GRID_SCALE
    if not (0 < quantum <= _SOC_QUANTUM_MAX_PCT):
        return float("nan")
    return quantum


def attach_ep_audits(
    discharge_segs: list[dict],
    df_raw: pd.DataFrame,
    total_energy_col: str,
    moving_energy_col: str,
    *,
    soc_col: str,
    odo_col: str,
    time_col: str,
) -> None:
    """Measure each discharge segment's EP-confidence diagnostics, in place.

    Writes a public ``ep_audit`` dict onto every segment (so it survives the
    ``_ANCHOR_PRIVATE_KEYS`` filter and reaches the row builder). Must run on the
    **final** discharge segments — after ``split_discharge_by_mass`` /
    ``merge_discharge_by_mass`` / ``_recompute_anchors`` / ``_enforce_anchor_ordering``
    — because every quantity is measured against the anchors those steps settle.

    Purely observational: it never modifies a segment's energy, distance or anchors.
    """
    if not discharge_segs:
        return

    df = df_raw[
        [
            c
            for c in (time_col, soc_col, odo_col, total_energy_col, moving_energy_col)
            if c in df_raw.columns
        ]
    ].copy()
    if time_col not in df.columns:
        for seg in discharge_segs:
            seg["ep_audit"] = {}
        return
    df[time_col] = pd.to_datetime(df[time_col], errors="coerce", utc=True)
    df = df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)
    if df.empty:
        for seg in discharge_segs:
            seg["ep_audit"] = {}
        return

    t_ns = _ns_view(df[time_col])

    soc_q = float("nan")
    soc_t = np.array([], dtype="int64")
    soc_v = np.array([], dtype=float)
    if soc_col in df.columns:
        soc_ser = pd.to_numeric(df[soc_col], errors="coerce")
        soc_ser = soc_ser.where(soc_ser != 0)
        soc_q = soc_quantum_pct(soc_ser)
        keep = soc_ser.notna().to_numpy()
        soc_t, soc_v = t_ns[keep], soc_ser[keep].to_numpy(dtype=float)

    # Odometer trace (km), valid samples only — the single source for every
    # "did the vehicle move outside the window" measurement.
    if odo_col in df.columns:
        odo = pd.to_numeric(df[odo_col], errors="coerce")
        keep = odo.notna().to_numpy()
        odo_t, odo_v = t_ns[keep], odo[keep].to_numpy(dtype=float)
    else:
        odo_t = np.array([], dtype="int64")
        odo_v = np.array([], dtype=float)

    def _odo_at(ts) -> float:
        """Odometer reading (km) at an arbitrary instant, by linear interpolation."""
        if len(odo_t) == 0:
            return float("nan")
        x = _to_ns(ts)
        if x is None:
            return float("nan")
        return float(np.interp(x, odo_t, odo_v))

    # Counter sample spacing, for context in the audit (not itself a check).
    gaps: dict[str, float] = {}
    for tag, col in (
        ("total_energy", total_energy_col),
        ("moving_energy", moving_energy_col),
    ):
        if col in df.columns:
            m = pd.to_numeric(df[col], errors="coerce").notna().to_numpy()
            ct = t_ns[m]
            gaps[tag] = (
                float(np.median(np.diff(ct)) / 6e10) if len(ct) > 2 else float("nan")
            )
        else:
            gaps[tag] = float("nan")

    for seg in discharge_segs:
        seg["ep_audit"] = _audit_one(
            seg, t_ns, _odo_at, odo_t, soc_q, gaps, soc_t, soc_v
        )


# ── The nanosecond invariant ────────────────────────────────────────────────
# Every instant in this module is an int64 count of UTC nanoseconds, and the two
# functions below are the only places one is produced. They must agree, because
# the measurements compare them directly: a sample trace from :func:`_ns_view`
# against a segment boundary from :func:`_to_ns`.
#
# The trap they exist to close: ``Timestamp.value`` is *always* nanoseconds, but a
# datetime Series' int64 view is whatever unit the Series carries, and since
# pandas 3 ``pd.to_datetime(<ISO strings>, utc=True)`` infers ``datetime64[us]``
# rather than ``datetime64[ns]``. A raw ``.astype("int64")`` therefore yields
# microseconds on pandas 3 and nanoseconds on pandas 2 — the same code silently
# measuring a 1000x-too-small window on one and correctly on the other, with no
# error to notice, which is exactly how ENERGY_WINDOW / DIST_WINDOW / DIST_EXTRAP
# / SOC_STEP came to be dead checks. Normalising the unit first is what keeps the
# two commensurable on both.


def _ns_view(s: pd.Series) -> np.ndarray:
    """int64 UTC-nanosecond view of a datetime Series, whatever unit it carries.

    See the note above: ``.as_unit("ns")`` is the load-bearing call, not
    decoration. Works for tz-aware and tz-naive series alike (a naive series is
    read as UTC, which is what the raw telematics frames are).
    """
    return pd.DatetimeIndex(s).as_unit("ns").asi8


def _to_ns(ts) -> int | None:
    """Normalise a timestamp to UTC nanoseconds since the epoch."""
    if ts is None:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.value)


def _audit_one(
    seg, t_ns, odo_at, odo_t, soc_q: float, gaps: dict, soc_t, soc_v
) -> dict:
    """Measure one segment's diagnostics (helper of :func:`attach_ep_audits`)."""
    trip_s, trip_e = _to_ns(seg.get("start_time")), _to_ns(seg.get("end_time"))
    src = seg.get("energy_source")
    delta_e = seg.get("delta_energy_kwh")

    audit: dict = {
        "energy_source": src,
        "soc_quantum_pct": soc_q,
        "counter_gap_min": gaps.get(src, float("nan")),
    }

    # ── Energy: reported vs what the counter measured over the same span ──────
    a_s, a_e = seg.get("_anchor_start_rel_kwh"), seg.get("_anchor_end_rel_kwh")
    counter_kwh = float("nan")
    if _finite(a_s) and _finite(a_e):
        counter_kwh = float(a_e) - float(a_s)
    audit["counter_kwh"] = counter_kwh
    audit["dup_ratio"] = (
        abs(float(delta_e)) / counter_kwh
        if (_finite(delta_e) and counter_kwh > 1e-9)
        else float("nan")
    )

    # ── Attribution windows: movement inside the anchor span, outside the trip ──
    a_st, a_et = _to_ns(seg.get("_anchor_start_time")), _to_ns(
        seg.get("_anchor_end_time")
    )
    audit["energy_outside_km"] = _outside_km(odo_at, trip_s, trip_e, a_st, a_et)
    audit["energy_outside_min"] = _outside_min(trip_s, trip_e, a_st, a_et)

    # The odometer has its own anchors: the row's distance is the difference
    # between the nearest valid readings around the trip, so the same overshoot
    # inflates the distance. Recover those anchor instants from the odometer trace.
    o_st = _nearest(odo_t, trip_s, "before")
    o_et = _nearest(odo_t, trip_e, "after")
    audit["dist_outside_km"] = _outside_km(odo_at, trip_s, trip_e, o_st, o_et)
    audit["odo_samples_in_trip"] = (
        int(np.count_nonzero((odo_t >= trip_s) & (odo_t <= trip_e)))
        if (len(odo_t) and trip_s is not None and trip_e is not None)
        else 0
    )

    # ── SOC: the largest single-sample step inside the trip window ────────────
    step_pct, step_rate = _largest_soc_step(soc_t, soc_v, trip_s, trip_e)
    audit["soc_step_pct"] = step_pct
    audit["soc_step_rate_pct_h"] = step_rate

    # Number of raw samples in the trip window — context for a reader judging a
    # short or sparsely-sampled row.
    audit["samples_in_trip"] = (
        int(np.count_nonzero((t_ns >= trip_s) & (t_ns <= trip_e)))
        if (trip_s is not None and trip_e is not None)
        else 0
    )
    return audit


def _largest_soc_step(soc_t, soc_v, trip_s, trip_e) -> tuple[float, float]:
    """Largest single-sample SOC drop in the trip window, and its rate.

    Returns ``(step_pct, rate_pct_per_h)`` for the fastest drop between two
    consecutive valid SOC samples inside the window — the candidate discontinuity.
    Only drops count: a rise is charging or a recovering sensor, neither of which
    inflates a discharge energy.
    """
    if len(soc_t) < 2 or trip_s is None or trip_e is None:
        return float("nan"), float("nan")
    m = (soc_t >= trip_s) & (soc_t <= trip_e)
    if np.count_nonzero(m) < 2:
        return float("nan"), float("nan")
    t_win, v_win = soc_t[m], soc_v[m]
    drop = -np.diff(v_win)  # positive where the SOC fell
    dt_h = np.diff(t_win) / 3.6e12
    ok = (drop > 0) & (dt_h > 0)
    if not np.any(ok):
        return float("nan"), float("nan")
    rate = drop[ok] / dt_h[ok]
    i = int(np.argmax(rate))
    return float(drop[ok][i]), float(rate[i])


def _nearest(times: np.ndarray, t, direction: str) -> int | None:
    """Nearest sample instant at or before / at or after ``t`` (ns), or None."""
    if len(times) == 0 or t is None:
        return None
    if direction == "before":
        idx = np.searchsorted(times, t, side="right") - 1
        return int(times[idx]) if idx >= 0 else None
    idx = np.searchsorted(times, t, side="left")
    return int(times[idx]) if idx < len(times) else None


def _outside_min(trip_s, trip_e, win_s, win_e) -> float:
    """Minutes of [win_s, win_e] lying outside [trip_s, trip_e]."""
    if None in (trip_s, trip_e, win_s, win_e):
        return float("nan")
    pre = max(0, trip_s - win_s)
    post = max(0, win_e - trip_e)
    return (pre + post) / 6e10


def _outside_km(odo_at, trip_s, trip_e, win_s, win_e) -> float:
    """Distance covered inside [win_s, win_e] but outside [trip_s, trip_e] (km)."""
    if None in (trip_s, trip_e, win_s, win_e):
        return float("nan")
    o_ts, o_te = odo_at(pd.Timestamp(trip_s, tz="UTC")), odo_at(
        pd.Timestamp(trip_e, tz="UTC")
    )
    o_ws, o_we = odo_at(pd.Timestamp(win_s, tz="UTC")), odo_at(
        pd.Timestamp(win_e, tz="UTC")
    )
    if not all(_finite(v) for v in (o_ts, o_te, o_ws, o_we)):
        return float("nan")
    pre = max(0.0, o_ts - o_ws) if win_s < trip_s else 0.0
    post = max(0.0, o_we - o_te) if win_e > trip_e else 0.0
    return pre + post


# =============================================================================
# Grading — the single rule engine
# =============================================================================
def assess_ep_confidence(
    audit: dict | None,
    *,
    energy_source,
    delta_soc_pct,
    distance_km,
    duration_h,
    energy_kwh,
    ep_kwh_km,
    capacity_ref_kwh=None,
) -> tuple[str | None, str | None]:
    """Grade one trip row's EP and explain the grade.

    Returns ``(grade, reason)`` where grade ∈ {``good``, ``caution``, ``poor``} and
    reason is a ``"CODE=value; …"`` string listing every triggered check, worst
    first. ``(None, None)`` means there is nothing to grade — a row without an EP
    value (a charge or Stop row, or a trip with no usable distance): a grade there
    would imply a judgement about a number that was never reported.

    Parameters
    ----------
    audit : the segment's ``ep_audit`` dict, or None when unavailable (the checks
        that need it are then skipped — a missing audit never invents a downgrade).
    capacity_ref_kwh : an *independent* reference capacity for the counter-vs-SOC
        cross-check, i.e. the corrected/effective capacity rather than this row's
        own implied capacity. Pass None to skip that check (the row builder does,
        since before the capacity correction the only reference available is the
        row's own implied value, which would compare a number with itself).
    """
    if not _finite(ep_kwh_km) or not _finite(distance_km) or float(distance_km) <= 0:
        return None, None

    audit = audit or {}
    ep = float(ep_kwh_km)
    dist = float(distance_km)
    dsoc = abs(float(delta_soc_pct)) if _finite(delta_soc_pct) else float("nan")
    src = energy_source if isinstance(energy_source, str) else ""
    # Findings: (severity, code, formatted measured value)
    found: list[tuple[int, str, str]] = []

    def add(sev: int, code: str, value: str) -> None:
        if sev > SEV_GOOD:
            found.append((sev, code, value))

    soc_q = audit.get("soc_quantum_pct", float("nan"))
    soc_res = (
        float(soc_q) / dsoc
        if (_finite(soc_q) and _finite(dsoc) and dsoc > 0)
        else float("nan")
    )

    # ── (1)/(2) Energy provenance against the counter it came from ────────────
    dup = audit.get("dup_ratio", float("nan"))
    if src in _COUNTER_SOURCES and _finite(dup):
        dup = float(dup)
        if dup > DUP_RATIO_POOR:
            add(SEV_POOR, CODE_DUP_ENERGY, f"{dup:.2f}")
        elif dup > DUP_RATIO_CAUTION:
            add(SEV_CAUTION, CODE_DUP_ENERGY, f"{dup:.2f}")
        elif dup < SPLIT_RATIO_CAUTION:
            add(SEV_CAUTION, CODE_SPLIT_ALLOC, f"{dup:.2f}")

    # ── (3)/(4) Attribution windows wider than the trip ───────────────────────
    if src in _COUNTER_SOURCES:
        # Driving done outside the trip window: its energy is in the counter
        # difference, so the share of the trip's own distance it represents is
        # also, to first order, the share of the energy that is not the trip's.
        add(*_window_finding(audit.get("energy_outside_km"), dist, CODE_ENERGY_WINDOW))
        # Standing still outside the trip window still draws auxiliary power, and a
        # multi-hour overshoot on a modest trip is material even at zero distance —
        # a case the distance-based check above cannot see.
        out_min = audit.get("energy_outside_min")
        if _finite(out_min) and _finite(energy_kwh) and abs(float(energy_kwh)) > 1e-9:
            idle_kwh = float(out_min) / 60.0 * AUX_POWER_KW
            add(*_window_finding(idle_kwh, abs(float(energy_kwh)), CODE_IDLE_WINDOW))
    add(*_window_finding(audit.get("dist_outside_km"), dist, CODE_DIST_WINDOW))
    if audit.get("odo_samples_in_trip") == 0 and audit.get("samples_in_trip", 0) > 0:
        # The reported distance is entirely extrapolated from readings outside the
        # trip window — the stale-odometer-anchor failure mode.
        add(SEV_POOR, CODE_DIST_EXTRAP, "0")

    # ── (5) SOC resolution, for energies that are ΔSOC × capacity ─────────────
    if src in _SOC_DERIVED_SOURCES and _finite(soc_res):
        if soc_res > SOC_RES_POOR:
            add(SEV_POOR, CODE_SOC_RES, f"{soc_res:.2f}")
        elif soc_res > SOC_RES_CAUTION:
            add(SEV_CAUTION, CODE_SOC_RES, f"{soc_res:.2f}")

    # ── (5b) A single implausible SOC discontinuity carrying the trip ─────────
    step = audit.get("soc_step_pct")
    rate = audit.get("soc_step_rate_pct_h")
    if (
        src in _SOC_DERIVED_SOURCES
        and _finite(step)
        and _finite(rate)
        and float(rate) > SOC_STEP_RATE_MAX_PCT_PER_H
        and _finite(dsoc)
        and dsoc > 0
    ):
        share = float(step) / dsoc
        if share >= SOC_STEP_SHARE_POOR:
            add(SEV_POOR, CODE_SOC_STEP, f"{share:.2f}")
        elif share >= SOC_STEP_SHARE_CAUTION:
            add(SEV_CAUTION, CODE_SOC_STEP, f"{share:.2f}")

    # ── (6) Counter energy vs ΔSOC × capacity: two paths to the same number ───
    if (
        src in _COUNTER_SOURCES
        and _finite(capacity_ref_kwh)
        and float(capacity_ref_kwh) > 0
        and _finite(energy_kwh)
        and _finite(dsoc)
        and dsoc >= CAP_CROSSCHECK_MIN_DSOC_PCT
        and (not _finite(soc_res) or soc_res <= SOC_RES_CAUTION)
    ):
        soc_energy = dsoc / 100.0 * float(capacity_ref_kwh)
        dev = abs(abs(float(energy_kwh)) - soc_energy) / soc_energy
        if dev > CAP_DEV_POOR:
            add(SEV_POOR, CODE_CAP_INCONS, f"{dev:.2f}")
        elif dev > CAP_DEV_CAUTION:
            add(SEV_CAUTION, CODE_CAP_INCONS, f"{dev:.2f}")

    # ── (7) Distance floor ────────────────────────────────────────────────────
    if dist < SHORT_DIST_POOR_KM:
        add(SEV_POOR, CODE_SHORT_DIST, f"{dist:.2f}")
    elif dist < SHORT_DIST_CAUTION_KM:
        add(SEV_CAUTION, CODE_SHORT_DIST, f"{dist:.2f}")

    # ── (8) Elapsed average speed ─────────────────────────────────────────────
    if _finite(duration_h) and float(duration_h) > 0:
        v = dist / float(duration_h)
        if v > SPEED_POOR_KMH:
            add(SEV_POOR, CODE_SPEED, f"{v:.0f}")
        elif v > SPEED_CAUTION_KMH:
            add(SEV_CAUTION, CODE_SPEED, f"{v:.0f}")

    # ── (9) Plausibility backstop ─────────────────────────────────────────────
    if not (EP_PLAUSIBLE_POOR[0] <= ep <= EP_PLAUSIBLE_POOR[1]):
        add(SEV_POOR, CODE_EP_RANGE, f"{ep:.2f}")
    elif not (EP_PLAUSIBLE_CAUTION[0] <= ep <= EP_PLAUSIBLE_CAUTION[1]):
        add(SEV_CAUTION, CODE_EP_RANGE, f"{ep:.2f}")

    if not found:
        return CONF_GOOD, None
    found.sort(key=lambda f: (-f[0], _CODE_ORDER.index(f[1])))
    grade = _GRADES[max(f[0] for f in found)]
    return grade, "; ".join(f"{code}={value}" for _sev, code, value in found)


def audit_key(ts) -> str | None:
    """Canonical key for matching a segment's audit to its finished report row.

    Both sides carry the segment's start instant, but one may be tz-naive (the
    detectors) and the other a tz-aware ``pd.Timestamp`` (the row), so compare
    UTC-normalised ISO strings rather than the raw objects.
    """
    if ts is None:
        return None
    try:
        t = pd.Timestamp(ts)
    except (TypeError, ValueError):
        return None
    if t is pd.NaT or pd.isna(t):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat()


def regrade_rows(
    rows: list,
    headers: tuple,
    *,
    audit_by_start: dict | None = None,
    capacity_ref_kwh: float | None = None,
) -> dict[str, int]:
    """Re-grade every row's EP confidence in place; return the grade tally.

    This is the **authoritative** grading pass and is meant to run last, once the
    rows are final. Between the row builder's first pass and this one, the
    effective-capacity post-processing can re-derive a row's energy from
    ``ΔSOC × capacity`` and relabel its ``Energy Source`` to ``soc_fallback`` —
    which switches which checks apply — and only here is an independent reference
    capacity available for the counter-versus-SOC cross-check.

    Rows that are not trip rows, and trip rows without an EP value, are set blank:
    grading a number the report does not state would be misleading.

    Parameters
    ----------
    rows : the row lists (mutated in place), each in ``headers`` order minus the
        leading 'Leg Number'.
    audit_by_start : ``{audit_key(segment start) → ep_audit}``; rows with no
        entry are graded on row values alone (audit-dependent checks skipped).
    capacity_ref_kwh : the report period's effective capacity, used as the
        independent referee for the counter-versus-SOC cross-check.
    """
    if "EP Confidence" not in headers:
        return {}
    i_conf = _row_col_index("EP Confidence", headers)
    i_reason = _row_col_index("EP Confidence Reason", headers)
    i_type = _row_col_index("Leg Type", headers)
    i_start = _row_col_index("Start Time (UTC)", headers)
    i_src = _row_col_index("Energy Source", headers)
    i_soc = _row_col_index("SOC Change (%)", headers)
    i_dist = _row_col_index("Distance (km)", headers)
    i_dur = _row_col_index("Duration (HH:MM:SS)", headers)
    i_energy = _row_col_index("Energy Change (kWh)", headers)
    i_ep = _row_col_index("Energy Performance (kWh/km)", headers)

    audit_by_start = audit_by_start or {}
    tally = {CONF_GOOD: 0, CONF_CAUTION: 0, CONF_POOR: 0, "ungraded": 0}
    n_failed = 0
    for row in rows:
        if not is_trip_leg(row[i_type]):
            row[i_conf] = None
            row[i_reason] = None
            tally["ungraded"] += 1
            continue
        dur_days = row[i_dur]
        # One unreadable row costs its own two cells, not the pass: the report is
        # already built at this point, and a grade that cannot be formed is
        # better left blank than allowed to discard every other row's grade.
        try:
            grade, reason = assess_ep_confidence(
                audit_by_start.get(audit_key(row[i_start])),
                energy_source=row[i_src],
                delta_soc_pct=row[i_soc],
                distance_km=row[i_dist],
                duration_h=(
                    float(dur_days) * 24.0 if _finite(dur_days) else float("nan")
                ),
                energy_kwh=row[i_energy],
                ep_kwh_km=row[i_ep],
                capacity_ref_kwh=capacity_ref_kwh,
            )
        except Exception:
            n_failed += 1
            grade, reason = None, None
            logger.warning(
                "EP-confidence grading failed for the row starting %s; "
                "leaving the grade blank",
                row[i_start],
                exc_info=True,
            )
        row[i_conf] = grade
        row[i_reason] = reason
        tally[grade if grade in tally else "ungraded"] += 1
    logger.info(
        "EP confidence: %d good, %d caution, %d poor (%d rows not graded%s)",
        tally[CONF_GOOD],
        tally[CONF_CAUTION],
        tally[CONF_POOR],
        tally["ungraded"],
        f", {n_failed} of them after an error" if n_failed else "",
    )
    return tally


def _window_finding(outside, reported: float, code: str) -> tuple[int, str, str]:
    """Grade one attribution window's contamination as a share of the reported value.

    ``outside`` and ``reported`` must be in the same unit — km against the trip's
    distance, or kWh against the trip's energy — so a single threshold pair applies
    to every flavour of the same mechanism.
    """
    if not _finite(outside) or reported <= 0:
        return SEV_GOOD, code, ""
    frac = float(outside) / reported
    if frac > WINDOW_OUTSIDE_FRAC_POOR:
        return SEV_POOR, code, f"{frac:.2f}"
    if frac > WINDOW_OUTSIDE_FRAC_CAUTION:
        return SEV_CAUTION, code, f"{frac:.2f}"
    return SEV_GOOD, code, ""
