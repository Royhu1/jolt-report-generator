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
``vehicles.json``) and written to it, and ``vehicles.json`` is never written. The
ledger file is never rewritten in place: each write replaces it atomically, so an
interrupted write leaves the previous ledger whole.

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

import contextlib
import json
import os
import stat
import tempfile
import time
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

#: Attempts at moving a newly written ledger onto the old one, and the pause
#: between them. On Windows the move fails with ``PermissionError`` while another
#: process — a sync client, an editor, a virus scanner — briefly holds the file.
_LEDGER_REPLACE_ATTEMPTS = 5
_LEDGER_REPLACE_DELAY_S = 0.2


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
    """Write the ledger in the ``vehicles.json`` format. The caller holds the lock.

    The write is atomic. The JSON goes to a temporary file in the ledger's own
    directory (``<ledger>.<random>.tmp``), is flushed and fsynced, and then
    replaces the ledger in a single ``os.replace``: a writer killed part-way, a
    full disk or an interrupted sync leaves the previous ledger whole, never a
    truncated file that the next read would reject, taking the capacity history
    with it. The bytes are those of a direct write, and the ledger keeps its
    permission bits (a new one gets those a direct write would have given it).
    A replace refused with ``PermissionError`` is retried a few times; when it
    still fails, or anything else goes wrong, the temporary file is removed and
    the error raised.
    """
    path = Path(path)
    mode = _ledger_file_mode(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None  # the file object owns the descriptor from here on
            json.dump(ledger, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        _replace_ledger_file(tmp_name, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        _discard_temporary_file(tmp_name)
        raise


def _ledger_file_mode(path: Path) -> int:
    """The permission bits for a rewritten ledger: its own, or a new file's."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        # What a direct ``open(path, "w")`` gives a new file: 0o666 less the
        # umask. The umask has no getter, so it is set and restored at once
        # (to the most restrictive value in between).
        umask = os.umask(0o077)
        os.umask(umask)
        return 0o666 & ~umask


def _replace_ledger_file(source: str, target: Path) -> None:
    """Move ``source`` onto ``target``, retrying while that is refused."""
    for attempt in range(1, _LEDGER_REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _LEDGER_REPLACE_ATTEMPTS:
                raise
            time.sleep(_LEDGER_REPLACE_DELAY_S)


def _discard_temporary_file(name: str) -> None:
    """Remove a temporary ledger file, as far as possible."""
    with contextlib.suppress(OSError):
        # Windows refuses to delete a read-only file (a copied read-only mode).
        os.chmod(name, stat.S_IREAD | stat.S_IWRITE)
    with contextlib.suppress(OSError):
        os.unlink(name)
