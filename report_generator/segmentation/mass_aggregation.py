"""
Per-segment vehicle-mass aggregation (configurable, robust).

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .constants import PIPELINE_CONFIGS, VEHICLE_CONFIG

logger = logging.getLogger(__name__)

# =============================================================================
# Per-segment vehicle-mass aggregation (configurable, robust)
# =============================================================================
# Single source of truth shared by the three plain-mean sites that estimate a
# segment's vehicle mass (Excel "Vehicle Mass (kg)" column, the validation-figure
# Panel-4 annotation, and the finetune recompute). The aggregation method is a
# configurable pipeline branch parameter (``mass_agg``), so a transient telematics
# weight spike (e.g. a ~49000 kg reading on a true ~30 t trip) no longer inflates
# the value. The default ``"mean"`` preserves the legacy behaviour bit-for-bit.

_MASS_AGG_METHODS = (
    "mean",
    "median",
    "iqr_median",
    "mad_median",
    "iqr_mean",
    "mad_mean",
    "mad_tw_mean",
    "trimmed_mean",
)

# ``mad_median`` / ``mad_mean`` fence width as a multiple of the MAD (median
# absolute deviation): inliers are kept within ``median ± _MAD_K * MAD``. Using
# the raw MAD (no 1.4826 normal-consistency scaling), k=3 gives a fence of
# ~2 robust-sigma — tight enough to shave a one-sided high-spike tail that an IQR
# fence leaves in (its q3 lands on the spikes), yet validated to never dip below
# the central body (see :func:`_agg_mass`).
_MAD_K = 3.0

# ``trimmed_mean`` per-tail trim fraction (symmetric). 0.20 drops the lowest and
# highest 20% of the samples and means the central 60% — the classic 20% trimmed
# mean (matching ``scipy.stats.trim_mean(proportiontocut=0.20)``).
_TRIM_FRAC = 0.20


def _iqr_inliers(sel: "pd.Series") -> "pd.Series":
    """Tukey 1.5·IQR inlier subset of ``sel`` (shared by ``iqr_median`` /
    ``iqr_mean`` so they fence identically).

    Returns the subset within ``[q1 - 1.5*iqr, q3 + 1.5*iqr]`` when the IQR is
    positive and >= 2 samples survive; otherwise returns ``sel`` unchanged (a
    zero IQR means a quantised GCW where the median already ignores a minority
    spike).
    """
    q1, q3 = sel.quantile([0.25, 0.75])
    iqr = q3 - q1
    if iqr > 0:
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        inliers = sel[(sel >= lo) & (sel <= hi)]
        if len(inliers) >= 2:
            return inliers
    return sel


def _mad_inliers(sel: "pd.Series") -> "pd.Series":
    """Median ± ``_MAD_K``·MAD inlier subset of ``sel`` (shared by ``mad_median`` /
    ``mad_mean`` so they fence identically).

    Returns the subset within ``median ± _MAD_K*MAD`` (raw median absolute
    deviation) when the MAD is positive and >= 2 samples survive; otherwise
    returns ``sel`` unchanged. Because the fence is centred on the robust median
    rather than on the quartiles, it shaves a dense body's one-sided HIGH-spike
    tail that the IQR fence leaves in (a cluster of spikes pins q3, so the IQR
    fence stays too wide).
    """
    med = sel.median()
    mad = float((sel - med).abs().median())
    if mad > 0:
        lo, hi = med - _MAD_K * mad, med + _MAD_K * mad
        inliers = sel[(sel >= lo) & (sel <= hi)]
        if len(inliers) >= 2:
            return inliers
    return sel


def _trimmed_inliers(sel: "pd.Series", frac: float = _TRIM_FRAC) -> "pd.Series":
    """Symmetric trimmed subset of ``sel``: drop the lowest & highest ``frac``.

    ``k = int(len * frac)`` samples are dropped from each tail (matching
    ``scipy.stats.trim_mean`` semantics) and the central subset is returned. When
    trimming would leave fewer than one sample (tiny windows) ``sel`` is returned
    unchanged so the caller still has a value.
    """
    n = len(sel)
    k = int(n * frac)
    if k <= 0 or n - 2 * k < 1:
        return sel
    ordered = sel.sort_values()
    return ordered.iloc[k : n - k]


def _coerce_seconds(ts: "pd.Series") -> "np.ndarray | None":
    """Coerce ``ts`` (a per-sample time axis aligned to a kept mass set) into a
    1-D float array of seconds, or ``None`` when it cannot be used.

    Accepts either a datetime-like Series (any unit / tz; e.g. the telematics
    ``eventDatetime``) or an already-numeric seconds Series (e.g. the dashboard's
    seconds-from-midnight). For datetimes the offset is taken from the first
    element, so only relative spacing matters (the time-weighted mean is invariant
    to a constant time offset / uniform scaling). Any NaN/NaT makes it unusable.
    """
    if ts is None or len(ts) == 0:
        return None
    s = ts if isinstance(ts, pd.Series) else pd.Series(list(ts))
    if pd.api.types.is_numeric_dtype(s):
        n = pd.to_numeric(s, errors="coerce")
        return n.to_numpy("float64") if bool(n.notna().all()) else None
    t = pd.to_datetime(s, errors="coerce", utc=True)
    if not bool(t.notna().all()):
        return None
    return (t - t.iloc[0]).dt.total_seconds().to_numpy("float64")


def _time_weighted_mean(values: "np.ndarray", seconds: "np.ndarray") -> float:
    """Trapezoidal time-weighted mean of ``values`` sampled at ``seconds``.

    Each sample's weight is the time interval it represents (trapezoidal rule:
    half the gap to each neighbour; the two endpoints get half of their single
    adjacent gap). A short dense burst of repeated readings therefore contributes
    only its (short) duration, not its (large) count — so a transient lag plateau
    that telematics happens to sample densely no longer biases the mean. Falls
    back to the plain arithmetic mean when the time axis is unusable (< 2 points,
    any non-finite second, or a zero span — e.g. all-duplicate timestamps), which
    makes ``mad_tw_mean`` degrade exactly to ``mad_mean``.
    """
    v = np.asarray(values, dtype="float64")
    s = np.asarray(seconds, dtype="float64")
    if v.size < 2 or s.size != v.size or not np.isfinite(s).all():
        return float(v.mean())
    order = np.argsort(s, kind="mergesort")
    s = s[order]
    v = v[order]
    if (s[-1] - s[0]) <= 0:
        return float(v.mean())
    w = np.empty_like(s)
    w[1:-1] = (s[2:] - s[:-2]) / 2.0
    w[0] = (s[1] - s[0]) / 2.0
    w[-1] = (s[-1] - s[-2]) / 2.0
    wsum = float(w.sum())
    if wsum <= 0:
        return float(v.mean())
    return float(np.dot(w, v) / wsum)


def _mad_tw_value(
    sel: "pd.Series", kept: "pd.Series", timestamps: "pd.Series | None"
) -> float:
    """Time-weighted mean of the MAD-fenced ``kept`` set, aligned to ``timestamps``.

    ``timestamps`` is expected to be index-aligned to ``sel`` (the pre-fence
    sample series), so it is reindexed onto ``kept.index`` to pick each survivor's
    time. Any failure (missing / mis-aligned / non-unique / unusable timestamps)
    falls back to ``kept.mean()`` — i.e. plain ``mad_mean`` — so the method is
    always safe to select even when a caller cannot supply a time axis.
    """
    if len(kept) >= 2 and isinstance(timestamps, pd.Series):
        try:
            secs = _coerce_seconds(timestamps.reindex(kept.index))
            if secs is not None and len(secs) == len(kept):
                return _time_weighted_mean(kept.to_numpy("float64"), secs)
        except Exception:  # pragma: no cover - defensive; degrade to mad_mean
            pass
    return float(kept.mean())


def _agg_mass(
    sel: "pd.Series", method: str = "mean", timestamps: "pd.Series | None" = None
) -> tuple[float, float]:
    """Aggregate an already-filtered mass-sample series into ``(mass_kg, cv)``.

    ``sel`` is expected to already be positive (> 0) and, where applicable,
    restricted to moving-only samples (the caller owns the filtering, so that the
    Excel column and the figure annotation share one definition).

    Each method is a two-step recipe — an outlier *fence* (which inliers to keep)
    followed by a central *estimator* (median or mean over the kept set):

        ``"mean"``         — no fence; arithmetic mean of all samples (the
                             default; non-opted-in vehicles use this).
        ``"median"``       — no fence; plain median (matches the diesel pipeline).
        ``"iqr_median"``   — Tukey 1.5·IQR fence (:func:`_iqr_inliers`) → median.
        ``"iqr_mean"``     — Tukey 1.5·IQR fence (:func:`_iqr_inliers`) → mean.
        ``"mad_median"``   — median ± _MAD_K·MAD fence (:func:`_mad_inliers`) →
                             median. The fence is centred on the robust median
                             rather than on the quartiles, so it shaves a dense
                             body's one-sided HIGH-spike tail that the IQR fence
                             leaves in (a cluster of spikes pins q3). Strengthens
                             high-outlier rejection over ``iqr_median`` without
                             dipping below the body.
        ``"mad_mean"``     — median ± _MAD_K·MAD fence (:func:`_mad_inliers`) →
                             mean of survivors. Same robust fence as ``mad_median``
                             but a mean estimator, so when the cleaned body still
                             carries a high-side lag/over-read tail (telematics
                             that lags the true load) the mean sits *below* the
                             body's median — useful where the median lands on
                             lag-high readings.
        ``"mad_tw_mean"``  — median ± _MAD_K·MAD fence (:func:`_mad_inliers`) → a
                             *time-weighted* mean of survivors (:func:`_mad_tw_value`
                             / :func:`_time_weighted_mean`), requiring per-sample
                             ``timestamps`` aligned to ``sel``. Each survivor is
                             weighted by the time interval it represents, so a
                             short dense burst of repeated lag/over-read values
                             (which count-weighting over-counts) contributes only
                             its brief duration. Where telematics samples are
                             bursty (dense transient clusters + sparse steady
                             cruise) this pulls the estimate toward the
                             duration-dominant settled body, matching how a 1 Hz
                             logger averages the same window. **Without usable
                             ``timestamps`` it degrades to ``mad_mean``** (identical
                             fence + plain mean), so it is always safe to select.
        ``"trimmed_mean"`` — symmetric 20% trimmed mean (:func:`_trimmed_inliers`
                             → mean): drop the lowest & highest 20% of samples and
                             mean the central 60% (a robust two-sided reference).

    ``timestamps`` is only consulted by ``mad_tw_mean``; every other method
    ignores it and is byte-identical to the pre-``timestamps`` behaviour.

    ``cv`` is ``std / mean`` of the kept set (sample std, ddof=1), matching the
    legacy reliability metric (``mad_tw_mean`` shares ``mad_mean``'s kept set, so
    its cv is identical). Unknown ``method`` falls back to ``"mean"`` with a
    warning. ``len(sel) < 2`` returns ``(nan, nan)``; ``mean <= 0`` yields a nan
    cv.
    """
    if sel is None or len(sel) < 2:
        return np.nan, np.nan
    m = (method or "mean").lower()
    if m not in _MASS_AGG_METHODS:
        logger.warning("Unknown mass_agg method %r; falling back to 'mean'", method)
        m = "mean"

    # Step 1 — outlier fence (the kept inlier set). ``mean`` / ``median`` use the
    # full window; each ``*_median`` and ``*_mean`` pair shares one fence helper,
    # so the median and mean variants of a fence keep BYTE-IDENTICAL inlier sets
    # (only the Step-2 estimator differs).
    if m in ("iqr_median", "iqr_mean"):
        kept = _iqr_inliers(sel)
    elif m in ("mad_median", "mad_mean", "mad_tw_mean"):
        kept = _mad_inliers(sel)
    elif m == "trimmed_mean":
        kept = _trimmed_inliers(sel)
    else:  # 'mean', 'median'
        kept = sel

    # Step 2 — central estimator over the kept set.
    if m in ("median", "iqr_median", "mad_median"):
        value = float(kept.median())
    elif m == "mad_tw_mean":
        # Time-weighted mean of the (shared) MAD fence; falls back to the plain
        # mean of the kept set when no usable time axis is supplied (== mad_mean).
        value = _mad_tw_value(sel, kept, timestamps)
    else:  # 'mean', 'iqr_mean', 'mad_mean', 'trimmed_mean'
        value = float(kept.mean())

    # ``cv`` is std/mean of the kept set (legacy reliability metric); for a
    # singleton kept set fall back to the full window so cv stays meaningful.
    cv_set = kept if len(kept) >= 2 else sel
    mu = float(cv_set.mean())
    cv = float(cv_set.std() / mu) if mu > 0 else np.nan
    return round(value, 1), round(cv, 4)


def resolve_mass_agg(reg: str, pipeline_cfg: dict | None = None) -> str:
    """Resolve the ``mass_agg`` method for ``reg``.

    Precedence: vehicle-level (``vehicles.json``) > pipeline-level
    (``pipelines.json``) > default ``"mean"``. When ``pipeline_cfg`` is not
    supplied it is derived from the vehicle's configured pipeline, so a bare
    ``resolve_mass_agg(reg)`` still honours a pipeline-level setting.
    """
    veh = VEHICLE_CONFIG.get(reg, {})
    m = veh.get("mass_agg")
    if m:
        return str(m)
    if pipeline_cfg is None:
        pname = veh.get("pipeline", "default_soc")
        pipeline_cfg = PIPELINE_CONFIGS.get(pname)
    if pipeline_cfg:
        m = pipeline_cfg.get("mass_agg")
        if m:
            return str(m)
    return "mean"
