"""
report_generator.depots
=======================
Where a vehicle is based, and the ``Leg Type`` of every trip and charge row.

The ``Leg Type`` of a row says where the row starts and ends relative to the
vehicle's bases (depots): a trip is ``"Outbound"`` (leaves a base),
``"Return"`` (arrives at one), ``"In House"`` / ``"Round Trip"`` (both ends at
the same base, up to / over :data:`ROUND_TRIP_MIN_KM`) or ``"In Transit"``
(neither end at a base); a charge is ``"<kind> Home"`` at a base and
``"<kind> Away"`` elsewhere, where ``<kind>`` is ``AC`` / ``DC`` / ``AC/DC`` /
``Charge`` as the row builder set it. ``"Stop"`` and anything else is left as
it is.

The bases are found from the report's own rows, once the whole run has been
segmented (:func:`find_bases`), separately for each operator the vehicle worked
for in the run (the per-row ``Operator``; a row without one belongs to the
operator of the nearest row before it, else after it). Every row of an operator
is pooled, so a vehicle that returns to an operator later in the run keeps one
set of bases for it. The evidence, strongest first:

1. **Overnight stays.** A stay is the time between two consecutive trips of the
   operator lasting at least :data:`OVERNIGHT_STOP_MIN_H`, and the open stays
   before the operator's first trip and after its last one. Its places are the
   earlier trip's destination, the later trip's origin and the position of every
   charge of the operator inside it. The places are clustered (densest first,
   within :data:`HOME_DETECTION_KM`) and a cluster touched by at least
   :data:`MIN_BASE_NIGHTS` stays — at least :data:`MIN_BASE_NIGHT_SHARE` of
   the operator's stays, and at least :data:`MIN_BASE_NIGHTS_PER_DRIVING_DAY`
   per day on which its vehicle drove — is a base. Several clusters can
   qualify: a vehicle based at two depots has two bases. A site where the
   vehicle only charges during the day, or where it only waits a few hours
   around midnight, is no base: nothing in this tier counts it.
2. **Charge sites** — only when no site qualifies on overnight stays: the site
   with the most charge sessions (ties: the most energy), if it has at least
   :data:`MIN_FALLBACK_SUPPORT` of them.
3. **Trip endpoints** — only when neither of the above gives a base: the site
   most trips start or end at, if at least :data:`MIN_FALLBACK_SUPPORT` do.

A shorter rest across midnight is deliberately not evidence: on a double-shifted
vehicle it falls wherever the vehicle is working at midnight — typically a
customer site it shuttles to — and would make that site a base.

Labelling (:func:`assign_leg_types`) is a second pass over the rows once the
bases are known: a position is at a base when it lies within
:data:`HOME_DETECTION_KM` of the base centre (the nearest base when several
are). Bases closer than :data:`BASE_GROUP_KM` to each other count as one place
— sparse feeds often place a trip's first or last sample away from where the
vehicle stood, which can leave two clusters of the same depot a kilometre
apart — so a trip between them is ``"In House"`` / ``"Round Trip"``. A trip
from one base to a different one is labelled ``"Return"`` (it ends at a base)
and counted in :attr:`LegTypeAssignment.base_to_base`.

The labels depend only on each row's kind (trip / charge / other), times,
positions, distance and operator — never on the row's current label — so
relabelling a set of rows twice changes nothing the second time, and a report
the generator wrote relabels to itself.

Public entry points: :func:`find_bases`, :func:`assign_leg_types` (no mutation)
and :func:`relabel_rows` (writes the labels into the rows). Rows are in the
row-tuple layout of ``headers`` minus the leading ``Leg Number``, as everywhere
in the package; both ``HEADERS`` (EV) and ``DIESEL_HEADERS`` work, and so does
any header tuple starting with ``Leg Number`` that holds the columns read.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Sequence

import numpy as np
import pandas as pd

from report_generator.columns import (
    HEADERS,
    _leg_is_charge,
    _leg_is_stop,
    _row_col_index,
    is_trip_leg,
)

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

#: A position within this great-circle distance of a base centre is at the base;
#: also the radius of the clusters the bases are found from.
HOME_DETECTION_KM = 0.5
#: A trip starting and ending at the same base and longer than this is a
#: ``"Round Trip"`` (a delivery round from the depot); otherwise ``"In House"``.
ROUND_TRIP_MIN_KM = 5.0
#: A stationary period between two trips at least this long is an overnight stay.
OVERNIGHT_STOP_MIN_H = 6.0
#: A site is a base when at least this many overnight stays touch it ...
MIN_BASE_NIGHTS = 2
#: ... they are at least this share of the operator's overnight stays ...
MIN_BASE_NIGHT_SHARE = 0.2
#: ... and there is at least this many of them per day the operator's vehicle
#: drove (one in ten days). The share alone over-rates a site on a vehicle that
#: rarely rests six hours — a double-shifted one, whose few long rests are
#: mostly weekends — where a customer site it is sometimes left at overnight
#: can reach the share while the vehicle visits it for a short stop every day.
MIN_BASE_NIGHTS_PER_DRIVING_DAY = 0.1
#: The support the fallback tiers need: charge sessions at the site, or trip
#: origins / destinations there.
MIN_FALLBACK_SUPPORT = 2
#: Bases closer than this to each other are one place when a trip's two ends
#: are compared (``"In House"`` / ``"Round Trip"`` rather than base to base).
BASE_GROUP_KM = 3.0

#: The five trip labels, and the two places a charge can be at.
TRIP_LEG_TYPES = ("In House", "Round Trip", "Outbound", "Return", "In Transit")
HOME = "Home"
AWAY = "Away"

#: The evidence a base was found from.
EVIDENCE_OVERNIGHT = "overnight"
EVIDENCE_CHARGES = "charges"
EVIDENCE_TRIP_ENDS = "trip_ends"

#: Columns the labelling reads; the first five are required.
_REQUIRED_COLUMNS = (
    "Leg Type",
    "Start Time (UTC)",
    "End Time (UTC)",
    "Origin (Lat, Lon)",
    "Destination (Lat, Lon)",
)
_OPTIONAL_COLUMNS = ("Distance (km)", "Operator", "Energy Change (kWh)")

_EARTH_RADIUS_KM = 6371.0088
_POINT_RE = re.compile(
    r"^\s*Point\(\s*([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s*\)"
)
_CHARGE_PLACE_RE = re.compile(r"\s+(?:Home|Away)\s*$", re.IGNORECASE)
_OVERNIGHT = pd.Timedelta(hours=OVERNIGHT_STOP_MIN_H)


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Base:
    """One base (depot) of an operator in a run.

    ``nights`` counts the operator's overnight stays with a place within
    :data:`HOME_DETECTION_KM` of the centre, out of ``operator_nights``, over
    ``driving_days`` days on which the operator's vehicle drove; ``charges`` /
    ``charge_kwh`` are the operator's charge sessions within that radius and the
    energy they put in. ``evidence`` is the tier the base comes from
    (``"overnight"`` / ``"charges"`` / ``"trip_ends"``). ``group`` is shared by
    bases closer than :data:`BASE_GROUP_KM` to each other.
    """

    operator: str | None
    lat: float
    lon: float
    evidence: str
    nights: int
    operator_nights: int
    driving_days: int
    charges: int
    charge_kwh: float
    group: int

    def as_dict(self) -> dict:
        """JSON-ready form (coordinates to 6 decimals, energy to 0.1 kWh)."""
        return {
            "operator": self.operator,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "evidence": self.evidence,
            "nights": self.nights,
            "operator_nights": self.operator_nights,
            "driving_days": self.driving_days,
            "charges": self.charges,
            "charge_kwh": round(self.charge_kwh, 1),
            "group": self.group,
        }


@dataclass
class LegTypeAssignment:
    """The outcome of labelling a run's rows.

    ``labels[i]`` is the Leg Type row ``i`` should carry — its current value for
    a row that is neither a trip nor a charge. ``changes`` counts
    ``(old, new)`` pairs over the rows whose label changes.
    """

    labels: list
    bases: dict
    base_to_base: int = 0
    changes: Counter = field(default_factory=Counter)
    trips: int = 0
    charges: int = 0
    rejected: dict = field(default_factory=dict)

    @property
    def n_changed(self) -> int:
        """Number of rows whose Leg Type changes."""
        return sum(self.changes.values())

    def summary(self) -> dict:
        """JSON-ready summary: the bases per operator, the label changes and the
        trips from one base to another."""
        return {
            "trips": self.trips,
            "charges": self.charges,
            "changed": self.n_changed,
            "changes": {
                f"{old} -> {new}": n
                for (old, new), n in sorted(
                    self.changes.items(), key=lambda kv: (-kv[1], str(kv[0]))
                )
            },
            "base_to_base": self.base_to_base,
            "bases": {
                _operator_key(op): [b.as_dict() for b in bases]
                for op, bases in self.bases.items()
            },
            "rejected_sites": {
                _operator_key(op): list(sites)
                for op, sites in self.rejected.items()
                if sites
            },
        }


# ── Value parsing (rows built in memory, or read back from a workbook) ──────


def _parse_point(value) -> tuple[float, float] | None:
    """``"Point(lat lon)"`` → ``(lat, lon)``; anything else, (0, 0) and
    out-of-range values → ``None``."""
    if not isinstance(value, str):
        return None
    m = _POINT_RE.match(value)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def _parse_time(value) -> pd.Timestamp | None:
    """A timestamp as naive UTC rounded to the second, or ``None``.

    Rounding makes a row built in memory and the same row read back from its
    workbook (an Excel serial date) give the same instant.
    """
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.round("s")


def _parse_float(value) -> float:
    """A number, or NaN (``None``, ``=NA()`` and other text included)."""
    if value is None or isinstance(value, bool):
        return math.nan
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def _parse_operator(value) -> str | None:
    """The operator code, or ``None`` for a blank / missing cell."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.upper() == "=NA()" or text.lower() == "nan":
        return None
    return text


def _positive(value: float) -> float:
    """``value`` when it is a positive number, else 0."""
    return value if value == value and value > 0 else 0.0


def _operator_key(operator: str | None) -> str:
    """Dictionary key of an operator in JSON output."""
    return operator if operator is not None else "<none>"


@dataclass
class _Row:
    index: int
    kind: str  # "trip" | "charge"
    label: str
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    origin: tuple[float, float] | None
    dest: tuple[float, float] | None
    distance_km: float
    operator: str | None
    energy_kwh: float

    @property
    def position(self) -> tuple[float, float] | None:
        """A charge's place (its origin; the destination when that is blank)."""
        return self.origin if self.origin is not None else self.dest

    @property
    def midpoint(self) -> pd.Timestamp | None:
        if self.start is None or self.end is None:
            return None
        return self.start + (self.end - self.start) / 2


def _column_indices(headers: Sequence[str]) -> dict[str, int | None]:
    headers = tuple(headers)
    if not headers or headers[0] != "Leg Number":
        raise ValueError("headers must start with 'Leg Number'")
    missing = [h for h in _REQUIRED_COLUMNS if h not in headers]
    if missing:
        raise ValueError(f"headers lack the column(s) {missing}")
    idx: dict[str, int | None] = {
        h: _row_col_index(h, headers) for h in _REQUIRED_COLUMNS
    }
    for h in _OPTIONAL_COLUMNS:
        idx[h] = _row_col_index(h, headers) if h in headers else None
    return idx


def _cell(row, i: int | None):
    if i is None or i >= len(row):
        return None
    return row[i]


def _read_rows(rows: Sequence, headers: Sequence[str]) -> list[_Row]:
    """The trip and charge rows, parsed, with each row's operator filled from
    its neighbours when blank."""
    idx = _column_indices(headers)
    parsed: list[_Row] = []
    for i, row in enumerate(rows):
        label = _cell(row, idx["Leg Type"])
        if _leg_is_stop(label):
            continue
        if _leg_is_charge(label):
            kind = "charge"
        elif is_trip_leg(label):
            kind = "trip"
        else:
            continue
        parsed.append(
            _Row(
                index=i,
                kind=kind,
                label=label,
                start=_parse_time(_cell(row, idx["Start Time (UTC)"])),
                end=_parse_time(_cell(row, idx["End Time (UTC)"])),
                origin=_parse_point(_cell(row, idx["Origin (Lat, Lon)"])),
                dest=_parse_point(_cell(row, idx["Destination (Lat, Lon)"])),
                distance_km=_parse_float(_cell(row, idx["Distance (km)"])),
                operator=_parse_operator(_cell(row, idx["Operator"])),
                energy_kwh=_parse_float(_cell(row, idx["Energy Change (kWh)"])),
            )
        )
    # Time order (rows without a start last, in input order), then fill a blank
    # operator from the nearest row before it, else after it.
    parsed.sort(
        key=lambda r: (
            r.start is None,
            r.start if r.start is not None else pd.Timestamp.min,
            r.index,
        )
    )
    last = None
    for r in parsed:
        if r.operator is None:
            r.operator = last
        else:
            last = r.operator
    nxt = None
    for r in reversed(parsed):
        if r.operator is None:
            r.operator = nxt
        else:
            nxt = r.operator
    return parsed


# ── Geometry ─────────────────────────────────────────────────────────────────


def _distance_km(lat1, lon1, lat2, lon2):
    """Great-circle (haversine) distance in km; broadcasts over arrays."""
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lon2) - np.radians(lon1)
    h = np.sin(dphi / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0)))


def _near_matrix(lat: np.ndarray, lon: np.ndarray, radius_km: float) -> np.ndarray:
    """``near[i, j]``: point ``j`` lies within ``radius_km`` of point ``i``
    (computed in row blocks, so only the boolean matrix is held in full)."""
    n = len(lat)
    near = np.empty((n, n), dtype=bool)
    for s in range(0, n, 1024):
        e = min(s + 1024, n)
        near[s:e] = (
            _distance_km(lat[s:e, None], lon[s:e, None], lat[None, :], lon[None, :])
            <= radius_km
        )
    return near


@dataclass
class _Site:
    lat: float
    lon: float
    support: int


def _medoid(lat: np.ndarray, lon: np.ndarray) -> int:
    """Index of the point with the smallest total distance to the others."""
    d = _distance_km(lat[:, None], lon[:, None], lat[None, :], lon[None, :])
    return int(np.argmin(d.sum(axis=1)))


def _densest_sites(
    points: list[tuple[float, float]],
    owners: list | None = None,
    weights: list[float] | None = None,
    *,
    radius_km: float = HOME_DETECTION_KM,
    min_support: int = 1,
    limit: int | None = None,
) -> list[_Site]:
    """Greedy densest-first clustering of ``points``.

    A point's support is the number of distinct ``owners`` (an overnight stay, a
    charge session, a trip endpoint) among the unassigned points within
    ``radius_km`` of it; ``owners`` defaults to one per point. The point with the
    largest support (ties: the largest weight within the radius, then the
    earliest point) seeds a site, every unassigned point within the radius of
    it joins the site, and the process repeats on the rest; the site's centre is
    its medoid, the member closest in total to the others, so that it sits in
    the middle of the site rather than at the edge a seed can lie on. The
    supports come out non-increasing; clustering stops below ``min_support`` or
    at ``limit`` sites.
    """
    n = len(points)
    if n == 0:
        return []
    lat = np.array([p[0] for p in points], dtype=float)
    lon = np.array([p[1] for p in points], dtype=float)
    near = _near_matrix(lat, lon, radius_km)
    w = np.zeros(n) if weights is None else np.asarray(weights, dtype=float)
    distinct_owners = owners is None or len(set(owners)) == n
    if not distinct_owners:
        owner_ids = {o: k for k, o in enumerate(dict.fromkeys(owners))}
        onehot = np.zeros((n, len(owner_ids)), dtype=np.int32)
        for i, o in enumerate(owners):
            onehot[i, owner_ids[o]] = 1
    remaining = np.ones(n, dtype=bool)
    sites: list[_Site] = []
    while remaining.any():
        live = near & remaining[None, :]
        if distinct_owners:
            support = live.sum(axis=1)
        else:
            support = ((live.astype(np.int32) @ onehot) > 0).sum(axis=1)
        support = np.where(remaining, support, -1)
        top = int(support.max())
        if top < min_support:
            break
        candidates = np.flatnonzero(support == top)
        weight = live[candidates].astype(float) @ w
        seed = candidates[int(np.argmax(weight))]  # first maximum: the earliest
        members = np.flatnonzero(live[seed])
        centre = members[_medoid(lat[members], lon[members])]
        sites.append(_Site(lat=float(lat[centre]), lon=float(lon[centre]), support=top))
        remaining[members] = False
        if limit is not None and len(sites) >= limit:
            break
    return sites


def _within(point, lat: float, lon: float, radius_km: float) -> bool:
    return (
        point is not None
        and float(_distance_km(point[0], point[1], lat, lon)) <= radius_km
    )


# ── Base detection ───────────────────────────────────────────────────────────


def _overnight_stays(
    trips: list[_Row], charges: list[_Row]
) -> list[list[tuple[float, float]]]:
    """The places of an operator's overnight stays (see the module docstring)."""
    timed = [t for t in trips if t.start is not None and t.end is not None]
    if not timed:
        return []
    charge_mids = [(c, c.midpoint) for c in charges if c.midpoint is not None]
    first, last = timed[0], timed[-1]
    stays = [
        [first.origin] + [c.position for c, mid in charge_mids if mid <= first.start]
    ]
    for a, b in zip(timed, timed[1:]):
        if b.start - a.end >= _OVERNIGHT:
            stays.append(
                [a.dest, b.origin]
                + [c.position for c, mid in charge_mids if a.end <= mid <= b.start]
            )
    stays.append(
        [last.dest] + [c.position for c, mid in charge_mids if mid >= last.end]
    )
    return [[p for p in stay if p is not None] for stay in stays]


def _operator_bases(
    operator: str | None, rows: list[_Row]
) -> tuple[list[Base], list[dict]]:
    """The bases of one operator's rows, and the overnight sites that were
    considered but fell short (for reporting)."""
    trips = [r for r in rows if r.kind == "trip"]
    charges = [r for r in rows if r.kind == "charge"]
    stays = [s for s in _overnight_stays(trips, charges) if s]
    n_stays = len(stays)
    n_days = len({t.start.date() for t in trips if t.start is not None})

    points, owners = [], []
    for k, stay in enumerate(stays):
        for p in stay:
            points.append(p)
            owners.append(k)
    sites = _densest_sites(points, owners, min_support=MIN_BASE_NIGHTS)
    night_min = max(
        MIN_BASE_NIGHTS,
        MIN_BASE_NIGHT_SHARE * n_stays,
        MIN_BASE_NIGHTS_PER_DRIVING_DAY * n_days,
    )
    chosen = [s for s in sites if s.support >= night_min]
    rejected = [
        {
            "lat": round(s.lat, 6),
            "lon": round(s.lon, 6),
            "nights": s.support,
            "operator_nights": n_stays,
            "driving_days": n_days,
        }
        for s in sites
        if s.support < night_min
    ][:5]
    evidence = EVIDENCE_OVERNIGHT

    if not chosen:
        placed = [c for c in charges if c.position is not None]
        chosen = _densest_sites(
            [c.position for c in placed],
            weights=[_positive(c.energy_kwh) for c in placed],
            min_support=MIN_FALLBACK_SUPPORT,
            limit=1,
        )
        evidence = EVIDENCE_CHARGES
    if not chosen:
        ends = [p for t in trips for p in (t.origin, t.dest) if p is not None]
        chosen = _densest_sites(ends, min_support=MIN_FALLBACK_SUPPORT, limit=1)
        evidence = EVIDENCE_TRIP_ENDS

    bases = []
    for s in chosen:
        near_charges = [
            c for c in charges if _within(c.position, s.lat, s.lon, HOME_DETECTION_KM)
        ]
        bases.append(
            Base(
                operator=operator,
                lat=s.lat,
                lon=s.lon,
                evidence=evidence,
                nights=sum(
                    1
                    for stay in stays
                    if any(_within(p, s.lat, s.lon, HOME_DETECTION_KM) for p in stay)
                ),
                operator_nights=n_stays,
                driving_days=n_days,
                charges=len(near_charges),
                charge_kwh=float(sum(_positive(c.energy_kwh) for c in near_charges)),
                group=-1,
            )
        )
    return _grouped(bases), rejected


def _grouped(bases: list[Base]) -> list[Base]:
    """Give bases closer than :data:`BASE_GROUP_KM` (single linkage) one group."""
    group = list(range(len(bases)))

    def find(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i in range(len(bases)):
        for j in range(i + 1, len(bases)):
            if (
                float(
                    _distance_km(bases[i].lat, bases[i].lon, bases[j].lat, bases[j].lon)
                )
                < BASE_GROUP_KM
            ):
                group[find(j)] = find(i)
    roots: dict[int, int] = {}
    return [
        replace(b, group=roots.setdefault(find(i), len(roots)))
        for i, b in enumerate(bases)
    ]


def _bases_by_operator(parsed: list[_Row]) -> tuple[dict, dict]:
    by_operator: dict[str | None, list[_Row]] = {}
    for r in parsed:
        by_operator.setdefault(r.operator, []).append(r)
    bases, rejected = {}, {}
    for op, rows in by_operator.items():
        bases[op], rejected[op] = _operator_bases(op, rows)
    return bases, rejected


def find_bases(rows: Sequence, headers: Sequence[str] = HEADERS) -> dict:
    """The bases of every operator in ``rows``: ``{operator: (Base, ...)}``.

    ``rows`` are report rows in the row-tuple layout of ``headers`` (minus the
    leading ``Leg Number``), in any order; Stop rows and rows of no known kind
    are ignored. An operator without a qualifying site maps to an empty tuple.
    """
    bases, _ = _bases_by_operator(_read_rows(rows, headers))
    return {op: tuple(b) for op, b in bases.items()}


def _km(value: float) -> str:
    return f"{value:g} km"


def leg_type_definition(diesel: bool = False) -> str:
    """The Definitions-sheet entry for the ``Leg Type`` column (EV or diesel),
    worded from the thresholds above."""
    trips = (
        '"Outbound" = starts at a base; "Return" = ends at one (base to base '
        'included); "In House" / "Round Trip" = starts and ends at the same base, '
        f'up to / over {_km(ROUND_TRIP_MIN_KM)}; "In Transit" = neither'
    )
    base = (
        "A base is a depot of the row's operator, found from this report: a site "
        f"({_km(HOME_DETECTION_KM)}) where the vehicle regularly stays "
        f"{OVERNIGHT_STOP_MIN_H:g} h or more between trips"
    )
    if diesel:
        return (
            f'Leg Type: trips (green) — {trips}. "Stop" = parked/idling gap '
            "between trips (white). Diesel vehicles have no charging events. "
            f"{base}; else its most frequent trip end."
        )
    return (
        f'Leg Type: trips (green) — {trips}. Charges (red) — "AC" / "DC" / '
        '"AC/DC" / "Charge" + "Home" (at a base) or "Away". "Stop" = gap between '
        f"segments (white). {base}; else where it charges most; else its most "
        "frequent trip end."
    )


# ── Labelling ────────────────────────────────────────────────────────────────


def _base_at(point, bases: list[Base]) -> Base | None:
    """The nearest base within :data:`HOME_DETECTION_KM` of ``point``."""
    if point is None or not bases:
        return None
    d = _distance_km(
        point[0],
        point[1],
        np.array([b.lat for b in bases]),
        np.array([b.lon for b in bases]),
    )
    i = int(np.argmin(d))
    return bases[i] if d[i] <= HOME_DETECTION_KM else None


def trip_leg_type(start_base, end_base, distance_km: float) -> str:
    """The label of a trip from its bases at each end (``None`` = no base).

    Same base group at both ends: ``"Round Trip"`` above
    :data:`ROUND_TRIP_MIN_KM`, else ``"In House"`` (a missing distance counts as
    short). Two different groups: ``"Return"``. One end only: ``"Outbound"`` /
    ``"Return"``. Neither: ``"In Transit"``.
    """
    if start_base is not None and end_base is not None:
        if start_base.group == end_base.group:
            return "Round Trip" if distance_km > ROUND_TRIP_MIN_KM else "In House"
        return "Return"
    if start_base is not None:
        return "Outbound"
    if end_base is not None:
        return "Return"
    return "In Transit"


def charge_leg_type(label: str, at_base: bool) -> str:
    """A charge label with its place set: ``"AC Away"`` → ``"AC Home"`` …

    The kind (``AC`` / ``DC`` / ``AC/DC`` / ``Charge``) is kept; a label without
    a place word gets one appended.
    """
    kind = _CHARGE_PLACE_RE.sub("", str(label).strip())
    return f"{kind} {HOME if at_base else AWAY}"


def assign_leg_types(
    rows: Sequence, headers: Sequence[str] = HEADERS, bases: dict | None = None
) -> LegTypeAssignment:
    """Find the bases of ``rows`` (or use ``bases``) and label every trip and
    charge row against them, without modifying ``rows``.

    Returns a :class:`LegTypeAssignment` whose ``labels`` align with ``rows``.
    Raises ``ValueError`` when ``headers`` lacks a required column.
    """
    parsed = _read_rows(rows, headers)
    rejected: dict = {}
    if bases is None:
        found, rejected = _bases_by_operator(parsed)
        bases = {op: tuple(b) for op, b in found.items()}
    idx = _column_indices(headers)
    labels = [_cell(row, idx["Leg Type"]) for row in rows]
    result = LegTypeAssignment(labels=labels, bases=bases, rejected=rejected)
    for r in parsed:
        op_bases = list(bases.get(r.operator, ()))
        if r.kind == "charge":
            new = charge_leg_type(r.label, _base_at(r.position, op_bases) is not None)
            result.charges += 1
        else:
            s, e = _base_at(r.origin, op_bases), _base_at(r.dest, op_bases)
            new = trip_leg_type(s, e, r.distance_km)
            if s is not None and e is not None and s.group != e.group:
                result.base_to_base += 1
            result.trips += 1
        if new != r.label:
            result.changes[(r.label, new)] += 1
        labels[r.index] = new
    return result


def relabel_rows(rows: Sequence, headers: Sequence[str] = HEADERS) -> LegTypeAssignment:
    """Label every trip and charge row of ``rows`` against the run's own bases,
    writing the new ``Leg Type`` into each row (the rows must be mutable).

    The entry point for a caller that assembles report rows itself; the
    generator calls it once the rows are final.
    """
    result = assign_leg_types(rows, headers)
    i_type = _row_col_index("Leg Type", tuple(headers))
    for row, label in zip(rows, result.labels):
        if i_type < len(row) and row[i_type] != label:
            row[i_type] = label
    return result


def describe_bases(bases: dict) -> list[str]:
    """One line per operator describing its bases; ``bases`` maps an operator
    to :class:`Base` objects or their :meth:`Base.as_dict` form."""
    lines = []
    for op, op_bases in bases.items():
        name = _operator_key(op)
        rows = [b.as_dict() if isinstance(b, Base) else b for b in op_bases]
        if not rows:
            lines.append(f"operator {name}: no base")
            continue
        parts = [
            f"({b['lat']:.4f}, {b['lon']:.4f}) from {b['evidence']}: "
            f"{b['nights']}/{b['operator_nights']} nights over "
            f"{b['driving_days']} driving days, {b['charges']} charges"
            f" ({b['charge_kwh']:.0f} kWh), group {b['group']}"
            for b in rows
        ]
        lines.append(f"operator {name}: " + "; ".join(parts))
    return lines
