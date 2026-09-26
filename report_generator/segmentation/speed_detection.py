"""
Speed-based trip / discharge segmentation.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..columns import DISTANCE_ONLY_SOURCE
from ..configs import _is_positive_number
from ..ep_confidence import (
    SOC_STEP_RATE_MAX_PCT_PER_H,
    SOC_STEP_SHARE_POOR,
    _largest_soc_step,
)
from .constants import (
    _CAPACITY_OUTSIDE_BAND_KEY,
    _ODOMETER_CONFIRMED_KEY,
    MOVING_COL,
    ODO_COL,
    SOC_COL,
    TIME_COL,
    TOTAL_ENERGY_COL,
)
from .timeutil import _in_form_of, _to_utc

# Energy sources that are a difference of a measured energy counter, rather than
# ΔSOC × capacity.
_COUNTER_ENERGY_SOURCES = frozenset({"total_energy", "moving_energy"})

#: The odometer distance (km) a trip needs, by default, to be kept on the
#: odometer's word when the SOC / energy floors reject it
#: (``speed_params.keep_odometer_confirmed_trips``): a few hundred metres of
#: yard manoeuvring stays a Stop, while a short hop between two sites counts.
DEFAULT_MIN_CONFIRMED_DISTANCE_KM = 0.5

# Why a trip window failed to become a segment (see _measure_window in
# find_discharge_segments_by_speed). The first three are the SOC / energy floors,
# which the odometer can overrule; the capacity band is not.
_FAILED_SOC_FLOOR = "soc_floor"
_FAILED_NO_ENERGY = "no_energy"
_FAILED_ENERGY_FLOOR = "energy_floor"
_FAILED_CAPACITY_BAND = "capacity_band"


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
    keep_trips_outside_cap_band: bool = False,
    keep_odometer_confirmed_trips: bool = False,
    min_confirmed_distance_km: float = DEFAULT_MIN_CONFIRMED_DISTANCE_KM,
    charge_segs: list[dict] | None = None,
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

    keep_trips_outside_cap_band : a trip whose SOC-implied capacity
        ``|ΔE| / (|ΔSOC|/100)`` lies outside ``[cap_lo, cap_hi]`` is dropped by
        default. With this set (only ``True`` switches it on), a trip whose
        energy comes from a measured counter (``total_energy`` /
        ``moving_energy``) is kept instead, with ``effective_capacity_kwh``
        ``None``: the speed signal confirms the trip and the counter measures its
        energy, so only the capacity implied by ΔSOC is implausible — a counter
        that excludes what the battery spends while parked, against an integer
        SOC that includes it, say — and a trip without a capacity is never a
        capacity donor. It also carries a private marker so the mass split and
        merge and the anchor ordering give nothing built from it a capacity
        either. A trip on ``soc_estimate`` energy is dropped as before.

    keep_odometer_confirmed_trips : a trip the SOC / energy floors reject
        (``min_soc_drop``, ``min_energy_kwh``: the SOC did not fall enough, or no
        energy could be measured) is dropped by default, and the driving it holds
        ends up inside a Stop row. With this set (only ``True`` switches it on),
        such a trip is kept when the odometer confirms the movement — at least
        ``min_confirmed_distance_km`` between the readings its distance is taken
        from:

        - the trip is first **cut at every charge** from ``charge_segs`` that
          overlaps it, so that nothing kept here overlaps a charge row: each
          part of the window outside those charges stands on its own, and a part
          the odometer does not confirm is dropped;
        - **the SOC rose across the trip, and a charge cut it**: on a sparse feed
          a charge taken while the telematics were silent can surface as a jump
          of the SOC after the vehicle has set off, so the charge detector places
          a charge inside the trip. Each part is then measured as a trip in its
          own right, under the same floors and capacity band: its SOC runs from
          its own first reading, which after the charge is the one past the
          jump, so the trip starts where the charge ends. A part the floors
          reject, or whose SOC change rests on a single physically impossible
          step, is kept distance-only (below), and one the capacity band rejects
          is dropped;
        - **otherwise** — a frozen SOC, a rise no charge accounts for, a drop
          below the floor — each part is kept **distance-only**: its times,
          distance, SOC readings and position as measured, its energy NaN, its
          ``energy_source`` ``"distance_only"``
          (:data:`report_generator.columns.DISTANCE_ONLY_SOURCE`), and no
          capacity and no energy anchors, so it is never a capacity donor and no
          EP can be formed from it. A charge inside such a trip is not measured
          beside: without a rise across the trip it is no jump but, on a feed
          interleaving a frozen SOC with a live one, the two alternating.

        A trip the capacity band rejects stays dropped: its energy was measured,
        and keeping it is ``keep_trips_outside_cap_band``'s decision. A trip that
        passes the floors is left exactly as it is, whatever it overlaps. Every
        segment kept this way carries a private marker, which
        :func:`~report_generator.segmentation.detection.run_segment_detection`
        counts and removes.

    min_confirmed_distance_km : the odometer distance (km) that confirms a trip
        for ``keep_odometer_confirmed_trips``: a positive number, default
        :data:`DEFAULT_MIN_CONFIRMED_DISTANCE_KM`. Read only with the key on.

    charge_segs : the leg's charge segments, which cut a rejected trip for
        ``keep_odometer_confirmed_trips``; ignored without the key.

    Returns
    -------
    list[dict] — the same list of segment dicts as find_discharge_segments_by_soc().
    Returns an empty list if the speed column is missing or all-zero (no trips) (the caller may fall back to SOC-based).
    """
    rescue = keep_odometer_confirmed_trips is True
    if rescue and not _is_positive_number(min_confirmed_distance_km):
        raise ValueError(
            "speed_params.min_confirmed_distance_km must be a positive number of "
            f"kilometres, not {min_confirmed_distance_km!r}"
        )

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

    def _soc_endpoints(t_s, t_e, win_mask) -> tuple[float, float]:
        """First and last valid SOC reading within the window."""
        win_soc = df.loc[win_mask & df["_soc"].notna(), "_soc"]
        if len(win_soc) < 1:
            # No SOC within the window → try SOC near the window boundaries
            soc_s, _ = _nearest_before("_soc", t_s)
            soc_e, _ = _nearest_after("_soc", t_e)
        else:
            soc_s = float(win_soc.iloc[0])
            soc_e = float(win_soc.iloc[-1])
        return soc_s, soc_e

    def _moving_delta(t_s, t_e) -> float | None:
        """delta_moving_kwh (independent of the primary energy source)."""
        delta_moving = None
        if has_moving:
            e_ms, _ = _nearest_before("_mov", t_s)
            e_me, _ = _nearest_after("_mov", t_e)
            if not np.isnan(e_ms) and not np.isnan(e_me):
                _dm = (e_me - e_ms) / 1000.0
                if _dm > 0:
                    delta_moving = round(_dm, 3)
        return delta_moving

    def _odometer(t_s, t_e) -> tuple[float, float]:
        """The valid odometer readings a window's distance is taken between."""
        odo_s, _ = _nearest_before("_odo", t_s)
        odo_e, _ = _nearest_after("_odo", t_e)
        return odo_s, odo_e

    def _gps(win_mask) -> tuple:
        """The first and the last position within the window."""
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
        return lat_s, lon_s, lat_e, lon_e

    def _measure_window(trip_start, trip_end) -> tuple[dict | None, str | None]:
        """Measure one trip window: ``(segment, None)``, or ``(None, why)``.

        ``why`` names the gate that rejected the window: one of the SOC / energy
        floors (:data:`_FAILED_SOC_FLOOR`, :data:`_FAILED_NO_ENERGY`,
        :data:`_FAILED_ENERGY_FLOOR`) or the capacity band
        (:data:`_FAILED_CAPACITY_BAND`).
        """
        t_s = trip_start.to_numpy().astype("datetime64[ns]")
        t_e = trip_end.to_numpy().astype("datetime64[ns]")

        # SOC: first and last valid reading within the trip window
        win_mask = (times_np >= t_s) & (times_np <= t_e)
        soc_s, soc_e = _soc_endpoints(t_s, t_e, win_mask)

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
            return None, _FAILED_SOC_FLOOR

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
            return None, _FAILED_NO_ENERGY
        if abs(delta_energy_kwh) < min_energy_kwh or delta_energy_kwh >= 0:
            return None, _FAILED_ENERGY_FLOOR

        # Effective capacity: computable only when SOC has an actual decline
        capacity_outside_band = False
        if delta_soc_abs > 0:
            eff_cap = abs(delta_energy_kwh) / (delta_soc_abs / 100.0)
            if cap_lo is not None and cap_hi is not None:
                if not (cap_lo <= eff_cap <= cap_hi):
                    if not (
                        keep_trips_outside_cap_band is True
                        and energy_source in _COUNTER_ENERGY_SOURCES
                    ):
                        return None, _FAILED_CAPACITY_BAND
                    # Opt-in: the trip stands (speed-confirmed, counter-measured
                    # energy); only its SOC-implied capacity is implausible, so
                    # it carries none and is no capacity donor.
                    eff_cap = None
                    capacity_outside_band = True
        else:
            eff_cap = None

        delta_moving = _moving_delta(t_s, t_e)

        # Distance
        odo_s, odo_e = _odometer(t_s, t_e)

        # GPS
        lat_s, lon_s, lat_e, lon_e = _gps(win_mask)

        seg = {
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
            "_anchor_start_time": anchor_s_time,
            "_anchor_end_time": anchor_e_time,
            "_anchor_start_rel_kwh": anchor_s_rel,
            "_anchor_end_rel_kwh": anchor_e_rel,
        }
        if capacity_outside_band:
            seg[_CAPACITY_OUTSIDE_BAND_KEY] = True
        return seg, None

    def _distance_only_segment(trip_start, trip_end) -> dict:
        """A trip window kept without an energy; everything else as measured."""
        t_s = trip_start.to_numpy().astype("datetime64[ns]")
        t_e = trip_end.to_numpy().astype("datetime64[ns]")
        win_mask = (times_np >= t_s) & (times_np <= t_e)
        soc_s, soc_e = _soc_endpoints(t_s, t_e, win_mask)
        has_soc = not (np.isnan(soc_s) or np.isnan(soc_e))
        odo_s, odo_e = _odometer(t_s, t_e)
        lat_s, lon_s, lat_e, lon_e = _gps(win_mask)
        return {
            "start_time": trip_start,
            "end_time": trip_end,
            "start_soc": round(soc_s, 2),
            "end_soc": round(soc_e, 2),
            "delta_soc_pct": round(soc_e - soc_s, 2) if has_soc else float("nan"),
            "delta_energy_kwh": float("nan"),
            "energy_source": DISTANCE_ONLY_SOURCE,
            "delta_moving_kwh": _moving_delta(t_s, t_e),
            "effective_capacity_kwh": None,
            "odo_start_km": round(odo_s, 3) if np.isfinite(odo_s) else None,
            "odo_end_km": round(odo_e, 3) if np.isfinite(odo_e) else None,
            "lat_start": lat_s,
            "lon_start": lon_s,
            "lat_end": lat_e,
            "lon_end": lon_e,
            "_anchor_start_time": None,
            "_anchor_end_time": None,
            "_anchor_start_rel_kwh": float("nan"),
            "_anchor_end_rel_kwh": float("nan"),
        }

    def _confirmed_km(trip_start, trip_end) -> float:
        """The odometer distance a window's segment reports (NaN without one)."""
        odo_s, odo_e = _odometer(
            trip_start.to_numpy().astype("datetime64[ns]"),
            trip_end.to_numpy().astype("datetime64[ns]"),
        )
        return odo_e - odo_s

    def _soc_rose(trip_start, trip_end) -> bool:
        """Whether the window's SOC ends above where it starts."""
        t_s = trip_start.to_numpy().astype("datetime64[ns]")
        t_e = trip_end.to_numpy().astype("datetime64[ns]")
        soc_s, soc_e = _soc_endpoints(t_s, t_e, (times_np >= t_s) & (times_np <= t_e))
        return bool(soc_e > soc_s)  # False when either is NaN

    charge_windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    soc_t_ns = np.array([], dtype="int64")
    soc_v = np.array([], dtype=float)
    if rescue:
        charge_windows = _charge_windows(charge_segs)
        _soc_ok = df["_soc"].notna().to_numpy()
        soc_t_ns = times_np.view("i8")[_soc_ok]
        soc_v = df["_soc"].to_numpy(dtype=float)[_soc_ok]

    def _soc_change_is_one_step(trip_start, trip_end, delta_soc_pct) -> bool:
        """Whether one physically impossible SOC step carries the window's change.

        The test the EP-confidence ``SOC_STEP`` check grades ``poor``: the fastest
        drop between two consecutive readings is quicker than any real discharge
        (:data:`~report_generator.ep_confidence.SOC_STEP_RATE_MAX_PCT_PER_H`) and
        is at least :data:`~report_generator.ep_confidence.SOC_STEP_SHARE_POOR` of
        the change. On a feed that interleaves a frozen SOC with a live one, a
        part cut out beside a charge that is only the two streams alternating
        runs from a live reading down to the frozen one in seconds.
        """
        step_pct, rate = _largest_soc_step(
            soc_t_ns,
            soc_v,
            pd.Timestamp(trip_start).value,
            pd.Timestamp(trip_end).value,
        )
        return bool(
            rate > SOC_STEP_RATE_MAX_PCT_PER_H
            and step_pct >= SOC_STEP_SHARE_POOR * abs(delta_soc_pct)
        )  # False when there is no drop (NaN)

    def _keep_confirmed(trip_start, trip_end) -> list[dict]:
        """What a trip the SOC / energy floors rejected leaves, on the odometer's word."""
        # Cut at every charge it overlaps, so that no trip kept here overlaps a
        # charge row. The parts are measured again only when the SOC rose across
        # the trip — the jump of a charge that surfaced late. Otherwise (a frozen
        # SOC, a drop below the floor) a charge inside the trip is no jump but,
        # say, a frozen and a live SOC alternating, and measuring beside it
        # would turn that alternation into energy.
        parts, cut = _parts_outside_charges(trip_start, trip_end, charge_windows)
        remeasure = cut and _soc_rose(trip_start, trip_end)
        kept: list[dict] = []
        for part_start, part_end in parts:
            # A distance of NaN (no odometer reading on one side) confirms nothing.
            if not _confirmed_km(part_start, part_end) >= min_confirmed_distance_km:
                continue
            seg = None
            if remeasure:
                seg, failed = _measure_window(part_start, part_end)
                if seg is None and failed == _FAILED_CAPACITY_BAND:
                    continue
                if seg is not None and _soc_change_is_one_step(
                    part_start, part_end, seg["delta_soc_pct"]
                ):
                    seg = None  # the SOC change is no measurement: no energy
            if seg is None:
                seg = _distance_only_segment(part_start, part_end)
            seg[_ODOMETER_CONFIRMED_KEY] = True
            kept.append(seg)
        return kept

    # 3. Compute segment metrics for each trip
    segments: list[dict] = []
    for trip_start, trip_end in trips:
        seg, failed = _measure_window(trip_start, trip_end)
        if seg is not None:
            segments.append(seg)
        elif rescue and failed != _FAILED_CAPACITY_BAND:
            segments.extend(_keep_confirmed(trip_start, trip_end))

    return segments


def _charge_windows(charge_segs) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """The ``(start, end)`` of each charge segment with a duration, in UTC, sorted."""
    windows = []
    for charge in charge_segs or ():
        try:
            c_start = _to_utc(charge["start_time"])
            c_end = _to_utc(charge["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if c_start < c_end:
            windows.append((c_start, c_end))
    return sorted(windows)


def _parts_outside_charges(trip_start, trip_end, charge_windows) -> tuple[list, bool]:
    """The parts of a trip window no charge covers, and whether a charge cut it.

    ``charge_windows`` are UTC ``(start, end)`` pairs, sorted
    (:func:`_charge_windows`). A charge overlaps the trip when it starts before
    the trip ends and ends after the trip starts; touching boundaries are no
    overlap. Without an overlapping charge the only part is the trip itself, as
    given; a charge covering the whole trip leaves no part. The parts keep the
    trip's own time-zone form.
    """
    t_start, t_end = _to_utc(trip_start), _to_utc(trip_end)
    parts: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cursor, cut = t_start, False
    for c_start, c_end in charge_windows:
        if c_end <= t_start or c_start >= t_end:
            continue
        cut = True
        if c_start > cursor:
            parts.append((cursor, c_start))
        cursor = max(cursor, c_end)
    if not cut:
        return [(trip_start, trip_end)], False
    if cursor < t_end:
        parts.append((cursor, t_end))
    return [
        (_in_form_of(p_start, trip_start), _in_form_of(p_end, trip_start))
        for p_start, p_end in parts
    ], True
