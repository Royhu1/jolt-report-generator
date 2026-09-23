"""The EP-confidence grade must never cost a report.

The grade is a commentary on the numbers, not one of them. Everything it reads —
raw timestamps, counter anchors, SOC traces — comes from the same telematics that
routinely contains values nothing can parse, and ``_to_ns`` forms a
``pd.Timestamp`` from them unguarded. An exception anywhere in the audit or
grading path must therefore cost the two confidence cells and nothing else:
never the row, never the other rows' grades, and never the report.

Each test here injects a failure at one of the four call sites and asserts what
survives it. The segmentation cases run under the frozen ``EVSPD01`` alias config
(speed branch), injected by the ``frozen_configs`` fixture, never a live vehicle.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from report_generator import _generator as gen_mod
from report_generator import ep_confidence as epc
from report_generator import row_builder
from report_generator._generator import JOLTReportGenerator
from report_generator.columns import HEADERS, _row_col_index
from report_generator.ep_confidence import CONF_GOOD, CONF_POOR
from report_generator.segmentation import detection

I_CONF = _row_col_index("EP Confidence", HEADERS)
I_REASON = _row_col_index("EP Confidence Reason", HEADERS)


class _Boom(Exception):
    """Distinct from anything the pipeline raises, so a leak is unmistakable."""


# =============================================================================
# 1. The measurement, in the segmentation layer
# =============================================================================
def _driving_frame():
    """One four-hour leg with a two-hour trip in it — enough for one segment."""
    t0 = pd.Timestamp("2026-01-05 06:00:00", tz="UTC")
    n = 240
    speed = np.zeros(n)
    speed[30:150] = 70.0
    km = np.cumsum(speed) / 60.0
    return pd.DataFrame(
        {
            "eventDatetime": [t0 + pd.Timedelta(minutes=i) for i in range(n)],
            "electricBatteryLevelPercent": 90.0 - km * 0.25,
            "wheel_based_speed": speed,
            "odometer": 1000.0 + km,
            "total_electric_energy_used_plugged_in_included": km * 1200.0,
            "electric_energy_wheelbased_speed_over_zero": km * 1200.0,
            "gross_combination_vehicle_weight": np.full(n, 30_000.0),
        }
    )


def test_segmentation_still_returns_its_segments_when_the_audit_fails(
    monkeypatch, frozen_configs
):
    def _half_then_raise(discharge_segs, *args, **kwargs):
        # Fail the way a real one would: partway through, with some segments
        # already written.
        for seg in discharge_segs:
            seg["ep_audit"] = {"dup_ratio": 2.0}
        raise _Boom("unparseable timestamp")

    monkeypatch.setattr(detection, "attach_ep_audits", _half_then_raise)

    _charge, discharge = detection.run_segment_detection(
        _driving_frame(), "EVSPD01", "2026-01-05", generate_validation_fig=False
    )

    assert len(discharge) == 1  # the segmentation result is untouched
    assert discharge[0]["energy_source"] == "total_energy"
    assert discharge[0]["delta_energy_kwh"] == pytest.approx(-168.0)
    # Half-measured diagnostics are removed rather than left to drive a grade:
    # the dup_ratio above would otherwise have graded this sound row poor.
    assert "ep_audit" not in discharge[0]


def test_a_working_audit_still_reaches_the_segment(frozen_configs):
    # The control for the test above: without an injected failure the audit is
    # attached as usual, so the assertion there is about the failure path only.
    _charge, discharge = detection.run_segment_detection(
        _driving_frame(), "EVSPD01", "2026-01-05", generate_validation_fig=False
    )
    assert isinstance(discharge[0]["ep_audit"], dict)
    assert discharge[0]["ep_audit"]["dup_ratio"] == pytest.approx(1.0)


# =============================================================================
# 2. The provisional grade, in the row builder
# =============================================================================
def _seg_to_row_with(monkeypatch, grader):
    monkeypatch.setattr(row_builder, "_get_leg_type", lambda *a, **k: "In Transit")
    monkeypatch.setattr(row_builder, "_get_postcode", lambda *a, **k: None)
    monkeypatch.setattr(
        row_builder, "_get_vehicle_mass", lambda *a, **k: (np.nan, np.nan)
    )
    monkeypatch.setattr(row_builder, "_get_recuperation", lambda *a: np.nan)
    monkeypatch.setattr(row_builder, "_get_elevation_diff", lambda *a: np.nan)
    monkeypatch.setattr(row_builder, "_get_propulsion_energy", lambda *a: np.nan)
    monkeypatch.setattr(row_builder, "assess_ep_confidence", grader)
    segment = {
        "start_time": pd.Timestamp("2025-11-18 07:19:46", tz="UTC"),
        "end_time": pd.Timestamp("2025-11-18 07:37:52", tz="UTC"),
        "odo_start_km": 19_719.780,
        "odo_end_km": 19_743.260,
        "start_soc": 90.0,
        "end_soc": 85.0,
        "delta_soc_pct": -5.0,
        "delta_energy_kwh": -25.0,
        "effective_capacity_kwh": 500.0,
        "energy_source": "total_energy",
    }
    row, _ = row_builder._seg_to_row(
        segment,
        mode="discharge",
        leg_uri="",
        charger_windows=[],
        logger_windows=[],
        df_leg=pd.DataFrame(),
        cumulative_km=0.0,
    )
    return row


def test_the_row_survives_a_failing_grader_with_only_its_grade_blank(monkeypatch):
    def _raise(*a, **k):
        raise _Boom("bad row value")

    row = _seg_to_row_with(monkeypatch, _raise)
    assert row[I_CONF] is None
    assert row[I_REASON] is None
    # Everything else the row reports is intact.
    assert row[_row_col_index("Distance (km)", HEADERS)] == pytest.approx(23.48)
    assert row[_row_col_index("Average Speed (km/h)", HEADERS)] == 77.83
    assert row[_row_col_index("Energy Change (kWh)", HEADERS)] == pytest.approx(-25.0)


def test_the_same_row_is_graded_when_the_grader_works(monkeypatch):
    row = _seg_to_row_with(monkeypatch, lambda *a, **k: (CONF_POOR, "DUP_ENERGY=2.00"))
    assert row[I_CONF] == CONF_POOR
    assert row[I_REASON] == "DUP_ENERGY=2.00"


# =============================================================================
# 3. The authoritative pass — one bad row does not cost the others
# =============================================================================
def _trip_row(distance_km):
    row = [float("nan")] * (len(HEADERS) - 1)

    def put(name, value):
        row[_row_col_index(name, HEADERS)] = value

    put("Leg Type", "In Transit")
    put("Start Time (UTC)", pd.Timestamp("2026-01-05 08:00:00", tz="UTC"))
    put("Energy Source", "total_energy")
    put("SOC Change (%)", -20.0)
    put("Distance (km)", distance_km)
    put("Duration (HH:MM:SS)", 1 / 24)
    put("Energy Change (kWh)", -60.0)
    put("Energy Performance (kWh/km)", 1.2)
    return row


def test_one_ungradeable_row_blanks_only_itself(monkeypatch):
    real = epc.assess_ep_confidence

    def _explode_on_the_marked_row(audit, **kw):
        if kw.get("distance_km") == 999.0:
            raise _Boom("this row only")
        return real(audit, **kw)

    monkeypatch.setattr(epc, "assess_ep_confidence", _explode_on_the_marked_row)

    good_before, bad, good_after = (
        _trip_row(50.0),
        _trip_row(999.0),
        _trip_row(50.0),
    )
    bad[I_CONF], bad[I_REASON] = "good", "stale"  # a stale grade must not survive
    tally = epc.regrade_rows([good_before, bad, good_after], HEADERS)

    assert [r[I_CONF] for r in (good_before, bad, good_after)] == [
        CONF_GOOD,
        None,
        CONF_GOOD,
    ]
    assert bad[I_REASON] is None
    # The failed row is counted as ungraded, not silently as a grade.
    assert tally[CONF_GOOD] == 2
    assert tally["ungraded"] == 1


# =============================================================================
# 4. The outer net — a catastrophic pass does not cost the report
# =============================================================================
def test_finalize_rows_returns_its_rows_when_the_grading_pass_explodes(monkeypatch):
    def _raise(*a, **k):
        raise _Boom("headers unusable")

    monkeypatch.setattr(gen_mod, "regrade_rows", _raise)

    gen = JOLTReportGenerator.__new__(JOLTReportGenerator)
    gen.debug_mode = False
    gen.fast_mode = True
    gen.srf_data = None

    rows = [_trip_row(50.0)]
    out_rows, _cap, _n, _src = gen._finalize_rows(
        rows, HEADERS, is_diesel=False, cfg={}, soc_est_cap=None
    )

    assert len(out_rows) == 1
    assert out_rows[0][_row_col_index("Distance (km)", HEADERS)] == pytest.approx(50.0)
