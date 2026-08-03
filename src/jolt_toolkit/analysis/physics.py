"""Battery-efficiency physics model.

Arrhenius temperature dependence of lithium-ion (NMC/LFP) discharge efficiency
(``eta_bat``), shared by physics-based EP simulation and downstream analyses.

Provenance
----------
Promoted verbatim from ``research_projects/simulation/models/vehicle_physics.py``
on 2026-06-11 under the sub-project-independence convention; this is the
canonical home for ``eta_bat``.
"""

# Source: research_projects/simulation/models/vehicle_physics.py
# Promoted: 2026-06-11
# Reason: sub-project-independence convention (shared analysis machinery → versioned toolkit).

from __future__ import annotations

import numpy as np


# Battery-efficiency model (Arrhenius)
def eta_bat(
    T: float, B: float = 3500.0, alpha: float = 0.027, T_ref: float = 25.0
) -> float:
    """
    Arrhenius model of battery discharge efficiency vs temperature (NMC/LFP lithium-ion).

    Parameters
    ----------
    T     : ambient temperature (deg C)
    B     : activation-energy coefficient (K), typical 3500 K
    alpha : efficiency coefficient, calibrated so eta_bat(0 deg C) ~ 0.95
    T_ref : reference temperature (deg C); eta_bat = 1.0 here

    Returns
    -------
    float, battery discharge efficiency (0, 1]
    """
    R_ratio = np.exp(B * (1.0 / (T + 273.15) - 1.0 / (T_ref + 273.15)))
    return float(min(1.0, 1.0 - alpha * (R_ratio - 1.0)))
