"""
Trip boundaries taken from the positions (opt-in post-pass).

A trip found on the speed or on the SOC can end before the vehicle has arrived:
on a sparse feed the SOC trips end at the last SOC step, and a SOC that freezes
while the vehicle drives hides the rest of the drive in the Stop that follows. A
trip can equally start after the vehicle has left — where a mass split cut it at
the first reading of a new trailer mass, say — or run on through a depot visit
its stop bridging did not see. The positions show where the vehicle actually
stayed. This module finds those stays in a leg's telematics and moves the trip
boundaries onto them: a trip that ended before the vehicle settled at the next
place it stayed at ends at its arrival there, one that started after the vehicle
had left the place before starts at its departure, and a trip is cut at a place
the vehicle is seen staying at on the way. A boundary the detector put at a
place is left where it is.

The pass works on the final discharge segments of one leg and is switched on
per pipeline (``position_trip_boundaries``); ``run_segment_detection`` calls it.
It never creates a trip where the detector found none, and never merges two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .constants import ODO_COL, TIME_COL
from .timeutil import _in_form_of, _to_utc

logger = logging.getLogger(__name__)

#: The radius (km) of a place: the vehicle stays at a place while every fix lies
#: within it of the first. Wide enough for a depot's yard, narrow enough that the
#: road leading to it is not the depot.
DEFAULT_STAY_RADIUS_KM = 0.5

#: The time (minutes) the vehicle must spend at a place for it to be a stay.
DEFAULT_STAY_MIN_MINUTES = 30.0

#: The most (km) the odometer may advance during a stay: a yard's manoeuvres stay
#: below it, a round trip that happens to end where it started does not.
DEFAULT_STAY_MAX_KM = 5.0

#: The parameters a pipeline's ``position_params`` may carry, with their defaults.
POSITION_PARAM_DEFAULTS = {
    "stay_radius_km": DEFAULT_STAY_RADIUS_KM,
    "stay_min_minutes": DEFAULT_STAY_MIN_MINUTES,
    "stay_max_km": DEFAULT_STAY_MAX_KM,
}

#: Between two fixes the vehicle stood still when the odometer advanced no more
#: than this (km): the counter's resolution, as in the replayed-odometer pass.
_STILL_ODOMETER_KM = 0.05

#: ... or, without an odometer reading on both, when the two positions lie no
#: further apart than this (km): a parked receiver's scatter.
_STILL_POSITION_KM = 0.1

#: Consecutive standing fixes spanning at least this long (minutes) are a halt:
#: a moved boundary never reaches across one. Longer than a queue at a junction.
_HALT_MIN_MINUTES = 5.0

#: A fix reached from, and left to, its neighbours faster than this (km/h) is a
#: position glitch and is ignored.
_GLITCH_SPEED_KMH = 250.0

#: The speed (km/h) at which the vehicle is taken to have covered the distance
#: from its last fix at a place to the next fix elsewhere: the rest of that gap
#: may have been spent at the place. A lorry's road speed, so the stay is not
#: taken to be shorter than it can have been.
_STAY_EXIT_SPEED_KMH = 90.0

_EARTH_RADIUS_KM = 6371.0088
_NS_PER_MIN = 60_000_000_000
_NS_PER_H = 3_600_000_000_000


@dataclass(frozen=True)
class Stay:
    """A place the vehicle stayed at, as the fixes of one leg show it (UTC).

    ``first`` and ``last`` are the first and last fix at the place. ``arrival``
    is the first fix from which the vehicle stood still — where a trip reaching
    the place ends — and ``departure`` the last fix it was reached standing
    still — where a trip leaving the place starts. Manoeuvres at the place, and
    the unobserved rest of a gap after ``last``, belong to the stay. ``cuts``
    tells whether the vehicle was seen standing there, from its arrival to its
    departure, for the stay's whole minimum time: only such a stay may cut a
    trip in two.
    """

    first: pd.Timestamp
    last: pd.Timestamp
    arrival: pd.Timestamp
    departure: pd.Timestamp
    cuts: bool


@dataclass(frozen=True)
class Positions:
    """What one leg's fixes say: where and when, its stays and its halts (UTC).

    ``fixes_ns`` are the fix times (nanoseconds, ascending), with the fixes'
    ``lat`` / ``lon`` and odometer ``odo`` (NaN where the fix has no reading);
    ``stays`` are the places the vehicle stayed at (:func:`read_positions`) and
    ``halts`` the ``(first, last)`` fixes of each run of standing fixes lasting
    at least :data:`_HALT_MIN_MINUTES`, in time order.
    """

    fixes_ns: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    odo: np.ndarray
    stays: list[Stay]
    halts: list[tuple[pd.Timestamp, pd.Timestamp]]

    def moved(self, a: int, b: int) -> bool:
        """Whether the vehicle moved between fix ``a`` and a later fix ``b``.

        By the odometer (it advanced more than :data:`_STILL_ODOMETER_KM`) where
        both fixes carry a reading, else by the position (more than
        :data:`_STILL_POSITION_KM` apart).
        """
        if np.isfinite(self.odo[a]) and np.isfinite(self.odo[b]):
            return bool(self.odo[b] - self.odo[a] > _STILL_ODOMETER_KM)
        step = _haversine_km(self.lat[a], self.lon[a], self.lat[b], self.lon[b])
        return bool(step > _STILL_POSITION_KM)


def position_params(pipeline_cfg: dict) -> dict:
    """A pipeline's stay parameters: its ``position_params``, else the defaults."""
    params = dict(POSITION_PARAM_DEFAULTS)
    params.update(pipeline_cfg.get("position_params") or {})
    return params


# =============================================================================
# The leg's position track
# =============================================================================
def _haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    lat1, lon1, lat2, lon2 = (
        np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2)
    )
    a = (
        np.sin((lat2 - lat1) / 2.0) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _position_track(df: pd.DataFrame) -> pd.DataFrame:
    """The leg's fixes in time order: ``t_ns`` (UTC), ``lat``, ``lon``, ``odo``.

    A fix is a row with a parseable timestamp and a position (a zero coordinate
    is the feed's placeholder for none). An isolated fix that the vehicle would
    have had to reach and leave faster than :data:`_GLITCH_SPEED_KMH` is dropped.
    The odometer is read as the frame carries it — the caller hands the frame
    whose replayed readings are already blanked. Empty without the columns.
    """
    empty = pd.DataFrame({"t_ns": [], "lat": [], "lon": [], "odo": []})
    if any(c not in df.columns for c in (TIME_COL, "latitude", "longitude")):
        return empty
    track = pd.DataFrame(
        {
            "t": pd.to_datetime(df[TIME_COL], errors="coerce", utc=True),
            "lat": pd.to_numeric(df["latitude"], errors="coerce"),
            "lon": pd.to_numeric(df["longitude"], errors="coerce"),
            "odo": (
                pd.to_numeric(df[ODO_COL], errors="coerce")
                if ODO_COL in df.columns
                else np.nan
            ),
        }
    )
    track.loc[(track["lat"] == 0) | (track["lon"] == 0), ["lat", "lon"]] = np.nan
    track = track.dropna(subset=["t", "lat", "lon"])
    if track.empty:
        return empty
    track = track.sort_values("t", kind="mergesort").reset_index(drop=True)
    track["t_ns"] = pd.DatetimeIndex(track["t"]).as_unit("ns").asi8
    return _drop_glitches(track[["t_ns", "lat", "lon", "odo"]])


def _drop_glitches(track: pd.DataFrame) -> pd.DataFrame:
    """Drop each fix that is a jump out and straight back (see _position_track)."""
    if len(track) < 3:
        return track
    lat = track["lat"].to_numpy()
    lon = track["lon"].to_numpy()
    step_km = _haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:])
    step_h = np.diff(track["t_ns"].to_numpy()) / _NS_PER_H
    speed = np.full(len(step_km), np.inf)
    moving = step_h > 0
    speed[moving] = step_km[moving] / step_h[moving]
    speed[~moving & (step_km <= _STILL_POSITION_KM)] = 0.0
    too_fast = speed > _GLITCH_SPEED_KMH
    glitch = np.zeros(len(track), dtype=bool)
    glitch[1:-1] = too_fast[:-1] & too_fast[1:]
    if not glitch.any():
        return track
    return track[~glitch].reset_index(drop=True)


# =============================================================================
# Stays and halts
# =============================================================================
def read_positions(
    df: pd.DataFrame,
    stay_radius_km: float = DEFAULT_STAY_RADIUS_KM,
    stay_min_minutes: float = DEFAULT_STAY_MIN_MINUTES,
    stay_max_km: float = DEFAULT_STAY_MAX_KM,
) -> Positions:
    """One leg's fixes, the places its vehicle stayed at and where it halted.

    A **stay** is a run of consecutive fixes that all lie within
    ``stay_radius_km`` of the run's first fix, over which the odometer advances
    no more than ``stay_max_km``, and that lasts at least ``stay_min_minutes`` —
    from its first fix to its last, plus the part of the gap to the next fix
    elsewhere that the distance to it leaves unexplained at
    :data:`_STAY_EXIT_SPEED_KMH`, since a feed that falls silent while the
    vehicle stands says nothing until it has left. Runs are taken greedily from
    the first fix on, each as long as it goes (the stay-point method). Jitter
    within the radius cannot end a stay, and an isolated position glitch is
    ignored.

    Between two fixes the vehicle stood still when the odometer advanced no more
    than :data:`_STILL_ODOMETER_KM` (without an odometer reading on both, when
    the position moved no more than :data:`_STILL_POSITION_KM`). Within a stay,
    the arrival is the first fix from which the vehicle stood still and the
    departure the last fix reached standing still; without such a step, the
    first and the last fix. A stay cuts (:attr:`Stay.cuts`) when its arrival and
    departure lie ``stay_min_minutes`` apart or more. A **halt** is a run of
    fixes each reached standing still from the one before, spanning at least
    :data:`_HALT_MIN_MINUTES`.
    """
    track = _position_track(df)
    n = len(track)
    t_ns = track["t_ns"].to_numpy(dtype="int64")
    lat = track["lat"].to_numpy(dtype=float)
    lon = track["lon"].to_numpy(dtype=float)
    odo = track["odo"].to_numpy(dtype=float)
    if n == 0:
        return Positions(t_ns, lat, lon, odo, [], [])
    min_ns = stay_min_minutes * _NS_PER_MIN

    # still[k]: the vehicle stood still between fix k and fix k + 1
    odo_known = np.isfinite(odo[:-1]) & np.isfinite(odo[1:])
    still = np.where(
        odo_known,
        (odo[1:] - odo[:-1]) <= _STILL_ODOMETER_KM,
        _haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:]) <= _STILL_POSITION_KM,
    )

    def first_elsewhere(i: int) -> int:
        """The first fix after ``i`` outside its place (``n`` if none)."""
        j = i + 1
        while j < n:
            stop = min(n, j + 256)
            inside = (
                _haversine_km(lat[i], lon[i], lat[j:stop], lon[j:stop])
                <= stay_radius_km
            )
            if np.isfinite(odo[i]):
                climbed = odo[j:stop] - odo[i]
                inside &= ~(np.isfinite(climbed) & (climbed > stay_max_km))
            outside = np.flatnonzero(~inside)
            if len(outside):
                return j + int(outside[0])
            j = stop
        return n

    def at(k: int) -> pd.Timestamp:
        return pd.Timestamp(int(t_ns[k]), tz="UTC")

    stays: list[Stay] = []
    i = 0
    while i < n:
        j = first_elsewhere(i)
        last = j - 1
        duration_ns = float(t_ns[last] - t_ns[i])
        if j < n:
            if np.isfinite(odo[last]) and np.isfinite(odo[j]) and odo[j] >= odo[last]:
                exit_km = float(odo[j] - odo[last])
            else:
                exit_km = float(_haversine_km(lat[last], lon[last], lat[j], lon[j]))
            exit_ns = exit_km / _STAY_EXIT_SPEED_KMH * _NS_PER_H
            duration_ns += max(0.0, float(t_ns[j] - t_ns[last]) - exit_ns)
        if duration_ns < min_ns:
            i += 1
            continue
        arrival = next((k for k in range(i, last) if still[k]), i)
        departure = next((k for k in range(last, i, -1) if still[k - 1]), last)
        if arrival > departure:
            arrival, departure = i, last
        stays.append(
            Stay(
                first=at(i),
                last=at(last),
                arrival=at(arrival),
                departure=at(departure),
                cuts=bool(t_ns[departure] - t_ns[arrival] >= min_ns),
            )
        )
        i = j

    halts: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    k = 0
    while k < n - 1:
        if not still[k]:
            k += 1
            continue
        a = k
        while k < n - 1 and still[k]:
            k += 1
        if t_ns[k] - t_ns[a] >= _HALT_MIN_MINUTES * _NS_PER_MIN:
            halts.append((at(a), at(k)))
    return Positions(t_ns, lat, lon, odo, stays, halts)


# =============================================================================
# Boundaries onto the stays
# =============================================================================
def _window(seg: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    return _to_utc(seg["start_time"]), _to_utc(seg["end_time"])


def _occupied(windows: list, lo: pd.Timestamp, hi: pd.Timestamp) -> bool:
    """Whether any of ``windows`` (UTC pairs) intersects the open interval (lo, hi)."""
    return any(w_start < hi and w_end > lo for w_start, w_end in windows)


def settled_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
    positions: Positions,
    others: list,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """The windows one trip becomes once its boundaries follow the stays (UTC).

    - **Start.** A trip that starts after the vehicle had left the place it
      stayed at before — the fix at or before the trip's start shows it moved
      since its departure from there — starts at that departure instead, or at
      the last fix of a later halt on the way, should there be one.
    - **End.** A trip that ends before the vehicle has settled at the next place
      it stays at — it moved between the fix at or after the trip's end and its
      arrival there — ends at that arrival instead, or at the first fix of an
      earlier halt on the way, should there be one.
    - **On the way.** A stay the vehicle was seen standing at for its whole
      minimum time (:attr:`Stay.cuts`) that lies wholly inside the trip cuts it
      in two: the part before ends at the arrival, the part after starts at the
      departure. A stay known only through a silent gap cuts nothing: the
      vehicle may have left it soon after its last fix there.

    A boundary that already lies at a place — within a stay, or at a halt — is
    left where the detector put it, and a boundary is moved outwards only over
    time no other segment of the leg (``others``, UTC pairs) occupies, so a trip
    never swallows another trip or a charge. Returns ``[(start, end)]``
    unchanged when nothing applies.
    """
    stays = positions.stays
    fixes_ns = positions.fixes_ns
    new_start, new_end = start, end

    def at_a_place(instant: pd.Timestamp) -> bool:
        return any(s.first <= instant <= s.last for s in stays) or any(
            h_first <= instant <= h_last for h_first, h_last in positions.halts
        )

    def fix_index(instant: pd.Timestamp) -> int:
        return int(np.searchsorted(fixes_ns, instant.value, side="left"))

    if not at_a_place(start):
        before = [s for s in stays if s.departure < start]
        if before:
            target = before[-1].departure
            halts = [h for h in positions.halts if target < h[1] < start]
            if halts:
                target = halts[-1][1]
            last_seen = int(np.searchsorted(fixes_ns, start.value, side="right")) - 1
            origin = fix_index(target)
            if (
                last_seen > origin
                and positions.moved(origin, last_seen)
                and not _occupied(others, target, start)
            ):
                new_start = target

    if not at_a_place(end):
        after = [s for s in stays if s.arrival > end]
        if after:
            target = after[0].arrival
            halts = [h for h in positions.halts if end < h[0] < target]
            if halts:
                target = halts[0][0]
            next_seen = fix_index(end)
            destination = fix_index(target)
            if (
                next_seen < destination
                and positions.moved(next_seen, destination)
                and not _occupied(others, end, target)
            ):
                new_end = target

    if new_end <= new_start:
        return [(start, end)]
    windows = []
    cursor = new_start
    for stay in stays:
        if stay.cuts and cursor < stay.arrival and stay.departure < new_end:
            windows.append((cursor, stay.arrival))
            cursor = stay.departure
    windows.append((cursor, new_end))
    return [(s, e) for s, e in windows if e > s]


def settle_trip_boundaries(
    discharge_segs: list[dict],
    charge_segs: list[dict],
    positions: Positions,
    measure: Callable[[pd.Timestamp, pd.Timestamp], list[dict]],
) -> tuple[list[dict], dict]:
    """Move one leg's trip boundaries onto its stays (see :func:`settled_windows`).

    ``measure(start, end)`` measures a trip window as the leg's detector would —
    its SOC and energy floors, its capacity band — and returns the segments it
    yields (empty when the floors reject it); it is handed the new boundaries in
    the form the trip wrote its own start in. A trip whose windows are its own
    is returned as it is, the same dict — and when no trip changes, the list
    handed in is returned itself. A trip that changes is replaced by its
    measured windows; a window the floors reject is left out (its time becomes
    part of a Stop), and when no window of a trip survives, the trip is kept
    unchanged. Trips are handled in time order, each bounded by the segments as
    they stand, so no two segments come to overlap.

    Returns ``(segments, counts)``: the leg's discharge segments in time order,
    and the numbers of trips ``moved`` (replaced by their measured windows),
    ``cut`` (of those, cut at a stay on the way) and ``kept`` (every window
    rejected, so kept as they were), and of windows ``dropped`` by the floors.
    """
    counts = {"moved": 0, "cut": 0, "kept": 0, "dropped": 0}
    if not discharge_segs or not positions.stays:
        return discharge_segs, counts
    order = sorted(
        range(len(discharge_segs)), key=lambda k: _window(discharge_segs[k])[0]
    )
    current: list[list[dict]] = [[seg] for seg in discharge_segs]
    charges = [_window(c) for c in charge_segs]
    for k in order:
        seg = discharge_segs[k]
        start, end = _window(seg)
        others = charges + [
            _window(s) for m, group in enumerate(current) if m != k for s in group
        ]
        windows = settled_windows(start, end, positions, others)
        if windows == [(start, end)]:
            continue
        like = seg["start_time"]
        measured: list[dict] = []
        for w_start, w_end in windows:
            parts = measure(_in_form_of(w_start, like), _in_form_of(w_end, like))
            if not parts:
                counts["dropped"] += 1
            measured.extend(parts)
        if not measured:
            counts["kept"] += 1
            continue
        counts["moved"] += 1
        if len(windows) > 1:
            counts["cut"] += 1
        current[k] = measured
    if not counts["moved"]:
        return discharge_segs, counts
    result = [s for group in current for s in group]
    result.sort(key=lambda s: _window(s)[0])
    return result, counts
