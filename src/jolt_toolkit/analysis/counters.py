"""Cumulative-counter interpolation at trip endpoints.

Helpers for differencing SRF raw-telematics cumulative energy counters over a
trip's ``[start, end]`` window via shared linear interpolation, so that the
total / propulsion / recuperation latch instants stay consistent.

Provenance
----------
Promoted verbatim from
``data_analysis_workspace/ep_temperature_decomposition/scripts/ep_temp_decomposition.py``
on 2026-06-11, under the sub-project-independence convention (stable analysis
machinery reused by 3+ sub-projects lives in the versioned toolkit so
sub-projects no longer cross-import each other).
"""

# Source: data_analysis_workspace/ep_temperature_decomposition/scripts/ep_temp_decomposition.py
# Promoted: 2026-06-11
# Reason: sub-project-independence convention (shared analysis machinery → versioned toolkit).

from __future__ import annotations

import numpy as np
import pandas as pd

# Raw-telematics cumulative energy counter column names (Wh).
COL_TOTAL = "total_electric_energy_used_plugged_in_included"
COL_PROP = "electric_energy_propulsion"
COL_RECUP = "electric_energy_recuperation_watthours"

# Robustness: per-km energy on short trips is amplified by a tiny denominator;
# drop trips below 3 km (callers additionally winsorise EP_total 1/99 in-vehicle).
MIN_DIST_KM = 3.0


def build_interp(df_tel: pd.DataFrame, col: str):
    """Build (ns time array, cumulative-value array) from raw telematics; return None if the column is missing."""
    if col not in df_tel.columns:
        return None
    t = pd.to_datetime(df_tel["eventDatetime"], errors="coerce", utc=True)
    v = pd.to_numeric(df_tel[col], errors="coerce")
    k = (~t.isna()) & (~v.isna())
    if k.sum() < 2:
        return None
    s = pd.DataFrame({"t": t[k], "v": v[k]}).drop_duplicates("t").sort_values("t")
    times = pd.DatetimeIndex(s["t"]).as_unit("ns").asi8.astype(np.int64)
    return times, s["v"].to_numpy(dtype=float)


def delta(interp, ts: pd.Timestamp, te: pd.Timestamp) -> float:
    """Linearly interpolate the cumulative counter at the [ts, te] endpoints and difference (kWh); return NaN if the window is out of range."""
    if interp is None:
        return np.nan
    times, vals = interp
    a = np.int64(ts.value)
    b = np.int64(te.value)
    if a < times[0] or b > times[-1]:
        return np.nan
    return (np.interp(b, times, vals) - np.interp(a, times, vals)) / 1000.0


def to_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t
