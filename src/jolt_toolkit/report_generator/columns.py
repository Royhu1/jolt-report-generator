"""
report_generator.columns
========================
Report column contracts: the EV ``HEADERS`` / diesel ``DIESEL_HEADERS`` tuples,
the row-tuple index helper, the leg-type predicates, the ``_is_nan`` guard, and
the telematics source-column name constants. Imports nothing package-internal so
every other report-builder module can depend on it without a cycle.

Split out of report_builder.py, which re-exports these names for backward
compatibility.
"""

from __future__ import annotations

import re

import numpy as np

# ── Excel report column headers (EV; identical field order to the legacy JOLT LegRecord) ──
HEADERS = (
    "Leg Number",
    "Leg Type",
    "Telematics Link",
    "Charger Link",
    "SRF Logger Link",
    "Start Time (UTC)",
    "Origin (Lat, Lon)",
    "Origin Place",
    "End Time (UTC)",
    "Destination (Lat, Lon)",
    "Destination Place",
    "Duration (HH:MM:SS)",
    "Distance (km)",
    "Average Speed (km/h)",
    "Elevation Difference (m)",
    "Vehicle Mass (kg)",
    "Vehicle Mass CV (reliability)",
    "Recuperation Energy (kWh)",
    "Start SOC (%)",
    "End SOC (%)",
    "SOC Change (%)",
    "Energy Change (kWh)",
    "Energy Charged AC (kWh)",
    "Energy Charged DC (kWh)",
    "CO2 level (g/kWh)",
    "Cumulative Distance (km)",
    "CO2 for event (g)",
    "Cumulative CO2 (g)",
    "Battery Power (kW)",
    "Energy Performance (kWh/km)",
    "Energy Performance Corrected by Elevation Difference (kWh/km)",
    "Battery Capacity (kWh)",
    "Energy Output from Charger (kWh)",
    "Wire Energy Efficiency (kWh/kWh)",
    "Peak Charging (kW)",
    "Average Charging (kW)",
    "Energy based on motor power (kWh)",
    "Average Temperature (C)",
    "Average Pressure (hPa)",
    "Average Humidity (%)",
    "Average Wind Speed (m/s)",
    "Average Wind Direction",
    "Weather Type",
    "Histogram of Accelerator Pedal Position",
    "Histogram of Decelerator Pedal Position",
    # Additional columns
    "Energy Source",
    "Energy Performance Kinetics Corrected (kWh/km)",
    # Total motor propulsion energy over the trip (kWh), from the
    # telematics `electric_energy_propulsion` cumulative counter (Wh), differenced
    # by time-window interpolation. Difference from 'Energy Change (kWh)':
    # propulsion does **not** deduct regenerative-braking recovery — it counts only
    # forward drive; commonly used to back-calculate η_BM. EV only; diesel goes
    # through DIESEL_HEADERS and does not carry this column.
    "Propulsion Energy (kWh)",
    # Net traction energy performance (kWh/km) after removing the
    # auxiliary / parked loads (HVAC, low-voltage systems, etc.). Definition in
    # data_analysis_workspace/energy_balance_check/report.md:
    #   EP_exclude_aux = (propulsion − recuperation) / distance = EP − auxiliary/distance
    # Equivalent derivation: the SRF identity total = propulsion + auxiliary −
    # recuperation, and a discharge trip's EP = |Energy Change| / dist ≈ total /
    # dist, hence
    #   EP − aux/dist = (total − aux)/dist = (propulsion − recuperation)/dist.
    # Uses the two trip-level quantities propulsion and recuperation directly,
    # without solving for aux.
    # Computable only when the counters (propulsion / recuperation) are both
    # non-empty for the trip, otherwise NaN — this naturally blanks
    # EX74JXW / EX74JXY / YN25RSY / YN75NMA (counters NaN/missing).
    # Appended at the **end** of HEADERS: LoggerPatcher / WeatherPatcher's
    # hard-coded column indices (temp=38 / wind=41 / link=5 / mass=16 / kin=47)
    # are all ≤ 47 and unaffected. Diesel goes through DIESEL_HEADERS and does not
    # carry this column.
    "EP_exclude_aux",
    # The per-vehicle, per-leg project operator CODE. The source
    # cascade is in report_generator/operators.py (SRF preferred: round-robin
    # takes leg.trip.trial.description, dedicated vehicles take
    # vehicle.organisation.name; vehicles.json is the fallback).
    # Appended at the **end** of HEADERS, moving no existing column index
    # (LoggerPatcher / WeatherPatcher's hard-coded indices and _generator's
    # _IDX_* are all ≤ 48 and unaffected).
    "Operator",
)

# ── Diesel-only column headers (a distinct set, not the EV HEADERS) ──
# Keeps only fields with physical meaning for diesel: no SOC, AC/DC charge energy,
# Battery Capacity, Energy Performance (kWh/km) or other electricity-related
# columns. Fuel consumption is expressed in L and L/100km.
DIESEL_HEADERS = (
    "Leg Number",
    "Leg Type",
    "SRF Logger Link",
    "Start Time (UTC)",
    "Origin (Lat, Lon)",
    "Origin Place",
    "End Time (UTC)",
    "Destination (Lat, Lon)",
    "Destination Place",
    "Duration (HH:MM:SS)",
    "Distance (km)",
    "Average Speed (km/h)",
    "Elevation Difference (m)",
    "Vehicle Mass (kg)",
    "Vehicle Mass CV (reliability)",
    "Cumulative Distance (km)",
    "Fuel Used (L)",
    "Fuel Consumption (L/100km)",
    "Average Temperature (C)",
    "Average Pressure (hPa)",
    "Average Humidity (%)",
    "Average Wind Speed (m/s)",
    "Average Wind Direction",
    "Weather Type",
    "Energy Source",
    # Operator code, aligning the diesel column set with the EV
    # one. Appended at the **end** (the diesel row appends it at the end, and the
    # length assertion len(row) == len(DIESEL_HEADERS) - 1 follows automatically).
    "Operator",
)


_CHARGE_LEG_RE = re.compile(r"^(AC|DC|Charge|Mix|estimated)", re.IGNORECASE)


def _leg_is_charge(leg_type) -> bool:
    """True if a Leg Type string denotes a charge segment (red row)."""
    return bool(
        leg_type and isinstance(leg_type, str) and _CHARGE_LEG_RE.match(leg_type)
    )


def _leg_is_stop(leg_type) -> bool:
    """True if a Leg Type string denotes a Stop segment (white row)."""
    return bool(
        leg_type and isinstance(leg_type, str) and leg_type.strip().lower() == "stop"
    )


def is_trip_leg(leg_type) -> bool:
    """True if a Leg Type denotes a driving / trip segment.

    The single shared definition of a "trip" (driving) row: a non-blank Leg Type
    that is neither a charge segment (:func:`_leg_is_charge`) nor a Stop
    (:func:`_leg_is_stop`) — i.e. one of "In House" / "Round Trip" / "Outbound" /
    "Return" / "In Transit" (see :func:`_get_leg_type`). Public because the
    weather patchers import it: weather is backfilled on trip rows ONLY (charge
    and Stop rows do not need weather and would only waste OpenWeather quota), so
    the patchers and the chart ``driving_only`` filter agree on what counts as a
    driving row.
    """
    if not leg_type or not isinstance(leg_type, str) or not leg_type.strip():
        return False
    return not _leg_is_charge(leg_type) and not _leg_is_stop(leg_type)


_WEIGHT_COL = "gross_combination_vehicle_weight"
# EV telematics speed column (km/h). The per-leg mass mean/CV preferably
# uses moving samples only, excluding the unreliable GCVW broadcast while
# stationary; falls back to the old all-(> 0)-samples behaviour when the column
# is missing.
_SPEED_COL = "wheel_based_speed"
_RECUP_COL = "electric_energy_recuperation_watthours"
# Cumulative motor propulsion energy counter (Wh, since vehicle
# inception). The trip start/end times obtain Δ by linear interpolation between
# the nearest RFMS snapshots, then divide by 1000 to convert to kWh and write it
# into the new `Propulsion Energy (kWh)` column. Diesel vehicles do not have this
# column.
_PROPULSION_COL = "electric_energy_propulsion"


def _row_col_index(col_name: str, headers: tuple = HEADERS) -> int:
    """Return the 0-based index of a column inside the row tuple.

    ``headers[0]`` is 'Leg Number' which is written separately by the Excel
    writer and does **not** appear in the row tuple — so the row tuple index is
    ``headers.index(col_name) - 1``. Pass ``DIESEL_HEADERS`` to get the layout
    used by diesel rows.
    """
    return headers.index(col_name) - 1


def _is_nan(v) -> bool:
    """Safe NaN check."""
    if v is None:
        return False
    try:
        return bool(np.isnan(v))
    except (TypeError, ValueError):
        return False
