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
ledger file is never rewritten in place: each write replaces it atomically (and on
POSIX durably), so an interrupted write leaves the previous ledger whole.

Consumers read the configs through the loaders below, never by file path:

  get_config_path(name)       path of a config file in the active directory
  get_capacity_ledger_path()  the external capacity-ledger file, or None
  load_vehicle_configs()      a fresh read of vehicles.json, ledger overlaid
  load_pipeline_configs()     a fresh read of pipelines.json, checked keys validated
  apply_capacity_ledger(v)    bring the ledger keys of loaded configs up to date
  effective_vehicle_config(cfg, when)
                              a vehicle's settings as they apply on a date

A vehicle's segmentation settings can change from a given date on: its
``period_overrides`` list holds dated windows, each with the settings it changes
(only :data:`PERIOD_OVERRIDE_KEYS`). :func:`load_vehicle_configs` validates the
list, and every leg is segmented with the settings of the window containing its
date (:func:`effective_vehicle_config`)::

  "period_overrides": [{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD" | null,
                        "reason": "<text>", "set": {"<key>": <value>, ...}}]

``from`` is inclusive, ``to`` exclusive (null or absent: open-ended), and the
windows of one vehicle may not overlap.

The package's shared ``VEHICLE_CONFIG`` is loaded once, at import, but the
ledger variable is read whenever it is needed: before it reads the vehicle's
config, every report makes the ledger keys of ``VEHICLE_CONFIG`` exactly what a
fresh :func:`load_vehicle_configs` gives (:func:`apply_capacity_ledger`), so a
variable set after the import, pointed at another file or at an edited one is
honoured, and nothing survives from a ledger the reports no longer read.

Ledger file schema (one entry per registration; only the two ledger keys are
read, and a key an entry does not carry leaves the ``vehicles.json`` value)::

  {"<REG>": {"effective_capacity_kwh": <float|null>,
             "effective_capacity_quarterly": {"<YYYYMMDD_YYYYMMDD>":
                                              {"kwh": <float>, "n": <int>}}}}
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)

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

#: The vehicle-entry key holding its date-effective settings.
PERIOD_OVERRIDES_KEY = "period_overrides"


def _is_flag(value: object) -> bool:
    return isinstance(value, bool)


def _is_name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


#: The settings a period override may ``set``, each with the check its value must
#: pass — exactly the vehicle settings the per-leg segmentation reads:
#: ``run_segment_detection`` (the pipeline, the Logger-speed switch, the stop gap,
#: the mass split, merge and long-stop split, the cluster gap) and
#: ``resolve_mass_agg`` (the per-segment mass estimator).
_PERIOD_OVERRIDE_VALUES: dict[str, tuple[Callable[[object], bool], str]] = {
    "pipeline": (_is_name, "the name of a pipeline in pipelines.json"),
    "prefer_logger_speed": (_is_flag, "true or false"),
    "min_stop_duration_min": (_is_positive_number, "a positive number of minutes"),
    "split_by_mass": (_is_flag, "true or false"),
    "merge_by_mass": (_is_flag, "true or false"),
    "split_long_stops_min": (
        lambda v: v is None or _is_positive_number(v),
        "a positive number of minutes, or null",
    ),
    "min_cluster_gap_kg": (_is_positive_number, "a positive number of kg"),
    "mass_agg": (_is_name, "the name of a mass-aggregation method"),
}

#: The keys the ``set`` of a period override may carry.
PERIOD_OVERRIDE_KEYS: tuple[str, ...] = tuple(_PERIOD_OVERRIDE_VALUES)

_PERIOD_OVERRIDE_FIELDS = ("from", "to", "reason", "set")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: The ``pipelines.json`` keys whose value the loader checks, by where they sit —
#: at the top level of a pipeline, or in its ``speed_params`` — each with the
#: check its value must pass. Absent means off for both.
_PIPELINE_TOP_LEVEL_VALUES: dict[str, tuple[Callable[[object], bool], str]] = {
    "soc_event_spike_pct": (
        _is_positive_number,
        "a positive number of SOC percentage points",
    ),
}
_PIPELINE_SPEED_PARAMS_VALUES: dict[str, tuple[Callable[[object], bool], str]] = {
    "keep_trips_outside_cap_band": (_is_flag, "true or false"),
}

#: A pipeline's parameter groups, each handed to a detector as keyword arguments.
_PIPELINE_PARAM_GROUPS = ("charge_params", "discharge_params", "speed_params")


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
    """Return a fresh read of ``pipelines.json`` from the active directory.

    The keys with a checked value are validated here, so a malformed one fails
    the load — at import too, which goes through this function — with a
    ``ValueError`` naming the pipeline and the key: ``soc_event_spike_pct``
    (top level) that is not a positive number, ``keep_trips_outside_cap_band``
    (in ``speed_params``) that is not ``true`` / ``false``, or either key in the
    wrong place, where it would be silently ignored or break a detector.
    """
    pipelines = _load_config_json("pipelines.json")
    _validate_pipeline_configs(pipelines)
    return pipelines


def load_vehicle_configs() -> dict:
    """Return a fresh read of ``vehicles.json`` with the capacity ledger overlaid.

    When ``JOLT_CAPACITY_LEDGER`` names a file, each registration present in
    both takes the ledger's ledger keys in place of the ``vehicles.json`` values
    (key by key: a key the ledger entry does not carry keeps the config value).
    A ledger registration that is not in ``vehicles.json`` is ignored — the
    ledger records state for configured vehicles, it cannot configure one. A
    ledger file that does not exist yet means no overlay, not an error. Without
    the variable the result is exactly the parsed ``vehicles.json``.

    Every vehicle's ``period_overrides`` are validated here, so a malformed one
    fails the load — at import too, which goes through this function — with a
    ``ValueError`` naming the vehicle and the override: a ``set`` key outside
    :data:`PERIOD_OVERRIDE_KEYS` or a value of the wrong kind, a missing or empty
    ``set``, a date that is not ``YYYY-MM-DD``, ``to`` not after ``from``,
    overlapping windows, a ``pipeline`` that is not in ``pipelines.json``, an
    unknown field, or the field on a DIESEL vehicle (whose legs are not
    segmented through the path the overrides feed).
    """
    return _load_vehicle_configs(get_capacity_ledger_path())


def apply_capacity_ledger(
    vehicles: dict, *, skip: Callable[[dict], bool] | None = None
) -> Path | None:
    """Bring the capacity state of vehicle configs already loaded up to date.

    ``vehicles`` maps each registration to its config dict — typically the
    package's shared in-memory ``VEHICLE_CONFIG``, loaded once at import and
    updated by every write-back since. When ``JOLT_CAPACITY_LEDGER`` names a
    file (looked up at call time, so a variable set after the import counts),
    the two ledger keys of each registration in ``vehicles`` that is also in
    ``vehicles.json`` become exactly what a fresh :func:`load_vehicle_configs`
    gives: the ledger entry's value where the entry carries the key, else the
    ``vehicles.json`` value, else no key at all. That holds however the ledger
    has changed in between — another file named, the file edited — so no value
    survives from a ledger the reports no longer read.

    The keys are set (as copies) or removed in place; the parameters are never
    touched. A registration not in ``vehicles.json`` is left alone — the ledger
    records state for configured vehicles only — and so is a config for which
    ``skip(cfg)`` is true.

    Without the variable this is a strict no-op: nothing is read, neither the
    ledger nor ``vehicles.json``, and ``vehicles`` is not touched. Returns the
    ledger path applied, or ``None``.
    """
    ledger_path = get_capacity_ledger_path()
    if ledger_path is None:
        return None
    loaded = _load_vehicle_configs(ledger_path)
    for reg, cfg in vehicles.items():
        current = loaded.get(reg)
        if current is None or (skip is not None and skip(cfg)):
            continue
        for key in LEDGER_KEYS:
            if key in current:
                cfg[key] = copy.deepcopy(current[key])
            else:
                cfg.pop(key, None)
    return ledger_path


def effective_vehicle_config(cfg: dict, when: dt.date | None) -> dict:
    """A vehicle's settings as they apply on ``when``.

    ``cfg`` is a vehicle's entry and ``when`` the date to resolve it for: a
    ``date``, or a ``datetime`` taken by its UTC date (a naive one as UTC), or
    ``None`` for no date, when no override applies. The result is a new dict —
    the entry updated with the ``set`` of the ``period_overrides`` window
    containing ``when`` (``from`` inclusive, ``to`` exclusive or open-ended), if
    any — and never carries ``period_overrides`` itself, so resolving it again,
    for any date, changes nothing. Pure: ``cfg`` is not modified, and the
    override's values are copies. The windows are expected to have passed the
    load-time validation (:func:`load_vehicle_configs`); should two contain the
    date, the first in the list applies.

    A leg is resolved for the UTC date of the first valid timestamp of its
    telematics frame; ``run_segment_detection`` and the generator do this
    themselves, so a caller handing them a frame needs nothing further.
    """
    day = _as_utc_date(when)
    result = {k: v for k, v in cfg.items() if k != PERIOD_OVERRIDES_KEY}
    if day is None:
        return result
    for override in cfg.get(PERIOD_OVERRIDES_KEY) or ():
        start, end = _override_window(override)
        if start <= day and (end is None or day < end):
            result.update(copy.deepcopy(override["set"]))
            break
    return result


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


def _load_vehicle_configs(ledger_path: Path | None) -> dict:
    """:func:`load_vehicle_configs` for a given ledger file (``None``: none)."""
    vehicles = _load_config_json("vehicles.json")
    _validate_period_overrides(vehicles)
    if ledger_path is not None:
        _overlay_capacity_ledger(vehicles, _read_capacity_ledger(ledger_path))
    return vehicles


# ── Pipeline keys with a checked value ─────────────────────────────────────


def _validate_pipeline_configs(pipelines: dict) -> None:
    """Raise ``ValueError`` for the first malformed checked key in ``pipelines``.

    Checks only the keys of :data:`_PIPELINE_TOP_LEVEL_VALUES` and
    :data:`_PIPELINE_SPEED_PARAMS_VALUES`: the value of each, and that each sits
    where it is read. A top-level key misplaced into a parameter group would
    reach a detector as an unexpected keyword argument; a ``speed_params`` key at
    the top level would be ignored, as it would in the charge or discharge
    parameters, which it would also break. Anything else in a pipeline is left
    to the code that reads it.
    """
    for name, pipeline in pipelines.items():
        if not isinstance(pipeline, dict):
            continue
        where = f"pipelines.json: {name}"
        for key, (check, kind) in _PIPELINE_TOP_LEVEL_VALUES.items():
            if key in pipeline and not check(pipeline[key]):
                raise ValueError(
                    f"{where}: {key} must be {kind}, not {pipeline[key]!r}"
                )
        for key in _PIPELINE_SPEED_PARAMS_VALUES:
            if key in pipeline:
                raise ValueError(
                    f"{where}: {key} belongs in speed_params, not at the top level "
                    "of the pipeline"
                )
        for group in _PIPELINE_PARAM_GROUPS:
            params = pipeline.get(group)
            if not isinstance(params, dict):
                continue
            for key in _PIPELINE_TOP_LEVEL_VALUES:
                if key in params:
                    raise ValueError(
                        f"{where}: {key} belongs at the top level of the "
                        f"pipeline, not in {group}"
                    )
            for key, (check, kind) in _PIPELINE_SPEED_PARAMS_VALUES.items():
                if key not in params:
                    continue
                if group != "speed_params":
                    raise ValueError(
                        f"{where}: {key} belongs in speed_params, not in {group}"
                    )
                if not check(params[key]):
                    raise ValueError(
                        f"{where}: speed_params.{key} must be {kind}, "
                        f"not {params[key]!r}"
                    )


# ── Date-effective settings: validation and window parsing ───────────────────


def _validate_period_overrides(vehicles: dict) -> None:
    """Raise ``ValueError`` for the first malformed ``period_overrides`` found.

    A vehicle without the field (or with it null) costs one key lookup, and
    ``pipelines.json`` is read only when an override sets a pipeline.
    """
    pipelines: dict | None = None
    for reg, cfg in vehicles.items():
        overrides = cfg.get(PERIOD_OVERRIDES_KEY) if isinstance(cfg, dict) else None
        if overrides is None:
            continue
        if not isinstance(overrides, list):
            raise ValueError(
                f"vehicles.json: {reg}: {PERIOD_OVERRIDES_KEY} must be a list of "
                f"overrides, not {type(overrides).__name__}"
            )
        if overrides and str(cfg.get("fuel_type", "")).upper() == "DIESEL":
            raise ValueError(
                f"vehicles.json: {reg}: {PERIOD_OVERRIDES_KEY} are not supported on "
                "a DIESEL vehicle — its legs are not segmented through the path "
                "they change; remove the field"
            )
        windows = []
        for index, override in enumerate(overrides):
            where = _override_label(reg, index, override)
            _check_override_fields(override, where)
            try:
                start, end = _override_window(override)
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from None
            if end is not None and end <= start:
                raise ValueError(
                    f"{where}: 'to' ({end.isoformat()}) must be after 'from' "
                    f"({start.isoformat()})"
                )
            settings = override["set"]
            if "pipeline" in settings and _is_name(settings["pipeline"]):
                if pipelines is None:
                    pipelines = _load_config_json("pipelines.json")
                if settings["pipeline"] not in pipelines:
                    raise ValueError(
                        f"{where}: pipeline {settings['pipeline']!r} is not in "
                        "pipelines.json"
                    )
            windows.append((start, end, where))
        windows.sort(key=lambda window: window[0])
        for (_, earlier_end, earlier), (later_start, _, later) in zip(
            windows, windows[1:]
        ):
            if earlier_end is None or earlier_end > later_start:
                raise ValueError(f"{later}: overlaps {earlier}")


def _override_label(reg: str, index: int, override: object) -> str:
    """How an error names an override: the vehicle, its position, its start."""
    label = f"vehicles.json: {reg}: {PERIOD_OVERRIDES_KEY}[{index}]"
    if isinstance(override, dict) and isinstance(override.get("from"), str):
        label += f" (from {override['from']})"
    return label


def _check_override_fields(override: object, where: str) -> None:
    """Everything about one override except its dates and its pipeline name."""
    if not isinstance(override, dict):
        raise ValueError(f"{where}: an override must be an object")
    unknown = sorted(set(override) - set(_PERIOD_OVERRIDE_FIELDS))
    if unknown:
        raise ValueError(
            f"{where}: unknown field(s) {', '.join(unknown)}; an override has "
            f"{', '.join(_PERIOD_OVERRIDE_FIELDS)}"
        )
    if "from" not in override:
        raise ValueError(f"{where}: 'from' is required")
    reason = override.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"{where}: 'reason' must be text")
    if "set" not in override:
        raise ValueError(f"{where}: 'set' is required")
    settings = override["set"]
    if not isinstance(settings, dict):
        raise ValueError(f"{where}: 'set' must be an object")
    if not settings:
        raise ValueError(f"{where}: 'set' is empty")
    outside = sorted(set(settings) - set(PERIOD_OVERRIDE_KEYS))
    if outside:
        raise ValueError(
            f"{where}: 'set' may only change {', '.join(PERIOD_OVERRIDE_KEYS)}; "
            f"not {', '.join(outside)}"
        )
    for key, value in settings.items():
        check, kind = _PERIOD_OVERRIDE_VALUES[key]
        if not check(value):
            raise ValueError(f"{where}: 'set'.{key} must be {kind}, not {value!r}")


def _override_window(override: dict) -> tuple[dt.date, dt.date | None]:
    """An override's ``[from, to)`` window; ``to`` is ``None`` when open-ended."""
    start = _parse_override_date(override.get("from"), "from")
    end = override.get("to")
    return start, (None if end is None else _parse_override_date(end, "to"))


def _parse_override_date(value: object, field: str) -> dt.date:
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"'{field}' must be a date written YYYY-MM-DD, not {value!r}")


def _as_utc_date(when: object) -> dt.date | None:
    """The date to resolve settings for: a ``datetime`` counts by its UTC date."""
    if when is None:
        return None
    if isinstance(when, dt.datetime):
        if when != when:  # NaT
            return None
        if when.tzinfo is not None:
            when = when.astimezone(dt.timezone.utc)
        return when.date()
    if isinstance(when, dt.date):
        return when
    raise TypeError(f"'when' must be a date or a datetime, not {type(when).__name__}")


def _overlay_capacity_ledger(vehicles: dict, ledger: dict) -> dict:
    """Overlay the ledger keys onto ``vehicles`` in place; return ``vehicles``."""
    for reg, entry in ledger.items():
        cfg = vehicles.get(reg)
        if cfg is None:
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
    with it. On POSIX the directory is then fsynced too, so a crash or power
    loss after this returns cannot undo the replace. The bytes are those of a
    direct write, and the ledger keeps its permission bits (a new one gets those
    a direct write would have given it). A replace refused with
    ``PermissionError`` is retried a few times; when it still fails, or anything
    else goes wrong before it, the temporary file is removed and the error
    raised.
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
    _fsync_directory(path.parent)


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


def _fsync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable: fsync the directory (POSIX only).

    On POSIX a rename is recorded in the directory, which a crash can lose
    unless the directory itself is flushed. Best effort: a file system that
    cannot fsync a directory refuses with an ``OSError``, which is logged at
    debug level and does not fail the write, since the replace itself has
    already happened. Windows has no equivalent to call, so nothing happens there.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        logger.debug("capacity ledger directory %s not fsynced: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("capacity ledger directory %s not fsynced: %s", directory, exc)
    finally:
        os.close(fd)


def _discard_temporary_file(name: str) -> None:
    """Remove a temporary ledger file, as far as possible."""
    with contextlib.suppress(OSError):
        # Windows refuses to delete a read-only file (a copied read-only mode).
        os.chmod(name, stat.S_IREAD | stat.S_IWRITE)
    with contextlib.suppress(OSError):
        os.unlink(name)
