"""The report's trip-average speed definition.

``Average Speed (km/h)`` is odometer distance over the **full elapsed** segment
duration, stopped time included, for both the EV and the diesel report. The EV
path once used a moving-duration denominator accumulated from sparse telematics
samples; a leg whose sampling gaps swallowed the stopped time then reported an
implausibly high speed. These tests pin the elapsed definition and prove a
leftover ``motion_duration_s`` on a segment dict can no longer override it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from jolt_toolkit.report_generator import row_builder
from jolt_toolkit.report_generator.columns import HEADERS, _row_col_index
from jolt_toolkit.report_generator.row_builder import _average_speed_kmh


def test_average_speed_uses_the_full_elapsed_segment_duration():
    # 23.48 km over 18 min 06 s = 0.30167 h → 77.83 km/h
    assert _average_speed_kmh(23.48, pd.Timedelta(minutes=18, seconds=6)) == 77.83


def test_average_speed_includes_a_stop_inside_the_window():
    """Doubling the window with the vehicle stationary halves the reported speed."""
    moving = _average_speed_kmh(60.0, pd.Timedelta(hours=1))
    with_a_stop = _average_speed_kmh(60.0, pd.Timedelta(hours=2))
    assert (moving, with_a_stop) == (60.0, 30.0)


def test_average_speed_rejects_invalid_inputs():
    """Neither a missing distance nor a non-positive duration is reportable."""
    assert math.isnan(_average_speed_kmh(0.0, pd.Timedelta(minutes=10)))
    assert math.isnan(_average_speed_kmh(np.nan, pd.Timedelta(minutes=10)))
    assert math.isnan(_average_speed_kmh(np.inf, pd.Timedelta(minutes=10)))
    assert math.isnan(_average_speed_kmh(10.0, pd.Timedelta(0)))
    assert math.isnan(_average_speed_kmh(10.0, pd.Timedelta(minutes=-1)))


def test_a_segment_row_ignores_a_legacy_motion_duration(monkeypatch):
    """A stale ``motion_duration_s`` key cannot resurrect the old denominator.

    The metric helpers are stubbed out because this test is about the speed
    denominator only: the segment carries no telematics frame, so every other
    column would be NaN anyway.
    """
    monkeypatch.setattr(
        row_builder, "_get_leg_type", lambda *args, **kwargs: "In Transit"
    )
    monkeypatch.setattr(row_builder, "_get_postcode", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        row_builder, "_get_vehicle_mass", lambda *args, **kwargs: (np.nan, np.nan)
    )
    monkeypatch.setattr(row_builder, "_get_recuperation", lambda *args: np.nan)
    monkeypatch.setattr(row_builder, "_get_elevation_diff", lambda *args: np.nan)
    monkeypatch.setattr(row_builder, "_get_propulsion_energy", lambda *args: np.nan)

    segment = {
        # 18 min 06 s elapsed; the legacy moving duration was 665 s (11 min 05 s),
        # which would have reported 127.11 km/h for the same 23.48 km.
        "start_time": pd.Timestamp("2025-11-18 07:19:46", tz="UTC"),
        "end_time": pd.Timestamp("2025-11-18 07:37:52", tz="UTC"),
        "odo_start_km": 19_719.780,
        "odo_end_km": 19_743.260,
        "motion_duration_s": 665.0,
        "start_soc": 90.0,
        "end_soc": 85.0,
        "delta_soc_pct": -5.0,
        "delta_energy_kwh": -25.0,
        "effective_capacity_kwh": 500.0,
        "energy_source": "total_energy",
    }

    row, _ = row_builder._seg_to_row(
        segment,
        "discharge",
        "https://data.example.org/api/legs/leg-1",
        [],
        [],
        pd.DataFrame(),
        0.0,
    )

    assert row[_row_col_index("Average Speed (km/h)")] == 77.83
    assert len(row) == len(HEADERS) - 1
