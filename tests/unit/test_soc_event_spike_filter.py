"""The event-row SOC filter (``soc_event_spike_pct``), on synthetic frames.

Some telematics feeds send a periodic row (``trigger_type`` ``"TIMER"``) and, in
between, a row per event (ignition on, a change of charging status, …). On such
a feed an event row can carry a SOC a few points above the periodic readings on
both sides of it — typically the ignition-on row after the vehicle has stood with
the ignition off — and the charge detector reads that excursion as a phantom
charge. ``_blank_event_soc_spikes`` sets such a reading to NaN when it exceeds
both the nearest preceding and the nearest following valid periodic SOC by at
least the threshold; a pipeline turns it on with ``soc_event_spike_pct`` and
``run_segment_detection`` applies it before any detector reads the SOC.
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
