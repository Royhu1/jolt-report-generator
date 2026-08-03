"""
report_generator.capacity
==========================
Effective battery-capacity post-processing, extracted from ``_generator``. Owns
the row-tuple column-index bookkeeping (``_row_idx`` / ``_IDX_*``), the
ΔSOC-weighted donor-capacity estimator (:func:`_soc_weighted_cap`), the
per-period donor capacity (:func:`_period_capacity_from_rows`), the quarterly
weighted-average schema (:func:`_recompute_weighted_capacity`), the time-local
±1σ capacity correction (:func:`_correct_effective_capacity`) and the
``vehicles.json`` capacity-ledger write-back
(:func:`_persist_effective_capacity`).

The two big functions were ``@staticmethod``s on ``JOLTReportGenerator``; they
are re-exposed there (``JOLTReportGenerator._correct_effective_capacity`` /
``_persist_effective_capacity``) by ``_generator`` so existing call sites keep
working (the cached-recompute tool
``.claude/skills/generate-excel-report/tools/recompute_from_cache.py`` and
:mod:`jolt_toolkit.report_generator.capacity_backfill`).
"""

import logging

import numpy as np
import pandas as pd

from jolt_toolkit.report_generator.report_builder import HEADERS
from jolt_toolkit.report_generator.segment_algorithms import VEHICLE_CONFIG

logger = logging.getLogger(__name__)


# ── row-tuple column indices (derived dynamically from HEADERS, excluding the leading Leg Number column) ──
# These constants are used only by the EV branch's effective-capacity
# post-processing; diesel vehicles use the separate DIESEL_HEADERS and do not go
# through this path.
def _row_idx(header_name: str) -> int:
    """Return the 0-based index of header_name in the row tuple (excluding the Leg Number column)."""
    return HEADERS.index(header_name) - 1


# The row tuple omits the leading 'Leg Number' column (see report_builder
# `_seg_to_row`), so every _IDX_* below is HEADERS.index(name) - 1. Guard the
# contract explicitly so a HEADERS reorder that moves 'Leg Number' fails loudly.
assert HEADERS[0] == "Leg Number"


_IDX_LEG_TYPE = _row_idx("Leg Type")
_IDX_SOC_CHANGE = _row_idx("SOC Change (%)")
_IDX_ENERGY = _row_idx("Energy Change (kWh)")
_IDX_DISTANCE = _row_idx("Distance (km)")
_IDX_EPERF = _row_idx("Energy Performance (kWh/km)")
_IDX_CAP = _row_idx("Battery Capacity (kWh)")
_IDX_ESOURCE = _row_idx("Energy Source")
_IDX_BPOWER = _row_idx("Battery Power (kW)")
_IDX_DURATION = _row_idx("Duration (HH:MM:SS)")
_IDX_ELEV = _row_idx("Elevation Difference (m)")
_IDX_MASS = _row_idx("Vehicle Mass (kg)")
_IDX_EPERF_CORR = _row_idx(
    "Energy Performance Corrected by Elevation Difference (kWh/km)"
)
_IDX_EPERF_KIN = _row_idx("Energy Performance Kinetics Corrected (kWh/km)")
_IDX_EP_EXCL_AUX = _row_idx("EP_exclude_aux")
_IDX_START = _row_idx("Start Time (UTC)")

# ── time-local (~1-month) effective-capacity window ─────────────────────────
# A soc_estimate segment's capacity is instead inferred from the non-soc_estimate
# effective capacities within a window of "that row's Start Time ±CAP_WINDOW_HALF_DAYS
# days", to reflect battery ageing and seasonal temperature drift (especially in
# long / full-range reports, e.g. EX74JXY 2025-04→2026-02). Within the window,
# "charge segments take priority over discharge segments" is still kept. If the
# window has no donor, the half-width is progressively doubled, eventually
# degrading to the whole-period mean (equivalent to the old global behaviour),
# then to fallback_kwh. Short reports (≤3 months) have a small period span so the
# window soon covers the whole period, with behaviour almost identical to the old
# version. Adjusting this constant tunes the window width overall.
CAP_WINDOW_HALF_DAYS = 15  # half window width (days); total window ≈ 1 month
_DAY_NS = 86_400 * 1_000_000_000


# ── quarterly (report-period) level effective-capacity weighted-average schema ──
# vehicles.json's ``effective_capacity_kwh`` is no longer overwritten by "the
# period mean of the last generation" (the old implementation = capacity drift /
# losing the degradation trajectory). Instead, take the donor-count weighted
# average over all "reliable" quarters in ``effective_capacity_quarterly``
# (``{period_key: {kwh, n}}``, period_key = the report-period string
# ``YYYYMMDD_YYYYMMDD``, 1:1 with the quarterly report): Σ(kwh·n)/Σn.
# A quarter whose donor count ``n < MIN_DONORS`` is treated as unreliable (too few
# samples: a single anomalous capacity reading can noticeably skew the quarter
# mean, standard error ≈ σ/√n): it is excluded from the weighted average and its
# stored ``kwh`` falls back to that average value (``n`` is still stored for
# identification). Keeping the ``effective_capacity_kwh`` key = all readers (PDF
# range / dashboard / SOC-estimate seed, etc.) automatically get the stable
# average with zero changes. The threshold is 5: an active EV usually has dozens
# of measured charge/discharge donors per quarter, far above 5; < 5 cleanly marks
# "genuinely sparse" partial windows (e.g. CMZ6260's ~6-week fill window) or
# barely-driven vehicles.
MIN_DONORS = 5


# ── per-vehicle SOC-energy fallback for stale counter anchors ────────────────
# On a handful of vehicles the Total-Energy-Used counter ANCHORS (endpoint
# snapshots) are stale / bursty — far from the trip boundary — so a discharge
# leg's counter delta under/over-attributes energy (a frozen anchor makes one leg
# read ≈0 kWh while the next leg swallows the whole backlog, giving an implied
# capacity 2–3× the fleet median and EP 2.5–5 kWh/km). These are counter-ANCHOR
# failures, not ΔSOC failures — on these vehicles the SOC channel is densely
# sampled and reliable. For such vehicles we opt in (``soc_energy_fallback: true``
# in vehicles.json) to a **gated** step-2 rewrite: an outlier (±1σ) counter-sourced
# leg whose ΔSOC is large enough AND whose implied capacity is far enough from the
# inlier replacement gets its energy re-derived from ``ΔSOC × replacement_capacity``
# (Energy Source → 'soc_fallback'). The dual gate keeps the intervention surgical —
# it only fires when the counter is BOTH an outlier AND has a trustworthy large
# ΔSOC — so ordinary short / small-ΔSOC legs (where SOC quantisation dominates and
# the counter is the better measurement) keep their counter energy (MODE A).
#
# Vehicles NOT enabled: AV24LXJ/K/L (user-verified high-rate trustworthy counters —
# see the MODE B rejection note in ``_correct_effective_capacity``), EX74JXW/JXY
# (logger-grade moving_energy), the SOC-only Mercedes (LN25NKE/YN25RSY/YN75NMA are
# 100% soc_estimate so this is moot), and diesel (no SOC/counter).
#
# ``min_dsoc_pct`` bounds the integer-% SOC quantisation error of the fallback
# energy to ≤ ~±5 % (a ±0.5 % rounding on |ΔSOC| ≥ 10 %); ``min_dev`` requires the
# implied capacity to sit ≥ 30 % away from the replacement before we distrust the
# counter. Both are overridable per vehicle (``soc_fallback_min_dsoc_pct`` /
# ``soc_fallback_min_dev``); no vehicle overrides them today.
SOC_FALLBACK_MIN_DSOC_PCT = 10.0
SOC_FALLBACK_MIN_DEV = 0.30


def _resolve_soc_fallback(cfg: dict) -> dict | None:
    """Build the per-vehicle SOC-energy-fallback control dict from a vehicle
    config, or ``None`` when the vehicle has not opted in.

    Returns ``{'enabled': True, 'min_dsoc_pct': float, 'min_dev': float}`` when
    ``cfg['soc_energy_fallback']`` is truthy, honouring optional per-vehicle
    overrides ``soc_fallback_min_dsoc_pct`` / ``soc_fallback_min_dev``; otherwise
    ``None`` (callers then run the default MODE A gate unchanged).
    """
    if not cfg or not cfg.get("soc_energy_fallback"):
        return None
    try:
        min_dsoc = float(
            cfg.get("soc_fallback_min_dsoc_pct", SOC_FALLBACK_MIN_DSOC_PCT)
        )
    except (TypeError, ValueError):
        min_dsoc = SOC_FALLBACK_MIN_DSOC_PCT
    try:
        min_dev = float(cfg.get("soc_fallback_min_dev", SOC_FALLBACK_MIN_DEV))
    except (TypeError, ValueError):
        min_dev = SOC_FALLBACK_MIN_DEV
    return {"enabled": True, "min_dsoc_pct": min_dsoc, "min_dev": min_dev}


def _cap_is_valid(v: object) -> bool:
    """Whether a capacity / SOC value is a valid number (not None / not NaN)."""
    if v is None:
        return False
    try:
        return not np.isnan(v)
    except (TypeError, ValueError):
        return False


def _soc_weighted_cap(caps, weights) -> float | None:
    """ΔSOC-weighted (combined-ratio) donor effective capacity — replaces the
    plain mean of per-segment implied capacities.

    Each donor's implied capacity is ``C_i = |ΔE_i| / (|ΔSOC_i|/100)``. SOC is
    integer-% quantised, so a single leg's ``C_i`` is noisy AND upward-biased for
    small ``|ΔSOC|`` (σ_C/C ≈ 0.5/|ΔSOC|, and Jensen makes ``E[1/ΔSOC] > 1/ΔSOC``);
    a **plain mean** of donor ``C_i`` is therefore inflated whenever the donor pool
    contains small-ΔSOC legs. This helper instead returns the ΔSOC-weighted mean

        C_eff = Σ(C_i · |ΔSOC_i|) / Σ|ΔSOC_i|  ==  100 · Σ|ΔE_i| / Σ|ΔSOC_i|

    i.e. the combined-ratio estimator — it uses all data (no arbitrary ΔSOC
    cutoff), smoothly down-weights the noisy short legs, and never divides by a
    single small ΔSOC, removing the small-ΔSOC bias entirely.

    Rejected alternatives (verified on AV24LXJ discharge donors): a hard
    ``|ΔSOC| ≥ X%`` cutoff (threshold-sensitive — ≥5% ok but ≥10% over-restricts
    and drifts, and discards data) and ``ΔSOC²`` / inverse-variance weighting
    (over-weights the largest legs and drifts low).

    ``weights`` are the per-donor ``|ΔSOC|`` (%). Entries whose weight is invalid
    / non-positive are excluded from the weighted sum (they can't be weighted); if
    the total usable weight is 0 the result degrades to a plain mean of ``caps``
    (guards ``Σ|ΔSOC| == 0``). Returns ``None`` for an empty pool.
    """
    caps = [float(c) for c in caps]
    if not caps:
        return None
    ws = [
        abs(float(w)) if (_cap_is_valid(w) and float(w) != 0.0) else 0.0
        for w in weights
    ]
    tot = sum(ws)
    if tot <= 0.0:
        return float(np.mean(caps))
    return sum(c * w for c, w in zip(caps, ws)) / tot


def _period_capacity_from_rows(
    rows: list, idx_cap: int, idx_soc: int, idx_esrc: int
) -> tuple[float | None, int, str]:
    """Replicate ``_correct_effective_capacity``'s donor-based ``avg_eff_cap``
    definition: take the effective capacity from non-``soc_estimate`` segments
    (measured charge / discharge legs) with "charge preferred", using within the
    donor set a **ΔSOC-weighted mean** (the combined-ratio estimate
    ``100·Σ|ΔE|/Σ|ΔSOC|``, see :func:`_soc_weighted_cap`) rather than a plain mean —
    a plain mean is biased upward when the donor pool contains small-ΔSOC segments
    (integer-% quantisation + Jensen). Returns ``(kwh|None, n_donors, source)``,
    source ∈ {'charge', 'discharge', 'fallback'}. ``n_donors`` is still the donor
    **count** (for the cross-quarter donor-count weighted average, semantics unchanged).

    Shares the same convention as ``_persist_effective_capacity`` / backfill:
    ``rows`` may be either report row-tuples (pass ``_IDX_CAP`` /
    ``_IDX_SOC_CHANGE`` / ``_IDX_ESOURCE``) or ``(cap, soc, src)`` triples read from
    the xlsx (pass 0, 1, 2), keeping live and backfill consistent.

    A donor must satisfy: ``Energy Source`` is a genuine measured string
    (``isinstance str`` and ∉ {'soc_estimate', 'soc_fallback'} — both are
    SOC-derived energy, not measured donors), ``cap`` is a positive valid number,
    and ``SOC Change`` is a usable non-zero value (used as the weighting weight; a
    segment lacking ΔSOC cannot be weighted, so it is excluded). The measured-leg
    criterion always holds on the live path for a genuine measured leg (its
    energy_source is always a string), so it has zero effect on the live donor set;
    but when backfill reads the final xlsx it **excludes Stop rows** — a Stop row's
    ``Energy Source`` / ``Battery Capacity`` are ``=NA()`` formulas, which openpyxl
    ``data_only=True`` reads as the integer ``0`` and which would otherwise be
    mistaken for a cap=0 donor polluting the mean. ``cap > 0`` is extra insurance
    (effective capacity is physically always positive; a real donor is always > 0,
    so it likewise does not affect the live values).
    """
    charge, discharge = [], []  # list[(cap, |ΔSOC|)]
    for row in rows:
        cap = row[idx_cap]
        src = row[idx_esrc]
        soc = row[idx_soc]
        # measured donor := real energy counter (NOT SOC-derived). Exclude both
        # 'soc_estimate' (SOC × nominal) and 'soc_fallback' (SOC-rewritten
        # stale-anchor legs) — neither is a measured capacity donor.
        if (
            isinstance(src, str)
            and src not in ("soc_estimate", "soc_fallback")
            and _cap_is_valid(cap)
            and cap > 0
            and _cap_is_valid(soc)
        ):
            if soc > 0:
                charge.append((float(cap), abs(float(soc))))
            elif soc < 0:
                discharge.append((float(cap), abs(float(soc))))
    if charge:
        caps, ws = zip(*charge)
        return _soc_weighted_cap(caps, ws), len(charge), "charge"
    if discharge:
        caps, ws = zip(*discharge)
        return _soc_weighted_cap(caps, ws), len(discharge), "discharge"
    return None, 0, "fallback"


def _recompute_weighted_capacity(
    quarterly: dict, min_donors: int = MIN_DONORS
) -> tuple[float | None, int, int]:
    """Given ``{period_key: {kwh, n}}``, take the donor-count weighted average over
    the **reliable** quarters (``n >= min_donors``): Σ(kwh·n)/Σn. In place, backfill
    the stored ``kwh`` of sparse quarters (``n < min_donors``) with that average
    value (``n`` unchanged). Returns ``(weighted_avg|None, n_reliable, n_sparse)``.

    Degradation: if there is no reliable quarter but there are donor quarters
    (``0 < n < min_donors``), use all donor quarters for the same weighted average,
    ensuring ``effective_capacity_kwh`` can still be written (the PDF range does not
    go blank). No donor quarter at all → return ``(None, 0, 0)`` and the caller
    leaves the existing scalar unchanged.
    """
    reliable = [
        (v["kwh"], v["n"])
        for v in quarterly.values()
        if v.get("kwh") is not None and v.get("n", 0) >= min_donors
    ]
    n_sparse = sum(
        1
        for v in quarterly.values()
        if v.get("kwh") is not None and 0 < v.get("n", 0) < min_donors
    )
    pool = reliable or [
        (v["kwh"], v["n"])
        for v in quarterly.values()
        if v.get("kwh") is not None and v.get("n", 0) > 0
    ]
    if not pool:
        return None, 0, n_sparse
    total_n = sum(n for _, n in pool)
    wavg = round(sum(k * n for k, n in pool) / total_n, 1)
    # A sparse quarter's stored kwh falls back to the weighted average (n kept for identification)
    for v in quarterly.values():
        if v.get("n", 0) < min_donors:
            v["kwh"] = wavg
    return wavg, len(reliable), n_sparse


def _correct_effective_capacity(
    rows: list,
    idx_cap: int,
    idx_energy: int,
    idx_soc: int,
    idx_eperf: int,
    idx_dist: int,
    idx_esrc: int,
    idx_bpower: int,
    idx_dur: int,
    idx_eperf_corr: int,
    idx_elev: int,
    idx_mass: int,
    fallback_kwh: float | None,
    idx_eperf_kin: int | None = None,
    idx_start: int | None = None,
    soc_fallback: dict | None = None,
) -> tuple[list, float | None, str]:
    """
    Post-processing: correct the effective battery capacity and re-derive the related fields.

    Returns (rows, effective_cap, cap_source):
    - effective_cap: the final effective-capacity mean (kWh) over the whole report
      period, for ``_persist_effective_capacity`` to write back to vehicles.json
      (semantics unchanged).
    - cap_source: 'charge' | 'discharge' | 'fallback'

    Time-local (~1-month) capacity
    ------------------------------
    Each soc_estimate segment's replacement capacity is inferred from the
    non-soc_estimate effective capacities within a window of "that segment's Start
    Time ±``CAP_WINDOW_HALF_DAYS`` days", keeping "charge segments preferred over
    discharge segments" within the window. This way, battery ageing / seasonal
    temperature drift in long reports is not flattened by a single whole-period
    mean. ``idx_start`` is the index of the 'Start Time (UTC)' column in the row
    tuple (value is a ``pd.Timestamp``); when ``None`` it reverts to the old
    single whole-period mean behaviour.

    Graceful degradation (in order):
    1. Donors within the ±CAP_WINDOW_HALF_DAYS-day window (charge first, then discharge).
    2. No donor in the window → progressively double the half-width until a donor
       is found or the window covers the whole period (covering the whole period is
       equivalent to the old global-mean behaviour).
    3. No donor in the whole period (or the row lacks a timestamp) → global mean.
    4. No donor globally either → ``fallback_kwh``.

    Donor capacities are aggregated with the ΔSOC-weighted (combined-ratio)
    estimator (see :func:`_soc_weighted_cap` / :func:`_period_capacity_from_rows`),
    not a plain mean.

    Step 1: replace each soc_estimate segment's capacity by the time-local logic
            above and re-derive its energy.
    Step 2: detect anomalous effective capacity with a **global ±1σ** gate; the
            replacement is again the "inlier donor time-local window mean", to
            avoid re-injecting a wrong-season deviation. **The energy re-derivation
            is gated by ``energy_source``**: only soc_estimate segments (whose
            energy is itself derived from SOC × capacity) recompute
            energy / EP / corrected / kinetics; counter-sourced segments
            (``energy_source`` ∈ {total_energy, moving_energy}) **by default**
            (MODE A) correct only the capacity column itself and keep the counter's
            original energy / EP / corrected / kinetics — an anomalous IMPLIED
            capacity usually comes from the unreliable integer-% quantisation of
            the denominator ΔSOC (short / small-ΔSOC underestimated), not from a
            wrong counter energy, and re-deriving from SOC would inject the
            integer-% underestimate into short legs, forming a spurious low-EP band.

    Per-vehicle SOC-energy fallback (opt-in, see the ``soc_fallback`` parameter)
    ---------------------------------------------------------------------------
    On a few vehicles the Total-Energy-Used counter anchors are stale / bursty
    (endpoint snapshots far from the trip boundary), causing paired
    under/over-attribution: one leg reads ≈0 kWh because the anchor is frozen while
    the next leg swallows the whole backlog, giving an IMPLIED capacity 2–3× the
    fleet median and EP 2.5–5 kWh/km. This is a counter-anchor failure, not a ΔSOC
    failure — these vehicles' SOC channel is densely sampled and reliable. For
    vehicles that opt in, step 2's counter-sourced outlier rows instead take a
    **dual-gated SOC re-derivation**: only when
    ``|ΔSOC| ≥ min_dsoc_pct`` (bounding the integer-% quantisation error to ≤ ~±5 %)
    AND ``|original IMPLIED cap − replacement cap| / replacement cap ≥ min_dev`` is
    the energy re-derived from ``ΔSOC/100 × replacement cap`` (sign follows ΔSOC,
    discharge negative), the Energy Source set to ``'soc_fallback'`` and the
    EP / Battery Power / corrected / kinetics recomputed as for a soc_estimate
    segment; otherwise it reverts to MODE A (correct only the capacity column, keep
    the counter energy). The dual gate keeps the intervention surgical: it only
    fires when the counter is BOTH a ±1σ outlier AND has a trustworthy large ΔSOC,
    so ordinary short / small-ΔSOC legs (where SOC quantisation dominates and the
    counter is more trustworthy) keep their counter value. With
    ``soc_fallback=None`` (the default, non-opted-in vehicles) the behaviour is
    identical to the old MODE A.

    Vehicles are opted in via ``soc_energy_fallback`` in vehicles.json. Vehicles
    with user-verified trustworthy high-rate counters (AV24LXJ/K/L) are
    deliberately NOT opted in. (The rejected "±1σ scaled by ΔSOC" MODE B
    alternative and the deferred systematic-counter-bias / anchor-spillover
    investigations live in the git history / changelogs / pending_issues.)
    """
    # Per-vehicle SOC-energy fallback control (None = MODE A only).
    fb_enabled = bool(soc_fallback and soc_fallback.get("enabled"))
    fb_min_dsoc = float(
        (soc_fallback or {}).get("min_dsoc_pct", SOC_FALLBACK_MIN_DSOC_PCT)
    )
    fb_min_dev = float((soc_fallback or {}).get("min_dev", SOC_FALLBACK_MIN_DEV))

    def _apply_soc_energy(row, soc_chg, cap):
        """Re-derive energy from ``ΔSOC/100 × cap`` (signed) and recompute the
        downstream EP / Battery Power / elevation- & kinetics-corrected EP,
        exactly as a native ``soc_estimate`` leg. Shared by the step-1
        soc_estimate rewrite and the step-2 SOC-fallback rewrite."""
        row[idx_energy] = round(soc_chg / 100.0 * cap, 3)
        dist = row[idx_dist]
        if _cap_is_valid(dist) and dist > 0 and _cap_is_valid(row[idx_energy]):
            row[idx_eperf] = round(abs(row[idx_energy]) / dist, 4)
        dur_days = row[idx_dur]
        if _cap_is_valid(dur_days) and dur_days > 0 and _cap_is_valid(row[idx_energy]):
            dur_h = dur_days * 24.0
            row[idx_bpower] = round(row[idx_energy] / dur_h, 3)
        _recalc_eperf_corrected(row)

    def _fallback_applies(row, original_cap, repl):
        """Dual gate for the SOC-energy fallback (counter-sourced outlier
        rows only). True iff ΔSOC is large enough that the fallback's integer-%
        quantisation error stays small (``|ΔSOC| ≥ fb_min_dsoc``) AND the row's
        original IMPLIED capacity sits far enough from the inlier replacement
        (``|original_cap − repl| / repl ≥ fb_min_dev``) to distrust the counter.
        ``repl`` must be a positive replacement capacity."""
        soc_chg = row[idx_soc]
        if not (_cap_is_valid(soc_chg) and abs(float(soc_chg)) >= fb_min_dsoc):
            return False
        if not (_cap_is_valid(repl) and repl > 0 and _cap_is_valid(original_cap)):
            return False
        return abs(float(original_cap) - repl) / repl >= fb_min_dev

    def _recalc_eperf_corrected(row):
        """Recompute the elevation-corrected and kinetics-corrected energy performance after energy is changed."""
        e = row[idx_energy]
        d = row[idx_dist]
        h = row[idx_elev]
        m = row[idx_mass]
        if not all(_cap_is_valid(v) for v in (e, d, h, m)):
            return
        if d <= 0:
            return
        ke_per_d = None
        if idx_eperf_kin is not None:
            old_corr = row[idx_eperf_corr]
            old_kin = row[idx_eperf_kin]
            if _cap_is_valid(old_corr) and _cap_is_valid(old_kin):
                ke_per_d = old_corr - old_kin
        e_grav = m * 9.81 * h / 3_600_000.0
        row[idx_eperf_corr] = round((abs(e) - e_grav) / d, 4)
        if ke_per_d is not None and idx_eperf_kin is not None:
            row[idx_eperf_kin] = round(row[idx_eperf_corr] - ke_per_d, 4)

    # ── Timestamp and window preparation ───────────────────────────────────
    def _naive_ns(v):
        """A row's Start Time → tz-naive ns integer; None if unparseable."""
        if v is None:
            return None
        try:
            ts = pd.Timestamp(v)
        except (TypeError, ValueError):
            return None
        if ts is pd.NaT or pd.isna(ts):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None)
        # pd.Timestamp.value is always ns for a scalar, no .as_unit needed
        return int(ts.value)

    # Start time (ns) of each row; when idx_start is None they are all None → revert to global behaviour
    if idx_start is not None:
        row_ns = [_naive_ns(row[idx_start]) for row in rows]
    else:
        row_ns = [None] * len(rows)
    valid_ns = [n for n in row_ns if n is not None]
    period_span_days = (
        (max(valid_ns) - min(valid_ns)) / _DAY_NS if len(valid_ns) >= 2 else 0.0
    )
    # Upper bound for doubling the half-width: covering the whole period is equivalent to the old global behaviour
    max_half_days = max(float(CAP_WINDOW_HALF_DAYS), period_span_days)

    def _window_mean(donors, t_ns, base_half_days):
        """donors: list[(ns, cap, w)], w = |ΔSOC|. Take the **ΔSOC-weighted mean**
        within the ±base_half-day window (the combined-ratio estimate Σ(cap·w)/Σw,
        see :func:`_soc_weighted_cap`); if there is no donor, progressively double
        the half-width until one is found or the whole period is covered. Returns
        (mean|None, n_used, half_used); degrades to a plain mean when Σw==0 in the
        window."""
        if not donors or t_ns is None:
            return None, 0, base_half_days
        arr_ns = np.array([d[0] for d in donors], dtype=np.int64)
        arr_cap = np.array([d[1] for d in donors], dtype=float)
        arr_w = np.array([d[2] for d in donors], dtype=float)
        half = float(base_half_days)
        while True:
            win_ns = int(half * _DAY_NS)
            mask = np.abs(arr_ns - t_ns) <= win_ns
            if mask.any():
                caps_w = arr_cap[mask]
                ws_w = arr_w[mask]
                tot = float(ws_w.sum())
                # same estimator as _soc_weighted_cap (kept inline verbatim): the
                # numpy vectorised Σ(cap·w)/Σw here differs in float accumulation
                # order (and skips the invalid-weight filtering, unreachable for
                # window donors) from the Python-level helper, so routing through it
                # could perturb the byte-identical output — deliberately not shared.
                m = (
                    float((caps_w * ws_w).sum() / tot)
                    if tot > 0.0
                    else float(caps_w.mean())
                )
                return m, int(mask.sum()), half
            if half >= max_half_days:
                return None, 0, half
            half = min(half * 2.0, max_half_days)

    # ── Step 1: time-local effective capacity replaces soc_estimate segments ────
    # Charge segment: SOC rising (soc > 0); discharge segment: SOC falling (soc < 0)
    # Donors carry |ΔSOC| as the combined-ratio weight (charge/discharge capacities
    # use a ΔSOC-weighted mean within the set, removing the integer-% quantisation
    # upward bias of small-ΔSOC segments; see _soc_weighted_cap).
    charge_donors = []  # list[(ns, cap, w)] ; w = |ΔSOC|
    discharge_donors = []
    charge_caps = []  # list[(cap, w)]  whole period (does not require a timestamp)
    discharge_caps = []
    for row, n in zip(rows, row_ns):
        cap = row[idx_cap]
        src = row[idx_esrc]
        soc = row[idx_soc]
        # measured donor := NOT SOC-derived. On fresh generation 'soc_fallback'
        # cannot yet appear here (it is assigned later, in step 2); the check is
        # defensive for replay paths that might feed already-corrected rows.
        if (
            _cap_is_valid(cap)
            and src not in ("soc_estimate", "soc_fallback")
            and _cap_is_valid(soc)
        ):
            w = abs(float(soc))
            if soc > 0:
                charge_caps.append((cap, w))
                if n is not None:
                    charge_donors.append((n, cap, w))
            elif soc < 0:
                discharge_caps.append((cap, w))
                if n is not None:
                    discharge_donors.append((n, cap, w))

    # Whole-period ΔSOC-weighted mean (charge preferred), used for degradation and
    # the final persistence semantics (convention unchanged). Uses the
    # timestamp-independent *_caps so that even with idx_start=None (old calls) it
    # gives the correct global charge/discharge weighted mean rather than wrongly
    # falling through to fallback.
    if charge_caps:
        caps, ws = zip(*charge_caps)
        avg_eff_cap = _soc_weighted_cap(caps, ws)
        cap_source = "charge"
    elif discharge_caps:
        caps, ws = zip(*discharge_caps)
        avg_eff_cap = _soc_weighted_cap(caps, ws)
        cap_source = "discharge"
    else:
        avg_eff_cap = fallback_kwh
        cap_source = "fallback"

    def _local_cap_for(t_ns):
        """Charge-preferred time-local effective capacity. Returns (cap|None, src, half)."""
        m, _, half = _window_mean(charge_donors, t_ns, CAP_WINDOW_HALF_DAYS)
        if m is not None:
            return m, "charge", half
        m, _, half = _window_mean(discharge_donors, t_ns, CAP_WINDOW_HALF_DAYS)
        if m is not None:
            return m, "discharge", half
        return None, cap_source, half

    n_local = n_widened = n_global = n_fallback = 0
    widened_half_max = 0.0
    for row, t_ns in zip(rows, row_ns):
        if row[idx_esrc] == "soc_estimate" and _cap_is_valid(row[idx_soc]):
            cap, _src, half = _local_cap_for(t_ns)
            if cap is None:
                # The row lacks a timestamp or the whole period has no donor → global mean (or fallback_kwh)
                cap = avg_eff_cap
                if cap_source == "fallback":
                    n_fallback += 1
                else:
                    n_global += 1
                # An un-onboarded vehicle with no capacity donors AND no
                # srf/nominal fallback leaves ``avg_eff_cap`` None. There is no
                # capacity to attribute, so leave this soc_estimate row's capacity
                # / energy as-is (NaN) instead of crashing on round(None). For any
                # onboarded vehicle ``fallback_kwh`` is always a number, so this
                # guard never fires and the numeric path stays byte-identical.
                if cap is None:
                    continue
            elif half <= CAP_WINDOW_HALF_DAYS:
                n_local += 1
            else:
                n_widened += 1
                widened_half_max = max(widened_half_max, half)
            row[idx_cap] = round(cap, 2)
            _apply_soc_energy(row, row[idx_soc], cap)

    logger.info(
        "Step 1 (time-local ±%d days, period span=%.0f days): soc_estimate replacement — "
        "local=%d, widened=%d(max half-width %.0f days), global fallback=%d, fallback=%d; "
        "donor(charge=%d, discharge=%d), global mean=%.1f kWh(source=%s)",
        CAP_WINDOW_HALF_DAYS,
        period_span_days,
        n_local,
        n_widened,
        widened_half_max,
        n_global,
        n_fallback,
        len(charge_donors),
        len(discharge_donors),
        avg_eff_cap,
        cap_source,
    )

    # ── Step 2: global ±1σ detection + time-local inlier mean replacement ────
    all_caps = [row[idx_cap] for row in rows if _cap_is_valid(row[idx_cap])]
    if len(all_caps) >= 3:
        cap_mean = float(np.mean(all_caps))
        cap_std = float(np.std(all_caps))
        lo = cap_mean - cap_std
        hi = cap_mean + cap_std
        # inlier donors (within ±1σ) together with the timestamp + |ΔSOC| weight,
        # for the anomalous rows to do a time-local replacement. The replacement
        # mean also uses ΔSOC weighting (the combined-ratio estimate), avoiding the
        # small-ΔSOC quantisation upward bias being re-injected into the
        # replacement value. Rows lacking ΔSOC are weighted 0 (naturally excluded
        # from the weighted sum).
        # (soc_fallback rows are SOC-derived, not measured caps — exclude them
        # as inlier donors. On fresh generation none exist at this point, so
        # this is a defensive no-op; it matters only for replay of already-
        # corrected rows. soc_estimate rows are intentionally KEPT here — after
        # step 1 they carry a donor-derived cap.)
        inlier_donors = [
            (
                n,
                row[idx_cap],
                abs(float(row[idx_soc])) if _cap_is_valid(row[idx_soc]) else 0.0,
            )
            for row, n in zip(rows, row_ns)
            if n is not None
            and _cap_is_valid(row[idx_cap])
            and lo <= row[idx_cap] <= hi
            and row[idx_esrc] != "soc_fallback"
        ]
        inlier_global_mean = (
            _soc_weighted_cap(
                [c for _, c, _ in inlier_donors], [w for _, _, w in inlier_donors]
            )
            if inlier_donors
            else cap_mean
        )
        corrected = n_repl_local = n_energy_kept = n_soc_fallback = 0
        for row, t_ns in zip(rows, row_ns):
            cap = row[idx_cap]
            if _cap_is_valid(cap) and (cap < lo or cap > hi):
                # Original IMPLIED capacity (the value **before** the capacity
                # column is replaced), used for the SOC-fallback deviation gate.
                original_cap = cap
                repl, _, _ = _window_mean(inlier_donors, t_ns, CAP_WINDOW_HALF_DAYS)
                if repl is not None:
                    n_repl_local += 1
                else:
                    repl = inlier_global_mean
                # Always correct the capacity column itself: this both makes the
                # display sensible and keeps the period's final persisted mean from
                # being polluted by the anomalous IMPLIED capacity (persistence
                # semantics unchanged).
                row[idx_cap] = round(repl, 2)
                # Energy re-derivation applies **only** to soc_estimate segments (no
                # reliable energy counter, their energy is itself derived from
                # SOC × capacity). For counter-sourced segments (energy_source ∈
                # {total_energy, moving_energy}), an anomalous IMPLIED capacity means
                # the denominator ΔSOC is unreliable (integer-% quantisation,
                # short/small-ΔSOC underestimated), not that the counter energy is
                # wrong — **by default (MODE A)** the counter's energy / EP /
                # corrected / kinetics must be kept and never overwritten by a SOC
                # re-derivation (otherwise short legs would get a spuriously low EP,
                # forming a false low band).
                if row[idx_esrc] == "soc_estimate":
                    soc_chg = row[idx_soc]
                    if _cap_is_valid(soc_chg) and soc_chg != 0:
                        _apply_soc_energy(row, soc_chg, repl)
                elif fb_enabled and _fallback_applies(row, original_cap, repl):
                    # Per-vehicle SOC-energy fallback: a counter-sourced
                    # anomalous row, but with ΔSOC large enough that the
                    # quantisation error is controllable AND the IMPLIED cap
                    # severely deviating — judged to be a stale counter anchor, so
                    # re-derive from SOC to replace the untrustworthy counter energy.
                    _apply_soc_energy(row, row[idx_soc], repl)
                    row[idx_esrc] = "soc_fallback"
                    n_soc_fallback += 1
                else:
                    n_energy_kept += 1
                corrected += 1
        logger.info(
            "Step 2 (global ±1σ detection + time-local replacement): corrected %d rows of anomalous effective "
            "capacity (of which %d rows used a local window mean; counter-sourced: %d rows SOC "
            "fallback re-derivation(soc_fallback), %d rows corrected the capacity column only and kept the counter energy; "
            "global mean=%.1f, σ=%.1f, range=[%.1f, %.1f], inlier donor=%d)",
            corrected,
            n_repl_local,
            n_soc_fallback,
            n_energy_kept,
            cap_mean,
            cap_std,
            lo,
            hi,
            len(inlier_donors),
        )
        # The whole-period mean after step 2 is the final persisted effective capacity (semantics unchanged)
        final_caps = [row[idx_cap] for row in rows if _cap_is_valid(row[idx_cap])]
        if final_caps:
            avg_eff_cap = float(np.mean(final_caps))

    # An all-fallback report on an un-onboarded vehicle with no capacity
    # source leaves ``avg_eff_cap`` None; return None rather than crashing on
    # round(None). For onboarded vehicles ``avg_eff_cap`` is always a number here
    # (fallback_kwh is never None), so this is a no-op for them.
    if avg_eff_cap is None:
        return rows, None, cap_source
    return rows, round(avg_eff_cap, 1), cap_source


def _persist_effective_capacity(
    reg: str, eff_cap: float | None, n_donors: int, source: str, period_key: str
) -> None:
    """Merge this report period's effective capacity into vehicles.json.

    New schema:
    - ``effective_capacity_quarterly``: ``{period_key: {kwh, n}}``, period_key =
      the report-period string ``YYYYMMDD_YYYYMMDD``, 1:1 with the quarterly report.
      This period writes ``{kwh: round(eff_cap, 1), n: n_donors}``.
    - ``effective_capacity_kwh``: the donor-count weighted average
      (Σ(kwh·n)/Σn) over all reliable quarters (``n >= MIN_DONORS``). Sparse
      quarters (``n < MIN_DONORS``) are not counted, and their stored ``kwh`` is
      backfilled by :func:`_recompute_weighted_capacity` to that average. Keeping
      the key name lets the PDF range / dashboard / SOC seed get the stable average
      with zero changes.

    Written only when source is 'charge' / 'discharge' (from telematics donors);
    source='fallback' (no donor, e.g. the pure-soc_estimate SOC-only Mercedes) does
    not write and does not touch the existing scalar. This also fixes the capacity
    drift bug caused by the old implementation's "single-period mean overwrite".
    """
    if source == "fallback" or eff_cap is None:
        logger.info(
            "effective capacity source is fallback (no donor), "
            "not updating vehicles.json: %s %s",
            reg,
            period_key,
        )
        return

    import json

    from filelock import FileLock

    from jolt_toolkit.configs import get_config_path

    path = get_config_path("vehicles.json")
    # Guard the read-modify-write so parallel report runs cannot clobber
    # each other's capacity ledger entries (values/timing unchanged).
    with FileLock(str(path) + ".lock"):
        with open(path, "r", encoding="utf-8") as f:
            all_cfg = json.load(f)
        if reg not in all_cfg:
            return

        entry = all_cfg[reg]
        old_val = entry.get("effective_capacity_kwh")
        quarterly = entry.get("effective_capacity_quarterly") or {}
        quarterly[period_key] = {"kwh": round(float(eff_cap), 1), "n": int(n_donors)}

        wavg, n_rel, n_sparse = _recompute_weighted_capacity(quarterly)
        entry["effective_capacity_quarterly"] = quarterly
        if wavg is not None:
            entry["effective_capacity_kwh"] = wavg

        with open(path, "w", encoding="utf-8") as f:
            json.dump(all_cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    # Sync the in-memory VEHICLE_CONFIG
    VEHICLE_CONFIG.setdefault(reg, {})
    VEHICLE_CONFIG[reg]["effective_capacity_quarterly"] = quarterly
    if wavg is not None:
        VEHICLE_CONFIG[reg]["effective_capacity_kwh"] = wavg
    logger.info(
        "effective_capacity updated: %s  %.1f → %.1f kWh "
        "(this period %s: kwh=%.1f n=%d source=%s; reliable quarters=%d, sparse=%d)",
        reg,
        old_val or 0,
        wavg if wavg is not None else (old_val or 0),
        period_key,
        round(float(eff_cap), 1),
        int(n_donors),
        source,
        n_rel,
        n_sparse,
    )
