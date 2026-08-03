"""Ordinary-least-squares helpers for fleet-wide trip regressions.

Plain OLS (``ols``), HC1 heteroskedasticity-robust OLS (``ols_hc1``), variance
inflation factors (``vif``), a standardised-beta + VIF fit block (``fit_block``)
and within-group demeaning for fixed effects (``demean_within``).

Provenance
----------
Promoted on 2026-06-11 under the sub-project-independence convention (shared
analysis machinery lives in the versioned toolkit so sub-projects no longer
cross-import each other):

* ``ols`` —— from
  ``data_analysis_workspace/ep_temperature_decomposition/scripts/ep_temp_decomposition.py``
* ``MASS_SPREAD_MIN_KG`` / ``ols_hc1`` / ``vif`` / ``fit_block`` (was
  ``_fit_block``) / ``demean_within`` —— from
  ``data_analysis_workspace/regen_recovery_factors/scripts/regen_recovery_factors.py``
"""

# Source: data_analysis_workspace/{ep_temperature_decomposition,regen_recovery_factors}/scripts/*.py
# Promoted: 2026-06-11
# Reason: sub-project-independence convention (shared analysis machinery → versioned toolkit).

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

# Within-vehicle mass span above this value (kg) → mass is treated as variable
# (used to decide whether the per-vehicle mass slope is identified).
MASS_SPREAD_MIN_KG = 2000.0


def ols(y: np.ndarray, X: np.ndarray):
    """Return (beta, se, p); X excludes the intercept, which is added automatically."""
    n = len(y)
    Xd = np.column_stack([np.ones(n), X])
    beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    resid = y - Xd @ beta
    dof = n - Xd.shape[1]
    if dof <= 0:
        return beta, np.full_like(beta, np.nan), np.full_like(beta, np.nan)
    s2 = (resid @ resid) / dof
    cov = s2 * np.linalg.inv(Xd.T @ Xd)
    se = np.sqrt(np.diag(cov))
    tstat = beta / se
    p = 2 * stats.t.sf(np.abs(tstat), dof)
    return beta, se, p


def ols_hc1(y: np.ndarray, X: np.ndarray):
    """OLS + HC1 heteroskedasticity-robust SE. X excludes the intercept (added automatically). Returns a dict."""
    n = len(y)
    Xd = np.column_stack([np.ones(n), X])
    k = Xd.shape[1]
    beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    resid = y - Xd @ beta
    XtX_inv = np.linalg.inv(Xd.T @ Xd)
    # HC1: (n/(n-k)) * (X'X)^-1 X' diag(e^2) X (X'X)^-1
    meat = Xd.T @ (Xd * (resid**2)[:, None])
    cov = (n / (n - k)) * XtX_inv @ meat @ XtX_inv
    se = np.sqrt(np.diag(cov))
    tstat = np.divide(beta, se, out=np.full_like(beta, np.nan), where=se > 0)
    p = 2 * stats.t.sf(np.abs(tstat), max(n - k, 1))
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return {"beta": beta, "se": se, "p": p, "r2": r2, "n": n}


def vif(X: np.ndarray) -> list[float]:
    """VIF = 1/(1-R^2_k) for each regressor (X excludes the intercept)."""
    out = []
    for k in range(X.shape[1]):
        yk = X[:, k]
        others = np.delete(X, k, axis=1)
        if others.shape[1] == 0:
            out.append(1.0)
            continue
        fit = ols_hc1(yk, others)
        out.append(float(1.0 / max(1e-9, 1.0 - fit["r2"])))
    return out


def fit_block(df: pd.DataFrame, ycol: str, xcols: list[str]) -> dict | None:
    """Run HC1-OLS for the given (y, X), with standardised beta and VIF attached."""
    sub = df.dropna(subset=[ycol] + xcols)
    if len(sub) < max(30, 5 * len(xcols)):
        return None
    y = sub[ycol].to_numpy(float)
    X = sub[xcols].to_numpy(float)
    fit = ols_hc1(y, X)
    sd_y = float(np.std(y))
    sd_x = X.std(axis=0)
    std_beta = {
        xc: (float(fit["beta"][i + 1]) * float(sd_x[i]) / sd_y if sd_y > 0 else np.nan)
        for i, xc in enumerate(xcols)
    }
    vifs = vif(X)
    return {
        "n": int(fit["n"]),
        "r2": float(fit["r2"]),
        "intercept": float(fit["beta"][0]),
        "beta": {xc: float(fit["beta"][i + 1]) for i, xc in enumerate(xcols)},
        "se": {xc: float(fit["se"][i + 1]) for i, xc in enumerate(xcols)},
        "p": {xc: float(fit["p"][i + 1]) for i, xc in enumerate(xcols)},
        "std_beta": std_beta,
        "vif": {xc: float(vifs[i]) for i, xc in enumerate(xcols)},
    }


def demean_within(
    df: pd.DataFrame, cols: list[str], group_col: str = "reg"
) -> pd.DataFrame:
    """Within-group demeaning (vehicle fixed effects)."""
    out = df.copy()
    for c in cols:
        out[c] = out[c] - out.groupby(group_col)[c].transform("mean")
    return out
