"""Turn a persisted raw artefact into an anonymised test fixture.

Run it from the repository root, offline:

    python tests/fixtures/make_fixture.py <raw CSV> --alias <ALIAS> \\
        [--rows START:END] [--registration <REG>] [--config-from <REG>] [--force]

``<raw CSV>`` is one of the raw artefacts the generator persists with ``--debug``:

    <out>/<REG>/raw_telematics/raw_<date>_<idx>.csv       EV telematics  -> kind "ev"
    <out>/<REG>/raw_logger_v<N>/logger_<date>_<idx>.csv    SRF logger     -> kind "diesel"

The tool

1. reads the CSV cell by cell as text, so every value it does not deliberately
   change is written back byte for byte;
2. keeps only the ``--rows START:END`` window of data rows, if given;
3. de-identifies it exactly as ``tests/fixtures/README.md`` documents: a rigid
   transform of every GPS position onto the synthetic origin (0.5, 0.5) with a
   random rotation that is never printed, logged or stored; heading / bearing
   columns turned with it; every driver column dropped; the vehicle identity
   (``vehicleId``, a VIN column) replaced by the alias;
4. refuses to write if the registration still appears anywhere in the output,
   in any case and however it is split (:func:`registration_pattern`);
5. writes ``tests/fixtures/raw/<ALIAS>/<file name>`` and registers it in
   ``tests/fixtures/raw_fixtures.json``;
6. with ``--config-from <REG>``, adds the frozen config entry for the alias —
   a copy of the live entry (and, for an EV, its pipeline under an alias name)
   with the identity and state fields removed; an entry with date-effective
   settings (``period_overrides``) is copied as it applies on the fixture's
   date.

Then generate the fixture's first golden and run the suite:

    python tests/fixtures/regenerate_goldens.py --alias <ALIAS>
    pytest
"""

from __future__ import annotations

import argparse
import atexit
import copy
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

FIXTURES_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURES_DIR.parents[1]
REGISTRY_PATH = FIXTURES_DIR / "raw_fixtures.json"
FROZEN_VEHICLES = FIXTURES_DIR / "configs" / "vehicles.json"
FROZEN_PIPELINES = FIXTURES_DIR / "configs" / "pipelines.json"

#: The synthetic origin every fixture's track is moved onto (degrees).
TARGET_LAT_DEG = 0.5
TARGET_LON_DEG = 0.5

#: Latitude / longitude column pairs, in the order they are looked for. Every
#: pair present in a file is moved by the SAME rotation.
GPS_PAIRS = (
    ("latitude", "longitude"),
    ("gnss_latitude", "gnss_longitude"),
    ("2 latitude", "2 longitude"),
)

#: Heading columns (degrees clockwise from north) and the position pair each is
#: measured at, in order of preference. A heading left untouched would give the
#: rotation away: heading minus the rotated track's own bearing IS the angle.
HEADING_COLUMNS = {
    "gnss_heading": (("gnss_latitude", "gnss_longitude"), ("latitude", "longitude")),
    "2 bearing": (("2 latitude", "2 longitude"),),
}

#: Driver columns — identity and tachograph state: the EV feed's ``driver1_*``
#: family, the logger's ``DI driver <n> identification`` and ``TCO1 driver <n>
#: working state``. Deliberately not "driver" anywhere: the J1939 signal
#: "driver's demand engine percent torque" is not personal data.
_DRIVER_COLUMN = re.compile(r"^driver\d*_|\bdriver \d+\b", re.IGNORECASE)

#: Vehicle-identity columns whose values become the alias.
IDENTITY_COLUMNS = ("vehicleId", "VIN vehicle identification number")

#: Frozen-config keys that are identity, provenance or machine-written state,
#: never part of a fixture's frozen parameters.
NON_FROZEN_KEYS = frozenset(
    {"vin", "description", "operator", "operators", "effective_capacity_quarterly"}
)

_ALIAS = re.compile(r"^[A-Z][A-Z0-9]{2,15}$")
_REG = re.compile(r"^[A-Z0-9]{2,10}$")
_EARTH_RADIUS_KM = 6371.0088


# =============================================================================
# Reading and writing — every cell as text
# =============================================================================


def detect_kind(path: Path) -> str:
    """``"ev"`` for a raw telematics CSV, ``"diesel"`` for an SRF logger CSV."""
    with open(path, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\r\n").split(",")
    if "eventDatetime" in header:
        return "ev"
    if header and header[0] == "":
        return "diesel"  # the logger CSV's first column is its unnamed time index
    raise ValueError(
        f"{path.name}: neither a raw telematics CSV (no 'eventDatetime' column) "
        "nor an SRF logger CSV (no unnamed timestamp index column)"
    )


def read_raw(path: Path, kind: str) -> pd.DataFrame:
    """Every cell as a string, empty cells as ``""`` — nothing parsed or coerced."""
    kwargs = dict(dtype=str, keep_default_na=False)
    if kind == "diesel":
        return pd.read_csv(path, index_col=0, **kwargs)
    return pd.read_csv(path, **kwargs)


def to_csv_text(df: pd.DataFrame, kind: str) -> str:
    """The CSV text exactly as the generator's own writer lays it out."""
    buf = io.StringIO()
    df.to_csv(buf, index=(kind == "diesel"), lineterminator="\n")
    return buf.getvalue()


# =============================================================================
# The rigid transform — a rotation of the sphere
# =============================================================================


def _unit_vectors(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    phi, lam = np.radians(lat_deg), np.radians(lon_deg)
    return np.stack(
        [np.cos(phi) * np.cos(lam), np.cos(phi) * np.sin(lam), np.sin(phi)], axis=-1
    )


def _lat_lon(xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lat = np.degrees(np.arcsin(np.clip(xyz[..., 2], -1.0, 1.0)))
    lon = np.degrees(np.arctan2(xyz[..., 1], xyz[..., 0]))
    return lat, lon


def _axis_rotation(axis: np.ndarray, angle_rad: float) -> np.ndarray:
    """Rodrigues: rotation by ``angle_rad`` about the unit vector ``axis``."""
    x, y, z = axis / np.linalg.norm(axis)
    k = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + np.sin(angle_rad) * k + (1.0 - np.cos(angle_rad)) * (k @ k)


def _rotation_onto_target(ref: np.ndarray, spin_rad: float) -> np.ndarray:
    """Move ``ref`` onto the synthetic origin, then spin about it by ``spin_rad``.

    A rotation of the sphere is a rigid motion of the surface: every great-circle
    distance and every angle between two directions at a point survives it
    exactly, wherever the track started.
    """
    target = _unit_vectors(np.array(TARGET_LAT_DEG), np.array(TARGET_LON_DEG))
    axis = np.cross(ref, target)
    s, c = np.linalg.norm(axis), float(np.dot(ref, target))
    if s < 1e-15:
        # Already there (or exactly opposite): any axis orthogonal to ref works.
        align = np.eye(3) if c > 0 else _axis_rotation(np.cross(ref, [0, 0, 1]), np.pi)
    else:
        align = _axis_rotation(axis, float(np.arctan2(s, c)))
    return _axis_rotation(target, spin_rad) @ align


def _north_east(xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Local north and east unit vectors at each unit position vector."""
    lat, lon = _lat_lon(xyz)
    phi, lam = np.radians(lat), np.radians(lon)
    north = np.stack(
        [-np.sin(phi) * np.cos(lam), -np.sin(phi) * np.sin(lam), np.cos(phi)], axis=-1
    )
    east = np.stack([-np.sin(lam), np.cos(lam), np.zeros_like(lam)], axis=-1)
    return north, east


def _rotate_headings(
    heading_deg: np.ndarray, xyz: np.ndarray, rot: np.ndarray
) -> np.ndarray:
    """Turn headings measured at ``xyz`` by the rotation ``rot`` (degrees)."""
    north, east = _north_east(xyz)
    h = np.radians(heading_deg)
    direction = np.cos(h)[:, None] * north + np.sin(h)[:, None] * east
    turned = direction @ rot.T
    north2, east2 = _north_east(xyz @ rot.T)
    out = np.degrees(
        np.arctan2(np.sum(turned * east2, axis=1), np.sum(turned * north2, axis=1))
    )
    return np.mod(out, 360.0)


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.where(series != "", np.nan), errors="coerce")


def _format(values: np.ndarray) -> list[str]:
    return [repr(float(v)) for v in values]


def transform_positions(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Rigidly move every GPS position, and every heading, of ``df`` (a copy).

    The reference point is the spherical centroid of all valid positions; it
    lands exactly on (0.5, 0.5). The spin about that point is drawn from ``rng``
    and exists only inside this call — it is neither returned nor recorded.
    A GPS cell that is present but not a number cannot be moved, so it is
    blanked rather than left behind with its real coordinate.
    """
    out = df.copy()
    pairs = [p for p in GPS_PAIRS if p[0] in out.columns and p[1] in out.columns]
    if not pairs:
        return out

    parsed = {}
    for lat_col, lon_col in pairs:
        lat, lon = _numeric(out[lat_col]), _numeric(out[lon_col])
        ok = lat.notna() & lon.notna()
        for col, values in ((lat_col, lat), (lon_col, lon)):
            unparseable = (out[col] != "") & values.isna()
            out.loc[unparseable, col] = ""
        # A half-present pair is as identifying as a whole one.
        out.loc[~ok, [lat_col, lon_col]] = ""
        parsed[(lat_col, lon_col)] = (lat[ok].to_numpy(), lon[ok].to_numpy(), ok)

    all_xyz = np.concatenate(
        [_unit_vectors(la, lo) for la, lo, _ in parsed.values()], axis=0
    )
    if len(all_xyz) == 0:
        return out
    centroid = all_xyz.sum(axis=0)
    ref = centroid / np.linalg.norm(centroid)
    rot = _rotation_onto_target(ref, float(rng.uniform(0.0, 2.0 * np.pi)))

    for (lat_col, lon_col), (la, lo, ok) in parsed.items():
        lat2, lon2 = _lat_lon(_unit_vectors(la, lo) @ rot.T)
        out.loc[ok, lat_col] = _format(lat2)
        out.loc[ok, lon_col] = _format(lon2)

    for heading_col, candidates in HEADING_COLUMNS.items():
        if heading_col not in out.columns:
            continue
        pair = next((p for p in candidates if p in parsed), None)
        heading = _numeric(df[heading_col])
        unparseable = (df[heading_col] != "") & heading.isna()
        out.loc[unparseable, heading_col] = ""
        has_heading = heading.notna()
        if pair is not None:
            _, _, ok = parsed[pair]
            src = df.loc[has_heading & ok, [pair[0], pair[1]]]
            at = has_heading & ok
            xyz = _unit_vectors(
                pd.to_numeric(src[pair[0]]).to_numpy(),
                pd.to_numeric(src[pair[1]]).to_numpy(),
            )
            out.loc[at, heading_col] = _format(
                _rotate_headings(heading[at].to_numpy(), xyz, rot)
            )
            rest = has_heading & ~ok
        else:
            rest = has_heading
        if rest.any():
            # No position on this row: turn it as it would be at the centroid.
            at_ref = np.repeat(ref[None, :], int(rest.sum()), axis=0)
            out.loc[rest, heading_col] = _format(
                _rotate_headings(heading[rest].to_numpy(), at_ref, rot)
            )
    return out


# =============================================================================
# Identity
# =============================================================================


def driver_columns(columns) -> list[str]:
    """The driver identity / working-state columns of a header."""
    return [c for c in columns if _DRIVER_COLUMN.search(str(c))]


#: What may stand between two characters of a registration where it is written
#: out: white space other than a line break (space, tab, the no-break and the
#: other Unicode spaces), an invisible zero-width character, a hyphen or dash, an
#: underscore. Never a comma, a quote or a line break, so a match can never run
#: across two CSV cells or two rows.
_REGISTRATION_SEPARATOR = (
    "["
    "\t    -   　"  # spaces
    "­​-‍⁠﻿"  # soft hyphen, zero-width characters
    "_\\-‐-―−"  # underscore, hyphens and dashes
    "]"
)


def registration_pattern(registration: str) -> re.Pattern[str]:
    """A case-insensitive regex matching every written spelling of ``registration``.

    Built from the compact registration's characters with an optional run of
    separators between every two of them, so it finds the plate however it is
    split — ``AB12 CDE`` (4+3), ``ABC 1234`` (3+4), ``A12 BCD`` (3+3) — or
    hyphenated, underscored, spaced out or in any case.
    """
    compact = [ch for ch in registration if ch.isalnum()]
    if not compact:
        raise ValueError(f"registration {registration!r} has no letters or digits")
    joiner = _REGISTRATION_SEPARATOR + "*"
    return re.compile(joiner.join(re.escape(ch) for ch in compact), re.IGNORECASE)


def anonymise(
    df: pd.DataFrame,
    *,
    alias: str,
    registration: str | None = None,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Return the de-identified copy of a raw frame read by :func:`read_raw`.

    Everything not named here is kept exactly: the other columns, their order,
    their names and every value, character for character.
    """
    rng = rng if rng is not None else np.random.default_rng()
    out = df.drop(columns=driver_columns(df.columns))
    for col in IDENTITY_COLUMNS:
        if col in out.columns:
            out.loc[out[col] != "", col] = alias
    out = transform_positions(out, rng)
    if registration:
        # Header, index and every cell: nothing may still carry the
        # registration, in any spelling.
        text = out.to_csv(index=True)
        if registration_pattern(registration).search(text):
            raise ValueError(
                "the registration still appears in the anonymised data; refusing "
                "to write the fixture (find the column carrying it and add it to "
                "IDENTITY_COLUMNS)"
            )
    return out


def fixture_file_name(source: Path, alias: str, registration: str | None) -> str:
    """The source file name, with every spelling of the registration replaced."""
    if not registration:
        return source.name
    return registration_pattern(registration).sub(lambda _match: alias, source.name)


def infer_registration(source: Path) -> str | None:
    """``<REG>`` from ``…/<REG>/raw_telematics/…`` or ``…/<REG>/raw_logger_v<N>/…``."""
    parent = source.parent.name
    if parent == "raw_telematics" or parent.startswith("raw_logger"):
        candidate = source.parent.parent.name.upper()
        if _REG.match(candidate):
            return candidate
    return None


# =============================================================================
# Registry and frozen configs
# =============================================================================


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")


def frozen_entry(
    live_vehicle: dict,
    live_pipelines: dict,
    alias: str,
    kind: str,
    *,
    when: datetime.date | None = None,
) -> tuple[dict, tuple[str, dict] | None]:
    """The frozen alias config (and, for an EV, its pipeline) from a live entry.

    Identity, provenance and machine-written state (:data:`NON_FROZEN_KEYS`) are
    left out; ``srf_reg`` becomes the alias. An EV's pipeline is copied under an
    alias name, ``<alias>_<branch>``, so the fixture can never resolve a live
    pipeline; a diesel vehicle's ``pipeline`` is a dispatch marker only
    (``<alias>_diesel_logger``), exactly as for the live diesel vehicles.

    A live entry with date-effective settings (``period_overrides``) is frozen as
    it applies on ``when``, the fixture frame's date: the frozen entry carries no
    overrides, and its pipeline is the one that frame is segmented with.
    """
    if live_vehicle.get("period_overrides"):
        from report_generator.configs import effective_vehicle_config

        live_vehicle = effective_vehicle_config(live_vehicle, when)
    is_diesel = str(live_vehicle.get("fuel_type", "")).upper() == "DIESEL"
    if is_diesel != (kind == "diesel"):
        raise ValueError(
            f"the source vehicle is {'diesel' if is_diesel else 'EV'} but the file "
            f"is a {'logger' if kind == 'diesel' else 'telematics'} CSV — an EV "
            "fixture is its raw telematics, a diesel fixture its logger CSV"
        )
    entry = {
        k: copy.deepcopy(v) for k, v in live_vehicle.items() if k not in NON_FROZEN_KEYS
    }
    entry["srf_reg"] = alias
    if is_diesel:
        entry["pipeline"] = f"{alias.lower()}_diesel_logger"
        return entry, None
    try:
        live_pipeline = live_pipelines[live_vehicle["pipeline"]]
    except KeyError:
        raise ValueError(
            f"the source vehicle's pipeline {live_vehicle.get('pipeline')!r} is not "
            "in the live pipelines.json"
        ) from None
    name = f"{alias.lower()}_{live_pipeline.get('branch', 'speed')}"
    entry["pipeline"] = name
    return entry, (name, copy.deepcopy(live_pipeline))


def _live_configs() -> tuple[dict, dict]:
    """The reviewed live ``vehicles.json`` / ``pipelines.json`` (no ledger overlay)."""
    if not os.environ.get("JOLT_CACHE_DIR"):
        # The package import touches the cache root; keep it off the real one.
        tmp_cache = tempfile.mkdtemp(prefix="jolt_make_fixture_")
        atexit.register(shutil.rmtree, tmp_cache, ignore_errors=True)
        os.environ["JOLT_CACHE_DIR"] = tmp_cache
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from report_generator import configs

    return (
        configs._load_config_json("vehicles.json"),
        configs._load_config_json("pipelines.json"),
    )


def plan_frozen_config(
    alias: str,
    source_reg: str,
    kind: str,
    *,
    force: bool,
    frame: pd.DataFrame | None = None,
) -> tuple[dict, tuple[str, dict] | None, str]:
    """Check and build the alias's frozen config, writing nothing yet.

    ``frame`` is the fixture's data: an EV entry with date-effective settings is
    frozen as it applies on the frame's date — the date the generator would
    resolve the leg for. Returns ``(entry, pipeline or None, the live pipeline
    name)``.
    """
    live_vehicles, live_pipelines = _live_configs()
    if source_reg not in live_vehicles:
        raise ValueError(f"{source_reg} is not in the live vehicles.json")
    if alias in live_vehicles:
        raise ValueError(f"{alias} is a live registration; an alias must not be one")
    if alias in _load_json(FROZEN_VEHICLES) and not force:
        raise ValueError(f"{alias} already has a frozen config (use --force)")
    when = None
    if kind == "ev" and frame is not None:
        from report_generator.segmentation.timeutil import frame_utc_date

        when = frame_utc_date(frame)
    entry, pipeline = frozen_entry(
        live_vehicles[source_reg], live_pipelines, alias, kind, when=when
    )
    if (
        pipeline is not None
        and pipeline[0] in _load_json(FROZEN_PIPELINES)
        and not force
    ):
        raise ValueError(f"frozen pipeline {pipeline[0]} already exists (use --force)")
    return entry, pipeline, live_vehicles[source_reg].get("pipeline", "")


def write_frozen_config(alias: str, entry: dict, pipeline) -> None:
    vehicles = _load_json(FROZEN_VEHICLES)
    vehicles[alias] = entry
    _dump_json(FROZEN_VEHICLES, vehicles)
    if pipeline is not None:
        pipelines = _load_json(FROZEN_PIPELINES)
        pipelines[pipeline[0]] = pipeline[1]
        _dump_json(FROZEN_PIPELINES, pipelines)


def register_fixture(alias: str, rel_path: str, kind: str) -> None:
    registry = _load_json(REGISTRY_PATH)
    registry[alias] = {"path": rel_path, "kind": kind}
    _dump_json(REGISTRY_PATH, registry)


# =============================================================================
# CLI
# =============================================================================


def _row_window(text: str | None) -> slice:
    if not text:
        return slice(None)
    match = re.fullmatch(r"(\d*):(\d*)", text)
    if not match:
        raise SystemExit("--rows takes START:END (data rows, 0-based, END exclusive)")
    start, end = (int(g) if g else None for g in match.groups())
    return slice(start, end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn a persisted raw artefact into an anonymised test fixture."
    )
    parser.add_argument("source", type=Path, help="raw_*.csv or logger_*.csv")
    parser.add_argument("--alias", required=True, help="e.g. EVSPD02 / DSL02")
    parser.add_argument("--rows", help="keep data rows START:END (0-based)")
    parser.add_argument(
        "--registration",
        help="the vehicle's registration (default: taken from the artefact path)",
    )
    parser.add_argument(
        "--config-from",
        metavar="REG",
        help="add the alias's frozen config from this live vehicle's entry",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing alias's fixture"
    )
    args = parser.parse_args(argv)

    alias = args.alias.upper()
    if not _ALIAS.match(alias):
        parser.error("--alias must be 3-16 upper-case letters / digits, e.g. EVSPD02")
    registration = args.registration or infer_registration(args.source)
    if registration is None:
        print(
            "warning: registration unknown (not a standard artefact path and no "
            "--registration); the file name and data are NOT checked for it",
            file=sys.stderr,
        )

    try:
        # Everything is checked and computed before anything is written.
        if alias in _load_json(REGISTRY_PATH) and not args.force:
            raise ValueError(f"{alias} is already registered (use --force)")
        kind = detect_kind(args.source)
        frame = read_raw(args.source, kind).iloc[_row_window(args.rows)]
        if frame.empty:
            raise ValueError("the row window selects no rows")
        dropped = driver_columns(frame.columns)
        clean = anonymise(frame, alias=alias, registration=registration)
        planned = None
        if args.config_from:
            planned = plan_frozen_config(
                alias,
                args.config_from.upper(),
                kind,
                force=args.force,
                frame=clean,
            )
    except ValueError as exc:
        raise SystemExit(f"make_fixture: {exc}") from None

    name = fixture_file_name(args.source, alias, registration)
    out_dir = FIXTURES_DIR / "raw" / alias
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(to_csv_text(clean, kind), encoding="utf-8", newline="")
    rel_path = f"raw/{alias}/{name}"
    previous = _load_json(REGISTRY_PATH).get(alias, {}).get("path")
    if previous and previous != rel_path:
        # --force onto a different source file: the old fixture is superseded
        # (it stays in the git history).
        (FIXTURES_DIR / previous).unlink(missing_ok=True)
    register_fixture(alias, rel_path, kind)
    print(
        f"wrote tests/fixtures/{rel_path}  ({len(clean)} rows x {clean.shape[1]} "
        f"columns; {len(dropped)} driver column(s) dropped; kind={kind})"
    )

    if planned is not None:
        entry, pipeline, live_pipeline = planned
        write_frozen_config(alias, entry, pipeline)
        print(f"frozen config for {alias} added to tests/fixtures/configs/")
        vehicle = " ".join(str(v) for v in (entry.get("make"), entry.get("model")) if v)
        print(
            "README fixture-table row: "
            f"| `{alias}` | {len(clean)} x {clean.shape[1]} | a {vehicle or 'vehicle'} "
            f"on `{live_pipeline}` | <what it exercises> |"
        )
    else:
        print(
            f"no frozen config added: add tests/fixtures/configs entries for {alias} "
            "(or re-run with --config-from REG) before generating its golden"
        )
    print(f"next: python tests/fixtures/regenerate_goldens.py --alias {alias}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
