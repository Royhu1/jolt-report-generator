"""Session-wide pytest configuration for the offline test suite.

Two things happen here that MUST happen before anything else, so they live at
module scope in the top-level ``tests/conftest.py`` (pytest imports it before it
imports any test module, and therefore before ``report_generator`` is first
imported):

1. ``JOLT_CACHE_DIR`` is pointed at a throwaway directory.
   ``report_generator/row_builder.py`` evaluates ``get_cache_dir()`` at IMPORT
   time to build ``_POSTCODE_CACHE_PATH`` and immediately loads that cache. If
   the variable is not already set, the module would anchor on ``./cache``
   relative to the working directory and could read (or, via
   ``_save_postcode_cache``, write) a real cache. Setting it here guarantees the
   suite never touches anything outside the temp directory.

2. ``SRF_API_KEY`` is forced to an empty string. No test may construct a real SRF
   client; making the key empty means a stray attempt fails loudly rather than
   silently authenticating.

The rest of the file provides the shared fixtures: the fixture directory, the
raw-telematics / logger loaders (matching production's ``read_csv`` options
exactly), and the frozen alias configs injected into the shared
``VEHICLE_CONFIG`` / ``PIPELINE_CONFIGS`` dictionaries.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pandas as pd
import pytest

# ── 1. Import-time environment (see the module docstring) ────────────────────
_TEST_CACHE_DIR = tempfile.mkdtemp(prefix="jolt_test_cache_")
os.environ["JOLT_CACHE_DIR"] = _TEST_CACHE_DIR
os.environ.setdefault("SRF_API_KEY", "")
# Never let a stray WeatherPatcher construction land on a real cache file.
os.environ.setdefault("WEATHER_CACHE_FILE", str(Path(_TEST_CACHE_DIR) / "weather.json"))
# OpenWeather key rotation reads this; empty means "no keys", so no request can
# ever be built even if a fetcher were constructed.
os.environ.setdefault("OPENWEATHER_API_KEYS", "")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest hook
    """Remove the throwaway cache directory created at import time."""
    shutil.rmtree(_TEST_CACHE_DIR, ignore_errors=True)


# ── Offline guarantee ────────────────────────────────────────────────────────


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a network socket.

    The suite is contractually offline (no SRF, no OpenWeather, no geocoding).
    Rather than trusting every call site, outbound sockets are disabled for the
    whole session; a test that needs remote data must inject a ``Mock`` instead.
    """


@pytest.fixture(scope="session", autouse=True)
def _block_network():
    """Disable outbound sockets for the entire session."""
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def _blocked(*args, **kwargs):
        raise NetworkAccessAttempted(
            "The JOLT test suite is offline-only; inject a Mock instead of "
            "calling out to SRF / OpenWeather / a geocoder."
        )

    socket.socket.connect = _blocked
    socket.create_connection = _blocked
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.create_connection = real_create_connection


# ── Fixture data ─────────────────────────────────────────────────────────────

#: Alias → relative path of the committed anonymised raw fixture.
RAW_FIXTURES = {
    "EVSPD01": "raw/EVSPD01/raw_2025-06-27_0000.csv",
    "EVSOC01": "raw/EVSOC01/raw_2026-04-24_0000.csv",
    "EVMAD01": "raw/EVMAD01/raw_2025-07-29_0000.csv",
    "DSL01": "raw/DSL01/logger_2025-10-07_0000.csv",
}

#: The per-alias leg suffix used when naming segmentation artefacts, derived
#: from the fixture file name (``<date>_<leg index>``).
FIXTURE_SUFFIXES = {
    alias: Path(rel).stem.split("_", 1)[1] for alias, rel in RAW_FIXTURES.items()
}


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Absolute path of ``tests/fixtures``."""
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def raw_fixture_path(fixtures_dir):
    """Return ``alias -> Path`` resolver for the committed raw CSV fixtures."""

    def _resolve(alias: str) -> Path:
        try:
            rel = RAW_FIXTURES[alias]
        except KeyError:  # pragma: no cover - guard against a typo in a test
            raise KeyError(
                f"Unknown fixture alias {alias!r}; known: {sorted(RAW_FIXTURES)}"
            ) from None
        path = fixtures_dir / rel
        assert path.exists(), f"missing committed fixture: {path}"
        return path

    return _resolve


@pytest.fixture(scope="session")
def load_raw_telematics(raw_fixture_path):
    """Load an EV raw-telematics fixture the way production loads it.

    ``_generator._process_fps_legs`` reads the cached SRF raw CSV with
    ``pd.read_csv(path, dtype=str)`` — every column arrives as a string and the
    segmentation code coerces per column. The loader mirrors that exactly so a
    test sees the same dtypes the real pipeline does.
    """

    def _load(alias: str) -> pd.DataFrame:
        return pd.read_csv(raw_fixture_path(alias), dtype=str)

    return _load


@pytest.fixture(scope="session")
def load_logger_csv(raw_fixture_path):
    """Load the diesel logger fixture the way production loads it.

    ``diesel_pipeline._logger_df_from_csv`` reads it with
    ``pd.read_csv(path, index_col=0)`` (no ``dtype=str``: the logger CSV is
    already numeric, and the first column is the timestamp index).
    """

    def _load(alias: str = "DSL01") -> pd.DataFrame:
        return pd.read_csv(raw_fixture_path(alias), index_col=0)

    return _load


# ── Frozen alias configs ─────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def frozen_config_data(fixtures_dir) -> dict:
    """The frozen ``vehicles.json`` / ``pipelines.json`` fixture pair.

    These are deliberately FROZEN copies keyed by anonymised aliases — never
    read the live ``report_generator/configs/*.json`` for a behavioural
    assertion, or a future parameter retune turns the suite red.
    """
    cfg_dir = fixtures_dir / "configs"
    with open(cfg_dir / "vehicles.json", encoding="utf-8") as fh:
        vehicles = json.load(fh)
    with open(cfg_dir / "pipelines.json", encoding="utf-8") as fh:
        pipelines = json.load(fh)
    return {"vehicles": vehicles, "pipelines": pipelines}


# ── Golden-file serialisation ────────────────────────────────────────────────


def _jsonable(value):
    """Convert one segment-dict value into a stable, JSON-round-trippable form."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, pd.Timestamp):
        # Keep the offset when the timestamp is tz-aware: the naive/aware split
        # is itself part of the behaviour a golden file should pin.
        return value.isoformat()
    if isinstance(value, float):
        # NaN has no JSON spelling; a sentinel keeps the diff readable.
        return "NaN" if value != value else round(value, 6)
    if isinstance(value, int):
        return value
    return str(value)


def serialise_segments(segments) -> list:
    """Serialise a list of segment dicts into a deterministic JSON structure.

    Used by BOTH the golden generator (``tests/fixtures/regenerate_goldens.py``)
    and the integration tests that assert against the goldens, so the two can
    never drift apart. Keys are sorted; the private ``_anchor_*`` fields are
    KEPT because the non-overlapping-anchor guarantee is part of the contract.
    """
    return [{key: _jsonable(seg[key]) for key in sorted(seg)} for seg in segments]


@pytest.fixture(scope="session")
def serialise():
    """The shared segment serialiser, as a fixture.

    Exposed this way (rather than ``from conftest import ...``) because a test
    module's plain ``import conftest`` resolves to the NEAREST conftest, which is
    the per-directory one.
    """
    return serialise_segments


@pytest.fixture(scope="session")
def raw_fixture_map() -> dict:
    """``alias -> relative fixture path`` (the same table the goldens record)."""
    return dict(RAW_FIXTURES)


@pytest.fixture
def frozen_configs(monkeypatch, frozen_config_data):
    """Inject the frozen alias configs into the shared config dictionaries.

    ``VEHICLE_CONFIG`` / ``PIPELINE_CONFIGS`` are loaded once in
    ``segmentation.constants`` and shared BY REFERENCE across the package, so the
    sanctioned injection point is ``monkeypatch.setitem`` on those very objects
    (production does the same for the runtime fallback config). ``monkeypatch``
    removes the alias keys again at teardown, leaving the live fleet entries
    untouched.

    Returns the ``{"vehicles": ..., "pipelines": ...}`` dict so a test can read
    the frozen values it just injected.
    """
    from report_generator.segmentation import constants

    for reg, cfg in frozen_config_data["vehicles"].items():
        monkeypatch.setitem(constants.VEHICLE_CONFIG, reg, cfg)
    for name, cfg in frozen_config_data["pipelines"].items():
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, name, cfg)
    return frozen_config_data
