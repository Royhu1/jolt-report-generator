"""The charge / trip boundary reconciliation, on synthetic segments.

``_reconcile_charge_boundaries`` clamps a charge that overlaps a trip: a trip
starting inside the charge moves the charge's end to the trip's start, a trip
ending inside it moves the charge's start to the trip's end. Only the time axis
changes; a charge whose overlap is not a boundary error (a trip wholly inside
it, or it wholly inside a trip), or which the clamps would leave without
duration, is left whole with a warning.
"""

from __future__ import annotations

import copy
import logging

import pandas as pd
import pytest

from report_generator.segmentation.detection import _reconcile_charge_boundaries

DAY = "2026-07-25"


def _t(hms: str, aware: bool = True) -> pd.Timestamp:
    ts = pd.Timestamp(f"{DAY}T{hms}")
    return ts.tz_localize("UTC") if aware else ts


def _charge(start: str, end: str, aware: bool = True) -> dict:
    return {
        "start_time": _t(start, aware),
        "end_time": _t(end, aware),
        "start_soc": 42.0,
        "end_soc": 99.0,
        "delta_soc_pct": 57.0,
        "delta_energy_kwh": 290.7,
        "energy_source": "soc_estimate",
        "charge_type": "estimated",
        "_anchor_start_time": _t(start, aware),
        "_anchor_end_time": _t(end, aware),
    }


def _trip(start: str, end: str, aware: bool = False) -> dict:
    return {"start_time": _t(start, aware), "end_time": _t(end, aware)}


def _run(charges, trips):
    return _reconcile_charge_boundaries(charges, trips, "UTVEH01", "test")


# ── The two directions ───────────────────────────────────────────────────────


def test_a_trip_starting_inside_a_charge_moves_its_end_to_the_trip_start():
    charge = _charge("09:30:16", "15:03:52")  # ends at a sample taken while driving
    before = copy.deepcopy(charge)
    trips = [_trip("15:02:23", "17:09:18")]

    assert _run([charge], trips) == 1

    assert charge["end_time"] == _t("15:02:23")
    assert charge["start_time"] == before["start_time"]
    # The observation is untouched: SOC, energy, its source and its anchors.
    unchanged = {k: v for k, v in charge.items() if k != "end_time"}
    assert unchanged == {k: v for k, v in before.items() if k != "end_time"}
    assert trips == [_trip("15:02:23", "17:09:18")]  # the trip wins, unchanged


def test_a_trip_ending_inside_a_charge_moves_its_start_to_the_trip_end():
    charge = _charge("11:30:00", "14:00:00")
    assert _run([charge], [_trip("09:15:00", "11:37:36")]) == 1
    assert charge["start_time"] == _t("11:37:36")
    assert charge["end_time"] == _t("14:00:00")


def test_a_charge_between_two_overlapping_trips_is_clamped_on_both_sides():
    charge = _charge("11:30:00", "15:03:52")
    trips = [_trip("15:02:23", "17:09:18"), _trip("09:15:00", "11:37:36")]
    assert _run([charge], trips) == 2
    assert (charge["start_time"], charge["end_time"]) == (
        _t("11:37:36"),
        _t("15:02:23"),
    )


def test_the_order_of_the_trips_does_not_matter():
    trips = [_trip("15:02:23", "17:09:18"), _trip("09:15:00", "11:37:36")]
    first, second = _charge("11:30:00", "15:03:52"), _charge("11:30:00", "15:03:52")
    _run([first], trips)
    _run([second], list(reversed(trips)))
    assert first == second


def test_each_charge_is_reconciled_on_its_own():
    clamped, clear = _charge("08:50:04", "09:21:40"), _charge("12:00:00", "13:00:00")
    trips = [_trip("09:19:19", "11:37:36"), _trip("13:30:00", "14:00:00")]
    assert _run([clamped, clear], trips) == 1
    assert clamped["end_time"] == _t("09:19:19")
    assert clear == _charge("12:00:00", "13:00:00")


# ── Nothing to do ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "trip",
    [
        _trip("13:00:00", "14:00:00"),  # touches the charge's end: no overlap
        _trip("09:00:00", "10:00:00"),  # touches its start
        _trip("15:00:00", "16:00:00"),  # clear of it
    ],
    ids=["touching the end", "touching the start", "disjoint"],
)
def test_a_trip_that_does_not_overlap_changes_nothing(trip):
    charge = _charge("10:00:00", "13:00:00")
    assert _run([charge], [trip]) == 0
    assert charge == _charge("10:00:00", "13:00:00")


def test_no_trips_and_no_charges_are_fine():
    assert _run([], [_trip("09:00:00", "10:00:00")]) == 0
    charge = _charge("10:00:00", "13:00:00")
    assert _run([charge], []) == 0
    assert charge == _charge("10:00:00", "13:00:00")


# ── Overlaps that are not a boundary error ───────────────────────────────────


@pytest.mark.parametrize(
    "trips, reason",
    [
        ([_trip("11:00:00", "12:00:00")], "lies wholly inside it"),
        ([_trip("09:00:00", "14:00:00")], "it lies wholly inside the trip"),
        ([_trip("10:00:00", "13:00:00")], "it lies wholly inside the trip"),  # same
        # The clamps meet: the trips would leave the charge no time at all ...
        (
            [_trip("09:00:00", "11:30:00"), _trip("11:30:00", "14:00:00")],
            "would leave no duration",
        ),
        # ... or less than none.
        (
            [_trip("09:00:00", "12:00:00"), _trip("11:00:00", "14:00:00")],
            "would leave no duration",
        ),
    ],
    ids=[
        "trip inside the charge",
        "charge inside a trip",
        "identical windows",
        "zero duration",
        "negative duration",
    ],
)
def test_an_overlap_that_clamping_cannot_resolve_is_left_with_a_warning(
    caplog, trips, reason
):
    charge = _charge("10:00:00", "13:00:00")
    with caplog.at_level(logging.WARNING, logger="report_generator.segmentation"):
        assert _run([charge], trips) == 0
    assert charge == _charge("10:00:00", "13:00:00")
    assert "left unchanged" in caplog.text
    assert reason in caplog.text
    assert "UTVEH01" in caplog.text


# ── Time zones ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "charge_aware, trip_aware",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_the_clamped_time_keeps_the_charges_own_form(charge_aware, trip_aware):
    charge = _charge("09:30:16", "15:03:52", aware=charge_aware)
    trip = _trip("15:02:23", "17:09:18", aware=trip_aware)
    assert _run([charge], [trip]) == 1
    end = charge["end_time"]
    assert (end.tzinfo is not None) == charge_aware
    expected = _t("15:02:23")  # the same instant, UTC
    assert (end if end.tzinfo else end.tz_localize("UTC")) == expected
