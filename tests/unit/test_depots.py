"""Depot (base) detection and the second-pass Leg Type of trip and charge rows.

Every test builds its rows inline in the report row-tuple layout (``HEADERS`` or
``DIESEL_HEADERS`` minus ``Leg Number``), on a synthetic map:

    A  (52.0, -1.0)   a depot
    B  (52.5, -1.5)   a second depot, ~65 km from A
    C  (52.2, -1.2)   a customer site, ~27 km from A
    D, E              further customer sites
    P  (52.1, -1.05)  a public charger, ~12 km from A

Distances are hand-checked against the haversine formula the module uses
(0.001 degree of latitude is ~111 m).
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import pandas as pd
import pytest

from report_generator import depots
from report_generator.columns import DIESEL_HEADERS, HEADERS, _row_col_index

NAN = float("nan")
A = (52.0, -1.0)
B = (52.5, -1.5)
C = (52.2, -1.2)
D = (52.3, -0.8)
E = (51.8, -1.3)
P = (52.1, -1.05)
T0 = pd.Timestamp("2026-03-02 00:00")  # a Monday


def north(point, km):
    """``point`` moved ``km`` due north (1 degree of latitude ~ 111.195 km)."""
    return (point[0] + km / 111.195, point[1])


def _pt(p):
    return None if p is None else f"Point({p[0]:.6f} {p[1]:.6f})"


def row(
    label,
    start,
    end,
    origin,
    dest=None,
    *,
    distance=NAN,
    operator="OPX",
    energy=NAN,
    headers=HEADERS,
):
    r = [NAN] * (len(headers) - 1)

    def put(name, value):
        if name in headers:
            r[_row_col_index(name, headers)] = value

    put("Leg Type", label)
    put("Start Time (UTC)", pd.Timestamp(start))
    put("End Time (UTC)", pd.Timestamp(end))
    put("Origin (Lat, Lon)", _pt(origin))
    put("Destination (Lat, Lon)", _pt(origin if dest is None else dest))
    put("Distance (km)", distance)
    put("Operator", operator)
    put("Energy Change (kWh)", energy)
    return r


def trip(start, end, origin, dest, *, distance=40.0, **kw):
    return row("In Transit", start, end, origin, dest, distance=distance, **kw)


def charge(start, end, place, *, label="DC Away", energy=120.0, **kw):
    return row(label, start, end, place, place, energy=energy, **kw)


def h(day, hour, minute=0):
    """Day ``day`` (0-based from T0), ``hour``:``minute``."""
    return T0 + pd.Timedelta(days=day, hours=hour, minutes=minute)


def depot_day(day, base, customer=C, *, operator="OPX", charge_at=None, **kw):
    """base -> customer 07-08, customer -> base 12-13, a charge 14-16 at ``charge_at``."""
    rows = [
        trip(h(day, 7), h(day, 8), base, customer, operator=operator, **kw),
        trip(h(day, 12), h(day, 13), customer, base, operator=operator, **kw),
    ]
    if charge_at is not None:
        rows.append(charge(h(day, 14), h(day, 16), charge_at, operator=operator, **kw))
    return rows


def labels(rows, headers=HEADERS):
    i = _row_col_index("Leg Type", headers)
    return [r[i] for r in rows]


def base_points(bases, operator="OPX"):
    return [(round(b.lat, 4), round(b.lon, 4)) for b in bases[operator]]


# ── One depot ────────────────────────────────────────────────────────────────


def test_one_depot_is_found_from_the_overnight_stays():
    rows = [r for d in range(5) for r in depot_day(d, A, charge_at=A)]
    bases = depots.find_bases(rows)

    assert base_points(bases) == [A]
    (base,) = bases["OPX"]
    assert base.evidence == depots.EVIDENCE_OVERNIGHT
    # 4 nights between the five days + the open stays before the first trip and
    # after the last one = 6 stays, every one at A.
    assert (base.nights, base.operator_nights, base.driving_days) == (6, 6, 5)
    assert (base.charges, base.charge_kwh) == (5, 600.0)


def test_trips_and_charges_are_labelled_against_the_depot():
    rows = [r for d in range(3) for r in depot_day(d, A, charge_at=A)]
    result = depots.relabel_rows(rows)

    assert labels(rows) == ["Outbound", "Return", "DC Home"] * 3
    assert result.trips == 6 and result.charges == 3
    assert result.changes == {
        ("In Transit", "Outbound"): 3,
        ("In Transit", "Return"): 3,
        ("DC Away", "DC Home"): 3,
    }
    assert result.base_to_base == 0


def test_the_first_charge_away_from_the_depot_does_not_become_home():
    # Day 0 starts with an opportunity charge at the public charger P, the
    # first charge of the run (the old rule took it as the home point).
    rows = [
        trip(h(0, 7), h(0, 8), A, P, distance=12.0),
        charge(h(0, 8, 10), h(0, 8, 50), P),
        trip(h(0, 9), h(0, 10), P, C),
        trip(h(0, 12), h(0, 13), C, A),
        charge(h(0, 14), h(0, 16), A),
    ] + [r for d in range(1, 4) for r in depot_day(d, A, charge_at=A)]
    depots.relabel_rows(rows)

    assert base_points(depots.find_bases(rows)) == [A]
    assert labels(rows)[:5] == [
        "Outbound",
        "DC Away",  # the public charger is no base
        "In Transit",
        "Return",
        "DC Home",
    ]


def test_a_daytime_charging_site_is_no_base_however_much_it_is_used():
    # Every day a long charge at P mid-shift, and every night at A: P has more
    # sessions and more energy than A, but no overnight stay.
    rows = []
    for d in range(5):
        rows += [
            trip(h(d, 6), h(d, 7), A, P, distance=12.0),
            charge(h(d, 7, 10), h(d, 9), P, energy=250.0),
            trip(h(d, 9, 10), h(d, 10), P, C),
            trip(h(d, 12), h(d, 13), C, A),
        ]
    rows.append(charge(h(4, 14), h(4, 15), A, energy=50.0))
    depots.relabel_rows(rows)

    assert base_points(depots.find_bases(rows)) == [A]
    charge_labels = [lt for lt in labels(rows) if lt.startswith("DC")]
    assert charge_labels == ["DC Away"] * 5 + ["DC Home"]


def test_a_site_used_for_one_overnight_stay_is_no_base():
    # Nine nights at A, one night at the customer site C.
    rows = []
    for d in range(10):
        if d == 4:  # out to C in the morning, back to A only the next day
            rows.append(trip(h(d, 7), h(d, 8), A, C))
        elif d == 5:
            rows.append(trip(h(d, 7), h(d, 8), C, A))
        else:
            rows += depot_day(d, A)
    assert base_points(depots.find_bases(rows)) == [A]


# ── Several depots ───────────────────────────────────────────────────────────


def _two_depot_run():
    """Five days based at A, a transfer A -> B, then four days based at B."""
    rows = [r for d in range(5) for r in depot_day(d, A, charge_at=A)]
    rows += [
        trip(h(5, 7), h(5, 9), A, B, distance=65.0),
        trip(h(5, 10), h(5, 11), B, C),
        trip(h(5, 13), h(5, 14), C, B),
    ]
    rows += [r for d in range(6, 9) for r in depot_day(d, B, charge_at=B)]
    return rows


def test_a_vehicle_with_two_depots_has_two_bases():
    bases = depots.find_bases(_two_depot_run())
    assert sorted(base_points(bases)) == sorted([A, B])
    # A: the open stay before the first trip + 5 nights (the last one ends the
    # transfer's night); B: 3 nights + the open stay after the last trip.
    by_place = {(round(b.lat, 4), round(b.lon, 4)): b for b in bases["OPX"]}
    assert (by_place[A].nights, by_place[B].nights) == (6, 4)
    assert by_place[A].operator_nights == 10
    assert by_place[A].group != by_place[B].group  # 65 km apart


def test_a_trip_from_one_depot_to_the_other_is_a_return_and_counted():
    rows = _two_depot_run()
    result = depots.relabel_rows(rows)

    transfer = labels(rows)[15]
    assert transfer == "Return"
    assert result.base_to_base == 1
    # Trips from and to B are labelled against B like any depot.
    assert labels(rows)[16:18] == ["Outbound", "Return"]
    assert labels(rows)[-3:] == ["Outbound", "Return", "DC Home"]


def test_two_operators_in_one_run_each_keep_their_own_bases():
    # Operator X based at A for four days, then operator Y based at B.
    rows = [r for d in range(4) for r in depot_day(d, A, operator="X", charge_at=A)]
    rows += [r for d in range(4, 8) for r in depot_day(d, B, operator="Y", charge_at=B)]
    # One day Y's vehicle goes to X's depot and charges there.
    rows += [
        trip(h(8, 7), h(8, 9), B, A, operator="Y", distance=65.0),
        charge(h(8, 9, 10), h(8, 10), A, operator="Y"),
        trip(h(8, 11), h(8, 13), A, B, operator="Y", distance=65.0),
    ]
    bases = depots.find_bases(rows)
    assert base_points(bases, "X") == [A]
    assert base_points(bases, "Y") == [B]

    depots.relabel_rows(rows)
    assert labels(rows)[-3:] == ["Outbound", "DC Away", "Return"]


def test_rows_without_an_operator_belong_to_their_neighbours():
    rows = [r for d in range(4) for r in depot_day(d, A, operator="X", charge_at=A)]
    i_op = _row_col_index("Operator", HEADERS)
    rows[4][i_op] = None  # a trip of day 1 with no operator resolved
    rows[5][i_op] = ""  # its charge, blank as read back from a workbook
    bases = depots.find_bases(rows)
    assert set(bases) == {"X"}
    depots.relabel_rows(rows)
    assert labels(rows)[3:6] == ["Outbound", "Return", "DC Home"]


def test_a_run_without_operators_is_one_stint():
    rows = [r for d in range(3) for r in depot_day(d, A, operator=None, charge_at=A)]
    bases = depots.find_bases(rows)
    assert base_points(bases, None) == [A]


# ── Sparse positions near a depot ────────────────────────────────────────────


def test_a_second_cluster_just_outside_the_depot_is_the_same_place():
    # The night's last trip ends at A, but the morning's first trip is placed
    # 1.2 km north (its first sample taken after setting off). Both clusters
    # become bases in one group, so a hop between them is In House.
    a2 = north(A, 1.2)
    rows = []
    for d in range(6):
        rows += [
            trip(h(d, 7), h(d, 8), a2, C),
            trip(h(d, 12), h(d, 13), C, A),
        ]
    rows.append(trip(h(6, 7), h(6, 7, 10), a2, A, distance=1.3))
    bases = depots.find_bases(rows)
    assert sorted(base_points(bases)) == sorted([(round(a2[0], 4), a2[1]), A])
    assert len({b.group for b in bases["OPX"]}) == 1

    result = depots.relabel_rows(rows)
    assert labels(rows)[:2] == ["Outbound", "Return"]
    assert labels(rows)[-1] == "In House"
    assert result.base_to_base == 0


# ── The thresholds ───────────────────────────────────────────────────────────


def _nights_at(sites_by_night):
    """One day per entry: from the entry's site out to D and back; when the
    next entry differs, a transfer trip moves the vehicle there for the night.
    So the night after day ``d`` is spent at entry ``d + 1``'s site."""
    rows = []
    for d, site in enumerate(sites_by_night):
        rows += [
            trip(h(d, 7), h(d, 8), site, D),
            trip(h(d, 12), h(d, 13), D, site),
        ]
        nxt = sites_by_night[d + 1] if d + 1 < len(sites_by_night) else None
        if nxt is not None and nxt != site:
            rows.append(trip(h(d, 14), h(d, 15), site, nxt))
    return rows


def test_a_site_under_a_fifth_of_the_nights_is_no_base():
    # Ten days: nights after days 3 and 4 at C, the other seven at A. With the
    # two open stays that is 11 stays; C's 2 are under 0.2 x 11 = 2.2.
    sites = [A] * 4 + [C, C] + [A] * 4
    result = depots.assign_leg_types(_nights_at(sites))
    assert base_points(result.bases) == [A]
    rejected = result.summary()["rejected_sites"]["OPX"]
    assert [
        (r["lat"], r["lon"], r["nights"], r["operator_nights"]) for r in rejected
    ] == [(*C, 2, 11)]


def test_a_site_with_a_fifth_of_the_nights_is_a_base():
    # Three nights at C out of 11 stays (>= 2.2), and 3 >= 0.1 x 10 driving days.
    sites = [A] * 4 + [C, C, C] + [A] * 3
    assert sorted(base_points(depots.find_bases(_nights_at(sites)))) == sorted([A, C])


def _shuttle(n_trips, rest_after, *, start=A, other=C):
    """A continuous start <-> other shuttle (1 h trips, 1 h apart) with a 10 h
    rest after each trip index in ``rest_after``: a double-shifted vehicle
    whose long rests are few."""
    rows, t, at = [], T0 + pd.Timedelta(hours=6), start
    for k in range(n_trips):
        dest = other if at == start else start
        rows.append(trip(t, t + pd.Timedelta(hours=1), at, dest, distance=2.5))
        at = dest
        t += pd.Timedelta(hours=11 if k in rest_after else 2)
    return rows


def test_a_rarely_used_overnight_site_of_a_double_shifted_vehicle_is_no_base():
    # Trip k ends at the other site (C) for even k and back at A for odd k.
    # Long rests: 7 at A, 3 at C -> with the open stays 12 stays, C = 3 (a
    # quarter), but over ~40 driving days 3 < 0.1 x 40.
    rest_after = {1, 41, 81, 121, 161, 201, 241, 20, 100, 180}
    rows = _shuttle(480, rest_after)
    days = len(
        {pd.Timestamp(r[_row_col_index("Start Time (UTC)")]).date() for r in rows}
    )
    assert days >= 40
    assert base_points(depots.find_bases(rows)) == [A]


def test_the_same_site_over_fewer_driving_days_is_a_base():
    # The same rests over a shorter run: 3 >= 0.1 x ~22 driving days.
    rest_after = {1, 41, 81, 121, 161, 201, 241, 20, 100, 180}
    rows = _shuttle(250, rest_after)
    assert sorted(base_points(depots.find_bases(rows))) == sorted([A, C])


def test_a_rest_under_six_hours_is_not_an_overnight_stay():
    # Every night the vehicle waits 5 h at C (a night delivery), and rests
    # properly (18 h) at A only at the end: C is never a base.
    rows = []
    for d in range(6):
        rows += [
            trip(h(d, 1), h(d, 2), A, C),
            trip(h(d, 7), h(d, 8), C, A),  # 5 h at C
            trip(h(d, 9), h(d, 10), A, D),
            trip(h(d, 12), h(d, 13), D, A),
        ]
    bases = depots.find_bases(rows)
    assert base_points(bases) == [A]


# ── Fallbacks when no site has overnight support ─────────────────────────────


def test_open_stays_alone_find_the_depot_of_a_single_day():
    rows = [
        trip(h(0, 7), h(0, 8), A, C),
        trip(h(0, 9), h(0, 10), C, D),
        trip(h(0, 11), h(0, 12), D, A),
    ]
    result = depots.relabel_rows(rows)
    (base,) = result.bases["OPX"]
    assert base.evidence == depots.EVIDENCE_OVERNIGHT
    assert (base.nights, base.operator_nights) == (2, 2)
    assert labels(rows) == ["Outbound", "In Transit", "Return"]


def test_without_overnight_support_the_most_used_charge_site_is_the_base():
    # The day starts at C and ends at E; the vehicle charges twice at A.
    rows = [
        trip(h(0, 7), h(0, 8), C, A),
        charge(h(0, 8, 10), h(0, 9), A),
        trip(h(0, 9, 10), h(0, 10), A, D),
        trip(h(0, 11), h(0, 12), D, A),
        charge(h(0, 12, 10), h(0, 13), A),
        trip(h(0, 13, 10), h(0, 14), A, E),
    ]
    result = depots.relabel_rows(rows)
    (base,) = result.bases["OPX"]
    assert base.evidence == depots.EVIDENCE_CHARGES
    assert (base.charges, base.charge_kwh) == (2, 240.0)
    assert labels(rows) == [
        "Return",
        "DC Home",
        "Outbound",
        "Return",
        "DC Home",
        "Outbound",
    ]


def test_without_charges_the_most_frequent_trip_end_is_the_base():
    rows = [
        trip(h(0, 7), h(0, 8), C, A, headers=DIESEL_HEADERS),
        trip(h(0, 9), h(0, 10), A, D, headers=DIESEL_HEADERS),
        trip(h(0, 11), h(0, 12), D, A, headers=DIESEL_HEADERS),
        trip(h(0, 13), h(0, 14), A, E, headers=DIESEL_HEADERS),
    ]
    result = depots.relabel_rows(rows, DIESEL_HEADERS)
    (base,) = result.bases["OPX"]
    assert base.evidence == depots.EVIDENCE_TRIP_ENDS
    assert labels(rows, DIESEL_HEADERS) == ["Return", "Outbound", "Return", "Outbound"]


def test_a_single_trip_between_two_places_has_no_base():
    rows = [trip(h(0, 7), h(0, 9), C, D)]
    result = depots.relabel_rows(rows)
    assert result.bases == {"OPX": ()}
    assert labels(rows) == ["In Transit"]
    assert depots.describe_bases(result.bases) == ["operator OPX: no base"]


def test_a_charge_only_run_takes_its_charge_site_as_the_base():
    rows = [charge(h(d, 20), h(d, 23), A) for d in range(3)]
    result = depots.relabel_rows(rows)
    assert result.bases["OPX"][0].evidence == depots.EVIDENCE_CHARGES
    assert labels(rows) == ["DC Home"] * 3


# ── Diesel ───────────────────────────────────────────────────────────────────


def test_diesel_trips_are_labelled_by_the_same_rule():
    rows = [r for d in range(5) for r in depot_day(d, A, headers=DIESEL_HEADERS)]
    rows.append(trip(h(5, 7), h(5, 9), A, A, distance=85.0, headers=DIESEL_HEADERS))
    rows.append(
        trip(h(5, 10), h(5, 10, 20), A, A, distance=3.0, headers=DIESEL_HEADERS)
    )
    rows.append(trip(h(5, 11), h(5, 12), C, D, headers=DIESEL_HEADERS))
    result = depots.relabel_rows(rows, DIESEL_HEADERS)

    assert base_points(result.bases) == [A]
    assert labels(rows, DIESEL_HEADERS) == ["Outbound", "Return"] * 5 + [
        "Round Trip",
        "In House",
        "In Transit",
    ]


# ── The label rules ──────────────────────────────────────────────────────────


def _base(group=0):
    return depots.Base(
        operator="OPX",
        lat=A[0],
        lon=A[1],
        evidence=depots.EVIDENCE_OVERNIGHT,
        nights=5,
        operator_nights=5,
        driving_days=5,
        charges=0,
        charge_kwh=0.0,
        group=group,
    )


@pytest.mark.parametrize(
    "distance, expected",
    [
        (0.0, "In House"),
        (4.9, "In House"),
        (5.0, "In House"),  # "over 5 km" is a Round Trip
        (5.1, "Round Trip"),
        (120.0, "Round Trip"),
        (NAN, "In House"),  # no distance: a short move
    ],
)
def test_same_base_at_both_ends(distance, expected):
    assert depots.trip_leg_type(_base(), _base(), distance) == expected


def test_bases_of_one_group_count_as_the_same_base():
    assert (
        depots.trip_leg_type(_base(0), replace_group(_base(0), 0), 12.0) == "Round Trip"
    )


def replace_group(base, group):
    from dataclasses import replace

    return replace(base, lat=base.lat + 0.01, group=group)


def test_two_different_bases_make_a_return():
    assert depots.trip_leg_type(_base(0), replace_group(_base(0), 1), 65.0) == "Return"


@pytest.mark.parametrize(
    "start, end, expected",
    [
        (True, False, "Outbound"),
        (False, True, "Return"),
        (False, False, "In Transit"),
    ],
)
def test_one_end_or_none_at_a_base(start, end, expected):
    s = _base() if start else None
    e = _base() if end else None
    assert depots.trip_leg_type(s, e, 30.0) == expected


@pytest.mark.parametrize(
    "label, at_base, expected",
    [
        ("AC Away", True, "AC Home"),
        ("DC Away", True, "DC Home"),
        ("AC/DC Home", False, "AC/DC Away"),
        ("Charge Away", True, "Charge Home"),
        ("Charge Home", True, "Charge Home"),
        ("dc away", True, "dc Home"),
        ("Mix", True, "Mix Home"),  # a legacy label without a place word
    ],
)
def test_charge_labels_keep_their_kind(label, at_base, expected):
    assert depots.charge_leg_type(label, at_base) == expected


# ── The second pass ──────────────────────────────────────────────────────────


def test_relabelling_twice_changes_nothing_the_second_time():
    rows = _two_depot_run()
    first = depots.relabel_rows(rows)
    second = depots.relabel_rows(rows)
    assert first.n_changed > 0
    assert second.n_changed == 0
    assert second.labels == first.labels


def test_the_labels_do_not_depend_on_the_labels_the_rows_carry():
    plain = _two_depot_run()
    scrambled = _two_depot_run()
    i = _row_col_index("Leg Type", HEADERS)
    for k, r in enumerate(scrambled):
        if r[i] == "In Transit":
            r[i] = ("Outbound", "Return", "In House", "Round Trip")[k % 4]
        else:
            r[i] = "DC Home" if k % 2 else "DC Away"
    depots.relabel_rows(plain)
    depots.relabel_rows(scrambled)
    assert labels(scrambled) == labels(plain)


def test_the_row_order_does_not_matter():
    rows = _two_depot_run()
    shuffled = [list(r) for r in reversed(rows)]
    depots.relabel_rows(rows)
    depots.relabel_rows(shuffled)
    assert labels(shuffled) == list(reversed(labels(rows)))


def test_values_as_read_back_from_a_workbook_give_the_same_labels():
    rows = _two_depot_run()
    read_back = []
    for r in rows:
        r2 = list(r)
        for name in ("Start Time (UTC)", "End Time (UTC)"):
            i = _row_col_index(name, HEADERS)
            # A naive datetime with the sub-second noise of an Excel serial date.
            r2[i] = r[i].to_pydatetime().replace(microsecond=400)
        i_dist = _row_col_index("Distance (km)", HEADERS)
        if math.isnan(r2[i_dist]):
            r2[i_dist] = "=NA()"
        read_back.append(r2)
    aware = []
    for r in rows:
        r3 = list(r)
        for name in ("Start Time (UTC)", "End Time (UTC)"):
            i = _row_col_index(name, HEADERS)
            r3[i] = r[i].tz_localize("UTC").tz_convert("Europe/London")
        aware.append(r3)
    expected = labels([list(r) for r in rows])
    depots.relabel_rows(rows)
    depots.relabel_rows(read_back)
    depots.relabel_rows(aware)
    assert labels(read_back) == labels(rows) == labels(aware)
    assert labels(rows) != expected  # the provisional labels did change


def test_stop_rows_and_rows_of_no_known_kind_are_left_alone():
    rows = [r for d in range(3) for r in depot_day(d, A, charge_at=A)]
    # A long Stop at C must not make C a base; a blank label stays blank.
    rows.append(row("Stop", h(1, 16), h(2, 7), C))
    rows.append(row(None, h(2, 20), h(2, 21), C))
    rows.append(row("", h(2, 22), h(2, 23), C))
    depots.relabel_rows(rows)
    assert base_points(depots.find_bases(rows)) == [A]
    assert labels(rows)[-3:] == ["Stop", None, ""]


@pytest.mark.parametrize(
    "unusable", ["=NA()", "Point(0.000000 0.000000)", "Point(nan nan)", None, NAN, 7]
)
def test_an_unusable_position_counts_as_no_position(unusable):
    rows = [r for d in range(3) for r in depot_day(d, A, charge_at=A)]
    i_o = _row_col_index("Origin (Lat, Lon)", HEADERS)
    i_d = _row_col_index("Destination (Lat, Lon)", HEADERS)
    rows[3][i_o] = unusable  # day 1's first trip, A -> C
    rows[5][i_o] = rows[5][i_d] = unusable  # day 1's charge at A
    depots.relabel_rows(rows)
    assert base_points(depots.find_bases(rows)) == [A]
    assert labels(rows)[3:6] == ["In Transit", "Return", "DC Away"]


def test_a_missing_required_column_is_an_error():
    headers = tuple(h for h in HEADERS if h != "Origin (Lat, Lon)")
    with pytest.raises(ValueError, match="Origin"):
        depots.assign_leg_types([], headers)
    with pytest.raises(ValueError, match="Leg Number"):
        depots.assign_leg_types([], HEADERS[1:])


def test_assign_leg_types_does_not_modify_the_rows():
    rows = [r for d in range(3) for r in depot_day(d, A, charge_at=A)]
    before = [list(r) for r in rows]
    result = depots.assign_leg_types(rows)
    assert labels(rows) == labels(before) == ["In Transit", "In Transit", "DC Away"] * 3
    assert result.labels[:3] == ["Outbound", "Return", "DC Home"]


def test_given_bases_are_used_instead_of_finding_them():
    rows = [r for d in range(3) for r in depot_day(d, A, charge_at=A)]
    elsewhere = {"OPX": (replace_group(_base(), 0),)}  # ~1.1 km north of A
    result = depots.assign_leg_types(rows, bases=elsewhere)
    assert result.labels[:3] == ["In Transit", "In Transit", "DC Away"]


def test_the_summary_is_json_ready():
    result = depots.assign_leg_types(_two_depot_run())
    summary = json.loads(json.dumps(result.summary()))
    assert summary["base_to_base"] == 1
    assert summary["changed"] == result.n_changed
    assert {b["evidence"] for b in summary["bases"]["OPX"]} == {"overnight"}
    assert set(summary["bases"]["OPX"][0]) == {
        "operator",
        "lat",
        "lon",
        "evidence",
        "nights",
        "operator_nights",
        "driving_days",
        "charges",
        "charge_kwh",
        "group",
    }
    assert "In Transit -> Outbound" in summary["changes"]


def test_describe_bases_takes_bases_or_their_dict_form():
    result = depots.assign_leg_types(_two_depot_run())
    from_objects = depots.describe_bases(result.bases)
    from_dicts = depots.describe_bases(result.summary()["bases"])
    assert from_objects == from_dicts
    assert from_objects[0].startswith("operator OPX: (52.0000, -1.0000) from overnight")


# ── The glossary entry ───────────────────────────────────────────────────────


def test_the_glossary_names_every_label_and_the_thresholds():
    ev = depots.leg_type_definition()
    diesel = depots.leg_type_definition(diesel=True)
    for text in (ev, diesel):
        for label in depots.TRIP_LEG_TYPES + ("Stop",):
            assert f'"{label}"' in text
        assert "5 km" in text and "6 h" in text and "0.5 km" in text
    assert '"Home"' in ev and '"Away"' in ev and "charges most" in ev
    assert "no charging events" in diesel and "charges most" not in diesel


def test_timestamps_parse_the_forms_a_row_can_hold():
    assert depots._parse_time(datetime(2026, 3, 2, 7, 0, 0, 499999)) == pd.Timestamp(
        "2026-03-02 07:00:00"
    )
    assert depots._parse_time(pd.Timestamp("2026-03-02 08:00+01:00")) == pd.Timestamp(
        "2026-03-02 07:00"
    )
    for blank in (None, NAN, "=NA()", "", pd.NaT):
        assert depots._parse_time(blank) is None
