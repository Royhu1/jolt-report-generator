"""Odometer readings out of the counter's sequence are ignored, on synthetic frames.

A trip's (or a charge's) distance is the difference between the valid odometer
readings nearest before its start and nearest after its end. Some telematics
feeds send, between the vehicle's current readings, an earlier odometer value
again — typically on the row the unit sends as the vehicle wakes up, just before
it sets off — and a trip leaving from there took the old value as its start, so
its distance included everything driven since that earlier reading: 73.60 km for
a trip the Logger measured at 49.24 km. ``_blank_replayed_odometer`` blanks, on
every leg and before any detector reads the odometer, a reading of zero and a run
of one value that breaks the counter's sequence between readings that agree with
each other; a drop the counter does not come back from (a reset) is kept.
"""

from __future__ import annotations

import copy
import logging

import numpy as np
import pandas as pd
import pytest

from report_generator.columns import _row_col_index
from report_generator.row_builder import _seg_to_row
from report_generator.segmentation import constants, detection
from report_generator.segmentation.detection import (
    _blank_replayed_odometer,
    _filter_replayed_odometer,
    _out_of_sequence_readings,
    run_segment_detection,
)

TIME = constants.TIME_COL
ODO = constants.ODO_COL
SOC = constants.SOC_COL
ENERGY = constants.TOTAL_ENERGY_COL
AC = constants.AC_COL
DC = constants.DC_COL
SPEED = "wheel_based_speed"
DAY = "2026-07-25"
NAN = np.nan

# The audited replay: parked at 36155.485 since 05:09, the row sent at 05:41:20
# carries 36131.13 — the value of a stop an hour earlier — and the next reading,
# 2 min 24 s later, is back at 36155.51.
AUDITED = [
    ("05:05:45", 36155.185),
    ("05:07:40", 36155.475),
    ("05:09:01", 36155.485),
    ("05:41:20", 36131.13),
    ("05:43:44", 36155.51),
    ("05:45:45", 36156.55),
]


def _frame(rows) -> pd.DataFrame:
    """A telematics frame read the way production reads it: every value text.

    ``rows`` are ``(hh:mm:ss, odometer)``; a NaN odometer is a missing value, as
    ``read_csv(dtype=str)`` gives it.
    """
    return pd.DataFrame(
        {
            TIME: [f"{DAY}T{hms}Z" for hms, _ in rows],
            ODO: [NAN if pd.isna(km) else str(km) for _, km in rows],
        },
        dtype=object,
    )


def _blanked(before: pd.DataFrame, after: pd.DataFrame) -> list[str]:
    """The times (hh:mm:ss) whose odometer the filter removed, row by row."""
    was = pd.to_numeric(before[ODO], errors="coerce").to_numpy()
    now = pd.to_numeric(after[ODO], errors="coerce").to_numpy()
    return [
        str(when)[11:19]
        for when, old, new in zip(before[TIME].to_numpy(), was, now)
        if not np.isnan(old) and np.isnan(new)
    ]


def _clean(rows) -> tuple[list[str], int]:
    frame = _frame(rows)
    cleaned, n = _blank_replayed_odometer(frame)
    return _blanked(frame, cleaned), n


# ── What is blanked ──────────────────────────────────────────────────────────


def test_an_earlier_value_sent_again_between_current_readings_is_blanked():
    # 36131.13 lies 24.355 km below 36155.485, and 36155.51 follows 36155.485
    # (+0.025 km in 34 min 43 s): the counter never left it.
    assert _clean(AUDITED) == (["05:41:20"], 1)


def test_a_value_sent_again_several_times_is_blanked_as_a_whole():
    rows = [
        ("07:15:59", 11280.245),
        ("07:18:02", 11280.38),
        ("07:19:19", 11094.44),
        ("07:57:17", 11094.44),
        ("08:10:33", 11094.44),
        ("08:13:36", 11094.44),
        ("08:15:51", 11280.5),
        ("08:17:17", 11280.56),
    ]
    # One run of 11094.44, 185.94 km below 11280.38; 11280.5 follows 11280.38.
    assert _clean(rows) == (["07:19:19", "07:57:17", "08:10:33", "08:13:36"], 4)


def test_a_value_sent_again_is_blanked_however_slowly_the_counter_returns():
    # From 14122.855 to 14129.705 in 19 min 33 s would be only 21 km/h, but
    # 14129.705 follows 14129.685 (+0.02 km): the vehicle stood still throughout.
    rows = [
        ("10:12:52", 14129.685),
        ("10:16:44", 14122.855),
        ("10:36:17", 14129.705),
        ("10:40:00", 14130.2),
    ]
    assert _clean(rows) == (["10:16:44"], 1)


@pytest.mark.parametrize(
    "rows, expected",
    [
        (
            [("09:00:00", 1000.0), ("09:01:00", 0), ("09:02:00", 1000.5)],
            ["09:01:00"],
        ),
        (
            [("09:00:00", 0), ("09:01:00", 1000.0), ("09:02:00", 1000.5)],
            ["09:00:00"],
        ),
        (
            [("09:00:00", 1000.0), ("09:01:00", 1000.5), ("09:02:00", -1)],
            ["09:02:00"],
        ),
    ],
    ids=["between", "first", "negative-last"],
)
def test_a_zero_or_negative_reading_is_blanked_wherever_it_is(rows, expected):
    assert _clean(rows) == (expected, 1)


def test_a_value_running_ahead_of_the_counter_is_blanked():
    # 6868.78 is 74.885 km ahead of 6793.895 after 57 s (130 km/h covers 2.06 km);
    # 6793.915 is lower than it and follows 6793.895 (+0.02 km).
    rows = [
        ("15:24:28", 6792.84),
        ("15:25:58", 6793.895),
        ("15:26:55", 6868.78),
        ("15:27:30", 6793.915),
        ("15:29:01", 6794.835),
    ]
    assert _clean(rows) == (["15:26:55"], 1)


def test_a_run_ahead_of_the_counter_is_blanked_as_a_whole():
    # 6868.78 is 70.29 km ahead of 6798.49 after 68 s; 6830.59 is lower than the
    # run and follows 6798.49 (+32.1 km in 23 min 52 s, 80.7 km/h).
    rows = [
        ("15:34:58", 6798.385),
        ("15:35:47", 6798.49),
        ("15:36:55", 6868.78),
        ("15:41:55", 6868.78),
        ("15:46:55", 6868.78),
        ("15:51:55", 6868.78),
        ("15:56:55", 6868.78),
        ("15:59:39", 6830.59),
        ("16:01:28", 6830.59),
    ]
    expected = ["15:36:55", "15:41:55", "15:46:55", "15:51:55", "15:56:55"]
    assert _clean(rows) == (expected, 5)


# ── What is kept ─────────────────────────────────────────────────────────────


def test_a_counter_reset_is_kept_with_everything_after_it():
    # The counter drops 192.32 km and carries on from there: 58477.86 does not
    # follow 58670.07, so nothing came back and nothing is blanked.
    rows = [
        ("00:12:38", 58670.07),
        ("05:22:32", 58670.07),
        ("05:24:40", 58477.75),
        ("05:25:50", 58477.86),
        ("05:27:10", 58477.87),
        ("06:24:53", 58477.87),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_replayed_odometer(frame)
    assert n == 0
    assert cleaned is frame


def test_a_reset_to_near_zero_is_kept():
    rows = [
        ("09:00:00", 50000.0),
        ("09:01:00", 50000.4),
        ("09:02:00", 0.2),
        ("09:03:00", 0.6),
        ("09:04:00", 1.1),
    ]
    assert _clean(rows) == ([], 0)


def test_a_leg_whose_readings_follow_one_another_is_returned_as_it_is():
    # Parked, 5 m quantisation steps both ways, and a 120 km/h minute.
    rows = [
        ("09:00:00", 1000.0),
        ("09:10:00", 1000.0),
        ("09:10:30", 1000.005),
        ("09:10:31", 1000.0),
        ("09:11:31", 1002.0),
        ("09:12:31", 1003.2),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_replayed_odometer(frame)
    assert n == 0
    assert cleaned is frame


@pytest.mark.parametrize(
    "middle, expected",
    [(999.96, []), (999.94, ["09:01:00"])],
    ids=["0.04 km back: resolution", "0.06 km back: out of sequence"],
)
def test_a_step_back_within_the_tolerance_is_resolution(middle, expected):
    # The tolerance is 0.05 km.
    rows = [("09:00:00", 1000.0), ("09:01:00", middle), ("09:02:00", 1000.0)]
    assert _clean(rows) == (expected, len(expected))


@pytest.mark.parametrize(
    "middle, expected",
    [(1002.1, []), (1002.3, ["09:01:00"])],
    ids=["2.1 km in 60 s: possible", "2.3 km in 60 s: too fast"],
)
def test_a_step_ahead_within_the_speed_limit_is_possible(middle, expected):
    # 130 km/h covers 2.167 km in 60 s, plus the 0.05 km tolerance: 2.217 km.
    rows = [("09:00:00", 1000.0), ("09:01:00", middle), ("09:02:00", 1000.0)]
    assert _clean(rows) == (expected, len(expected))


def test_a_jump_ahead_the_counter_stays_at_is_kept():
    # Nothing after 1100.0 comes back below it.
    rows = [
        ("09:00:00", 1000.0),
        ("09:01:00", 1000.5),
        ("09:02:00", 1100.0),
        ("09:03:00", 1100.5),
        ("09:04:00", 1101.0),
    ]
    assert _clean(rows) == ([], 0)


@pytest.mark.parametrize(
    "rows",
    [
        [("05:41:20", 36131.13), ("05:43:44", 36155.51), ("05:45:45", 36156.55)],
        [("05:43:44", 36155.51), ("05:45:45", 36156.55), ("05:47:00", 36131.13)],
    ],
    ids=["first", "last"],
)
def test_the_first_and_the_last_run_of_a_leg_are_kept(rows):
    # Nothing on one side to judge them against.
    assert _clean(rows) == ([], 0)


# ── How rows are read ────────────────────────────────────────────────────────


def test_readings_are_judged_in_time_order_not_row_order():
    rows = [AUDITED[i] for i in (4, 0, 3, 5, 2, 1)]
    assert _clean(rows) == (["05:41:20"], 1)


def test_a_row_without_a_timestamp_is_left_alone():
    frame = pd.concat(
        [
            _frame(AUDITED),
            pd.DataFrame({TIME: ["not a time"], ODO: ["0"]}, dtype=object),
        ],
        ignore_index=True,
    )
    cleaned, n = _blank_replayed_odometer(frame)
    assert n == 1
    assert cleaned[ODO].iloc[-1] == "0"


def test_the_result_maps_back_onto_any_index():
    frame = _frame(AUDITED)
    frame.index = [f"row{i}" for i in range(len(frame))]
    cleaned, _ = _blank_replayed_odometer(frame)
    assert pd.isna(cleaned.loc["row3", ODO])
    assert cleaned.drop(index="row3")[ODO].notna().all()


def test_the_callers_frame_is_never_modified():
    frame = _frame(AUDITED)
    before = frame.copy(deep=True)
    cleaned, n = _blank_replayed_odometer(frame)
    assert n == 1
    assert cleaned is not frame
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize(
    "values, dtype",
    [
        (["1000.0", "0", "1000.5"], object),  # the production read: text
        ([1000.0, 0.0, 1000.5], "float64"),
        ([1000, 0, 1001], object),  # an integer column cannot hold NaN
    ],
    ids=["text", "float", "integer"],
)
def test_the_odometer_column_keeps_a_type_that_holds_the_blank(values, dtype):
    frame = pd.DataFrame(
        {
            TIME: [f"{DAY}T09:00:00Z", f"{DAY}T09:01:00Z", f"{DAY}T09:02:00Z"],
            ODO: values,
        }
    )
    cleaned, n = _blank_replayed_odometer(frame)
    assert n == 1
    assert cleaned[ODO].dtype == dtype
    assert pd.isna(cleaned[ODO].iloc[1])
    assert pd.to_numeric(cleaned[ODO]).iloc[[0, 2]].tolist() == [
        float(values[0]),
        float(values[2]),
    ]


@pytest.mark.parametrize("missing", [TIME, ODO])
def test_a_frame_without_the_time_or_odometer_column_is_returned_as_it_is(missing):
    frame = _frame(AUDITED).drop(columns=[missing])
    cleaned, n = _blank_replayed_odometer(frame)
    assert (cleaned is frame, n) == (True, 0)


def test_the_array_rule_needs_three_readings():
    times = pd.DatetimeIndex([f"{DAY}T09:00:00Z", f"{DAY}T09:01:00Z"]).asi8
    assert not _out_of_sequence_readings(times, np.array([1000.0, 900.0])).any()


def test_the_number_of_readings_ignored_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger=detection.logger.name):
        _filter_replayed_odometer(_frame(AUDITED), "UTODO01", "leg")
    assert any(
        "odometer: 1 readings ignored" in r.getMessage()
        and "UTODO01 leg" in r.getMessage()
        for r in caplog.records
    )


# ── Through run_segment_detection ────────────────────────────────────────────

REG = "UTODO01"
PIPELINE = "ut_odometer_speed"
_PIPELINE = {
    "branch": "speed",
    "charge_params": {
        "plateau_window_min": 60,
        "min_soc_rise": 5.0,
        "min_energy_kwh": 5.0,
    },
    "discharge_params": {
        "plateau_window_min": 20,
        "soc_rise_abort_pct": 3.0,
        "min_soc_drop": 5.0,
        "min_energy_kwh": 2.0,
    },
    "speed_params": {
        "speed_threshold_kmh": 1.0,
        "min_stop_duration_min": 10.0,
        "min_trip_duration_min": 2.0,
        "min_soc_drop": 1.0,
        "min_energy_kwh": 1.0,
    },
}
CAP_LO, CAP_HI = 300.0, 1200.0

# The audited morning: parked, the wake-up row at 05:41:20 carries the replayed
# 36131.13 (and no speed or energy), then an hour's driving to 36204.73. The SOC
# falls 82 -> 72 and the energy counter rises 60 kWh: a 600 kWh capacity.
#          time        speed   SOC    odometer     energy (Wh)
MORNING = [
    ("05:00:00", "0", "82", "36155.485", "1000000"),
    ("05:09:01", "0", "82", "36155.485", "1000000"),
    ("05:41:20", NAN, "82", "36131.13", NAN),
    ("05:43:44", "3", "82", "36155.51", "1000200"),
    ("05:49:36", "81", "80", "36160.73", "1010000"),
    ("06:05:46", "82", "77", "36177.83", "1030000"),
    ("06:21:53", "49", "74", "36196.165", "1048000"),
    ("06:33:54", "25", "72", "36204.345", "1058000"),
    ("06:37:14", "0", "72", "36204.725", "1060000"),
    ("06:41:12", "0", "72", "36204.73", "1060000"),
    ("06:42:07", "0", "72", "36204.73", "1060000"),
]


def _leg(rows=MORNING) -> pd.DataFrame:
    return pd.DataFrame(
        {
            TIME: [f"{DAY}T{r[0]}Z" for r in rows],
            SPEED: [r[1] for r in rows],
            SOC: [r[2] for r in rows],
            ODO: [r[3] for r in rows],
            ENERGY: [r[4] for r in rows],
        },
        dtype=object,
    )


def _logger_speed() -> pd.DataFrame:
    """Logger speed every minute: standing to 05:42, driving 05:43-06:40, standing."""
    index = pd.date_range(f"{DAY} 05:30", f"{DAY} 06:50", freq="1min", tz="UTC")
    moving = (index >= f"{DAY} 05:43") & (index <= f"{DAY} 06:40")
    return pd.DataFrame({"logger_speed": np.where(moving, 50.0, 0.0)}, index=index)


@pytest.fixture
def configure(monkeypatch):
    """Inject a speed-branch vehicle; ``prefer_logger_speed`` optional."""

    def _configure(*, prefer_logger_speed=False, pipeline_overrides=None):
        cfg = {
            "srf_reg": REG,
            "nominal_kwh": 600,
            "effective_capacity_kwh": 600,
            "pipeline": PIPELINE,
            "speed_col": SPEED,
            "split_by_mass": False,
        }
        if prefer_logger_speed:
            cfg["prefer_logger_speed"] = True
        monkeypatch.setitem(constants.VEHICLE_CONFIG, REG, cfg)
        pipeline = copy.deepcopy(_PIPELINE)
        pipeline.update(pipeline_overrides or {})
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, PIPELINE, pipeline)

    return _configure


def _segment(frame, **kwargs):
    return run_segment_detection(
        frame,
        reg=REG,
        suffix=f"{DAY}_0005",
        out_dir=kwargs.pop("out_dir", None),
        generate_validation_fig=True,
        cap_lo=CAP_LO,
        cap_hi=CAP_HI,
        **kwargs,
    )


def _utc(when) -> pd.Timestamp:
    """A segment time as an aware UTC instant (detectors return either form)."""
    ts = pd.Timestamp(when)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _at(hms: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY} {hms}", tz="UTC")


def _row_distance(seg: dict, mode: str, frame: pd.DataFrame) -> float:
    """The segment's ``Distance (km)`` cell, as the generator builds the row."""
    clean = {k: v for k, v in seg.items() if k not in constants._ANCHOR_PRIVATE_KEYS}
    row, _ = _seg_to_row(
        clean, mode, "https://data.example.org/api/legs/leg-1", [], [], frame, 0.0
    )
    return row[_row_col_index("Distance (km)")]


def test_a_trip_leaving_from_the_wake_up_row_starts_at_the_last_current_reading(
    configure,
):
    configure()
    frame = _leg()
    _, trips = _segment(frame)
    # The trip is 05:41:20 (the stand-still row before the first moving one) to
    # 06:37:14. Its start anchor is the reading at or before 05:41:20 once the
    # replayed one is gone: 36155.485 at 05:09:01, not 36131.13, so the distance
    # is 36204.725 - 36155.485 = 49.24 km instead of 73.595 km.
    assert len(trips) == 1
    trip = trips[0]
    assert _utc(trip["start_time"]) == _at("05:41:20")
    assert (trip["odo_start_km"], trip["odo_end_km"]) == (36155.485, 36204.725)
    assert _row_distance(trip, "discharge", frame) == 49.24


def test_a_trip_found_on_the_logger_speed_starts_at_the_last_current_reading(
    configure,
):
    configure(prefer_logger_speed=True)
    frame = _leg()
    _, trips = _segment(frame, logger_speed_df=_logger_speed())
    # The Logger trip is 05:42:00 - 06:41:00; its anchors are the readings at or
    # before 05:42:00 and at or after 06:41:00: 36155.485 and 36204.73, so the
    # distance is 49.245 km — the audited trip, reported as 73.60 km (36204.73 -
    # 36131.13) while the replayed reading was taken for its start.
    assert len(trips) == 1
    trip = trips[0]
    assert _utc(trip["start_time"]) == _at("05:42:00")
    assert (trip["odo_start_km"], trip["odo_end_km"]) == (36155.485, 36204.73)
    assert _row_distance(trip, "discharge", frame) == 49.245


def test_the_ep_diagnostics_measure_the_distance_window_on_the_same_readings(
    configure,
):
    configure(prefer_logger_speed=True)
    _, trips = _segment(_leg(), logger_speed_df=_logger_speed())
    # Odometer anchors 05:09:01 and 06:41:12 around the trip 05:42:00 - 06:41:00.
    # Interpolated, the counter moves 0.025 x 1979/2083 = 0.02375 km between the
    # start anchor and the trip start (05:09:01 -> 05:43:44 spans 2083 s), and
    # 0.005 x 12/238 = 0.00025 km between the trip end and the end anchor
    # (06:37:14 -> 06:41:12 spans 238 s): 0.024 km outside the trip — where the
    # replayed reading, taken as an anchor 40 s before the trip, put 6.77 km.
    assert trips[0]["ep_audit"]["dist_outside_km"] == pytest.approx(0.024, abs=1e-3)


def test_a_charge_starting_on_the_wake_up_row_covers_no_distance(configure):
    configure()
    #          time        speed  SOC    odometer    AC (Wh)    DC (Wh)
    plugged = [
        ("10:00:00", "0", "40", "36204.73", "5000000", "0"),
        ("10:30:00", NAN, "40", "36131.13", NAN, NAN),
        ("10:40:00", "0", "50", "36204.73", "5060000", "0"),
        ("10:50:00", "0", "60", NAN, "5120000", "0"),
        ("11:00:00", "0", "70", "36204.73", "5180000", "0"),
        ("11:10:00", "0", "70", "36204.73", "5180000", "0"),
    ]
    frame = pd.DataFrame(
        {
            TIME: [f"{DAY}T{r[0]}Z" for r in plugged],
            SPEED: [r[1] for r in plugged],
            SOC: [r[2] for r in plugged],
            ODO: [r[3] for r in plugged],
            AC: [r[4] for r in plugged],
            DC: [r[5] for r in plugged],
        },
        dtype=object,
    )
    charges, trips = _segment(frame)
    # The SOC rises 40 -> 70 from the 10:30 row: a 180 kWh AC charge. Its odometer
    # anchors are 36204.73 on both sides (36204.7 at the charge's 0.1 km
    # resolution), so the row reports no distance — not the 73.6 km the replayed
    # reading on its first row gave it.
    assert trips == []
    assert len(charges) == 1
    charge = charges[0]
    assert _utc(charge["start_time"]) == _at("10:30:00")
    assert (charge["odo_start_km"], charge["odo_end_km"]) == (36204.7, 36204.7)
    assert np.isnan(_row_distance(charge, "charge", frame))


@pytest.mark.parametrize(
    "min_trip_distance_km, kept",
    [(None, True), (50.0, False)],
    ids=["no filter", "50 km filter"],
)
def test_the_soc_branch_anchors_on_the_last_current_reading(
    configure, min_trip_distance_km, kept
):
    overrides = {"branch": "soc"}
    if min_trip_distance_km is not None:
        overrides["min_trip_distance_km"] = min_trip_distance_km
    configure(pipeline_overrides=overrides)
    rows = [list(r) for r in MORNING]
    rows[3][
        2
    ] = NAN  # no SOC at 05:43:44: the wake-up row is the last one before the fall
    _, trips = _segment(_leg([tuple(r) for r in rows]))
    # The SOC falls from 82 at 05:41:20 to 72 at 06:33:54: odometer anchors
    # 36155.485 (05:09:01) and 36204.345, a distance of 48.86 km — below a 50 km
    # minimum, where the replayed 36131.13 made it 73.215 km and kept the trip.
    if not kept:
        assert trips == []
        return
    assert len(trips) == 1
    trip = trips[0]
    assert _utc(trip["start_time"]) == _at("05:41:20")
    assert (trip["odo_start_km"], trip["odo_end_km"]) == (36155.485, 36204.345)


def test_the_callers_frame_is_left_as_it_was(configure):
    configure()
    frame = _leg()
    before = frame.copy(deep=True)
    _segment(frame)
    pd.testing.assert_frame_equal(frame, before)


def test_the_painter_is_handed_the_cleaned_frame(configure, tmp_path):
    configure()
    calls = []
    frame = _leg()
    _segment(frame, out_dir=tmp_path, figure_hook=lambda *a, **k: calls.append(a))
    painted = calls[0][0]
    at_wake_up = painted[TIME] == f"{DAY}T05:41:20Z"
    assert pd.to_numeric(painted.loc[at_wake_up, ODO], errors="coerce").isna().all()
    # ... while the caller's own frame still carries the reading.
    assert frame.loc[frame[TIME] == f"{DAY}T05:41:20Z", ODO].tolist() == ["36131.13"]
