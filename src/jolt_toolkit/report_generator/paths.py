"""Deployment-time path and endpoint resolution.

Small, env-overridable helpers so the toolkit can run against a writable cache
location and an alternate SRF API root without any code change. Every default
resolves relative to the repository root when the process is launched from
there, so existing caches keep hitting.
"""

from __future__ import annotations

import os
from pathlib import Path

# Cache root. Historically hardcoded as "./cache" (relative to the working
# directory). Override with JOLT_CACHE_DIR.
_DEFAULT_CACHE_DIR = "./cache"

# SRF API root. Override with SRF_API_ROOT.
_DEFAULT_SRF_API_ROOT = "https://data.csrf.ac.uk/api/"


def get_cache_dir() -> Path:
    """Return the cache root directory.

    Reads ``JOLT_CACHE_DIR`` (a writable directory holding the SRF HTTP/raw,
    weather and postcode caches); falls back to ``./cache`` — the same location
    the code previously hardcoded, when launched from the repository root.
    """
    return Path(os.environ.get("JOLT_CACHE_DIR") or _DEFAULT_CACHE_DIR)


def get_srf_api_root() -> str:
    """Return the SRF API root URL.

    Reads ``SRF_API_ROOT``; falls back to the production endpoint. This is the
    single source of the endpoint literal (previously duplicated across the
    generator and both patchers). A later refactor phase may relocate it into a
    dedicated SRF-session module.
    """
    return os.environ.get("SRF_API_ROOT") or _DEFAULT_SRF_API_ROOT
