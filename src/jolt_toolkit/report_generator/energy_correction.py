"""Battery-side energy correction for a trip's net elevation change."""

from __future__ import annotations

GRAVITY_M_S2 = 9.81
JOULES_PER_KWH = 3_600_000.0
ELEVATION_ENERGY_EFFICIENCY = 0.90


def battery_elevation_energy_kwh(
    elevation_m: float,
    mass_kg: float,
    efficiency: float = ELEVATION_ENERGY_EFFICIENCY,
) -> float:
    """Return the battery-side energy attributable to net elevation change.

    Positive elevation change represents an uphill trip: the battery must
    provide ``m g delta_h / efficiency``. Negative elevation change represents
    a downhill trip: at most ``efficiency * m g |delta_h|`` is recovered, so the
    returned value remains negative. This piecewise treatment keeps both energy
    flows physically bounded when one symmetric efficiency is assumed.
    """
    if not 0.0 < efficiency <= 1.0:
        raise ValueError("efficiency must be greater than 0 and no greater than 1")
    potential_energy_kwh = mass_kg * GRAVITY_M_S2 * elevation_m / JOULES_PER_KWH
    if elevation_m >= 0:
        return potential_energy_kwh / efficiency
    return potential_energy_kwh * efficiency
