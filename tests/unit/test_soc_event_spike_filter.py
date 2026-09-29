"""The event-row SOC filter (``soc_event_spike_pct``), on synthetic frames.

Some telematics feeds send a periodic row (``trigger_type`` ``"TIMER"``) and, in
between, a row per event (ignition on, a change of charging status, …). On such
a feed an event row can carry a SOC a few points above the periodic readings on
both sides of it — typically the ignition-on row after the vehicle has stood with
the ignition off — and the charge detector reads that excursion as a phantom
charge. ``_blank_event_soc_spikes`` sets such a reading to NaN when it exceeds
both the nearest preceding and the nearest following valid periodic SOC by at
least the threshold — unless it looks like a genuine change of charge: the
periodic readings either side differ by at least its smaller excess and no
reading within two minutes of it lies the threshold below it, as at the end of a
charge the event rows carried, followed by driving or a parked drain. A pipeline
turns the filter on with ``soc_event_spike_pct`` and ``run_segment_detection``
applies it before any detector reads the SOC.
"""

from __future__ import annotations

import copy
import logging

import numpy as np
import pandas as pd
import pytest

from report_generator.segmentation import constants, detection
from report_generator.segmentation.detection import (
    _blank_event_soc_spikes,
    _filter_event_soc_spikes,
    run_segment_detection,
)

TIME = constants.TIME_COL
SOC = constants.SOC_COL
TRIGGER = constants.TRIGGER_TYPE_COL
DAY = "2026-07-25"
NAN = np.nan

# The documented phantom: parked with the ignition off, the periodic rows lose
# their SOC for a few minutes and the ignition-on row reports 64 between a 58
# before and a 60 after.
PARKED_IGNITION_ON = [
    ("18:06:31", "TIMER", 58),
    ("18:07:31", "TIMER", NAN),
    ("18:08:31", "TIMER", NAN),
    ("18:09:31", "TIMER", NAN),
    ("18:10:31", "TIMER", NAN),
    ("18:11:31", "IGNITION_ON", 64),
    ("18:12:31", "TIMER", 60),
]


def _frame(rows, *, trigger: bool = True) -> pd.DataFrame:
    """A telematics frame read the way production reads it: every value text.

    ``rows`` are ``(hh:mm:ss, trigger_type, soc)``; a NaN SOC is a missing
    value, as ``read_csv(dtype=str)`` gives it.
    """
    data = {
        TIME: [f"{DAY}T{hms}Z" for hms, _, _ in rows],
        SOC: [NAN if pd.isna(soc) else str(soc) for _, _, soc in rows],
    }
    if trigger:
        data[TRIGGER] = [kind for _, kind, _ in rows]
    return pd.DataFrame(data, dtype=object)


def _soc(frame: pd.DataFrame) -> list[float]:
    return pd.to_numeric(frame[SOC], errors="coerce").tolist()


def _blanked(before: pd.DataFrame, after: pd.DataFrame) -> list[str]:
    """The times (hh:mm:ss) whose SOC the filter removed, row by row."""
    was = pd.to_numeric(before[SOC], errors="coerce").to_numpy()
    now = pd.to_numeric(after[SOC], errors="coerce").to_numpy()
    return [
        str(when)[11:19]
        for when, old, new in zip(before[TIME].to_numpy(), was, now)
        if not np.isnan(old) and np.isnan(new)
    ]


# ── What is blanked ──────────────────────────────────────────────────────────


def test_the_ignition_on_excursion_after_a_parked_spell_is_blanked():
    frame = _frame(PARKED_IGNITION_ON)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    # 64 - 58 = 6 and 64 - 60 = 4, both >= 3; the missing periodic SOCs between
    # are skipped when looking for the nearest valid reading before it.
    assert n == 1
    assert _blanked(frame, cleaned) == ["18:11:31"]
    assert np.allclose(_soc(cleaned), [58, NAN, NAN, NAN, NAN, NAN, 60], equal_nan=True)


def test_a_rise_carried_by_event_rows_during_a_charge_is_kept():
    # A genuine charge persists into the next periodic reading, so no event row
    # stands above the reading after it.
    rows = [
        ("10:00:00", "TIMER", 40),
        ("10:03:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 43),
        ("10:06:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 45),
        ("10:10:00", "TIMER", 47),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 0
    assert cleaned is frame


def test_only_the_excursion_at_the_end_of_a_genuine_charge_is_blanked():
    # 51: +3 over the 48 before but +1 over the 50 after -> kept.
    # 52: +4 and +2 -> kept.  53: +5 and +3 -> blanked.
    rows = [
        ("12:24:27", "TIMER", 48),
        ("12:26:29", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 51),
        ("12:27:26", "IGNITION_ON", 52),
        ("12:27:27", "TRAILER_CONNECTED", 53),
        ("12:34:27", "TIMER", 50),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 1
    assert _blanked(frame, cleaned) == ["12:27:27"]


def test_several_event_rows_of_one_excursion_are_all_blanked():
    rows = [
        ("07:00:00", "TIMER", 70),
        ("07:04:10", "IGNITION_ON", 75),
        ("07:04:11", "DRIVER_LOGIN", 75),
        ("07:05:00", "TIMER", 71),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 2
    assert _blanked(frame, cleaned) == ["07:04:10", "07:04:11"]


# ── What is never blanked ────────────────────────────────────────────────────


def test_a_periodic_reading_is_never_blanked():
    rows = [
        ("09:00:00", "TIMER", 50),
        ("09:01:00", "TIMER", 58),
        ("09:02:00", "TIMER", 50),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 0
    assert cleaned is frame


def test_an_excursion_below_the_threshold_on_one_side_is_kept():
    rows = [
        ("09:00:00", "TIMER", 58),
        ("09:00:30", "IGNITION_ON", 64),  # +6 before, only +2 after
        ("09:01:00", "TIMER", 62),
    ]
    cleaned, n = _blank_event_soc_spikes(_frame(rows), 3.0)
    assert n == 0


@pytest.mark.parametrize("threshold, expected", [(3.0, 1), (3.5, 0), (2, 1)])
def test_the_threshold_is_inclusive(threshold, expected):
    rows = [
        ("09:00:00", "TIMER", 60),
        ("09:00:30", "IGNITION_ON", 63),  # exactly +3 on both sides
        ("09:01:00", "TIMER", 60),
    ]
    assert _blank_event_soc_spikes(_frame(rows), threshold)[1] == expected


def test_a_reading_below_its_neighbours_is_kept():
    rows = [
        ("09:00:00", "TIMER", 58),
        ("09:00:30", "IGNITION_ON", 50),
        ("09:01:00", "TIMER", 60),
    ]
    assert _blank_event_soc_spikes(_frame(rows), 3.0)[1] == 0


@pytest.mark.parametrize(
    "rows",
    [
        [("06:00:00", "IGNITION_ON", 80), ("06:01:00", "TIMER", 70)],
        [("06:00:00", "TIMER", 70), ("06:01:00", "IGNITION_OFF", 80)],
    ],
    ids=["before-the-first-periodic-reading", "after-the-last-periodic-reading"],
)
def test_an_event_row_without_a_periodic_reading_on_both_sides_is_kept(rows):
    assert _blank_event_soc_spikes(_frame(rows), 3.0)[1] == 0


def test_a_zero_periodic_soc_is_not_a_reference():
    # The detectors read a zero SOC as missing, so the nearest valid reading
    # before the event row is the 63, not the 0: +1, kept. Taking the 0 as a
    # reference would have blanked it (+64 and +3).
    rows = [
        ("08:00:00", "TIMER", 63),
        ("08:05:00", "TIMER", 0),
        ("08:06:00", "IGNITION_ON", 64),
        ("08:07:00", "TIMER", 61),
    ]
    assert _blank_event_soc_spikes(_frame(rows), 3.0)[1] == 0


def test_a_row_without_a_timestamp_is_left_alone():
    rows = PARKED_IGNITION_ON + [("99:99:99", "IGNITION_ON", 90)]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 1  # the ignition-on excursion only
    assert pd.to_numeric(cleaned[SOC], errors="coerce").iloc[-1] == 90


# ── A genuine change of charge ───────────────────────────────────────────────

# The excursion a charge ends on at 2 points: 69 between periodic readings of 67,
# the rows either side of it already back at 67.
TWO_POINT_EXCURSION = [
    ("14:05:55", "TIMER", 67),
    ("14:06:55", "TIMER", NAN),
    ("14:07:23", "IGNITION_ON", 69),
    ("14:08:06", "IGNITION_OFF", 67),
    ("14:08:46", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 67),
    ("14:09:46", "TIMER", 67),
]

# A charge whose rise the event rows carry while the periodic rows are silent,
# ending at 60; the vehicle then stands, the battery drains, and the first
# periodic reading after the ignition-on is 57.
CHARGE_THEN_PARKED_DRAIN = [
    ("08:00:00", "TIMER", 40),
    ("09:00:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 45),
    ("09:30:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 50),
    ("10:00:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 55),
    ("10:30:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 60),
    ("16:00:00", "IGNITION_ON", 57),
    ("16:01:00", "TIMER", 57),
]

# A charge carried by event rows ends at 84 and the vehicle drives off: the level
# holds for minutes, falling to 82 by the next periodic reading.
CHARGE_THEN_DRIVE_OFF = [
    ("10:40:19", "TIMER", 59),
    ("11:12:13", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 84),
    ("11:12:19", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 84),
    ("11:13:39", "MOVEMENT", 84),
    ("11:14:02", "DRIVER_1_WORKING_STATE_CHANGED", 84),
    ("11:16:02", "DRIVER_1_WORKING_STATE_CHANGED", 83),
    ("11:20:19", "TIMER", 82),
]


@pytest.mark.parametrize("threshold, expected", [(2.0, ["14:07:23"]), (3.0, [])])
def test_a_two_point_excursion_is_blanked_at_two_points_only(threshold, expected):
    frame = _frame(TWO_POINT_EXCURSION)
    cleaned, n = _blank_event_soc_spikes(frame, threshold)
    assert n == len(expected)
    assert _blanked(frame, cleaned) == expected


@pytest.mark.parametrize("threshold", [2.0, 3.0])
def test_the_end_of_a_charge_followed_by_a_parked_drain_is_kept(threshold):
    # 60 is 20 above the 40 before and 3 above the 57 after, but the two
    # periodic readings differ by 17 (the level moved: a charge) and nothing
    # within two minutes of 10:30 reads 58 or less (the level held).
    frame = _frame(CHARGE_THEN_PARKED_DRAIN)
    cleaned, n = _blank_event_soc_spikes(frame, threshold)
    assert n == 0
    assert cleaned is frame


def test_the_end_of_a_charge_followed_by_driving_is_kept():
    # 84 is 25 above the 59 before and 2 above the 82 after; the 83 two minutes
    # after the last 84 is only 1 below it.
    assert _blank_event_soc_spikes(_frame(CHARGE_THEN_DRIVE_OFF), 2.0)[1] == 0


def test_an_excursion_right_after_a_charge_is_blanked():
    # The level moved between the periodic readings (45 -> 52, a charge), but the
    # row a second before the 54 already reads 52: the 54 is stale.
    rows = [
        ("11:04:56", "TIMER", 45),
        ("11:13:45", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 52),
        ("11:14:32", "DRIVER_2_WORKING_STATE_CHANGED", 52),
        ("11:14:33", "TRAILER_CONNECTED", 54),
        ("11:14:56", "TIMER", 52),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 2.0)
    assert n == 1
    assert _blanked(frame, cleaned) == ["11:14:33"]
    # Only 2 above the reading after it: below a 3-point threshold.
    assert _blank_event_soc_spikes(frame, 3.0)[1] == 0


@pytest.mark.parametrize(
    "contradiction_at, expected",
    [("10:32:00", 1), ("10:32:01", 0), ("10:28:00", 1), ("10:27:59", 0)],
    ids=["2-min-after", "just-beyond-after", "2-min-before", "just-beyond-before"],
)
def test_a_reading_contradicts_within_two_minutes_either_side_bounds_included(
    contradiction_at, expected
):
    # The 60 of a charge followed by a drain, and one reading 2 below it at
    # ``contradiction_at``: within the window the 60 is stale, outside it the
    # level held.
    rows = [
        ("08:00:00", "TIMER", 40),
        ("10:30:00", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 60),
        (contradiction_at, "IGNITION_ON", 58),
        ("16:00:00", "TIMER", 57),
    ]
    assert _blank_event_soc_spikes(_frame(rows), 2.0)[1] == expected


@pytest.mark.parametrize("threshold", [2.0, 3.0])
def test_an_excursion_that_comes_back_is_blanked_however_far_its_neighbours(
    threshold,
):
    # Ten-minute periodic readings and no other row near the ignition-on: the
    # 70 has nothing within two minutes to contradict it, but the SOC comes back
    # to 67, where it was.
    rows = [
        ("10:43:12", "TIMER", 67),
        ("10:47:19", "IGNITION_ON", 70),
        ("10:53:12", "TIMER", 67),
    ]
    assert _blank_event_soc_spikes(_frame(rows), threshold)[1] == 1


def test_at_two_points_the_reading_that_rose_with_the_charge_stays():
    # The end of a genuine charge at 2 points: 52 is 4 above the 48 before and 2
    # above the 50 after, on a level that moved by 2 between them, and nothing
    # within two minutes of it reads 50 or less, so it stays; 53 is 5 and 3
    # above them, which are closer to each other than to it: blanked.
    rows = [
        ("12:24:27", "TIMER", 48),
        ("12:26:29", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 51),
        ("12:27:26", "IGNITION_ON", 52),
        ("12:27:27", "TRAILER_CONNECTED", 53),
        ("12:34:27", "TIMER", 50),
    ]
    frame = _frame(rows)
    cleaned, n = _blank_event_soc_spikes(frame, 2.0)
    assert n == 1
    assert _blanked(frame, cleaned) == ["12:27:27"]


def test_a_top_up_that_falls_back_to_the_level_before_it_reads_as_an_excursion():
    # Known limit: SOC values alone cannot tell a 2-point top-up that the
    # vehicle has used again by the next periodic reading from a stale reading;
    # the level comes back to where it was, so both readings at 100 go.
    rows = [
        ("12:03:06", "TIMER", 98),
        ("12:06:31", "BATTERY_PACK_CHARGING_STATUS_CHANGE", 100),
        ("12:14:44", "IGNITION_ON", 100),
        ("12:40:00", "TIMER", 98),
    ]
    assert _blank_event_soc_spikes(_frame(rows), 2.0)[1] == 2


# ── How the rows are read ────────────────────────────────────────────────────


def test_neighbours_are_found_in_time_order_not_row_order():
    frame = _frame(PARKED_IGNITION_ON)
    shuffled = frame.iloc[[5, 0, 6, 2, 4, 1, 3]].reset_index(drop=True)
    cleaned, n = _blank_event_soc_spikes(shuffled, 3.0)
    assert n == 1
    assert _blanked(shuffled, cleaned) == ["18:11:31"]


def test_the_result_maps_back_onto_any_index():
    frame = _frame(PARKED_IGNITION_ON)
    frame.index = [7, 7, 3, 3, 9, 9, 1]  # unsorted and duplicated labels
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 1
    assert list(cleaned.index) == list(frame.index)
    assert _blanked(frame, cleaned) == ["18:11:31"]


def test_a_row_without_a_trigger_type_counts_as_an_event_row():
    rows = [
        ("09:00:00", "TIMER", 58),
        ("09:00:30", NAN, 64),
        ("09:01:00", "TIMER", 60),
    ]
    assert _blank_event_soc_spikes(_frame(rows), 3.0)[1] == 1


def test_the_periodic_trigger_is_matched_without_regard_to_case_or_padding():
    rows = [
        ("09:00:00", " timer", 58),
        ("09:00:30", "IGNITION_ON", 64),
        ("09:01:00", "Timer ", 60),
    ]
    assert _blank_event_soc_spikes(_frame(rows), 3.0)[1] == 1


def test_a_frame_without_a_trigger_type_column_is_returned_as_it_is():
    frame = _frame(PARKED_IGNITION_ON, trigger=False)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 0
    assert cleaned is frame


def test_the_callers_frame_is_never_modified():
    frame = _frame(PARKED_IGNITION_ON)
    before = frame.copy(deep=True)
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 1
    assert cleaned is not frame
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize(
    "values, dtype",
    [
        (["58", "64", "60"], object),  # the production read: text
        ([58.0, 64.0, 60.0], "float64"),
        ([58, 64, 60], object),  # an integer column cannot hold NaN
    ],
    ids=["text", "float", "integer"],
)
def test_the_soc_column_keeps_a_type_that_holds_the_blank(values, dtype):
    frame = pd.DataFrame(
        {
            TIME: [f"{DAY}T09:00:00Z", f"{DAY}T09:00:30Z", f"{DAY}T09:01:00Z"],
            SOC: values,
            TRIGGER: ["TIMER", "IGNITION_ON", "TIMER"],
        }
    )
    cleaned, n = _blank_event_soc_spikes(frame, 3.0)
    assert n == 1
    assert cleaned[SOC].dtype == dtype
    assert pd.isna(cleaned[SOC].iloc[1])
    assert pd.to_numeric(cleaned[SOC]).iloc[[0, 2]].tolist() == [58, 60]


# ── The pipeline switch ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, -3, "3", True, None])
def test_a_threshold_that_is_not_a_positive_number_is_refused(value):
    with pytest.raises(ValueError, match="soc_event_spike_pct must be a positive"):
        _filter_event_soc_spikes(_frame(PARKED_IGNITION_ON), value, "p", "R", "s")


def test_a_feed_without_trigger_type_is_logged_once_per_vehicle(monkeypatch, caplog):
    monkeypatch.setattr(detection, "_NO_TRIGGER_TYPE_LOGGED", set())
    frame = _frame(PARKED_IGNITION_ON, trigger=False)
    with caplog.at_level(logging.INFO, logger=detection.logger.name):
        for suffix in ("leg1", "leg2", "leg3"):
            assert _filter_event_soc_spikes(frame, 3, "p", "UTSPK01", suffix) is frame
        _filter_event_soc_spikes(frame, 3, "p", "UTSPK02", "leg1")
    notes = [r.getMessage() for r in caplog.records if "trigger_type" in r.getMessage()]
    assert len(notes) == 2
    assert "UTSPK01" in notes[0] and "UTSPK02" in notes[1]


def test_the_number_of_readings_blanked_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger=detection.logger.name):
        _filter_event_soc_spikes(_frame(PARKED_IGNITION_ON), 3, "p", "UTSPK01", "leg")
    assert any(
        "1 event-row SOC readings" in r.getMessage() and "UTSPK01 leg" in r.getMessage()
        for r in caplog.records
    )


# ── Through run_segment_detection ────────────────────────────────────────────

REG = "UTSPK01"
PIPELINE = "ut_spike_soc"
_PIPELINE = {
    "branch": "soc",
    "charge_params": {
        "plateau_window_min": 60,
        "min_soc_rise": 5.0,
        "min_energy_kwh": 5.0,
    },
    "discharge_params": {
        "plateau_window_min": 15,
        "soc_rise_abort_pct": 3.0,
        "min_soc_drop": 5.0,
        "min_energy_kwh": 2.0,
    },
}


def _leg() -> pd.DataFrame:
    """A parked afternoon: steady 58, the ignition-on excursion, then 60."""
    rows = [(f"18:0{m}:31", "TIMER", 58) for m in range(0, 7)]
    rows += PARKED_IGNITION_ON[1:]
    rows += [(f"18:{m}:31", "TIMER", 60) for m in range(13, 20)]
    return _frame(rows)


@pytest.fixture
def configure(monkeypatch):
    """Inject a SOC-branch vehicle whose pipeline may carry the threshold."""
    monkeypatch.setattr(detection, "_NO_TRIGGER_TYPE_LOGGED", set())
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        REG,
        {
            "srf_reg": REG,
            "nominal_kwh": 624,
            "effective_capacity_kwh": 417.0,
            "pipeline": PIPELINE,
            "split_by_mass": False,
        },
    )

    def _configure(spike_pct=None):
        pipeline = copy.deepcopy(_PIPELINE)
        if spike_pct is not None:
            pipeline["soc_event_spike_pct"] = spike_pct
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, PIPELINE, pipeline)

    return _configure


def _segment(frame, out_dir=None, figure_hook=None):
    return run_segment_detection(
        frame,
        reg=REG,
        suffix=f"{DAY}_0000",
        out_dir=out_dir,
        generate_validation_fig=True,
        cap_lo=312.0,
        cap_hi=1248.0,
        figure_hook=figure_hook,
    )


def test_without_the_key_the_excursion_is_a_phantom_charge(configure):
    configure(None)
    charges, _ = _segment(_leg())
    # 58 -> 64 is a +6 rise (>= min_soc_rise 5): a charge of 6 % x 417 kWh.
    assert len(charges) == 1
    assert charges[0]["delta_soc_pct"] == 6.0
    assert charges[0]["energy_source"] == "soc_estimate"


def test_with_the_key_the_phantom_charge_is_gone(configure):
    configure(3)
    charges, discharges = _segment(_leg())
    # 58 -> 60 is +2 once the excursion is blanked: no charge at all.
    assert charges == []
    assert discharges == []


def test_the_callers_frame_is_left_as_it_was(configure):
    configure(3)
    frame = _leg()
    before = frame.copy(deep=True)
    _segment(frame)
    pd.testing.assert_frame_equal(frame, before)


def test_the_painter_is_handed_the_cleaned_frame(configure, tmp_path):
    configure(3)
    calls = []
    frame = _leg()
    _segment(frame, out_dir=tmp_path, figure_hook=lambda *a, **k: calls.append(a))
    painted = calls[0][0]
    at_ignition = painted[TIME] == f"{DAY}T18:11:31Z"
    assert pd.to_numeric(painted.loc[at_ignition, SOC], errors="coerce").isna().all()
    # ... while the caller's own frame still carries the reading.
    assert frame.loc[frame[TIME] == f"{DAY}T18:11:31Z", SOC].tolist() == ["64"]


def test_without_a_trigger_type_column_the_key_changes_nothing(configure, serialise):
    frame = _leg().drop(columns=[TRIGGER])
    configure(None)
    without_key = _segment(frame)
    configure(3)
    with_key = _segment(frame)
    assert serialise(with_key[0]) == serialise(without_key[0])
    assert serialise(with_key[1]) == serialise(without_key[1])
    assert len(with_key[0]) == 1  # the phantom stays: nothing to judge it by


def test_an_invalid_threshold_supplied_at_run_time_is_refused(configure):
    configure(-1)
    with pytest.raises(ValueError, match="ut_spike_soc.*soc_event_spike_pct"):
        _segment(_leg())


def test_at_two_points_the_phantom_charge_is_gone_too(configure):
    configure(2)
    assert _segment(_leg()) == ([], [])


def _clock(start: str, minutes: int) -> str:
    """``start`` (hh:mm:ss) plus ``minutes``, as hh:mm:ss."""
    when = pd.Timestamp(f"{DAY}T{start}") + pd.Timedelta(minutes=minutes)
    return when.strftime("%H:%M:%S")


def _charge_ending_on_the_excursion() -> pd.DataFrame:
    """A charge from 29 % to 67 % on periodic rows, 2 points a minute; the SOC
    stays at 67 % until the ignition-on row reports 69 %, and the rows after it
    read 67 % again."""
    rows = [(_clock("13:00:55", m), "TIMER", 29 + 2 * m) for m in range(20)]
    rows += [(_clock("13:00:55", m), "TIMER", 67) for m in range(20, 65)]
    rows += TWO_POINT_EXCURSION
    rows += [(_clock("14:10:46", m), "TIMER", 67) for m in range(10)]
    return _frame(rows)


@pytest.mark.parametrize(
    "spike_pct, end_time, end_soc",
    [(3, "14:07:23", 69.0), (2, "13:19:55", 67.0)],
    ids=["3-points-ends-on-the-excursion", "2-points-ends-on-the-charge"],
)
def test_a_charge_ending_on_a_two_point_excursion(
    configure, spike_pct, end_time, end_soc
):
    configure(spike_pct)
    charges, discharges = _segment(_charge_ending_on_the_excursion())
    assert discharges == []
    assert len(charges) == 1
    charge = charges[0]
    assert (charge["start_soc"], charge["end_soc"]) == (29.0, end_soc)
    assert charge["end_time"] == pd.Timestamp(f"{DAY}T{end_time}Z")
    assert charge["energy_source"] == "soc_estimate"
    # ΔSOC x the vehicle's 417 kWh: 40 points on the excursion, 38 without it.
    assert charge["delta_soc_pct"] == end_soc - 29.0
    assert charge["delta_energy_kwh"] == pytest.approx((end_soc - 29.0) / 100 * 417.0)


def test_a_charge_carried_by_event_rows_keeps_its_end_despite_a_parked_drain(
    configure,
):
    # At 2 points the rule alone would blank the 60 that ends the charge — 3
    # above the periodic reading after the parked spell — and cut the charge
    # short at 55; the level moved and held, so the whole 40 -> 60 stays.
    configure(2)
    rows = [(_clock("07:54:00", m), "TIMER", 40) for m in range(6)]
    rows += CHARGE_THEN_PARKED_DRAIN
    rows += [(_clock("16:02:00", m), "TIMER", 57) for m in range(8)]
    charges, discharges = _segment(_frame(rows))
    assert discharges == []  # the 3-point drain is below the 5-point floor
    assert len(charges) == 1
    charge = charges[0]
    assert (charge["start_soc"], charge["end_soc"]) == (40.0, 60.0)
    assert charge["start_time"] == pd.Timestamp(f"{DAY}T08:00:00Z")
    assert charge["end_time"] == pd.Timestamp(f"{DAY}T10:30:00Z")
    assert charge["delta_energy_kwh"] == pytest.approx(0.20 * 417.0)
