"""Shared configuration: where the config files live and how they are loaded.

``vehicles.json`` holds two kinds of data, and this module keeps them apart:

* **Parameters** — the reviewed per-vehicle settings (column names, pipeline,
  nominal capacity, mass aggregation, …), plus the named segmentation parameter
  sets in ``pipelines.json``. They change only through a reviewed edit.
* **State** — the effective-capacity ledger, the two keys in :data:`LEDGER_KEYS`,
  which every EV report writes back.

By default both live in ``vehicles.json`` inside the vendored ``configs/``
directory, and ``JOLT_CONFIG_DIR`` moves that whole directory. Setting
``JOLT_CAPACITY_LEDGER`` to the path of a JSON file separates the state from the
parameters: the ledger keys are then read from that file (overlaid on
``vehicles.json``) and written to it, and ``vehicles.json`` is never written.

Consumers read the configs through the loaders below, never by file path:

  get_config_path(name)       path of a config file in the active directory
  get_capacity_ledger_path()  the external capacity-ledger file, or None
  load_vehicle_configs()      a fresh read of vehicles.json, ledger overlaid
  load_pipeline_configs()     a fresh read of pipelines.json
  apply_capacity_ledger(v)    overlay the ledger again onto configs already loaded

The package's shared ``VEHICLE_CONFIG`` is loaded once, at import, but the
ledger variable is read whenever it is needed: every report re-applies the
ledger to ``VEHICLE_CONFIG`` (:func:`apply_capacity_ledger`) before it reads the
vehicle's config, so a variable set after the import is still honoured.

Ledger file schema (one entry per registration; only the two ledger keys are
read, and a key an entry does not carry leaves the ``vehicles.json`` value)::

  {"<REG>": {"effective_capacity_kwh": <float|null>,
             "effective_capacity_quarterly": {"<YYYYMMDD_YYYYMMDD>":
                                              {"kwh": <float>, "n": <int>}}}}
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock, Timeout

CONFIGS_DIR: Path = Path(__file__).resolve().parent

#: Environment variable naming the external capacity-ledger file.
CAPACITY_LEDGER_ENV_VAR = "JOLT_CAPACITY_LEDGER"

#: The per-vehicle keys that are machine-written state rather than parameters.
LEDGER_KEYS: tuple[str, ...] = (
    "effective_capacity_kwh",
    "effective_capacity_quarterly",
)


def get_config_path(name: str) -> Path:
    """Return the absolute path to ``name`` under the active config directory.

    Uses ``JOLT_CONFIG_DIR`` when it is set and non-empty; otherwise the
    vendored ``configs/`` directory (the historical default).
    """
    override = os.environ.get("JOLT_CONFIG_DIR")
    if override:
        return Path(override) / name
    return CONFIGS_DIR / name


def get_capacity_ledger_path() -> Path | None:
    """Return the external capacity-ledger file, or ``None`` when there is none.

    Read from ``JOLT_CAPACITY_LEDGER`` at call time. Unset, empty or blank means
    no external ledger: the ledger keys then live in ``vehicles.json``. A
    relative path resolves against the working directory, so deployments should
    give an absolute one.
    """
    value = os.environ.get(CAPACITY_LEDGER_ENV_VAR, "").strip()
    return Path(value) if value else None


def load_pipeline_configs() -> dict:
    """Return a fresh read of ``pipelines.json`` from the active directory."""
    return _load_config_json("pipelines.json")


def load_vehicle_configs() -> dict:
    """Return a fresh read of ``vehicles.json`` with the capacity ledger overlaid.

    When ``JOLT_CAPACITY_LEDGER`` names a file, each registration present in
    both takes the ledger's ledger keys in place of the ``vehicles.json`` values
    (key by key: a key the ledger entry does not carry keeps the config value).
    A ledger registration that is not in ``vehicles.json`` is ignored — the
    ledger records state for configured vehicles, it cannot configure one. A
    ledger file that does not exist yet means no overlay, not an error. Without
    the variable the result is exactly the parsed ``vehicles.json``.
    """
    vehicles = _load_config_json("vehicles.json")
    apply_capacity_ledger(vehicles)
    return vehicles


def apply_capacity_ledger(
    vehicles: dict, *, skip: Callable[[dict], bool] | None = None
) -> Path | None:
    """Overlay the capacity ledger onto vehicle configs that are already loaded.

    ``vehicles`` maps each registration to its config dict — typically the
    package's shared in-memory ``VEHICLE_CONFIG``, which is loaded once, at
    import. The ledger file is looked up and read at call time, so a
    ``JOLT_CAPACITY_LEDGER`` set after the import is honoured. The overlay is
    the one :func:`load_vehicle_configs` applies — key by key, a registration
    not in ``vehicles`` ignored — written in place into the per-registration
    dicts, so applying it again is harmless. A config for which ``skip(cfg)`` is
    true is left alone.

    Without the variable this is a strict no-op: nothing is read and
    ``vehicles`` is not touched. Returns the ledger path applied, or ``None``.
    """
    ledger_path = get_capacity_ledger_path()
    if ledger_path is None:
        return None
    _overlay_capacity_ledger(vehicles, _read_capacity_ledger(ledger_path), skip=skip)
    return ledger_path


# ── Internals shared with the capacity write-back and backfill ───────────────


def _load_config_json(name: str) -> dict:
    """Load a JSON config file from the active config directory.

    Raises ``FileNotFoundError`` with an actionable message when the file is
    missing, rather than returning ``{}`` and surfacing much later as an empty
    ``VEHICLE_CONFIG`` and a cryptic 'vehicle not registered' error.
    """
    path = get_config_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file '{name}' not found at {path}. Either the vendored "
            f"report_generator/configs/ directory is incomplete, or "
            f"JOLT_CONFIG_DIR points at a directory that does not hold "
            f"vehicles.json and pipelines.json."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _overlay_capacity_ledger(
    vehicles: dict, ledger: dict, *, skip: Callable[[dict], bool] | None = None
) -> dict:
    """Overlay the ledger keys onto ``vehicles`` in place; return ``vehicles``."""
    for reg, entry in ledger.items():
        cfg = vehicles.get(reg)
        if cfg is None or (skip is not None and skip(cfg)):
            continue
        for key in LEDGER_KEYS:
            if key in entry:
                cfg[key] = entry[key]
    return vehicles


def _ledger_lock(path: Path) -> FileLock:
    """The lock guarding every read-modify-write of the ledger file."""
    return FileLock(str(path) + ".lock")


def _read_capacity_ledger(path: Path, *, lock: bool = True) -> dict:
    """Read the ledger file; ``{}`` when it does not exist or is blank.

    With ``lock`` (the default) the read happens under the ledger lock, so a
    concurrent writer is never observed half-way through its write. Where the
    lock file cannot be created at all — a read-only location, which no writer
    can be using either — the file is read without it. Pass ``lock=False`` when
    the caller already holds the lock, or must not create a lock file.

    Raises ``ValueError`` when the content is not an object mapping each
    registration to an object: a damaged ledger must fail loudly, never be read
    as empty and then overwritten.
    """
    path = Path(path)
    if not path.exists():
        return {}
    if lock:
        guard = _ledger_lock(path)
        try:
            guard.acquire()
        except Timeout:
            raise
        except OSError:
            guard = None
        try:
            text = path.read_text(encoding="utf-8")
        finally:
            if guard is not None:
                guard.release()
    else:
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict) or not all(
        isinstance(entry, dict) for entry in data.values()
    ):
        raise ValueError(
            f"Capacity ledger {path} must be a JSON object mapping each "
            f"registration to an object holding {', '.join(LEDGER_KEYS)}."
        )
    return data


def _write_capacity_ledger(path: Path, ledger: dict) -> None:
    """Write the ledger in the ``vehicles.json`` format. The caller holds the lock."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
        f.write("\n")
