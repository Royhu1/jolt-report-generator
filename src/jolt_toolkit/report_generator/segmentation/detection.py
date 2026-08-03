"""
Top-level orchestrator: run charge + discharge segmentation for one leg and
optionally render the validation figure.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .constants import (
    AC_COL,
    DC_COL,
    MASS_COL,
    MIN_CLUSTER_GAP_KG,
    MOVING_COL,
    MOVING_SPEED_THRESHOLD_KMH,
    PIPELINE_CONFIGS,
    TIME_COL,
    TOTAL_ENERGY_COL,
    VEHICLE_CONFIG,
)
from .mass_aggregation import resolve_mass_agg
from .mass_clustering import (
    _enforce_anchor_ordering,
    _recompute_anchors,
    cluster_mass_data,
    merge_discharge_by_mass,
    split_discharge_by_mass,
)
from .soc_detection import (
    find_charge_segments_by_soc,
    find_discharge_segments_by_soc,
)
from .speed_detection import (
    find_discharge_segments_by_speed,
    find_speed_trips,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Wrapper: run charge + discharge segmentation together + optionally render the validation figure
# =============================================================================
def run_segment_detection(
    df_raw: pd.DataFrame,
    reg: str,
    suffix: str,
    out_dir=None,
    generate_validation_fig: bool = True,
    charge_params: dict | None = None,
    discharge_params: dict | None = None,
    cap_lo: float | None = None,
    cap_hi: float | None = None,
    logger_speed_df: pd.DataFrame | None = None,
    logger_mass_df: pd.DataFrame | None = None,
    charger_meter_df: pd.DataFrame | None = None,
    export_dsoc_overlay: bool = False,
    *,
    figure_hook: Callable | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Run the charge and discharge segmentation algorithms together on one leg's raw data.

    Automatically extracts each energy column name and the nominal capacity from
    VEHICLE_CONFIG[reg] and injects them into
    find_charge_segments_by_soc / find_discharge_segments_by_soc, ensuring the
    column-name mapping and the SOC-estimate fallback both use the vehicle's
    correct configuration.

    Parameters
    ----------
    df_raw   : raw telemetry DataFrame (single leg)
    reg      : vehicle registration (e.g. 'AV24LXK'), used to look up VEHICLE_CONFIG
    suffix   : leg identifier (e.g. '2024-10-01_0000'), used in the figure file name
    out_dir  : output directory (validation figures saved to out_dir/validation_figures/)
    generate_validation_fig : whether to invoke ``figure_hook`` (default True). Has
                       no effect unless ``figure_hook`` is also provided.
    charge_params    : extra parameter dict passed to find_charge_segments_by_soc
    discharge_params : extra parameter dict passed to find_discharge_segments_by_soc
    cap_lo, cap_hi   : capacity thresholds, passed to both algorithms
    logger_speed_df  : optional Logger speed DataFrame (for validation figure Panel 1 right axis)
    logger_mass_df   : optional Logger CVW mass DataFrame (for validation figure Panel 4)
    export_dsoc_overlay : passed through to ``figure_hook``. When True, the
                       rounded-corner data annotation boxes on all panels
                       (dSOC / energy delta / charger / regen / mass) are NOT baked
                       into the PNG; instead a ``<png>.boxes.json`` sidecar is
                       written for the inspect HTML to do the interactive overlay.
    figure_hook : optional keyword-only external painter. The package no
                       longer imports matplotlib or paints validation figures
                       itself; the ``report-visuals`` skill supplies its own painter
                       here so figures come out identical in a single pass. When
                       ``None`` (the default), NO figure is drawn and everything
                       else is unchanged.

                       When provided AND ``generate_validation_fig`` is True AND
                       ``out_dir`` is not None, it is invoked EXACTLY where the old
                       ``plot_leg_validation`` call sat (after split / merge /
                       ``_recompute_anchors`` / ``_enforce_anchor_ordering``), with
                       the SAME argument names and values ``plot_leg_validation``
                       received:

                           figure_hook(
                               df_raw,               # positional: augmented df,
                                                     #   incl. mass_cluster / mass_moving
                               charge_segs,          # positional
                               discharge_segs,       # positional
                               reg,                  # positional
                               suffix,               # positional
                               out_path,             # positional: out_dir /
                                                     #   validation_figures /
                                                     #   f"validation_{reg}_{suffix}.png"
                               ac_col=...,
                               dc_col=...,
                               panel3_col=...,        # resolved total/moving energy col
                               mass_col=...,
                               speed_col=...,
                               logger_speed_df=logger_speed_df,
                               logger_mass_df=logger_mass_df,
                               charger_meter_df=charger_meter_df,
                               mass_from_logger=...,  # bool: Logger-CVW fallback used
                               mass_agg=...,          # resolved robust aggregator
                               export_dsoc_overlay=export_dsoc_overlay,
                           )

                       The hook is expected to create ``out_path.parent`` and write
                       the PNG (+ optional ``.boxes.json`` sidecar); this function
                       does not touch the filesystem for figures.

    Returns
    -------
    (charge_segs, discharge_segs)
    The _anchor_* fields are included; the caller must filter them out before
    saving to CSV (see _ANCHOR_PRIVATE_KEYS).
    """
    cfg = VEHICLE_CONFIG.get(reg, {})
    _ac_col = cfg.get("ac_col", AC_COL)
    _dc_col = cfg.get("dc_col", DC_COL)
    _tot_col = cfg.get("total_energy_col", TOTAL_ENERGY_COL)
    _mov_col = cfg.get("moving_energy_col", MOVING_COL)
    _nominal = cfg.get("nominal_kwh")
    _srf_cap = cfg.get("srf_capacity_kwh", _nominal)
    _eff_cap = cfg.get("effective_capacity_kwh")
    # Capacity priority for the SOC estimate: effective > srf > nominal
    _soc_est_cap = _eff_cap or _srf_cap or _nominal

    # Pipeline params as defaults; caller-passed overrides take precedence
    _pipeline_name = cfg.get("pipeline", "default_soc")
    _pipeline_cfg = PIPELINE_CONFIGS.get(
        _pipeline_name, PIPELINE_CONFIGS["default_soc"]
    )
    c_params = dict(_pipeline_cfg.get("charge_params", {}))
    c_params.update(charge_params or {})
    d_params = dict(_pipeline_cfg.get("discharge_params", {}))
    d_params.update(discharge_params or {})

    if cap_lo is not None:
        c_params.setdefault("cap_lo", cap_lo)
        d_params.setdefault("cap_lo", cap_lo)
    if cap_hi is not None:
        c_params.setdefault("cap_hi", cap_hi)
        d_params.setdefault("cap_hi", cap_hi)

    # Inject the column-name mapping (overriding any stale values in params)
    c_params["ac_col"] = _ac_col
    c_params["dc_col"] = _dc_col
    c_params["moving_energy_col"] = _mov_col
    # SOC-estimate fallback capacity: effective > srf > nominal
    if _soc_est_cap is not None:
        c_params.setdefault("nominal_kwh", _soc_est_cap)

    d_params["total_energy_col"] = _tot_col
    d_params["moving_energy_col"] = _mov_col
    if _soc_est_cap is not None:
        d_params.setdefault("nominal_kwh", _soc_est_cap)

    # Inject the pipeline top-level min_trip_distance_km (default 0.0 = no
    # filtering, backward compatible). Used only by the soc branch; the speed
    # branch is controlled by find_speed_trips's min_trip_duration_min.
    _min_trip_km = float(_pipeline_cfg.get("min_trip_distance_km", 0.0))
    if _min_trip_km > 0.0:
        d_params.setdefault("min_trip_distance_km", _min_trip_km)

    # ── pipeline branch ────────────────────────────────────────────────────
    # The algorithm branch is decided by PIPELINE_CONFIGS[pipeline_name]['branch'].
    # To add an algorithm branch: add an elif branch == '...' here and implement
    # the corresponding function.
    branch = _pipeline_cfg.get("branch", "soc")

    if branch == "soc":
        charge_segs = find_charge_segments_by_soc(df_raw, **c_params)
        discharge_segs = find_discharge_segments_by_soc(df_raw, **d_params)

    elif branch == "speed":
        charge_segs = find_charge_segments_by_soc(df_raw, **c_params)
        # Speed-segmentation parameters
        _speed_col = cfg.get("speed_col", "wheel_based_speed")
        speed_p = dict(_pipeline_cfg.get("speed_params", {}))
        speed_p["speed_col"] = _speed_col
        # Per-vehicle min_stop_duration_min override (vehicles.json): defaults to
        # the pipeline speed_params value; a vehicle that needs a wider
        # stop-bridge gap (e.g. TA70WTL's ~7-min pickup/drop pauses that should
        # not split a single round) can raise it without touching the shared
        # pipeline value (renault_speed also serves N88GNW / T88RNW). Only
        # vehicles that set the key are affected.
        _min_stop_override = cfg.get("min_stop_duration_min")
        if _min_stop_override is not None:
            speed_p["min_stop_duration_min"] = float(_min_stop_override)
        # Pass the energy columns and capacity parameters from the vehicle config
        speed_p["total_energy_col"] = _tot_col
        speed_p["moving_energy_col"] = _mov_col
        if _soc_est_cap:
            speed_p.setdefault("nominal_kwh", _soc_est_cap)
        if cap_lo:
            speed_p.setdefault("cap_lo", cap_lo)
        if cap_hi:
            speed_p.setdefault("cap_hi", cap_hi)
        # Trip endpoint anchoring strategy (pipeline top-level field).
        # zero_speed is the fleet-wide default; a pipeline can explicitly set
        # trip_endpoint_anchor: "first_motion" to revert to the old behaviour.
        # See the find_speed_trips() docstring for the zero_speed mode.
        _anchor = _pipeline_cfg.get("trip_endpoint_anchor", "zero_speed")
        _max_ext = float(_pipeline_cfg.get("max_extend_minutes", 5.0))
        speed_p["trip_endpoint_anchor"] = _anchor
        speed_p["max_extend_minutes"] = _max_ext
        # When telematics speed is unavailable, or the vehicle config sets
        # prefer_logger_speed, use Logger speed to detect the trip windows.
        # prefer_logger_speed: telematics speed exists but is unreliable (e.g.
        # YN25RSY is almost all-zero with only sporadic noise, so .any() would
        # wrongly deem it usable) → explicitly force the reliable Logger speed
        # (consistent with the diesel logger paradigm), avoiding falling back to
        # the SOC fallback which would cut out a huge segment tracking the SOC drop.
        _prefer_logger = bool(cfg.get("prefer_logger_speed", False))
        _has_tele_speed = (
            _speed_col in df_raw.columns
            and pd.to_numeric(df_raw[_speed_col], errors="coerce")
            .pipe(lambda s: s.notna() & (s > 0))
            .any()
        )
        if (
            (_prefer_logger or not _has_tele_speed)
            and logger_speed_df is not None
            and not logger_speed_df.empty
        ):
            # Build a DataFrame for trip detection from the Logger speed DataFrame
            _logger_spd_df = pd.DataFrame(
                {
                    TIME_COL: logger_speed_df.index,
                    _speed_col: logger_speed_df.iloc[:, 0].values,
                }
            )
            _logger_trips = find_speed_trips(
                _logger_spd_df,
                speed_col=_speed_col,
                speed_threshold_kmh=speed_p.get("speed_threshold_kmh", 1.0),
                min_stop_duration_min=speed_p.get("min_stop_duration_min", 5.0),
                min_trip_duration_min=speed_p.get("min_trip_duration_min", 2.0),
                trip_endpoint_anchor=_anchor,
                max_extend_minutes=_max_ext,
            )
            speed_p["trips"] = _logger_trips
            _logger_reason = (
                "using Logger Speed (prefer_logger_speed)"
                if _prefer_logger
                else "telematics speed unavailable, falling back to Logger Speed"
            )
            logger.info(
                f"  speed data: {_logger_reason}"
                f" (detected {len(_logger_trips)} trips)"
            )
        discharge_segs = find_discharge_segments_by_speed(df_raw, **speed_p)
        # Fallback: if speed segmentation yields nothing, fall back to SOC-based
        if not discharge_segs:
            discharge_segs = find_discharge_segments_by_soc(df_raw, **d_params)

    else:
        raise ValueError(
            f"Unknown algorithm branch {branch!r} for pipeline {_pipeline_name!r} "
            f"(vehicle {reg!r}). Supported: soc, speed"
        )

    # ── Mass clustering + cluster-based split and merge ──────────────────────
    _m_col = cfg.get("mass_col", MASS_COL)
    _mass_from_logger = False
    # Check whether the telematics mass data is valid; if not, and Logger mass is present, fall back
    _has_tele_mass = False
    if _m_col in df_raw.columns:
        _tele_mass = pd.to_numeric(df_raw[_m_col], errors="coerce")
        _has_tele_mass = bool((_tele_mass.notna() & (_tele_mass > 0)).any())
    if not _has_tele_mass and logger_mass_df is not None and not logger_mass_df.empty:
        # Logger mass fallback: merge the Logger CVW into df_raw's mass column
        df_raw = df_raw.copy()
        if _m_col not in df_raw.columns:
            df_raw[_m_col] = np.nan
        _times_utc = pd.to_datetime(df_raw[TIME_COL], errors="coerce", utc=True)
        _df_times = (
            pd.DataFrame(
                {
                    "_idx": df_raw.index,
                    "_time": _times_utc,
                }
            )
            .dropna(subset=["_time"])
            .sort_values("_time")
        )
        _log_df = pd.DataFrame(
            {
                "_time": logger_mass_df.index,
                "_logger_mass": logger_mass_df.iloc[:, 0].values,
            }
        ).sort_values("_time")
        _merged = pd.merge_asof(
            _df_times,
            _log_df,
            on="_time",
            tolerance=pd.Timedelta("5min"),
            direction="nearest",
        )
        # df_raw is read with dtype=str, so floats must be cast to str to avoid a TypeError
        _mass_vals = _merged["_logger_mass"].values
        df_raw[_m_col] = df_raw[_m_col].astype(object)
        df_raw.loc[_merged["_idx"].values, _m_col] = _mass_vals
        _mass_from_logger = True
        logger.info(
            "  mass data: telematics mass unavailable, falling back to Logger CVW"
        )

    if cfg.get("split_by_mass", True) and _m_col in df_raw.columns:
        # 1. Cluster the mass data over the whole leg, adding mass_cluster + mass_moving columns
        #    Cluster means use only "moving" mass readings (stationary GCVW
        #    is unreliable); if the mass-from-logger path has no telematics speed
        #    column, cluster_mass_data automatically falls back to clustering all
        #    valid readings (low-risk, no behaviour change).
        _gap_kg = cfg.get("min_cluster_gap_kg", MIN_CLUSTER_GAP_KG)
        _split_speed_col = cfg.get("speed_col", "wheel_based_speed")
        _speed_p_top = (
            _pipeline_cfg.get("speed_params", {}) if branch == "speed" else {}
        )
        _move_thr = float(
            _speed_p_top.get("speed_threshold_kmh", MOVING_SPEED_THRESHOLD_KMH)
        )
        df_raw = cluster_mass_data(
            df_raw,
            mass_col=_m_col,
            min_cluster_gap_kg=_gap_kg,
            speed_col=_split_speed_col,
            speed_threshold_kmh=_move_thr,
        )
        # 2. Split discharge segments where the cluster label changes (load/unload events)
        #    Scheme B: a v=0 sample must exist within ±W/2 of the split point,
        #    otherwise it is treated as a CVW noise spike. W reuses the speed
        #    parameter min_stop_duration_min (consistent with the zero_speed anchor).
        # Honour the per-vehicle min_stop_duration_min override here too, so the
        # zero-speed split window stays coherent with the trip-detection gap.
        _min_stop_min = float(
            cfg.get(
                "min_stop_duration_min", _speed_p_top.get("min_stop_duration_min", 5.0)
            )
        )
        _split_window_s = _min_stop_min * 60.0
        discharge_segs = split_discharge_by_mass(
            discharge_segs,
            df_raw,
            speed_col=_split_speed_col,
            zero_speed_window_seconds=_split_window_s,
        )
        # 3. Merge adjacent discharge segments with the same cluster (removing spurious splits not from load/unload events)
        #    The pipeline top-level `merge_by_mass: false` can disable this merge
        #    (keeping the split). Use case: vehicles like scania_speed_00 /
        #    scania_speed_01 / volvo_speed_03 whose mass cluster is unchanged all
        #    day — the merge would combine all trips into a single long In Transit.
        #    Per-vehicle override (vehicles.json `merge_by_mass`) wins over the
        #    pipeline flag; both default to True. This lets a single vehicle on a
        #    shared pipeline (e.g. TA70WTL on renault_speed) disable the merge
        #    without affecting its pipeline siblings (N88GNW / T88RNW stay merge-ON).
        _merge_by_mass = cfg.get(
            "merge_by_mass", _pipeline_cfg.get("merge_by_mass", True)
        )
        if _merge_by_mass:
            # Long-stationary split (opt-in): enabled by vehicles.json's
            # split_long_stops_min (minutes). Only vehicles that set this key
            # (currently Nestlé: EV73SAL / YK73WFN) are affected; for other
            # vehicles cfg.get(...) is None → merge behaviour is unchanged verbatim.
            _long_stop_min = cfg.get("split_long_stops_min")
            discharge_segs = merge_discharge_by_mass(
                discharge_segs,
                df_raw,
                charge_segs=charge_segs,
                max_merge_gap_min=_long_stop_min,
            )
        # 4. Recompute the energy anchors lost by the split (used for validation-figure annotations)
        _recompute_anchors(discharge_segs, df_raw, _tot_col, _mov_col)

    # ── Enforce non-overlapping anchors ────────────────────────────────────
    # Run on the final discharge segments (after split / merge / _recompute_anchors):
    # clamp the double-counting of energy between adjacent segments caused by a
    # sparse cumulative counter (anchor_end(i) > anchor_start(i+1)). Placed
    # outside the split_by_mass block and before the validation figure → the
    # final segments of both the speed and soc branches are covered.
    _n_clamped = _enforce_anchor_ordering(discharge_segs, reg)
    if _n_clamped:
        logger.info(
            "  anchor-overlap correction: %d segments clamped (%s %s)",
            _n_clamped,
            reg,
            suffix,
        )

    # ── Validation-figure seam ──────────────────────────────────────────────
    # The package no longer paints figures or imports matplotlib. When an external
    # ``figure_hook`` is supplied (by the report-visuals skill), it is called here
    # — at exactly the point, and with exactly the arguments, the former inline
    # ``plot_leg_validation`` used — so the painter reproduces identical figures in
    # a single pass. With no hook (the default) this block is a no-op.
    if figure_hook is not None and generate_validation_fig and out_dir is not None:
        # Panel 3 column: prefer total_energy_col (if the discharge segments actually used it)
        panel3_col = _mov_col  # default
        if discharge_segs:
            if discharge_segs[0].get("energy_source") == "total_energy":
                panel3_col = _tot_col
        else:
            # With no discharge segments, check whether total_energy_col has valid data in df
            if _tot_col in df_raw.columns:
                if pd.to_numeric(df_raw[_tot_col], errors="coerce").notna().sum() > 0:
                    panel3_col = _tot_col

        val_dir = Path(out_dir) / "validation_figures"
        out_path = val_dir / f"validation_{reg}_{suffix}.png"
        _mass_col = cfg.get("mass_col", MASS_COL)
        _speed_col = cfg.get("speed_col", "wheel_based_speed")
        _mass_agg = resolve_mass_agg(reg, _pipeline_cfg)
        figure_hook(
            df_raw,
            charge_segs,
            discharge_segs,
            reg,
            suffix,
            out_path,
            ac_col=_ac_col,
            dc_col=_dc_col,
            panel3_col=panel3_col,
            mass_col=_mass_col,
            speed_col=_speed_col,
            logger_speed_df=logger_speed_df,
            logger_mass_df=logger_mass_df,
            charger_meter_df=charger_meter_df,
            mass_from_logger=_mass_from_logger,
            mass_agg=_mass_agg,
            export_dsoc_overlay=export_dsoc_overlay,
        )

    return charge_segs, discharge_segs
