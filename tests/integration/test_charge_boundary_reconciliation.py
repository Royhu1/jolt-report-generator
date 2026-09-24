"""The charge / trip boundary reconciliation on a real fixture leg.

The EVSOC01 fixture shows the sparse-feed pattern the reconciliation exists for:
its afternoon charge (15:29:43 -> 16:13:35) ends at a telematics sample taken
while the vehicle is already moving (5 km/h at 16:13:35). With the leg on a
Logger-speed pipeline and a 1 Hz Logger speed trace that sees the vehicle roll
off at 16:12:30, the trip starts inside the charge. A pipeline with
``reconcile_charge_boundaries`` clamps the charge's end to the trip's start;
without the key, or without an overlap, nothing changes. The pipeline is reached
the way the field is meant to be used: through a ``period_overrides`` window.
"""

from __future__ import annotations

import copy
import json

import pandas as pd
import pytest

from report_generator.charger_patcher import _find_charger_matches
from report_generator.columns import HEADERS, _row_col_index
from report_generator.report_builder import _insert_stop_rows, _seg_to_row
from report_generator.segment_algorithms import _ANCHOR_PRIVATE_KEYS
from report_generator.segmentation import constants
from report_generator.segmentation.detection import run_segment_detection
from report_generator.segmentation.timeutil import frame_utc_date

ALIAS = "EVSOC01"
TIME = "eventDatetime"
PIPELINE = "evsoc01_logger_speed"  # the reconciling test pipeline
ROLL_OFF = pd.Timestamp("2026-04-24T16:12:30Z")  # when the Logger sees motion
TELEMATICS_END = pd.Timestamp("2026-04-24T16:13:35Z")  # the charge's last sample


def _utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _overlaps(charge, discharge):
    return [
        (c["start_time"], c["end_time"], d["start_time"], d["end_time"])
        for c in charge
        for d in discharge
        if _utc(d["start_time"]) < _utc(c["end_time"])
        and _utc(d["end_time"]) > _utc(c["start_time"])
    ]


def _logger_speed(frame: pd.DataFrame, *, ramp: bool) -> pd.DataFrame:
    """The frame's own speed as a Logger channel, plus a 1 Hz roll-off if asked."""
    times = pd.DatetimeIndex(pd.to_datetime(frame[TIME], utc=True))
    speed = pd.Series(pd.to_numeric(frame["speed"], errors="coerce").to_numpy(), times)
    if ramp:
        seconds = pd.date_range(
            ROLL_OFF - pd.Timedelta("30s"), TELEMATICS_END, freq="1s"
        )
        seconds = seconds[seconds < TELEMATICS_END]
        rolling = [
            max(0.0, (s - ROLL_OFF).total_seconds()) * 5.0 / 65.0 for s in seconds
        ]
        speed = pd.concat([speed, pd.Series(rolling, seconds)]).sort_index()
    return speed.to_frame("CCVS wheel based vehicle speed")


@pytest.fixture
def frame(load_raw_telematics):
    return load_raw_telematics(ALIAS)


@pytest.fixture
def configure(monkeypatch, frozen_configs, frame):
    """EVSOC01 switched, from its own date, to a Logger-speed pipeline."""

    def _configure(reconcile):
        pipeline = copy.deepcopy(frozen_configs["pipelines"]["evspd01_speed"])
        if reconcile is not None:
            pipeline["reconcile_charge_boundaries"] = reconcile
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, PIPELINE, pipeline)
        cfg = copy.deepcopy(frozen_configs["vehicles"][ALIAS])
        cfg["period_overrides"] = [
            {
                "from": frame_utc_date(frame).isoformat(),
                "set": {"pipeline": PIPELINE, "prefer_logger_speed": True},
            }
        ]
        monkeypatch.setitem(constants.VEHICLE_CONFIG, ALIAS, cfg)

    return _configure


def _segment(frame, frozen_configs, logger_speed, **kwargs):
    nominal = frozen_configs["vehicles"][ALIAS]["nominal_kwh"]
    return run_segment_detection(
        frame,
        ALIAS,
        "test",
        out_dir=kwargs.pop("out_dir", None),
        generate_validation_fig=kwargs.pop("generate_validation_fig", False),
        cap_lo=nominal * 0.5,
        cap_hi=nominal * 2.0,
        logger_speed_df=logger_speed,
        **kwargs,
    )


def _afternoon(charges):
    (charge,) = [c for c in charges if _utc(c["start_time"]).hour == 15]
    return charge


# ── The reconciliation on the real overlap ───────────────────────────────────


def test_without_the_key_the_charge_overruns_the_trip(frame, frozen_configs, configure):
    configure(None)
    charge, discharge = _segment(frame, frozen_configs, _logger_speed(frame, ramp=True))
    assert _utc(_afternoon(charge)["end_time"]) == TELEMATICS_END
    assert len(_overlaps(charge, discharge)) == 1


def test_the_charge_end_is_clamped_to_the_trip_start(
    frame, frozen_configs, configure, serialise, caplog
):
    logger_speed = _logger_speed(frame, ramp=True)
    configure(False)
    before_charge, before_trips = _segment(frame, frozen_configs, logger_speed)
    configure(True)
    with caplog.at_level("INFO", logger="report_generator.segmentation"):
        charge, discharge = _segment(frame, frozen_configs, logger_speed)

    clamped = _afternoon(charge)
    assert _utc(clamped["end_time"]) == ROLL_OFF
    assert clamped["end_time"].tzinfo is not None  # the charge keeps its form
    assert _overlaps(charge, discharge) == []
    assert "1 charge boundaries clamped" in caplog.text
    # Only the charge's end moved: its observation and every trip are unchanged.
    original = _afternoon(before_charge)
    assert serialise([{k: v for k, v in clamped.items() if k != "end_time"}]) == (
        serialise([{k: v for k, v in original.items() if k != "end_time"}])
    )
    assert serialise(discharge) == serialise(before_trips)
    others = [c for c in charge if c is not clamped]
    assert serialise(others) == serialise(
        [c for c in before_charge if c is not original]
    )


def test_the_key_off_is_the_key_absent(frame, frozen_configs, configure, serialise):
    logger_speed = _logger_speed(frame, ramp=True)
    configure(None)
    absent = _segment(frame, frozen_configs, logger_speed)
    configure(False)
    off = _segment(frame, frozen_configs, logger_speed)
    assert [serialise(s) for s in off] == [serialise(s) for s in absent]


def test_with_no_overlap_the_key_changes_nothing(
    frame, frozen_configs, configure, serialise
):
    # The sparse speed alone: the trip starts exactly at the charge's end.
    logger_speed = _logger_speed(frame, ramp=False)
    configure(False)
    off = _segment(frame, frozen_configs, logger_speed)
    assert _overlaps(*off) == []
    configure(True)
    on = _segment(frame, frozen_configs, logger_speed)
    assert [serialise(s) for s in on] == [serialise(s) for s in off]


def test_an_external_renderer_is_handed_the_clamped_charges(
    frame, frozen_configs, configure, tmp_path, serialise
):
    configure(True)
    painted = {}

    def painter(_df, charge_segs, discharge_segs, *_args, **_kwargs):
        painted["charge"] = serialise(charge_segs)
        painted["discharge"] = serialise(discharge_segs)

    charge, discharge = _segment(
        frame,
        frozen_configs,
        _logger_speed(frame, ramp=True),
        out_dir=tmp_path,
        generate_validation_fig=True,
        figure_hook=painter,
    )
    assert painted == {"charge": serialise(charge), "discharge": serialise(discharge)}
    assert _utc(_afternoon(charge)["end_time"]) == ROLL_OFF


# ── What is derived from the clamped times ───────────────────────────────────


def _row(seg, mode, frame):
    clean = {k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS}
    row, _ = _seg_to_row(
        clean,
        mode,
        "https://srf.example/api/legs/fixture/",
        [],
        [],
        frame,
        0.0,
        None,
        srf_data=None,
        speed_col="wheel_based_speed",
        operator="OPX",
    )
    return list(row)


def test_the_rows_and_the_stop_insertion_follow_the_clamped_charge(
    frame, frozen_configs, configure
):
    configure(True)
    charge, discharge = _segment(frame, frozen_configs, _logger_speed(frame, ramp=True))
    clamped = _afternoon(charge)
    (trip,) = [d for d in discharge if _utc(d["start_time"]) == ROLL_OFF]
    charge_row, trip_row = _row(clamped, "charge", frame), _row(
        trip, "discharge", frame
    )

    hours = (ROLL_OFF - _utc(clamped["start_time"])).total_seconds() / 3600.0
    assert charge_row[_row_col_index("Duration (HH:MM:SS)")] == pytest.approx(
        hours / 24.0
    )
    assert charge_row[_row_col_index("Battery Power (kW)")] == pytest.approx(
        round(clamped["delta_energy_kwh"] / hours, 3)
    )
    # The charge now ends where the trip starts: no gap, so no Stop row between.
    assert _insert_stop_rows([charge_row, trip_row], headers=HEADERS) == [
        charge_row,
        trip_row,
    ]


def test_charger_fusion_still_finds_the_session_of_a_clamped_charge(
    frame, frozen_configs, configure
):
    configure(True)
    charge, _ = _segment(frame, frozen_configs, _logger_speed(frame, ramp=True))
    clamped = _afternoon(charge)
    session = (
        pd.Timestamp("2026-04-24T15:31:00Z"),
        pd.Timestamp("2026-04-24T16:10:00Z"),
        "https://srf.example/api/charging/1/",
        104.0,
    )
    assert _find_charger_matches(
        [session], clamped["start_time"], clamped["end_time"]
    ) == [session]


# ── The goldens ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reconcile", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("alias", ["EVSPD01", "EVSOC01", "EVMAD01"])
def test_every_golden_is_byte_identical_with_the_key_off_or_on(
    monkeypatch,
    frozen_configs,
    load_raw_telematics,
    fixtures_dir,
    serialise,
    alias,
    reconcile,
):
    # None of the fixture legs has a charge overlapping a trip, so even switched
    # on the reconciliation leaves every golden as it is.
    cfg = frozen_configs["vehicles"][alias]
    pipeline = copy.deepcopy(frozen_configs["pipelines"][cfg["pipeline"]])
    pipeline["reconcile_charge_boundaries"] = reconcile
    monkeypatch.setitem(constants.PIPELINE_CONFIGS, cfg["pipeline"], pipeline)
    nominal = cfg.get("nominal_kwh")
    charge, discharge = run_segment_detection(
        load_raw_telematics(alias),
        alias,
        "test",
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=nominal * 0.5 if nominal else None,
        cap_hi=nominal * 2.0 if nominal else None,
    )
    golden_path = fixtures_dir / "expected" / f"segments_{alias}.json"
    golden_text = golden_path.read_text(encoding="utf-8")
    payload = {
        "alias": alias,
        "source": json.loads(golden_text)["source"],
        "charge": serialise(charge),
        "discharge": serialise(discharge),
    }
    assert json.dumps(payload, indent=2) + "\n" == golden_text
