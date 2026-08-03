"""
pedal_histogram.py
==================
Pedal-position histogram computation tool.

Extracts peak events from the Logger's EEC2 (accelerator-pedal position) and
EBC1 (brake-pedal position) channels, then encodes the peak distribution as a
comma-separated histogram string.

Algorithm provenance: the legacy JOLT codebase jolt_utils.py, migrated to the
jolt_toolkit unified pipeline.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Logger EEC2 / EBC1 channel column names ────────────────────────────────
EEC2_COL = "EEC2 accelerator pedal position 1"
EBC1_COL = "EBC1 brake pedal position"

# Compute the pedal histogram only for discharge segments with a driving distance > 10 km
MIN_DISTANCE_FOR_PEDAL_KM = 10.0

# Minimum number of samples (below this, the data is deemed insufficient and no histogram is computed)
MIN_SAMPLES = 10


def extract_pedal_events_by_rise_fall(
    df: pd.DataFrame,
    time_col: str = "Time",
    value_col: str = EEC2_COL,
    delta_up: float = 10.0,
    delta_down: float = 8.0,
    smooth_window: int = 0,
    min_width: int = 1,
    min_separation: int = 1,
) -> dict:
    """
    Rise/fall threshold-based pedal event detection.

    Event definition: from a local minimum, the value rises by at least delta_up,
    then falls by at least delta_down from the peak within that segment.

    Args:
        df:              DataFrame containing a time column and a pedal-position column
        time_col:        time column name
        value_col:       pedal-position column name (percentage, 0-100)
        delta_up:        rise-detection threshold (percentage points)
        delta_down:      fall-detection threshold (percentage points)
        smooth_window:   optional: rolling-median denoising window (odd number, 0 = no denoising)
        min_width:       optional: minimum event duration in samples
        min_separation:  optional: minimum separation between events in samples

    Returns:
        peaks: {1: {"t": peak timestamp, "value": peak value}, 2: {...}, ...}
    """
    s = df.copy()
    s[time_col] = pd.to_datetime(s[time_col])
    s = s.sort_values(time_col).reset_index(drop=True)

    y = s[value_col].astype(float).values.copy()
    t = s[time_col].values

    if len(y) < 2:
        return {}

    # Optional denoising (usually unnecessary for 1 Hz data; 3 or 5 recommended when there are spikes)
    if smooth_window and smooth_window >= 3 and smooth_window % 2 == 1:
        y = (
            pd.Series(y)
            .rolling(smooth_window, center=True)
            .median()
            .bfill()
            .ffill()
            .values
        )

    # State machine
    SEEK_RISE, IN_EVENT = 0, 1
    state = SEEK_RISE

    # Trackers
    last_min_val = y[0]
    last_min_idx = 0
    peak_val = y[0]
    peak_idx = 0
    last_event_end_idx = -(10**9)

    events = []
    for i in range(1, len(y)):
        val = y[i]

        if state == SEEK_RISE:
            # Update the local minimum
            if val < last_min_val:
                last_min_val = val
                last_min_idx = i

            # Whether the rise threshold is met
            if val - last_min_val >= delta_up:
                state = IN_EVENT
                peak_val = val
                peak_idx = i

        elif state == IN_EVENT:
            # Track the peak
            if val > peak_val:
                peak_val = val
                peak_idx = i

            # Whether the fall threshold is met (relative to this event's peak)
            if peak_val - val >= delta_down:
                # Minimum-width & event-separation checks
                if (peak_idx - last_min_idx + 1) >= min_width and (
                    i - last_event_end_idx
                ) >= min_separation:
                    events.append((peak_idx, peak_val))
                    last_event_end_idx = i

                # Reset to seek the next rise: count the current point as the new "local minimum"
                state = SEEK_RISE
                last_min_val = val
                last_min_idx = i

    # Assemble the dict (numbered in time order)
    peaks = {}
    for k, (idx, pval) in enumerate(events, start=1):
        peaks[k] = {"t": pd.Timestamp(t[idx]), "value": float(pval)}
    return peaks


def peaks_histogram_string(peaks: dict, bins: int = 10) -> str | None:
    """
    Encode the detected peak distribution as a comma-separated histogram string.

    Bins the 0-100% peaks into ``bins`` intervals and counts them.

    Args:
        peaks: the peaks dict returned by extract_pedal_events_by_rise_fall
        bins:  number of histogram bins (divides 0-100% into ``bins`` intervals)

    Returns:
        A comma-separated count string, e.g. "2,5,8,12,15,18,14,11,8,3"; returns None when there are no peaks
    """
    if not peaks:
        return None

    v = [0] * bins
    bin_width = 100.0 / bins

    for event in peaks.values():
        if event and "value" in event:
            bin_index = min(int(event["value"] / bin_width), bins - 1)
            v[bin_index] += 1

    return ",".join(map(str, v))


def compute_pedal_histogram(
    pedal_series: pd.Series | pd.DataFrame,
    value_col: str | None = None,
) -> str | None:
    """
    Convenience function: compute the histogram string from a pedal-position time series.

    Accepts a Series or DataFrame indexed by timestamp:
    - Series: uses the values directly
    - DataFrame: uses the column specified by value_col

    Args:
        pedal_series: pedal-position data (percentage, 0-100), indexed by datetime
        value_col:    column name to use when a DataFrame

    Returns:
        The histogram string or None
    """
    if pedal_series is None or len(pedal_series) < MIN_SAMPLES:
        return None

    # Normalise to DataFrame format
    if isinstance(pedal_series, pd.Series):
        df = pedal_series.dropna().reset_index()
        df.columns = ["Time", "value"]
        col = "value"
    elif isinstance(pedal_series, pd.DataFrame):
        if value_col is None:
            # Take the first non-index column
            value_col = pedal_series.columns[0]
        df = pedal_series[[value_col]].dropna().reset_index()
        df.columns = ["Time", value_col]
        col = value_col
    else:
        return None

    if len(df) < MIN_SAMPLES:
        return None

    peaks = extract_pedal_events_by_rise_fall(df, time_col="Time", value_col=col)
    return peaks_histogram_string(peaks)
