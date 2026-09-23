"""UTC coercion in the two timestamp helpers the pipeline compares against, and
in the discharge-segment merge that consumes the segmentation one.

Both helpers used to return an aware timestamp unchanged, so a non-UTC ``tzinfo``
could reach a comparison against a UTC-indexed series or a UTC-normalised leg
start. They now convert. Instants are preserved either way, so no report cell
moves — what changes is that the comparison can no longer be made against a
wall-clock reading of a different zone.
"""

from __future__ import annotations

import pandas as pd

from report_generator.operators import _resolve_operator_from_config
from report_generator.segmentation.mass_clustering import _merge_two_discharge_segs
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


def test_a_merge_normalises_mixed_zone_endpoints_to_utc():
    """Merged endpoints and their private anchors come out as UTC instants.

    ``seg_a`` is written in Europe/London (BST, UTC+1 on this date) and ``seg_b``
    in UTC; ``seg_b`` is the earlier segment. The merge must compare and emit
    instants, not wall-clock readings of two different zones.
    """
    seg_a = {
        "start_time": pd.Timestamp("2026-07-01 12:00:00", tz="Europe/London"),
        "end_time": pd.Timestamp("2026-07-01 12:30:00", tz="Europe/London"),
        "start_soc": 80.0,
        "end_soc": 70.0,
        "delta_energy_kwh": -10.0,
        "energy_source": "total_energy",
        "_anchor_start_time": pd.Timestamp("2026-07-01 11:55:00", tz="Europe/London"),
        "_anchor_end_time": pd.Timestamp("2026-07-01 12:35:00", tz="Europe/London"),
    }
    seg_b = {
        "start_time": pd.Timestamp("2026-07-01 11:35:00", tz="UTC"),
        "end_time": pd.Timestamp("2026-07-01 12:00:00", tz="UTC"),
        "start_soc": 70.0,
        "end_soc": 60.0,
        "delta_energy_kwh": -10.0,
        "energy_source": "total_energy",
        "_anchor_start_time": pd.Timestamp("2026-07-01 11:30:00", tz="UTC"),
        "_anchor_end_time": pd.Timestamp("2026-07-01 12:05:00", tz="UTC"),
    }

    result = _merge_two_discharge_segs(seg_a, seg_b)

    # seg_a starts 12:00 BST = 11:00 UTC; seg_b ends 12:00 UTC.
    assert result["start_time"] == pd.Timestamp("2026-07-01 11:00:00", tz="UTC")
    assert result["end_time"] == pd.Timestamp("2026-07-01 12:00:00", tz="UTC")
    assert str(result["start_time"].tz) == "UTC"
    assert str(result["end_time"].tz) == "UTC"
    # seg_a's anchor starts 11:55 BST = 10:55 UTC; seg_b's anchor ends 12:05 UTC.
    assert result["_anchor_start_time"] == pd.Timestamp("2026-07-01 10:55:00", tz="UTC")
    assert result["_anchor_end_time"] == pd.Timestamp("2026-07-01 12:05:00", tz="UTC")


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
