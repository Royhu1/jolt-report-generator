"""
SOC-based charge / discharge segmentation.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .constants import (
    AC_COL,
    DC_COL,
    MOVING_COL,
    ODO_COL,
    SOC_COL,
    TIME_COL,
    TOTAL_ENERGY_COL,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Charge-segment detection
# =============================================================================
def find_charge_segments_by_soc(
    df_raw: pd.DataFrame,
    plateau_window_min: float = 60,
    min_soc_rise: float = 5.0,
    min_energy_kwh: float = 5.0,
    cap_lo: float | None = None,
    cap_hi: float | None = None,
    ac_col: str = AC_COL,
    dc_col: str = DC_COL,
    moving_energy_col: str = MOVING_COL,
    nominal_kwh: float | None = None,
) -> list[dict]:
    """
    Detect charge segments (segments of sustained SOC rise) from sparse raw telemetry data.

    Energy-source priority
    ----------------------
    1. AC+DC columns (ac_col + dc_col) with valid data → energy_source='ac_dc'
    2. SOC × nominal_kwh estimate                      → energy_source='soc_estimate'

    Returned fields (v2 unified schema)
    -----------------------------------
    start_time, end_time, start_soc, end_soc,
    delta_soc_pct (positive),
    delta_energy_kwh (positive),
    energy_source,
    delta_moving_kwh (>= 0, or None),
    effective_capacity_kwh,
    charge_type,
    ac_start_wh, ac_end_wh, dc_start_wh, dc_end_wh (None if no AC/DC data),
    odo_start_km, odo_end_km, latitude, longitude

    Temporary fields (_ANCHOR_PRIVATE_KEYS, must be filtered out before saving to CSV):
    _anchor_start_time, _anchor_end_time,
    _anchor_start_rel_kwh, _anchor_end_rel_kwh
    """
    if SOC_COL not in df_raw.columns or TIME_COL not in df_raw.columns:
        return []

    df = df_raw.copy()
    df[TIME_COL] = pd.to_datetime(df[TIME_COL], errors="coerce", utc=True)
    df = df.dropna(subset=[TIME_COL]).sort_values(TIME_COL).reset_index(drop=True)

    df[SOC_COL] = pd.to_numeric(df[SOC_COL], errors="coerce")
    df.loc[df[SOC_COL] == 0, SOC_COL] = np.nan

    has_acdc = ac_col in df.columns and dc_col in df.columns
    if has_acdc:
        df[ac_col] = pd.to_numeric(df[ac_col], errors="coerce")
        df[dc_col] = pd.to_numeric(df[dc_col], errors="coerce")

    has_moving = moving_energy_col in df.columns
    if has_moving:
        df[moving_energy_col] = pd.to_numeric(df[moving_energy_col], errors="coerce")

    if ODO_COL in df.columns:
        df[ODO_COL] = pd.to_numeric(df[ODO_COL], errors="coerce")
    for _c in ("latitude", "longitude"):
        if _c in df.columns:
            df[_c] = pd.to_numeric(df[_c], errors="coerce")
            df.loc[df[_c] == 0, _c] = np.nan

    # ── SOC rising-block detection ──────────────────────────────────────────
    soc_mask = df[SOC_COL].notna()
    soc_pos = np.array(df.index[soc_mask].tolist(), dtype=np.intp)
    if len(soc_pos) < 2:
        return []

    soc_vals = df.loc[soc_pos, SOC_COL].values.astype(float)
    times_all = df[TIME_COL].values.astype("datetime64[ns]")
    n = len(soc_pos)

    rising = np.concatenate([[False], np.diff(soc_vals) > 0])
    blocks: list[list[int]] = []
    i = 1
    while i < n:
        if rising[i]:
            s = i
            while i < n and rising[i]:
                i += 1
            blocks.append([s, i - 1])
        else:
            i += 1

    if not blocks:
        return []

    # ── Merge adjacent blocks ───────────────────────────────────────────────
    plateau_ns = int(plateau_window_min * 60 * 1_000_000_000)
    merged = [blocks[0][:]]
    for blk in blocks[1:]:
        prev_end = merged[-1][1]
        gap_ns = int(times_all[soc_pos[blk[0]]] - times_all[soc_pos[prev_end]])
        has_drop = any(
            soc_vals[j] < soc_vals[j - 1] for j in range(prev_end + 1, blk[0])
        )
        if gap_ns <= plateau_ns and not has_drop:
            merged[-1][1] = blk[1]
        else:
            merged.append(blk[:])

    # ── Prepare energy series ───────────────────────────────────────────────
    energy_pos = np.array([], dtype=np.intp)
    base_wh = 0.0
    if has_acdc:
        emask = df[ac_col].notna() & df[dc_col].notna()
        energy_pos = np.array(df.index[emask].tolist(), dtype=np.intp)
        if len(energy_pos) > 0:
            base_wh = float(df.at[energy_pos[0], ac_col]) + float(
                df.at[energy_pos[0], dc_col]
            )

    mov_pos = np.array([], dtype=np.intp)
    if has_moving:
        mmask = df[moving_energy_col].notna()
        mov_pos = np.array(df.index[mmask].tolist(), dtype=np.intp)

    # ── Build segments ──────────────────────────────────────────────────────
    segments: list[dict] = []
    for blk_start, blk_end in merged:
        soc_row_s = int(soc_pos[blk_start - 1])
        soc_row_e = int(soc_pos[blk_end])
        soc_s = float(soc_vals[blk_start - 1])
        soc_e = float(soc_vals[blk_end])
        delta_soc = soc_e - soc_s  # positive for charge

        if delta_soc < min_soc_rise or delta_soc <= 0:
            continue

        # ── delta_energy_kwh: AC+DC → SOC estimate ────────────────────────
        delta_energy = None
        energy_source = None
        ac_s = ac_e = dc_s = dc_e = None
        anchor_s_time = anchor_e_time = None
        anchor_s_rel = anchor_e_rel = float("nan")

        if len(energy_pos) > 0:
            idx_s = int(np.searchsorted(energy_pos, soc_row_s, side="right")) - 1
            idx_e = int(np.searchsorted(energy_pos, soc_row_e, side="left"))
            if idx_s < 0:
                idx_s = 0
            if idx_e >= len(energy_pos):
                idx_e = len(energy_pos) - 1
            ext_s = int(energy_pos[idx_s])
            ext_e = int(energy_pos[idx_e])
            if ext_s < ext_e:
                _ac_s = float(df.at[ext_s, ac_col])
                _ac_e = float(df.at[ext_e, ac_col])
                _dc_s = float(df.at[ext_s, dc_col])
                _dc_e = float(df.at[ext_e, dc_col])
                _delta = ((_ac_e - _ac_s) + (_dc_e - _dc_s)) / 1000.0
                if _delta > 0:
                    delta_energy = _delta
                    energy_source = "ac_dc"
                    ac_s, ac_e, dc_s, dc_e = _ac_s, _ac_e, _dc_s, _dc_e
                    anchor_s_time = pd.Timestamp(df.at[ext_s, TIME_COL])
                    anchor_e_time = pd.Timestamp(df.at[ext_e, TIME_COL])
                    anchor_s_rel = round((_ac_s + _dc_s - base_wh) / 1000.0, 4)
                    anchor_e_rel = round((_ac_e + _dc_e - base_wh) / 1000.0, 4)

        if delta_energy is None and nominal_kwh is not None:
            delta_energy = (delta_soc / 100.0) * nominal_kwh
            energy_source = "soc_estimate"
            anchor_s_time = pd.Timestamp(df.at[soc_row_s, TIME_COL])
            anchor_e_time = pd.Timestamp(df.at[soc_row_e, TIME_COL])
            anchor_s_rel = float("nan")
            anchor_e_rel = float("nan")

        if delta_energy is None or delta_energy < min_energy_kwh or delta_energy <= 0:
            continue

        eff_cap = delta_energy / (delta_soc / 100.0)
        if cap_lo is not None and cap_hi is not None:
            if not (cap_lo <= eff_cap <= cap_hi):
                continue

        # ── delta_moving_kwh (separate, always >= 0) ─────────────────────
        delta_moving = None
        if len(mov_pos) > 0:
            idx_ms = int(np.searchsorted(mov_pos, soc_row_s, side="right")) - 1
            idx_me = int(np.searchsorted(mov_pos, soc_row_e, side="left"))
            if idx_ms < 0:
                idx_ms = 0
            if idx_me >= len(mov_pos):
                idx_me = len(mov_pos) - 1
            ext_ms = int(mov_pos[idx_ms])
            ext_me = int(mov_pos[idx_me])
            if ext_ms < ext_me:
                _mov_s = float(df.at[ext_ms, moving_energy_col])
                _mov_e = float(df.at[ext_me, moving_energy_col])
                _dm = (_mov_e - _mov_s) / 1000.0
                if _dm >= 0:
                    delta_moving = round(_dm, 3)

        # Charge type
        if energy_source == "ac_dc":
            thr = 0.5
            d_ac = (ac_e - ac_s) / 1000.0
            d_dc = (dc_e - dc_s) / 1000.0
            if d_ac >= thr and d_dc >= thr:
                charge_type = "Mix charge"
            elif d_ac >= thr:
                charge_type = "AC charge"
            else:
                charge_type = "DC charge"
        else:
            charge_type = "estimated"

        seg: dict = {
            "start_time": pd.Timestamp(df.at[soc_row_s, TIME_COL]),
            "end_time": pd.Timestamp(df.at[soc_row_e, TIME_COL]),
            "start_soc": round(soc_s, 1),
            "end_soc": round(soc_e, 1),
            "delta_soc_pct": round(delta_soc, 1),  # positive
            "delta_energy_kwh": round(delta_energy, 3),  # positive
            "energy_source": energy_source,
            "delta_moving_kwh": delta_moving,
            "effective_capacity_kwh": round(eff_cap, 1),
            "charge_type": charge_type,
            "ac_start_wh": round(ac_s, 0) if ac_s is not None else None,
            "ac_end_wh": round(ac_e, 0) if ac_e is not None else None,
            "dc_start_wh": round(dc_s, 0) if dc_s is not None else None,
            "dc_end_wh": round(dc_e, 0) if dc_e is not None else None,
            "_anchor_start_time": anchor_s_time,
            "_anchor_end_time": anchor_e_time,
            "_anchor_start_rel_kwh": anchor_s_rel,
            "_anchor_end_rel_kwh": anchor_e_rel,
        }

        if ODO_COL in df.columns:
            ob = df.loc[:soc_row_s, ODO_COL].dropna()
            oa = df.loc[soc_row_e:, ODO_COL].dropna()
            seg["odo_start_km"] = round(float(ob.iloc[-1]), 1) if len(ob) else None
            seg["odo_end_km"] = round(float(oa.iloc[0]), 1) if len(oa) else None

        for _c in ("latitude", "longitude"):
            if _c in df.columns:
                vals = df.loc[soc_row_s:soc_row_e, _c].dropna()
                seg[_c] = round(float(vals.mean()), 5) if len(vals) else None

        segments.append(seg)

    return segments


# =============================================================================
# Discharge-segment detection
# =============================================================================
def find_discharge_segments_by_soc(
    df_raw: pd.DataFrame,
    plateau_window_min: float = 60,
    soc_rise_abort_pct: float = 3.0,
    min_soc_drop: float = 10.0,
    min_energy_kwh: float = 2.0,
    cap_lo: float | None = None,
    cap_hi: float | None = None,
    total_energy_col: str = TOTAL_ENERGY_COL,
    moving_energy_col: str = MOVING_COL,
    nominal_kwh: float | None = None,
    min_trip_distance_km: float = 0.0,
) -> list[dict]:
    """
    Detect discharge/driving segments (segments of sustained SOC fall) from sparse raw telemetry data.

    Energy-source priority
    ----------------------
    1. total_energy_col (total_electric_energy_used_plugged_in_included) → energy_source='total_energy'
    2. moving_energy_col (electric_energy_wheelbased_speed_over_zero)    → energy_source='moving_energy'
    3. SOC × nominal_kwh estimate                                       → energy_source='soc_estimate'

    Returned fields (v2 unified schema)
    -----------------------------------
    start_time, end_time, start_soc, end_soc,
    delta_soc_pct (negative, e.g. -25.0),
    delta_energy_kwh (negative, e.g. -80.0),
    energy_source,
    delta_moving_kwh (>= 0, or None),
    effective_capacity_kwh,
    odo_start_km, odo_end_km, lat_start, lon_start, lat_end, lon_end

    Temporary fields (_ANCHOR_PRIVATE_KEYS, must be filtered out before saving to CSV):
    _anchor_start_time, _anchor_end_time,
    _anchor_start_rel_kwh, _anchor_end_rel_kwh

    Parameters
    ----------
    min_trip_distance_km : float, default 0.0
        Minimum-distance filter threshold (km) for a discharge segment. If
        (odo_end_km - odo_start_km) < this value, the segment is dropped. The
        default 0.0 means no filtering, backward compatible. Typical use: set to
        10.0 on mercedes_soc to suppress the EP outliers caused by depot/short-haul
        SOC jitter (short segments have very noisy EP).
    """
    if SOC_COL not in df_raw.columns or TIME_COL not in df_raw.columns:
        return []
    if df_raw.empty:
        return []

    df = df_raw.copy()
    df[TIME_COL] = pd.to_datetime(df[TIME_COL], errors="coerce", utc=True)
    df = df.dropna(subset=[TIME_COL]).sort_values(TIME_COL).reset_index(drop=True)

    plateau_ns = int(plateau_window_min * 60 * 1_000_000_000)

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

    soc_rows = df.index[df["_soc"].notna()].tolist()
    if len(soc_rows) < 2:
        return []
    soc_vals = df.loc[soc_rows, "_soc"].values.astype(float)
    soc_times = df.loc[soc_rows, TIME_COL].values.astype("datetime64[ns]")

    # Total energy arrays
    tot_vals = tot_times = None
    tot_base_wh = 0.0
    if has_total:
        tr = df.index[df["_tot"].notna()].tolist()
        if tr:
            tot_vals = df.loc[tr, "_tot"].values.astype(float)
            tot_times = df.loc[tr, TIME_COL].values.astype("datetime64[ns]")
            tot_base_wh = float(tot_vals[0])

    # Moving energy arrays
    mov_vals = mov_times = None
    mov_base_wh = 0.0
    if has_moving:
        mr = df.index[df["_mov"].notna()].tolist()
        if mr:
            mov_vals = df.loc[mr, "_mov"].values.astype(float)
            mov_times = df.loc[mr, TIME_COL].values.astype("datetime64[ns]")
            mov_base_wh = float(mov_vals[0])

    # Odometer
    odo_rows = df.index[df["_odo"].notna()].tolist()
    odo_vals = (
        df.loc[odo_rows, "_odo"].values.astype(float) if odo_rows else np.array([])
    )
    odo_times = (
        df.loc[odo_rows, TIME_COL].values.astype("datetime64[ns]")
        if odo_rows
        else np.array([], dtype="datetime64[ns]")
    )
    n_soc = len(soc_rows)

    def _at_or_before(tarr, varr, t):
        if tarr is None or not len(tarr):
            return -1, np.nan
        pos = int(np.searchsorted(tarr, t, side="right")) - 1
        return (pos, float(varr[pos])) if pos >= 0 else (-1, np.nan)

    def _at_or_after(tarr, varr, t):
        if tarr is None or not len(tarr):
            return -1, np.nan
        pos = int(np.searchsorted(tarr, t, side="left"))
        return (pos, float(varr[pos])) if pos < len(tarr) else (-1, np.nan)

    # ── SOC falling-block detection ─────────────────────────────────────────
    soc_diff = np.diff(soc_vals)
    declining = np.concatenate([[False], soc_diff < 0])
    blocks: list[list[int]] = []
    i = 1
    while i < n_soc:
        if declining[i]:
            s = i
            while i < n_soc and declining[i]:
                i += 1
            blocks.append([s, i - 1])
        else:
            i += 1

    if not blocks:
        return []

    # ── Merge adjacent blocks ───────────────────────────────────────────────
    merged = [blocks[0][:]]
    for blk in blocks[1:]:
        prev_end = merged[-1][1]
        gap_ns = int(soc_times[blk[0]] - soc_times[prev_end])
        max_rise = max(
            (soc_vals[j] - soc_vals[j - 1] for j in range(prev_end + 1, blk[0])),
            default=0.0,
        )
        if gap_ns <= plateau_ns and max_rise < soc_rise_abort_pct:
            merged[-1][1] = blk[1]
        else:
            merged.append(blk[:])

    # ── Compute segment metrics ─────────────────────────────────────────────
    segments: list[dict] = []
    for blk_start, blk_end in merged:
        i_s = blk_start - 1
        i_e = blk_end
        t_s = soc_times[i_s]
        t_e = soc_times[i_e]
        soc_s = float(soc_vals[i_s])
        soc_e = float(soc_vals[i_e])
        delta_soc_signed = soc_e - soc_s  # negative for discharge
        delta_soc_abs = soc_s - soc_e  # positive (drop magnitude)

        if delta_soc_abs < min_soc_drop or delta_soc_abs <= 0:
            continue

        # ── delta_energy_kwh: total → moving → SOC estimate ──────────────
        delta_energy_kwh = None
        energy_source = None
        anchor_s_time = anchor_e_time = None
        anchor_s_rel = anchor_e_rel = float("nan")

        # 1. Total energy col (preferred)
        if tot_vals is not None:
            pos_s, e_s = _at_or_before(tot_times, tot_vals, t_s)
            pos_e, e_e = _at_or_after(tot_times, tot_vals, t_e)
            if pos_s >= 0 and pos_e >= 0 and not np.isnan(e_s) and not np.isnan(e_e):
                _raw = (e_e - e_s) / 1000.0
                if _raw > 0:
                    delta_energy_kwh = -_raw
                    energy_source = "total_energy"
                    anchor_s_time = pd.Timestamp(tot_times[pos_s])
                    anchor_e_time = pd.Timestamp(tot_times[pos_e])
                    anchor_s_rel = round((e_s - tot_base_wh) / 1000.0, 4)
                    anchor_e_rel = round((e_e - tot_base_wh) / 1000.0, 4)

        # 2. Moving energy col (fallback)
        if delta_energy_kwh is None and mov_vals is not None:
            pos_s, e_s = _at_or_before(mov_times, mov_vals, t_s)
            pos_e, e_e = _at_or_after(mov_times, mov_vals, t_e)
            if pos_s >= 0 and pos_e >= 0 and not np.isnan(e_s) and not np.isnan(e_e):
                _raw = (e_e - e_s) / 1000.0
                if _raw > 0:
                    delta_energy_kwh = -_raw
                    energy_source = "moving_energy"
                    anchor_s_time = pd.Timestamp(mov_times[pos_s])
                    anchor_e_time = pd.Timestamp(mov_times[pos_e])
                    anchor_s_rel = round((e_s - mov_base_wh) / 1000.0, 4)
                    anchor_e_rel = round((e_e - mov_base_wh) / 1000.0, 4)

        # 3. SOC estimate (last resort)
        if delta_energy_kwh is None and nominal_kwh is not None:
            delta_energy_kwh = (delta_soc_signed / 100.0) * nominal_kwh  # negative
            energy_source = "soc_estimate"
            anchor_s_time = pd.Timestamp(t_s)
            anchor_e_time = pd.Timestamp(t_e)
            anchor_s_rel = float("nan")
            anchor_e_rel = float("nan")

        if delta_energy_kwh is None:
            continue
        if abs(delta_energy_kwh) < min_energy_kwh or delta_energy_kwh >= 0:
            continue

        eff_cap = abs(delta_energy_kwh) / (delta_soc_abs / 100.0)
        if cap_lo is not None and cap_hi is not None:
            if not (cap_lo <= eff_cap <= cap_hi):
                continue

        # ── delta_moving_kwh (always >= 0, separate from primary energy) ─
        delta_moving = None
        if mov_vals is not None:
            pos_ms, e_ms = _at_or_before(mov_times, mov_vals, t_s)
            pos_me, e_me = _at_or_after(mov_times, mov_vals, t_e)
            if pos_ms >= 0 and pos_me >= 0:
                _dm = (e_me - e_ms) / 1000.0
                if _dm > 0:
                    delta_moving = round(_dm, 3)

        _, odo_s = _at_or_before(odo_times, odo_vals, t_s)
        _, odo_e = _at_or_after(odo_times, odo_vals, t_e)

        has_latlon = "latitude" in df.columns and "longitude" in df.columns
        if has_latlon:
            srow_s = soc_rows[i_s]
            srow_e = soc_rows[i_e]
            win = df.loc[srow_s:srow_e]
            lat_v = win["latitude"].dropna()
            lon_v = win["longitude"].dropna()
            lat_s = round(float(lat_v.iloc[0]), 6) if len(lat_v) else None
            lon_s = round(float(lon_v.iloc[0]), 6) if len(lon_v) else None
            lat_e = round(float(lat_v.iloc[-1]), 6) if len(lat_v) else None
            lon_e = round(float(lon_v.iloc[-1]), 6) if len(lon_v) else None
        else:
            lat_s = lon_s = lat_e = lon_e = None

        segments.append(
            {
                "start_time": pd.Timestamp(t_s),
                "end_time": pd.Timestamp(t_e),
                "start_soc": round(soc_s, 2),
                "end_soc": round(soc_e, 2),
                "delta_soc_pct": round(delta_soc_signed, 2),  # negative
                "delta_energy_kwh": round(delta_energy_kwh, 3),  # negative
                "energy_source": energy_source,
                "delta_moving_kwh": delta_moving,
                "effective_capacity_kwh": round(eff_cap, 1),
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
        )

    # ── Minimum-distance filter (per-pipeline optional; default 0.0 = no filtering, backward compatible) ──
    # Purpose: suppress the EP outliers caused by depot/short-haul jitter (short
    # segments have very noisy EP). Applied as a post-filter after all segments
    # are formed, so the drop count can be reported exactly.
    if min_trip_distance_km > 0.0 and segments:
        _kept: list[dict] = []
        _drops: list[float] = []
        for _seg in segments:
            _o_s = _seg.get("odo_start_km")
            _o_e = _seg.get("odo_end_km")
            if _o_s is None or _o_e is None:
                # Do not apply the distance filter when there is no odo information (avoids wrongful deletion)
                _kept.append(_seg)
                continue
            _dist = float(_o_e) - float(_o_s)
            if _dist < min_trip_distance_km:
                _drops.append(_dist)
                continue
            _kept.append(_seg)
        if _drops:
            logger.info(
                "  minimum-distance filter: dropped %d segments with distance < %.2f km "
                "(dropped distances km: %s)",
                len(_drops),
                min_trip_distance_km,
                ", ".join(f"{d:.2f}" for d in _drops),
            )
        segments = _kept

    return segments
