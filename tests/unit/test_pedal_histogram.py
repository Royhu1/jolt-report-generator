"""Pedal-position peak extraction and histogram encoding.

``extract_pedal_events_by_rise_fall`` is a deterministic two-state machine
(SEEK_RISE / IN_EVENT). The synthetic series below is walked by hand in the
comments so the expected peaks are derived from the algorithm's definition, not
from a previous run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from report_generator import pedal_histogram as ph

#  idx   0  1   2   3   4   5  6   7   8   9  10
#  val   0  5  20  40  30  10  0  15  35  20   5
#
#  i=2  rise 20 >= delta_up(10)          -> IN_EVENT, peak 20
#  i=3  new peak 40
#  i=4  fall 40-30 = 10 >= delta_down(8) -> EVENT #1 at idx 3, value 40
#  i=6  local minimum drops to 0
#  i=7  rise 15 >= 10                    -> IN_EVENT, peak 15
#  i=8  new peak 35
#  i=9  fall 35-20 = 15 >= 8             -> EVENT #2 at idx 8, value 35
RISE_FALL_VALUES = [0, 5, 20, 40, 30, 10, 0, 15, 35, 20, 5]


def _series_frame(values, start="2025-06-27T08:00:00Z", freq="1s", col=ph.EEC2_COL):
    idx = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.DataFrame({"Time": idx, col: [float(v) for v in values]})


def test_extract_two_peaks_from_the_hand_walked_series():
    peaks = ph.extract_pedal_events_by_rise_fall(_series_frame(RISE_FALL_VALUES))
    assert list(peaks) == [1, 2]  # numbered in time order from 1
    assert peaks[1]["value"] == 40.0
    assert peaks[2]["value"] == 35.0
    assert peaks[1]["t"] < peaks[2]["t"]


def test_extract_peak_timestamps_point_at_the_peak_sample():
    frame = _series_frame(RISE_FALL_VALUES)
    peaks = ph.extract_pedal_events_by_rise_fall(frame)
    # The extractor takes ``.values`` off the time column, which drops the tz
    # and leaves a naive UTC instant — compare like for like.
    assert peaks[1]["t"] == frame["Time"].iloc[3].tz_convert(None)
    assert peaks[2]["t"] == frame["Time"].iloc[8].tz_convert(None)


def test_extract_sorts_by_time_before_walking():
    frame = _series_frame(RISE_FALL_VALUES).iloc[::-1].reset_index(drop=True)
    peaks = ph.extract_pedal_events_by_rise_fall(frame)
    assert [p["value"] for p in peaks.values()] == [40.0, 35.0]


def test_extract_no_event_when_the_rise_is_too_small():
    # Never rises by delta_up (10) above the running local minimum.
    peaks = ph.extract_pedal_events_by_rise_fall(_series_frame([0, 3, 6, 9, 6, 3, 0]))
    assert peaks == {}


def test_extract_no_event_when_the_fall_is_too_small():
    # Rises past the threshold but never falls back by delta_down (8).
    peaks = ph.extract_pedal_events_by_rise_fall(_series_frame([0, 20, 40, 60, 55]))
    assert peaks == {}


def test_extract_custom_thresholds():
    values = [0, 3, 6, 9, 6, 3, 0]
    assert ph.extract_pedal_events_by_rise_fall(_series_frame(values)) == {}
    peaks = ph.extract_pedal_events_by_rise_fall(
        _series_frame(values), delta_up=5.0, delta_down=2.0
    )
    assert [p["value"] for p in peaks.values()] == [9.0]


def test_extract_min_separation_suppresses_the_second_event():
    peaks = ph.extract_pedal_events_by_rise_fall(
        _series_frame(RISE_FALL_VALUES), min_separation=100
    )
    assert [p["value"] for p in peaks.values()] == [40.0]


def test_extract_min_width_suppresses_a_narrow_event():
    peaks = ph.extract_pedal_events_by_rise_fall(
        _series_frame(RISE_FALL_VALUES), min_width=100
    )
    assert peaks == {}


def test_extract_needs_two_samples():
    assert ph.extract_pedal_events_by_rise_fall(_series_frame([50])) == {}
    assert ph.extract_pedal_events_by_rise_fall(_series_frame([])) == {}


def test_extract_smoothing_removes_a_single_sample_spike():
    # A one-sample 90 % spike inside an otherwise flat pedal trace is an event
    # without smoothing and is removed by a 3-point rolling median.
    values = [10, 10, 10, 90, 10, 10, 10, 10]
    raw = ph.extract_pedal_events_by_rise_fall(_series_frame(values))
    smoothed = ph.extract_pedal_events_by_rise_fall(
        _series_frame(values), smooth_window=3
    )
    assert len(raw) == 1
    assert smoothed == {}


def test_extract_even_smoothing_window_is_ignored():
    values = [10, 10, 10, 90, 10, 10, 10, 10]
    assert (
        len(
            ph.extract_pedal_events_by_rise_fall(_series_frame(values), smooth_window=4)
        )
        == 1
    )


# ── peaks_histogram_string ───────────────────────────────────────────────────


def test_histogram_string_bins_peaks_into_ten_deciles():
    peaks = {1: {"value": 40.0}, 2: {"value": 35.0}}
    # 40 -> bin 4, 35 -> bin 3 (bin width 10 %).
    assert ph.peaks_histogram_string(peaks) == "0,0,0,1,1,0,0,0,0,0"


def test_histogram_string_clamps_the_top_of_the_range():
    # 100 % must land in the LAST bin, not overflow into an 11th.
    assert ph.peaks_histogram_string({1: {"value": 100.0}}) == "0,0,0,0,0,0,0,0,0,1"
    assert ph.peaks_histogram_string({1: {"value": 130.0}}) == "0,0,0,0,0,0,0,0,0,1"


def test_histogram_string_lower_edge_lands_in_the_first_bin():
    assert ph.peaks_histogram_string({1: {"value": 0.0}}) == "1,0,0,0,0,0,0,0,0,0"


def test_histogram_string_custom_bin_count():
    assert ph.peaks_histogram_string({1: {"value": 40.0}}, bins=4) == "0,1,0,0"


def test_histogram_string_counts_repeats():
    peaks = {i: {"value": 45.0} for i in range(1, 6)}
    assert ph.peaks_histogram_string(peaks) == "0,0,0,0,5,0,0,0,0,0"


def test_histogram_string_none_for_no_peaks():
    assert ph.peaks_histogram_string({}) is None


# ── compute_pedal_histogram (gates + input shapes) ───────────────────────────


def test_compute_pedal_histogram_from_a_series():
    idx = pd.date_range(
        "2025-06-27T08:00:00Z", periods=len(RISE_FALL_VALUES), freq="1s"
    )
    series = pd.Series([float(v) for v in RISE_FALL_VALUES], index=idx)
    assert ph.compute_pedal_histogram(series) == "0,0,0,1,1,0,0,0,0,0"


def test_compute_pedal_histogram_from_a_dataframe_column():
    frame = _series_frame(RISE_FALL_VALUES).set_index("Time")
    assert ph.compute_pedal_histogram(frame, value_col=ph.EEC2_COL) == (
        "0,0,0,1,1,0,0,0,0,0"
    )


def test_compute_pedal_histogram_defaults_to_the_first_column():
    frame = _series_frame(RISE_FALL_VALUES, col=ph.EBC1_COL).set_index("Time")
    assert ph.compute_pedal_histogram(frame) == "0,0,0,1,1,0,0,0,0,0"


def test_compute_pedal_histogram_sample_gate():
    assert ph.MIN_SAMPLES == 10
    idx = pd.date_range("2025-06-27T08:00:00Z", periods=9, freq="1s")
    short = pd.Series(np.linspace(0.0, 90.0, 9), index=idx)
    assert ph.compute_pedal_histogram(short) is None


def test_compute_pedal_histogram_sample_gate_applies_after_dropna():
    idx = pd.date_range("2025-06-27T08:00:00Z", periods=12, freq="1s")
    values = [float(v) for v in RISE_FALL_VALUES[:8]] + [np.nan] * 4
    assert ph.compute_pedal_histogram(pd.Series(values, index=idx)) is None


@pytest.mark.parametrize("bad", [None, "not a frame"])
def test_compute_pedal_histogram_rejects_unusable_input(bad):
    # Note: a SIZELESS object (e.g. an int) raises TypeError from the len() gate
    # rather than returning None. Both call sites in ``_seg_to_row`` wrap the
    # call in try/except, so that path is unreachable in production; it is not
    # asserted here so the suite does not pin an accident.
    assert ph.compute_pedal_histogram(bad) is None


def test_distance_gate_constant_is_the_documented_ten_kilometres():
    # _seg_to_row only computes a histogram for trips longer than this.
    assert ph.MIN_DISTANCE_FOR_PEDAL_KM == 10.0


def test_channel_column_names_match_the_logger_feed():
    assert ph.EEC2_COL == "EEC2 accelerator pedal position 1"
    assert ph.EBC1_COL == "EBC1 brake pedal position"
