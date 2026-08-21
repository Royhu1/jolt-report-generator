"""Diesel logger path on the DSL01 fixture, end to end.

``_logger_df_from_csv`` -> ``_finalise_logger_df`` -> ``_segments_from_df``
-> ``_trip_metrics`` -> ``_diesel_seg_to_row``.

The fixture is one real ~9-minute SRFLOGGER leg with genuine J1939 channel names,
so the channel plumbing, the LFC/VDHR differencing and the Channel-7 weather all
run on real data. The filter chain and the mass fallback chain are additionally
exercised with synthetic frames, because a single clean leg cannot demonstrate a
rejection.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from report_generator import diesel_pipeline as dp
from report_generator.columns import DIESEL_HEADERS

TRIP_METRIC_KEYS = {
    "start_time",
    "end_time",
    "fuel_l",
    "energy_kwh",
    "distance_km",
    "avg_speed",
    "veh_mass",
    "veh_mass_cv",
    "mass_source",
    "elev_diff",
    "temp_avg",
    "pressure_avg",
    "humidity_avg",
    "wind_speed_avg",
    "wind_dir_text",
    "lat_s",
    "lon_s",
    "lat_e",
    "lon_e",
    "fuel_consumption_l_per_100km",
}


# ── _logger_df_from_csv / _finalise_logger_df ────────────────────────────────


def test_logger_csv_rebuilds_into_a_utc_indexed_frame(diesel_fixture_frame):
    frame, _cfg = diesel_fixture_frame
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert not frame.index.has_duplicates
    assert len(frame) == 544


def test_logger_frame_gains_the_time_column_for_speed_segmentation(
    diesel_fixture_frame,
):
    from report_generator.segment_algorithms import TIME_COL

    frame, _cfg = diesel_fixture_frame
    assert TIME_COL in frame.columns
    assert (frame[TIME_COL] == frame.index).all()


def test_logger_frame_carries_the_real_j1939_channel_names(diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    for key in (
        "speed_col",
        "fuel_energy_col",
        "distance_col",
        "mass_col",
        "altitude_col",
        "ambient_temp_col",
    ):
        assert cfg[key] in frame.columns, f"{cfg[key]} missing from the logger frame"


def test_logger_csv_missing_file_returns_none(tmp_path, frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    assert dp._logger_df_from_csv(tmp_path / "nope.csv", cfg) is None


def _synthetic_logger_frame(
    cfg, *, n=300, speed=50.0, include=("speed",), start="2025-10-07T06:00:00Z"
):
    """A 1 Hz synthetic logger frame carrying only the requested channels."""
    idx = pd.date_range(start, periods=n, freq="1s", tz="UTC")
    data = {}
    if "speed" in include:
        data[cfg["speed_col"]] = np.full(n, speed)
    if "gps_speed" in include:
        data[cfg["speed_col_fallback"]] = np.full(n, speed / 3.6)
    if "fuel" in include:
        data[cfg["fuel_energy_col"]] = np.linspace(1000.0, 1002.0, n)
    if "distance" in include:
        data[cfg["distance_col"]] = np.linspace(50000.0, 50004.0, n)
    if "mass" in include:
        data[cfg["mass_col"]] = np.full(n, 38000.0)
    if "temp" in include:
        data[cfg["ambient_temp_col"]] = np.full(n, 12.0)
    return pd.DataFrame(data, index=idx)


def test_finalise_uses_the_gps_speed_fallback(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    raw = _synthetic_logger_frame(cfg, include=("gps_speed",))
    out = dp._finalise_logger_df(raw, cfg, source="synthetic")
    assert out is not None
    # GPS speed is m/s; the fallback converts to km/h.
    assert out[cfg["speed_col"]].iloc[0] == pytest.approx(50.0)


def test_finalise_prefers_a_populated_primary_speed_channel(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    raw = _synthetic_logger_frame(cfg, include=("speed", "gps_speed"), speed=44.0)
    out = dp._finalise_logger_df(raw, cfg, source="synthetic")
    assert out[cfg["speed_col"]].iloc[0] == 44.0


def test_finalise_returns_none_without_any_speed_source(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    raw = _synthetic_logger_frame(cfg, include=("fuel", "distance"))
    assert dp._finalise_logger_df(raw, cfg, source="synthetic") is None


def test_finalise_deduplicates_and_sorts_the_index(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    idx = pd.to_datetime(
        [
            "2025-10-07T06:00:02Z",
            "2025-10-07T06:00:00Z",
            "2025-10-07T06:00:00Z",
            "2025-10-07T06:00:01Z",
        ]
    )
    raw = pd.DataFrame({cfg["speed_col"]: [3.0, 1.0, 99.0, 2.0]}, index=idx)
    out = dp._finalise_logger_df(raw, cfg, source="synthetic")
    assert list(out[cfg["speed_col"]]) == [1.0, 2.0, 3.0]  # first duplicate kept


def test_finalise_localises_a_naive_index(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    idx = pd.date_range("2025-10-07T06:00:00", periods=3, freq="1s")
    raw = pd.DataFrame({cfg["speed_col"]: [1.0, 2.0, 3.0]}, index=idx)
    out = dp._finalise_logger_df(raw, cfg, source="synthetic")
    assert str(out.index.tz) == "UTC"


# ── _segments_from_df on the real fixture ────────────────────────────────────


@pytest.fixture
def diesel_segments(diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    trips, seg_metrics = dp._segments_from_df(frame, cfg, source="fixture")
    return trips, seg_metrics, cfg


def test_fixture_yields_one_trip(diesel_segments):
    trips, seg_metrics, _cfg = diesel_segments
    assert len(trips) == 1
    assert len(seg_metrics) == 1


def test_trip_metrics_dict_has_exactly_the_twenty_documented_keys(diesel_segments):
    _trips, seg_metrics, _cfg = diesel_segments
    seg = seg_metrics[0]
    assert set(seg) == TRIP_METRIC_KEYS
    assert len(TRIP_METRIC_KEYS) == 20


def test_trip_metrics_values_are_physically_plausible(diesel_segments):
    _trips, seg_metrics, cfg = diesel_segments
    seg = seg_metrics[0]
    assert seg["fuel_l"] > 0
    assert seg["distance_km"] > 0
    assert seg["energy_kwh"] == pytest.approx(
        seg["fuel_l"] * cfg["diesel_lhv_kwh_per_l"]
    )
    assert seg["fuel_consumption_l_per_100km"] == pytest.approx(
        seg["fuel_l"] / seg["distance_km"] * 100.0, abs=1e-3
    )
    assert 0 < seg["avg_speed"] < 120
    assert 5000 < seg["veh_mass"] < 60000
    assert seg["mass_source"] == "cvw_trip"
    assert -40 < seg["temp_avg"] < 50
    assert 800 < seg["pressure_avg"] < 1100
    assert 0 <= seg["humidity_avg"] <= 100
    assert seg["wind_dir_text"] in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def test_trip_metrics_matches_the_frozen_golden(
    diesel_segments, load_golden, serialise, raw_fixture_map
):
    _trips, seg_metrics, _cfg = diesel_segments
    golden = load_golden("diesel_segments_DSL01.json")
    assert golden["source"] == raw_fixture_map["DSL01"]
    assert serialise(seg_metrics) == golden["trips"]


def test_csv_sourced_frame_carries_origin_destination_coordinates(diesel_segments):
    """The CSV path must yield the same coordinates as the live path.

    Both ``_build_logger_df`` and ``_logger_df_from_csv`` rename the Channel-2
    latitude/longitude to ``_lat`` / ``_lon`` via the shared ``_GPS_LAT`` /
    ``_GPS_LON`` constants. This used to hold for the live path only, so a report
    regenerated from cached CSVs silently lost its origin/destination coordinates;
    the assertion is inverted here to keep that regression from returning.
    """
    _trips, seg_metrics, _cfg = diesel_segments
    seg = seg_metrics[0]
    assert seg["lat_s"] is not None and seg["lon_s"] is not None
    assert seg["lat_e"] is not None and seg["lon_e"] is not None
    # the fixture's GPS was rigid-transformed to a synthetic origin near (0.5, 0.5)
    assert -1.0 < seg["lat_s"] < 2.0 and -1.0 < seg["lon_s"] < 2.0


def test_renaming_the_gps_columns_recovers_the_coordinates(diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    renamed = frame.rename(columns={"2 latitude": "_lat", "2 longitude": "_lon"})
    _trips, seg_metrics = dp._segments_from_df(renamed, cfg, source="renamed")
    seg = seg_metrics[0]
    assert seg["lat_s"] is not None and seg["lon_s"] is not None
    assert seg["lat_e"] is not None and seg["lon_e"] is not None


# ── The filter chain ─────────────────────────────────────────────────────────


def test_trips_below_min_trip_distance_km_are_dropped(diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    strict = dict(cfg, min_trip_distance_km=5.0)  # the real trip is ~1 km
    trips, seg_metrics = dp._segments_from_df(frame, strict, source="fixture")
    assert trips  # the speed detector still finds the window ...
    assert seg_metrics == []  # ... but the distance gate drops it


def test_a_zero_distance_trip_is_dropped(monkeypatch, diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    real = dp._trip_metrics

    def _zero_distance(*args, **kwargs):
        seg = dict(real(*args, **kwargs))
        seg["distance_km"] = 0.0
        return seg

    monkeypatch.setattr(dp, "_trip_metrics", _zero_distance)
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source="fixture")
    assert seg_metrics == []


def test_a_nan_distance_trip_is_dropped(monkeypatch, diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    real = dp._trip_metrics

    def _nan_distance(*args, **kwargs):
        seg = dict(real(*args, **kwargs))
        seg["distance_km"] = float("nan")
        return seg

    monkeypatch.setattr(dp, "_trip_metrics", _nan_distance)
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source="fixture")
    assert seg_metrics == []


def test_a_pathological_all_nan_trip_is_dropped(frozen_configs):
    # Speed only: distance comes from speed integration, but fuel / mass /
    # temperature are all absent, so the trip is unusable downstream.
    # ``weight_class_t`` is removed too — otherwise the mass fallback chain would
    # supply a nominal mass and the "all NaN" gate would never fire.
    cfg = dict(frozen_configs["vehicles"]["DSL01"])
    cfg.pop("weight_class_t")
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, n=600, speed=50.0, include=("speed",)),
        cfg,
        source="synthetic",
    )
    trips, seg_metrics = dp._segments_from_df(frame, cfg, source="synthetic")
    assert trips  # the speed detector finds the window ...
    assert seg_metrics == []  # ... but nothing usable comes out of it


def test_a_trip_with_only_temperature_survives_the_pathological_gate(frozen_configs):
    cfg = dict(frozen_configs["vehicles"]["DSL01"])
    cfg.pop("weight_class_t")
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, n=600, speed=50.0, include=("speed", "temp")),
        cfg,
        source="synthetic",
    )
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source="synthetic")
    assert len(seg_metrics) == 1
    assert seg_metrics[0]["temp_avg"] == 12.0


def test_the_weight_class_fallback_alone_keeps_a_trip_alive(frozen_configs):
    # Control for the two tests above: WITH weight_class_t the mass is never NaN,
    # so the same speed-only trip is kept.
    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, n=600, speed=50.0, include=("speed",)),
        cfg,
        source="synthetic",
    )
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source="synthetic")
    assert len(seg_metrics) == 1
    assert seg_metrics[0]["mass_source"] == "weight_class"


def test_no_trips_returns_two_empty_lists(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, n=300, speed=0.0, include=("speed",)),
        cfg,
        source="synthetic",
    )
    assert dp._segments_from_df(frame, cfg, source="synthetic") == ([], [])


# ── Mass source precedence ───────────────────────────────────────────────────


def _window(frame):
    return frame.index[0], frame.index[-1]


def test_mass_source_prefers_the_trip_cvw_median(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, include=("speed", "mass")), cfg, source="s"
    )
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg, mass_fallback_kg=11111.0)
    assert seg["veh_mass"] == 38000.0
    assert seg["mass_source"] == "cvw_trip"


def test_mass_source_falls_back_to_the_carry_over(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, include=("speed",)), cfg, source="s"
    )
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg, mass_fallback_kg=33000.0)
    assert seg["veh_mass"] == 33000.0
    assert seg["mass_source"] == "cvw_carryover"


def test_mass_source_falls_back_to_the_weight_class(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, include=("speed",)), cfg, source="s"
    )
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg, mass_fallback_kg=None)
    assert seg["veh_mass"] == cfg["weight_class_t"] * 1000.0
    assert seg["mass_source"] == "weight_class"


def test_mass_stays_nan_without_any_fallback(frozen_configs):
    cfg = dict(frozen_configs["vehicles"]["DSL01"])
    cfg.pop("weight_class_t")
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, include=("speed",)), cfg, source="s"
    )
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg, mass_fallback_kg=None)
    assert math.isnan(seg["veh_mass"])


def test_zero_cvw_broadcasts_are_excluded(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    raw = _synthetic_logger_frame(cfg, n=300, include=("speed", "mass"))
    raw.iloc[:100, raw.columns.get_loc(cfg["mass_col"])] = 0.0
    frame = dp._finalise_logger_df(raw, cfg, source="s")
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg)
    assert seg["veh_mass"] == 38000.0


def test_carry_over_only_promotes_a_real_cvw_reading(frozen_configs):
    """A weight_class fallback must NOT poison the next trip's carry-over slot."""
    cfg = frozen_configs["vehicles"]["DSL01"]
    # Two trips separated by a >5 min stop; only the SECOND has CVW data.
    first = _synthetic_logger_frame(cfg, n=300, include=("speed", "temp"))
    gap = _synthetic_logger_frame(
        cfg,
        n=600,
        speed=0.0,
        include=("speed",),
        start="2025-10-07T06:05:00Z",
    )
    second = _synthetic_logger_frame(
        cfg,
        n=300,
        include=("speed", "mass", "temp"),
        start="2025-10-07T06:20:00Z",
    )
    frame = dp._finalise_logger_df(pd.concat([first, gap, second]), cfg, source="s")
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source="s")
    assert len(seg_metrics) == 2
    assert seg_metrics[0]["mass_source"] == "weight_class"
    assert seg_metrics[1]["mass_source"] == "cvw_trip"


def test_empty_window_yields_an_empty_metrics_dict(diesel_fixture_frame):
    frame, cfg = diesel_fixture_frame
    out = dp._trip_metrics(
        frame,
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-01T01:00:00Z"),
        cfg,
    )
    assert out == {}


def test_lfc_counter_that_does_not_tick_leaves_fuel_unknown(frozen_configs):
    """A short trip may not move the 0.5 L LFC counter: that is UNKNOWN, not zero."""
    cfg = frozen_configs["vehicles"]["DSL01"]
    raw = _synthetic_logger_frame(cfg, n=300, include=("speed", "distance"))
    raw[cfg["fuel_energy_col"]] = 1000.0  # constant counter
    frame = dp._finalise_logger_df(raw, cfg, source="s")
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg)
    assert math.isnan(seg["fuel_l"])
    assert math.isnan(seg["energy_kwh"])
    assert math.isnan(seg["fuel_consumption_l_per_100km"])


def test_distance_falls_back_to_speed_integration(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._finalise_logger_df(
        _synthetic_logger_frame(cfg, n=361, speed=36.0, include=("speed",)),
        cfg,
        source="s",
    )
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg)
    # 36 km/h = 10 m/s over 360 s = 3.6 km (the last sample contributes no dt).
    assert seg["distance_km"] == pytest.approx(3.6, abs=0.05)


def test_channel_seven_temperature_beats_the_engine_bay_sensor(frozen_configs):
    cfg = frozen_configs["vehicles"]["DSL01"]
    raw = _synthetic_logger_frame(cfg, n=300, include=("speed", "temp"))
    raw["7 temperature"] = 8.0  # the dedicated weather station
    frame = dp._finalise_logger_df(raw, cfg, source="s")
    t_s, t_e = _window(frame)
    seg = dp._trip_metrics(frame, t_s, t_e, cfg)
    assert seg["temp_avg"] == 8.0


# ── _diesel_seg_to_row ───────────────────────────────────────────────────────


def test_diesel_row_length_contract(diesel_segments):
    _trips, seg_metrics, _cfg = diesel_segments
    row, cumulative = dp._diesel_seg_to_row(
        seg_metrics[0], "https://data.example.org/api/legs/l1", 0.0, operator="WJF"
    )
    assert len(row) == len(DIESEL_HEADERS) - 1
    assert cumulative == pytest.approx(seg_metrics[0]["distance_km"])


def test_diesel_row_field_placement(diesel_segments):
    from report_generator.columns import _row_col_index

    _trips, seg_metrics, _cfg = diesel_segments
    seg = seg_metrics[0]
    row, _ = dp._diesel_seg_to_row(
        seg, "https://data.example.org/api/legs/l1", 100.0, operator="WJF"
    )

    def at(header):
        return row[_row_col_index(header, DIESEL_HEADERS)]

    assert at("Leg Type") == "In Transit"
    assert at("Energy Source") == "lfc_fuel"
    assert at("Operator") == "WJF"
    assert at("Distance (km)") == seg["distance_km"]
    assert at("Fuel Used (L)") == seg["fuel_l"]
    assert at("Cumulative Distance (km)") == pytest.approx(100.0 + seg["distance_km"])
    assert at("SRF Logger Link").startswith("https://data.example.org/explore/")
    # Duration is stored as a fraction of a day.
    span_s = (
        pd.Timestamp(seg["end_time"]) - pd.Timestamp(seg["start_time"])
    ).total_seconds()
    assert at("Duration (HH:MM:SS)") == pytest.approx(span_s / 86400.0)


def test_diesel_row_cumulative_distance_advances_across_trips(diesel_segments):
    from report_generator.columns import _row_col_index

    _trips, seg_metrics, _cfg = diesel_segments
    seg = seg_metrics[0]
    cumulative = 0.0
    totals = []
    for _ in range(3):
        row, cumulative = dp._diesel_seg_to_row(seg, "https://x/legs/1", cumulative)
        totals.append(row[_row_col_index("Cumulative Distance (km)", DIESEL_HEADERS)])
    assert totals == sorted(totals)
    assert totals[-1] == pytest.approx(3 * seg["distance_km"])
