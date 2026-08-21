"""UTC coercion in the two timestamp helpers the pipeline compares against.

Both helpers used to return an aware timestamp unchanged, so a non-UTC ``tzinfo``
could reach a comparison against a UTC-indexed series or a UTC-normalised leg
start. They now convert. Instants are preserved either way, so no report cell
moves — what changes is that the comparison can no longer be made against a
wall-clock reading of a different zone.
"""

from __future__ import annotations

import pandas as pd

from report_generator.operators import _resolve_operator_from_config
from report_generator.segmentation.timeutil import _to_utc


class _FakeLeg:
    """Minimal stand-in exposing only the ``start_time`` the resolver reads."""

    def __init__(self, start_time):
        self.start_time = start_time


# ── segmentation.timeutil._to_utc ────────────────────────────────────────────


def test_to_utc_localises_a_naive_timestamp():
    result = _to_utc(pd.Timestamp("2026-01-01 12:00:00"))
    assert str(result.tz) == "UTC"
    assert result == pd.Timestamp("2026-01-01 12:00:00", tz="UTC")


def test_to_utc_converts_an_aware_timestamp_preserving_the_instant():
    # 2026-07-01 is BST (UTC+1) in Europe/London.
    source = pd.Timestamp("2026-07-01 12:00:00", tz="Europe/London")
    result = _to_utc(source)
    assert str(result.tz) == "UTC"
    assert result == pd.Timestamp("2026-07-01 11:00:00", tz="UTC")
    assert result.value == source.value


def test_to_utc_leaves_a_utc_timestamp_unchanged():
    source = pd.Timestamp("2026-07-01 11:00:00", tz="UTC")
    result = _to_utc(source)
    assert str(result.tz) == "UTC"
    assert result == source


# ── operators._resolve_operator_from_config ──────────────────────────────────


def test_an_operator_window_is_read_as_absolute_instants():
    """An offset-bearing config bound resolves by instant, not wall clock.

    ``from`` below is 11:00 UTC written as 12:00+01:00, so a leg starting
    11:30 UTC is inside the window and 10:30 UTC is before it.
    """
    vehicles = {
        "TEST001": {
            "operators": [
                {"code": "OP_LATE", "from": "2026-07-01T12:00:00+01:00", "to": None},
            ]
        }
    }
    inside = _FakeLeg(pd.Timestamp("2026-07-01 11:30:00", tz="UTC"))
    before = _FakeLeg(pd.Timestamp("2026-07-01 10:30:00", tz="UTC"))

    assert _resolve_operator_from_config("TEST001", inside, vehicles) == "OP_LATE"
    assert _resolve_operator_from_config("TEST001", before, vehicles) is None


def test_the_window_upper_bound_stays_exclusive_across_offsets():
    vehicles = {
        "TEST002": {
            "operators": [
                {"code": "OP_EARLY", "from": None, "to": "2026-07-01T12:00:00+01:00"},
                {"code": "OP_LATE", "from": "2026-07-01T12:00:00+01:00", "to": None},
            ]
        }
    }
    # Exactly on the boundary instant (11:00 UTC) → the later window.
    on_bound = _FakeLeg(pd.Timestamp("2026-07-01 11:00:00", tz="UTC"))
    just_before = _FakeLeg(pd.Timestamp("2026-07-01 10:59:59", tz="UTC"))

    assert _resolve_operator_from_config("TEST002", on_bound, vehicles) == "OP_LATE"
    assert _resolve_operator_from_config("TEST002", just_before, vehicles) == "OP_EARLY"


def test_a_naive_leg_start_is_treated_as_utc():
    vehicles = {
        "TEST003": {
            "operators": [{"code": "OP", "from": "2026-07-01T00:00:00Z", "to": None}]
        }
    }
    naive = _FakeLeg(pd.Timestamp("2026-07-01 06:00:00"))
    assert _resolve_operator_from_config("TEST003", naive, vehicles) == "OP"
