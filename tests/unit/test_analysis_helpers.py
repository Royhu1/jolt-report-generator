"""``jolt_toolkit.analysis``: the battery-efficiency model, cumulative-counter
interpolation and the OLS helpers.

Expected values come from the closed forms in the module docstrings (Arrhenius,
linear interpolation, ordinary least squares), computed independently here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from jolt_toolkit import analysis
from jolt_toolkit.analysis import counters, physics, stats

# ── physics.eta_bat ──────────────────────────────────────────────────────────


def test_eta_bat_is_exactly_one_at_the_reference_temperature():
    assert physics.eta_bat(25.0) == 1.0


def test_eta_bat_is_about_0_95_at_zero_celsius():
    # The alpha coefficient is calibrated so eta_bat(0 C) ~ 0.95.
    assert physics.eta_bat(0.0) == pytest.approx(0.95, abs=0.005)


def test_eta_bat_matches_the_arrhenius_closed_form():
    T, B, alpha, T_ref = -5.0, 3500.0, 0.027, 25.0
    r_ratio = np.exp(B * (1.0 / (T + 273.15) - 1.0 / (T_ref + 273.15)))
    expected = min(1.0, 1.0 - alpha * (r_ratio - 1.0))
    assert physics.eta_bat(T) == pytest.approx(expected)


def test_eta_bat_is_monotonically_non_decreasing_in_temperature():
    temps = np.arange(-15.0, 40.0, 1.0)
    values = [physics.eta_bat(float(t)) for t in temps]
    assert all(b >= a - 1e-12 for a, b in zip(values, values[1:]))


def test_eta_bat_is_capped_at_one_above_the_reference():
    assert physics.eta_bat(35.0) == 1.0
    assert physics.eta_bat(60.0) == 1.0


def test_eta_bat_stays_in_the_unit_interval_over_the_fleet_range():
    for t in np.arange(-20.0, 45.0, 0.5):
        value = physics.eta_bat(float(t))
        assert 0.0 < value <= 1.0


def test_eta_bat_returns_a_plain_float():
    assert isinstance(physics.eta_bat(10.0), float)


# ── counters.build_interp / delta ────────────────────────────────────────────


def _tel_frame(times, values, col=counters.COL_TOTAL):
    return pd.DataFrame({"eventDatetime": times, col: values})


TIMES = [
    "2025-06-27T08:00:00Z",
    "2025-06-27T09:00:00Z",
    "2025-06-27T10:00:00Z",
]
VALUES = [1000.0, 3000.0, 4000.0]  # Wh, cumulative


def test_build_interp_returns_sorted_ns_times_and_values():
    times, values = counters.build_interp(_tel_frame(TIMES, VALUES), counters.COL_TOTAL)
    assert list(values) == VALUES
    assert list(times) == sorted(times)
    # Nanosecond resolution: one hour apart.
    assert times[1] - times[0] == 3_600 * 1_000_000_000


def test_build_interp_drops_duplicate_timestamps_and_sorts():
    frame = _tel_frame([TIMES[1], TIMES[0], TIMES[1]], [3000.0, 1000.0, 3333.0])
    times, values = counters.build_interp(frame, counters.COL_TOTAL)
    assert len(times) == 2
    assert list(values) == [1000.0, 3000.0]


def test_build_interp_missing_column_is_none():
    assert counters.build_interp(_tel_frame(TIMES, VALUES), "no_such_col") is None


def test_build_interp_needs_two_valid_samples():
    frame = _tel_frame(TIMES, [1000.0, np.nan, np.nan])
    assert counters.build_interp(frame, counters.COL_TOTAL) is None


def test_delta_interpolates_and_converts_to_kwh():
    interp = counters.build_interp(_tel_frame(TIMES, VALUES), counters.COL_TOTAL)
    # 08:30 -> 2000 Wh, 09:30 -> 3500 Wh  =>  1500 Wh = 1.5 kWh
    out = counters.delta(
        interp,
        pd.Timestamp("2025-06-27T08:30:00Z"),
        pd.Timestamp("2025-06-27T09:30:00Z"),
    )
    assert out == pytest.approx(1.5)


def test_delta_out_of_range_is_nan():
    interp = counters.build_interp(_tel_frame(TIMES, VALUES), counters.COL_TOTAL)
    before = counters.delta(
        interp,
        pd.Timestamp("2025-06-27T07:00:00Z"),
        pd.Timestamp("2025-06-27T09:00:00Z"),
    )
    after = counters.delta(
        interp,
        pd.Timestamp("2025-06-27T08:00:00Z"),
        pd.Timestamp("2025-06-27T11:00:00Z"),
    )
    assert np.isnan(before) and np.isnan(after)


def test_delta_none_interp_is_nan():
    assert np.isnan(
        counters.delta(
            None,
            pd.Timestamp("2025-06-27T08:00:00Z"),
            pd.Timestamp("2025-06-27T09:00:00Z"),
        )
    )


def test_to_utc_localises_naive_and_preserves_aware():
    assert counters.to_utc("2025-06-27 08:00:00").tzinfo is not None
    aware = pd.Timestamp("2025-06-27T08:00:00+02:00")
    assert counters.to_utc(aware) == aware


def test_counter_column_names_are_the_raw_telematics_spellings():
    assert counters.COL_TOTAL == "total_electric_energy_used_plugged_in_included"
    assert counters.COL_PROP == "electric_energy_propulsion"
    assert counters.COL_RECUP == "electric_energy_recuperation_watthours"
    assert counters.MIN_DIST_KM == 3.0


# ── stats.ols / ols_hc1 ──────────────────────────────────────────────────────
#
# x = [1, 2, 3, 4], y = [2, 4, 5, 4]
#   xbar = 2.5, ybar = 3.75, Sxx = 5.0, Sxy = 3.5
#   beta1 = Sxy/Sxx = 0.7 ; beta0 = ybar - beta1*xbar = 2.0
#   residuals = [-0.7, 0.6, 0.9, -0.8] -> SSR = 2.30, dof = 2, s2 = 1.15
#   se(beta1) = sqrt(s2/Sxx)                     = sqrt(0.23)
#   se(beta0) = sqrt(s2*(1/n + xbar^2/Sxx))      = sqrt(1.725)
#   SST = 4.75 -> R^2 = 1 - 2.30/4.75

X = np.array([[1.0], [2.0], [3.0], [4.0]])
Y = np.array([2.0, 4.0, 5.0, 4.0])


def test_ols_beta_matches_the_closed_form():
    beta, se, p = stats.ols(Y, X)
    assert beta[0] == pytest.approx(2.0)
    assert beta[1] == pytest.approx(0.7)
    assert se[0] == pytest.approx(np.sqrt(1.725))
    assert se[1] == pytest.approx(np.sqrt(0.23))
    assert all(0.0 <= v <= 1.0 for v in p)


def test_ols_adds_the_intercept_itself():
    beta, _, _ = stats.ols(Y, X)
    assert len(beta) == X.shape[1] + 1


def test_ols_returns_nan_errors_when_there_are_no_degrees_of_freedom():
    beta, se, p = stats.ols(np.array([1.0, 2.0]), np.array([[1.0], [2.0]]))
    assert np.isnan(se).all() and np.isnan(p).all()


def test_ols_hc1_shares_the_point_estimates_and_reports_r_squared():
    plain_beta, plain_se, _ = stats.ols(Y, X)
    fit = stats.ols_hc1(Y, X)
    assert fit["beta"] == pytest.approx(plain_beta)
    assert fit["n"] == 4
    assert fit["r2"] == pytest.approx(1 - 2.30 / 4.75)
    # HC1 is a DIFFERENT variance estimator, so the standard errors must not be
    # the homoskedastic ones — but they stay finite and positive.
    assert np.all(np.isfinite(fit["se"])) and np.all(fit["se"] > 0)
    assert fit["se"][1] != pytest.approx(plain_se[1])


def test_ols_hc1_recovers_an_exact_relationship():
    x = np.arange(1.0, 11.0).reshape(-1, 1)
    y = (3.0 * x[:, 0] + 1.0) + np.array([0.1, -0.1] * 5)
    fit = stats.ols_hc1(y, x)
    assert fit["beta"][1] == pytest.approx(3.0, abs=0.05)
    assert fit["r2"] > 0.999


def test_vif_is_one_for_an_orthogonal_design():
    rng = np.random.default_rng(0)
    x1 = np.arange(50.0)
    x2 = rng.normal(size=50)
    out = stats.vif(np.column_stack([x1, x2]))
    assert all(v == pytest.approx(1.0, abs=0.2) for v in out)


def test_vif_explodes_for_a_collinear_design():
    x1 = np.arange(50.0)
    x2 = 2.0 * x1 + 1e-6
    out = stats.vif(np.column_stack([x1, x2]))
    assert max(out) > 100


def test_vif_single_regressor_is_one():
    assert stats.vif(np.arange(10.0).reshape(-1, 1)) == [1.0]


# ── stats.fit_block ──────────────────────────────────────────────────────────


def _fit_frame(n, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n)
    return pd.DataFrame({"y": 2.0 * x + rng.normal(scale=0.1, size=n), "x": x})


def test_fit_block_minimum_n_gate():
    # The gate is max(30, 5 * len(xcols)); with one regressor that is 30.
    assert stats.fit_block(_fit_frame(29), "y", ["x"]) is None
    assert stats.fit_block(_fit_frame(30), "y", ["x"]) is not None


def test_fit_block_gate_scales_with_the_regressor_count():
    rng = np.random.default_rng(2)
    n = 34
    frame = pd.DataFrame({f"x{i}": rng.normal(size=n) for i in range(7)})
    frame["y"] = frame.sum(axis=1)
    # max(30, 5*7) = 35 > 34 rows -> refused.
    assert stats.fit_block(frame, "y", [f"x{i}" for i in range(7)]) is None


def test_fit_block_drops_rows_with_missing_values_before_the_gate():
    frame = _fit_frame(32)
    frame.loc[:4, "x"] = np.nan  # 27 usable rows
    assert stats.fit_block(frame, "y", ["x"]) is None


def test_fit_block_returns_the_documented_shape():
    out = stats.fit_block(_fit_frame(60), "y", ["x"])
    assert set(out) == {
        "n",
        "r2",
        "intercept",
        "beta",
        "se",
        "p",
        "std_beta",
        "vif",
    }
    assert out["n"] == 60
    assert out["beta"]["x"] == pytest.approx(2.0, abs=0.1)
    assert out["r2"] > 0.9
    assert out["vif"]["x"] == pytest.approx(1.0)


# ── stats.demean_within ──────────────────────────────────────────────────────


def test_demean_within_removes_each_group_mean():
    frame = pd.DataFrame({"reg": ["A", "A", "B", "B"], "ep": [1.0, 3.0, 10.0, 20.0]})
    out = stats.demean_within(frame, ["ep"])
    assert list(out["ep"]) == [-1.0, 1.0, -5.0, 5.0]
    # Original untouched (the helper copies).
    assert list(frame["ep"]) == [1.0, 3.0, 10.0, 20.0]


def test_demean_within_supports_a_custom_group_column():
    frame = pd.DataFrame({"veh": ["A", "A", "B"], "m": [2.0, 4.0, 9.0]})
    out = stats.demean_within(frame, ["m"], group_col="veh")
    assert list(out["m"]) == [-1.0, 1.0, 0.0]


def test_mass_spread_constant_is_exported():
    assert stats.MASS_SPREAD_MIN_KG == 2000.0
    assert analysis.MASS_SPREAD_MIN_KG == stats.MASS_SPREAD_MIN_KG
