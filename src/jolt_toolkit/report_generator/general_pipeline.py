"""
report_generator.general_pipeline
==================================
General **fallback** pipeline for registrations that are NOT onboarded in
``configs/vehicles.json``. The platform must never surface an
"onboard first" prompt: any registration that exists on SRF is guaranteed to
produce a structurally valid ``jolt_report_<REG>_<start>_<end>.xlsx`` — for both
EV and diesel — with graceful degradation and clear English log warnings.

Design (see ``.claude/architecture/plan_v310_platform_slim.md`` §2c)
--------------------------------------------------------------------
When ``_generator.JOLTReportGenerator.generate_report`` finds ``reg`` is not in
``VEHICLE_CONFIG`` it calls :func:`build_runtime_vehicle_config`, which:

  1. **SRF resolution** — resolve the input registration (uppercase, no spaces per
     the project convention) to the SRF-stored spelling by trying the verbatim
     form and standard UK spacing variants (``AB12CDE`` → ``AB12 CDE``; NI-style
     ``CMZ6260`` → ``CMZ 6260``; also 6-char forms). The first
     ``srf_data.vehicles.get(obj_id=...)`` that returns a vehicle wins. If none
     resolve → :class:`VehicleNotFoundError` (the ONE case where a clear English
     error is correct: no data exists anywhere).
  2. **Fuel-type routing** — SRF ``vehicle.fuel`` == ``ELECTRIC`` → EV path,
     ``DIESEL`` → diesel path; unknown → probe the fetched legs: an FPS leg's raw
     telematics carrying a SOC column → EV, else SRFLOGGER_V1 legs present →
     diesel, else EV with degradation.
  3. **Runtime config** — assemble a **runtime** vehicle-config dict (NEVER written
     to ``vehicles.json``; no invented entries). EV: auto-detect the energy /
     speed / mass / altitude column names from the first fetched leg's raw
     telematics against the candidate sets used across the fleet, missing columns
     degrade the corresponding feature exactly as the existing code already does
     for a configured vehicle that lacks them. Diesel: the standard SRFLOGGER_V1
     channel set + the ``DEFAULT_*`` constants from ``diesel_pipeline``.

The runtime config carries an internal marker (:data:`_RUNTIME_MARKER`) so the
generator can (a) skip the ``effective_capacity`` write-back and (b) keep treating
the run as a fallback even if the same reg is generated twice in one process (the
first call injects the cfg into the in-memory ``VEHICLE_CONFIG``).

This module owns ONLY the runtime-config construction. The actual segmentation,
row building and Excel writing are the ordinary package pipeline — the runtime
cfg is injected into the in-memory ``VEHICLE_CONFIG`` so
``run_segment_detection`` / ``resolve_mass_agg`` / the diesel pipeline resolve it
by reference exactly as for an onboarded vehicle.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Iterable

import pandas as pd
from srf_client import paging

from jolt_toolkit.report_generator.data_fetcher import fetch_events
from jolt_toolkit.report_generator.diesel_pipeline import (
    DEFAULT_ALTITUDE_COL,
    DEFAULT_AMBIENT_TEMP_COL,
    DEFAULT_DIESEL_LHV_KWH_PER_L,
    DEFAULT_DISTANCE_COL,
    DEFAULT_FUEL_COL,
    DEFAULT_FUEL_RATE_COL,
    DEFAULT_MASS_COL,
    DEFAULT_MIN_TRIP_DISTANCE_KM,
    DEFAULT_SPEED_COL,
    DEFAULT_SPEED_FALLBACK,
)
from jolt_toolkit.report_generator.paths import get_cache_dir
from jolt_toolkit.report_generator.segment_algorithms import SOC_COL

logger = logging.getLogger(__name__)


# Internal marker key placed on a runtime-fallback config. Never written to
# vehicles.json (the config is only ever injected into the in-memory
# VEHICLE_CONFIG). Used by the generator to skip the capacity write-back and to
# recognise an already-injected runtime reg on a repeat generation.
_RUNTIME_MARKER = "_runtime_fallback"


class VehicleNotFoundError(Exception):
    """Raised when a registration cannot be resolved to any SRF vehicle.

    This is the single case where the general fallback pipeline is entitled to
    fail with a clear English error: the vehicle exists nowhere on the platform,
    so there is genuinely no data to report.
    """


# ── EV raw-telematics column candidates (priority order), derived from the ────
# spellings used across the onboarded fleet in vehicles.json. The first candidate
# present in a leg's raw telematics wins; if none are present the field is omitted
# and the corresponding feature degrades exactly as it already does for a
# configured vehicle missing that column.
_SOC_CANDIDATES = (SOC_COL,)  # "electricBatteryLevelPercent"
_EV_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "speed_col": ("wheel_based_speed", "speed"),
    "ac_col": ("battery_pack_ac_watthours",),
    "dc_col": ("battery_pack_dc_watthours",),
    "total_energy_col": (
        "total_electric_energy_used_plugged_in_included",
        "total_electric_energy_used",
    ),
    "moving_energy_col": ("electric_energy_wheelbased_speed_over_zero",),
    "mass_col": ("gross_combination_vehicle_weight",),
    "altitude_col": ("gnss_altitude",),
}


def reg_spacing_variants(reg_input: str) -> list[str]:
    """Return ordered, de-duplicated SRF-registration spelling candidates.

    The JOLT convention stores a registration uppercase with no spaces
    (``YK73WFN``), but SRF stores the DVLA-style spaced spelling (``YK73 WFN``,
    NI-style ``CMZ 6260``). Neither the input nor the SRF spelling is known to be
    canonical for ``vehicles.get(obj_id=...)``, so we try, in order:

      1. the verbatim input with spaces stripped (``YK73WFN``);
      2. the input exactly as given (may already contain a space);
      3. current UK format ``LLNN LLL`` (7 chars) → space after the 4th
         (``YK73 WFN``);
      4. a split at the alpha→digit(→alpha) boundary (NI ``CMZ6260`` →
         ``CMZ 6260``; older ``ABC123D`` forms);
      5. a generic "space before the last three characters" (covers 6-char and
         other short forms).

    Pure / offline — no network.
    """
    raw = (reg_input or "").strip().upper()
    nospace = raw.replace(" ", "")
    variants: list[str] = []

    def _add(candidate: str) -> None:
        if candidate and candidate not in variants:
            variants.append(candidate)

    _add(nospace)
    _add(raw)

    # Current UK format: 2 letters + 2 digits + 3 letters → space after char 4.
    if (
        len(nospace) == 7
        and nospace[:2].isalpha()
        and nospace[2:4].isdigit()
        and nospace[4:].isalpha()
    ):
        _add(nospace[:4] + " " + nospace[4:])

    # Alpha→digit(→alpha) boundary split (NI 'CMZ6260', dateless 'ABC123D').
    m = re.match(r"^([A-Z]+)(\d+)([A-Z]*)$", nospace)
    if m:
        alpha, digits, tail = m.groups()
        if tail:
            _add(f"{alpha}{digits} {tail}")
            _add(f"{alpha} {digits}{tail}")
        else:
            _add(f"{alpha} {digits}")

    # Generic: space before the last three characters (6-char and other forms).
    if len(nospace) >= 5:
        _add(nospace[:-3] + " " + nospace[-3:])

    return variants


def detect_ev_columns(columns: Iterable[str]) -> dict:
    """Auto-detect the EV column-name mapping from a leg's raw telematics columns.

    ``columns`` is the raw telematics header (e.g. the columns of the first
    fetched FPS leg). Returns a dict with a ``has_soc`` flag plus, for every
    resolvable field, the detected column name (fields with no candidate present
    are omitted so the caller falls back to the package default / degrades).

    Pure / offline — no network. Exercised directly by the unit tests with
    synthetic column sets.
    """
    cols = set(columns or [])
    detected: dict = {"has_soc": any(c in cols for c in _SOC_CANDIDATES)}
    for field, candidates in _EV_COLUMN_CANDIDATES.items():
        for cand in candidates:
            if cand in cols:
                detected[field] = cand
                break
    return detected


def build_runtime_config(
    reg: str,
    srf_reg: str,
    *,
    fuel_type: str,
    make: str | None = None,
    model: str | None = None,
    weight_class_t: float | None = None,
    srf_capacity_kwh: float | None = None,
    columns: Iterable[str] | None = None,
) -> dict:
    """Assemble a runtime vehicle-config dict (PURE — no network).

    ``fuel_type`` ∈ {"EV", "DIESEL"} (case-insensitive). For EV the energy /
    speed / mass / altitude columns are auto-detected from ``columns`` (the first
    fetched leg's raw telematics). For diesel the standard SRFLOGGER_V1 channel
    set is used (the diesel pipeline's ``DEFAULT_*`` constants).

    The returned dict is NEVER written to ``vehicles.json``; it is injected into
    the in-memory ``VEHICLE_CONFIG`` only. It carries the internal
    :data:`_RUNTIME_MARKER` key so the generator skips the capacity write-back.
    """
    if str(fuel_type).upper() == "DIESEL":
        cfg: dict = {
            "srf_reg": srf_reg,
            "make": make,
            "model": model,
            "fuel_type": "DIESEL",
            # Dispatch marker (fuel_type==DIESEL + leg_source==SRFLOGGER_V1);
            # deliberately NOT a pipelines.json entry, exactly like the onboarded
            # diesel vehicles (WU70GLV / YT21EFD).
            "pipeline": "daf_diesel_logger",
            "leg_source": "SRFLOGGER_V1",
            # Mass third-level fallback (× 1000 kg). SRF weight_class is in tonnes;
            # None when the API does not expose it (mass then degrades to NaN
            # rather than erroring).
            "weight_class_t": (float(weight_class_t) if weight_class_t else None),
            "diesel_lhv_kwh_per_l": DEFAULT_DIESEL_LHV_KWH_PER_L,
            # Standard SRFLOGGER_V1 channel set (the diesel pipeline already falls
            # back to these DEFAULT_* via cfg.get(...); set them explicitly so the
            # runtime config is self-describing).
            "speed_col": DEFAULT_SPEED_COL,
            "speed_col_fallback": DEFAULT_SPEED_FALLBACK,
            "fuel_energy_col": DEFAULT_FUEL_COL,
            "fuel_rate_col": DEFAULT_FUEL_RATE_COL,
            "distance_col": DEFAULT_DISTANCE_COL,
            "mass_col": DEFAULT_MASS_COL,
            "altitude_col": DEFAULT_ALTITUDE_COL,
            "ambient_temp_col": DEFAULT_AMBIENT_TEMP_COL,
            "min_trip_distance_km": DEFAULT_MIN_TRIP_DISTANCE_KM,
            _RUNTIME_MARKER: True,
        }
        return cfg

    # ── EV path ──────────────────────────────────────────────────────────────
    detected = detect_ev_columns(columns or [])
    cfg = {
        "srf_reg": srf_reg,
        "make": make,
        "model": model,
        "fuel_type": "EV",
        # Unknown nominal capacity → None. cap_lo/hi become None (no bounds) and
        # the SOC-estimate seed falls back to srf_capacity_kwh (if the API gave a
        # fuel_capacity) or None (soc_estimate energy then stays NaN rather than
        # crashing — see capacity._correct_effective_capacity guards).
        "nominal_kwh": None,
        "srf_capacity_kwh": (float(srf_capacity_kwh) if srf_capacity_kwh else None),
        "effective_capacity_kwh": None,
        # Base params from default_soc; branch by SOC presence (soc when the SOC
        # column is present, else speed).
        "pipeline": "default_soc" if detected["has_soc"] else "default_speed",
        _RUNTIME_MARKER: True,
    }
    for field in (
        "speed_col",
        "ac_col",
        "dc_col",
        "total_energy_col",
        "moving_energy_col",
        "mass_col",
        "altitude_col",
    ):
        if detected.get(field):
            cfg[field] = detected[field]
    return cfg


def is_runtime_config(cfg: dict | None) -> bool:
    """True if ``cfg`` is a runtime fallback config (built by this module)."""
    return bool(cfg) and bool(cfg.get(_RUNTIME_MARKER))


# ── SRF-touching orchestration (network) ─────────────────────────────────────


def resolve_srf_vehicle(reg_input: str, srf_data):
    """Resolve an input registration to an SRF vehicle + its stored spelling.

    Tries :func:`reg_spacing_variants` in order via
    ``srf_data.vehicles.get(obj_id=...)``. Returns ``(vehicle_obj, srf_reg)``
    where ``srf_reg`` is the SRF-stored ``registration`` (used verbatim for the
    leg / transaction filters). Raises :class:`VehicleNotFoundError` when no
    spelling resolves — the vehicle does not exist on SRF.
    """
    tried = reg_spacing_variants(reg_input)
    for cand in tried:
        try:
            vehicle = srf_data.vehicles.get(obj_id=cand)
        except Exception as exc:  # a 404 / lookup failure just means "try next"
            logger.debug("SRF vehicle lookup for %r failed: %s", cand, exc)
            vehicle = None
        if vehicle is not None:
            srf_reg = getattr(vehicle, "registration", None) or cand
            logger.info(
                "Resolved un-onboarded registration %r to SRF vehicle %r",
                reg_input,
                srf_reg,
            )
            return vehicle, srf_reg
    raise VehicleNotFoundError(
        f"Vehicle {reg_input!r} was not found on SRF (tried registrations: "
        f"{', '.join(tried)}). No data exists for this registration on the "
        f"platform, so there is nothing to report — check the registration or "
        f"onboard the vehicle."
    )


def _read_fps_leg_columns(leg) -> list[str] | None:
    """Return the raw telematics column names of one FPS leg (cached).

    Uses the SAME application-level cache location and read logic as
    ``_generator._process_fps_legs`` (``<cache>/srf_raw/<leg-hash>.csv``), so a
    peek here populates the cache and the main FPS loop reuses it — no double
    download and byte-identical CSV content.
    """
    try:
        leg_uri_hash = leg.uri.rstrip("/").rsplit("/", 1)[-1]
        cache_path = get_cache_dir() / "srf_raw" / f"{leg_uri_hash}.csv"
        if cache_path.exists():
            df = pd.read_csv(cache_path, dtype=str, nrows=1)
        else:
            raw_chunks = list(leg.get_raw_data())
            if not raw_chunks:
                return None
            csv_text = "\n".join(c for c in raw_chunks if c.strip())
            df = pd.read_csv(io.StringIO(csv_text), dtype=str)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path, index=False)
        return list(df.columns)
    except Exception as exc:
        logger.debug("Runtime FPS-leg column read failed: %s", exc)
        return None


def _peek_legs(srf_reg, date_start, date_end, srf_data):
    """Probe the fetched legs for the fuel-type decision + EV column detection.

    Returns ``(has_soc, has_logger, first_fps_columns)``:
      * ``has_soc`` — the first non-empty FPS leg's raw telematics carries the SOC
        column (→ EV);
      * ``has_logger`` — any SRFLOGGER leg exists (→ diesel when there is no SOC);
      * ``first_fps_columns`` — that FPS leg's raw column names (for EV auto-detect)
        or ``None``.

    Fetches its own ``ServerData`` (the srf HTTP cache dedups the repeat the main
    generator flow issues), so there is no shared-iterator hazard with the main
    ``_collect_legs``.
    """
    has_soc = False
    has_logger = False
    first_cols: list[str] | None = None
    try:
        server_data = fetch_events(
            vehicle_registration=srf_reg,
            date_start=date_start,
            date_end=date_end,
            srf_data=srf_data,
        )
        for leg in paging.paged_items(server_data.legs):
            try:
                src = leg.trip.source or ""
            except Exception:
                src = ""
            if src.startswith("SRFLOGGER"):
                has_logger = True
            elif src == "FPS" and first_cols is None:
                cols = _read_fps_leg_columns(leg)
                if cols:
                    first_cols = cols
                    has_soc = any(c in cols for c in _SOC_CANDIDATES)
            # Once SOC is confirmed the vehicle is EV — no need to scan further.
            if has_soc:
                break
    except Exception as exc:
        logger.warning("Runtime leg probe failed (%s); degrading to EV path", exc)
    return has_soc, has_logger, first_cols


def _resolve_srf_capacity(vehicle_obj) -> float | None:
    """Best-effort battery / fuel capacity (kWh) from SRF metadata, or None.

    The SRF ``Vehicle`` object does not itself expose ``fuel_capacity`` (that lives
    on ``VehicleClass``); we read a few plausible direct attributes and return the
    first positive number, otherwise None. None is a fully supported degradation
    (soc_estimate energies stay NaN; counter-sourced legs still produce EP).
    """
    for attr in ("fuel_capacity", "capacity", "battery_capacity"):
        try:
            val = getattr(vehicle_obj, attr, None)
        except Exception:
            val = None
        if val:
            try:
                cap = float(val)
            except (TypeError, ValueError):
                continue
            if cap > 0:
                return cap
    return None


def build_runtime_vehicle_config(
    reg: str,
    reg_input: str,
    date_start,
    date_end,
    *,
    srf_data,
) -> dict:
    """Resolve + probe + detect + assemble the runtime config for an un-onboarded reg.

    The single network-touching entry point called by
    ``_generator.generate_report`` when ``reg`` is not in ``VEHICLE_CONFIG``.
    Raises :class:`VehicleNotFoundError` when the registration does not exist on
    SRF (the one legitimate failure — no data anywhere).
    """
    vehicle_obj, srf_reg = resolve_srf_vehicle(reg_input, srf_data)

    fuel_raw = (getattr(vehicle_obj, "fuel", None) or "").strip().upper()
    make = getattr(vehicle_obj, "make", None)
    model = getattr(vehicle_obj, "model", None)
    weight_class = getattr(vehicle_obj, "weight_class", None)

    # Fuel-type routing: metadata first, then probe.
    if fuel_raw == "DIESEL":
        fuel_type: str | None = "DIESEL"
    elif fuel_raw == "ELECTRIC":
        fuel_type = "EV"
    else:
        fuel_type = None  # decide by probing the legs

    columns: list[str] | None = None
    if fuel_type in (None, "EV"):
        has_soc, has_logger, first_cols = _peek_legs(
            srf_reg, date_start, date_end, srf_data
        )
        if fuel_type is None:
            if has_soc:
                fuel_type = "EV"
            elif has_logger:
                fuel_type = "DIESEL"
            else:
                fuel_type = "EV"
                logger.warning(
                    "Vehicle %s (SRF fuel=%r): no SOC column in the FPS legs and "
                    "no SRFLOGGER legs — defaulting to the EV path with "
                    "degradation (the report may be sparse/empty).",
                    reg,
                    fuel_raw or None,
                )
        if fuel_type == "EV":
            columns = first_cols

    if fuel_type == "DIESEL":
        cfg = build_runtime_config(
            reg,
            srf_reg,
            fuel_type="DIESEL",
            make=make,
            model=model,
            weight_class_t=weight_class,
        )
    else:
        srf_cap = _resolve_srf_capacity(vehicle_obj)
        cfg = build_runtime_config(
            reg,
            srf_reg,
            fuel_type="EV",
            make=make,
            model=model,
            srf_capacity_kwh=srf_cap,
            columns=columns,
        )

    logger.info(
        "Runtime fallback config for %s: fuel_type=%s make=%s model=%s "
        "pipeline=%s srf_reg=%r capacity=%s kWh",
        reg,
        cfg.get("fuel_type"),
        make,
        model,
        cfg.get("pipeline"),
        srf_reg,
        cfg.get("srf_capacity_kwh"),
    )
    return cfg
