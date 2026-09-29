"""Speed trips kept on the odometer's word (``keep_odometer_confirmed_trips``).

``find_discharge_segments_by_speed`` drops a speed trip whose SOC did not fall by
``min_soc_drop`` or whose energy could not be measured (``min_energy_kwh``). On a
sparse feed that loses real driving: the SOC freezes while the vehicle drives, or
a charge taken while the telematics were silent surfaces as a jump of the SOC
after the vehicle has set off, so the SOC rises across the trip. With the
``speed_params`` key on, such a trip is kept when the odometer confirms it
(``min_confirmed_distance_km``, default 0.5 km): it is first cut at every charge
it overlaps; where the SOC rose across it the parts are measured again, each from
its own first SOC reading — so after the jump — and otherwise each part is kept
distance-only, with no energy, no capacity and ``energy_source``
``"distance_only"``. Nothing changes without the key.

The frames are synthetic: one leg sampled every minute from 08:00, the vehicle
moving at 48 km/h from 08:06 to 08:50, so the zero-speed trip window is
08:05 - 08:51 and every odometer distance is a hand computation (0.8 km a minute).
"""

from __future__ import annotations

import copy
import datetime
import logging

import numpy as np
import openpyxl
import pandas as pd
import pytest

from report_generator import capacity_backfill
from report_generator._generator import JOLTReportGenerator
from report_generator.capacity import (
    _IDX_CAP,
    _IDX_ENERGY,
    _IDX_ESOURCE,
    _IDX_SOC_CHANGE,
    _IDX_START,
    _period_capacity_from_rows,
)
from report_generator.columns import DISTANCE_ONLY_SOURCE, HEADERS, _row_col_index
from report_generator.ep_confidence import CODE_NO_ENERGY, assess_ep_confidence
from report_generator.report_builder import _seg_to_row, _write_excel_report
from report_generator.segmentation import constants, detection
from report_generator.segmentation.constants import _ODOMETER_CONFIRMED_KEY
from report_generator.segmentation.detection import run_segment_detection
from report_generator.segmentation.mass_clustering import (
    _enforce_anchor_ordering,
    merge_discharge_by_mass,
    split_discharge_by_mass,
)
from report_generator.segmentation.soc_detection import find_charge_segments_by_soc
from report_generator.segmentation.speed_detection import (
    DEFAULT_MIN_CONFIRMED_DISTANCE_KM,
    _charge_windows,
    _parts_outside_charges,
    find_discharge_segments_by_speed,
)

TIME = constants.TIME_COL
SOC = constants.SOC_COL
ODO = constants.ODO_COL
SPEED = "wheel_based_speed"
MOV = "electric_energy_wheelbased_speed_over_zero"
TOT = "total_electric_energy_used_plugged_in_included"
MASS = "gross_combination_vehicle_weight"
DAY = "2026-08-12"
T0 = pd.Timestamp(f"{DAY} 08:00:00", tz="UTC")
NOMINAL = 400.0
CAP_LO, CAP_HI = NOMINAL * 0.5, NOMINAL * 2.0
KM_PER_MIN = 0.8  # 48 km/h


def _at(minute: float) -> pd.Timestamp:
    """08:00 + ``minute`` minutes, naive UTC: the form speed trips come in."""
    return (T0 + pd.Timedelta(minutes=minute)).tz_convert(None)


def _odo(minute: float) -> float:
    """The odometer at a minute: 0.8 km for each minute moved from 08:06 on."""
    return 10_000.0 + KM_PER_MIN * float(np.clip(minute - 5.0, 0.0, 45.0))


def _frame(soc, extra: tuple = (), counters: dict | None = None) -> pd.DataFrame:
    """One leg, 08:00 - 09:00 every minute (plus ``extra`` minutes), as strings.

    ``soc(minute)`` gives the SOC at a sample. ``counters`` maps an energy
    counter's column to its value (Wh) as a function of the minute.
    """
    minutes = sorted(set(float(m) for m in range(61)) | set(extra))
    moving = [6.0 <= m <= 50.0 for m in minutes]
    frame = pd.DataFrame(
        {
            TIME: [(T0 + pd.Timedelta(minutes=m)).isoformat() for m in minutes],
            SOC: [soc(m) for m in minutes],
            SPEED: [48.0 if mv else 0.0 for mv in moving],
            ODO: [_odo(m) for m in minutes],
        }
    )
    for col, fn in (counters or {}).items():
        frame[col] = [fn(m) for m in minutes]
    return frame.astype(str)


def _frozen(minute: float) -> float:
    return 80.0


def _jump(minute: float) -> float:
    """A stale 40 % until 08:10, then 90 % at 08:11 falling to 80 % by 08:51."""
    if minute <= 10.0:
        return 40.0
    return round(90.0 - 10.0 * (min(minute, 51.0) - 11.0) / 40.0, 1)


def _charges(frame) -> list[dict]:
    return find_charge_segments_by_soc(
        frame,
        plateau_window_min=60,
        min_soc_rise=5.0,
        min_energy_kwh=5.0,
        cap_lo=CAP_LO,
        cap_hi=CAP_HI,
        nominal_kwh=NOMINAL,
    )


def _detect(frame, charges=None, **kwargs):
    params = dict(
        speed_col=SPEED,
        speed_threshold_kmh=1.0,
        min_stop_duration_min=5.0,
        min_trip_duration_min=2.0,
        min_soc_drop=1.0,
        min_energy_kwh=1.0,
        cap_lo=CAP_LO,
        cap_hi=CAP_HI,
        total_energy_col=TOT,
        moving_energy_col=MOV,
        nominal_kwh=NOMINAL,
        charge_segs=charges,
    )
    params.update(kwargs)
    return find_discharge_segments_by_speed(frame, **params)


def _km(seg) -> float:
    return seg["odo_end_km"] - seg["odo_start_km"]


# ── Without the key nothing changes ──────────────────────────────────────────


def test_by_default_a_trip_with_a_frozen_soc_is_dropped():
    assert _detect(_frame(_frozen)) == []


@pytest.mark.parametrize("value", [False, "yes", 1, "true"], ids=str)
def test_only_true_switches_the_key_on(value):
    assert _detect(_frame(_frozen), keep_odometer_confirmed_trips=value) == []


def test_a_trip_that_passes_the_floors_is_the_same_with_the_key(serialise):
    def falling(minute):
        return round(80.0 - 10.0 * float(np.clip(minute - 5.0, 0.0, 46.0)) / 46.0, 1)

    frame = _frame(falling)
    off = _detect(frame)
    on = _detect(frame, _charges(frame), keep_odometer_confirmed_trips=True)
    assert len(off) == 1 and off[0]["energy_source"] == "soc_estimate"
    assert serialise(on) == serialise(off)
    assert _ODOMETER_CONFIRMED_KEY not in on[0]


def test_without_the_key_an_invalid_threshold_is_never_read():
    assert _detect(_frame(_frozen), min_confirmed_distance_km="far") == []


# ── A frozen SOC: the trip is kept with its distance only ────────────────────


def test_a_frozen_soc_trip_is_kept_distance_only():
    (trip,) = _detect(_frame(_frozen), keep_odometer_confirmed_trips=True)
    assert trip["energy_source"] == DISTANCE_ONLY_SOURCE
    assert np.isnan(trip["delta_energy_kwh"])
    assert trip["effective_capacity_kwh"] is None
    # Everything else is measured as for any trip.
    assert trip["start_time"] == _at(5) and trip["end_time"] == _at(51)
    assert _km(trip) == pytest.approx(36.0)
    assert (trip["start_soc"], trip["end_soc"], trip["delta_soc_pct"]) == (
        80.0,
        80.0,
        0.0,
    )
    # No energy anchors: nothing measured an energy.
    assert trip["_anchor_start_time"] is None and trip["_anchor_end_time"] is None
    assert np.isnan(trip["_anchor_start_rel_kwh"])
    assert np.isnan(trip["_anchor_end_rel_kwh"])
    assert trip[_ODOMETER_CONFIRMED_KEY] is True


def test_a_drop_below_the_floor_is_kept_distance_only():
    def barely(minute):
        return 80.0 if minute < 30 else 79.6  # 0.4 points: below the 1-point floor

    (trip,) = _detect(_frame(barely), keep_odometer_confirmed_trips=True)
    assert trip["energy_source"] == DISTANCE_ONLY_SOURCE
    assert trip["delta_soc_pct"] == pytest.approx(-0.4)
    # A lower floor measures the same trip: the option does not stand in the way.
    (measured,) = _detect(
        _frame(barely), keep_odometer_confirmed_trips=True, min_soc_drop=0.4
    )
    assert measured["energy_source"] == "soc_estimate"
    assert measured["delta_energy_kwh"] == pytest.approx(-0.4 / 100 * NOMINAL)


def test_a_rise_no_charge_accounts_for_is_kept_distance_only():
    def small_rise(minute):
        return 80.0 if minute < 20 else 82.0  # 2 points: no charge (5-point floor)

    frame = _frame(small_rise)
    assert _charges(frame) == []
    (trip,) = _detect(frame, _charges(frame), keep_odometer_confirmed_trips=True)
    assert trip["energy_source"] == DISTANCE_ONLY_SOURCE
    assert trip["delta_soc_pct"] == pytest.approx(2.0)
    assert trip["start_time"] == _at(5) and trip["end_time"] == _at(51)


def test_a_trip_without_any_soc_reading_is_kept_distance_only():
    frame = _frame(_frozen)
    frame[SOC] = ""
    (trip,) = _detect(frame, keep_odometer_confirmed_trips=True)
    assert trip["energy_source"] == DISTANCE_ONLY_SOURCE
    assert np.isnan(trip["start_soc"]) and np.isnan(trip["delta_soc_pct"])


def test_a_counter_feed_whose_soc_floor_rejects_the_trip_keeps_it_distance_only():
    # The SOC floor is applied before the energy cascade, so the counter is not
    # read: the key keeps the trip, not an energy the floor rejected.
    frame = _frame(_frozen, counters={MOV: lambda m: 5e6 + 400.0 * m})
    (trip,) = _detect(frame, keep_odometer_confirmed_trips=True)
    assert trip["energy_source"] == DISTANCE_ONLY_SOURCE
    assert np.isnan(trip["delta_energy_kwh"])


# ── The odometer threshold ───────────────────────────────────────────────────


def test_the_default_threshold_is_half_a_kilometre():
    assert DEFAULT_MIN_CONFIRMED_DISTANCE_KM == 0.5


@pytest.mark.parametrize(
    "threshold, kept",
    [(35.9, True), (36.0, True), (36.1, False)],
    ids=["below", "equal", "above"],
)
def test_the_odometer_must_cover_the_threshold(threshold, kept):
    trips = _detect(
        _frame(_frozen),
        keep_odometer_confirmed_trips=True,
        min_confirmed_distance_km=threshold,
    )
    assert bool(trips) is kept


def test_a_trip_the_odometer_does_not_confirm_stays_dropped():
    frame = _frame(_frozen)
    frame[ODO] = "10000.0"  # the vehicle "drove" without the odometer moving
    assert _detect(frame, keep_odometer_confirmed_trips=True) == []


def test_a_trip_without_odometer_readings_stays_dropped():
    frame = _frame(_frozen).drop(columns=[ODO])
    assert _detect(frame, keep_odometer_confirmed_trips=True) == []


@pytest.mark.parametrize("value", [0, -0.5, "0.5", True, None], ids=str)
def test_an_invalid_threshold_is_refused_at_run_time(value):
    with pytest.raises(
        ValueError, match="min_confirmed_distance_km must be a positive"
    ):
        _detect(
            _frame(_frozen),
            keep_odometer_confirmed_trips=True,
            min_confirmed_distance_km=value,
        )


# ── The SOC rose across the trip: a charge surfaced late ─────────────────────


def test_the_jump_charge_lies_inside_the_trip_window():
    frame = _frame(_jump)
    (charge,) = _charges(frame)
    assert (charge["start_soc"], charge["end_soc"]) == (40.0, 90.0)
    assert charge["start_time"] == _at(10).tz_localize("UTC")
    assert charge["end_time"] == _at(11).tz_localize("UTC")
    assert _detect(frame, [charge]) == []  # the trip is lost without the key


def test_after_the_jump_the_trip_is_measured_again_from_the_charge_end():
    frame = _frame(_jump)
    charges = _charges(frame)
    before, after = _detect(frame, charges, keep_odometer_confirmed_trips=True)
    # The trip now starts where the charge ends, on the reading past the jump.
    assert after["start_time"] == _at(11) and after["end_time"] == _at(51)
    assert (after["start_soc"], after["end_soc"]) == (90.0, 80.0)
    assert after["energy_source"] == "soc_estimate"
    assert after["delta_energy_kwh"] == pytest.approx(-0.10 * NOMINAL)
    assert after["effective_capacity_kwh"] == pytest.approx(NOMINAL)
    assert _km(after) == pytest.approx(_odo(51) - _odo(11))
    # The driving before the charge, on the stale SOC, is kept with its distance.
    assert before["energy_source"] == DISTANCE_ONLY_SOURCE
    assert before["start_time"] == _at(5) and before["end_time"] == _at(10)
    assert _km(before) == pytest.approx(_odo(10) - _odo(5))
    # Nothing overlaps the charge.
    (charge,) = charges
    assert before["end_time"] <= charge["start_time"].tz_convert(None)
    assert after["start_time"] >= charge["end_time"].tz_convert(None)


def test_a_part_before_the_charge_below_the_threshold_is_dropped():
    frame = _frame(_jump)
    (after,) = _detect(
        frame,
        _charges(frame),
        keep_odometer_confirmed_trips=True,
        min_confirmed_distance_km=5.0,  # the 4 km before the charge fall short
    )
    assert after["start_time"] == _at(11)
    assert after["energy_source"] == "soc_estimate"


def test_a_part_after_the_jump_that_fails_the_floors_is_kept_distance_only():
    def jump_then_flat(minute):
        return 40.0 if minute <= 10.0 else 90.0

    frame = _frame(jump_then_flat)
    trips = _detect(frame, _charges(frame), keep_odometer_confirmed_trips=True)
    assert [t["energy_source"] for t in trips] == [DISTANCE_ONLY_SOURCE] * 2
    assert trips[1]["start_time"] == _at(11)
    assert trips[1]["delta_soc_pct"] == 0.0


def test_a_part_whose_change_is_one_impossible_step_gets_no_energy():
    # After the jump the SOC falls 45 points in 20 seconds and stays there: two
    # SOC streams alternating, not a discharge. The part still clears the floors
    # and the capacity band, but its change is one impossible step.
    def alternating(minute):
        if minute <= 10.0:
            return 40.0
        return 90.0 if minute < 11.3 else 45.0

    frame = _frame(alternating, extra=(11 + 1 / 3,))
    trips = _detect(frame, _charges(frame), keep_odometer_confirmed_trips=True)
    after = trips[-1]
    assert after["start_time"] == _at(11)
    assert after["energy_source"] == DISTANCE_ONLY_SOURCE
    assert np.isnan(after["delta_energy_kwh"])
    assert after["delta_soc_pct"] == pytest.approx(-45.0)


def test_a_part_the_capacity_band_rejects_is_dropped():
    # Counter energy of 5 kWh after the jump over 10 SOC points implies 50 kWh,
    # far below the band: that part is dropped, as a whole trip would be.
    def counter(minute):
        return 5e6 + (0.0 if minute <= 11 else 5000.0 * (minute - 11) / 40.0)

    frame = _frame(_jump, counters={MOV: counter})
    trips = _detect(frame, _charges(frame), keep_odometer_confirmed_trips=True)
    assert [t["start_time"] for t in trips] == [_at(5)]  # only the part before
    assert trips[0]["energy_source"] == DISTANCE_ONLY_SOURCE


def test_a_trip_the_capacity_band_rejects_is_not_kept_by_the_odometer():
    def falling(minute):
        return round(80.0 - 10.0 * float(np.clip(minute - 5.0, 0.0, 46.0)) / 46.0, 1)

    # 5 kWh over 10 points implies 50 kWh: the band, not a floor, rejects it.
    frame = _frame(falling, counters={MOV: lambda m: 5e6 + 5000.0 * m / 60.0})
    assert _detect(frame, keep_odometer_confirmed_trips=True) == []


# ── No rise across the trip: a charge inside it only cuts ────────────────────


def test_a_charge_inside_a_trip_without_a_rise_cuts_it_into_distance_only_parts():
    # A frozen 80 % until 08:29, then a live stream at 90 % sinking back to the
    # frozen value by the end: the charge detector sees a charge, but the SOC
    # ends where it started, so it is no jump. Measured beside the charge, the
    # gentle fall after it would read as 10 points of energy.
    def back_to_frozen(minute):
        if minute < 30.0:
            return 80.0
        return round(90.0 - 10.0 * (min(minute, 51.0) - 30.0) / 21.0, 1)

    frame = _frame(back_to_frozen)
    (charge,) = _charges(frame)
    trips = _detect(frame, [charge], keep_odometer_confirmed_trips=True)
    assert [t["energy_source"] for t in trips] == [DISTANCE_ONLY_SOURCE] * 2
    assert [(t["start_time"], t["end_time"]) for t in trips] == [
        (_at(5), _at(29)),
        (_at(30), _at(51)),
    ]
    assert trips[1]["delta_soc_pct"] == pytest.approx(-10.0)
    assert all(np.isnan(t["delta_energy_kwh"]) for t in trips)


def test_a_charge_covering_the_whole_trip_leaves_nothing():
    frame = _frame(_frozen)
    whole = {"start_time": _at(0), "end_time": _at(60)}
    assert _detect(frame, [whole], keep_odometer_confirmed_trips=True) == []


# ── Cutting a window at charges ──────────────────────────────────────────────


def _window(start, end):
    return {"start_time": start, "end_time": end}


@pytest.mark.parametrize(
    "charge, parts, cut",
    [
        ((_at(0), _at(4)), [(_at(5), _at(51))], False),
        ((_at(0), _at(5)), [(_at(5), _at(51))], False),  # touching: no overlap
        ((_at(51), _at(55)), [(_at(5), _at(51))], False),
        ((_at(0), _at(10)), [(_at(10), _at(51))], True),
        ((_at(40), _at(55)), [(_at(5), _at(40))], True),
        ((_at(20), _at(30)), [(_at(5), _at(20)), (_at(30), _at(51))], True),
        ((_at(0), _at(60)), [], True),
    ],
    ids=["before", "touching", "after", "start", "end", "inside", "covering"],
)
def test_the_parts_outside_the_charges(charge, parts, cut):
    windows = _charge_windows([_window(*charge)])
    assert _parts_outside_charges(_at(5), _at(51), windows) == (parts, cut)


def test_the_parts_keep_the_trips_own_time_zone_form():
    aware = _window(_at(20).tz_localize("UTC"), _at(30).tz_localize("UTC"))
    naive_parts, _ = _parts_outside_charges(_at(5), _at(51), _charge_windows([aware]))
    assert all(t.tzinfo is None for part in naive_parts for t in part)
    aware_parts, _ = _parts_outside_charges(
        _at(5).tz_localize("UTC"), _at(51).tz_localize("UTC"), _charge_windows([aware])
    )
    assert all(t.tzinfo is not None for part in aware_parts for t in part)


def test_two_charges_leave_three_parts_and_malformed_ones_are_ignored():
    windows = _charge_windows(
        [
            _window(_at(30), _at(35)),
            {"start_time": _at(10)},  # no end: ignored
            _window(_at(15), _at(15)),  # no duration: ignored
            _window(_at(12), _at(20)),
        ]
    )
    parts, cut = _parts_outside_charges(_at(5), _at(51), windows)
    assert cut is True
    assert parts == [(_at(5), _at(12)), (_at(20), _at(30)), (_at(35), _at(51))]


# ── The mass split, the merge and the anchor ordering ────────────────────────


def _trip(start, end, source="soc_estimate", energy=-20.0, anchors=None):
    seg = {
        "start_time": pd.Timestamp(f"{DAY} {start}", tz="UTC"),
        "end_time": pd.Timestamp(f"{DAY} {end}", tz="UTC"),
        "start_soc": 80.0,
        "end_soc": 75.0,
        "delta_soc_pct": -5.0,
        "delta_energy_kwh": energy,
        "energy_source": source,
        "delta_moving_kwh": None,
        "effective_capacity_kwh": 400.0 if source != DISTANCE_ONLY_SOURCE else None,
        "odo_start_km": 0.0,
        "odo_end_km": 10.0,
        "lat_start": None,
        "lon_start": None,
        "lat_end": None,
        "lon_end": None,
        "_anchor_start_time": None,
        "_anchor_end_time": None,
        "_anchor_start_rel_kwh": float("nan"),
        "_anchor_end_rel_kwh": float("nan"),
    }
    if anchors is not None:
        (t_s, rel_s), (t_e, rel_e) = anchors
        seg["_anchor_start_time"] = pd.Timestamp(f"{DAY} {t_s}", tz="UTC")
        seg["_anchor_end_time"] = pd.Timestamp(f"{DAY} {t_e}", tz="UTC")
        seg["_anchor_start_rel_kwh"] = rel_s
        seg["_anchor_end_rel_kwh"] = rel_e
    return seg


def _distance_only(start, end):
    return _trip(start, end, source=DISTANCE_ONLY_SOURCE, energy=float("nan"))


def _one_cluster_frame():
    """A frame whose every mass reading falls in one cluster (for merge/split)."""
    times = pd.date_range(f"{DAY} 06:00", f"{DAY} 12:00", freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            TIME: [t.isoformat() for t in times],
            "mass_cluster": 1.0,
            "mass_moving": True,
            SPEED: 40.0,
        }
    )


def test_a_distance_only_trip_is_never_merged_and_keeps_its_neighbours_apart():
    first = _trip("07:00", "07:30")
    middle = _distance_only("07:40", "08:00")
    last = _trip("08:10", "08:40")
    merged = merge_discharge_by_mass([first, middle, last], _one_cluster_frame())
    assert [s["start_time"] for s in merged] == [
        first["start_time"],
        middle["start_time"],
        last["start_time"],
    ]
    # Without it, the two trips of the same load merge.
    (both,) = merge_discharge_by_mass([first, last], _one_cluster_frame())
    assert both["delta_energy_kwh"] == pytest.approx(-40.0)


def test_the_mass_split_leaves_a_distance_only_trip_whole():
    frame = _one_cluster_frame()
    frame.loc[frame.index >= 90, "mass_cluster"] = 2.0  # a load change at 07:30
    trip = _distance_only("07:00", "08:00")
    assert split_discharge_by_mass([trip], frame) == [trip]
    # An ordinary trip across the same change is split there.
    ordinary = _trip("07:00", "08:00")
    ordinary["delta_soc_pct"] = -20.0
    ordinary["end_soc"] = 60.0
    frame[SOC] = np.linspace(80.0, 60.0, len(frame))
    assert len(split_discharge_by_mass([ordinary], frame)) == 2


def test_the_anchor_ordering_passes_over_a_distance_only_trip():
    # The first trip's end anchor (09:10) overshoots the last trip's start anchor
    # (09:00); the distance-only trip between them has no anchors to compare.
    cur = _trip(
        "08:00", "08:40", "total_energy", -30.0, (("07:58", 0.0), ("09:10", 30.0))
    )
    middle = _distance_only("08:45", "08:55")
    nxt = _trip(
        "09:05", "10:00", "total_energy", -30.0, (("09:00", 20.0), ("10:05", 50.0))
    )
    assert _enforce_anchor_ordering([cur, middle, nxt], "UTODO01") == 1
    assert cur["delta_energy_kwh"] == pytest.approx(-20.0)
    assert np.isnan(middle["delta_energy_kwh"])


# ── Through run_segment_detection ────────────────────────────────────────────

REG = "UTODO01"
PIPELINE = "ut_odo_speed"
_PIPELINE = {
    "branch": "speed",
    "charge_params": {"plateau_window_min": 60, "min_soc_rise": 5.0},
    "discharge_params": {
        "plateau_window_min": 15,
        "soc_rise_abort_pct": 3.0,
        "min_soc_drop": 5.0,
        "min_energy_kwh": 2.0,
    },
    "speed_params": {
        "speed_threshold_kmh": 1.0,
        "min_stop_duration_min": 5.0,
        "min_trip_duration_min": 2.0,
        "min_soc_drop": 1.0,
        "min_energy_kwh": 1.0,
    },
}
_VEHICLE = {
    "srf_reg": REG,
    "nominal_kwh": NOMINAL,
    "srf_capacity_kwh": NOMINAL,
    "pipeline": PIPELINE,
    "speed_col": SPEED,
    "moving_energy_col": MOV,
    "total_energy_col": TOT,
    "mass_col": MASS,
}


@pytest.fixture
def configure(monkeypatch):
    def _configure(keep=None, **speed_params):
        pipeline = copy.deepcopy(_PIPELINE)
        if keep is not None:
            pipeline["speed_params"]["keep_odometer_confirmed_trips"] = keep
        pipeline["speed_params"].update(speed_params)
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, PIPELINE, pipeline)
        monkeypatch.setitem(constants.VEHICLE_CONFIG, REG, dict(_VEHICLE))

    return _configure


def _run(frame):
    return run_segment_detection(
        frame,
        reg=REG,
        suffix=f"{DAY}_0000",
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=CAP_LO,
        cap_hi=CAP_HI,
    )


def test_the_pipeline_key_hands_the_charges_over_and_the_marker_does_not_leave(
    configure, caplog
):
    configure(True)
    with caplog.at_level(logging.INFO, logger=detection.logger.name):
        charges, trips = _run(_frame(_jump))
    assert len(charges) == 1
    assert [t["energy_source"] for t in trips] == [
        DISTANCE_ONLY_SOURCE,
        "soc_estimate",
    ]
    assert trips[1]["start_time"] == _at(11)
    assert all(_ODOMETER_CONFIRMED_KEY not in t for t in trips)
    assert all("ep_audit" in t for t in trips)
    message = next(
        r.getMessage() for r in caplog.records if "odometer-confirmed" in r.getMessage()
    )
    assert "2 the SOC / energy floors rejected are kept" in message
    assert "1 measured again" in message and "1 with their distance only" in message


@pytest.mark.parametrize("keep", [None, False], ids=["absent", "false"])
def test_without_the_key_a_frozen_soc_leg_has_no_trip(configure, keep):
    configure(keep)
    assert _run(_frame(_frozen)) == ([], [])


def test_trips_kept_on_the_odometer_stand_in_for_the_soc_fallback(configure):
    # Without the key the speed branch yields nothing on this leg, so the SOC
    # branch runs instead and finds the fall after the jump. With it, the speed
    # trips stand, and the fallback does not run.
    configure(None)
    _charges_off, (fallback,) = _run(_frame(_jump))
    assert fallback["energy_source"] == "soc_estimate"
    assert (fallback["start_time"], fallback["end_time"]) == (_at(11), _at(51))
    configure(True)
    _charges_on, trips = _run(_frame(_jump))
    assert [(t["start_time"], t["end_time"]) for t in trips] == [
        (_at(5), _at(10)),
        (_at(11), _at(51)),
    ]
    assert trips[1]["delta_energy_kwh"] == pytest.approx(fallback["delta_energy_kwh"])


def test_the_pipeline_threshold_reaches_the_detector(configure):
    configure(True, min_confirmed_distance_km=40.0)
    _charges_out, trips = _run(_frame(_frozen))
    assert trips == []  # 36 km falls short of 40


# ── Downstream: the row, the capacity model, the grade, the workbook ─────────

I_EP = _row_col_index("Energy Performance (kWh/km)", HEADERS)
I_EP_CORR = _row_col_index(
    "Energy Performance Corrected by Elevation Difference (kWh/km)", HEADERS
)
I_EP_KIN = _row_col_index("Energy Performance Kinetics Corrected (kWh/km)", HEADERS)
I_EP_AUX = _row_col_index("EP_exclude_aux", HEADERS)
I_DIST = _row_col_index("Distance (km)", HEADERS)
I_SPEED = _row_col_index("Average Speed (km/h)", HEADERS)
I_CUM = _row_col_index("Cumulative Distance (km)", HEADERS)
I_CONF = _row_col_index("EP Confidence", HEADERS)
I_REASON = _row_col_index("EP Confidence Reason", HEADERS)
I_TYPE = _row_col_index("Leg Type", HEADERS)


def _rows(configure):
    """A distance-only trip and an ordinary one, as report rows."""
    configure(True)
    frozen = _frame(
        _frozen,
        counters={
            "electric_energy_propulsion": lambda m: 1e6 + 500.0 * m,
            "electric_energy_recuperation_watthours": lambda m: 2e5 + 100.0 * m,
        },
    )
    _c, (distance_only,) = _run(frozen)

    def falling(minute):
        return round(80.0 - 10.0 * float(np.clip(minute - 5.0, 0.0, 46.0)) / 46.0, 1)

    # 45 kWh on the counter between the trip's zero-speed ends over 10 SOC
    # points: a capacity of 450 kWh, in the band.
    later = _frame(
        falling,
        counters={MOV: lambda m: 5e6 + 45000.0 * float(np.clip((m - 5) / 46, 0, 1))},
    )
    later[TIME] = [
        (pd.Timestamp(t) + pd.Timedelta(hours=3)).isoformat() for t in later[TIME]
    ]
    _c, (ordinary,) = _run(later)
    rows, cumulative = [], 0.0
    for seg, frame in ((distance_only, frozen), (ordinary, later)):
        clean = {
            k: v for k, v in seg.items() if k not in constants._ANCHOR_PRIVATE_KEYS
        }
        row, cumulative = _seg_to_row(
            clean,
            "discharge",
            "https://data.example.org/api/legs/leg-1",
            [],
            [],
            frame,
            cumulative,
            None,
            srf_data=None,
            speed_col=SPEED,
            mass_col=MASS,
        )
        rows.append(list(row))
    return rows


def test_the_row_of_a_distance_only_trip_has_no_energy_and_no_ep(configure):
    row, _ordinary = _rows(configure)
    assert row[_IDX_ESOURCE] == DISTANCE_ONLY_SOURCE
    assert np.isnan(row[_IDX_ENERGY])
    for idx in (I_EP, I_EP_CORR, I_EP_KIN, I_EP_AUX):
        assert np.isnan(row[idx])
    assert row[_IDX_CAP] is None
    # Its distance, speed and cumulative distance are measured as for any trip.
    assert row[I_DIST] == pytest.approx(36.0)
    assert row[I_SPEED] == pytest.approx(36.0 / (46 / 60), abs=0.01)
    assert row[I_CUM] == pytest.approx(36.0)
    assert is_driving(row[I_TYPE])
    # Not graded; the reason says why.
    assert row[I_CONF] is None
    assert row[I_REASON] == CODE_NO_ENERGY


def is_driving(leg_type) -> bool:
    from report_generator.columns import is_trip_leg

    return is_trip_leg(leg_type)


def test_the_grade_of_a_distance_only_trip():
    assert assess_ep_confidence(
        None,
        energy_source=DISTANCE_ONLY_SOURCE,
        delta_soc_pct=0.0,
        distance_km=36.0,
        duration_h=0.77,
        energy_kwh=float("nan"),
        ep_kwh_km=float("nan"),
    ) == (None, CODE_NO_ENERGY)


def test_a_distance_only_trip_is_no_capacity_donor(configure):
    distance_only, ordinary = _rows(configure)
    assert ordinary[_IDX_CAP] == pytest.approx(450.0)
    assert _period_capacity_from_rows(
        [distance_only, ordinary], _IDX_CAP, _IDX_SOC_CHANGE, _IDX_ESOURCE
    ) == (pytest.approx(450.0), 1, "discharge")
    assert _period_capacity_from_rows(
        [distance_only], _IDX_CAP, _IDX_SOC_CHANGE, _IDX_ESOURCE
    ) == (None, 0, "fallback")


def test_the_generator_finalises_a_report_holding_a_distance_only_trip(configure):
    distance_only, ordinary = _rows(configure)
    gen = JOLTReportGenerator.__new__(JOLTReportGenerator)
    gen.debug_mode = False
    gen.fast_mode = True
    gen.srf_data = None
    out, period_kwh, period_n, period_src = gen._finalize_rows(
        [copy.copy(distance_only), copy.copy(ordinary)],
        HEADERS,
        is_diesel=False,
        cfg=dict(_VEHICLE),
        soc_est_cap=NOMINAL,
        ep_audits={},
    )
    kept = next(r for r in out if r[_IDX_START] == distance_only[_IDX_START])
    # The capacity correction leaves it alone: no capacity, no energy, no EP.
    assert kept[_IDX_CAP] is None
    assert np.isnan(kept[_IDX_ENERGY]) and np.isnan(kept[I_EP])
    assert (kept[I_CONF], kept[I_REASON]) == (None, CODE_NO_ENERGY)
    # The ledger would record the ordinary trip's capacity only.
    assert (period_kwh, period_n, period_src) == (
        pytest.approx(450.0),
        1,
        "discharge",
    )


def _definitions(path) -> list[str]:
    ws = openpyxl.load_workbook(path)["Definitions"]
    return [c.value for c in ws["A"] if c.value]


def test_a_distance_only_trip_in_a_workbook(configure, tmp_path):
    distance_only, ordinary = _rows(configure)
    path = tmp_path / f"jolt_report_{REG}_20260812_20260812.xlsx"
    _write_excel_report(
        [distance_only, ordinary],
        REG,
        datetime.date(2026, 8, 12),
        datetime.date(2026, 8, 12),
        path,
        headers=HEADERS,
    )
    formulas = openpyxl.load_workbook(path)["Report"]
    values = openpyxl.load_workbook(path, data_only=True)["Report"]

    def col(name):
        return HEADERS.index(name) + 1

    # Energy and every EP are the =NA() "no data" formula, blank to a reader.
    for name in (
        "Energy Change (kWh)",
        "Energy Performance (kWh/km)",
        "Energy Performance Corrected by Elevation Difference (kWh/km)",
        "Energy Performance Kinetics Corrected (kWh/km)",
        "EP_exclude_aux",
    ):
        assert formulas.cell(2, col(name)).value == "=NA()", name
        assert values.cell(2, col(name)).value in (None, ""), name
    assert values.cell(2, col("Battery Capacity (kWh)")).value in (None, "")
    assert values.cell(2, col("Energy Source")).value == DISTANCE_ONLY_SOURCE
    assert values.cell(2, col("EP Confidence")).value in (None, "")
    assert values.cell(2, col("EP Confidence Reason")).value == CODE_NO_ENERGY
    assert values.cell(2, col("Distance (km)")).value == pytest.approx(36.0)
    # The glossary explains the energy source.
    assert any('"distance_only"' in text for text in _definitions(path))
    # The ledger backfill reads no donor from it.
    assert capacity_backfill._read_report_donor_capacity(path) == (
        pytest.approx(450.0),
        1,
        "discharge",
    )


def test_the_glossary_is_unchanged_without_a_distance_only_trip(configure, tmp_path):
    _distance_only, ordinary = _rows(configure)
    with_trip = tmp_path / "a.xlsx"
    without = tmp_path / "b.xlsx"
    for path, rows in ((with_trip, [_distance_only, ordinary]), (without, [ordinary])):
        _write_excel_report(
            rows,
            REG,
            datetime.date(2026, 8, 12),
            datetime.date(2026, 8, 12),
            path,
            headers=HEADERS,
        )
    plain = _definitions(without)
    assert not any("distance_only" in text for text in plain)
    extended = _definitions(with_trip)
    assert len(extended) == len(plain) + 1
    # The one added entry defines the energy source; every other entry is the
    # same, in the same order, and the Leg Type entry stays the last one.
    assert [text for text in extended if "distance_only" not in text] == plain
    assert extended[-1] == plain[-1] and plain[-1].startswith("Leg Type:")
