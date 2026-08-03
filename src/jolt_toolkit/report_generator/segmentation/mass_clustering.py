"""
Mass-cluster based trip splitting / merging and energy-anchor recomputation.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .constants import (
    MASS_COL,
    MIN_CLUSTER_GAP_KG,
    MOVING_SPEED_THRESHOLD_KMH,
    ODO_COL,
    SOC_COL,
    TIME_COL,
    TRACTOR_ONLY_MAX_KG,
)
from .timeutil import _to_utc

logger = logging.getLogger(__name__)


def cluster_mass_data(
    df_raw: pd.DataFrame,
    mass_col: str = MASS_COL,
    min_cluster_gap_kg: float = MIN_CLUSTER_GAP_KG,
    speed_col: str | None = None,
    speed_threshold_kmh: float = MOVING_SPEED_THRESHOLD_KMH,
    keep_tractor_only_label: bool = False,
) -> pd.DataFrame:
    """
    Perform 1D clustering of the mass column in the telemetry data, adding a ``mass_cluster`` column to df_raw.

    Algorithm
    ---------
    1. Extract all valid (non-NaN, >0) readings from the mass column.
    2. **Cluster means are computed only from "moving" (speed > ``speed_threshold_kmh``) valid readings**
       (the GCVW broadcast while stationary is unreliable). Sort by value
       and split into separate clusters where the difference between adjacent
       sorted values is ≥ ``min_cluster_gap_kg``.
    3. Compute each cluster's mean; if adjacent cluster means differ by < ``min_cluster_gap_kg``, merge them.
    4. Renumber the clusters from smallest to largest mean: 0 = lowest mass (usually the tractor's own weight).
    5. Assign every **valid** reading (moving + stationary) to its nearest cluster mean.

    Moving flag and fallback
    ---------------------------------
    - New boolean column ``mass_moving``: ``True`` means the row's mass is valid
      and the vehicle was moving (speed > threshold) when sampled. Downstream
      :func:`_get_seg_dominant_cluster` uses this to prefer voting on moving rows.
    - If ``speed_col`` is provided but there are no moving mass readings anywhere
      (sparse-mass vehicles), revert to the old behaviour: cluster on all valid
      readings, logging an info line.
    - If ``speed_col`` is not provided (or the column is missing), ``mass_moving``
      is set to "mass valid" itself, with behaviour identical to the old version.

    Rows with NaN / invalid mass values do not take part in clustering; ``mass_cluster`` stays NaN and ``mass_moving`` is False.

    ``keep_tractor_only_label`` (opt-in; default ``False``)
    -------------------------------------------------------
    When ``True`` the returned copy carries an extra boolean column
    ``mass_tractor_only`` marking exactly the readings that step 4 judges to be
    tractor-only (cluster 0 mean < ``TRACTOR_ONLY_MAX_KG``) and therefore blanks
    from ``mass_cluster``. This lets a caller RECOVER those report-excluded
    ~tractor-weight readings (e.g. the dashboard drill-down, which still wants to
    DISPLAY bare-tractor / bobtail events, just marked distinctly) without any
    effect on the shared segmentation contract. The default (``False``) adds no
    column and leaves the output byte-identical to every existing caller.

    Returns
    -------
    A **copy** of df_raw with the two columns ``mass_cluster`` (int or NaN) and
    ``mass_moving`` (bool) (plus a ``mass_tractor_only`` boolean column when
    ``keep_tractor_only_label=True``).
    """
    df = df_raw.copy()
    df["mass_cluster"] = np.nan
    df["mass_moving"] = False
    if keep_tractor_only_label:
        # Opt-in marker column: default False everywhere; set True only on the
        # rows step 4 drops as tractor-only. Initialised here so every early-return
        # path (missing / all-invalid mass) still exposes the column.
        df["mass_tractor_only"] = False

    if mass_col not in df.columns:
        return df

    mass_numeric = pd.to_numeric(df[mass_col], errors="coerce")
    valid_mask = mass_numeric.notna() & (mass_numeric > 0)
    if valid_mask.sum() == 0:
        return df

    # ── Moving mask: speed > threshold (a NaN speed is treated as not moving) ──
    if speed_col is not None and speed_col in df.columns:
        spd_numeric = pd.to_numeric(df[speed_col], errors="coerce")
        moving_mask = (
            valid_mask & spd_numeric.notna() & (spd_numeric > speed_threshold_kmh)
        )
    else:
        # No speed information: treat all valid readings as "usable for clustering", equivalent to the old behaviour
        moving_mask = valid_mask.copy()

    # Source readings for the cluster means: prefer moving only; fall back to all valid if there are no moving readings anywhere
    if moving_mask.any():
        cluster_source_vals = mass_numeric[moving_mask].values.astype(float)
    else:
        cluster_source_vals = mass_numeric[valid_mask].values.astype(float)
        if speed_col is not None and speed_col in df.columns:
            logger.info(
                "  mass clustering: no moving mass readings anywhere, falling back to clustering on all valid readings "
                "(%d readings)",
                int(valid_mask.sum()),
            )

    # mass_moving column: mass valid + moving (just "mass valid" when there is no speed information)
    df["mass_moving"] = moving_mask

    valid_vals = mass_numeric[valid_mask].values.astype(float)

    # ── 1. Sort and split by gap (based on cluster_source_vals only) ──────────
    sorted_vals = np.sort(cluster_source_vals)
    diffs = np.diff(sorted_vals)
    break_indices = np.where(diffs >= min_cluster_gap_kg)[0]

    # Build the [start, end) slices of each cluster on the sorted array
    slices: list[tuple[int, int]] = []
    start = 0
    for b in break_indices:
        slices.append((start, b + 1))
        start = b + 1
    slices.append((start, len(sorted_vals)))

    # Mean of each cluster
    means = [float(np.mean(sorted_vals[s:e])) for s, e in slices]

    # ── 2. Merge adjacent clusters whose mean difference is < min_cluster_gap_kg ──
    merged_means: list[float] = [means[0]]
    merged_slices: list[tuple[int, int]] = [slices[0]]
    for i in range(1, len(means)):
        if means[i] - merged_means[-1] < min_cluster_gap_kg:
            old_s, _ = merged_slices[-1]
            _, new_e = slices[i]
            merged_slices[-1] = (old_s, new_e)
            merged_means[-1] = float(np.mean(sorted_vals[old_s:new_e]))
        else:
            merged_means.append(means[i])
            merged_slices.append(slices[i])

    # ── 3. Assign each reading to the nearest cluster mean → labels start from 0 (lowest) ──
    means_arr = np.array(merged_means)  # already sorted ascending
    distances = np.abs(valid_vals[:, np.newaxis] - means_arr[np.newaxis, :])
    labels = np.argmin(distances, axis=1).astype(float)

    df.loc[valid_mask, "mass_cluster"] = labels

    # ── 4. Tractor-only detection: treat as tractor-only when cluster 0's mean < threshold ──
    #    These rows' mass_cluster is set to NaN so the later merge/split algorithms ignore them
    if merged_means[0] < TRACTOR_ONLY_MAX_KG:
        tractor_mask = valid_mask & (df["mass_cluster"] == 0)
        if keep_tractor_only_label:
            # Record which readings we are about to blank, so an opted-in caller
            # can recover them (the dashboard displays them, marked tractor-only).
            df.loc[tractor_mask, "mass_tractor_only"] = True
        df.loc[tractor_mask, "mass_cluster"] = np.nan
        logger.info(
            "  mass clustering: cluster 0 mean %.0f kg < %.0f kg, "
            "judged tractor-only, %d readings ignored",
            merged_means[0],
            TRACTOR_ONLY_MAX_KG,
            tractor_mask.sum(),
        )

    return df


def _get_seg_dominant_cluster(
    df_raw: pd.DataFrame,
    t_start: pd.Timestamp,
    t_end: pd.Timestamp,
) -> float | None:
    """Return the most frequent mass_cluster label within the time window [t_start, t_end].

    **Prefer voting on moving** (``mass_moving == True``) mass rows (the
    GCVW broadcast while stationary is unreliable). If the window has no moving
    mass rows, fall back to voting on stationary rows (the user's requirement to
    "still use stationary mass data when no moving data is available"). If the
    ``mass_moving`` column is missing (legacy data / call path), revert to the
    original behaviour: all valid rows vote together.
    """
    if "mass_cluster" not in df_raw.columns:
        return None
    t_s = _to_utc(t_start)
    t_e = _to_utc(t_end)
    times = pd.to_datetime(df_raw[TIME_COL], errors="coerce", utc=True)
    base_mask = (times >= t_s) & (times <= t_e) & df_raw["mass_cluster"].notna()
    # Prefer moving mass rows
    if "mass_moving" in df_raw.columns:
        moving_mask = base_mask & df_raw["mass_moving"].astype(bool)
        moving_vals = df_raw.loc[moving_mask, "mass_cluster"]
        if not moving_vals.empty:
            return float(moving_vals.mode().iloc[0])
        # No moving mass rows in the window → fall back to stationary mass rows
    vals = df_raw.loc[base_mask, "mass_cluster"]
    if vals.empty:
        return None
    return float(vals.mode().iloc[0])


# =============================================================================
# Mass-cluster based trip splitting (load/unload detection)
# =============================================================================


def _split_point_has_zero_speed(
    df_raw: pd.DataFrame,
    t_split: pd.Timestamp,
    speed_col: str,
    window_seconds: float,
) -> bool:
    """
    Check whether a v==0 sample exists within the centre-symmetric neighbourhood [t-W/2, t+W/2] of the split point t_split.

    Physical meaning: mass-cluster based trip splitting is essentially
    "load/unload event" detection. Loading/unloading is necessarily accompanied
    by a stop, so a valid split point must have a strictly zero-speed reading
    nearby (the ±W/2 time window). Otherwise it is treated as a CVW noise spike
    (e.g. a transient mass-reading jump while driving), and the split should be
    abandoned to avoid cutting a single continuous high-speed trip into two.

    Parameters
    ----------
    df_raw         : raw telemetry DataFrame, must contain TIME_COL and speed_col
    t_split        : candidate split-point timestamp
    speed_col      : speed column name (default 'wheel_based_speed', in km/h)
    window_seconds : total width W of the centre-symmetric window (seconds);
                     usually equal to min_stop_duration_min × 60
    """
    if speed_col not in df_raw.columns:
        # Revert to conservative behaviour when there is no speed information: keep the split point (following the old logic)
        return True

    t = _to_utc(t_split)
    half = pd.Timedelta(seconds=window_seconds / 2.0)
    t_lo, t_hi = t - half, t + half

    times = pd.to_datetime(df_raw[TIME_COL], errors="coerce", utc=True)
    mask = (times >= t_lo) & (times <= t_hi)
    if not mask.any():
        return False
    spd = pd.to_numeric(df_raw.loc[mask, speed_col], errors="coerce")
    # Strict v == 0 (the same zero-speed criterion as trip_endpoint_anchor=zero_speed)
    return bool((spd == 0).any())


def _detect_cluster_transitions(
    df_raw: pd.DataFrame,
    t_start: pd.Timestamp,
    t_end: pd.Timestamp,
    speed_col: str | None = None,
    zero_speed_window_seconds: float | None = None,
) -> list[pd.Timestamp]:
    """
    Detect mass_cluster label change points within the time window [t_start, t_end].

    When adjacent **moving** mass readings have different mass_cluster values, it
    is treated as a load/unload event and the discharge segment is split at that
    time point.

    Moving-readings restriction
    ------------------------------------
    Transition-point detection scans only rows with ``mass_moving == True``: the
    J1939 GCVW broadcast while stationary is unreliable, and including it would
    either create false split points at stops (a fragment dropped by
    ``min_soc_drop`` → a chunk of the trip lost) or blur genuine moving-mass
    changes. Consistent with ``cluster_mass_data``'s moving-only means
    and ``_get_seg_dominant_cluster``'s moving-first voting. If the ``mass_moving``
    column is missing (legacy data / call path), fall back to all valid readings.

    Optional zero-speed window filter
    ----------------------------------
    If both ``speed_col`` and ``zero_speed_window_seconds`` are provided, each
    candidate split point must pass ``_split_point_has_zero_speed``: a v=0 sample
    must exist within the ±W/2 window around the split point, otherwise the split
    point is dropped (treated as CVW noise).

    Returns: a list of split time points (UTC). An empty list means no split is needed.
    """
    if "mass_cluster" not in df_raw.columns:
        return []

    t_s = _to_utc(t_start)
    t_e = _to_utc(t_end)
    times = pd.to_datetime(df_raw[TIME_COL], errors="coerce", utc=True)
    mask = (times >= t_s) & (times <= t_e) & df_raw["mass_cluster"].notna()
    # Scan only the "moving" (mass_moving == True) mass readings when detecting
    # cluster-transition split points. A standstill J1939 GCVW broadcast is
    # unreliable (loading/unloading transients, default/last-held values), so a
    # stationary row assigned to a neighbouring cluster would otherwise either
    # (a) fabricate a spurious split at a stop — yielding a tiny sub-segment that
    # gets dropped by min_soc_drop, so a chunk of the trip goes missing — or
    # (b) blur a genuine moving-mass change. This mirrors cluster_mass_data's
    # moving-only cluster means and _get_seg_dominant_cluster's
    # moving-first voting. If mass_moving is absent (legacy data / call path),
    # fall back to the historical all-valid-readings behaviour.
    if "mass_moving" in df_raw.columns:
        mask = mask & df_raw["mass_moving"].astype(bool)
    sub = df_raw.loc[mask, [TIME_COL, "mass_cluster"]].copy()
    sub[TIME_COL] = times[mask]
    sub = sub.sort_values(TIME_COL).reset_index(drop=True)

    if len(sub) < 2:
        return []

    clusters = sub["mass_cluster"].values
    splits: list[pd.Timestamp] = []
    for i in range(1, len(clusters)):
        if clusters[i] != clusters[i - 1]:
            splits.append(sub.iloc[i][TIME_COL])

    # Scheme B: require a v=0 sample near the split point, otherwise treat it as a CVW noise spike and abandon the split
    if splits and speed_col is not None and zero_speed_window_seconds is not None:
        splits = [
            t
            for t in splits
            if _split_point_has_zero_speed(
                df_raw,
                t,
                speed_col,
                zero_speed_window_seconds,
            )
        ]

    return splits


def _split_seg_at_times(
    seg: dict,
    df_raw: pd.DataFrame,
    split_times: list[pd.Timestamp],
    min_soc_drop: float = 5.0,
    min_energy_kwh: float = 2.0,
) -> list[dict]:
    """
    Split one discharge segment into multiple sub-segments at the given time points.

    How the sub-segments' key metrics are computed
    ----------------------------------------------
    - start_soc / end_soc : linearly interpolated from the raw data
    - delta_soc_pct       : end_soc - start_soc (negative)
    - delta_energy_kwh    : allocated proportionally from the parent segment by SOC
    - effective_capacity_kwh : |delta_energy| / (|delta_soc| / 100)
    - odo / lat / lon     : nearest-value lookup from the raw data
    - _anchor_*           : set to None (anchors are meaningless after proportional allocation)

    Sub-segments that do not meet min_soc_drop or min_energy_kwh are dropped.
    If there are ultimately no valid sub-segments, returns [seg] (the original segment).
    """
    t_seg_s = _to_utc(seg["start_time"])
    t_seg_e = _to_utc(seg["end_time"])

    splits_ok = sorted(
        _to_utc(t) for t in split_times if t_seg_s < _to_utc(t) < t_seg_e
    )
    if not splits_ok:
        return [seg]

    df_r = df_raw.copy()
    df_r[TIME_COL] = pd.to_datetime(df_r[TIME_COL], errors="coerce", utc=True)
    df_r = df_r.dropna(subset=[TIME_COL]).sort_values(TIME_COL).reset_index(drop=True)

    if SOC_COL in df_r.columns:
        df_r["_soc_n"] = pd.to_numeric(df_r[SOC_COL], errors="coerce")
        df_r.loc[df_r["_soc_n"] == 0, "_soc_n"] = np.nan
    else:
        df_r["_soc_n"] = np.nan
    soc_valid = df_r[df_r["_soc_n"].notna()].sort_values(TIME_COL)

    if ODO_COL in df_r.columns:
        df_r["_odo_n"] = pd.to_numeric(df_r[ODO_COL], errors="coerce")
        odo_valid = df_r[df_r["_odo_n"].notna()].sort_values(TIME_COL)
    else:
        odo_valid = pd.DataFrame()

    def _soc_at(t: pd.Timestamp) -> float:
        if soc_valid.empty:
            return float("nan")
        before = soc_valid[soc_valid[TIME_COL] <= t]
        after = soc_valid[soc_valid[TIME_COL] >= t]
        if before.empty:
            return float(after.iloc[0]["_soc_n"])
        if after.empty:
            return float(before.iloc[-1]["_soc_n"])
        t0, v0 = before.iloc[-1][TIME_COL], float(before.iloc[-1]["_soc_n"])
        t1, v1 = after.iloc[0][TIME_COL], float(after.iloc[0]["_soc_n"])
        if t0 == t1:
            return v0
        frac = max(0.0, min(1.0, (t - t0).total_seconds() / (t1 - t0).total_seconds()))
        return v0 + frac * (v1 - v0)

    def _odo_at(t: pd.Timestamp) -> float:
        if odo_valid.empty:
            return float("nan")
        before = odo_valid[odo_valid[TIME_COL] <= t]
        return float(before.iloc[-1]["_odo_n"]) if not before.empty else float("nan")

    # Precompute the valid lat/lon subset (avoids repeatedly copying the DataFrame in the loop)
    _ll_valid = pd.DataFrame()
    if "latitude" in df_r.columns and "longitude" in df_r.columns:
        _ll_tmp = df_r[[TIME_COL, "latitude", "longitude"]].copy()
        _ll_tmp["_lat"] = pd.to_numeric(_ll_tmp["latitude"], errors="coerce")
        _ll_tmp["_lon"] = pd.to_numeric(_ll_tmp["longitude"], errors="coerce")
        _ll_valid = _ll_tmp[
            _ll_tmp["_lat"].notna() & (_ll_tmp["_lat"] != 0) & _ll_tmp["_lon"].notna()
        ].sort_values(TIME_COL)

    def _latlon_at(t: pd.Timestamp, side: str = "start"):
        if _ll_valid.empty:
            return None, None
        subset = (
            _ll_valid[_ll_valid[TIME_COL] >= t]
            if side == "start"
            else _ll_valid[_ll_valid[TIME_COL] <= t]
        )
        row = (
            subset.iloc[0]
            if (side == "start" and not subset.empty)
            else (
                subset.iloc[-1]
                if (side == "end" and not subset.empty)
                else _ll_valid.iloc[-1 if side == "end" else 0]
            )
        )
        return round(float(row["_lat"]), 6), round(float(row["_lon"]), 6)

    boundaries = [t_seg_s] + splits_ok + [t_seg_e]
    orig_dsoc = float(seg["delta_soc_pct"])  # negative
    orig_denergy = float(seg["delta_energy_kwh"])  # negative

    result: list[dict] = []
    for k in range(len(boundaries) - 1):
        sub_t_s = boundaries[k]
        sub_t_e = boundaries[k + 1]
        is_first = k == 0
        is_last = k == len(boundaries) - 2

        sub_soc_s = float(seg["start_soc"]) if is_first else _soc_at(sub_t_s)
        sub_soc_e = float(seg["end_soc"]) if is_last else _soc_at(sub_t_e)
        if np.isnan(sub_soc_s) or np.isnan(sub_soc_e):
            continue

        sub_dsoc = sub_soc_e - sub_soc_s  # should be negative
        if -sub_dsoc < min_soc_drop:
            continue

        # Allocate energy proportionally by SOC
        if orig_dsoc != 0 and np.isfinite(orig_denergy):
            sub_denergy = orig_denergy * (sub_dsoc / orig_dsoc)
        else:
            sub_denergy = float("nan")
        if np.isnan(sub_denergy) or abs(sub_denergy) < min_energy_kwh:
            continue

        sub_effcap = (
            abs(sub_denergy) / (abs(sub_dsoc) / 100.0)
            if abs(sub_dsoc) > 0
            else float("nan")
        )

        sub_odo_s = (
            float(seg.get("odo_start_km") or float("nan"))
            if is_first
            else _odo_at(sub_t_s)
        )
        sub_odo_e = (
            float(seg.get("odo_end_km") or float("nan"))
            if is_last
            else _odo_at(sub_t_e)
        )

        lat_s, lon_s = (
            (seg.get("lat_start"), seg.get("lon_start"))
            if is_first
            else _latlon_at(sub_t_s, "start")
        )
        lat_e, lon_e = (
            (seg.get("lat_end"), seg.get("lon_end"))
            if is_last
            else _latlon_at(sub_t_e, "end")
        )

        result.append(
            {
                "start_time": sub_t_s,
                "end_time": sub_t_e,
                "start_soc": round(sub_soc_s, 2),
                "end_soc": round(sub_soc_e, 2),
                "delta_soc_pct": round(sub_dsoc, 2),
                "delta_energy_kwh": round(sub_denergy, 3),
                "energy_source": seg.get("energy_source"),
                "delta_moving_kwh": None,
                "effective_capacity_kwh": (
                    round(sub_effcap, 1) if np.isfinite(sub_effcap) else None
                ),
                "odo_start_km": round(sub_odo_s, 3) if np.isfinite(sub_odo_s) else None,
                "odo_end_km": round(sub_odo_e, 3) if np.isfinite(sub_odo_e) else None,
                "lat_start": lat_s,
                "lon_start": lon_s,
                "lat_end": lat_e,
                "lon_end": lon_e,
                "_anchor_start_time": None,
                "_anchor_end_time": None,
                "_anchor_start_rel_kwh": float("nan"),
                "_anchor_end_rel_kwh": float("nan"),
            }
        )

    return result if result else [seg]


def split_discharge_by_mass(
    discharge_segs: list[dict],
    df_raw: pd.DataFrame,
    min_soc_drop: float = 5.0,
    min_energy_kwh: float = 2.0,
    speed_col: str | None = None,
    zero_speed_window_seconds: float | None = None,
) -> list[dict]:
    """
    Split discharge segments based on mass-cluster label changes.

    Precondition: df_raw has had the ``mass_cluster`` column added by ``cluster_mass_data()``.

    For each discharge segment, detect whether mass_cluster changes within its
    time window; if so, split the segment at that time point into multiple
    sub-segments (each with a consistent mass cluster). Segments with no detected
    change are left unchanged.

    Optional zero-speed split-point filter (Scheme B)
    -------------------------------------------------
    If both ``speed_col`` and ``zero_speed_window_seconds`` are provided, a
    candidate split point is accepted only if a v=0 sample exists within the ±W/2
    time window around it; otherwise it is treated as a CVW noise spike and the
    split point is dropped. This avoids cutting a single trip into two because of
    a transient mass-reading jump during continuous high-speed driving (e.g. the
    EX74JXW 07-17_0008 case).

    Parameters
    ----------
    discharge_segs            : list of discharge segments
    df_raw                    : raw telemetry DataFrame with the mass_cluster column
    min_soc_drop              : minimum SOC drop (%) for a sub-segment, dropped below this
                                (default 5.0)
    min_energy_kwh            : minimum energy (kWh) for a sub-segment, dropped below this (default 2.0)
    speed_col                 : speed column name, used for the zero-speed window check (optional)
    zero_speed_window_seconds : total width W of the zero-speed window (seconds), usually =
                                min_stop_duration_min × 60 (optional)
    """
    result: list[dict] = []
    for seg in discharge_segs:
        splits = _detect_cluster_transitions(
            df_raw,
            seg["start_time"],
            seg["end_time"],
            speed_col=speed_col,
            zero_speed_window_seconds=zero_speed_window_seconds,
        )
        if not splits:
            result.append(seg)
        else:
            sub_segs = _split_seg_at_times(
                seg,
                df_raw,
                splits,
                min_soc_drop=min_soc_drop,
                min_energy_kwh=min_energy_kwh,
            )
            result.extend(sub_segs)
    return result


# =============================================================================
# Recompute missing energy anchors
# =============================================================================


def _recompute_anchors(
    segments: list[dict],
    df_raw: pd.DataFrame,
    total_energy_col: str,
    moving_energy_col: str,
) -> None:
    """
    Recompute energy anchors for segments missing an anchor (modifies in place).

    After split_discharge_by_mass splits a segment, the sub-segments' anchors are
    cleared (because the energy is allocated proportionally by SOC and no longer
    corresponds to actual energy-counter readings). This function re-looks-up the
    nearest energy-counter readings based on each sub-segment's energy_source,
    restoring the anchors for validation-figure annotations.
    """
    if not segments:
        return

    df = df_raw.copy()
    df[TIME_COL] = pd.to_datetime(df[TIME_COL], errors="coerce", utc=True)
    df = df.dropna(subset=[TIME_COL]).sort_values(TIME_COL).reset_index(drop=True)
    times_np = df[TIME_COL].values.astype("datetime64[ns]")

    has_total = total_energy_col in df.columns
    has_moving = moving_energy_col in df.columns

    tot_base = mov_base = 0.0
    if has_total:
        df["_tot"] = pd.to_numeric(df[total_energy_col], errors="coerce")
        _tv = df.loc[df["_tot"].notna(), "_tot"]
        if len(_tv):
            tot_base = float(_tv.iloc[0])
    if has_moving:
        df["_mov"] = pd.to_numeric(df[moving_energy_col], errors="coerce")
        _mv = df.loc[df["_mov"].notna(), "_mov"]
        if len(_mv):
            mov_base = float(_mv.iloc[0])

    def _nb(col: str, t_np):
        mask = df[col].notna() & (times_np <= t_np)
        idx = df.index[mask]
        if len(idx) == 0:
            return np.nan, None
        i = idx[-1]
        return float(df.loc[i, col]), pd.Timestamp(times_np[i])

    def _na(col: str, t_np):
        mask = df[col].notna() & (times_np >= t_np)
        idx = df.index[mask]
        if len(idx) == 0:
            return np.nan, None
        i = idx[0]
        return float(df.loc[i, col]), pd.Timestamp(times_np[i])

    for seg in segments:
        # Skip segments that already have valid anchors
        a_st = seg.get("_anchor_start_time")
        a_et = seg.get("_anchor_end_time")
        a_sv = seg.get("_anchor_start_rel_kwh", float("nan"))
        a_ev = seg.get("_anchor_end_rel_kwh", float("nan"))
        if (
            a_st is not None
            and a_et is not None
            and not np.isnan(a_sv)
            and not np.isnan(a_ev)
        ):
            continue

        t_s = pd.Timestamp(seg["start_time"]).asm8.astype("datetime64[ns]")
        t_e = pd.Timestamp(seg["end_time"]).asm8.astype("datetime64[ns]")
        src = seg.get("energy_source", "")

        if src == "total_energy" and has_total:
            e_s, t_es = _nb("_tot", t_s)
            e_e, t_ee = _na("_tot", t_e)
            if not np.isnan(e_s) and not np.isnan(e_e):
                seg["_anchor_start_time"] = t_es
                seg["_anchor_end_time"] = t_ee
                seg["_anchor_start_rel_kwh"] = round((e_s - tot_base) / 1000.0, 4)
                seg["_anchor_end_rel_kwh"] = round((e_e - tot_base) / 1000.0, 4)
        elif src == "moving_energy" and has_moving:
            e_s, t_es = _nb("_mov", t_s)
            e_e, t_ee = _na("_mov", t_e)
            if not np.isnan(e_s) and not np.isnan(e_e):
                seg["_anchor_start_time"] = t_es
                seg["_anchor_end_time"] = t_ee
                seg["_anchor_start_rel_kwh"] = round((e_s - mov_base) / 1000.0, 4)
                seg["_anchor_end_rel_kwh"] = round((e_e - mov_base) / 1000.0, 4)


# =============================================================================
# Merge adjacent discharge segments by mass similarity
# =============================================================================


def _merge_two_discharge_segs(seg_a: dict, seg_b: dict) -> dict:
    """Merge two adjacent discharge segments into one, for internal use by merge_discharge_by_mass."""
    soc_s = float(seg_a["start_soc"])
    soc_e = float(seg_b["end_soc"])
    dsoc = soc_e - soc_s  # negative

    # Sum energies (both negative for discharge)
    e_a = seg_a.get("delta_energy_kwh")
    e_b = seg_b.get("delta_energy_kwh")
    denergy = float("nan")
    if e_a is not None and e_b is not None:
        fa, fb = float(e_a), float(e_b)
        if np.isfinite(fa) and np.isfinite(fb):
            denergy = fa + fb

    effcap = float("nan")
    if np.isfinite(denergy) and abs(dsoc) > 0:
        effcap = abs(denergy) / (abs(dsoc) / 100.0)

    # Prefer higher-priority energy source
    _prio = {"total_energy": 0, "moving_energy": 1, "soc_estimate": 2}
    src_a = seg_a.get("energy_source", "soc_estimate")
    src_b = seg_b.get("energy_source", "soc_estimate")
    src = src_a if _prio.get(src_a, 9) <= _prio.get(src_b, 9) else src_b

    return {
        "start_time": seg_a["start_time"],
        "end_time": seg_b["end_time"],
        "start_soc": soc_s,
        "end_soc": soc_e,
        "delta_soc_pct": round(dsoc, 2),
        "delta_energy_kwh": round(denergy, 3) if np.isfinite(denergy) else None,
        "energy_source": src,
        "delta_moving_kwh": None,
        "effective_capacity_kwh": round(effcap, 1) if np.isfinite(effcap) else None,
        "odo_start_km": seg_a.get("odo_start_km"),
        "odo_end_km": seg_b.get("odo_end_km"),
        "lat_start": seg_a.get("lat_start"),
        "lon_start": seg_a.get("lon_start"),
        "lat_end": seg_b.get("lat_end"),
        "lon_end": seg_b.get("lon_end"),
        "_anchor_start_time": seg_a.get("_anchor_start_time"),
        "_anchor_end_time": seg_b.get("_anchor_end_time"),
        "_anchor_start_rel_kwh": seg_a.get("_anchor_start_rel_kwh", float("nan")),
        "_anchor_end_rel_kwh": seg_b.get("_anchor_end_rel_kwh", float("nan")),
    }


def merge_discharge_by_mass(
    discharge_segs: list[dict],
    df_raw: pd.DataFrame,
    charge_segs: list[dict] | None = None,
    max_merge_gap_min: float | None = None,
) -> list[dict]:
    """
    Merge adjacent discharge segments based on mass-cluster labels.

    Precondition: df_raw has had the ``mass_cluster`` column added by ``cluster_mass_data()``.

    Merge conditions (all must hold):
    - Adjacent segments have the same dominant mass_cluster (same mass class)
    - No charge segment in the gap
    - The gap (stationary duration) < ``max_merge_gap_min`` (if provided)

    Different cluster → treated as a load/unload event, kept separate.

    Long-stationary split (optional, opt-in)
    ----------------------------------------
    Speed segmentation (``find_speed_trips``) has already cut gaps between driving
    periods with a stop >= ``min_stop_duration_min`` into adjacent trips; this
    function then re-merges adjacent trips of the same mass cluster with no charge
    in the gap back into a single long trip. When ``max_merge_gap_min`` is
    provided, if the stationary gap between two adjacent trips is >= that
    threshold (minutes), it is treated as a trip boundary and merging is refused —
    even if the mass is unchanged. Use: cut "drive → long stop → drive again" into
    two trips (e.g. the Nestlé vehicle YK73WFN's mid-day stops of several hours).
    ``None`` (default) = disabled, behaviour identical to history.

    Parameters
    ----------
    discharge_segs    : list of discharge segments
    df_raw            : raw telemetry DataFrame with the mass_cluster column
    charge_segs       : list of charge segments; blocks merging if a charge exists in the gap (default None)
    max_merge_gap_min : maximum stationary gap (minutes) allowed for merging two
                        adjacent trips. A gap >= this value refuses the merge
                        (keeps them as two trips). ``None`` = not applied
                        (default, backward compatible; enabled only for the Nestlé
                        vehicles via vehicles.json ``split_long_stops_min``).
    """
    if len(discharge_segs) <= 1 or "mass_cluster" not in df_raw.columns:
        return discharge_segs

    # Pre-convert the charge-segment times
    charge_intervals: list[tuple] = []
    if charge_segs:
        for c in charge_segs:
            try:
                charge_intervals.append(
                    (_to_utc(c["start_time"]), _to_utc(c["end_time"]))
                )
            except Exception:
                pass

    def _has_charge_in_gap(gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> bool:
        for c_s, c_e in charge_intervals:
            if c_s < gap_end and c_e > gap_start:
                return True
        return False

    # Dominant mass_cluster of each segment
    seg_clusters = [
        _get_seg_dominant_cluster(df_raw, s["start_time"], s["end_time"])
        for s in discharge_segs
    ]

    result: list[dict] = []
    i = 0
    while i < len(discharge_segs):
        seg = discharge_segs[i].copy()
        c_cur = seg_clusters[i]
        j = i + 1

        while j < len(discharge_segs):
            c_next = seg_clusters[j]
            # Unknown cluster → conservatively do not merge
            if c_cur is None or c_next is None:
                break
            # Different cluster → load/unload event, keep separate
            if c_cur != c_next:
                break
            # Charge in the gap → do not merge
            gap_start = _to_utc(seg["end_time"])
            gap_end = _to_utc(discharge_segs[j]["start_time"])
            if _has_charge_in_gap(gap_start, gap_end):
                break
            # Long stationary → trip boundary (opt-in, Nestlé only): even with
            # unchanged mass and no charge in the gap, refuse the merge if the
            # stationary gap >= max_merge_gap_min.
            if max_merge_gap_min is not None:
                _gap_min = (gap_end - gap_start).total_seconds() / 60.0
                if _gap_min >= max_merge_gap_min:
                    break
            # Same cluster, no charge, gap not long → merge
            seg = _merge_two_discharge_segs(seg, discharge_segs[j])
            j += 1

        result.append(seg)
        i = j

    return result


# =============================================================================
# Enforce non-overlapping anchors (correct the double-counting of energy between adjacent segments caused by a sparse cumulative counter)
# =============================================================================


def _enforce_anchor_ordering(discharge_segs: list[dict], reg: str = "") -> int:
    """Enforce non-overlapping energy anchors on adjacent discharge segments (previous anchor_end <= next anchor_start).

    Background
    ----------
    In the speed branch each trip's energy is computed with
    ``_nearest_before(total_energy, trip_start)`` →
    ``_nearest_after(total_energy, trip_end)``. ``total_energy`` is a cumulative
    counter of sparse readings: if there is no counter reading between the end of
    one trip and the next, ``_nearest_after(trip_end)`` jumps to a reading
    "during/after the next trip", so this segment's energy is double-counted into
    the next segment's energy and causes the time overlap
    ``anchor_end(i) > anchor_start(i+1)`` → ``delta_energy_kwh`` /
    ``effective_capacity_kwh`` / EP are overestimated.

    This post-processing runs after ``merge_discharge_by_mass`` +
    ``_recompute_anchors``, on the **final** discharge segments: it clamps the
    overlapping previous segment's ``_anchor_end_time`` to the next segment's
    ``_anchor_start_time`` and recomputes ``delta_energy_kwh`` and
    ``effective_capacity_kwh`` from the anchor relative values (already in kWh).
    Only segments with an actual overlap are modified (the sparse-counter case);
    when the counter has readings in the gap there is no overlap → no change.

    Relationship between the anchor relative values and energy (see find_discharge_segments_by_speed):
        delta_energy_kwh = -(anchor_end_rel_kwh - anchor_start_rel_kwh)
    (both zeroed relative to the leg start, in kWh; discharge is negative).

    Modifies the segment dicts in ``discharge_segs`` in place; returns the number of clamped segments (for the regen log stats).
    """
    if not discharge_segs or len(discharge_segs) < 2:
        return 0

    # 1. Defensively sort by start_time (only to determine adjacency; the segment
    #    dicts are shared references, so in-place modification propagates back to
    #    discharge_segs without changing the caller list's original order).
    segs = sorted(discharge_segs, key=lambda s: _to_utc(s["start_time"]))

    def _usable_anchor(s: dict) -> bool:
        # soc_estimate segments have no real counter anchor (rel is NaN) → skip
        if s.get("energy_source") == "soc_estimate":
            return False
        if s.get("_anchor_start_time") is None or s.get("_anchor_end_time") is None:
            return False
        a_sv = s.get("_anchor_start_rel_kwh", float("nan"))
        a_ev = s.get("_anchor_end_rel_kwh", float("nan"))
        try:
            return bool(np.isfinite(a_sv) and np.isfinite(a_ev))
        except (TypeError, ValueError):
            return False

    n_clamped = 0
    for k in range(len(segs) - 1):
        cur = segs[k]
        nxt = segs[k + 1]
        if not (_usable_anchor(cur) and _usable_anchor(nxt)):
            continue

        cur_end = _to_utc(cur["_anchor_end_time"])
        nxt_start = _to_utc(nxt["_anchor_start_time"])
        if cur_end <= nxt_start:
            continue  # No overlap → leave unchanged (the normal case where the counter has readings in the gap)

        # Overlap → clamp the previous segment's anchor_end to the next segment's anchor_start and recompute energy from the anchor relative values
        new_end_rel = nxt["_anchor_start_rel_kwh"]
        new_delta = -(new_end_rel - cur["_anchor_start_rel_kwh"])
        if not (new_delta < 0):
            # No longer a valid discharge after clamping (anchor collapse / pathological) → keep the original segment unchanged, only warn
            logger.warning(
                "[%s] anchor overlap clamp skipped (would collapse to "
                "non-discharge): cur trip start=%s, anchor_end %s > next "
                "anchor_start %s, new_delta=%.3f",
                reg,
                cur.get("start_time"),
                cur_end,
                nxt_start,
                new_delta,
            )
            continue

        cur["_anchor_end_time"] = nxt["_anchor_start_time"]
        cur["_anchor_end_rel_kwh"] = new_end_rel
        cur["delta_energy_kwh"] = round(new_delta, 3)
        dsoc_abs = abs(cur.get("delta_soc_pct") or 0.0)
        if dsoc_abs > 0:
            cur["effective_capacity_kwh"] = round(
                abs(new_delta) / (dsoc_abs / 100.0), 1
            )
        n_clamped += 1
        logger.info(
            "[%s] anchor overlap clamped: cur trip start=%s, anchor_end %s → %s "
            "(next anchor_start); delta_energy_kwh=%s, eff_cap=%s",
            reg,
            cur.get("start_time"),
            cur_end,
            nxt_start,
            cur["delta_energy_kwh"],
            cur.get("effective_capacity_kwh"),
        )

    return n_clamped
