"""Analysis utilities promoted from data_analysis_workspace sub-projects.

Promoted on 2026-06-11 under the sub-project-independence convention: stable
analysis machinery reused by 3+ ``data_analysis_workspace`` sub-projects is
hoisted into the versioned toolkit so consumers import these helpers from
``jolt_toolkit.analysis`` instead of cross-importing other sub-projects.

Modules
-------
counters : cumulative-counter interpolation at trip endpoints
stats    : OLS / HC1-OLS / VIF / fixed-effects demeaning helpers
physics  : Arrhenius battery-efficiency model (``eta_bat``)
"""

from .counters import (
    COL_PROP,
    COL_RECUP,
    COL_TOTAL,
    MIN_DIST_KM,
    build_interp,
    delta,
    to_utc,
)
from .physics import eta_bat
from .stats import (
    MASS_SPREAD_MIN_KG,
    demean_within,
    fit_block,
    ols,
    ols_hc1,
    vif,
)

__all__ = [
    # counters
    "COL_TOTAL",
    "COL_PROP",
    "COL_RECUP",
    "MIN_DIST_KM",
    "build_interp",
    "delta",
    "to_utc",
    # stats
    "MASS_SPREAD_MIN_KG",
    "ols",
    "ols_hc1",
    "vif",
    "fit_block",
    "demean_within",
    # physics
    "eta_bat",
]
