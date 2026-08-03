"""
Speed-based trip / discharge segmentation.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .constants import (
    MOVING_COL,
    ODO_COL,
    SOC_COL,
    TIME_COL,
    TOTAL_ENERGY_COL,
)


# =============================================================================
# Speed-based discharge trip segmentation
# =============================================================================
def _extend_trip_endpoint_to_zero(
    times_ns: np.ndarray,
    spd_arr: np.ndarray,
    idx0: int,
    direction: str,
    max_extend_ns: int,
) -> int:
    """
    Extend a trip endpoint outward to the nearest v == 0 sample (for the zero_speed anchor mode).

    Parameters
    ----------
    times_ns    : full-leg timestamps (datetime64[ns] view as int64)
    spd_arr     : full-leg speed array (NaN already filled with 0; same length as times_ns)
    idx0        : current endpoint index (position of the first/last moving sample in df)
    direction   : 'backward' (extend the trip start earlier) or 'forward' (extend the trip end later)
    max_extend_ns : maximum extension window (nanoseconds); beyond this, give up the extension and fall back to idx0

    Returns
    -------
    The extended endpoint index; returns idx0 if no v == 0 sample is found within the window (fallback behaviour).
    """
    n = len(times_ns)
    t0 = times_ns[idx0]
    if direction == "backward":
        j = idx0
        while j > 0:
            j -= 1
            if (t0 - times_ns[j]) > max_extend_ns:
                return idx0  # beyond the window, fall back
            if spd_arr[j] == 0:
                return j
        return idx0  # reached the leg start without hitting v==0
    else:  # 'forward'
        j = idx0
        while j < n - 1:
            j += 1
            if (times_ns[j] - t0) > max_extend_ns:
                return idx0  # beyond the window, fall back
            if spd_arr[j] == 0:
                return j
        return idx0  # reached the leg end without hitting v==0


def find_speed_trips(
    df_raw: pd.DataFrame,
    speed_col: str = "wheel_based_speed",
    speed_threshold_kmh: float = 1.0,
    min_stop_duration_min: float = 5.0,
    min_trip_duration_min: float = 2.0,
    trip_endpoint_anchor: str = "zero_speed",
    max_extend_minutes: float = 5.0,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """
    Detect trip start/end times from vehicle speed.

    Parameters
    ----------
    df_raw              : raw telemetry DataFrame (must contain the TIME_COL and speed_col columns)
    speed_col           : speed column name (km/h)
    speed_threshold_kmh : speed above this value counts as moving
    min_stop_duration_min : a trip ends only when continuous zero speed exceeds this duration (minutes);
                            shorter zero-speed intervals are bridged (e.g. red lights, brief stops)
    min_trip_duration_min : discard trips shorter than this duration (minutes) as noise
    trip_endpoint_anchor : trip endpoint anchoring strategy:
        - 'zero_speed' (default): after split + merge + filtering,
                                  extend the endpoints outward to the nearest
                                  v == 0 sample (within max_extend_minutes minutes).
                                  Purpose: let the trip window fully cover the
                                  zero-speed tails on low-frequency telemetry
                                  heartbeats, avoiding start/end times landing on
                                  transient points such as 76 km/h. Now the
                                  fleet-wide standard.
        - 'first_motion' (opt-out / legacy): endpoints = the first/last
                                  v > speed_threshold_kmh sample within the trip
                                  (the legacy behaviour, kept for
                                  backward compatibility / per-pipeline override).
    max_extend_minutes  : maximum window (minutes) for extending the endpoints in
                          zero_speed mode; beyond this, silently fall back to the
                          first_motion endpoints. Only in effect when anchor==zero_speed.

    Returns
    -------
    [(trip_start, trip_end), ...] — a time-sorted list of trip time windows.
    Returns an empty list if the speed column is absent or entirely invalid.
    """
    if TIME_COL not in df_raw.columns:
        return []
    if speed_col not in df_raw.columns:
        return []

    df = df_raw[[TIME_COL, speed_col]].copy()
    df[TIME_COL] = pd.to_datetime(df[TIME_COL], errors="coerce", utc=True)
    df = df.dropna(subset=[TIME_COL]).sort_values(TIME_COL).reset_index(drop=True)
    if df.empty:
        return []

    # Speed: NaN → 0
    df["_spd"] = pd.to_numeric(df[speed_col], errors="coerce").fillna(0.0)

    # No trips if the speed column is all 0
    if (df["_spd"] <= speed_threshold_kmh).all():
        return []

    # Mark the moving state
    df["_moving"] = df["_spd"] > speed_threshold_kmh
    times = df[TIME_COL].values.astype("datetime64[ns]")
    moving = df["_moving"].values

    # Find contiguous moving blocks
    raw_trips: list[tuple[int, int]] = []  # (first_moving_idx, last_moving_idx)
    i = 0
    n = len(df)
    while i < n:
        if moving[i]:
            start = i
            while i < n and moving[i]:
                i += 1
            raw_trips.append((start, i - 1))
        else:
            i += 1

    if not raw_trips:
        return []

    # Bridge: merge two moving blocks if the zero-speed gap between them < min_stop_duration_min
    min_stop_ns = int(min_stop_duration_min * 60 * 1_000_000_000)
    merged_trips: list[tuple[int, int]] = [raw_trips[0]]
    for trip_s, trip_e in raw_trips[1:]:
        prev_e = merged_trips[-1][1]
        gap_ns = int(times[trip_s] - times[prev_e])
        if gap_ns <= min_stop_ns:
            # Bridge: extend the previous trip to the current trip's end
            merged_trips[-1] = (merged_trips[-1][0], trip_e)
        else:
            merged_trips.append((trip_s, trip_e))

    # Filter out short trips
    min_trip_ns = int(min_trip_duration_min * 60 * 1_000_000_000)
    spd_arr = df["_spd"].values
    max_extend_ns = int(max_extend_minutes * 60 * 1_000_000_000)
    use_zero_anchor = trip_endpoint_anchor == "zero_speed"

    result: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for trip_s, trip_e in merged_trips:
        # Trip-length filter is based on the original first_motion endpoints
        if int(times[trip_e] - times[trip_s]) < min_trip_ns:
            continue

        s_idx, e_idx = trip_s, trip_e
        if use_zero_anchor:
            # Extend the endpoints outward to the nearest v==0 sample, falling back beyond max_extend_ns
            times_ns_int = times.view("i8")
            s_idx = _extend_trip_endpoint_to_zero(
                times_ns_int,
                spd_arr,
                trip_s,
                "backward",
                max_extend_ns,
            )
            e_idx = _extend_trip_endpoint_to_zero(
                times_ns_int,
                spd_arr,
                trip_e,
                "forward",
                max_extend_ns,
            )

        result.append((pd.Timestamp(times[s_idx]), pd.Timestamp(times[e_idx])))

    return result


def find_discharge_segments_by_speed(
    df_raw: pd.DataFrame,
    speed_col: str = "wheel_based_speed",
    speed_threshold_kmh: float = 1.0,
    min_stop_duration_min: float = 5.0,
    min_trip_duration_min: float = 2.0,
    min_soc_drop: float = 1.0,
    min_energy_kwh: float = 1.0,
    cap_lo: float | None = None,
    cap_hi: float | None = None,
    total_energy_col: str = TOTAL_ENERGY_COL,
    moving_energy_col: str = MOVING_COL,
    nominal_kwh: float | None = None,
    trips: list[tuple] | None = None,
    trip_endpoint_anchor: str = "zero_speed",
    max_extend_minutes: float = 5.0,
) -> list[dict]:
    """
    Speed-based discharge trip segmentation: detect trip boundaries from speed, use SOC/energy to compute metrics.

    The output schema is identical to find_discharge_segments_by_soc() (v2 unified
    schema), so downstream logic (report_builder / merge_discharge_by_mass, etc.)
    needs no modification.

    Differences from SOC-based discharge segmentation:
    - Trip boundaries are defined by the speed signal (more precise) rather than the SOC decline trend
    - Uses looser min_soc_drop (default 1.0 vs 5.0) and min_energy_kwh (1.0 vs 2.0)
    - The energy-source cascade logic is identical: total_energy → moving_energy → soc_estimate

    Parameters
    ----------
    See the parameter descriptions in find_speed_trips() and find_discharge_segments_by_soc().

    Returns
    -------
    list[dict] — the same list of segment dicts as find_discharge_segments_by_soc().
    Returns an empty list if the speed column is missing or all-zero (no trips) (the caller may fall back to SOC-based).
    """
    # 1. Obtain the speed-defined trip time windows (externally precomputed trips are accepted)
    if trips is None:
        trips = find_speed_trips(
            df_raw,
            speed_col=speed_col,
            speed_threshold_kmh=speed_threshold_kmh,
            min_stop_duration_min=min_stop_duration_min,
            min_trip_duration_min=min_trip_duration_min,
            trip_endpoint_anchor=trip_endpoint_anchor,
            max_extend_minutes=max_extend_minutes,
        )
    if not trips:
        return []

    # 2. Prepare the data columns
    if SOC_COL not in df_raw.columns or TIME_COL not in df_raw.columns:
        return []

    df = df_raw.copy()
    df[TIME_COL] = pd.to_datetime(df[TIME_COL], errors="coerce", utc=True)
    df = df.dropna(subset=[TIME_COL]).sort_values(TIME_COL).reset_index(drop=True)
    if df.empty:
        return []

    df["_soc"] = pd.to_numeric(df[SOC_COL], errors="coerce")
    df.loc[df["_soc"] == 0, "_soc"] = np.nan

    has_total = total_energy_col in df.columns
    has_moving = moving_energy_col in df.columns

    if has_total:
        df["_tot"] = pd.to_numeric(df[total_energy_col], errors="coerce")
    if has_moving:
        df["_mov"] = pd.to_numeric(df[moving_energy_col], errors="coerce")

    if ODO_COL in df.columns:
        df["_odo"] = pd.to_numeric(df[ODO_COL], errors="coerce")
    else:
        df["_odo"] = np.nan

    for _c in ("latitude", "longitude"):
        if _c in df.columns:
            df[_c] = pd.to_numeric(df[_c], errors="coerce")
            df.loc[df[_c] == 0, _c] = np.nan

    # Speed column (used to compute the cumulative v>0 sub-interval duration in zero_speed anchor mode)
    if speed_col in df.columns:
        df["_spd"] = pd.to_numeric(df[speed_col], errors="coerce").fillna(0.0)
    else:
        df["_spd"] = 0.0

    times_np = df[TIME_COL].values.astype("datetime64[ns]")

    # Total-energy baseline (used for the anchor relative values)
    tot_base_wh = 0.0
    if has_total:
        _tot_valid = df.loc[df["_tot"].notna(), "_tot"]
        if len(_tot_valid):
            tot_base_wh = float(_tot_valid.iloc[0])

    mov_base_wh = 0.0
    if has_moving:
        _mov_valid = df.loc[df["_mov"].notna(), "_mov"]
        if len(_mov_valid):
            mov_base_wh = float(_mov_valid.iloc[0])

    def _nearest_before(col_name: str, t_np):
        """Find the nearest valid value at or before time t_np."""
        mask = df[col_name].notna() & (times_np <= t_np)
        idx = df.index[mask]
        if len(idx) == 0:
            return np.nan, None
        i = idx[-1]
        return float(df.loc[i, col_name]), pd.Timestamp(times_np[i])

    def _nearest_after(col_name: str, t_np):
        """Find the nearest valid value at or after time t_np."""
        mask = df[col_name].notna() & (times_np >= t_np)
        idx = df.index[mask]
        if len(idx) == 0:
            return np.nan, None
        i = idx[0]
        return float(df.loc[i, col_name]), pd.Timestamp(times_np[i])

    # 3. Compute segment metrics for each trip
    segments: list[dict] = []
    for trip_start, trip_end in trips:
        t_s = trip_start.to_numpy().astype("datetime64[ns]")
        t_e = trip_end.to_numpy().astype("datetime64[ns]")

        # SOC: first and last valid reading within the trip window
        win_mask = (times_np >= t_s) & (times_np <= t_e)
        win_soc = df.loc[win_mask & df["_soc"].notna(), "_soc"]
        if len(win_soc) < 1:
            # No SOC within the window → try SOC near the window boundaries
            soc_s, _ = _nearest_before("_soc", t_s)
            soc_e, _ = _nearest_after("_soc", t_e)
        else:
            soc_s = float(win_soc.iloc[0])
            soc_e = float(win_soc.iloc[-1])

        # SOC change
        has_soc = not (np.isnan(soc_s) or np.isnan(soc_e))
        if has_soc:
            delta_soc_signed = soc_e - soc_s  # negative = discharge
            delta_soc_abs = soc_s - soc_e  # positive = drop magnitude
        else:
            delta_soc_signed = 0.0
            delta_soc_abs = 0.0

        # SOC-change filter: drop trips with insufficient SOC decline
        if has_soc and delta_soc_abs < min_soc_drop:
            continue

        # ── delta_energy_kwh: energy-source cascade ────────────────────
        # In speed segmentation the trip is already confirmed by speed, so prefer the energy counters (higher precision than SOC).
        delta_energy_kwh = None
        energy_source = None
        anchor_s_time = anchor_e_time = None
        anchor_s_rel = anchor_e_rel = float("nan")

        # 1. Total energy col (preferred)
        if has_total:
            e_s, t_es = _nearest_before("_tot", t_s)
            e_e, t_ee = _nearest_after("_tot", t_e)
            if not np.isnan(e_s) and not np.isnan(e_e):
                _raw = (e_e - e_s) / 1000.0
                if _raw > 0:
                    delta_energy_kwh = -_raw
                    energy_source = "total_energy"
                    anchor_s_time = t_es
                    anchor_e_time = t_ee
                    anchor_s_rel = round((e_s - tot_base_wh) / 1000.0, 4)
                    anchor_e_rel = round((e_e - tot_base_wh) / 1000.0, 4)

        # 2. Moving energy col (fallback)
        if delta_energy_kwh is None and has_moving:
            e_s, t_es = _nearest_before("_mov", t_s)
            e_e, t_ee = _nearest_after("_mov", t_e)
            if not np.isnan(e_s) and not np.isnan(e_e):
                _raw = (e_e - e_s) / 1000.0
                if _raw > 0:
                    delta_energy_kwh = -_raw
                    energy_source = "moving_energy"
                    anchor_s_time = t_es
                    anchor_e_time = t_ee
                    anchor_s_rel = round((e_s - mov_base_wh) / 1000.0, 4)
                    anchor_e_rel = round((e_e - mov_base_wh) / 1000.0, 4)

        # 3. SOC estimate (last resort) — used only when SOC has an actual decline
        if delta_energy_kwh is None and nominal_kwh is not None and delta_soc_abs > 0:
            delta_energy_kwh = (delta_soc_signed / 100.0) * nominal_kwh
            energy_source = "soc_estimate"
            anchor_s_time = trip_start
            anchor_e_time = trip_end
            anchor_s_rel = float("nan")
            anchor_e_rel = float("nan")

        if delta_energy_kwh is None:
            continue
        if abs(delta_energy_kwh) < min_energy_kwh or delta_energy_kwh >= 0:
            continue

        # Effective capacity: computable only when SOC has an actual decline
        if delta_soc_abs > 0:
            eff_cap = abs(delta_energy_kwh) / (delta_soc_abs / 100.0)
            if cap_lo is not None and cap_hi is not None:
                if not (cap_lo <= eff_cap <= cap_hi):
                    continue
        else:
            eff_cap = None

        # delta_moving_kwh (independent of the primary energy source)
        delta_moving = None
        if has_moving:
            e_ms, _ = _nearest_before("_mov", t_s)
            e_me, _ = _nearest_after("_mov", t_e)
            if not np.isnan(e_ms) and not np.isnan(e_me):
                _dm = (e_me - e_ms) / 1000.0
                if _dm > 0:
                    delta_moving = round(_dm, 3)

        # Distance
        odo_s, _ = _nearest_before("_odo", t_s)
        odo_e, _ = _nearest_after("_odo", t_e)

        # GPS
        has_latlon = "latitude" in df.columns and "longitude" in df.columns
        if has_latlon:
            win = df.loc[win_mask]
            lat_v = win["latitude"].dropna()
            lon_v = win["longitude"].dropna()
            lat_s = round(float(lat_v.iloc[0]), 6) if len(lat_v) else None
            lon_s = round(float(lon_v.iloc[0]), 6) if len(lon_v) else None
            lat_e = round(float(lat_v.iloc[-1]), 6) if len(lat_v) else None
            lon_e = round(float(lon_v.iloc[-1]), 6) if len(lon_v) else None
        else:
            lat_s = lon_s = lat_e = lon_e = None

        # ── motion_duration_s (cumulative duration of v>0 sub-intervals, zero_speed anchor mode only)
        # Written only when trip_endpoint_anchor='zero_speed'; downstream _seg_to_row
        # uses this value instead of the endpoint difference as the avg_speed
        # denominator, avoiding the zero-speed tails diluting the speed.
        motion_duration_s = None
        if trip_endpoint_anchor == "zero_speed":
            win_idx = np.where(win_mask)[0]
            if len(win_idx) >= 2:
                spd_win = df["_spd"].values[win_idx]
                tns_win = times_np[win_idx].view("i8")
                # Forward difference: dt[i] = t[i+1] - t[i], counted if spd[i] > threshold
                dt_ns = tns_win[1:] - tns_win[:-1]
                moving_mask = spd_win[:-1] > speed_threshold_kmh
                motion_duration_s = float(dt_ns[moving_mask].sum()) / 1e9

        segments.append(
            {
                "start_time": trip_start,
                "end_time": trip_end,
                "start_soc": round(soc_s, 2),
                "end_soc": round(soc_e, 2),
                "delta_soc_pct": round(delta_soc_signed, 2),
                "delta_energy_kwh": round(delta_energy_kwh, 3),
                "energy_source": energy_source,
                "delta_moving_kwh": delta_moving,
                "effective_capacity_kwh": (
                    round(eff_cap, 1) if eff_cap is not None else None
                ),
                "odo_start_km": round(odo_s, 3) if np.isfinite(odo_s) else None,
                "odo_end_km": round(odo_e, 3) if np.isfinite(odo_e) else None,
                "lat_start": lat_s,
                "lon_start": lon_s,
                "lat_end": lat_e,
                "lon_end": lon_e,
                "motion_duration_s": motion_duration_s,
                "_anchor_start_time": anchor_s_time,
                "_anchor_end_time": anchor_e_time,
                "_anchor_start_rel_kwh": anchor_s_rel,
                "_anchor_end_rel_kwh": anchor_e_rel,
            }
        )

    return segments
