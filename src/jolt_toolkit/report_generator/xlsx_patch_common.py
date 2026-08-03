"""
report_generator.xlsx_patch_common
===================================
Shared scaffolding for the openpyxl-based xlsx patchers (charger / logger).

Consolidates the helpers that ``charger_patcher`` and ``logger_patcher``
previously copy-pasted verbatim (``_parse_report_filename`` / ``_cell_is_empty``
/ ``_to_timestamp``) and the ``SeparateBodyFileCache`` SRF-client construction
that those two patchers plus ``_generator._make_srf_data`` triplicated
(``make_srf_client``). Behaviour is unchanged — the moved helpers are the exact
prior bodies (``_cell_is_empty`` uses the single canonical form, provably equal
to logger's multi-line variant for every input), and ``make_srf_client``
reproduces the identical cache path / client parameters.

Chinese comments are preserved from the source; Phase 4 translates them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import srf_client

from jolt_toolkit.report_generator.paths import get_cache_dir, get_srf_api_root


def _parse_report_filename(path: Path) -> tuple[str, str, str] | None:
    """Parse (reg, date_start, date_end) from a report file name."""
    m = re.match(r"jolt_report_(\w+)_(\d{8})_(\d{8})\.xlsx$", path.name)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _cell_is_empty(cell) -> bool:
    """Return whether a cell is empty."""
    v = cell.value
    return v is None or (isinstance(v, str) and v.strip() == "")


def _to_timestamp(dt_val):
    """Convert a datetime value read by openpyxl to a pd.Timestamp (UTC)."""
    import pandas as pd

    if dt_val is None:
        return None
    try:
        ts = pd.Timestamp(dt_val)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts
    except Exception:
        return None


def make_srf_client(
    cache_dir: str | None = None,
    *,
    api_key: str | None = None,
    root: str | None = None,
    verify: bool = True,
):
    """Build an ``srf_client.SRFData`` with a ``SeparateBodyFileCache`` under
    ``<cache_dir>/srf_http``.

    Reproduces the identical construction that ``ChargerPatcher`` /
    ``LoggerPatcher`` and ``_generator._make_srf_data`` used verbatim: an
    on-disk HTTP body cache when ``cache_dir`` is truthy (default resolved via
    :func:`get_cache_dir`), the SRF API root from :func:`get_srf_api_root`
    (overridable), and ``verify`` passed straight through. ``api_key`` is used
    as-is (callers resolve it from the environment and log their own
    labelled missing-key warning).
    """
    if cache_dir is None:
        cache_dir = str(get_cache_dir())
    if root is None:
        root = get_srf_api_root()
    cache = None
    if cache_dir:
        from cachecontrol.caches import SeparateBodyFileCache

        srf_cache_path = os.path.join(cache_dir, "srf_http")
        os.makedirs(srf_cache_path, exist_ok=True)
        cache = SeparateBodyFileCache(srf_cache_path)
    return srf_client.SRFData(
        api_key=api_key,
        cache=cache,
        root=root,
        verify=verify,
    )
