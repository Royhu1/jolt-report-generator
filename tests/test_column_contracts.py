"""Column-index contract for the append-only HEADERS / DIESEL_HEADERS layout.

Every patcher writes into fixed 1-based Excel columns via hardcoded ``_COL_*``
literals. Those literals MUST equal ``HEADERS.index(<name>) + 1``; if HEADERS is
ever reordered the patchers would silently corrupt the wrong column. The patchers
already assert this at import time — this test pins the same contract as an
explicit, independent artifact (reading each module's actual literal), and locks
in the documented EV-vs-diesel divergence.
"""

import importlib

import pytest

from jolt_toolkit.report_generator.report_builder import DIESEL_HEADERS, HEADERS

charger_patcher = importlib.import_module(
    "jolt_toolkit.report_generator.charger_patcher"
)
logger_patcher = importlib.import_module("jolt_toolkit.report_generator.logger_patcher")
weather_patcher = importlib.import_module(
    "jolt_toolkit.report_generator.weather_patcher"
)

# (module, _COL_* attribute, HEADERS column name) — the contract each patcher relies on.
COLUMN_CONTRACTS = [
    # charger_patcher
    (charger_patcher, "_COL_LEG_TYPE", "Leg Type"),
    (charger_patcher, "_COL_CHARGER", "Charger Link"),
    (charger_patcher, "_COL_START_TIME", "Start Time (UTC)"),
    (charger_patcher, "_COL_END_TIME", "End Time (UTC)"),
    (charger_patcher, "_COL_CHARGER_ENERGY", "Energy Output from Charger (kWh)"),
    # logger_patcher
    (logger_patcher, "_COL_LEG_TYPE", "Leg Type"),
    (logger_patcher, "_COL_LOGGER_LINK", "SRF Logger Link"),
    (logger_patcher, "_COL_START_TIME", "Start Time (UTC)"),
    (logger_patcher, "_COL_END_TIME", "End Time (UTC)"),
    (logger_patcher, "_COL_MASS", "Vehicle Mass (kg)"),
    (logger_patcher, "_COL_MASS_CV", "Vehicle Mass CV (reliability)"),
    (logger_patcher, "_COL_TEMP", "Average Temperature (C)"),
    (logger_patcher, "_COL_PRESSURE", "Average Pressure (hPa)"),
    (logger_patcher, "_COL_HUMIDITY", "Average Humidity (%)"),
    (logger_patcher, "_COL_WIND_SPEED", "Average Wind Speed (m/s)"),
    (logger_patcher, "_COL_WIND_DIR", "Average Wind Direction"),
    (logger_patcher, "_COL_DISTANCE", "Distance (km)"),
    (logger_patcher, "_COL_ELEV_DIFF", "Elevation Difference (m)"),
    (logger_patcher, "_COL_ENERGY", "Energy Change (kWh)"),
    (
        logger_patcher,
        "_COL_EPERF_KIN",
        "Energy Performance Kinetics Corrected (kWh/km)",
    ),
    (logger_patcher, "_COL_HIST_ACC", "Histogram of Accelerator Pedal Position"),
    (logger_patcher, "_COL_HIST_DEC", "Histogram of Decelerator Pedal Position"),
    # weather_patcher
    (weather_patcher, "_COL_LEG_TYPE", "Leg Type"),
    (weather_patcher, "_COL_START_TIME", "Start Time (UTC)"),
    (weather_patcher, "_COL_ORIGIN", "Origin (Lat, Lon)"),
    (weather_patcher, "_COL_END_TIME", "End Time (UTC)"),
    (weather_patcher, "_COL_DEST", "Destination (Lat, Lon)"),
    (weather_patcher, "_COL_TEMP", "Average Temperature (C)"),
    (weather_patcher, "_COL_PRESSURE", "Average Pressure (hPa)"),
    (weather_patcher, "_COL_HUMIDITY", "Average Humidity (%)"),
    (weather_patcher, "_COL_WIND_SPEED", "Average Wind Speed (m/s)"),
    (weather_patcher, "_COL_WIND_DIR", "Average Wind Direction"),
    (weather_patcher, "_COL_WEATHER_TYPE", "Weather Type"),
]


@pytest.mark.parametrize(
    "mod,attr,header_name",
    COLUMN_CONTRACTS,
    ids=[f"{m.__name__.split('.')[-1]}.{a}" for m, a, _ in COLUMN_CONTRACTS],
)
def test_col_literal_matches_header_index(mod, attr, header_name):
    literal = getattr(mod, attr)
    assert header_name in HEADERS, f"{header_name!r} not in HEADERS"
    assert literal == HEADERS.index(header_name) + 1, (
        f"{mod.__name__}.{attr}={literal} but HEADERS index of "
        f"{header_name!r} is {HEADERS.index(header_name) + 1}"
    )


def test_headers_first_column_is_leg_number():
    # The -1 offset contract in capacity._row_idx / _COL_* depends on this.
    assert HEADERS[0] == "Leg Number"
    assert DIESEL_HEADERS[0] == "Leg Number"


def test_header_lengths():
    assert len(HEADERS) == 50
    assert len(DIESEL_HEADERS) == 26


def test_ev_diesel_temperature_column_diverges():
    # Same field, different 1-based column: EV col 38 vs diesel col 19.
    name = "Average Temperature (C)"
    assert HEADERS.index(name) + 1 == 38
    assert DIESEL_HEADERS.index(name) + 1 == 19


def test_operator_is_last_column_in_both():
    assert HEADERS[-1] == "Operator"
    assert DIESEL_HEADERS[-1] == "Operator"


def test_ev_trailing_columns():
    assert tuple(HEADERS[-3:]) == (
        "Propulsion Energy (kWh)",
        "EP_exclude_aux",
        "Operator",
    )


def test_diesel_trailing_columns():
    assert tuple(DIESEL_HEADERS[-2:]) == ("Energy Source", "Operator")


@pytest.mark.parametrize(
    "electric_col",
    ["Battery Capacity (kWh)", "Energy Performance (kWh/km)", "Charger Link"],
)
def test_electric_columns_are_ev_only(electric_col):
    # Diesel dropped every battery/charging column (v2.2.2); they must not leak in.
    assert electric_col in HEADERS
    assert electric_col not in DIESEL_HEADERS


def test_diesel_only_columns_are_the_fuel_pair():
    # Diesel is a distinct header set, not a truncation of EV: the only columns it
    # has that EV lacks are the two fuel metrics.
    assert set(DIESEL_HEADERS) - set(HEADERS) == {
        "Fuel Used (L)",
        "Fuel Consumption (L/100km)",
    }
    assert "Fuel Used (L)" not in HEADERS
    # Energy Source exists in BOTH layouts (added v2.2.3 / v2.2.5).
    assert "Energy Source" in HEADERS
    assert "Energy Source" in DIESEL_HEADERS
