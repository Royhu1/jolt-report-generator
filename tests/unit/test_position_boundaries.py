"""Trip boundaries from the positions, on synthetic fixes.

``read_positions`` finds the places a leg's vehicle stayed at (a run of fixes
within a radius of the first, lasting long enough, the odometer barely moving)
and the halts on the way; ``settled_windows`` moves one trip's boundaries onto
them and ``settle_trip_boundaries`` does it for a leg's trips, measuring each
moved trip again. The fixes here lie on a north-south line, positions given in
km from an origin, times in minutes from midnight.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from report_generator.segmentation.position_boundaries import (
    Positions,
    Stay,
    read_positions,
    settle_trip_boundaries,
    settled_windows,
)

DAY = pd.Timestamp("2026-07-22T00:00:00Z")
KM_PER_DEG_LAT = 111.195


def _t(minutes: float) -> pd.Timestamp:
    return DAY + pd.Timedelta(minutes=minutes)


def _frame(rows) -> pd.DataFrame:
    """``rows``: (minute, km north of the origin, odometer km[, km east]) tuples."""
    out = []
    for row in rows:
        minute, north_km, odo = row[:3]
        east_km = row[3] if len(row) > 3 else 0.0
        out.append(
            {
                "eventDatetime": _t(minute).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "latitude": str(53.0 + north_km / KM_PER_DEG_LAT),
                "longitude": str(
                    -1.0 + east_km / (KM_PER_DEG_LAT * np.cos(np.radians(53.0)))
                ),
                "odometer": "" if odo is None else str(odo),
                "electricBatteryLevelPercent": "80",
            }
        )
    return pd.DataFrame(out, dtype=str)


def _parked(start, end, step, north_km=0.0, odo=100.0, jitter_km=0.0):
    """Fixes every ``step`` minutes at one place, the odometer standing."""
    rows = []
    for k, minute in enumerate(np.arange(start, end + 1e-9, step)):
        wobble = jitter_km * (1 if k % 2 else -1)
        rows.append((float(minute), north_km + wobble, odo))
    return rows


def _drive(start, end, step, north_from, north_to, odo_from):
    """Fixes every ``step`` minutes on the way, the odometer following."""
    rows = []
    minutes = np.arange(start, end + 1e-9, step)
    for minute in minutes:
        f = (minute - start) / (end - start)
        north = north_from + f * (north_to - north_from)
        rows.append((float(minute), north, odo_from + abs(north - north_from)))
    return rows


# ── Stays ────────────────────────────────────────────────────────────────────


def test_a_parked_vehicle_is_one_stay_whatever_its_gps_scatter():
    df = _frame(_parked(0, 120, 5, jitter_km=0.03))
    positions = read_positions(df, stay_min_minutes=30)
    assert len(positions.stays) == 1
    stay = positions.stays[0]
    assert (stay.first, stay.last) == (_t(0), _t(120))
    assert (stay.arrival, stay.departure) == (_t(0), _t(120))
    assert stay.cuts


def test_continuous_driving_has_no_stay_and_no_halt():
    df = _frame(_drive(0, 120, 2, 0.0, 150.0, 1000.0))
    positions = read_positions(df, stay_min_minutes=30)
    assert positions.stays == []
    assert positions.halts == []


def test_a_stop_shorter_than_the_minimum_is_a_halt_not_a_stay():
    rows = _drive(0, 30, 2, 0.0, 40.0, 1000.0)
    rows += _parked(32, 52, 2, north_km=40.0, odo=1040.0)
    rows += _drive(54, 80, 2, 40.0, 70.0, 1040.0)
    positions = read_positions(_frame(rows), stay_min_minutes=30)
    assert positions.stays == []
    assert positions.halts == [(_t(30), _t(54))]


def test_arrival_and_departure_are_the_first_and_last_standing_fixes():
    rows = [(0.0, -3.0, 97.0), (2.0, -0.1, 99.9), (4.0, 0.0, 100.0)]  # arriving
    rows += _parked(6, 60, 6, odo=100.0)
    rows += [(62.0, 0.15, 100.15), (64.0, 0.3, 100.3)]  # leaving, still inside
    rows += _drive(66, 90, 2, 1.5, 30.0, 101.5)
    stay = read_positions(_frame(rows), stay_min_minutes=30).stays[0]
    assert stay.first == _t(2)  # first fix inside the radius, still rolling
    assert stay.arrival == _t(4)  # first fix from which it stood still
    assert stay.departure == _t(60)  # last fix reached standing still
    assert stay.last == _t(64)


def test_a_silent_gap_at_a_place_counts_towards_the_stay_but_cannot_cut():
    # One fix at the place, then nothing for an hour, then the vehicle 5 km on.
    rows = _drive(0, 20, 2, -20.0, 0.0, 980.0) + [(80.0, 5.0, 1005.0)]
    rows += _drive(82, 100, 2, 7.0, 25.0, 1007.0)
    stays = read_positions(_frame(rows), stay_min_minutes=30).stays
    assert len(stays) == 1
    stay = stays[0]
    # 60 minutes of gap, of which 5 km at 90 km/h explain 3.3: a stay...
    assert stay.arrival == stay.departure == _t(20)
    # ...but no one saw the vehicle standing there for 30 minutes.
    assert not stay.cuts


def test_no_stay_spans_a_loop_back_to_the_same_place():
    # Back at the same position after 40 minutes of silence, with 40 km more on
    # the odometer: the vehicle drove a loop, it did not stay.
    rows = _parked(0, 20, 5) + _parked(60, 100, 5, north_km=0.05, odo=140.0)
    stays = read_positions(_frame(rows), stay_min_minutes=30).stays
    assert all(s.last <= _t(20) or s.first >= _t(60) for s in stays)
    assert [(s.first, s.last) for s in stays if s.first >= _t(60)] == [
        (_t(60), _t(100))
    ]


def test_an_isolated_position_glitch_does_not_break_a_stay():
    rows = _parked(0, 40, 4)
    rows.insert(5, (18.0, 20.0, 100.0))  # 20 km away and back within 4 minutes
    rows += _parked(44, 80, 4)
    positions = read_positions(_frame(sorted(rows)), stay_min_minutes=30)
    assert len(positions.stays) == 1
    assert (positions.stays[0].first, positions.stays[0].last) == (_t(0), _t(80))


def test_without_positions_there_is_nothing():
    df = _frame(_parked(0, 60, 5)).drop(columns=["latitude", "longitude"])
    positions = read_positions(df)
    assert positions.stays == [] and positions.halts == []
    assert len(positions.fixes_ns) == 0


def test_zero_coordinates_are_no_fixes():
    df = _frame(_parked(0, 60, 5))
    df.loc[:, ["latitude", "longitude"]] = "0"
    assert len(read_positions(df).fixes_ns) == 0


def test_standing_without_odometer_readings_is_judged_by_position():
    rows = [(m, 0.0, None) for m in range(0, 61, 5)]
    rows += [(62.0, 2.0, None), (64.0, 4.0, None)]
    positions = read_positions(_frame(rows), stay_min_minutes=30)
    assert len(positions.stays) == 1
    assert (positions.stays[0].arrival, positions.stays[0].departure) == (_t(0), _t(60))


# ── One trip's windows ───────────────────────────────────────────────────────


def _positions(stays=(), halts=(), fixes=()):
    """``fixes``: (minute, odometer km) pairs; the positions play no part here."""
    minutes = [m for m, _ in fixes]
    return Positions(
        fixes_ns=np.array([_t(m).value for m in minutes], dtype="int64"),
        lat=np.zeros(len(minutes)),
        lon=np.zeros(len(minutes)),
        odo=np.array([o for _, o in fixes], dtype=float),
        stays=list(stays),
        halts=list(halts),
    )


def _stay(first, arrival, departure, last, cuts=True):
    return Stay(_t(first), _t(last), _t(arrival), _t(departure), cuts)


# Parked at km 100 until minute 60, then driving 1 km a minute until the vehicle
# settles at minute 100 at km 140, parked there until minute 160.
_PARKED_DRIVE_PARKED = (
    [(0, 100.0), (60, 100.0)]
    + [(m, 100.0 + (m - 60)) for m in range(62, 100, 2)]
    + [(100, 140.0), (130, 140.0), (160, 140.0)]
)
_STAYS = [_stay(0, 0, 60, 60), _stay(100, 100, 160, 160)]


def test_an_end_short_of_the_next_place_moves_to_its_arrival():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    assert settled_windows(_t(70), _t(80), positions, []) == [(_t(60), _t(100))]


def test_an_end_at_the_last_fix_before_the_arrival_moves_to_the_arrival():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    assert settled_windows(_t(61), _t(98), positions, []) == [(_t(61), _t(100))]


def test_a_start_before_the_vehicle_is_seen_moving_stays():
    # Between the departure fix and the next fix: nothing says the vehicle had
    # left before the start, so a precise start stays.
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    assert settled_windows(_t(61), _t(100), positions, []) == [(_t(61), _t(100))]


def test_a_start_after_the_vehicle_left_moves_back_to_the_departure():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    assert settled_windows(_t(70), _t(100), positions, []) == [(_t(60), _t(100))]


def test_a_start_at_a_fix_on_the_way_moves_back_to_the_departure():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    assert settled_windows(_t(62), _t(100), positions, []) == [(_t(60), _t(100))]


def test_a_boundary_already_at_a_place_is_left_where_it_is():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    # Starting while still parked, ending after arriving: both at a place.
    assert settled_windows(_t(30), _t(130), positions, []) == [(_t(30), _t(130))]


def test_a_boundary_is_not_moved_over_another_segment():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    others = [(_t(85), _t(95))]  # a trip in between
    assert settled_windows(_t(70), _t(80), positions, others) == [(_t(60), _t(80))]


def test_a_boundary_at_a_halt_stays_and_a_halt_ends_an_extension():
    fixes = [
        (0, 100.0),
        (60, 100.0),
        (62, 102.0),
        (64, 104.0),
        (66, 104.0),
        (72, 104.0),
    ]
    fixes += [(m, 104.0 + (m - 72)) for m in range(74, 100, 2)]
    fixes += [(100, 132.0), (160, 132.0)]
    positions = _positions(stays=_STAYS, halts=[(_t(64), _t(72))], fixes=fixes)
    # A trip ending at the halt stays put; one ending before it goes no further.
    assert settled_windows(_t(60), _t(68), positions, []) == [(_t(60), _t(68))]
    assert settled_windows(_t(60), _t(61), positions, []) == [(_t(60), _t(64))]
    # A trip starting after the halt starts back at its last fix, not at 60.
    assert settled_windows(_t(80), _t(100), positions, []) == [(_t(72), _t(100))]


def test_a_stay_on_the_way_cuts_the_trip():
    fixes = [(0, 100.0), (20, 120.0), (40, 140.0), (42, 140.0), (100, 140.0)]
    fixes += [(102, 142.0), (160, 200.0)]
    positions = _positions(stays=[_stay(40, 42, 100, 102)], fixes=fixes)
    assert settled_windows(_t(0), _t(160), positions, []) == [
        (_t(0), _t(42)),
        (_t(100), _t(160)),
    ]


def test_a_stay_known_only_through_a_gap_cuts_nothing():
    fixes = [(0, 100.0), (40, 140.0), (100, 145.0), (160, 200.0)]
    positions = _positions(stays=[_stay(40, 40, 40, 40, cuts=False)], fixes=fixes)
    assert settled_windows(_t(0), _t(160), positions, []) == [(_t(0), _t(160))]


# ── A leg's trips ────────────────────────────────────────────────────────────


def _trip(start, end, aware=False):
    s, e = _t(start), _t(end)
    if not aware:
        s, e = s.tz_convert(None), e.tz_convert(None)
    return {"start_time": s, "end_time": e, "delta_soc_pct": -5.0}


def _measure_all(start, end):
    return [{"start_time": start, "end_time": end, "measured": True}]


def _naive(minute):
    return _t(minute).tz_convert(None)


def test_a_trip_that_does_not_move_is_returned_as_it_is():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    trip = _trip(61, 100)
    segs, counts = settle_trip_boundaries([trip], [], positions, _measure_all)
    assert segs == [trip] and segs[0] is trip
    assert counts == {"moved": 0, "cut": 0, "kept": 0, "dropped": 0}


def test_a_moved_trip_is_measured_again_in_its_own_time_zone_form():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    seen = []

    def measure(start, end):
        seen.append((start, end))
        return _measure_all(start, end)

    segs, counts = settle_trip_boundaries([_trip(70, 80)], [], positions, measure)
    assert seen == [(_naive(60), _naive(100))]
    assert segs[0]["measured"] and counts["moved"] == 1

    seen.clear()
    settle_trip_boundaries([_trip(70, 80, aware=True)], [], positions, measure)
    assert seen == [(_t(60), _t(100))]


def test_a_window_the_floors_reject_is_dropped_and_a_trip_losing_all_is_kept():
    fixes = [(0, 100.0), (20, 120.0), (40, 140.0), (42, 140.0), (100, 140.0)]
    fixes += [(102, 142.0), (160, 200.0)]
    positions = _positions(stays=[_stay(40, 42, 100, 102)], fixes=fixes)

    def reject_after(start, end):
        if pd.Timestamp(start) >= _naive(100):
            return []
        return _measure_all(start, end)

    segs, counts = settle_trip_boundaries([_trip(0, 160)], [], positions, reject_after)
    assert [(s["start_time"], s["end_time"]) for s in segs] == [(_naive(0), _naive(42))]
    assert counts == {"moved": 1, "cut": 1, "kept": 0, "dropped": 1}

    trip = _trip(0, 160)
    segs, counts = settle_trip_boundaries([trip], [], positions, lambda s, e: [])
    assert segs == [trip] and segs[0] is trip
    assert counts == {"moved": 0, "cut": 0, "kept": 1, "dropped": 2}


def test_trips_never_come_to_overlap_each_other_or_a_charge():
    positions = _positions(stays=_STAYS, fixes=_PARKED_DRIVE_PARKED)
    trips = [_trip(70, 75), _trip(78, 84)]
    charge = {"start_time": _t(90), "end_time": _t(95)}
    segs, _ = settle_trip_boundaries(trips, [charge], positions, _measure_all)
    windows = [
        (pd.Timestamp(s["start_time"]), pd.Timestamp(s["end_time"])) for s in segs
    ]
    # The first trip starts back at the departure; nothing moves across the
    # other trip or the charge.
    assert windows == [(_naive(60), _naive(75)), (_naive(78), _naive(84))]


@pytest.mark.parametrize(
    "radius, minutes, expected", [(0.5, 30, 1), (0.05, 30, 0), (0.5, 200, 0)]
)
def test_the_stay_parameters_are_honoured(radius, minutes, expected):
    df = _frame(_parked(0, 120, 5, jitter_km=0.1))
    stays = read_positions(df, stay_radius_km=radius, stay_min_minutes=minutes).stays
    assert len(stays) == expected


def test_the_stay_parameters_are_checked_at_run_time():
    from report_generator.segmentation.detection import _checked_position_params
    from report_generator.segmentation.position_boundaries import (
        POSITION_PARAM_DEFAULTS,
    )

    assert _checked_position_params({}, "ut") == POSITION_PARAM_DEFAULTS
    given = {"position_params": {"stay_min_minutes": 31}}
    assert _checked_position_params(given, "ut")["stay_min_minutes"] == 31
    with pytest.raises(ValueError, match=r"position_params\.stay_radius_km must be"):
        _checked_position_params({"position_params": {"stay_radius_km": 0}}, "ut")
    with pytest.raises(ValueError, match="is not a stay parameter"):
        _checked_position_params({"position_params": {"radius": 1}}, "ut")
    with pytest.raises(ValueError, match="must be an object"):
        _checked_position_params({"position_params": [0.5]}, "ut")


def test_the_loader_checks_the_parameters_the_pass_reads():
    from report_generator import configs
    from report_generator.segmentation.position_boundaries import (
        POSITION_PARAM_DEFAULTS,
    )

    assert set(configs._POSITION_PARAMS_KEYS) == set(POSITION_PARAM_DEFAULTS)
