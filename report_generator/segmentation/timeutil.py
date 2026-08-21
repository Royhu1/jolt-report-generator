"""UTC timestamp coercion helper shared across the segmentation package.

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

import pandas as pd


def _to_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
