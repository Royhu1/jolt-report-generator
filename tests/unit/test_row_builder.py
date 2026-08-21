"""Row-builder helpers: URLs, overlap matching, per-metric telematics maths,
leg-type classification and Stop-row synthesis.

Everything here is pure (no SRF, no geocoding): ``_get_postcode`` returns ``None``
without an ``srf_data`` object, so no test in this module can reach the network.
"""

from __future__ import annotations

import math
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pytest
from geopy import Point as GeoPoint

from jolt_toolkit.report_generator import row_builder as rb
from jolt_toolkit.report_generator.columns import (
    DIESEL_HEADERS,
    HEADERS,
    _row_col_index,
)

TIME_COL = "eventDatetime"


def _ts(text):
    return pd.Timestamp(text)


# ── _ts_iso + URL builders ───────────────────────────────────────────────────


def test_ts_iso_localises_a_naive_timestamp_to_utc():
    assert rb._ts_iso(_ts("2025-06-27 10:00:00")) == "2025-06-27 10:00:00+00:00"


def test_ts_iso_preserves_an_existing_offset():
    assert rb._ts_iso(_ts("2025-06-27T10:00:00+02:00")) == "2025-06-27 10:00:00+02:00"


def test_build_telematics_url_shape():
    url = rb._build_telematics_url(
        "https://data.example.org/api/legs/abc123",
        _ts("2025-06-27 08:00:00"),
        _ts("2025-06-27 09:00:00"),
    )
    parsed = urlparse(url)
    assert parsed.netloc == "data.example.org"
    assert parsed.path == "/explore/graphics/plots"
    q = parse_qs(parsed.query)
    assert q["start"] == ["2025-06-27 08:00:00+00:00"]
    assert q["end"] == ["2025-06-27 09:00:00+00:00"]
    # The (96, "HSS2") tuple is expanded by doseq, not stringified as a tuple.
    assert q["type"] == ["96", "HSS2"]
    assert q["resourceUri"] == ["https://data.example.org/api/legs/abc123"]


def test_build_charger_url_shape():
    url = rb._build_charger_url(
        "https://data.example.org/api/transactions/t1",
        _ts("2025-06-27 08:00:00"),
        _ts("2025-06-27 09:00:00"),
    )
    assert urlparse(url).path == "/explore/graphics/usage"
    assert "type=" not in url


def test_build_logger_url_shape():
    url = rb._build_logger_url(
        "https://data.example.org/api/legs/lg1",
        _ts("2025-06-27 08:00:00"),
        _ts("2025-06-27 09:00:00"),
    )
    assert urlparse(url).path == "/explore/graphics/plots"


@pytest.mark.parametrize(
    "builder",
    [rb._build_telematics_url, rb._build_charger_url, rb._build_logger_url],
)
@pytest.mark.parametrize("uri", [None, ""])
def test_url_builders_return_none_without_a_uri(builder, uri):
    assert builder(uri, _ts("2025-06-27 08:00:00"), _ts("2025-06-27 09:00:00")) is None


# ── _find_overlap ────────────────────────────────────────────────────────────

WINDOWS = [
    ("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z", "uri-A"),
    ("2025-06-27T12:00:00Z", "2025-06-27T13:00:00Z", "uri-B"),
]


def test_find_overlap_matches_a_containing_window():
    assert (
        rb._find_overlap(WINDOWS, _ts("2025-06-27 08:10"), _ts("2025-06-27 08:20"))
        == "uri-A"
    )


def test_find_overlap_returns_the_first_match():
    overlapping = [("2025-06-27T07:00:00Z", "2025-06-27T23:00:00Z", "uri-Z"), *WINDOWS]
    assert (
        rb._find_overlap(overlapping, _ts("2025-06-27 12:30"), _ts("2025-06-27 12:40"))
        == "uri-Z"
    )


def test_find_overlap_no_match_returns_none():
    assert (
        rb._find_overlap(WINDOWS, _ts("2025-06-27 10:00"), _ts("2025-06-27 11:00"))
        is None
    )


@pytest.mark.parametrize(
    "start, end, tol_min, expected",
    [
        # Segment ends 4 minutes BEFORE the window opens: inside a 5 min tolerance.
        ("2025-06-27 07:50", "2025-06-27 07:56", 5, "uri-A"),
        # ... but not inside a 3 min tolerance.
        ("2025-06-27 07:50", "2025-06-27 07:56", 3, None),
        # Segment starts 4 minutes AFTER the window closes.
        ("2025-06-27 09:04", "2025-06-27 09:30", 5, "uri-A"),
        ("2025-06-27 09:04", "2025-06-27 09:30", 3, None),
    ],
)
def test_find_overlap_tolerance_extends_both_ends(start, end, tol_min, expected):
    assert rb._find_overlap(WINDOWS, _ts(start), _ts(end), tol_min=tol_min) == expected


def test_find_overlap_skips_malformed_entries():
    windows = [("not-a-date", "also-not", "uri-bad"), *WINDOWS]
    assert (
        rb._find_overlap(windows, _ts("2025-06-27 08:10"), _ts("2025-06-27 08:20"))
        == "uri-A"
    )


def test_find_overlap_empty_window_list():
    assert (
        rb._find_overlap([], _ts("2025-06-27 08:10"), _ts("2025-06-27 08:20")) is None
    )


# ── _point_str ───────────────────────────────────────────────────────────────


def test_point_str_uses_six_decimal_places():
    assert rb._point_str(0.5, 0.25) == "Point(0.500000 0.250000)"
    assert rb._point_str("0.1234567", "-1.9999999") == "Point(0.123457 -2.000000)"


@pytest.mark.parametrize(
    "lat, lon", [(None, 1.0), (1.0, None), (None, None), ("abc", 1.0)]
)
def test_point_str_returns_none_for_unusable_coordinates(lat, lon):
    assert rb._point_str(lat, lon) is None


# ── _get_leg_type ────────────────────────────────────────────────────────────

HOME = GeoPoint(0.5, 0.5)
AWAY_LAT, AWAY_LON = 1.5, 1.5


@pytest.mark.parametrize(
    "ac, dc, expected",
    [
        (50.0, 0.0, "AC Home"),
        (0.0, 50.0, "DC Home"),
        (50.0, 50.0, "AC/DC Home"),
        (0.0, 0.0, "Charge Home"),
        (float("nan"), float("nan"), "Charge Home"),
        (0.5, 0.5, "Charge Home"),  # both below the 1 kWh threshold
    ],
)
def test_get_leg_type_charge_at_home(ac, dc, expected):
    seg = {"latitude": 0.5, "longitude": 0.5}
    assert rb._get_leg_type("charge", seg, ac, dc, HOME) == expected


@pytest.mark.parametrize(
    "ac, dc, expected",
    [(50.0, 0.0, "AC Away"), (0.0, 50.0, "DC Away"), (0.0, 0.0, "Charge Away")],
)
def test_get_leg_type_charge_away(ac, dc, expected):
    seg = {"latitude": AWAY_LAT, "longitude": AWAY_LON}
    assert rb._get_leg_type("charge", seg, ac, dc, expected and HOME) == expected


def test_get_leg_type_charge_without_a_home_point_is_away():
    seg = {"latitude": 0.5, "longitude": 0.5}
    assert rb._get_leg_type("charge", seg, 50.0, 0.0, None) == "AC Away"


def _trip_seg(lat_s, lon_s, lat_e, lon_e, odo_s=None, odo_e=None):
    return {
        "lat_start": lat_s,
        "lon_start": lon_s,
        "lat_end": lat_e,
        "lon_end": lon_e,
        "odo_start_km": odo_s,
        "odo_end_km": odo_e,
    }


def test_get_leg_type_in_house_for_a_short_depot_shuffle():
    seg = _trip_seg(0.5, 0.5, 0.5, 0.5, odo_s=100.0, odo_e=102.0)  # 2 km < 5 km
    assert rb._get_leg_type("discharge", seg, np.nan, np.nan, HOME) == "In House"


def test_get_leg_type_round_trip_when_the_depot_loop_is_long():
    seg = _trip_seg(0.5, 0.5, 0.5, 0.5, odo_s=100.0, odo_e=180.0)
    assert rb._get_leg_type("discharge", seg, np.nan, np.nan, HOME) == "Round Trip"


def test_get_leg_type_in_house_when_the_odometer_is_missing():
    seg = _trip_seg(0.5, 0.5, 0.5, 0.5)
    assert rb._get_leg_type("discharge", seg, np.nan, np.nan, HOME) == "In House"


def test_get_leg_type_outbound_return_and_in_transit():
    outbound = _trip_seg(0.5, 0.5, AWAY_LAT, AWAY_LON)
    ret = _trip_seg(AWAY_LAT, AWAY_LON, 0.5, 0.5)
    transit = _trip_seg(AWAY_LAT, AWAY_LON, 2.5, 2.5)
    assert rb._get_leg_type("discharge", outbound, np.nan, np.nan, HOME) == "Outbound"
    assert rb._get_leg_type("discharge", ret, np.nan, np.nan, HOME) == "Return"
    assert rb._get_leg_type("discharge", transit, np.nan, np.nan, HOME) == "In Transit"


def test_get_leg_type_without_a_home_point_is_always_in_transit():
    seg = _trip_seg(0.5, 0.5, 0.5, 0.5)
    assert rb._get_leg_type("discharge", seg, np.nan, np.nan, None) == "In Transit"


def test_is_home_uses_the_documented_radius():
    # HOME_DETECTION_KM is 0.5 km; ~0.001 deg latitude is ~111 m.
    assert rb._is_home(0.5005, 0.5, HOME) is True
    assert rb._is_home(0.55, 0.5, HOME) is False
    assert rb._is_home(None, 0.5, HOME) is False
    assert rb._is_home(0.5, 0.5, None) is False


# ── Energy-performance corrections ───────────────────────────────────────────


def test_ep_exclude_aux_closed_form():
    # (propulsion - recuperation) / distance
    assert rb._ep_exclude_aux(100.0, 20.0, 50.0) == pytest.approx(1.6)


@pytest.mark.parametrize(
    "prop, recup, dist",
    [
        (float("nan"), 20.0, 50.0),
        (100.0, float("nan"), 50.0),
        (100.0, 20.0, float("nan")),
        (None, 20.0, 50.0),
        (100.0, 20.0, 0.0),
        (100.0, 20.0, -5.0),
    ],
)
def test_ep_exclude_aux_degrades_to_nan(prop, recup, dist):
    assert math.isnan(rb._ep_exclude_aux(prop, recup, dist))


def test_corrected_energy_perf_removes_the_battery_side_elevation_energy():
    # E_potential = m*g*dh/3.6e6 = 30000*9.81*100/3_600_000 = 8.175 kWh
    # uphill draws it from the battery through the drivetrain: 8.175 / 0.90 = 9.0833
    # corrected = (|-100| - 9.0833) / 50 = 90.9167 / 50 = 1.8183
    assert rb._corrected_energy_perf(-100.0, 50.0, 100.0, 30000.0) == pytest.approx(
        1.8183, abs=5e-5
    )


def test_corrected_energy_perf_downhill_adds_back_only_the_recovered_share():
    # Downhill returns eta * E_potential = 0.90 * 8.175 = 7.3575 kWh
    # corrected = (|-100| + 7.3575) / 50 = 107.3575 / 50 = 2.14715, which the
    # column's 4-decimal rounding resolves downwards in binary floating point.
    assert rb._corrected_energy_perf(-100.0, 50.0, -100.0, 30000.0) == pytest.approx(
        2.14715, abs=1e-4
    )


def test_corrected_energy_perf_downhill_adds_the_recovered_potential():
    uphill = rb._corrected_energy_perf(-100.0, 50.0, 100.0, 30000.0)
    downhill = rb._corrected_energy_perf(-100.0, 50.0, -100.0, 30000.0)
    assert downhill > uphill


def test_corrected_energy_perf_elevation_losses_are_one_directional():
    """The same hill costs more to climb than it repays on the descent.

    The uphill deduction is ``E/eta`` and the downhill credit only ``eta*E``, so
    a there-and-back trip over the same net height is not energy neutral.
    """
    flat = rb._corrected_energy_perf(-100.0, 50.0, 0.0, 30000.0)
    uphill = rb._corrected_energy_perf(-100.0, 50.0, 100.0, 30000.0)
    downhill = rb._corrected_energy_perf(-100.0, 50.0, -100.0, 30000.0)
    assert (flat - uphill) > (downhill - flat)


def test_corrected_energy_perf_uses_the_documented_gravity_constant():
    assert rb._G == 9.81


@pytest.mark.parametrize(
    "energy, dist, elev, mass",
    [
        (float("nan"), 50.0, 10.0, 30000.0),
        (-100.0, float("nan"), 10.0, 30000.0),
        (-100.0, 50.0, float("nan"), 30000.0),
        (-100.0, 50.0, 10.0, float("nan")),
        (-100.0, 0.0, 10.0, 30000.0),
    ],
)
def test_corrected_energy_perf_degrades_to_nan(energy, dist, elev, mass):
    assert math.isnan(rb._corrected_energy_perf(energy, dist, elev, mass))


def test_kinetics_corrected_energy_perf_penalises_a_stop_start_cycle():
    # A constant-speed trip has no net kinetic term, so the kinetics correction
    # equals the plain elevation correction; an accelerate/decelerate cycle costs
    # extra battery energy and therefore corrects LOWER.
    steady = np.full(120, 50.0)
    cyclic = np.concatenate([np.linspace(0, 80, 60), np.linspace(80, 0, 60)])
    flat = rb._kinetics_corrected_energy_perf(-100.0, 50.0, 0.0, 30000.0, steady)
    varied = rb._kinetics_corrected_energy_perf(-100.0, 50.0, 0.0, 30000.0, cyclic)
    assert flat == pytest.approx(2.0, abs=1e-6)
    assert varied < flat


def test_kinetics_corrected_energy_perf_rejects_an_implausible_correction():
    # Guard: a correction above 80 % of the battery energy is not believable.
    speeds = np.concatenate([np.zeros(30), np.full(30, 100.0)])
    assert math.isnan(
        rb._kinetics_corrected_energy_perf(-0.5, 50.0, 0.0, 40000.0, speeds)
    )


@pytest.mark.parametrize("speeds", [None, np.array([]), np.array([10.0])])
def test_kinetics_corrected_energy_perf_needs_two_speed_samples(speeds):
    assert math.isnan(
        rb._kinetics_corrected_energy_perf(-100.0, 50.0, 0.0, 30000.0, speeds)
    )


def test_kinetics_corrected_energy_perf_tolerates_a_nan_elevation():
    out = rb._kinetics_corrected_energy_perf(
        -100.0, 50.0, float("nan"), 30000.0, np.full(60, 50.0)
    )
    assert out == pytest.approx(2.0, abs=1e-6)


def test_kinetics_efficiency_constants_are_symmetric():
    assert rb.ETA_DT == 0.90
    assert rb.ETA_REGEN == 0.90
    assert rb._V_MAX_KMH == 100.0


# ── _get_vehicle_mass ────────────────────────────────────────────────────────


def _mass_df(rows):
    """rows: list of (iso time, mass, speed)."""
    return pd.DataFrame(
        {
            TIME_COL: [r[0] for r in rows],
            "gross_combination_vehicle_weight": [r[1] for r in rows],
            "wheel_based_speed": [r[2] for r in rows],
        }
    )


T0, T1 = "2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"


def test_get_vehicle_mass_prefers_moving_samples():
    df = _mass_df(
        [
            ("2025-06-27T08:00:00Z", 12000.0, 0.0),  # stationary, unreliable
            ("2025-06-27T08:10:00Z", 30000.0, 60.0),
            ("2025-06-27T08:20:00Z", 30200.0, 60.0),
            ("2025-06-27T08:30:00Z", 11000.0, 0.0),  # stationary
        ]
    )
    mass, cv = rb._get_vehicle_mass(df, T0, T1, method="mean")
    assert mass == 30100.0
    assert cv > 0


def test_get_vehicle_mass_falls_back_to_all_positive_samples():
    # Only ONE moving sample -> the >= 2 rule fails, so all > 0 samples are used.
    df = _mass_df(
        [
            ("2025-06-27T08:00:00Z", 12000.0, 0.0),
            ("2025-06-27T08:10:00Z", 30000.0, 60.0),
        ]
    )
    mass, _ = rb._get_vehicle_mass(df, T0, T1, method="mean")
    assert mass == 21000.0


def test_get_vehicle_mass_filters_zero_and_negative_broadcasts():
    df = _mass_df(
        [
            ("2025-06-27T08:00:00Z", 0.0, 60.0),  # J1939 default while stationary
            ("2025-06-27T08:10:00Z", 30000.0, 60.0),
            ("2025-06-27T08:20:00Z", 32000.0, 60.0),
        ]
    )
    mass, _ = rb._get_vehicle_mass(df, T0, T1, method="mean")
    assert mass == 31000.0


def test_get_vehicle_mass_respects_the_window():
    df = _mass_df(
        [
            ("2025-06-27T07:00:00Z", 44000.0, 60.0),  # before the window
            ("2025-06-27T08:10:00Z", 30000.0, 60.0),
            ("2025-06-27T08:20:00Z", 30000.0, 60.0),
            ("2025-06-27T10:00:00Z", 10000.0, 60.0),  # after the window
        ]
    )
    mass, _ = rb._get_vehicle_mass(df, T0, T1, method="mean")
    assert mass == 30000.0


def test_get_vehicle_mass_honours_the_aggregation_method():
    df = _mass_df(
        [
            ("2025-06-27T08:00:00Z", 30000.0, 60.0),
            ("2025-06-27T08:10:00Z", 30000.0, 60.0),
            ("2025-06-27T08:20:00Z", 30000.0, 60.0),
            ("2025-06-27T08:30:00Z", 49000.0, 60.0),
        ]
    )
    mean_mass, _ = rb._get_vehicle_mass(df, T0, T1, method="mean")
    median_mass, _ = rb._get_vehicle_mass(df, T0, T1, method="median")
    assert mean_mass == 34750.0
    assert median_mass == 30000.0


def test_get_vehicle_mass_mad_tw_mean_builds_a_time_axis():
    df = _mass_df(
        [
            ("2025-06-27T08:00:00Z", 30000.0, 60.0),
            ("2025-06-27T08:00:01Z", 30000.0, 60.0),
            ("2025-06-27T08:00:02Z", 30000.0, 60.0),
            ("2025-06-27T08:50:00Z", 31000.0, 60.0),
        ]
    )
    plain, _ = rb._get_vehicle_mass(df, T0, T1, method="mad_mean")
    weighted, _ = rb._get_vehicle_mass(df, T0, T1, method="mad_tw_mean")
    assert weighted > plain


def test_get_vehicle_mass_without_the_weight_column():
    df = pd.DataFrame({TIME_COL: [T0], "wheel_based_speed": [10.0]})
    mass, cv = rb._get_vehicle_mass(df, T0, T1)
    assert math.isnan(mass) and math.isnan(cv)


def test_get_vehicle_mass_without_a_speed_column_uses_all_positive_samples():
    df = pd.DataFrame(
        {
            TIME_COL: ["2025-06-27T08:00:00Z", "2025-06-27T08:10:00Z"],
            "gross_combination_vehicle_weight": [20000.0, 30000.0],
        }
    )
    mass, _ = rb._get_vehicle_mass(df, T0, T1, method="mean")
    assert mass == 25000.0


# ── Recuperation ─────────────────────────────────────────────────────────────


def test_get_recuperation_differences_the_cumulative_counter():
    df = pd.DataFrame(
        {
            TIME_COL: ["2025-06-27T08:00:00Z", "2025-06-27T08:30:00Z"],
            "electric_energy_recuperation_watthours": [1000.0, 6500.0],
        }
    )
    assert rb._get_recuperation(df, T0, T1) == 5.5


def test_get_recuperation_needs_two_samples():
    df = pd.DataFrame(
        {
            TIME_COL: ["2025-06-27T08:00:00Z"],
            "electric_energy_recuperation_watthours": [1000.0],
        }
    )
    assert math.isnan(rb._get_recuperation(df, T0, T1))


def test_get_recuperation_without_the_column():
    df = pd.DataFrame({TIME_COL: ["2025-06-27T08:00:00Z"]})
    assert math.isnan(rb._get_recuperation(df, T0, T1))


# ── Elevation ────────────────────────────────────────────────────────────────


def test_get_elevation_diff_is_end_minus_start():
    df = pd.DataFrame(
        {
            TIME_COL: ["2025-06-27T08:00:00Z", "2025-06-27T08:30:00Z"],
            "gnss_altitude": [100.0, 142.5],
        }
    )
    assert rb._get_elevation_diff(df, T0, T1, "gnss_altitude") == 42.5


@pytest.mark.parametrize("col", [None, "no_such_column"])
def test_get_elevation_diff_without_a_usable_column(col):
    df = pd.DataFrame({TIME_COL: ["2025-06-27T08:00:00Z"], "gnss_altitude": [100.0]})
    assert math.isnan(rb._get_elevation_diff(df, T0, T1, col))


# ── Propulsion counter ───────────────────────────────────────────────────────


def _prop_df(pairs):
    return pd.DataFrame(
        {
            TIME_COL: [p[0] for p in pairs],
            "electric_energy_propulsion": [p[1] for p in pairs],
        }
    )


def test_propulsion_at_interpolates_linearly():
    times = (
        pd.DatetimeIndex(
            pd.to_datetime(["2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"])
        )
        .as_unit("ns")
        .asi8
    )
    values = np.array([1000.0, 3000.0])
    mid = rb._propulsion_at(pd.Timestamp("2025-06-27T08:30:00Z"), times, values)
    assert mid == pytest.approx(2000.0)


def test_propulsion_at_clamps_outside_the_sample_range():
    times = (
        pd.DatetimeIndex(
            pd.to_datetime(["2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"])
        )
        .as_unit("ns")
        .asi8
    )
    values = np.array([1000.0, 3000.0])
    assert (
        rb._propulsion_at(pd.Timestamp("2025-06-27T07:00:00Z"), times, values) == 1000.0
    )
    assert (
        rb._propulsion_at(pd.Timestamp("2025-06-27T10:00:00Z"), times, values) == 3000.0
    )


def test_get_propulsion_energy_brackets_the_window():
    # Sparse RFMS snapshots at 08:00 (1000 Wh) and 09:00 (3000 Wh); the window
    # 08:15..08:45 interpolates to 1500 and 2500 Wh -> 1.0 kWh.
    df = _prop_df([("2025-06-27T08:00:00Z", 1000.0), ("2025-06-27T09:00:00Z", 3000.0)])
    out = rb._get_propulsion_energy(
        df, pd.Timestamp("2025-06-27T08:15:00Z"), pd.Timestamp("2025-06-27T08:45:00Z")
    )
    assert out == pytest.approx(1.0)


def test_get_propulsion_energy_partial_coverage_uses_in_window_samples():
    df = _prop_df(
        [
            ("2025-06-27T08:00:00Z", 1000.0),
            ("2025-06-27T08:40:00Z", 2000.0),
            ("2025-06-27T09:00:00Z", 3000.0),
        ]
    )
    out = rb._get_propulsion_energy(
        df, pd.Timestamp("2025-06-27T08:30:00Z"), pd.Timestamp("2025-06-27T10:00:00Z")
    )
    assert out == pytest.approx(1.0)


def test_get_propulsion_energy_rejects_a_decreasing_counter():
    df = _prop_df([("2025-06-27T08:00:00Z", 3000.0), ("2025-06-27T09:00:00Z", 1000.0)])
    out = rb._get_propulsion_energy(
        df, pd.Timestamp("2025-06-27T08:15:00Z"), pd.Timestamp("2025-06-27T08:45:00Z")
    )
    assert math.isnan(out)


def test_get_propulsion_energy_out_of_range_window():
    df = _prop_df([("2025-06-27T08:00:00Z", 1000.0), ("2025-06-27T09:00:00Z", 3000.0)])
    out = rb._get_propulsion_energy(
        df, pd.Timestamp("2025-06-27T12:00:00Z"), pd.Timestamp("2025-06-27T13:00:00Z")
    )
    assert math.isnan(out)


def test_get_propulsion_energy_without_the_column():
    df = pd.DataFrame({TIME_COL: ["2025-06-27T08:00:00Z"]})
    assert math.isnan(
        rb._get_propulsion_energy(
            df,
            pd.Timestamp("2025-06-27T08:00:00Z"),
            pd.Timestamp("2025-06-27T09:00:00Z"),
        )
    )


# ── Postcode lookup stays offline ────────────────────────────────────────────


def test_get_postcode_without_srf_data_returns_none_and_writes_nothing(tmp_path):
    assert rb._get_postcode(51.5, -0.12, srf_data=None) is None
    assert rb._get_postcode(None, None, srf_data=None) is None
    assert rb._get_postcode(0.0, 0.0, srf_data=None) is None  # null island guard
    assert rb._get_postcode("abc", "def", srf_data=None) is None


# ── Stop-row synthesis ───────────────────────────────────────────────────────


def _ev_row(**values):
    row = [float("nan")] * (len(HEADERS) - 1)
    for header, value in values.items():
        row[_row_col_index(header)] = value
    return row


PREV = dict(
    **{
        "Leg Type": "In Transit",
        "End Time (UTC)": pd.Timestamp("2025-06-27T09:00:00Z"),
        "Destination (Lat, Lon)": "Point(0.500000 0.500000)",
        "Destination Place": "AB1 2CD",
        "Vehicle Mass (kg)": 30000.0,
        "Vehicle Mass CV (reliability)": 0.02,
        "Cumulative Distance (km)": 120.0,
        "End SOC (%)": 60.0,
        "Operator": "WJF",
    }
)
NEXT = dict(
    **{
        "Leg Type": "In Transit",
        "Start Time (UTC)": pd.Timestamp("2025-06-27T11:00:00Z"),
        "Origin (Lat, Lon)": "Point(0.500001 0.500001)",
        "Origin Place": "AB1 2CD",
        "Start SOC (%)": 58.0,
        "Operator": "WJF",
    }
)


def test_stop_row_core_fields():
    stop = rb._stop_row_from_neighbours(_ev_row(**PREV), _ev_row(**NEXT))

    def get(header):
        return stop[_row_col_index(header)]

    assert get("Leg Type") == "Stop"
    assert get("Start Time (UTC)") == PREV["End Time (UTC)"]
    assert get("End Time (UTC)") == NEXT["Start Time (UTC)"]
    # Duration is stored as a fraction of a day (2 h = 1/12).
    assert get("Duration (HH:MM:SS)") == pytest.approx(2 / 24)
    assert get("Distance (km)") == 0.0
    assert get("Average Speed (km/h)") == 0.0
    assert get("Elevation Difference (m)") == 0.0
    # Location is carried from the previous leg's destination.
    assert get("Origin (Lat, Lon)") == PREV["Destination (Lat, Lon)"]
    assert get("Destination (Lat, Lon)") == PREV["Destination (Lat, Lon)"]
    assert get("Origin Place") == "AB1 2CD"
    # Mass and cumulative distance carry over.
    assert get("Vehicle Mass (kg)") == 30000.0
    assert get("Cumulative Distance (km)") == 120.0
    # SOC endpoints expose the standby drain across the stop.
    assert get("Start SOC (%)") == 60.0
    assert get("End SOC (%)") == 58.0
    assert get("SOC Change (%)") == pytest.approx(-2.0)
    # Links are explicitly blanked (None, not NaN) so the writer leaves them empty.
    for link in ("Telematics Link", "Charger Link", "SRF Logger Link"):
        assert get(link) is None
    assert get("Operator") == "WJF"


def test_stop_row_falls_back_to_the_next_rows_origin():
    prev = _ev_row(**{k: v for k, v in PREV.items() if not k.startswith("Destination")})
    stop = rb._stop_row_from_neighbours(prev, _ev_row(**NEXT))
    assert stop[_row_col_index("Origin (Lat, Lon)")] == NEXT["Origin (Lat, Lon)"]


def test_stop_row_takes_the_operator_from_the_next_leg_when_missing():
    prev = _ev_row(**{k: v for k, v in PREV.items() if k != "Operator"})
    stop = rb._stop_row_from_neighbours(prev, _ev_row(**NEXT))
    assert stop[_row_col_index("Operator")] == "WJF"


def test_stop_row_diesel_layout_has_no_soc_bookkeeping():
    def diesel_row(**values):
        row = [float("nan")] * (len(DIESEL_HEADERS) - 1)
        for header, value in values.items():
            row[_row_col_index(header, DIESEL_HEADERS)] = value
        return row

    prev = diesel_row(
        **{
            "Leg Type": "In Transit",
            "End Time (UTC)": pd.Timestamp("2025-10-07T09:00:00Z"),
            "Destination (Lat, Lon)": "Point(0.500000 0.500000)",
            "Vehicle Mass (kg)": 40000.0,
            "Operator": "WJF",
        }
    )
    nxt = diesel_row(
        **{
            "Leg Type": "In Transit",
            "Start Time (UTC)": pd.Timestamp("2025-10-07T10:00:00Z"),
        }
    )
    stop = rb._stop_row_from_neighbours(prev, nxt, headers=DIESEL_HEADERS)
    assert len(stop) == len(DIESEL_HEADERS) - 1
    assert stop[_row_col_index("Leg Type", DIESEL_HEADERS)] == "Stop"
    assert stop[_row_col_index("Vehicle Mass (kg)", DIESEL_HEADERS)] == 40000.0
    assert "Start SOC (%)" not in DIESEL_HEADERS


# ── _insert_stop_rows ────────────────────────────────────────────────────────


def _timed_row(start, end, operator="WJF", leg_type="In Transit"):
    return _ev_row(
        **{
            "Leg Type": leg_type,
            "Start Time (UTC)": pd.Timestamp(start),
            "End Time (UTC)": pd.Timestamp(end),
            "Operator": operator,
        }
    )


def test_insert_stop_rows_fills_a_real_gap():
    rows = [
        _timed_row("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"),
        _timed_row("2025-06-27T11:00:00Z", "2025-06-27T12:00:00Z"),
    ]
    out = rb._insert_stop_rows(rows)
    assert [r[0] for r in out] == ["In Transit", "Stop", "In Transit"]


def test_insert_stop_rows_ignores_a_sub_threshold_gap():
    # 30 s < STOP_MIN_GAP_SECONDS (60 s): a segmentation boundary artefact.
    rows = [
        _timed_row("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"),
        _timed_row("2025-06-27T09:00:30Z", "2025-06-27T10:00:00Z"),
    ]
    assert len(rb._insert_stop_rows(rows)) == 2


def test_insert_stop_rows_threshold_is_configurable():
    rows = [
        _timed_row("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"),
        _timed_row("2025-06-27T09:00:30Z", "2025-06-27T10:00:00Z"),
    ]
    assert len(rb._insert_stop_rows(rows, min_gap_seconds=10)) == 3


def test_insert_stop_rows_does_not_mutate_its_input():
    rows = [
        _timed_row("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"),
        _timed_row("2025-06-27T11:00:00Z", "2025-06-27T12:00:00Z"),
    ]
    before = len(rows)
    out = rb._insert_stop_rows(rows)
    assert len(rows) == before
    assert out is not rows
    # The original row objects are re-used by reference (no copying), so a Stop
    # must never have been spliced INTO the caller's list.
    assert out[0] is rows[0] and out[2] is rows[1]


def test_insert_stop_rows_carries_the_operator_into_the_stop():
    rows = [
        _timed_row("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z", operator="JLP"),
        _timed_row("2025-06-27T11:00:00Z", "2025-06-27T12:00:00Z", operator="JLP"),
    ]
    stop = rb._insert_stop_rows(rows)[1]
    assert stop[_row_col_index("Operator")] == "JLP"


def test_insert_stop_rows_handles_multiple_gaps():
    rows = [
        _timed_row("2025-06-27T06:00:00Z", "2025-06-27T07:00:00Z"),
        _timed_row("2025-06-27T09:00:00Z", "2025-06-27T10:00:00Z"),
        _timed_row("2025-06-27T13:00:00Z", "2025-06-27T14:00:00Z"),
    ]
    out = rb._insert_stop_rows(rows)
    assert [r[0] for r in out] == [
        "In Transit",
        "Stop",
        "In Transit",
        "Stop",
        "In Transit",
    ]


def test_insert_stop_rows_empty_and_single_row():
    assert rb._insert_stop_rows([]) == []
    single = [_timed_row("2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z")]
    assert len(rb._insert_stop_rows(single)) == 1


def test_insert_stop_rows_skips_unparseable_timestamps():
    bad = _ev_row(**{"Leg Type": "In Transit", "End Time (UTC)": "not-a-time"})
    good = _timed_row("2025-06-27T11:00:00Z", "2025-06-27T12:00:00Z")
    assert len(rb._insert_stop_rows([bad, good])) == 2


def test_stop_min_gap_seconds_is_the_documented_default():
    assert rb.STOP_MIN_GAP_SECONDS == 60.0
