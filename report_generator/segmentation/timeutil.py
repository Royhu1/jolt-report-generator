"""UTC timestamp helpers shared across the segmentation package.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from .constants import TIME_COL


def _to_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def frame_utc_date(df: pd.DataFrame, time_col: str = TIME_COL) -> dt.date | None:
    """The UTC date of the first valid timestamp of a raw telematics frame.

    This is the date a leg's date-effective vehicle settings are resolved for
    (:func:`report_generator.configs.effective_vehicle_config`). The generator and
    ``run_segment_detection`` derive it from the frame itself, so every caller
    handed the same frame resolves the same settings. "First" is in frame order;
    a naive timestamp counts as UTC. ``None`` when the frame has no parseable
    timestamp, and then no override applies.
    """
    if df is None or time_col not in df.columns:
        return None
    valid = pd.to_datetime(df[time_col], errors="coerce", utc=True).dropna()
    if valid.empty:
        return None
    return valid.iloc[0].date()
