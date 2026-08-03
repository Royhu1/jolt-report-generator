"""Effective-capacity arithmetic: the donor estimator, the quarterly ledger maths
and the small predicates that gate them.

These are the numbers that end up in ``Battery Capacity (kWh)`` and in the
``vehicles.json`` capacity ledger, so every expectation below is derived from the
documented closed form rather than from a previous run of the code.
"""

from __future__ import annotations

import numpy as np
import pytest

from jolt_toolkit.report_generator import capacity as cap

# ── _cap_is_valid ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, False),
        (float("nan"), False),
        (np.nan, False),
        (0.0, True),
        (-3.0, True),
        (420.5, True),
        (42, True),
        ("soc_estimate", False),  # np.isnan raises -> treated as invalid
        ([1, 2], False),
    ],
)
def test_cap_is_valid(value, expected):
    assert cap._cap_is_valid(value) is expected


# ── _soc_weighted_cap ────────────────────────────────────────────────────────


def test_soc_weighted_cap_matches_the_combined_ratio_closed_form():
    # Two donors: C1 = 400 kWh at |dSOC| = 40 %, C2 = 600 kWh at |dSOC| = 10 %.
    #   dE1 = 400 * 0.40 = 160 kWh ; dE2 = 600 * 0.10 = 60 kWh
    #   C_eff = 100 * (160 + 60) / (40 + 10) = 22000 / 50 = 440 kWh
    caps = [400.0, 600.0]
    weights = [40.0, 10.0]
    assert cap._soc_weighted_cap(caps, weights) == pytest.approx(440.0)

    # The equivalent 100*SUM|dE|/SUM|dSOC| spelling of the same estimator.
    energies = [c * w / 100.0 for c, w in zip(caps, weights)]
    assert cap._soc_weighted_cap(caps, weights) == pytest.approx(
        100.0 * sum(energies) / sum(weights)
    )


def test_soc_weighted_cap_removes_the_small_dsoc_upward_bias():
    # The documented failure mode: a plain mean of per-leg implied capacities is
    # dragged up by the noisy small-|dSOC| donor.
    caps = [400.0, 600.0]
    weights = [40.0, 10.0]
    plain_mean = float(np.mean(caps))
    weighted = cap._soc_weighted_cap(caps, weights)
    assert plain_mean == pytest.approx(500.0)
    assert weighted < plain_mean


def test_soc_weighted_cap_signed_weights_use_magnitude():
    # Discharge donors carry a negative SOC change; only the magnitude weights.
    assert cap._soc_weighted_cap([400.0, 600.0], [-40.0, -10.0]) == pytest.approx(440.0)


def test_soc_weighted_cap_empty_pool_is_none():
    assert cap._soc_weighted_cap([], []) is None


def test_soc_weighted_cap_degrades_to_plain_mean_when_total_weight_is_zero():
    assert cap._soc_weighted_cap([300.0, 500.0], [0.0, 0.0]) == pytest.approx(400.0)


def test_soc_weighted_cap_ignores_invalid_weights():
    # A NaN weight cannot be weighted, so that donor drops out of the sum but the
    # remaining donors still produce a value.
    out = cap._soc_weighted_cap([400.0, 600.0], [40.0, float("nan")])
    assert out == pytest.approx(400.0)


# ── _period_capacity_from_rows ───────────────────────────────────────────────
#
# Rows are (cap, soc, src) triples read with idx 0/1/2 — the same convention the
# xlsx backfill uses, so live and backfill agree.

IDX = (0, 1, 2)


def _triples(*rows):
    return list(rows)


def test_period_capacity_prefers_charge_donors_over_discharge():
    rows = _triples(
        (400.0, 40.0, "ac_dc"),  # charge
        (600.0, 10.0, "ac_dc"),  # charge
        (900.0, -50.0, "total_energy"),  # discharge, must be ignored
    )
    kwh, n, source = cap._period_capacity_from_rows(rows, *IDX)
    assert source == "charge"
    assert n == 2
    assert kwh == pytest.approx(440.0)


def test_period_capacity_falls_back_to_discharge_donors():
    rows = _triples(
        (500.0, -20.0, "total_energy"),
        (300.0, -20.0, "moving_energy"),
    )
    kwh, n, source = cap._period_capacity_from_rows(rows, *IDX)
    assert (source, n) == ("discharge", 2)
    assert kwh == pytest.approx(400.0)


@pytest.mark.parametrize("excluded_source", ["soc_estimate", "soc_fallback"])
def test_period_capacity_excludes_soc_derived_sources(excluded_source):
    rows = _triples(
        (510.0, 30.0, excluded_source),
        (510.0, -30.0, excluded_source),
    )
    assert cap._period_capacity_from_rows(rows, *IDX) == (None, 0, "fallback")


def test_period_capacity_rejects_non_positive_and_invalid_capacities():
    rows = _triples(
        (0.0, 40.0, "ac_dc"),  # cap == 0 (a Stop row read back from xlsx)
        (-5.0, 40.0, "ac_dc"),  # negative cap
        (float("nan"), 40.0, "ac_dc"),  # NaN cap
        (400.0, float("nan"), "ac_dc"),  # unusable weight
        (400.0, 0.0, "ac_dc"),  # zero SOC change -> neither charge nor discharge
    )
    assert cap._period_capacity_from_rows(rows, *IDX) == (None, 0, "fallback")


def test_period_capacity_requires_a_string_energy_source():
    # openpyxl reads a Stop row's =NA() cells as None / "": neither is a donor.
    rows = _triples((400.0, 40.0, None), (400.0, 40.0, 0))
    assert cap._period_capacity_from_rows(rows, *IDX) == (None, 0, "fallback")


def test_period_capacity_empty_rows():
    assert cap._period_capacity_from_rows([], *IDX) == (None, 0, "fallback")


# ── _recompute_weighted_capacity ─────────────────────────────────────────────


def test_recompute_weighted_capacity_weights_by_donor_count():
    # reliable pool: (400 kWh, n=10) and (500 kWh, n=30)
    #   wavg = (400*10 + 500*30) / 40 = 19000 / 40 = 475.0
    quarterly = {
        "20250101_20250401": {"kwh": 400.0, "n": 10},
        "20250401_20250701": {"kwh": 500.0, "n": 30},
        "20250701_20251001": {"kwh": 900.0, "n": 2},  # sparse
    }
    wavg, n_reliable, n_sparse = cap._recompute_weighted_capacity(quarterly)
    assert wavg == 475.0
    assert (n_reliable, n_sparse) == (2, 1)


def test_recompute_weighted_capacity_backfills_sparse_quarters_in_place():
    quarterly = {
        "A": {"kwh": 400.0, "n": 10},
        "B": {"kwh": 500.0, "n": 30},
        "C": {"kwh": 900.0, "n": 2},
    }
    wavg, _, _ = cap._recompute_weighted_capacity(quarterly)
    # Mutated IN PLACE: the sparse quarter's stored kwh becomes the average,
    # its donor count is preserved for identification, reliable ones untouched.
    assert quarterly["C"] == {"kwh": wavg, "n": 2}
    assert quarterly["A"] == {"kwh": 400.0, "n": 10}
    assert quarterly["B"] == {"kwh": 500.0, "n": 30}


def test_recompute_weighted_capacity_degrades_to_all_donor_quarters():
    # No quarter reaches MIN_DONORS, but the ledger must still yield a number so
    # the PDF range does not go blank.
    #   wavg = (900*2 + 300*4) / 6 = 3000 / 6 = 500.0
    quarterly = {"C": {"kwh": 900.0, "n": 2}, "D": {"kwh": 300.0, "n": 4}}
    wavg, n_reliable, n_sparse = cap._recompute_weighted_capacity(quarterly)
    assert wavg == 500.0
    assert (n_reliable, n_sparse) == (0, 2)
    assert quarterly["C"]["kwh"] == 500.0 and quarterly["D"]["kwh"] == 500.0


def test_recompute_weighted_capacity_no_donor_quarter_leaves_scalar_alone():
    quarterly = {"E": {"kwh": None, "n": 0}, "F": {"kwh": 500.0, "n": 0}}
    assert cap._recompute_weighted_capacity(quarterly) == (None, 0, 0)


def test_recompute_weighted_capacity_empty_ledger():
    assert cap._recompute_weighted_capacity({}) == (None, 0, 0)


def test_recompute_weighted_capacity_honours_a_custom_min_donors():
    quarterly = {"A": {"kwh": 400.0, "n": 3}, "B": {"kwh": 600.0, "n": 3}}
    wavg, n_reliable, _ = cap._recompute_weighted_capacity(quarterly, min_donors=3)
    assert wavg == 500.0
    assert n_reliable == 2


def test_min_donors_default_is_the_documented_threshold():
    assert cap.MIN_DONORS == 5


# ── _resolve_soc_fallback ────────────────────────────────────────────────────


@pytest.mark.parametrize("cfg", [None, {}, {"soc_energy_fallback": False}])
def test_resolve_soc_fallback_not_opted_in(cfg):
    assert cap._resolve_soc_fallback(cfg) is None


def test_resolve_soc_fallback_defaults():
    assert cap._resolve_soc_fallback({"soc_energy_fallback": True}) == {
        "enabled": True,
        "min_dsoc_pct": cap.SOC_FALLBACK_MIN_DSOC_PCT,
        "min_dev": cap.SOC_FALLBACK_MIN_DEV,
    }


def test_resolve_soc_fallback_per_vehicle_overrides():
    out = cap._resolve_soc_fallback(
        {
            "soc_energy_fallback": True,
            "soc_fallback_min_dsoc_pct": 20,
            "soc_fallback_min_dev": "0.5",
        }
    )
    assert out == {"enabled": True, "min_dsoc_pct": 20.0, "min_dev": 0.5}


def test_resolve_soc_fallback_bad_override_falls_back_to_the_default():
    out = cap._resolve_soc_fallback(
        {
            "soc_energy_fallback": True,
            "soc_fallback_min_dsoc_pct": "not-a-number",
            "soc_fallback_min_dev": None,
        }
    )
    assert out["min_dsoc_pct"] == cap.SOC_FALLBACK_MIN_DSOC_PCT
    assert out["min_dev"] == cap.SOC_FALLBACK_MIN_DEV


# ── _row_idx / the hard-coded index constants ────────────────────────────────


def test_row_idx_offsets_by_the_leg_number_column():
    from jolt_toolkit.report_generator.columns import HEADERS

    assert HEADERS[0] == "Leg Number"
    for name in ("Leg Type", "Battery Capacity (kWh)", "Energy Source"):
        assert cap._row_idx(name) == HEADERS.index(name) - 1
