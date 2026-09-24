"""The two opt-in keys on the committed fixture legs.

* ``soc_event_spike_pct`` (pipeline top level): EVMAD01's feed carries a
  ``trigger_type`` column, and its midday charge (11:57:52 -> 12:27:27, 18 -> 53 %)
  ends on an event row whose 53 % stands 5 points above the periodic reading
  before it (48 %, 12:24:27) and 3 points above the one after it (50 %,
  12:34:27). With a 3-point threshold that reading is blanked and the charge
  ends one row earlier, at the ignition-on row's 52 %; the event rows carrying
  the rise itself (51 %, 52 %) stay. EVSPD01's feed also carries the column but
  has no such excursion, and EVSOC01's has no column at all: both unchanged.
* ``keep_trips_outside_cap_band`` (``speed_params``): EVSPD01 has one speed
  trip the capacity band drops — 17:04:25 -> 17:13:28, ΔSOC -1 %, 22.4 kWh on the
  total-energy counter, an implied 2240.6 kWh against a 1080 kWh ceiling. With
  the key it is kept without a capacity; it has the next trip's mass and no
  charge in between, so the mass merge joins the two, and the merged trip
  carries no capacity either. EVMAD01's trips all lie inside the band:
  unchanged.

With both keys absent, or set off, every fixture reproduces its golden.
"""

from __future__ import annotations

import copy
import datetime

import pandas as pd
import pytest

from report_generator._generator import JOLTReportGenerator
from report_generator.capacity import (
    _IDX_CAP,
    _IDX_ESOURCE,
    _IDX_SOC_CHANGE,
    _IDX_START,
    _period_capacity_from_rows,
)
from report_generator.columns import HEADERS, _row_col_index
from report_generator.report_builder import _seg_to_row, _write_excel_report
from report_generator.segment_algorithms import _ANCHOR_PRIVATE_KEYS
from report_generator.segmentation import constants

EV_ALIASES = ("EVSPD01", "EVSOC01", "EVMAD01")
SOC = constants.SOC_COL
TIME = constants.TIME_COL


@pytest.fixture
def with_keys(monkeypatch, frozen_configs):
    """Set the two keys on a fixture vehicle's frozen pipeline (None: leave out)."""

    def _set(alias, spike_pct=None, keep=None):
        name = frozen_configs["vehicles"][alias]["pipeline"]
        pipeline = copy.deepcopy(frozen_configs["pipelines"][name])
        if spike_pct is not None:
            pipeline["soc_event_spike_pct"] = spike_pct
        if keep is not None:
            pipeline.setdefault("speed_params", {})[
                "keep_trips_outside_cap_band"
            ] = keep
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, name, pipeline)

    return _set


def _golden(load_golden, alias):
    return load_golden(f"segments_{alias}.json")


def _by_start(serialised):
    return {seg["start_time"]: seg for seg in serialised}


# ── Off is exactly the goldens ───────────────────────────────────────────────


@pytest.mark.parametrize("alias", EV_ALIASES)
def test_the_band_key_set_off_reproduces_the_golden(
    alias, with_keys, run_fixture_segmentation, load_golden, serialise
):
    with_keys(alias, keep=False)
    charge, discharge = run_fixture_segmentation(alias)
    golden = _golden(load_golden, alias)
    assert serialise(charge) == golden["charge"]
    assert serialise(discharge) == golden["discharge"]


# ── The event-row SOC filter ─────────────────────────────────────────────────


def test_the_filter_trims_the_excursion_ending_a_real_charge(
    with_keys, run_fixture_segmentation, load_golden, serialise
):
    with_keys("EVMAD01", spike_pct=3)
    charge, discharge = run_fixture_segmentation("EVMAD01")
    golden = _golden(load_golden, "EVMAD01")

    # Every trip and the other two charges are untouched.
    assert serialise(discharge) == golden["discharge"]
    got, want = serialise(charge), golden["charge"]
    assert [got[0], got[2]] == [want[0], want[2]]

    trimmed, before = got[1], want[1]
    assert (before["start_time"], before["end_time"]) == (
        "2025-07-29T11:57:52+00:00",
        "2025-07-29T12:27:27+00:00",
    )
    assert trimmed["start_time"] == before["start_time"]
    assert trimmed["end_time"] == "2025-07-29T12:27:26+00:00"
    assert (trimmed["start_soc"], trimmed["end_soc"]) == (18.0, 52.0)
    # 34 points x the 504 kWh SOC-estimate capacity.
    assert trimmed["delta_energy_kwh"] == pytest.approx(0.34 * 504.0)
    changed = {k for k in before if trimmed[k] != before[k]}
    assert changed == {
        "end_time",
        "_anchor_end_time",
        "end_soc",
        "delta_soc_pct",
        "delta_energy_kwh",
    }


@pytest.mark.parametrize("alias", ["EVSPD01", "EVSOC01"])
def test_the_filter_leaves_a_leg_without_an_excursion_as_its_golden(
    alias, with_keys, run_fixture_segmentation, load_golden, serialise
):
    # EVSPD01: the column is there, no event row stands 3 points above both of
    # its periodic neighbours. EVSOC01: no trigger_type column to judge by.
    with_keys(alias, spike_pct=3)
    charge, discharge = run_fixture_segmentation(alias)
    golden = _golden(load_golden, alias)
    assert serialise(charge) == golden["charge"]
    assert serialise(discharge) == golden["discharge"]


def _soc_at(frame: pd.DataFrame, when: str) -> list[float]:
    at = pd.to_datetime(frame[TIME], utc=True) == pd.Timestamp(when)
    return pd.to_numeric(frame.loc[at, SOC], errors="coerce").tolist()


def test_the_painter_is_handed_the_filtered_leg_and_the_caller_keeps_its_own(
    with_keys, frozen_configs, load_raw_telematics, tmp_path
):
    from report_generator.segment_algorithms import run_segment_detection

    with_keys("EVMAD01", spike_pct=3)
    calls = []
    frame = load_raw_telematics("EVMAD01")
    nominal = frozen_configs["vehicles"]["EVMAD01"]["nominal_kwh"]
    run_segment_detection(
        frame,
        reg="EVMAD01",
        suffix="2025-07-29_0000",
        out_dir=tmp_path,
        generate_validation_fig=True,
        cap_lo=nominal * 0.5,
        cap_hi=nominal * 2.0,
        figure_hook=lambda *args, **kwargs: calls.append(args),
    )
    painted = calls[0][0]
    assert _soc_at(frame, "2025-07-29T12:27:27Z") == [53.0]
    assert pd.isna(_soc_at(painted, "2025-07-29T12:27:27Z")).all()
    # One reading blanked; the rest of the SOC column is what the feed sent.
    was = pd.to_numeric(frame[SOC], errors="coerce")
    now = pd.to_numeric(painted[SOC], errors="coerce")
    blanked = was.notna() & now.isna()
    assert int(blanked.sum()) == 1
    pd.testing.assert_series_equal(now[~blanked], was[~blanked], check_dtype=False)


# ── Trips kept outside the capacity band ─────────────────────────────────────


def test_keeping_changes_nothing_where_every_trip_lies_in_the_band(
    with_keys, run_fixture_segmentation, load_golden, serialise
):
    with_keys("EVMAD01", keep=True)
    charge, discharge = run_fixture_segmentation("EVMAD01")
    golden = _golden(load_golden, "EVMAD01")
    assert serialise(charge) == golden["charge"]
    assert serialise(discharge) == golden["discharge"]


def test_the_trip_the_band_dropped_comes_back_without_a_capacity(
    with_keys, run_fixture_segmentation, load_golden, serialise
):
    with_keys("EVSPD01", keep=True)
    charge, discharge = run_fixture_segmentation("EVSPD01")
    golden = _golden(load_golden, "EVSPD01")
    assert serialise(charge) == golden["charge"]

    got, want = _by_start(serialise(discharge)), _by_start(golden["discharge"])
    # The kept trip starts at 17:04:25 and is merged into the 17:30:40 trip.
    assert set(want) - set(got) == {"2025-06-27T17:30:40"}
    assert set(got) - set(want) == {"2025-06-27T17:04:25+00:00"}
    merged = got["2025-06-27T17:04:25+00:00"]
    # The same instant; the merge writes its times tz-aware.
    assert _naive(merged["end_time"]) == _naive(want["2025-06-27T17:30:40"]["end_time"])
    assert merged["delta_soc_pct"] == pytest.approx(-42.0)
    assert merged["energy_source"] == "total_energy"
    assert merged["effective_capacity_kwh"] is None
    assert "_capacity_outside_band" not in merged
    # Without the key that trip reported 118.9 kWh, below the 270 kWh floor.
    assert want["2025-06-27T17:30:40"]["effective_capacity_kwh"] == pytest.approx(118.9)
    # Every other trip is untouched.
    for start in set(got) & set(want):
        assert got[start] == want[start]


def _naive(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert(None) if ts.tzinfo is not None else ts


def _ev_rows(frame, charge, discharge, cfg):
    """``_generator._process_fps_legs``' row building, offline."""
    rows = []
    cumulative_km = 0.0
    for mode, segs in (("charge", charge), ("discharge", discharge)):
        for seg in segs:
            clean = {k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS}
            row, km = _seg_to_row(
                clean,
                mode,
                "https://data.example.org/api/legs/leg-1",
                [],
                [],
                frame,
                cumulative_km,
                None,
                srf_data=None,
                altitude_col=cfg.get("altitude_col"),
                speed_col=cfg.get("speed_col", "wheel_based_speed"),
                mass_agg=cfg.get("mass_agg", "mean"),
            )
            if mode == "discharge":
                cumulative_km = km
            rows.append((seg["start_time"], list(row)))
    rows.sort(key=lambda pair: _naive(pair[0]))
    return [row for _, row in rows]


def test_a_report_with_a_kept_trip_is_finalised_and_written(
    with_keys,
    run_fixture_segmentation,
    load_raw_telematics,
    frozen_configs,
    tmp_path,
):
    with_keys("EVSPD01", keep=True)
    cfg = frozen_configs["vehicles"]["EVSPD01"]
    frame = load_raw_telematics("EVSPD01")
    charge, discharge = run_fixture_segmentation("EVSPD01")
    rows = _ev_rows(frame, charge, discharge, cfg)
    kept_start = pd.Timestamp("2025-06-27T17:04:25Z")
    kept = next(r for r in rows if pd.Timestamp(r[_IDX_START]) == kept_start)
    assert kept[_IDX_CAP] is None

    gen = JOLTReportGenerator.__new__(JOLTReportGenerator)
    gen.debug_mode = False
    gen.fast_mode = True
    gen.srf_data = None
    out, period_kwh, period_n, period_src = gen._finalize_rows(
        [list(r) for r in rows],
        HEADERS,
        is_diesel=False,
        cfg=cfg,
        soc_est_cap=cfg["effective_capacity_kwh"],
        ep_audits={},
    )
    kept_out = next(r for r in out if pd.Timestamp(r[_IDX_START]) == kept_start)
    assert kept_out[_IDX_CAP] is None  # the capacity correction left it alone
    assert kept_out[_row_col_index("EP Confidence", HEADERS)] in (
        "good",
        "caution",
        "poor",
    )
    # The period capacity is the donors' — the kept trip is not one of them.
    donors = [r for r in out if r is not kept_out]
    assert (period_kwh, period_n, period_src) == _period_capacity_from_rows(
        donors, _IDX_CAP, _IDX_SOC_CHANGE, _IDX_ESOURCE
    )

    # _finalize_rows has inserted the Stop rows; the workbook takes them as they are.
    path = tmp_path / "jolt_report_EVSPD01_20250627_20250628.xlsx"
    _write_excel_report(
        out,
        "EVSPD01",
        datetime.date(2025, 6, 27),
        datetime.date(2025, 6, 28),
        path,
        headers=HEADERS,
    )
    from report_generator.capacity_backfill import _read_report_donor_capacity

    # Read back as the ledger backfill reads it: the same donors.
    assert _read_report_donor_capacity(path) == (
        pytest.approx(period_kwh),
        period_n,
        period_src,
    )
