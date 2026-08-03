"""
report_generator.weather_fetcher.openweather
=============================================
Shared OpenWeather (One Call 3.0 timemachine) infrastructure for the two weather
patchers — the coarse ``weather_patcher.WeatherPatcher`` and the multi-sample
``fine_grained_patcher.FineGrainedWeatherPatcher``.

Consolidates the three near-identical pieces those patchers previously
duplicated:

* :class:`KeyManager` — multi-key rotation from ``OPENWEATHER_API_KEYS`` (was
  byte-identical bar the log label, now a ``label`` parameter).
* :class:`WeatherCache` — the filelock-guarded JSON cache. Behaviour-identical;
  the two patchers differed only in the freshly-created file's ``metadata``
  block (coarse writes none, fine writes a description block), which is now a
  ``metadata`` parameter so **each patcher writes exactly its previous init
  file**. The cache **key formula** ``f"{lat:.{precision}f},{lon:.{precision}f},
  {(dt // bucket) * bucket}"`` and the stored 6-tuple schema are unchanged, so
  existing ``cache/.weather_cache.json`` / ``cache/weather/.weather_cache_fine.json``
  files keep hitting.
* :class:`WeatherFetcher` — the threaded fetch. K→C conversion at the same point,
  identical 429 → disable-key handling, identical stored tuple; the per-patcher
  differences (stat counters, failure warning, tqdm label) are parameters.

Chinese comments are preserved from the source; Phase 4 translates them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from filelock import FileLock
from tqdm import tqdm

logger = logging.getLogger(__name__)

# OpenWeather One Call 3.0 historical (timemachine) endpoint — shared by both
# patchers (previously duplicated as each class's ``API_URL``).
TIMEMACHINE_URL = "https://api.openweathermap.org/data/3.0/onecall/timemachine"


class KeyManager:
    """Multi-key rotation manager (loaded from the OPENWEATHER_API_KEYS environment variable).

    ``label`` is the caller name used in the load / missing-key log lines
    (e.g. ``"WeatherPatcher"`` / ``"FineGrainedWeatherPatcher"``) so each
    patcher keeps its original message text.
    """

    def __init__(self, label: str):
        keys_str = os.environ.get("OPENWEATHER_API_KEYS", "")
        self._keys = {
            k.strip(): {"active": True, "usage": 0}
            for k in keys_str.split(",")
            if k.strip()
        }
        if self._keys:
            logger.info(f"{label}: {len(self._keys)} API key(s) loaded")
        else:
            logger.warning(f"{label}: OPENWEATHER_API_KEYS not set")

    def get_key(self) -> str | None:
        for k, v in self._keys.items():
            if v["active"]:
                return k
        return None

    def increment(self, key: str):
        if key in self._keys:
            self._keys[key]["usage"] += 1

    def disable(self, key: str):
        if key in self._keys and self._keys[key]["active"]:
            self._keys[key]["active"] = False
            masked = f"...{key[-8:]}" if len(key) > 8 else "***"
            logger.warning(f"Disabled API key: {masked}")

    def summary(self) -> dict:
        total = sum(v["usage"] for v in self._keys.values())
        active = sum(1 for v in self._keys.values() if v["active"])
        return {"total_keys": len(self._keys), "active": active, "total_usage": total}


class WeatherCache:
    """JSON file cache (thread-safe via filelock).

    Cache-key quantisation: the coordinate is rounded to ``precision`` decimal
    places (~1 km grid at 2) and the timestamp floored to a ``time_bucket_s``
    bucket (default 3600 s = 1 h, matching the hourly timemachine data), so
    nearby / same-hour points share a key and reuse one fetched value.

    ``metadata`` controls only the file written when the cache does not yet
    exist: ``None`` (coarse) writes ``{"cache": {}}`` verbatim; a dict (fine)
    writes ``{"metadata": <dict>, "cache": {}}`` with ``indent=2`` and logs
    ``init_log``. Existing caches are never rewritten, so this is init-only and
    keeps each patcher's prior on-disk format byte-for-byte.
    """

    def __init__(
        self,
        cache_file: str | Path,
        precision: int = 2,
        time_bucket_s: int = 3600,
        metadata: dict | None = None,
        init_log: str | None = None,
    ):
        self._path = Path(cache_file)
        self._lock_path = Path(str(cache_file) + ".lock")
        self._precision = precision
        self._time_bucket_s = max(int(time_bucket_s), 1)
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                if metadata is None:
                    json.dump({"cache": {}}, f)
                else:
                    json.dump({"metadata": metadata, "cache": {}}, f, indent=2)
            if init_log:
                logger.info(init_log)

    def _key(self, lat: float, lon: float, dt: int) -> str:
        # Quantise the coordinate to a ~``precision`` grid and floor the
        # timestamp to a ``time_bucket_s`` bucket (hourly), so nearby /
        # same-hour points share a key and reuse one fetched value.
        dt_bucket = (int(dt) // self._time_bucket_s) * self._time_bucket_s
        return f"{lat:.{self._precision}f},{lon:.{self._precision}f},{dt_bucket}"

    def get_batch(self, locations: list[tuple]) -> tuple[dict, list]:
        """Return (hit_map, miss_list). hit_map: {loc: weather_tuple}."""
        with FileLock(self._lock_path, timeout=10):
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        cache = data.get("cache", {})
        hit_map: dict = {}
        misses: list = []
        for loc in locations:
            k = self._key(*loc)
            if k in cache:
                hit_map[loc] = tuple(cache[k])
            else:
                misses.append(loc)
        return hit_map, misses

    def put_batch(self, results: dict):
        """results: {(lat, lon, dt): (temp, press, humid, wind_s, wind_d, weather_type)}."""
        with FileLock(self._lock_path, timeout=10):
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache = data.get("cache", {})
            for loc, weather in results.items():
                cache[self._key(*loc)] = list(weather)
            data["cache"] = cache
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

    @property
    def size(self) -> int:
        with FileLock(self._lock_path, timeout=10):
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return len(data.get("cache", {}))


class WeatherFetcher:
    """Threaded OpenWeather timemachine fetcher shared by both patchers.

    Holds the :class:`KeyManager`, a rotation lock, the worker count and
    per-``patch_file`` stat counters (``api_calls`` / ``failures`` — read by the
    fine patcher's summary, ignored by the coarse one). :meth:`fetch_single`
    performs the K→C conversion and 429 → disable-key handling identically to the
    two former private methods; :meth:`fetch_batch` runs them under a
    ``ThreadPoolExecutor``. ``desc`` and ``warn_on_failure`` reproduce each
    caller's tqdm label and end-of-batch failure warning.
    """

    API_URL = TIMEMACHINE_URL

    def __init__(self, keys: KeyManager, max_workers: int):
        self._keys = keys
        self._lock = threading.Lock()
        self._max_workers = max_workers
        self.api_calls = 0
        self.failures = 0

    def reset_stats(self) -> None:
        self.api_calls = 0
        self.failures = 0

    def fetch_single(self, loc: tuple) -> tuple[tuple, tuple | None]:
        """Fetch weather data for a single location (thread-safe)."""
        lat, lon, dt = loc
        with self._lock:
            api_key = self._keys.get_key()
        if not api_key:
            with self._lock:
                self.failures += 1
            return loc, None
        try:
            resp = requests.get(
                self.API_URL,
                params={"lat": lat, "lon": lon, "dt": dt, "appid": api_key},
                timeout=10,
            )

            if resp.status_code == 429:
                with self._lock:
                    self._keys.disable(api_key)
                    self.failures += 1
                return loc, None

            if resp.status_code != 200:
                logger.debug(
                    f"  HTTP {resp.status_code} for ({lat:.4f}, {lon:.4f}, dt={dt})"
                )
                with self._lock:
                    self.failures += 1
                return loc, None

            w = resp.json()["data"][0]
            with self._lock:
                self._keys.increment(api_key)
                self.api_calls += 1

            # Extract the weather type (e.g. Clear, Clouds, Rain)
            weather_type = None
            if "weather" in w and w["weather"]:
                weather_type = w["weather"][0].get("main")

            return loc, (
                round(w["temp"] - 273.15, 1),  # K → C
                w["pressure"],  # hPa
                w["humidity"],  # %
                w["wind_speed"],  # m/s
                w["wind_deg"],  # degrees
                weather_type,  # weather type
            )
        except Exception as e:
            logger.debug(f"  API error for ({lat:.4f}, {lon:.4f}): {e}")
            with self._lock:
                self.failures += 1
            return loc, None

    def fetch_batch(
        self, locations: list[tuple], *, desc: str, warn_on_failure: bool
    ) -> dict:
        """Fetch weather data for multiple locations concurrently."""
        results: dict = {}
        logger.info(
            f"  Fetching {len(locations)} locations "
            f"({self._max_workers} workers)..."
        )

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self.fetch_single, loc): loc for loc in locations}
            for f in tqdm(
                as_completed(futures), desc=desc, total=len(futures), leave=False
            ):
                loc, weather = f.result()
                if weather is not None:
                    results[loc] = weather

        if warn_on_failure:
            failed = len(locations) - len(results)
            if failed > 0:
                logger.warning(f"  {failed}/{len(locations)} locations failed")

        return results
