"""``_correct_effective_capacity`` on synthetic row sets.

Step 1 replaces every ``soc_estimate`` leg's capacity with a TIME-LOCAL donor
mean (+-15 days, widening on demand, then the whole-period mean, then
``fallback_kwh``) and re-derives its energy. Step 2 flags any capacity outside
the global +-1 sigma band and replaces it, re-deriving energy ONLY for
SOC-derived legs (MODE A) unless the vehicle opts in to the dual-gated
SOC-energy fallback.

The rows here are synthetic on purpose: each scenario isolates one branch with
numbers that can be checked by hand.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from jolt_toolkit.report_generator.capacity import (
    _IDX_BPOWER,
    _IDX_CAP,
    _IDX_DISTANCE,
    _IDX_DURATION,
    _IDX_ELEV,
    _IDX_ENERGY,
    _IDX_EPERF,
    _IDX_EPERF_CORR,
    _IDX_EPERF_KIN,
    _IDX_ESOURCE,
    _IDX_MASS,
    _IDX_SOC_CHANGE,
    _IDX_START,
    CAP_WINDOW_HALF_DAYS,
    _correct_effective_capacity,
)
from jolt_toolkit.report_generator.columns import HEADERS


def _row(
    *,
    source,
    start=None,
    cap=float("nan"),
    soc=float("nan"),
    energy=float("nan"),
    dist=float("nan"),
    eperf=float("nan"),
    dur=0.25,
    elev=0.0,
    mass=30000.0,
):
    row = [float("nan")] * (len(HEADERS) - 1)
    row[_IDX_ESOURCE] = source
    row[_IDX_CAP] = cap
    row[_IDX_SOC_CHANGE] = soc
    row[_IDX_ENERGY] = energy
    row[_IDX_DISTANCE] = dist
    row[_IDX_EPERF] = eperf
    row[_IDX_DURATION] = dur
    row[_IDX_ELEV] = elev
    row[_IDX_MASS] = mass
    if start is not None:
        row[_IDX_START] = pd.Timestamp(start)
    return row


def _correct(rows, fallback_kwh=None, idx_start=_IDX_START, soc_fallback=None):
    return _correct_effective_capacity(
        rows,
        _IDX_CAP,
        _IDX_ENERGY,
        _IDX_SOC_CHANGE,
        _IDX_EPERF,
        _IDX_DISTANCE,
        _IDX_ESOURCE,
        _IDX_BPOWER,
        _IDX_DURATION,
        _IDX_EPERF_CORR,
        _IDX_ELEV,
        _IDX_MASS,
        fallback_kwh,
        _IDX_EPERF_KIN,
        idx_start=idx_start,
        soc_fallback=soc_fallback,
    )


def _charge_donor(start, cap, soc=40.0):
    return _row(
        source="ac_dc",
        start=start,
        cap=cap,
        soc=soc,
        energy=cap * soc / 100.0,
        dist=float("nan"),
    )


def _discharge_donor(start, cap, soc=-40.0, dist=100.0):
    energy = -abs(cap * soc / 100.0)
    return _row(
        source="total_energy",
        start=start,
        cap=cap,
        soc=soc,
        energy=energy,
        dist=dist,
        eperf=round(abs(energy) / dist, 4),
    )


# ── Step 1: time-local window ────────────────────────────────────────────────


def test_step1_uses_the_donor_inside_the_time_local_window():
    near = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    far = _charge_donor("2025-06-01T08:00:00Z", 600.0)
    target = _row(
        source="soc_estimate", start="2025-01-05T08:00:00Z", soc=-20.0, dist=100.0
    )
    rows, eff_cap, source = _correct([near, far, target])

    assert source == "charge"
    # The +-15 day window around 05 Jan contains only the 400 kWh donor.
    assert target[_IDX_CAP] == 400.0
    assert target[_IDX_ENERGY] == pytest.approx(-80.0)  # -20 % x 400 kWh
    assert target[_IDX_EPERF] == pytest.approx(0.8)  # 80 kWh / 100 km
    assert eff_cap is not None


def test_step1_picks_the_other_donor_for_a_later_leg():
    near = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    far = _charge_donor("2025-06-01T08:00:00Z", 600.0)
    target = _row(
        source="soc_estimate", start="2025-06-05T08:00:00Z", soc=-20.0, dist=100.0
    )
    _correct([near, far, target])
    assert target[_IDX_CAP] == 600.0
    assert target[_IDX_ENERGY] == pytest.approx(-120.0)


def test_step1_prefers_charge_donors_over_discharge_in_the_same_window():
    charge = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    discharge = _discharge_donor("2025-01-02T08:00:00Z", 500.0)
    target = _row(
        source="soc_estimate", start="2025-01-03T08:00:00Z", soc=-10.0, dist=50.0
    )
    _correct([charge, discharge, target])
    assert target[_IDX_CAP] == 400.0


def test_step1_widens_the_window_until_a_donor_is_found(caplog):
    # The only donor sits 59 days away: 15 -> 30 -> 60 days of half-width.
    donor = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    target = _row(
        source="soc_estimate", start="2025-03-01T08:00:00Z", soc=-25.0, dist=100.0
    )
    assert CAP_WINDOW_HALF_DAYS == 15
    with caplog.at_level("INFO"):
        _correct([donor, target])
    assert target[_IDX_CAP] == 400.0
    assert target[_IDX_ENERGY] == pytest.approx(-100.0)
    assert "widened=1" in caplog.text


def test_step1_falls_back_to_the_global_mean_without_timestamps():
    # idx_start=None reverts to the historical single whole-period mean, which is
    # the SOC-weighted combined ratio over the donors: (400*40 + 600*40)/80 = 500.
    near = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    far = _charge_donor("2025-06-01T08:00:00Z", 600.0)
    target = _row(source="soc_estimate", soc=-20.0, dist=100.0)
    _correct([near, far, target], idx_start=None)
    assert target[_IDX_CAP] == 500.0
    assert target[_IDX_ENERGY] == pytest.approx(-100.0)


def test_step1_falls_back_to_fallback_kwh_without_any_donor(caplog):
    target = _row(
        source="soc_estimate", start="2025-01-01T08:00:00Z", soc=-30.0, dist=100.0
    )
    with caplog.at_level("INFO"):
        rows, eff_cap, source = _correct([target], fallback_kwh=510.0)
    assert source == "fallback"
    assert target[_IDX_CAP] == 510.0
    assert target[_IDX_ENERGY] == pytest.approx(-153.0)  # -30 % x 510 kWh
    assert eff_cap == 510.0


def test_step1_leaves_the_row_alone_when_there_is_no_capacity_at_all():
    target = _row(
        source="soc_estimate", start="2025-01-01T08:00:00Z", soc=-30.0, dist=100.0
    )
    _rows, eff_cap, source = _correct([target], fallback_kwh=None)
    assert (eff_cap, source) == (None, "fallback")
    assert math.isnan(target[_IDX_CAP])
    assert math.isnan(target[_IDX_ENERGY])


def test_step1_recomputes_battery_power_and_the_corrected_ep():
    donor = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    target = _row(
        source="soc_estimate",
        start="2025-01-02T08:00:00Z",
        soc=-20.0,
        dist=100.0,
        dur=0.25,  # 6 hours
        elev=0.0,
        mass=30000.0,
    )
    _correct([donor, target])
    # -80 kWh over 6 h.
    assert target[_IDX_BPOWER] == pytest.approx(-80.0 / 6.0, abs=1e-3)
    # With zero elevation the corrected EP equals the plain EP.
    assert target[_IDX_EPERF_CORR] == pytest.approx(0.8)


def test_step1_elevation_correction_uses_the_gravitational_term():
    donor = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    target = _row(
        source="soc_estimate",
        start="2025-01-02T08:00:00Z",
        soc=-20.0,
        dist=100.0,
        elev=200.0,
        mass=30000.0,
    )
    _correct([donor, target])
    e_grav = 30000.0 * 9.81 * 200.0 / 3_600_000.0
    assert target[_IDX_EPERF_CORR] == pytest.approx(round((80.0 - e_grav) / 100.0, 4))


def test_step1_ignores_a_soc_estimate_row_without_a_soc_change():
    donor = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    target = _row(source="soc_estimate", start="2025-01-02T08:00:00Z", dist=100.0)
    _correct([donor, target])
    assert math.isnan(target[_IDX_CAP])


# ── Step 2: the +-1 sigma outlier gate ───────────────────────────────────────


def _three_inliers():
    return [
        _discharge_donor("2025-01-01T08:00:00Z", 400.0),
        _discharge_donor("2025-01-02T08:00:00Z", 400.0),
        _discharge_donor("2025-01-03T08:00:00Z", 400.0),
    ]


def test_step2_mode_a_corrects_only_the_capacity_column():
    outlier = _row(
        source="total_energy",
        start="2025-01-04T08:00:00Z",
        cap=900.0,
        soc=-11.0,
        energy=-100.0,
        dist=100.0,
        eperf=1.0,
    )
    rows = [*_three_inliers(), outlier]
    _correct(rows)

    # mean 525, sigma 216.5 -> the 900 kWh implied capacity is an outlier.
    assert outlier[_IDX_CAP] == 400.0
    # MODE A: the counter's energy and every derived metric survive untouched.
    assert outlier[_IDX_ENERGY] == -100.0
    assert outlier[_IDX_EPERF] == 1.0
    assert outlier[_IDX_ESOURCE] == "total_energy"


def test_step2_leaves_inliers_alone():
    rows = [
        *_three_inliers(),
        _row(
            source="total_energy",
            start="2025-01-04T08:00:00Z",
            cap=900.0,
            soc=-11.0,
            energy=-100.0,
            dist=100.0,
            eperf=1.0,
        ),
    ]
    _correct(rows)
    for row in rows[:3]:
        assert row[_IDX_CAP] == 400.0
        assert row[_IDX_ENERGY] == pytest.approx(-160.0)


def test_step2_re_derives_energy_for_a_soc_estimate_outlier():
    # A soc_estimate row whose step-1 capacity lands far from the body: its
    # energy IS SOC-derived, so it must be recomputed from the replacement.
    rows = [
        _charge_donor("2025-01-01T08:00:00Z", 400.0),
        _charge_donor("2025-01-02T08:00:00Z", 400.0),
        _charge_donor("2025-01-03T08:00:00Z", 400.0),
        _charge_donor("2025-07-01T08:00:00Z", 1000.0),
        _row(
            source="soc_estimate", start="2025-07-02T08:00:00Z", soc=-20.0, dist=100.0
        ),
    ]
    _correct(rows)
    target = rows[-1]
    # Step 1 gives it the 1000 kWh July donor; step 2 finds that an outlier and
    # replaces it with the inlier body, re-deriving the energy.
    assert target[_IDX_CAP] == 400.0
    assert target[_IDX_ENERGY] == pytest.approx(-80.0)
    assert target[_IDX_EPERF] == pytest.approx(0.8)


def test_step2_is_skipped_with_fewer_than_three_capacities():
    donor = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    outlier = _row(
        source="total_energy",
        start="2025-01-02T08:00:00Z",
        cap=5000.0,
        soc=-10.0,
        energy=-500.0,
        dist=100.0,
        eperf=5.0,
    )
    _correct([donor, outlier])
    assert outlier[_IDX_CAP] == 5000.0  # untouched: no sigma to compute


def test_step2_reports_the_period_mean_after_correction():
    outlier = _row(
        source="total_energy",
        start="2025-01-04T08:00:00Z",
        cap=900.0,
        soc=-11.0,
        energy=-100.0,
        dist=100.0,
        eperf=1.0,
    )
    rows = [*_three_inliers(), outlier]
    _rows, eff_cap, source = _correct(rows)
    assert source == "discharge"
    assert eff_cap == 400.0  # every row now reads 400 kWh


# ── Step 2: the opt-in SOC-energy fallback dual gate ─────────────────────────

FALLBACK = {"enabled": True, "min_dsoc_pct": 10.0, "min_dev": 0.30}


def test_soc_fallback_fires_when_both_gates_are_met():
    outlier = _row(
        source="total_energy",
        start="2025-01-04T08:00:00Z",
        cap=900.0,  # deviation |900-400|/400 = 1.25 >= 0.30
        soc=-11.0,  # |dSOC| = 11 >= 10
        energy=-100.0,
        dist=100.0,
        eperf=1.0,
    )
    rows = [*_three_inliers(), outlier]
    _correct(rows, soc_fallback=FALLBACK)

    assert outlier[_IDX_CAP] == 400.0
    assert outlier[_IDX_ESOURCE] == "soc_fallback"
    assert outlier[_IDX_ENERGY] == pytest.approx(-44.0)  # -11 % x 400 kWh
    assert outlier[_IDX_EPERF] == pytest.approx(0.44)


def test_soc_fallback_does_not_fire_on_a_small_soc_change():
    outlier = _row(
        source="total_energy",
        start="2025-01-04T08:00:00Z",
        cap=900.0,
        soc=-5.0,  # below min_dsoc_pct -> integer-% quantisation dominates
        energy=-100.0,
        dist=100.0,
        eperf=1.0,
    )
    rows = [*_three_inliers(), outlier]
    _correct(rows, soc_fallback=FALLBACK)
    assert outlier[_IDX_ESOURCE] == "total_energy"  # MODE A
    assert outlier[_IDX_ENERGY] == -100.0
    assert outlier[_IDX_CAP] == 400.0


def test_soc_fallback_does_not_fire_on_a_small_deviation():
    outlier = _row(
        source="total_energy",
        start="2025-01-04T08:00:00Z",
        cap=480.0,  # deviation |480-400|/400 = 0.20 < 0.30
        soc=-20.0,
        energy=-96.0,
        dist=100.0,
        eperf=0.96,
    )
    rows = [*_three_inliers(), outlier]
    _correct(rows, soc_fallback=FALLBACK)
    assert outlier[_IDX_ESOURCE] == "total_energy"
    assert outlier[_IDX_ENERGY] == -96.0
    assert outlier[_IDX_CAP] == 400.0  # the capacity column is still corrected


def test_soc_fallback_is_off_by_default():
    outlier = _row(
        source="total_energy",
        start="2025-01-04T08:00:00Z",
        cap=900.0,
        soc=-11.0,
        energy=-100.0,
        dist=100.0,
        eperf=1.0,
    )
    rows = [*_three_inliers(), outlier]
    _correct(rows, soc_fallback=None)
    assert outlier[_IDX_ESOURCE] == "total_energy"
    assert outlier[_IDX_ENERGY] == -100.0


def test_soc_fallback_rows_are_excluded_as_inlier_donors_on_replay():
    # A replay of already-corrected rows must not treat 'soc_fallback' energy as
    # a measured donor.
    rows = [
        *_three_inliers(),
        _row(
            source="soc_fallback",
            start="2025-01-04T08:00:00Z",
            cap=400.0,
            soc=-11.0,
            energy=-44.0,
            dist=100.0,
            eperf=0.44,
        ),
    ]
    _rows, _eff, source = _correct(rows)
    assert source == "discharge"  # the soc_fallback row did not become a donor


# ── Mutation contract ────────────────────────────────────────────────────────


def test_correct_effective_capacity_mutates_its_rows_in_place():
    donor = _charge_donor("2025-01-01T08:00:00Z", 400.0)
    target = _row(
        source="soc_estimate", start="2025-01-02T08:00:00Z", soc=-20.0, dist=100.0
    )
    rows = [donor, target]
    out_rows, _eff, _src = _correct(rows)
    assert out_rows is rows
    assert out_rows[1] is target
    assert target[_IDX_CAP] == 400.0  # the caller's own object was rewritten


def test_empty_row_list_is_a_clean_fallback():
    rows, eff_cap, source = _correct([], fallback_kwh=None)
    assert (rows, eff_cap, source) == ([], None, "fallback")
