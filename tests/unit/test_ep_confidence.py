"""Contract tests for the EP-confidence grading (``report_generator.ep_confidence``).

Three things are pinned here:

* **the grading contract** — a grade exists exactly where an EP value exists, the
  grade is the worst finding, and each check fires on its own mechanism at its own
  threshold (a threshold change is then a deliberate, visible edit, not a silent
  drift in what the fleet's numbers claim about themselves);
* **the measurement** — ``attach_ep_audits`` reproduces the documented fingerprints
  on synthetic raw frames built to contain each defect, including the exact 2×
  double count that motivated the column;
* **the hand-off** — the audit travels from segment to row by start-time key, and
  the final pass wins over the row builder's provisional grade.
"""

import numpy as np
import pandas as pd
import pytest

from report_generator.columns import HEADERS, _row_col_index
from report_generator.ep_confidence import (
    CODE_CAP_INCONS,
    CODE_DIST_EXTRAP,
    CODE_DUP_ENERGY,
    CODE_ENERGY_WINDOW,
    CODE_EP_RANGE,
    CODE_IDLE_WINDOW,
    CODE_SHORT_DIST,
    CODE_SOC_RES,
    CODE_SOC_STEP,
    CODE_SPEED,
    CODE_SPLIT_ALLOC,
    CONF_CAUTION,
    CONF_GOOD,
    CONF_POOR,
    assess_ep_confidence,
    attach_ep_audits,
    audit_key,
    regrade_rows,
    soc_quantum_pct,
)

TIME_COL = "eventDatetime"
SOC_COL = "electricBatteryLevelPercent"
ODO_COL = "odometer"
TOT_COL = "total_electric_energy_used_plugged_in_included"
MOV_COL = "electric_energy_wheelbased_speed_over_zero"

# A row that trips no check — the baseline every case below perturbs one field of.
CLEAN = dict(
    energy_source="total_energy",
    delta_soc_pct=-20.0,
    distance_km=50.0,
    duration_h=1.0,
    energy_kwh=-60.0,
    ep_kwh_km=1.2,
)
CLEAN_AUDIT = {"dup_ratio": 1.0, "soc_quantum_pct": 1.0}


def codes(reason):
    return [] if not reason else [f.split("=")[0] for f in reason.split("; ")]


# =============================================================================
# What is and is not graded
# =============================================================================
def test_clean_row_is_good_with_no_reason():
    grade, reason = assess_ep_confidence(CLEAN_AUDIT, **CLEAN)
    assert grade == CONF_GOOD
    assert reason is None


@pytest.mark.parametrize("ep", [None, float("nan")])
def test_row_without_ep_is_not_graded(ep):
    # A charge or Stop row states no EP; grading one would imply a judgement about
    # a number the report never made.
    assert assess_ep_confidence(CLEAN_AUDIT, **{**CLEAN, "ep_kwh_km": ep}) == (
        None,
        None,
    )


@pytest.mark.parametrize("dist", [None, float("nan"), 0.0, -1.0])
def test_row_without_usable_distance_is_not_graded(dist):
    assert assess_ep_confidence(CLEAN_AUDIT, **{**CLEAN, "distance_km": dist}) == (
        None,
        None,
    )


def test_missing_audit_never_invents_a_downgrade():
    # Audit-dependent checks are skipped, not failed: a row whose diagnostics were
    # not measured must not be presented as defective.
    grade, reason = assess_ep_confidence(None, **CLEAN)
    assert grade == CONF_GOOD
    assert reason is None


# =============================================================================
# Individual checks and their thresholds
# =============================================================================
def test_double_counted_energy_is_poor():
    # The fingerprint of the merge defect: the row claims twice the energy the
    # counter measured across the same anchor span.
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "dup_ratio": 2.0}, **{**CLEAN, "energy_kwh": -120.0}
    )
    assert grade == CONF_POOR
    assert codes(reason)[0] == CODE_DUP_ENERGY
    assert "2.00" in reason


def test_mild_energy_excess_is_caution_not_poor():
    grade, reason = assess_ep_confidence({**CLEAN_AUDIT, "dup_ratio": 1.15}, **CLEAN)
    assert grade == CONF_CAUTION
    assert CODE_DUP_ENERGY in codes(reason)


def test_rounding_scale_excess_stays_good():
    # The anchor arithmetic rounds; a sub-percent excess is noise, not a defect.
    assert assess_ep_confidence({**CLEAN_AUDIT, "dup_ratio": 1.02}, **CLEAN)[0] == (
        CONF_GOOD
    )


def test_split_allocation_is_caution():
    grade, reason = assess_ep_confidence({**CLEAN_AUDIT, "dup_ratio": 0.6}, **CLEAN)
    assert grade == CONF_CAUTION
    assert CODE_SPLIT_ALLOC in codes(reason)


def test_energy_window_contamination_scales_with_trip_distance():
    # 3 km of foreign driving is noise on a 100 km trip and decisive on a 5 km one.
    audit = {**CLEAN_AUDIT, "energy_outside_km": 3.0}

    def at(distance_km):
        # Hold the implied average speed at 50 km/h so only the window check moves.
        return {**CLEAN, "distance_km": distance_km, "duration_h": distance_km / 50.0}

    assert assess_ep_confidence(audit, **at(100.0))[0] == CONF_GOOD
    assert assess_ep_confidence(audit, **at(20.0))[0] == CONF_CAUTION
    grade, reason = assess_ep_confidence(audit, **at(5.0))
    assert grade == CONF_POOR
    assert CODE_ENERGY_WINDOW in codes(reason)


def test_parked_overshoot_is_caught_without_any_distance():
    # The case the distance-based check cannot see: hours of standing time inside
    # the energy window, at zero km.
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "energy_outside_km": 0.0, "energy_outside_min": 600.0},
        **{**CLEAN, "energy_kwh": -60.0},
    )
    assert grade == CONF_POOR  # 10 h x 3 kW = 30 kWh against a 60 kWh trip
    assert CODE_IDLE_WINDOW in codes(reason)


def test_short_overshoot_of_a_large_trip_stays_good():
    assert (
        assess_ep_confidence({**CLEAN_AUDIT, "energy_outside_min": 30.0}, **CLEAN)[0]
        == CONF_GOOD
    )


def test_extrapolated_distance_is_poor():
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "odo_samples_in_trip": 0, "samples_in_trip": 40}, **CLEAN
    )
    assert grade == CONF_POOR
    assert CODE_DIST_EXTRAP in codes(reason)


def test_no_samples_at_all_is_not_treated_as_extrapolation():
    # An empty window means nothing was measured, which is not evidence of a defect.
    grade, _ = assess_ep_confidence(
        {**CLEAN_AUDIT, "odo_samples_in_trip": 0, "samples_in_trip": 0}, **CLEAN
    )
    assert grade == CONF_GOOD


def test_soc_resolution_applies_only_to_soc_derived_energy():
    audit = {**CLEAN_AUDIT, "soc_quantum_pct": 1.0}
    small = {**CLEAN, "delta_soc_pct": -2.0}  # one step is half the change
    # Counter-sourced: the SOC channel does not enter the energy, so it is not a
    # reason to distrust EP.
    assert CODE_SOC_RES not in codes(assess_ep_confidence(audit, **small)[1])
    # SOC-derived: the same coarse change IS the energy.
    grade, reason = assess_ep_confidence(
        audit, **{**small, "energy_source": "soc_estimate"}
    )
    assert grade == CONF_POOR
    assert CODE_SOC_RES in codes(reason)


def test_soc_fallback_is_graded_like_soc_estimate():
    # The capacity post-processing relabels some counter rows to soc_fallback and
    # re-derives their energy from SOC; the SOC checks must then apply.
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "soc_quantum_pct": 1.0},
        **{**CLEAN, "energy_source": "soc_fallback", "delta_soc_pct": -3.0},
    )
    assert grade == CONF_POOR
    assert CODE_SOC_RES in codes(reason)


def test_well_resolved_soc_energy_can_still_be_good():
    # Grade measures resolution and consistency, not provenance — Energy Source
    # already records provenance, so a clean SOC-derived row is not penalised.
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "soc_quantum_pct": 0.4},
        **{**CLEAN, "energy_source": "soc_estimate", "delta_soc_pct": -40.0},
    )
    assert grade == CONF_GOOD
    assert reason is None


def test_soc_discontinuity_carrying_the_whole_trip_is_poor():
    # LN25NKE's worst rows: a 12-point drop between two samples 25 s apart.
    grade, reason = assess_ep_confidence(
        {
            "soc_quantum_pct": 0.4,
            "soc_step_pct": 12.0,
            "soc_step_rate_pct_h": 1728.0,
        },
        **{
            **CLEAN,
            "energy_source": "soc_estimate",
            "delta_soc_pct": -12.0,
            "distance_km": 0.315,
            "energy_kwh": -55.44,
            "ep_kwh_km": 176.0,
        },
    )
    assert grade == CONF_POOR
    assert codes(reason)[0] == CODE_SOC_STEP  # the mechanism leads, not the symptom
    assert codes(reason)[-1] == CODE_EP_RANGE  # the backstop reports last


def test_plausible_soc_rate_is_not_a_discontinuity():
    # A fast but physical discharge (90 %/h) must not be flagged.
    grade, _ = assess_ep_confidence(
        {"soc_quantum_pct": 0.4, "soc_step_pct": 3.0, "soc_step_rate_pct_h": 90.0},
        **{**CLEAN, "energy_source": "soc_estimate"},
    )
    assert grade == CONF_GOOD


def test_counter_and_soc_disagreement_is_flagged_when_soc_is_a_fair_referee():
    # 60 kWh from the counter against 20 % of a 200 kWh pack = 40 kWh: a 50 % gap.
    grade, reason = assess_ep_confidence(CLEAN_AUDIT, **CLEAN, capacity_ref_kwh=200.0)
    assert grade == CONF_CAUTION
    assert CODE_CAP_INCONS in codes(reason)


def test_cross_check_is_skipped_when_soc_is_too_coarse_to_referee():
    # With ΔSOC only two steps wide the gap says more about the SOC channel than
    # about the counter, so the cross-check must stay silent.
    reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "soc_quantum_pct": 1.0},
        **{**CLEAN, "delta_soc_pct": -2.0},
        capacity_ref_kwh=200.0,
    )[1]
    assert CODE_CAP_INCONS not in codes(reason)


def test_cross_check_is_skipped_on_a_coarse_channel_even_with_a_large_change():
    # LN25NKE-style: ΔSOC is 12 % but each step is 4 %, so three steps carry the
    # whole change and it cannot referee a 30 % disagreement.
    reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "soc_quantum_pct": 4.0},
        **{**CLEAN, "delta_soc_pct": -12.0},
        capacity_ref_kwh=200.0,
    )[1]
    assert CODE_CAP_INCONS not in codes(reason)


def test_cross_check_is_skipped_without_a_reference_capacity():
    # The row builder passes no reference, because the only capacity available
    # there is the row's own implied value — comparing a number with itself.
    assert CODE_CAP_INCONS not in codes(
        assess_ep_confidence(CLEAN_AUDIT, **CLEAN)[1] or ""
    )


@pytest.mark.parametrize(
    "dist,expected",
    [(0.5, CONF_POOR), (2.0, CONF_CAUTION), (5.0, CONF_GOOD)],
)
def test_distance_floor(dist, expected):
    grade, _ = assess_ep_confidence(
        CLEAN_AUDIT, **{**CLEAN, "distance_km": dist, "duration_h": dist / 40.0}
    )
    assert grade == expected


def test_impossible_average_speed_is_poor():
    grade, reason = assess_ep_confidence(
        CLEAN_AUDIT, **{**CLEAN, "distance_km": 169.0, "duration_h": 1.49}
    )
    assert grade == CONF_POOR
    assert CODE_SPEED in codes(reason)


@pytest.mark.parametrize(
    "ep,expected", [(3.0, CONF_GOOD), (5.0, CONF_CAUTION), (20.0, CONF_POOR)]
)
def test_plausibility_backstop(ep, expected):
    grade, _ = assess_ep_confidence(CLEAN_AUDIT, **{**CLEAN, "ep_kwh_km": ep})
    assert grade == expected


def test_grade_is_the_worst_finding_not_a_vote():
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "dup_ratio": 2.0, "energy_outside_km": 6.0},
        **{**CLEAN, "distance_km": 40.0, "duration_h": 0.8, "energy_kwh": -120.0},
    )
    assert grade == CONF_POOR
    assert set(codes(reason)) >= {CODE_DUP_ENERGY, CODE_ENERGY_WINDOW}


def test_reason_lists_worst_severity_first():
    grade, reason = assess_ep_confidence(
        {**CLEAN_AUDIT, "dup_ratio": 2.0},
        **{**CLEAN, "distance_km": 2.0, "energy_kwh": -120.0, "ep_kwh_km": 2.5},
    )
    assert grade == CONF_POOR
    # DUP_ENERGY (poor) must precede SHORT_DIST (caution at 2 km).
    assert codes(reason).index(CODE_DUP_ENERGY) < codes(reason).index(CODE_SHORT_DIST)


# =============================================================================
# The SOC quantisation estimator
# =============================================================================
@pytest.mark.parametrize("quantum", [0.4, 1.0, 0.5])
def test_soc_quantum_recovered_from_a_grid(quantum):
    # Values on a grid, sampled unevenly so single-step changes are not guaranteed
    # to appear — the gcd recovers the step regardless.
    steps = np.array([1, 3, 2, 1, 5, 2, 1, 4] * 4)
    soc = pd.Series(90.0 - np.cumsum(steps) * quantum)
    assert soc_quantum_pct(soc) == pytest.approx(quantum)


def test_soc_quantum_needs_enough_transitions():
    assert np.isnan(soc_quantum_pct(pd.Series([90.0, 89.0, 88.0])))


def test_soc_quantum_of_a_flat_signal_is_unknown():
    assert np.isnan(soc_quantum_pct(pd.Series([90.0] * 50)))


def test_off_grid_reading_only_makes_the_estimate_more_lenient():
    # A single stray value drags the gcd down, never up, so it can soften the
    # SOC-resolution check but never manufacture a false downgrade.
    clean = pd.Series(90.0 - np.arange(40) * 0.4)
    noisy = clean.copy()
    noisy.iloc[20] = float(noisy.iloc[20]) - 0.13
    assert soc_quantum_pct(noisy) <= soc_quantum_pct(clean)


# =============================================================================
# Measurement from a raw frame
# =============================================================================
def _frame(minutes, odo, soc, counter_wh):
    t0 = pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    return pd.DataFrame(
        {
            TIME_COL: [t0 + pd.Timedelta(minutes=m) for m in minutes],
            ODO_COL: odo,
            SOC_COL: soc,
            TOT_COL: counter_wh,
            MOV_COL: [np.nan] * len(minutes),
        }
    )


def _frame_us(minutes, odo, soc, counter_wh):
    """The same frame with its time column pinned to the microsecond unit.

    ``pd.to_datetime(<ISO strings>, utc=True)`` infers ``datetime64[us, UTC]`` on
    pandas 3 and ``datetime64[ns, UTC]`` on pandas 2, so the raw telematics frames
    reaching ``attach_ep_audits`` are microsecond-unit in production on pandas 3.
    The unit is forced here rather than inferred because the point of the test is
    the non-nanosecond case, and it has to be the non-nanosecond case on whichever
    pandas runs it.
    """
    df = _frame(minutes, odo, soc, counter_wh)
    df[TIME_COL] = df[TIME_COL].astype("datetime64[us, UTC]")
    return df


def _attach(segs, df):
    attach_ep_audits(
        segs, df, TOT_COL, MOV_COL, soc_col=SOC_COL, odo_col=ODO_COL, time_col=TIME_COL
    )


def test_two_segments_sharing_one_counter_interval_measure_as_a_2x_duplicate():
    # The defect, reconstructed: the counter reports only at 08:00 and 09:00, two
    # trips run in between, each takes the whole interval, and the merge sums them.
    df = _frame(
        minutes=[0, 10, 20, 30, 40, 50, 60],
        odo=[100.0, 105.0, 110.0, 110.0, 115.0, 120.0, 120.0],
        soc=[90.0, 86.0, 82.0, 82.0, 78.0, 74.0, 74.0],
        counter_wh=[10_000, np.nan, np.nan, np.nan, np.nan, np.nan, 50_000],
    )
    t0 = pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    merged = {
        "start_time": t0 + pd.Timedelta(minutes=1),
        "end_time": t0 + pd.Timedelta(minutes=50),
        # Each half claimed the whole 40 kWh interval and the merge added them.
        "delta_energy_kwh": -80.0,
        "energy_source": "total_energy",
        "delta_soc_pct": -16.0,
        "_anchor_start_time": t0,
        "_anchor_end_time": t0 + pd.Timedelta(minutes=60),
        "_anchor_start_rel_kwh": 0.0,
        "_anchor_end_rel_kwh": 40.0,
        "odo_start_km": 100.0,
        "odo_end_km": 120.0,
    }
    _attach([merged], df)
    assert merged["ep_audit"]["dup_ratio"] == pytest.approx(2.0)
    grade, reason = assess_ep_confidence(
        merged["ep_audit"],
        energy_source="total_energy",
        delta_soc_pct=-16.0,
        distance_km=20.0,
        duration_h=49 / 60,
        energy_kwh=-80.0,
        ep_kwh_km=4.0,
    )
    assert grade == CONF_POOR
    assert codes(reason)[0] == CODE_DUP_ENERGY


def test_anchor_overshoot_measures_the_driving_it_swallowed():
    # The trip is 10 km, but the counter's anchor window reaches back over another
    # 5 km the vehicle drove before the trip started.
    df = _frame(
        minutes=[0, 10, 20, 30],
        odo=[100.0, 105.0, 110.0, 115.0],
        soc=[90.0, 88.0, 86.0, 84.0],
        counter_wh=[10_000, np.nan, np.nan, 40_000],
    )
    t0 = pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    seg = {
        "start_time": t0 + pd.Timedelta(minutes=10),
        "end_time": t0 + pd.Timedelta(minutes=30),
        "delta_energy_kwh": -30.0,
        "energy_source": "total_energy",
        "delta_soc_pct": -4.0,
        "_anchor_start_time": t0,
        "_anchor_end_time": t0 + pd.Timedelta(minutes=30),
        "_anchor_start_rel_kwh": 0.0,
        "_anchor_end_rel_kwh": 30.0,
        "odo_start_km": 105.0,
        "odo_end_km": 115.0,
    }
    _attach([seg], df)
    audit = seg["ep_audit"]
    assert audit["energy_outside_km"] == pytest.approx(5.0)
    assert audit["energy_outside_min"] == pytest.approx(10.0)
    assert CODE_ENERGY_WINDOW in codes(
        assess_ep_confidence(
            audit,
            energy_source="total_energy",
            delta_soc_pct=-4.0,
            distance_km=10.0,
            duration_h=1 / 3,
            energy_kwh=-30.0,
            ep_kwh_km=3.0,
        )[1]
    )


def test_audit_reports_when_no_odometer_sample_falls_inside_the_trip():
    df = _frame(
        minutes=[0, 60],
        odo=[100.0, 200.0],
        soc=[90.0, 70.0],
        counter_wh=[10_000, 80_000],
    )
    t0 = pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    seg = {
        "start_time": t0 + pd.Timedelta(minutes=20),
        "end_time": t0 + pd.Timedelta(minutes=40),
        "delta_energy_kwh": -70.0,
        "energy_source": "total_energy",
        "delta_soc_pct": -20.0,
        "_anchor_start_time": t0,
        "_anchor_end_time": t0 + pd.Timedelta(minutes=60),
        "_anchor_start_rel_kwh": 0.0,
        "_anchor_end_rel_kwh": 70.0,
        "odo_start_km": 100.0,
        "odo_end_km": 200.0,
    }
    _attach([seg], df)
    audit = seg["ep_audit"]
    assert audit["odo_samples_in_trip"] == 0
    # Corroborate the zero, so this cannot pass merely because the measurement
    # broke: the trip window really is empty (no raw sample between 08:20 and
    # 08:40 either), and the distance the empty window extrapolates over is the
    # 33.3 km either side of it that the surrounding readings actually cover.
    assert audit["samples_in_trip"] == 0
    assert audit["dist_outside_km"] == pytest.approx(66.667, abs=1e-3)


def test_windows_are_measured_in_nanoseconds_on_a_microsecond_frame():
    # Regression guard for the unit bug: every instant in ep_confidence is int64
    # UTC nanoseconds, but a datetime Series' int64 view is in the Series' OWN
    # unit, which pandas 3 infers as microseconds. Comparing that against the
    # nanosecond instants from Timestamp.value silently measured every window
    # 1000x small — zeroing the sample counts, NaN-ing the distances and leaving
    # ENERGY_WINDOW / DIST_WINDOW / DIST_EXTRAP / SOC_STEP dead. Each assertion
    # below is chosen to be off by 1000x, zero or NaN if that returns.
    df = _frame_us(
        minutes=[0, 10, 20, 30, 40, 50, 60],
        odo=[100.0, 105.0, 110.0, 115.0, 120.0, 125.0, 130.0],
        soc=[90.0, 88.0, 86.0, 84.0, 82.0, 80.0, 78.0],
        counter_wh=[0, np.nan, 20_000, np.nan, 40_000, np.nan, 60_000],
    )
    assert df[TIME_COL].dtype == "datetime64[us, UTC]"  # the precondition
    t0 = pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    seg = {
        "start_time": t0 + pd.Timedelta(minutes=10),
        "end_time": t0 + pd.Timedelta(minutes=40),
        "delta_energy_kwh": -30.0,
        "energy_source": "total_energy",
        "delta_soc_pct": -6.0,
        "_anchor_start_time": t0,
        "_anchor_end_time": t0 + pd.Timedelta(minutes=50),
        "_anchor_start_rel_kwh": 0.0,
        "_anchor_end_rel_kwh": 30.0,
        "odo_start_km": 105.0,
        "odo_end_km": 120.0,
    }
    _attach([seg], df)
    audit = seg["ep_audit"]
    # Sample counts inside 08:10–08:40 inclusive: the samples at 10/20/30/40 min.
    assert audit["samples_in_trip"] == 4
    assert audit["odo_samples_in_trip"] == 4
    # 10 min of overshoot each side of the trip, over 5 km of driving each side.
    assert audit["energy_outside_min"] == pytest.approx(20.0)
    assert audit["energy_outside_km"] == pytest.approx(10.0)
    # A counter reading every 20 minutes, and a 2-point SOC fall per 10 minutes.
    assert audit["counter_gap_min"] == pytest.approx(20.0)
    assert audit["soc_step_pct"] == pytest.approx(2.0)
    assert audit["soc_step_rate_pct_h"] == pytest.approx(12.0)


def test_audit_survives_a_frame_without_the_optional_columns():
    df = pd.DataFrame(
        {TIME_COL: [pd.Timestamp("2026-01-05 08:00:00", tz="UTC")], SOC_COL: [90.0]}
    )
    seg = {
        "start_time": pd.Timestamp("2026-01-05 08:00:00", tz="UTC"),
        "end_time": pd.Timestamp("2026-01-05 08:30:00", tz="UTC"),
        "delta_energy_kwh": -10.0,
        "energy_source": "soc_estimate",
    }
    _attach([seg], df)
    assert isinstance(seg["ep_audit"], dict)


def test_the_measurement_never_alters_the_segment():
    df = _frame([0, 30], [100.0, 120.0], [90.0, 82.0], [10_000, 40_000])
    seg = {
        "start_time": pd.Timestamp("2026-01-05 08:00:00", tz="UTC"),
        "end_time": pd.Timestamp("2026-01-05 08:30:00", tz="UTC"),
        "delta_energy_kwh": -30.0,
        "energy_source": "total_energy",
        "delta_soc_pct": -8.0,
        "_anchor_start_rel_kwh": 0.0,
        "_anchor_end_rel_kwh": 30.0,
    }
    before = dict(seg)
    _attach([seg], df)
    for key, value in before.items():
        assert seg[key] == value


# =============================================================================
# The row-level hand-off
# =============================================================================
def test_audit_key_matches_naive_and_aware_timestamps():
    aware = pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    assert audit_key(aware) == audit_key(pd.Timestamp("2026-01-05 08:00:00"))
    assert audit_key(None) is None
    assert audit_key(pd.NaT) is None


def _row(leg_type, **over):
    row = [float("nan")] * (len(HEADERS) - 1)

    def put(name, value):
        row[_row_col_index(name, HEADERS)] = value

    put("Leg Type", leg_type)
    put("Start Time (UTC)", pd.Timestamp("2026-01-05 08:00:00", tz="UTC"))
    put("Energy Source", "total_energy")
    put("SOC Change (%)", -20.0)
    put("Distance (km)", 50.0)
    put("Duration (HH:MM:SS)", 1 / 24)
    put("Energy Change (kWh)", -60.0)
    put("Energy Performance (kWh/km)", 1.2)
    for name, value in over.items():
        put(name.replace("_", " "), value)
    return row


def test_regrade_blanks_every_non_trip_row():
    i_conf = _row_col_index("EP Confidence", HEADERS)
    i_reason = _row_col_index("EP Confidence Reason", HEADERS)
    rows = [_row("Stop"), _row("AC Charge Home"), _row("In Transit")]
    for r in rows[:2]:  # a stale grade must not survive
        r[i_conf], r[i_reason] = "good", "stale"
    tally = regrade_rows(rows, HEADERS)
    assert [r[i_conf] for r in rows] == [None, None, CONF_GOOD]
    assert [r[i_reason] for r in rows] == [None, None, None]
    assert tally["ungraded"] == 2
    assert tally[CONF_GOOD] == 1


def test_regrade_uses_the_audit_for_the_matching_row_only():
    i_conf = _row_col_index("EP Confidence", HEADERS)
    matched = _row("In Transit")
    other = _row("In Transit")
    other[_row_col_index("Start Time (UTC)", HEADERS)] = pd.Timestamp(
        "2026-02-02 09:00:00", tz="UTC"
    )
    audits = {
        audit_key(pd.Timestamp("2026-01-05 08:00:00", tz="UTC")): {
            "dup_ratio": 2.0,
            "soc_quantum_pct": 1.0,
        }
    }
    regrade_rows([matched, other], HEADERS, audit_by_start=audits)
    assert matched[i_conf] == CONF_POOR
    assert other[i_conf] == CONF_GOOD


def test_regrade_overwrites_the_provisional_grade():
    # The row builder cannot run the counter-versus-SOC cross-check (it has no
    # independent capacity); the final pass can, and its verdict must replace the
    # earlier one. Note it runs here with no audit at all — the cross-check must not
    # depend on diagnostics it does not need.
    i_conf = _row_col_index("EP Confidence", HEADERS)
    row = _row("In Transit")
    row[i_conf] = CONF_GOOD
    regrade_rows([row], HEADERS, capacity_ref_kwh=200.0)
    assert row[i_conf] == CONF_CAUTION
    assert CODE_CAP_INCONS in codes(
        row[_row_col_index("EP Confidence Reason", HEADERS)]
    )


def test_regrade_is_a_no_op_on_a_header_set_without_the_columns():
    from report_generator.columns import DIESEL_HEADERS

    assert regrade_rows([[None] * (len(DIESEL_HEADERS) - 1)], DIESEL_HEADERS) == {}
