"""``keep_odometer_confirmed_trips`` on the committed fixture legs.

* EVSPD02 (an SOC-only feed on a speed pipeline, SOC in 0.4-point steps) is a day
  whose first trip was lost. The vehicle charged while the telematics were
  silent, and the feed kept sending the SOC from before the charge (27.6 %) until
  05:45:30, when the vehicle was already moving; at 05:49:45 it reads 95.6 %. The
  charge detector places a +68 % charge at 05:45:30 -> 05:49:45, inside the speed
  trip 05:42:29 -> 06:49:45, whose SOC therefore rises (27.6 -> 81.6 %) and fails
  the 1-point floor. With the key the trip is cut at the charge and measured
  again after it: 05:49:45 -> 06:49:45, 95.6 -> 81.6 %, 69.825 km and 14 % of
  462 kWh. The 0.395 km before the charge fall below the 0.5 km threshold. Three
  short hops the floor rejects are kept distance-only: 08:23:36 -> 08:32:38
  (0.955 km, 66.4 -> 66.0 %), 09:12:56 -> 09:19:14 (0.585 km, a stale 66.0 %,
  ending where the next charge starts) and 14:47:22 -> 15:07:07 (4.15 km,
  34.8 -> 34.0 %); two shorter ones (0.455 km, 0.075 km) stay dropped.
* EVSPD01 (a counter feed with an integer SOC) gains one distance-only yard move
  after a charge: 16:41:01 -> 16:56:06, 1.95 km at a SOC of 99 %.
* EVSOC01 (the SOC branch) and EVMAD01 have no trip the floors reject: unchanged.

With the key absent or set off, every fixture reproduces its golden.
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from report_generator._generator import JOLTReportGenerator
from report_generator.columns import DISTANCE_ONLY_SOURCE, HEADERS, _row_col_index
from report_generator.ep_confidence import CODE_NO_ENERGY
from report_generator.report_builder import _seg_to_row
from report_generator.segment_algorithms import _ANCHOR_PRIVATE_KEYS
from report_generator.segmentation import constants

EV_ALIASES = ("EVSPD01", "EVSOC01", "EVMAD01", "EVSPD02")


@pytest.fixture
def with_key(monkeypatch, frozen_configs):
    """Set the key (and other speed parameters) on a fixture's frozen pipeline."""

    def _set(alias, keep=True, **speed_params):
        name = frozen_configs["vehicles"][alias]["pipeline"]
        pipeline = copy.deepcopy(frozen_configs["pipelines"][name])
        params = pipeline.setdefault("speed_params", {})
        if keep is not None:
            params["keep_odometer_confirmed_trips"] = keep
        params.update(speed_params)
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, name, pipeline)

    return _set


def _golden(load_golden, alias):
    return load_golden(f"segments_{alias}.json")


def _utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _km(seg) -> float:
    return round(seg["odo_end_km"] - seg["odo_start_km"], 3)


# ── Off is exactly the goldens ───────────────────────────────────────────────


@pytest.mark.parametrize("alias", EV_ALIASES)
def test_the_key_set_off_reproduces_the_golden(
    alias, with_key, run_fixture_segmentation, load_golden, serialise
):
    with_key(alias, keep=False)
    charges, trips = run_fixture_segmentation(alias)
    golden = _golden(load_golden, alias)
    assert serialise(charges) == golden["charge"]
    assert serialise(trips) == golden["discharge"]


@pytest.mark.parametrize("alias", ["EVSOC01", "EVMAD01"])
def test_a_fixture_without_a_rejected_trip_is_unchanged_by_the_key(
    alias, with_key, run_fixture_segmentation, load_golden, serialise
):
    with_key(alias)
    charges, trips = run_fixture_segmentation(alias)
    golden = _golden(load_golden, alias)
    assert serialise(charges) == golden["charge"]
    assert serialise(trips) == golden["discharge"]


# ── The day with the lost trip ───────────────────────────────────────────────


@pytest.fixture
def jump_day(with_key, run_fixture_segmentation, load_golden, serialise):
    """EVSPD02 segmented with the key on, and its golden (the key off)."""
    with_key("EVSPD02")
    charges, trips = run_fixture_segmentation("EVSPD02")
    return charges, trips, _golden(load_golden, "EVSPD02"), serialise


def test_the_charges_and_the_trips_already_kept_are_unchanged(jump_day):
    charges, trips, golden, serialise = jump_day
    assert serialise(charges) == golden["charge"]
    kept_before = {seg["start_time"]: seg for seg in golden["discharge"]}
    now = {seg["start_time"]: seg for seg in serialise(trips)}
    assert {start: now[start] for start in kept_before} == kept_before


def test_the_lost_trip_comes_out_as_a_charge_then_the_trip(jump_day):
    charges, trips, _golden_day, _serialise = jump_day
    jump = next(c for c in charges if c["start_soc"] == 27.6)
    assert (_utc(jump["start_time"]), _utc(jump["end_time"])) == (
        pd.Timestamp("2025-11-25 05:45:30.544", tz="UTC"),
        pd.Timestamp("2025-11-25 05:49:45.740", tz="UTC"),
    )
    (trip,) = [t for t in trips if _utc(t["start_time"]) == _utc(jump["end_time"])]
    assert _utc(trip["end_time"]) == pd.Timestamp("2025-11-25 06:49:45.783", tz="UTC")
    assert (trip["start_soc"], trip["end_soc"]) == (95.6, 81.6)
    assert trip["delta_soc_pct"] == pytest.approx(-14.0)
    assert trip["energy_source"] == "soc_estimate"
    assert trip["delta_energy_kwh"] == pytest.approx(-0.14 * 462)
    assert trip["effective_capacity_kwh"] == pytest.approx(462.0)
    assert _km(trip) == pytest.approx(69.825)
    # Nothing is kept for the 0.395 km driven before the charge, on the stale SOC.
    assert not [t for t in trips if _utc(t["end_time"]) <= _utc(jump["start_time"])]


def test_the_short_hops_are_kept_distance_only(jump_day):
    _charges, trips, _golden_day, _serialise = jump_day
    distance_only = [
        (
            _utc(t["start_time"]).strftime("%H:%M:%S"),
            _km(t),
            t["start_soc"],
            t["end_soc"],
        )
        for t in trips
        if t["energy_source"] == DISTANCE_ONLY_SOURCE
    ]
    assert distance_only == [
        ("08:23:36", 0.955, 66.4, 66.0),
        ("09:12:56", 0.585, 66.0, 66.0),
        ("14:47:22", 4.15, 34.8, 34.0),
    ]
    for trip in trips:
        if trip["energy_source"] == DISTANCE_ONLY_SOURCE:
            assert np.isnan(trip["delta_energy_kwh"])
            assert trip["effective_capacity_kwh"] is None
            assert "ep_audit" in trip


def test_nothing_kept_overlaps_a_charge(jump_day):
    charges, trips, _golden_day, _serialise = jump_day
    for trip in trips:
        for charge in charges:
            overlap = _utc(trip["start_time"]) < _utc(charge["end_time"]) and _utc(
                charge["start_time"]
            ) < _utc(trip["end_time"])
            assert not overlap, (trip["start_time"], charge["start_time"])


def test_a_larger_threshold_drops_the_short_hops(
    with_key, run_fixture_segmentation, load_golden, serialise
):
    with_key("EVSPD02", min_confirmed_distance_km=5.0)
    _charges, trips = run_fixture_segmentation("EVSPD02")
    kept = {seg["start_time"] for seg in _golden(load_golden, "EVSPD02")["discharge"]}
    added = [t for t in serialise(trips) if t["start_time"] not in kept]
    # Only the trip after the jump remains: every hop is shorter than 5 km.
    assert [(t["start_time"], t["energy_source"]) for t in added] == [
        ("2025-11-25T05:49:45.740000", "soc_estimate")
    ]


def test_a_lower_soc_floor_measures_the_hops_the_soc_saw(
    with_key, run_fixture_segmentation
):
    # The later re-tune this option must work with: at the feed's 0.4-point
    # resolution, a one-step floor gives the hops whose SOC moved an energy.
    with_key("EVSPD02", min_soc_drop=0.4)
    _charges, trips = run_fixture_segmentation("EVSPD02")
    by_start = {_utc(t["start_time"]).strftime("%H:%M:%S"): t for t in trips}
    assert by_start["08:23:36"]["energy_source"] == "soc_estimate"
    assert by_start["08:23:36"]["delta_energy_kwh"] == pytest.approx(-0.004 * 462)
    assert by_start["14:47:22"]["delta_energy_kwh"] == pytest.approx(-0.008 * 462)
    # The stale SOC measures nothing, whatever the floor.
    assert by_start["09:12:56"]["energy_source"] == DISTANCE_ONLY_SOURCE


# ── The counter feed ─────────────────────────────────────────────────────────


def test_the_counter_feed_gains_one_distance_only_yard_move(
    with_key, run_fixture_segmentation, load_golden, serialise
):
    with_key("EVSPD01")
    charges, trips = run_fixture_segmentation("EVSPD01")
    golden = _golden(load_golden, "EVSPD01")
    assert serialise(charges) == golden["charge"]
    now = serialise(trips)
    kept = {seg["start_time"] for seg in golden["discharge"]}
    added = [t for t in now if t["start_time"] not in kept]
    assert [t for t in now if t["start_time"] in kept] == golden["discharge"]
    (move,) = added
    assert (move["start_time"], move["end_time"]) == (
        "2025-06-27T16:41:01",
        "2025-06-27T16:56:06",
    )
    assert move["energy_source"] == DISTANCE_ONLY_SOURCE
    assert (move["start_soc"], move["end_soc"]) == (99.0, 99.0)
    assert _km(move) == pytest.approx(1.95)


# ── The report rows of the day ───────────────────────────────────────────────

I_TYPE = _row_col_index("Leg Type", HEADERS)
I_START = _row_col_index("Start Time (UTC)", HEADERS)
I_END = _row_col_index("End Time (UTC)", HEADERS)
I_DIST = _row_col_index("Distance (km)", HEADERS)
I_ENERGY = _row_col_index("Energy Change (kWh)", HEADERS)
I_EP = _row_col_index("Energy Performance (kWh/km)", HEADERS)
I_SRC = _row_col_index("Energy Source", HEADERS)
I_CAP = _row_col_index("Battery Capacity (kWh)", HEADERS)
I_CONF = _row_col_index("EP Confidence", HEADERS)
I_REASON = _row_col_index("EP Confidence Reason", HEADERS)


def _day_rows(charges, trips, frame, cfg):
    """The day's report rows as the generator builds them, Stop rows included."""
    rows = []
    cumulative = 0.0
    for mode, group in (("charge", charges), ("discharge", trips)):
        for seg in group:
            clean = {k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS}
            row, cumulative = _seg_to_row(
                clean,
                mode,
                "https://data.example.org/api/legs/leg-1",
                [],
                [],
                frame,
                cumulative,
                None,
                srf_data=None,
                speed_col=cfg["speed_col"],
                mass_col=cfg["mass_col"],
            )
            rows.append((seg["start_time"], list(row)))
    rows.sort(key=lambda pair: _utc(pair[0]))
    gen = JOLTReportGenerator.__new__(JOLTReportGenerator)
    gen.debug_mode = False
    gen.fast_mode = True
    gen.srf_data = None
    out, _kwh, _n, _src = gen._finalize_rows(
        [row for _start, row in rows],
        HEADERS,
        is_diesel=False,
        cfg=cfg,
        soc_est_cap=cfg["nominal_kwh"],
        ep_audits={},
    )
    return out


def test_the_days_report_rows(jump_day, frozen_configs, load_raw_telematics):
    charges, trips, _golden_day, _serialise = jump_day
    cfg = frozen_configs["vehicles"]["EVSPD02"]
    rows = _day_rows(charges, trips, load_raw_telematics("EVSPD02"), cfg)
    # One after the other: no row starts before the previous one ends.
    for previous, row in zip(rows, rows[1:]):
        assert _utc(row[I_START]) >= _utc(previous[I_END])
    first_trip = next(r for r in rows if r[I_TYPE] not in ("Stop",) and r[I_DIST] > 60)
    assert _utc(first_trip[I_START]) == pd.Timestamp(
        "2025-11-25 05:49:45.740", tz="UTC"
    )
    assert first_trip[I_EP] == pytest.approx(0.14 * 462 / 69.825, abs=1e-4)
    distance_only = [r for r in rows if r[I_SRC] == DISTANCE_ONLY_SOURCE]
    assert [r[I_DIST] for r in distance_only] == [0.955, 0.585, 4.15]
    for row in distance_only:
        assert np.isnan(row[I_ENERGY]) and np.isnan(row[I_EP])
        assert row[I_CAP] is None
        assert (row[I_CONF], row[I_REASON]) == (None, CODE_NO_ENERGY)
