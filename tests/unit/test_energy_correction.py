"""The battery-side elevation energy correction.

``energy_correction`` is the single place the ``m·g·Δh`` term is turned into a
battery-side quantity. Both corrected-EP columns and the capacity-correction
pass call it, so its efficiency semantics are the contract those three sites
agree on; the values below are hand-computed rather than derived from the module
so a change to the formula cannot make the test agree with itself.
"""

from __future__ import annotations

import math

import pytest

from report_generator.energy_correction import (
    ELEVATION_ENERGY_EFFICIENCY,
    GRAVITY_M_S2,
    JOULES_PER_KWH,
    battery_elevation_energy_kwh,
)
from report_generator.row_builder import _corrected_energy_perf

_MASS_KG = 30_000.0
_ELEVATION_M = 100.0
#: m·g·Δh / 3.6e6 = 30000 * 9.81 * 100 / 3_600_000
_RAW_POTENTIAL_KWH = 8.175


def test_the_module_constants_are_the_documented_ones():
    assert (GRAVITY_M_S2, JOULES_PER_KWH) == (9.81, 3_600_000.0)
    assert ELEVATION_ENERGY_EFFICIENCY == 0.90


def test_uphill_demand_divides_by_the_efficiency():
    """Climbing costs the battery more than the potential energy gained."""
    assert battery_elevation_energy_kwh(_ELEVATION_M, _MASS_KG, 0.9) == pytest.approx(
        _RAW_POTENTIAL_KWH / 0.9, abs=1e-9
    )


def test_downhill_recovery_multiplies_by_the_efficiency():
    """Descending returns only part of the potential energy, and stays negative."""
    recovered = battery_elevation_energy_kwh(-_ELEVATION_M, _MASS_KG, 0.9)
    assert recovered == pytest.approx(-_RAW_POTENTIAL_KWH * 0.9, abs=1e-9)
    assert recovered < 0


def test_a_level_trip_has_no_elevation_energy():
    assert battery_elevation_energy_kwh(0.0, _MASS_KG) == 0.0


def test_the_losses_are_one_directional():
    """The same hill costs more to climb than it repays on the descent."""
    uphill = battery_elevation_energy_kwh(_ELEVATION_M, _MASS_KG)
    downhill = battery_elevation_energy_kwh(-_ELEVATION_M, _MASS_KG)
    assert abs(uphill) > _RAW_POTENTIAL_KWH > abs(downhill)


def test_the_default_efficiency_is_ninety_percent():
    """The default is the symmetric η shared with the kinetics correction."""
    assert battery_elevation_energy_kwh(
        _ELEVATION_M, _MASS_KG
    ) == battery_elevation_energy_kwh(_ELEVATION_M, _MASS_KG, 0.90)


def test_unity_efficiency_collapses_to_the_plain_potential_energy():
    for elevation_m, expected in (
        (_ELEVATION_M, _RAW_POTENTIAL_KWH),
        (-_ELEVATION_M, -_RAW_POTENTIAL_KWH),
    ):
        assert battery_elevation_energy_kwh(
            elevation_m, _MASS_KG, 1.0
        ) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("efficiency", [0.0, -0.5, 1.5])
def test_an_efficiency_outside_the_valid_range_is_rejected(efficiency):
    """A non-physical efficiency must fail loudly rather than skew the report."""
    with pytest.raises(ValueError):
        battery_elevation_energy_kwh(_ELEVATION_M, _MASS_KG, efficiency)


def test_an_unknown_elevation_propagates_nan():
    """A missing elevation difference cannot become a silent zero correction."""
    assert math.isnan(battery_elevation_energy_kwh(math.nan, _MASS_KG))


def test_the_reported_corrected_ep_deducts_the_battery_side_energy():
    """``row_builder`` routes the report column through the shared helper."""
    e_uphill_kwh = _RAW_POTENTIAL_KWH / 0.9
    assert _corrected_energy_perf(-50.0, 10.0, _ELEVATION_M, _MASS_KG) == round(
        (50.0 - e_uphill_kwh) / 10.0, 4
    )
    assert _corrected_energy_perf(-50.0, 10.0, _ELEVATION_M, _MASS_KG) == 4.0917
