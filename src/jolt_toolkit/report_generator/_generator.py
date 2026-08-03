"""
report_generator.py
=========================
Orchestrates the fetch -> segment -> write main flow.
Uses segment_algorithms for the unified charge/discharge segmentation, and
report_builder to generate the Excel report and optional validation figures.
"""

import datetime
import io
import logging
import os
import re
from pathlib import Path
from time import perf_counter

import pandas as pd
from srf_client import paging
from tqdm import tqdm

from jolt_toolkit.report_generator.capacity import (  # noqa: F401
    _DAY_NS,
    _IDX_BPOWER,
    _IDX_CAP,
    _IDX_DISTANCE,
    _IDX_DURATION,
    _IDX_ELEV,
    _IDX_ENERGY,
    _IDX_EP_EXCL_AUX,
    _IDX_EPERF,
    _IDX_EPERF_CORR,
    _IDX_EPERF_KIN,
    _IDX_ESOURCE,
    _IDX_LEG_TYPE,
    _IDX_MASS,
    _IDX_SOC_CHANGE,
    _IDX_START,
    CAP_WINDOW_HALF_DAYS,
    MIN_DONORS,
    SOC_FALLBACK_MIN_DEV,
    SOC_FALLBACK_MIN_DSOC_PCT,
    _cap_is_valid,
    _correct_effective_capacity,
    _period_capacity_from_rows,
    _persist_effective_capacity,
    _recompute_weighted_capacity,
    _resolve_soc_fallback,
    _row_idx,
    _soc_weighted_cap,
)
from jolt_toolkit.report_generator.data_class import ServerData
from jolt_toolkit.report_generator.data_fetcher import fetch_events
from jolt_toolkit.report_generator.diesel_pipeline import (
    process_diesel_leg,
)
from jolt_toolkit.report_generator.general_pipeline import (  # noqa: F401
    VehicleNotFoundError,
    build_runtime_vehicle_config,
    is_runtime_config,
)
from jolt_toolkit.report_generator.operators import derive_leg_operator
from jolt_toolkit.report_generator.paths import get_cache_dir, get_srf_api_root
from jolt_toolkit.report_generator.report_builder import (
    DIESEL_HEADERS,
    HEADERS,
    _insert_stop_rows,
    _seg_to_row,
    _write_excel_report,
)
from jolt_toolkit.report_generator.segment_algorithms import (
    _ANCHOR_PRIVATE_KEYS,
    PIPELINE_CONFIGS,
    SOC_COL,
    TIME_COL,
    VEHICLE_CONFIG,
    resolve_mass_agg,
    run_segment_detection,
)
from jolt_toolkit.report_generator.xlsx_patch_common import make_srf_client

logger = logging.getLogger(__name__)


# ── Logger value conversion: a superset of srf_client.pandas.to_numeric ───────
# srf_client's default to_numeric only handles numbers and the "NaN" string; for
# J1939 boolean fields (e.g. CCVS cruise control active / brake switch / clutch
# switch's "true"/"false") it indiscriminately returns math.nan, causing these
# columns to be entirely lost in the Logger CSV. Here the boolean strings are
# mapped to 0/1 while the original behaviour for other values is preserved — a
# strict superset.
_LOGGER_BOOL_TRUE = frozenset({"true", "True", "TRUE"})
_LOGGER_BOOL_FALSE = frozenset({"false", "False", "FALSE"})


def _logger_to_numeric(v: str):
    """Boolean-aware numeric converter, for leg.get_data_frame(conversion=...)."""
    import math

    if v in _LOGGER_BOOL_TRUE:
        return 1
    if v in _LOGGER_BOOL_FALSE:
        return 0
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return math.nan


def _ensure_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the DataFrame index is in the UTC time zone."""
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _load_logger_channel(
    logger_legs: list,
    channels: list[tuple[str, str | tuple[str, ...]]],
    *,
    target_col: str,
    desc: str,
) -> pd.DataFrame | None:
    """Generic function to load a given channel's data from Logger legs.

    Args:
        logger_legs: list of Logger leg objects
        channels:    list of (channel_name, column_name_or_candidates) tuples in priority order.
                     column_name may be a single string or a tuple of candidate column names (tried in priority order).
        target_col:  column name of the output DataFrame
        desc:        tqdm progress-bar description
    Returns:
        The merged DataFrame (indexed by UTC timestamps) or None
    """
    dfs = []
    for lg in tqdm(logger_legs, desc=desc, leave=False):
        try:
            avail = lg.types
            for channel, col_spec in channels:
                if channel in avail:
                    df_ch = lg.get_data_frame(channel, resolution="1s")
                    # col_spec supports a single column name or multiple candidate column names
                    candidates = (col_spec,) if isinstance(col_spec, str) else col_spec
                    for col in candidates:
                        if col in df_ch.columns:
                            dfs.append(df_ch[[col]].rename(columns={col: target_col}))
                            break
                    break  # use the first matching channel
        except Exception as exc:
            logger.debug(
                "%s: failed to read a Logger leg's channel data: %s", desc, exc
            )
    if not dfs:
        return None
    result = pd.concat(dfs).sort_index()
    return _ensure_utc_index(result)


class JOLTReportGenerator:
    """Fetch data from the SRF API, run the segmentation algorithm, and generate the Excel report."""

    def __init__(
        self,
        report_output_folder: str = "./excel_report_database",
        overwrite_existing_report: bool = True,
        debug_mode: bool = False,
        fast_mode: bool = False,
        save_figures: bool = True,
    ) -> None:
        """
        save_figures
            **No-op**, retained only for backward-compatible call
            sites. The package no longer paints validation figures or writes the
            inspect HTML during generation — that is the report-visuals skill's
            job (it re-drives ``run_segment_detection`` / the diesel segmentation
            with its own painter). ``debug_mode`` still governs raw-artefact
            persistence (raw telematics + raw logger/charger CSVs); this flag no
            longer changes any output.
        """
        self.srf_data = self._make_srf_data(
            api_key=os.environ.get("SRF_API_KEY"),
            root=get_srf_api_root(),
            cache_dir=str(get_cache_dir()),
            verify=True,
        )
        self.report_output_folder = report_output_folder
        self.overwrite_existing_report = overwrite_existing_report
        self.debug_mode = debug_mode
        self.fast_mode = fast_mode
        self.save_figures = save_figures

    def generate_report(
        self, vehicle_registration: str, date_start: str, date_end: str
    ) -> str | None:
        """Generate a JOLT Excel report for the given vehicle and date range. Returns the Excel path or None."""
        time_start = perf_counter()
        logger.info(
            "Starting report generation: %s  %s ~ %s",
            vehicle_registration,
            date_start,
            date_end,
        )

        reg = vehicle_registration.replace(" ", "")
        # Date bounds are needed by the runtime-fallback probe below (before the
        # main fetch), so parse them up front; pure computation, unchanged for
        # onboarded vehicles.
        ds = datetime.datetime.strptime(date_start, "%Y-%m-%d")
        de = datetime.datetime.strptime(date_end, "%Y-%m-%d")

        cfg = VEHICLE_CONFIG.get(reg)
        # ── General fallback pipeline for un-onboarded registrations ──────────
        # A registration absent from vehicles.json no longer errors: build a
        # RUNTIME vehicle config from SRF metadata + column auto-detection and
        # inject it into the in-memory VEHICLE_CONFIG (never written to disk), so
        # the ordinary pipeline resolves it by reference. Only when the vehicle
        # does not exist on SRF at all does this raise (VehicleNotFoundError).
        if cfg is None:
            cfg = build_runtime_vehicle_config(
                reg,
                vehicle_registration,
                ds,
                de,
                srf_data=self.srf_data,
            )
            VEHICLE_CONFIG[reg] = cfg  # in-memory only — never persisted
            logger.info(
                "Vehicle %s is not onboarded — using the general fallback "
                "pipeline (generic parameters; for tuned segmentation onboard "
                "the vehicle).",
                reg,
            )
        is_runtime_cfg = is_runtime_config(cfg)
        reg_srf = cfg["srf_reg"]
        # Report-period string (YYYYMMDD_YYYYMMDD), 1:1 with the quarterly report / quarterly schema key
        period_key = f"{date_start.replace('-', '')}_{date_end.replace('-', '')}"
        # Diesel vehicles have no battery, skip all capacity-related fields
        is_diesel = str(cfg.get("fuel_type", "")).upper() == "DIESEL"
        nominal_kwh = cfg.get("nominal_kwh") if not is_diesel else None
        srf_capacity_kwh = cfg.get("srf_capacity_kwh", nominal_kwh)
        # effective_capacity_kwh: the donor-weighted average over all reliable
        # quarters; None when not computed, falling back to srf_capacity.
        eff_cap_kwh = cfg.get("effective_capacity_kwh")
        # SOC-estimate capacity seed: prefer **this report period**'s
        # reliable (n ≥ MIN_DONORS) quarterly capacity, otherwise the whole-fleet
        # weighted-average effective_capacity_kwh, otherwise srf_capacity /
        # nominal. This lets each period's SOC seed use that period's corresponding
        # capacity (subsequently still overwritten by the per-leg time-local
        # correction, so it only affects the fallback value when the window has no
        # donor at all, not the measured-leg values).
        quarterly_cfg = cfg.get("effective_capacity_quarterly") or {}
        this_q = quarterly_cfg.get(period_key)
        if (
            this_q
            and this_q.get("n", 0) >= MIN_DONORS
            and this_q.get("kwh") is not None
        ):
            soc_est_cap = this_q["kwh"]
        else:
            soc_est_cap = eff_cap_kwh or srf_capacity_kwh or nominal_kwh
        altitude_col = cfg.get("altitude_col")
        speed_col = cfg.get("speed_col", "wheel_based_speed")
        # Per-segment mass aggregation method (vehicle > pipeline > default
        # 'mean'). Resolved once and passed through to each _seg_to_row so the Excel
        # column and the validation figure use the same robust estimate.
        mass_agg = resolve_mass_agg(
            reg, PIPELINE_CONFIGS.get(cfg.get("pipeline", "default_soc"))
        )
        cap_lo = nominal_kwh * 0.5 if nominal_kwh else None
        cap_hi = nominal_kwh * 2.0 if nominal_kwh else None

        out_dir = Path(self.report_output_folder) / reg
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = None
        if self.debug_mode:
            raw_dir = out_dir / "raw_telematics"
            raw_dir.mkdir(exist_ok=True)

        server_data = fetch_events(
            vehicle_registration=reg_srf,
            date_start=ds,
            date_end=de,
            srf_data=self.srf_data,
        )
        time_fetch = perf_counter()
        logger.info("Data fetch complete, took %.2f seconds", time_fetch - time_start)

        charger_windows, charger_objects = self._collect_charger_windows(server_data)

        logger_windows, logger_legs, logger_legs_by_src, fps_legs = self._collect_legs(
            server_data, is_diesel, charger_windows
        )

        if self.debug_mode:
            self._save_charger_data(charger_objects, out_dir)
            for src_name, src_legs in logger_legs_by_src.items():
                self._save_logger_data(src_legs, out_dir, source=src_name)

        (
            logger_speed_all,
            logger_mass_all,
            logger_acc_pedal_all,
            logger_dec_pedal_all,
        ) = self._preload_logger_channels(logger_legs, is_diesel)

        charger_meter_all = self._preload_charger_meter(charger_objects)

        all_rows = []
        cumulative_km = 0.0
        home_point = None

        # ── Operator resolution ─────────────────────────────────────────
        # SRF-preferred: round-robin vehicles take leg.trip.trial.description
        # (varies per leg), dedicated vehicles take vehicle.organisation.name
        # (consistent for the whole vehicle). Fetch the static org once, memoise
        # trial.description by trip URI, and op_acc aggregates a one-off log.
        srf_org_raw = None
        try:
            srf_org_raw = self.srf_data.vehicles.get(obj_id=reg_srf).organisation.name
        except Exception as exc:
            logger.debug("Could not fetch vehicle.organisation.name: %s", exc)
        trial_cache: dict = {}
        op_acc: dict = {}

        # ── Diesel branch: use the SRFLOGGER_V1 legs as the main loop source ────
        if is_diesel:
            cumulative_km = self._process_diesel_legs(
                logger_legs,
                cfg,
                reg,
                out_dir,
                cumulative_km,
                srf_org_raw,
                trial_cache,
                op_acc,
                all_rows,
            )
            # Skip the FPS main loop and home_point / charger reclassification
            fps_legs = []

        cumulative_km, home_point = self._process_fps_legs(
            fps_legs,
            reg,
            out_dir,
            raw_dir,
            cap_lo,
            cap_hi,
            logger_speed_all,
            logger_mass_all,
            logger_acc_pedal_all,
            logger_dec_pedal_all,
            charger_meter_all,
            logger_legs,
            altitude_col,
            speed_col,
            mass_agg,
            srf_org_raw,
            trial_cache,
            op_acc,
            home_point,
            cumulative_km,
            all_rows,
        )

        self._reclassify_home_charging(all_rows, home_point)

        time_process = perf_counter()
        logger.info(
            "Segmentation processing complete, took %.2f seconds",
            time_process - time_fetch,
        )

        all_rows.sort(
            key=lambda x: (
                pd.Timestamp(x[0]).tz_convert(None)
                if pd.Timestamp(x[0]).tzinfo is not None
                else pd.Timestamp(x[0])
            )
        )
        sorted_rows = [r for _, r in all_rows]

        if not sorted_rows:
            if is_runtime_cfg:
                # Ultimate-degradation guarantee: an un-onboarded vehicle
                # never surfaces an "onboard first" error. With zero usable
                # segments we still write a structurally complete, header-only
                # report so the caller always gets a valid xlsx.
                logger.warning(
                    "No usable segments for un-onboarded vehicle %s — writing a "
                    "structurally complete empty report (header row only, no data "
                    "rows). Onboard the vehicle for a tuned, populated report.",
                    reg,
                )
                # fall through to write the empty report
            else:
                logger.warning("No valid segments detected, skipping Excel generation")
                return None

        # Choose the column layout by fuel type (EV HEADERS / diesel DIESEL_HEADERS)
        out_headers = DIESEL_HEADERS if is_diesel else HEADERS

        # ── One-off operator-resolution summary ─────────────────────────
        if op_acc:
            unknown = op_acc.pop("_unknown", set())
            dist = ", ".join(
                f"{(k if k is not None else '<none>')}={v}"
                for k, v in sorted(op_acc.items(), key=lambda kv: -kv[1])
            )
            logger.info("Operator resolution (counted by leg): %s", dist or "none")
            bad = {c for c in unknown if c is not None}
            if bad:
                logger.warning(
                    "Operator codes not in KNOWN_OPERATOR_CODES: %s",
                    ", ".join(sorted(bad)),
                )
            if None in op_acc:
                logger.warning(
                    "%d legs could not have their operator determined", op_acc[None]
                )

        sorted_rows, period_cap_kwh, period_n, period_src = self._finalize_rows(
            sorted_rows, out_headers, is_diesel, cfg, soc_est_cap
        )

        return self._write_outputs(
            sorted_rows,
            out_headers,
            reg,
            ds,
            de,
            out_dir,
            is_diesel,
            period_cap_kwh,
            period_n,
            period_src,
            period_key,
            charger_windows,
            logger_legs,
            logger_windows,
            time_start,
            is_runtime_cfg=is_runtime_cfg,
        )

    def _collect_charger_windows(self, server_data):
        """Collect charger transactions into (start, end, uri, energy_kwh) windows
        and keep the raw charger objects. fast_mode skips both."""
        charger_windows = []
        charger_objects = []
        if not self.fast_mode:
            try:
                for ct in paging.paged_items(server_data.charging_events):
                    try:
                        energy_kwh = None
                        if ct.start_meter is not None and ct.end_meter is not None:
                            energy_kwh = ct.end_meter - ct.start_meter
                        charger_windows.append(
                            (ct.start_time, ct.end_time, ct.uri, energy_kwh)
                        )
                        charger_objects.append(ct)
                    except AttributeError as exc:
                        logger.debug(
                            "Charger transaction missing an expected attribute; skipped: %s",
                            exc,
                        )
            except Exception as exc:
                logger.warning("Charger-event fetch failed: %s", exc)
        return charger_windows, charger_objects

    def _collect_legs(self, server_data, is_diesel, charger_windows):
        """Split legs into SRFLOGGER (logger) and FPS; fast_mode still collects the
        logger legs for diesel. Logs the per-source counts."""
        logger_windows = []
        logger_legs = []  # all logger legs
        logger_legs_by_src = {}  # {source_str: [legs]} grouped by version
        fps_legs = []
        # A diesel vehicle's legs all come from SRFLOGGER_V1, so fast_mode must still collect them
        _collect_logger = (not self.fast_mode) or is_diesel
        try:
            for leg in paging.paged_items(server_data.legs):
                try:
                    src = leg.trip.source or ""
                    if _collect_logger and src.startswith("SRFLOGGER"):
                        logger_windows.append((leg.start_time, leg.end_time, leg.uri))
                        logger_legs.append(leg)
                        logger_legs_by_src.setdefault(src, []).append(leg)
                    elif src == "FPS":
                        fps_legs.append(leg)
                except AttributeError as exc:
                    logger.debug(
                        "Leg missing an expected attribute (trip.source); skipped: %s",
                        exc,
                    )
        except Exception as exc:
            logger.warning("Legs fetch failed: %s", exc)

        if self.fast_mode:
            logger.info(
                "Fast mode: skipping Charger/Logger data. FPS legs: %d", len(fps_legs)
            )
        else:
            logger.info(
                "FPS legs: %d  Chargers: %d  Logger: %d (%s)",
                len(fps_legs),
                len(charger_windows),
                len(logger_windows),
                ", ".join(f"{k}={len(v)}" for k, v in logger_legs_by_src.items())
                or "none",
            )
        return logger_windows, logger_legs, logger_legs_by_src, fps_legs

    def _preload_logger_channels(self, logger_legs, is_diesel):
        """Preload the speed / mass / accelerator / brake logger channels once for
        the whole period (EV only; diesel pulls its channels per leg)."""
        # ── Preload each Logger channel's data ───────────────────────────
        # A diesel vehicle's diesel_pipeline pulls the needed channels per leg and does not rely on preloading.
        logger_speed_all = None
        logger_mass_all = None
        logger_acc_pedal_all = None
        logger_dec_pedal_all = None
        if logger_legs and not is_diesel:
            logger_speed_all = _load_logger_channel(
                logger_legs,
                [("CCVS", "CCVS wheel based vehicle speed"), ("2", "2 speed")],
                target_col="logger_speed",
                desc="Loading Logger speed",
            )
            if logger_speed_all is not None:
                logger.info("Logger speed data: %d rows", len(logger_speed_all))

            logger_mass_all = _load_logger_channel(
                logger_legs,
                [
                    ("CVW", "CVW gross combination vehicle weight"),
                    ("VW", ("VW axle weight", "VW cargo weight")),
                ],
                target_col="logger_mass",
                desc="Loading Logger mass",
            )
            if logger_mass_all is not None:
                logger.info("Logger mass data: %d rows", len(logger_mass_all))

            logger_acc_pedal_all = _load_logger_channel(
                logger_legs,
                [("EEC2", "EEC2 accelerator pedal position 1")],
                target_col="EEC2 accelerator pedal position 1",
                desc="Loading Logger EEC2",
            )
            if logger_acc_pedal_all is not None:
                logger.info(
                    "Logger EEC2 accelerator-pedal data: %d rows",
                    len(logger_acc_pedal_all),
                )

            logger_dec_pedal_all = _load_logger_channel(
                logger_legs,
                [("EBC1", "EBC1 brake pedal position")],
                target_col="EBC1 brake pedal position",
                desc="Loading Logger EBC1",
            )
            if logger_dec_pedal_all is not None:
                logger.info(
                    "Logger EBC1 brake-pedal data: %d rows", len(logger_dec_pedal_all)
                )
        return (
            logger_speed_all,
            logger_mass_all,
            logger_acc_pedal_all,
            logger_dec_pedal_all,
        )

    def _preload_charger_meter(self, charger_objects):
        """Preload charger start/end meter readings into a time-indexed frame for
        the validation figures (debug_mode only)."""
        # ── Preload Charger meter data (for the validation figures) ────────
        charger_meter_all = None
        if self.debug_mode and charger_objects:
            meter_rows = []
            for ct in charger_objects:
                try:
                    if ct.start_meter is not None and ct.end_meter is not None:
                        ts_start = pd.Timestamp(ct.start_time)
                        ts_end = pd.Timestamp(ct.end_time)
                        if ts_start.tzinfo is None:
                            ts_start = ts_start.tz_localize("UTC")
                        if ts_end.tzinfo is None:
                            ts_end = ts_end.tz_localize("UTC")
                        meter_rows.append(
                            {"time": ts_start, "meter_kwh": ct.start_meter}
                        )
                        meter_rows.append({"time": ts_end, "meter_kwh": ct.end_meter})
                except Exception as exc:
                    logger.debug(
                        "Charger meter reading unavailable for a transaction; skipped: %s",
                        exc,
                    )
            if meter_rows:
                charger_meter_all = pd.DataFrame(meter_rows).sort_values("time")
                charger_meter_all = charger_meter_all.set_index("time")
        return charger_meter_all

    def _process_diesel_legs(
        self,
        logger_legs,
        cfg,
        reg,
        out_dir,
        cumulative_km,
        srf_org_raw,
        trial_cache,
        op_acc,
        all_rows,
    ):
        """Diesel branch: build rows from SRFLOGGER_V1 legs via process_diesel_leg,
        appending them to all_rows. Returns the updated cumulative_km."""
        logger.info("Diesel branch: processing %d SRFLOGGER legs", len(logger_legs))
        for leg_idx, leg in enumerate(
            tqdm(logger_legs, desc="Processing diesel logger legs")
        ):
            try:
                # process_diesel_leg no longer paints figures (the
                # report-visuals skill does). The diesel raw logger CSV is written
                # independently by _save_logger_data (gated on debug_mode). The
                # out_dir / reg / leg_idx / debug_mode args are retained for
                # backward-compatible call-site parity but are inert here.
                trip_rows, cumulative_km = process_diesel_leg(
                    leg,
                    cfg,
                    cumulative_km,
                    srf_data=self.srf_data,
                    out_dir=out_dir,
                    reg=reg,
                    reg_code=reg,
                    srf_org_raw=srf_org_raw,
                    trial_cache=trial_cache,
                    op_acc=op_acc,
                    debug_mode=self.debug_mode,
                    leg_idx=leg_idx,
                )
                all_rows.extend(trip_rows)
            except Exception:
                logger.exception(
                    "Diesel leg processing failed: %s", getattr(leg, "uri", "?")
                )
        return cumulative_km

    def _process_fps_legs(
        self,
        fps_legs,
        reg,
        out_dir,
        raw_dir,
        cap_lo,
        cap_hi,
        logger_speed_all,
        logger_mass_all,
        logger_acc_pedal_all,
        logger_dec_pedal_all,
        charger_meter_all,
        logger_legs,
        altitude_col,
        speed_col,
        mass_agg,
        srf_org_raw,
        trial_cache,
        op_acc,
        home_point,
        cumulative_km,
        all_rows,
    ):
        """Main EV loop: per FPS leg cache raw telematics, run segmentation, build
        charge/discharge rows, and detect the home charging point. Returns the
        updated (cumulative_km, home_point)."""
        for leg_idx, leg in enumerate(tqdm(fps_legs, desc="Processing FPS legs")):
            try:
                # ── Application-level cache: cache the raw telematics CSV by leg URI ──
                leg_uri_hash = leg.uri.rstrip("/").rsplit("/", 1)[-1]
                cache_path = get_cache_dir() / "srf_raw" / f"{leg_uri_hash}.csv"

                if cache_path.exists():
                    df_leg = pd.read_csv(cache_path, dtype=str)
                else:
                    raw_chunks = list(leg.get_raw_data())
                    if not raw_chunks:
                        continue
                    csv_text = "\n".join(c for c in raw_chunks if c.strip())
                    df_leg = pd.read_csv(io.StringIO(csv_text), dtype=str)
                    # Cache to disk
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    df_leg.to_csv(cache_path, index=False)
            except Exception as exc:
                logger.warning("Leg %d read failed: %s", leg_idx, exc)
                continue

            if df_leg.empty or SOC_COL not in df_leg.columns:
                continue

            leg_date = str(leg.start_time.date())
            leg_uri = leg.uri
            suffix = f"{leg_date}_{leg_idx:04d}"

            if self.debug_mode:
                raw_name = f"raw_{suffix}.csv"
                df_leg.to_csv(raw_dir / raw_name, index=False)

            # Current leg's time window
            ts_s = pd.Timestamp(leg.start_time)
            ts_e = pd.Timestamp(leg.end_time)
            if ts_s.tzinfo is None:
                ts_s = ts_s.tz_localize("UTC")
            if ts_e.tzinfo is None:
                ts_e = ts_e.tz_localize("UTC")

            # Slice the current leg's time window from the logger speed data
            leg_logger_spd = None
            if logger_speed_all is not None:
                try:
                    leg_logger_spd = logger_speed_all.loc[ts_s:ts_e]
                    if leg_logger_spd.empty:
                        leg_logger_spd = None
                except Exception:
                    leg_logger_spd = None

            # Slice the current leg's time window from the logger mass data
            leg_logger_mass = None
            if logger_mass_all is not None:
                try:
                    leg_logger_mass = logger_mass_all.loc[ts_s:ts_e]
                    if leg_logger_mass.empty:
                        leg_logger_mass = None
                except Exception:
                    leg_logger_mass = None

            # Slice the current leg's time window from the charger meter data
            leg_charger_meter = None
            if charger_meter_all is not None:
                try:
                    leg_charger_meter = charger_meter_all.loc[ts_s:ts_e]
                    if leg_charger_meter.empty:
                        leg_charger_meter = None
                except Exception:
                    leg_charger_meter = None

            # Figures are painted by the report-visuals skill via an
            # external figure_hook, never inline here — so no hook is passed and
            # segmentation runs figure-free. The raw CSV was written independently
            # above (gated on debug_mode).
            c_segs, d_segs = run_segment_detection(
                df_leg,
                reg=reg,
                suffix=suffix,
                out_dir=None,
                generate_validation_fig=False,
                cap_lo=cap_lo,
                cap_hi=cap_hi,
                logger_speed_df=leg_logger_spd,
                logger_mass_df=leg_logger_mass,
                charger_meter_df=leg_charger_meter,
            )

            # ── Operator code (per-leg; shared by all segments of the same leg) ──
            op_code, _op_src, _op_unknown = derive_leg_operator(
                leg,
                reg,
                srf_org_raw=srf_org_raw,
                vehicles=VEHICLE_CONFIG,
                trial_cache=trial_cache,
            )
            op_acc.setdefault(op_code, 0)
            op_acc[op_code] += 1
            if _op_unknown:
                op_acc.setdefault("_unknown", set())
                op_acc["_unknown"].add(op_code)

            for seg in c_segs:
                seg_clean = {
                    k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS
                }
                row, _ = _seg_to_row(
                    seg_clean,
                    "charge",
                    leg_uri,
                    [],
                    [],  # charger/logger links filled in by the patcher
                    df_leg,
                    cumulative_km,
                    home_point,
                    srf_data=self.srf_data,
                    altitude_col=altitude_col,
                    speed_col=speed_col,
                    operator=op_code,
                    mass_agg=mass_agg,
                )
                all_rows.append((seg["start_time"], list(row)))

            for seg in d_segs:
                seg_clean = {
                    k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS
                }
                row, cumulative_km = _seg_to_row(
                    seg_clean,
                    "discharge",
                    leg_uri,
                    [],
                    [],  # charger/logger links filled in by the patcher
                    df_leg,
                    cumulative_km,
                    home_point,
                    srf_data=self.srf_data,
                    altitude_col=altitude_col,
                    speed_col=speed_col,
                    has_logger=bool(logger_legs),
                    logger_speed_all=logger_speed_all,
                    logger_acc_pedal_all=logger_acc_pedal_all,
                    logger_dec_pedal_all=logger_dec_pedal_all,
                    operator=op_code,
                    mass_agg=mass_agg,
                )
                all_rows.append((seg["start_time"], list(row)))

            if home_point is None and c_segs:
                from geopy import Point as GeoPoint

                for s in c_segs:
                    lat_h = s.get("latitude")
                    lon_h = s.get("longitude")
                    if lat_h is not None and lon_h is not None:
                        try:
                            home_point = GeoPoint(float(lat_h), float(lon_h))
                            logger.info("Home point: (%.4f, %.4f)", lat_h, lon_h)
                        except Exception as exc:
                            logger.debug(
                                "Home point construction failed for a charge segment: %s",
                                exc,
                            )
                        if home_point is not None:
                            break
        return cumulative_km, home_point

    def _reclassify_home_charging(self, all_rows, home_point):
        """Post-process: relabel 'Away' charge rows within 0.5 km of the detected
        home point as 'Home' (mutates all_rows in place)."""
        # ── Post-processing: reclassify charge segments using the detected home_point ──
        # The first charge segments were classified as Away while home_point was unknown; corrected here
        if home_point is not None:
            from geopy import Point as GeoPoint
            from geopy.distance import geodesic

            _ri_leg_type = _row_idx("Leg Type")
            _ri_origin = _row_idx("Origin (Lat, Lon)")
            reclassified = 0
            for _, row in all_rows:
                lt = row[_ri_leg_type]
                if not isinstance(lt, str) or "Away" not in lt:
                    continue
                origin_str = row[_ri_origin]
                if not origin_str or not isinstance(origin_str, str):
                    continue
                m = re.match(
                    r"Point\(([+-]?\d+\.?\d*)\s+([+-]?\d+\.?\d*)\)", origin_str
                )
                if not m:
                    continue
                lat_f, lon_f = float(m.group(1)), float(m.group(2))
                try:
                    if geodesic(home_point, GeoPoint(lat_f, lon_f)).km < 0.5:
                        row[_ri_leg_type] = lt.replace("Away", "Home")
                        reclassified += 1
                except Exception as exc:
                    logger.debug(
                        "Home-distance reclassification failed for a row: %s", exc
                    )
            if reclassified:
                logger.info(
                    "Charge-segment reclassification: %d rows Away → Home", reclassified
                )

    def _finalize_rows(self, sorted_rows, out_headers, is_diesel, cfg, soc_est_cap):
        """Effective-capacity correction (EV) + non-discharge EP scrub + per-period
        donor capacity + Stop-row insertion. Returns
        (sorted_rows, period_cap_kwh, period_n, period_src)."""
        # ── Post-processing: effective capacity correction ──────────────
        # Diesel vehicles have no SOC / battery capacity, skip this step.
        if is_diesel:
            computed_eff_cap = None
            cap_source = "diesel"
            period_cap_kwh = period_n = None
            period_src = "diesel"
        else:
            sorted_rows, computed_eff_cap, cap_source = (
                self._correct_effective_capacity(
                    sorted_rows,
                    _IDX_CAP,
                    _IDX_ENERGY,
                    _IDX_SOC_CHANGE,
                    _IDX_EPERF,
                    _IDX_DISTANCE,
                    _IDX_ESOURCE,
                    _IDX_BPOWER,
                    _IDX_DURATION,
                    _IDX_EPERF_CORR,
                    _IDX_ELEV,
                    _IDX_MASS,
                    soc_est_cap,
                    _IDX_EPERF_KIN,
                    idx_start=_IDX_START,
                    soc_fallback=_resolve_soc_fallback(cfg),
                )
            )

            # Bug fix: a charge row's Energy Performance should always be NaN, but
            # some paths (e.g. the effective-capacity step-2 re-derivation after
            # removing ±1σ outliers, for a charge event with a wrongly-filled
            # distance > 0) accidentally write an EP value. Here we explicitly clear
            # the three EP columns of all non-discharge rows as a final safeguard.
            for row in sorted_rows:
                lt = row[_IDX_LEG_TYPE]
                if not isinstance(lt, str):
                    continue
                lt_low = lt.strip().lower()
                if lt_low == "stop" or re.match(
                    r"^(ac|dc|charge|mix|estimated)", lt_low
                ):
                    row[_IDX_EPERF] = float("nan")
                    row[_IDX_EPERF_CORR] = float("nan")
                    row[_IDX_EPERF_KIN] = float("nan")
                    row[_IDX_EP_EXCL_AUX] = float("nan")

            # This report period's donor-based capacity (kwh, n), same
            # convention as _correct_effective_capacity's avg_eff_cap (charge-preferred
            # measured-leg mean). Computed before Stop rows are inserted, on the same
            # sorted_rows after _correct, for _persist_effective_capacity to merge
            # into the quarterly weighted average.
            period_cap_kwh, period_n, period_src = _period_capacity_from_rows(
                sorted_rows, _IDX_CAP, _IDX_SOC_CHANGE, _IDX_ESOURCE
            )

        # ── Post-processing: fill in Stop rows (stationary segments between trip/charge) ──
        # Must run AFTER sorting + effective capacity correction so that the
        # Stop rows pick up the corrected SOC/mass endpoints from their
        # neighbours.
        n_before_stops = len(sorted_rows)
        sorted_rows = _insert_stop_rows(sorted_rows, headers=out_headers)
        n_stops_added = len(sorted_rows) - n_before_stops
        if n_stops_added:
            logger.info(
                "Inserted %d Stop rows (%d rows total)", n_stops_added, len(sorted_rows)
            )
        return sorted_rows, period_cap_kwh, period_n, period_src

    def _write_outputs(
        self,
        sorted_rows,
        out_headers,
        reg,
        ds,
        de,
        out_dir,
        is_diesel,
        period_cap_kwh,
        period_n,
        period_src,
        period_key,
        charger_windows,
        logger_legs,
        logger_windows,
        time_start,
        is_runtime_cfg: bool = False,
    ):
        """Persist effective capacity (EV), write the xlsx, then run the
        charger/logger patchers (EV only). Returns the report path (or the
        existing path when overwrite is disabled).

        ``is_runtime_cfg``: the vehicle is un-onboarded (a runtime fallback
        config). The ``effective_capacity`` write-back is skipped — no invented
        vehicles.json entry is created — and the computed capacity is logged
        instead.
        """
        # Persist effective_capacity to vehicles.json (merge semantics:
        # write this period's quarterly[period_key] = {kwh, n}, then recompute the
        # donor weighted average from all reliable quarters). Skipped for diesel
        # (no battery) and for un-onboarded runtime configs (never invent a
        # vehicles.json entry).
        if is_runtime_cfg:
            logger.info(
                "Un-onboarded vehicle %s: computed effective capacity = %s kWh "
                "(n=%s, source=%s) — NOT persisted to vehicles.json (runtime "
                "fallback config).",
                reg,
                round(period_cap_kwh, 1) if period_cap_kwh is not None else None,
                period_n,
                period_src,
            )
        elif not is_diesel:
            self._persist_effective_capacity(
                reg, period_cap_kwh, period_n, period_src, period_key
            )

        ds_str = ds.strftime("%Y%m%d")
        de_str = de.strftime("%Y%m%d")
        report_name = f"jolt_report_{reg}_{ds_str}_{de_str}.xlsx"
        report_path = out_dir / report_name

        if report_path.exists() and not self.overwrite_existing_report:
            logger.info(
                "Report already exists and overwrite is disabled: %s", report_path
            )
            return str(report_path)

        period_start = ds.date()
        period_end = de.date()
        _write_excel_report(
            sorted_rows, reg, period_start, period_end, report_path, headers=out_headers
        )

        if self.debug_mode:
            # Debug persists raw data only — no figures, no inspect HTML
            # from the package. Render them via the report-visuals skill.
            logger.info(
                "Raw data persisted; render validation figures/inspect HTML "
                "via the report-visuals skill."
            )

        time_write = perf_counter()

        # ── Post-processing: fill in Charger/Logger data with the patchers ───────
        # A diesel vehicle already has mass / temperature / links filled directly
        # from the Logger channels in the main pipeline, and has no charge events,
        # so both patchers are skipped.
        if not self.fast_mode and not is_diesel:
            from jolt_toolkit.report_generator.charger_patcher import ChargerPatcher
            from jolt_toolkit.report_generator.logger_patcher import LoggerPatcher

            ChargerPatcher(srf_data=self.srf_data).patch_file(
                report_path, charger_windows=charger_windows
            )
            LoggerPatcher(srf_data=self.srf_data).patch_file(
                report_path, logger_legs=logger_legs, logger_windows=logger_windows
            )

        logger.info("Report written: %s", report_path)
        logger.info(
            "Excel write complete, took %.2f seconds", perf_counter() - time_write
        )
        logger.info("Total report time: %.2f seconds", perf_counter() - time_start)
        return str(report_path)

    @staticmethod
    def _save_charger_data(charger_objects, out_dir):
        """Save the charger-transaction summary data to CSV (merge with existing data, dedup by uri).

        Delegates to charger_patcher.merge_save_charger_transactions — each run
        only fetches this period's transactions, and overwriting directly would
        make the raw_charger CSV forget history outside the window, so it instead
        "merges with the existing CSV, dedups by uri (keep latest), sorts by
        start_time". The schema is identical to the old version.
        """
        from jolt_toolkit.report_generator.charger_patcher import (
            merge_save_charger_transactions,
        )

        merge_save_charger_transactions(charger_objects, out_dir)

    @staticmethod
    def _save_logger_data(logger_legs, out_dir, source: str = "SRFLOGGER"):
        """Save SRF Logger data to CSV via get_data_frame."""
        if not logger_legs:
            return
        # Name the folder by version: SRFLOGGER_V1 → raw_logger_v1
        suffix = source.replace("SRFLOGGER", "").strip("_").lower()
        dir_name = f"raw_logger_{suffix}" if suffix else "raw_logger"
        logger_dir = out_dir / dir_name
        logger_dir.mkdir(exist_ok=True)
        saved = 0
        for idx, leg in enumerate(logger_legs):
            try:
                available = leg.types
                if not available:
                    continue
                df = leg.get_data_frame(
                    list(available),
                    resolution="1s",
                    conversion=_logger_to_numeric,
                )
                if df is None or df.empty:
                    continue
                leg_date = str(leg.start_time.date())
                csv_name = f"logger_{leg_date}_{idx:04d}.csv"
                df.to_csv(logger_dir / csv_name)
                saved += 1
            except Exception as exc:
                logger.warning("Logger leg %d read failed: %s", idx, exc)
        if saved:
            logger.info("Saved Logger data: %d legs -> %s", saved, logger_dir.name)

    @staticmethod
    def _make_srf_data(api_key, root, cache_dir=None, verify=True):
        try:
            return make_srf_client(cache_dir, api_key=api_key, root=root, verify=verify)
        except Exception as e:
            logger.error("SRF client creation failed: %s", e)
            raise


# The two capacity post-processing helpers now live in ``report_generator.capacity``;
# re-expose them as staticmethods so existing call sites (``JOLTReportGenerator.
# _correct_effective_capacity`` in scripts.recompute_from_cache, and ``self.<name>``
# inside generate_report) keep resolving unchanged.
JOLTReportGenerator._correct_effective_capacity = staticmethod(
    _correct_effective_capacity
)
JOLTReportGenerator._persist_effective_capacity = staticmethod(
    _persist_effective_capacity
)
