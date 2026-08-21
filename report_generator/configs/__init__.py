"""Shared configuration directory.

Provides ``CONFIGS_DIR`` and ``get_config_path()`` so every module locates
``vehicles.json`` and ``pipelines.json`` uniformly.

By default these live inside the vendored package. Set ``JOLT_CONFIG_DIR`` to a
writable directory holding both JSONs to override the location — this is also
where the ``effective_capacity`` write-back lands, so a read-only deployment of
the package directory can still persist the capacity ledger.
"""

import os
from pathlib import Path

CONFIGS_DIR: Path = Path(__file__).resolve().parent


def get_config_path(name: str) -> Path:
    """Return the absolute path to ``name`` under the active config directory.

    Uses ``JOLT_CONFIG_DIR`` when it is set and non-empty; otherwise the
    vendored ``configs/`` directory (the historical default).
    """
    override = os.environ.get("JOLT_CONFIG_DIR")
    if override:
        return Path(override) / name
    return CONFIGS_DIR / name
