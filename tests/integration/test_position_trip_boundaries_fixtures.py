"""``position_trip_boundaries`` on real, anonymised Mercedes eActros 600 legs.

* EVSPD04 (a registered fixture) is a day with no Logger data, so its speed
  pipeline falls back to the SOC detector, whose trips end at the last SOC step.
  At 03:05:08 the SOC froze at 92 % while the vehicle drove on for 112 km, then
  read 74 % at 04:34:37, the first fix after it had parked: the trip ended at
  03:05:08 and the drive became a 0 km Stop. Two trips ended on the road into the
  depot, 1.2-1.6 km short of the charger (10:19:53, 14:15:29). With the key the
  first trip runs to the arrival, 02:31:39 -> 04:34:37 over 157.515 km, its SOC
  99 -> 74 % carrying the whole drop, and every trip reaching the depot ends
  within 0.5 km of the charger.
* EVPOS02 and EVPOS01 (excerpts, not registered: their trips come from the
  Logger speed, which a fixture cannot carry) are handed the trips the upstream
  steps produced. On EVPOS02 a mass split had cut the day's first trip at the
  first reading of the new trailer mass, 1.4 km out: the trip starts at the
  departure from the depot again, 04:11:12. On EVPOS01 a trip bridged a depot
  visit (arrival 09:07:34, 51 minutes seen standing) and ran on to a yard
  1.6 km away: it ends at the depot arrival, and the yard move, too small for
  the pipeline's SOC floor, is left out.

With the key absent or off, EVSPD04 reproduces its golden.
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from report_generator.segmentation import constants
from report_generator.segmentation.detection import (
    _ODOMETER_CONFIRMED_KEY,
    _blank_replayed_odometer,
    _trip_measurer,
)
from report_generator.segmentation.position_boundaries import (
    read_positions,
    settle_trip_boundaries,
)

POSITION_PARAMS = {"stay_radius_km": 0.5, "stay_min_minutes": 31, "stay_max_km": 5}


def _km(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return float(2 * 6371.0088 * np.arcsin(np.sqrt(a)))


@pytest.fixture
def with_key(monkeypatch, frozen_configs):
    """Set ``position_trip_boundaries`` (and its parameters) on EVSPD04's pipeline."""

    def _set(value=True, params=POSITION_PARAMS):
        name = frozen_configs["vehicles"]["EVSPD04"]["pipeline"]
        pipeline = copy.deepcopy(frozen_configs["pipelines"][name])
        pipeline["position_trip_boundaries"] = value
        if params is not None:
            pipeline["position_params"] = dict(params)
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, name, pipeline)

    return _set


def _windows(segs):
    return [
        (
            str(s["start_time"]),
            str(s["end_time"]),
            s["start_soc"],
            s["end_soc"],
            s["odo_start_km"],
            s["odo_end_km"],
            s["delta_energy_kwh"],
        )
        for s in segs
    ]


# ── EVSPD04: the SOC fallback's trips on a day without Logger data ───────────


def test_the_key_off_reproduces_the_golden(
    with_key, run_fixture_segmentation, load_golden, serialise
):
    golden = load_golden("segments_EVSPD04.json")
    with_key(False)
    charges, trips = run_fixture_segmentation("EVSPD04")
    assert serialise(charges) == golden["charge"]
    assert serialise(trips) == golden["discharge"]


def test_the_trips_follow_the_positions(with_key, run_fixture_segmentation):
    with_key()
    _, trips = run_fixture_segmentation("EVSPD04")
    assert _windows(trips) == [
        # The frozen SOC's drive, now inside the trip it belongs to.
        (
            "2026-07-22 02:31:39",
            "2026-07-22 04:34:37",
            99.0,
            74.0,
            35458.79,
            35616.305,
            -150.0,
        ),
        (
            "2026-07-22 05:13:08",
            "2026-07-22 06:31:47",
            74.0,
            69.0,
            35616.32,
            35641.01,
            -30.0,
        ),
        (
            "2026-07-22 06:41:25",
            "2026-07-22 07:38:09",
            69.0,
            63.0,
            35646.155,
            35672.3,
            -36.0,
        ),
        (
            "2026-07-22 08:55:58",
            "2026-07-22 10:23:23",
            61.0,
            43.0,
            35683.94,
            35790.525,
            -108.0,
        ),
        (
            "2026-07-22 12:13:41",
            "2026-07-22 12:53:20",
            73.0,
            66.0,
            35792.135,
            35822.95,
            -42.0,
        ),
        (
            "2026-07-22 12:56:35",
            "2026-07-22 14:24:59",
            66.0,
            61.0,
            35822.95,
            35853.83,
            -30.0,
        ),
    ]


def test_the_drive_the_soc_froze_on_is_no_longer_a_stop(
    with_key, run_fixture_segmentation, load_raw_telematics
):
    with_key()
    _, trips = run_fixture_segmentation("EVSPD04")
    df = load_raw_telematics("EVSPD04")
    t = pd.to_datetime(df["eventDatetime"], utc=True)
    odo = pd.to_numeric(df["odometer"], errors="coerce")

    def odometer_between(start, end):
        inside = (t >= pd.Timestamp(start, tz="UTC")) & (
            t <= pd.Timestamp(end, tz="UTC")
        )
        return float(odo[inside].max() - odo[inside].min())

    first, second = trips[0], trips[1]
    # What the Stop after the first trip holds: 15 m of manoeuvring, not 112 km.
    assert odometer_between(first["end_time"], second["start_time"]) < 0.05
    assert first["odo_end_km"] - first["odo_start_km"] == pytest.approx(157.515)


def test_the_trips_into_the_depot_end_at_the_depot(with_key, run_fixture_segmentation):
    with_key(False)
    charges, before = run_fixture_segmentation("EVSPD04")
    with_key()
    _, after = run_fixture_segmentation("EVSPD04")
    depot = (charges[-1]["latitude"], charges[-1]["longitude"])

    def to_depot(seg):
        return _km(seg["lat_end"], seg["lon_end"], *depot)

    # Without the key: two trips end on the road in, 0.9 and 1.6 km short.
    assert [to_depot(s) for s in before if to_depot(s) < 2.0] == pytest.approx(
        [0.9, 1.6], abs=0.05
    )
    # With it, every trip reaching the depot ends within half a kilometre.
    assert all(to_depot(s) < 0.5 for s in after if to_depot(s) < 2.0)
    assert sum(to_depot(s) < 0.5 for s in after) == 2


def test_the_segments_leave_with_the_public_schema(with_key, run_fixture_segmentation):
    with_key()
    _, trips = run_fixture_segmentation("EVSPD04")
    assert all(_ODOMETER_CONFIRMED_KEY not in s for s in trips)
    assert all("ep_audit" in s for s in trips)
    assert all(s["energy_source"] == "soc_estimate" for s in trips)


def test_the_default_stay_parameters_give_the_same_day(
    with_key, run_fixture_segmentation
):
    # 31 minutes is the pipeline's stop gap; the 30-minute default agrees here.
    with_key(params=None)
    _, trips = run_fixture_segmentation("EVSPD04")
    assert [w[:2] for w in _windows(trips)][0] == (
        "2026-07-22 02:31:39",
        "2026-07-22 04:34:37",
    )


def test_the_result_is_deterministic(with_key, run_fixture_segmentation, serialise):
    with_key()
    first = run_fixture_segmentation("EVSPD04")
    second = run_fixture_segmentation("EVSPD04")
    assert serialise(first[1]) == serialise(second[1])


# ── Excerpts: trips from the Logger speed ────────────────────────────────────


@pytest.fixture
def excerpt(fixtures_dir, frozen_configs):
    """An excerpt's frame (odometer cleaned as the generator does) and a measure."""

    def _load(name):
        df = pd.read_csv(fixtures_dir / "positions" / f"{name}.csv", dtype=str)
        df, _ = _blank_replayed_odometer(df)
        vehicle = frozen_configs["vehicles"]["EVSPD04"]
        pipeline = frozen_configs["pipelines"][vehicle["pipeline"]]
        speed_p = dict(
            pipeline["speed_params"],
            speed_col=vehicle["speed_col"],
            total_energy_col=vehicle["total_energy_col"],
            moving_energy_col=vehicle["moving_energy_col"],
            nominal_kwh=vehicle["nominal_kwh"],
            cap_lo=vehicle["nominal_kwh"] * 0.5,
            cap_hi=vehicle["nominal_kwh"] * 2.0,
        )
        measure = _trip_measurer(df, [], speed_p, {})
        return df, measure

    return _load


def _upstream(measure, windows):
    """The trips as the Logger-speed detector and the mass split left them."""
    segs = []
    for start, end, aware in windows:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        if aware:  # a mass split writes its parts' boundaries aware
            s, e = s.tz_localize("UTC"), e.tz_localize("UTC")
        segs.extend(measure(s, e))
    assert len(segs) == len(windows)
    return segs


def test_a_start_the_mass_split_moved_onto_the_road_returns_to_the_depot(excerpt):
    df, measure = excerpt("EVPOS02_2026-09-22")
    trips = _upstream(
        measure,
        [
            ("2026-09-22 04:15:45", "2026-09-22 06:38:03", True),
            ("2026-09-22 07:09:15", "2026-09-22 07:50:41", False),
            ("2026-09-22 08:27:06", "2026-09-22 09:51:44", True),
        ],
    )
    positions = read_positions(df, **POSITION_PARAMS)
    settled, counts = settle_trip_boundaries(trips, [], positions, measure)
    depot = (
        float(df["latitude"].iloc[0]),
        float(df["longitude"].iloc[0]),
    )  # at the charger

    first = settled[0]
    assert str(first["start_time"]) == "2026-09-22 04:11:12+00:00"
    assert _km(trips[0]["lat_start"], trips[0]["lon_start"], *depot) == pytest.approx(
        1.42, abs=0.01
    )
    assert _km(first["lat_start"], first["lon_start"], *depot) < 0.5
    # The SOC step the vehicle made leaving the depot now belongs to the trip.
    assert (first["start_soc"], first["end_soc"], first["delta_energy_kwh"]) == (
        99.0,
        82.0,
        -102.0,
    )
    assert settled[1] is trips[1]  # a trip already bounded by stays is kept as it is
    assert counts == {"moved": 2, "cut": 0, "kept": 0, "dropped": 0}


def test_a_trip_through_a_depot_visit_ends_at_the_depot(excerpt):
    df, measure = excerpt("EVPOS01_2026-08-26")
    trips = _upstream(
        measure,
        [
            ("2026-08-26 02:43:56", "2026-08-26 04:04:40", False),
            ("2026-08-26 05:57:54", "2026-08-26 06:31:31", False),
            ("2026-08-26 07:55:37", "2026-08-26 11:08:42", True),
        ],
    )
    positions = read_positions(df, **POSITION_PARAMS)
    settled, counts = settle_trip_boundaries(trips, [], positions, measure)
    depot = (float(df["latitude"].iloc[0]), float(df["longitude"].iloc[0]))

    assert settled[:2] == trips[:2]
    last = settled[2]
    assert (str(last["start_time"]), str(last["end_time"])) == (
        "2026-08-26 07:37:21+00:00",
        "2026-08-26 09:07:34+00:00",
    )
    assert _km(trips[2]["lat_end"], trips[2]["lon_end"], *depot) == pytest.approx(
        1.63, abs=0.01
    )
    assert _km(last["lat_end"], last["lon_end"], *depot) < 0.5
    # The move to the yard after the visit (SOC 70 -> 69 %) is below the
    # pipeline's 2-point floor: it is no trip, and nothing replaces it.
    assert len(settled) == 3
    assert counts == {"moved": 1, "cut": 1, "kept": 0, "dropped": 1}
