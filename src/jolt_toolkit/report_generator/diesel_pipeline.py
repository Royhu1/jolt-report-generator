"""
diesel_pipeline.py
==================
Diesel-vehicle Logger-side data-processing pipeline.

Main differences from the EV pipeline:
  * The data source is SRFLOGGER_V1 legs (not FPS telematics legs)
  * Energy comes from the LFC `engine total fuel used` cumulative fuel (L), converted to kWh by the diesel LHV
  * Distance comes from the VDHR `hr total vehicle distance` cumulative mileage (km)
  * Vehicle mass comes from the Logger-side `CVW gross combination vehicle weight`
  * No SOC, no charge events, no effective-capacity correction
  * Trip segmentation reuses segment_algorithms.find_speed_trips (fully shared logic)
  * **Independent Excel column set**: uses ``DIESEL_HEADERS`` (see report_builder.py),
    no longer mixing in the EV ``HEADERS`` and writing NaN into the electricity-related
    columns (SOC, Battery Capacity, Energy Performance kWh/km, etc.).

External entry point:
  * process_diesel_leg(leg, cfg, cumulative_km, srf_data, ...) -> (row_tuples, cumulative_km)

This module no longer paints the diesel validation figures. The 4-panel
diesel painter (Speed / cumulative fuel / cumulative mileage / GCVW) and its
in-place overlay-regenerate entry moved to the report-visuals skill together with
matplotlib; the skill re-drives the shared, package-side segmentation
(:func:`_segments_from_df` / :func:`_logger_df_from_csv`) to produce identical
figures. The data-processing half (channel pulls, trip metrics, row building)
stays here.
"""

from __future__ import annotations

import logging
from math import nan
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from jolt_toolkit.report_generator.operators import derive_leg_operator
from jolt_toolkit.report_generator.report_builder import (
    DIESEL_HEADERS,
    _build_logger_url,
    _get_postcode,
    _point_str,
)
from jolt_toolkit.report_generator.segment_algorithms import (
    TIME_COL,
    VEHICLE_CONFIG,
    find_speed_trips,
)

logger = logging.getLogger(__name__)


# ── Logger channel-name constants (defaults corresponding to the vehicles.json fields) ──
DEFAULT_SPEED_COL = "CCVS wheel based vehicle speed"
DEFAULT_SPEED_FALLBACK = "2 speed"  # GPS m/s
DEFAULT_FUEL_COL = "LFC engine total fuel used"  # cumulative L
DEFAULT_FUEL_RATE_COL = "LFE fuel rate"  # instantaneous L/h
DEFAULT_DISTANCE_COL = "VDHR hr total vehicle distance"  # cumulative km
DEFAULT_MASS_COL = "CVW gross combination vehicle weight"
DEFAULT_ALTITUDE_COL = "2 altitude"
DEFAULT_AMBIENT_TEMP_COL = "AMB ambient air temperature"
DEFAULT_DIESEL_LHV_KWH_PER_L = (
    10.0  # diesel lower heating value ≈ 36 MJ/L / 3600 s/h = 10 kWh/L
)
DEFAULT_MIN_TRIP_DISTANCE_KM = (
    1.0  # a "trip" below this distance is treated as depot-shuffling noise
)

# ── Logger Channel 7 weather channels (consistent with logger_patcher.py) ──
_W_TEMP = "7 temperature"
_W_PRESS = "7 pressure"
_W_HUMID = "7 humidity"
_W_WIND_S = "7 wind speed"
_W_WIND_D = "7 wind direction"
_CARDINALS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _build_logger_df(leg, cfg: dict) -> pd.DataFrame | None:
    """
    Pull all channels the diesel pipeline needs from a single SRFLOGGER leg, merged into one DataFrame.

    The returned DataFrame:
      - Index: UTC timestamps
      - Columns (added if present):
        {speed_col}, {fuel_col}, {fuel_rate_col}, {distance_col}, {mass_col},
        {altitude_col}, {ambient_temp_col}, "_lat", "_lon"
      - Extra column TIME_COL: same value as the index, for find_speed_trips to use

    Returns None if the leg has neither of the two speed sources CCVS and GPS Channel 2.
    """
    types_avail = set(leg.types)

    speed_col = cfg.get("speed_col", DEFAULT_SPEED_COL)
    speed_fb = cfg.get("speed_col_fallback", DEFAULT_SPEED_FALLBACK)
    fuel_col = cfg.get("fuel_energy_col", DEFAULT_FUEL_COL)
    fuel_rate_col = cfg.get("fuel_rate_col", DEFAULT_FUEL_RATE_COL)
    dist_col = cfg.get("distance_col", DEFAULT_DISTANCE_COL)
    mass_col = cfg.get("mass_col", DEFAULT_MASS_COL)
    alt_col = cfg.get("altitude_col", DEFAULT_ALTITUDE_COL)
    temp_col = cfg.get("ambient_temp_col", DEFAULT_AMBIENT_TEMP_COL)

    frames: list[pd.DataFrame] = []

    def _pull(channel: str, wanted_cols: list[str]) -> None:
        """Fetch one J1939 channel, filter out the columns in wanted_cols that actually exist, and add to frames."""
        if channel not in types_avail:
            return
        try:
            df_ch = leg.get_data_frame(channel, resolution="1s")
        except Exception as exc:
            logger.debug("  leg %s channel %s fetch failed: %s", leg.uri, channel, exc)
            return
        if df_ch is None or df_ch.empty:
            return
        keep = [c for c in wanted_cols if c in df_ch.columns]
        if keep:
            frames.append(df_ch[keep])

    # ── CCVS (primary speed source) ───────────────────────────────────
    _pull("CCVS", [speed_col])

    # ── LFC (cumulative fuel) ─────────────────────────────────────────
    _pull("LFC", [fuel_col])

    # ── LFE (instantaneous fuel) ──────────────────────────────────────
    _pull("LFE", [fuel_rate_col])

    # ── VDHR (cumulative mileage) ─────────────────────────────────────
    _pull("VDHR", [dist_col])

    # ── CVW (real-time vehicle mass) ──────────────────────────────────
    _pull("CVW", [mass_col])

    # ── AMB (ambient temperature) ─────────────────────────────────────
    _pull("AMB", [temp_col])

    # ── Channel 7 (Logger weather station: temperature/pressure/humidity/wind speed/wind direction) ──
    if "7" in types_avail:
        try:
            df_w = leg.get_data_frame("7", resolution="1s")
            if df_w is not None and not df_w.empty:
                keep = [
                    c
                    for c in (_W_TEMP, _W_PRESS, _W_HUMID, _W_WIND_S, _W_WIND_D)
                    if c in df_w.columns
                ]
                if keep:
                    frames.append(df_w[keep])
        except Exception as exc:
            logger.debug("  leg %s channel 7 fetch failed: %s", leg.uri, exc)

    # ── Channel 2 (GPS positioning + fallback speed + altitude) ──────
    if "2" in types_avail:
        try:
            df_gps = leg.get_data_frame("2", resolution="1s")
            if df_gps is not None and not df_gps.empty:
                gps_cols = {}
                if "2 longitude" in df_gps.columns:
                    gps_cols["_lon"] = df_gps["2 longitude"]
                if "2 latitude" in df_gps.columns:
                    gps_cols["_lat"] = df_gps["2 latitude"]
                if alt_col in df_gps.columns:
                    gps_cols[alt_col] = df_gps[alt_col]
                if speed_fb in df_gps.columns:
                    gps_cols[speed_fb] = df_gps[speed_fb]
                if gps_cols:
                    frames.append(pd.DataFrame(gps_cols, index=df_gps.index))
        except Exception as exc:
            logger.debug("  leg %s channel 2 fetch failed: %s", leg.uri, exc)

    if not frames:
        return None

    # Outer join by timestamp (different channels may have different sampling intervals)
    df = pd.concat(frames, axis=1)
    return _finalise_logger_df(df, cfg, source=getattr(leg, "uri", "?"))


def _finalise_logger_df(
    df: pd.DataFrame, cfg: dict, source: str = "?"
) -> pd.DataFrame | None:
    """
    Finalise a Logger DataFrame: dedup + UTC index + TIME_COL + speed fallback.

    Extracted from the tail of :func:`_build_logger_df`, shared by the two data sources:
      * SRF leg (the concat result after ``_build_logger_df`` pulls)
      * local ``raw_logger_*/logger_*.csv`` (after :func:`_logger_df_from_csv` reads it)

    ``source`` is only used to identify the source in debug logs. Returns None if there is no usable primary speed column after processing.
    """
    speed_col = cfg.get("speed_col", DEFAULT_SPEED_COL)
    speed_fb = cfg.get("speed_col_fallback", DEFAULT_SPEED_FALLBACK)

    df = df[~df.index.duplicated(keep="first")].sort_index()

    # Ensure the index carries the UTC time zone
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # Add the TIME_COL column for find_speed_trips to use
    df[TIME_COL] = df.index

    # Primary speed column handling: if CCVS speed is empty, use the GPS speed × 3.6 fallback
    if (
        speed_col not in df.columns
        or pd.to_numeric(df[speed_col], errors="coerce").notna().sum() == 0
    ):
        if speed_fb in df.columns:
            df[speed_col] = pd.to_numeric(df[speed_fb], errors="coerce") * 3.6
            logger.debug("  leg %s using GPS speed fallback (m/s × 3.6)", source)
        else:
            return None  # no speed source

    return df


def _logger_df_from_csv(csv_path: Path, cfg: dict) -> pd.DataFrame | None:
    """
    Rebuild the diesel pipeline's Logger DataFrame from a local ``raw_logger_*/logger_*.csv``.

    These CSVs are written by :func:`_generator.JOLTReportGenerator._save_logger_data`
    with ``get_data_frame(list(available), resolution='1s')``; the first column is
    the timestamp index and the rest are the real column names of all J1939
    channels (CCVS / LFC / VDHR / CVW / Channel 7, etc.), the same shape as the
    result of :func:`_build_logger_df` pulling and concatenating live. After
    reading, it is handed to :func:`_finalise_logger_df` for the same index / speed
    fallback finishing, so SRF is not needed.
    """
    try:
        df = pd.read_csv(csv_path, index_col=0)
    except Exception as exc:
        logger.debug("  reading logger CSV failed %s: %s", csv_path.name, exc)
        return None
    if df is None or df.empty:
        return None
    idx = pd.to_datetime(df.index, errors="coerce", utc=True)
    df = df.loc[~idx.isna()]
    df.index = idx[~idx.isna()]
    if df.empty:
        return None
    return _finalise_logger_df(df, cfg, source=csv_path.name)


def _safe_stat(series: pd.Series, fn=np.nanmean, default=nan) -> float:
    """Aggregate a series that may have NaN/be empty; returns default when empty."""
    try:
        s = pd.to_numeric(series, errors="coerce").dropna()
        if len(s) == 0:
            return default
        val = fn(s.values)
        if pd.isna(val):
            return default
        return float(val)
    except Exception:
        return default


def _trip_metrics(
    df: pd.DataFrame,
    t_start: pd.Timestamp,
    t_end: pd.Timestamp,
    cfg: dict,
    mass_fallback_kg: float | None = None,
) -> dict[str, Any]:
    """
    Compute all metrics within a trip time window.

    ``mass_fallback_kg`` is the CVW reading of the previous valid trip (maintained
    by process_diesel_leg), used as a carry-over when the current trip's CVW window
    has no valid samples at all. If the carry-over is also unavailable, it falls
    back to ``cfg['weight_class_t'] * 1000``.
    """
    fuel_col = cfg.get("fuel_energy_col", DEFAULT_FUEL_COL)
    dist_col = cfg.get("distance_col", DEFAULT_DISTANCE_COL)
    mass_col = cfg.get("mass_col", DEFAULT_MASS_COL)
    alt_col = cfg.get("altitude_col", DEFAULT_ALTITUDE_COL)
    temp_col = cfg.get("ambient_temp_col", DEFAULT_AMBIENT_TEMP_COL)
    lhv = float(cfg.get("diesel_lhv_kwh_per_l", DEFAULT_DIESEL_LHV_KWH_PER_L))

    if t_start.tzinfo is None:
        t_start = t_start.tz_localize("UTC")
    if t_end.tzinfo is None:
        t_end = t_end.tz_localize("UTC")

    sl = df.loc[t_start:t_end]
    if sl.empty:
        return {}

    # ── Fuel: cumulative-column differencing ─────────────────────────
    # The LFC counter updates in 0.5 L steps; a short trip window may not tick at
    # all, causing delta=0 on a moving trip. That case is actually "unknown", not
    # "zero consumption" — corrected downstream with a fuel_l IS NULL dist-guard.
    fuel_l = nan
    if fuel_col in sl.columns:
        fuel_series = pd.to_numeric(sl[fuel_col], errors="coerce").dropna()
        if len(fuel_series) >= 2:
            delta = float(fuel_series.iloc[-1] - fuel_series.iloc[0])
            if delta > 0:
                fuel_l = round(delta, 3)
            # delta == 0 on a moving trip → LFC counter did not tick; leave NaN
    energy_kwh = round(fuel_l * lhv, 3) if not np.isnan(fuel_l) else nan

    # ── Distance: cumulative-mileage differencing ────────────────────
    distance_km = nan
    if dist_col in sl.columns:
        d_series = pd.to_numeric(sl[dist_col], errors="coerce").dropna()
        if len(d_series) >= 2:
            delta = float(d_series.iloc[-1] - d_series.iloc[0])
            if delta >= 0:
                distance_km = round(delta, 3)

    # ── Speed-integration fallback ───────────────────────────────────
    speed_col = cfg.get("speed_col", DEFAULT_SPEED_COL)
    if (np.isnan(distance_km) or distance_km == 0) and speed_col in sl.columns:
        spd = pd.to_numeric(sl[speed_col], errors="coerce").fillna(0.0)
        if len(spd) >= 2:
            dt_s = (
                np.diff(sl.index.values).astype("timedelta64[ms]").astype(float)
                / 1000.0
            )
            v_ms = spd.values[:-1] / 3.6
            distance_km = round(float(np.sum(v_ms * dt_s) / 1000.0), 3)

    # ── Average speed ────────────────────────────────────────────────
    dur_s = (t_end - t_start).total_seconds()
    avg_speed = (
        round(distance_km / (dur_s / 3600.0), 2)
        if (not np.isnan(distance_km) and dur_s > 0)
        else nan
    )

    # ── Mass: trip CVW median; if unavailable, take the value via the fallback chain ──
    veh_mass = nan
    veh_mass_cv = nan
    mass_source = "cvw_trip"  # 'cvw_trip' | 'cvw_carryover' | 'weight_class'
    if mass_col in sl.columns:
        m_all = pd.to_numeric(sl[mass_col], errors="coerce")
        # Exclude the 0 broadcast by the CVW counter while stationary
        valid_m = m_all.notna() & (m_all > 0)
        # Prefer moving (speed > threshold) CVW readings only; a trip's
        # stop transients (red lights / load/unload) have equally unreliable CVW.
        # Revert to all > 0 when there are fewer than 1 moving samples in the window.
        thr = float(cfg.get("speed_threshold_kmh", 1.0))
        if speed_col in sl.columns:
            spd_m = pd.to_numeric(sl[speed_col], errors="coerce")
            moving_m = valid_m & spd_m.notna() & (spd_m > thr)
            if moving_m.sum() >= 1:
                valid_m = moving_m
        m = m_all[valid_m]
        if len(m) > 0:
            veh_mass = float(np.nanmedian(m.values))
            if len(m) > 2 and m.mean() > 0:
                veh_mass_cv = round(float(m.std() / m.mean()), 4)
    if np.isnan(veh_mass) and mass_fallback_kg is not None and mass_fallback_kg > 0:
        veh_mass = float(mass_fallback_kg)
        mass_source = "cvw_carryover"
    if np.isnan(veh_mass):
        wc_t = cfg.get("weight_class_t")
        if wc_t is not None:
            veh_mass = float(wc_t) * 1000.0
            mass_source = "weight_class"

    # ── Elevation difference ─────────────────────────────────────────
    elev_diff = nan
    if alt_col in sl.columns:
        e = pd.to_numeric(sl[alt_col], errors="coerce").dropna()
        if len(e) >= 2:
            elev_diff = round(float(e.iloc[-1] - e.iloc[0]), 2)

    # ── Ambient-temperature average ──────────────────────────────────
    # Prefer the Logger Channel 7 weather station (`7 temperature`), because it is
    # a dedicated meteorological sensor and closer to the report's semantic
    # "ambient temperature" than the engine-bay AMB reading. Fall back to AMB when
    # Channel 7 is unavailable.
    temp_avg = nan
    if _W_TEMP in sl.columns:
        temp_avg = _safe_stat(sl[_W_TEMP])
    if np.isnan(temp_avg) and temp_col in sl.columns:
        temp_avg = _safe_stat(sl[temp_col])

    # ── Logger Channel 7 remaining weather fields (pressure / humidity / wind speed / wind direction) ──
    pressure_avg = _safe_stat(sl[_W_PRESS]) if _W_PRESS in sl.columns else nan
    humidity_avg = _safe_stat(sl[_W_HUMID]) if _W_HUMID in sl.columns else nan
    wind_speed_avg = _safe_stat(sl[_W_WIND_S]) if _W_WIND_S in sl.columns else nan
    wind_dir_text = None
    if _W_WIND_D in sl.columns:
        wd_deg = _safe_stat(sl[_W_WIND_D])
        if not np.isnan(wd_deg):
            wind_dir_text = _CARDINALS[round(wd_deg / 45.0) % 8]

    # ── Origin / Destination ─────────────────────────────────────────
    lat_s = lon_s = lat_e = lon_e = None
    if "_lat" in sl.columns and "_lon" in sl.columns:
        lat_series = pd.to_numeric(sl["_lat"], errors="coerce").dropna()
        lon_series = pd.to_numeric(sl["_lon"], errors="coerce").dropna()
        if len(lat_series) >= 1 and len(lon_series) >= 1:
            lat_s = float(lat_series.iloc[0])
            lon_s = float(lon_series.iloc[0])
            lat_e = float(lat_series.iloc[-1])
            lon_e = float(lon_series.iloc[-1])

    # ── Fuel Consumption (L/100km) — the diesel vehicle's primary metric ──
    fuel_consumption_l_per_100km = nan
    if not np.isnan(distance_km) and distance_km > 0 and not np.isnan(fuel_l):
        fuel_consumption_l_per_100km = round(fuel_l / distance_km * 100.0, 3)

    return {
        "start_time": t_start,
        "end_time": t_end,
        "fuel_l": fuel_l,
        "energy_kwh": energy_kwh,
        "distance_km": distance_km,
        "avg_speed": avg_speed,
        "veh_mass": veh_mass,
        "veh_mass_cv": veh_mass_cv,
        "mass_source": mass_source,
        "elev_diff": elev_diff,
        "temp_avg": temp_avg,
        "pressure_avg": pressure_avg,
        "humidity_avg": humidity_avg,
        "wind_speed_avg": wind_speed_avg,
        "wind_dir_text": wind_dir_text,
        "lat_s": lat_s,
        "lon_s": lon_s,
        "lat_e": lat_e,
        "lon_e": lon_e,
        "fuel_consumption_l_per_100km": fuel_consumption_l_per_100km,
    }


def _diesel_seg_to_row(
    seg: dict,
    leg_uri: str,
    cumulative_km: float,
    srf_data=None,
    operator: str | None = None,
) -> tuple[tuple, float]:
    """
    Convert one diesel trip dict into a row tuple (order matches DIESEL_HEADERS, excluding Leg Number).

    The diesel column set is DIESEL_HEADERS, independent of the EV HEADERS — no
    SOC, AC/DC, Battery Capacity, Energy Performance (kWh/km) or other
    electricity-related columns. Stop / Charge are never generated here (diesel
    vehicles have no charging, and Stop rows are synthesised in the
    _insert_stop_rows post-processing).
    """
    t_s = seg["start_time"]
    t_e = seg["end_time"]

    distance = seg.get("distance_km", nan)
    if not np.isnan(distance):
        cumulative_km += distance

    # Only the SRF Logger leg link; diesel vehicles have no FPS telematics and no charger
    logger_url = _build_logger_url(leg_uri, t_s, t_e)

    # Origin / Destination
    origin = _point_str(seg.get("lat_s"), seg.get("lon_s"))
    destination = _point_str(seg.get("lat_e"), seg.get("lon_e"))
    origin_pc = _get_postcode(seg.get("lat_s"), seg.get("lon_s"), srf_data)
    dest_pc = _get_postcode(seg.get("lat_e"), seg.get("lon_e"), srf_data)

    # Duration (fractional days for Excel [hh]:mm:ss format)
    dur_days = (pd.Timestamp(t_e) - pd.Timestamp(t_s)).total_seconds() / 86400.0

    # Leg Type
    leg_type = "In Transit"  # reuse the EV discharge Trip naming, directly comparable across vehicles

    row = (
        leg_type,  # Leg Type
        logger_url,  # SRF Logger Link
        pd.Timestamp(t_s),  # Start Time (UTC)
        origin,  # Origin (Lat, Lon)
        origin_pc,  # Origin Place
        pd.Timestamp(t_e),  # End Time (UTC)
        destination,  # Destination (Lat, Lon)
        dest_pc,  # Destination Place
        dur_days,  # Duration (HH:MM:SS)
        distance,  # Distance (km)
        seg.get("avg_speed", nan),  # Average Speed (km/h)
        seg.get("elev_diff", nan),  # Elevation Difference (m)
        seg.get("veh_mass", nan),  # Vehicle Mass (kg)
        seg.get("veh_mass_cv", nan),  # Vehicle Mass CV (reliability)
        cumulative_km,  # Cumulative Distance (km)
        seg.get("fuel_l", nan),  # Fuel Used (L)
        seg.get("fuel_consumption_l_per_100km", nan),  # Fuel Consumption (L/100km)
        seg.get("temp_avg", nan),  # Average Temperature (C)  — Logger Channel 7
        seg.get("pressure_avg", nan),  # Average Pressure (hPa)   — Logger Channel 7
        seg.get("humidity_avg", nan),  # Average Humidity (%)     — Logger Channel 7
        seg.get("wind_speed_avg", nan),  # Average Wind Speed (m/s) — Logger Channel 7
        seg.get("wind_dir_text") or nan,  # Average Wind Direction   — Logger Channel 7
        nan,  # Weather Type — text label, still needs the OpenWeather WeatherPatcher
        "lfc_fuel",  # Energy Source
        operator,  # Operator (project code)
    )

    assert (
        len(row) == len(DIESEL_HEADERS) - 1
    ), f"diesel row length {len(row)} != expected {len(DIESEL_HEADERS) - 1}"
    return row, cumulative_km


def _segments_from_df(
    df: pd.DataFrame, cfg: dict, source: str = "?"
) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp]], list[dict]]:
    """
    Speed segmentation + per-trip metrics + filter chain, returning ``(trips, seg_metrics)``.

    Shared, package-side segmentation logic with two consumers (DRY):
      * ``process_diesel_leg`` — generates xlsx rows (the caller converts seg_metrics to rows).
      * the report-visuals skill's diesel figure regenerate — re-drives
        this to redraw the 4-panel validation figure without re-running the xlsx.

    The returned ``trips`` are all windows from :func:`find_speed_trips` (for the
    trip shading on the figure); ``seg_metrics`` contains only the trips that pass
    the filter chain (short / pathological trips already dropped), in trip time
    order, with the mass carry-over already applied in sequence internally.
    """
    speed_col = cfg.get("speed_col", DEFAULT_SPEED_COL)
    speed_thr = float(cfg.get("speed_threshold_kmh", 1.0))
    min_stop = float(cfg.get("min_stop_duration_min", 5.0))
    min_trip = float(cfg.get("min_trip_duration_min", 2.0))
    min_trip_km = float(cfg.get("min_trip_distance_km", DEFAULT_MIN_TRIP_DISTANCE_KM))

    trips = find_speed_trips(
        df,
        speed_col=speed_col,
        speed_threshold_kmh=speed_thr,
        min_stop_duration_min=min_stop,
        min_trip_duration_min=min_trip,
    )
    if not trips:
        return [], []

    seg_metrics: list[dict] = []
    # Mass carry-over across trips in the same leg — previous trip's CVW
    # median is used as fallback for a trip whose window has no valid CVW.
    mass_carry: float | None = None
    n_dropped_short = 0
    n_dropped_pathological = 0
    for t_start, t_end in trips:
        seg = _trip_metrics(
            df,
            pd.Timestamp(t_start),
            pd.Timestamp(t_end),
            cfg,
            mass_fallback_kg=mass_carry,
        )
        if not seg:
            continue

        dist = seg.get("distance_km", nan)
        if np.isnan(dist) or dist <= 0:
            continue

        # (1) Drop ghost micro-trips (depot shuffling, GPS noise) below the
        # minimum trip distance. These corrupt fuel/100km because a 0.5 L
        # cold-start cost divided by 300 m gives a triple-digit value.
        if dist < min_trip_km:
            n_dropped_short += 1
            continue

        # (2) Drop pathological trips where Logger channels have no valid
        # data in the window (the 13 m "trip" row 22 case). A trip that
        # doesn't give us at least mass OR fuel OR temperature isn't usable
        # for anything downstream.
        all_nan = (
            np.isnan(seg.get("fuel_l", nan))
            and np.isnan(seg.get("veh_mass", nan))
            and np.isnan(seg.get("temp_avg", nan))
        )
        if all_nan:
            n_dropped_pathological += 1
            continue

        # Promote this trip's CVW median to the carry-over slot so the next
        # trip's fallback chain can use it. We only promote real CVW reads
        # (mass_source == 'cvw_trip'), never fallback values — otherwise a
        # single missing trip would propagate weight_class_t forever.
        if seg.get("mass_source") == "cvw_trip":
            mass_carry = seg["veh_mass"]

        seg_metrics.append(seg)

    if n_dropped_short or n_dropped_pathological:
        logger.debug(
            "  leg %s: dropped %d short (<%.1f km) + %d pathological trips",
            source,
            n_dropped_short,
            min_trip_km,
            n_dropped_pathological,
        )

    return trips, seg_metrics


def process_diesel_leg(
    leg,
    cfg: dict,
    cumulative_km: float,
    srf_data=None,
    out_dir: Path | str | None = None,
    reg: str | None = None,
    debug_mode: bool = False,
    generate_validation_fig: bool = True,
    leg_idx: int = 0,
    reg_code: str | None = None,
    srf_org_raw: str | None = None,
    trial_cache: dict | None = None,
    op_acc: dict | None = None,
) -> tuple[list, float]:
    """
    Process one SRFLOGGER_V1 leg: pull Logger channels, speed segmentation, per-trip computation, and generate row tuples.

    This function no longer paints the diesel validation figure — that is
    the report-visuals skill's job. The ``out_dir`` / ``debug_mode`` /
    ``generate_validation_fig`` / ``leg_idx`` parameters are retained for
    backward-compatible call sites but are now inert (no figure is drawn here); the
    raw logger CSV is still persisted independently by the upstream
    ``_save_logger_data`` (gated on ``debug_mode`` in ``_generator``), and figures
    are rendered later via the report-visuals skill.

    Returns
    -------
    (row_list, cumulative_km)
        row_list: list of (start_time, row_tuple) pairs — the caller keeps the same
                  format as the EV pipeline, so the later sort + stop insertion
                  logic can handle them uniformly.
        cumulative_km: the updated cumulative mileage.
    """
    df = _build_logger_df(leg, cfg)
    if df is None or df.empty:
        return [], cumulative_km

    trips, seg_metrics = _segments_from_df(df, cfg, source=getattr(leg, "uri", "?"))
    if not trips:
        return [], cumulative_km

    # ── Operator code (per-leg) ───────────────────────────────────────────
    # Diesel legs come from SRFLOGGER; trial.description for dedicated vehicles is
    # usually "JOLT - <OP>" (does not match the round-robin regex), falling back to
    # vehicle.organisation.name resolution.
    op_code, op_source, op_unknown = derive_leg_operator(
        leg,
        reg_code or reg,
        srf_org_raw=srf_org_raw,
        vehicles=VEHICLE_CONFIG,
        trial_cache=trial_cache,
    )
    if op_acc is not None:
        op_acc.setdefault(op_code, 0)
        op_acc[op_code] += 1
        if op_unknown:
            op_acc.setdefault("_unknown", set())
            op_acc["_unknown"].add(op_code)

    # Build the row tuples from the kept segments (sequential, so cumulative_km
    # advances trip by trip). ``_segments_from_df`` has already applied the
    # mass carry-over + distance / pathological filtering, so iterating its
    # ``seg_metrics`` here is equivalent to the previous single combined loop.
    rows: list = []
    for seg in seg_metrics:
        row, cumulative_km = _diesel_seg_to_row(
            seg,
            leg.uri,
            cumulative_km,
            srf_data=srf_data,
            operator=op_code,
        )
        rows.append((seg["start_time"], list(row)))

    return rows, cumulative_km
