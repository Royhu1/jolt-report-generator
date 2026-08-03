"""
weather_patch.py
================
Unified weather-patching entry point (dispatcher).

This is the single place where the *default* weather-patching strategy lives.
By default it uses the coarse :class:`WeatherPatcher` (origin/destination two-point
average per leg, with cross-row de-duplication), which is quota-friendly. The
fine-grained :class:`FineGrainedWeatherPatcher` (in-trip multi-sampling) is used
**only when explicitly opted in** via ``mode="fine"`` (or the ``--fine-grained``
CLI flag).

Why coarse is the default
-------------------------
Fine-grained patching samples every GPS point inside each trip window (one
OpenWeather call per ~1 km / 1 h cache key). For a single vehicle this is on the
order of ~17k cold-cache calls, and ~220k fleet-wide, which immediately trips the
OpenWeather subscription into HTTP 429 ("exceeding of requests limitation of your
subscription type") and temporarily bans the account. The coarse patcher only
queries the origin and destination of each leg (de-duplicated across rows), so its
call volume is two orders of magnitude smaller and stays within quota. Empirically
the origin/destination average is close to the fine-grained aggregate, so coarse is
the right default and fine-grained is reserved for analyses that genuinely need
high spatial resolution.

Usage (Python)
--------------
    from jolt_toolkit.report_generator.weather_patch import patch_weather

    # default — coarse, quota-friendly
    patch_weather("excel_report_database/<version>/KY24LHT/")

    # explicit opt-in — fine-grained (needs --debug raw_telematics CSVs)
    patch_weather(
        "excel_report_database/<version>/YK73WFN/jolt_report_YK73WFN_20250601_20250830.xlsx",
        mode="fine",
        force_repatch=True,
    )

Usage (CLI)
-----------
    # default coarse
    python -m jolt_toolkit.report_generator.weather_patch \
        excel_report_database/<version>/KY24LHT/

    # explicit fine-grained
    python -m jolt_toolkit.report_generator.weather_patch \
        excel_report_database/<version>/YK73WFN/jolt_report_YK73WFN_20250601_20250830.xlsx \
        --fine-grained --force-repatch

Both patcher classes are left untouched and remain importable directly; this module
only centralises which one is the default. Requires the environment variable
``OPENWEATHER_API_KEYS`` (comma-separated keys).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from jolt_toolkit.report_generator.report_builder import DIESEL_HEADERS, HEADERS
from jolt_toolkit.report_generator.weather_fetcher.fine_grained_patcher import (
    FineGrainedWeatherPatcher,
)
from jolt_toolkit.report_generator.weather_patcher import WeatherPatcher

logger = logging.getLogger(__name__)

# ── Weather-patching modes ───────────────────────────────────────────────────
WEATHER_MODE_COARSE = "coarse"
WEATHER_MODE_FINE = "fine"
WEATHER_MODES = (WEATHER_MODE_COARSE, WEATHER_MODE_FINE)
#: The default mode. Coarse is quota-friendly; fine must be requested explicitly.
DEFAULT_WEATHER_MODE = WEATHER_MODE_COARSE


def patch_weather(
    target: str | Path,
    *,
    mode: str = DEFAULT_WEATHER_MODE,
    headers: tuple = HEADERS,
    raw_telematics_dir: str | Path | None = None,
    min_sample_interval_s: int = 60,
    force_repatch: bool = False,
    overwrite: bool = True,
    cache_file: str | Path | None = None,
    max_workers: int | None = None,
) -> Any:
    """
    Patch weather columns into an xlsx report (single file) or every
    ``jolt_report_*.xlsx`` in a folder, dispatching to the chosen patcher.

    Args:
        target:                 An xlsx file or a folder containing reports.
        mode:                   ``"coarse"`` (default, :class:`WeatherPatcher`) or
                                ``"fine"`` (:class:`FineGrainedWeatherPatcher`,
                                explicit opt-in only).
        headers:                Column layout for the fine-grained patcher's dynamic
                                column resolution. ``HEADERS`` (EV, default) or
                                ``DIESEL_HEADERS``. Ignored by the coarse patcher,
                                which uses fixed EV-layout indices.
        raw_telematics_dir:     Fine-grained only — directory of ``raw_*.csv``. If
                                ``None`` the patcher auto-locates ``<xlsx>/../
                                raw_telematics``.
        min_sample_interval_s:  Fine-grained only — in-trip down-sample interval (s).
        force_repatch:          Fine-grained only — rewrite all trip-like rows rather
                                than only cells that still need patching.
        overwrite:              Fine-grained only — overwrite the source xlsx
                                (``False`` writes ``*_fineweather.xlsx``).
        cache_file:             Override the patcher's cache JSON path.
        max_workers:            Override the patcher's concurrent-request count.

    Returns:
        Whatever the underlying patcher returns: coarse ``patch_file`` -> int
        (rows patched), coarse ``patch_folder`` -> ``{filename: rows}``; fine
        ``patch_file`` -> stats dict, fine ``patch_folder`` -> ``{filename: stats}``.

    Raises:
        ValueError: if ``mode`` is not one of :data:`WEATHER_MODES`.
    """
    mode = (mode or DEFAULT_WEATHER_MODE).lower()
    if mode not in WEATHER_MODES:
        raise ValueError(
            f"Unknown weather mode {mode!r}; expected one of {WEATHER_MODES}."
        )

    target = Path(target)
    is_folder = target.is_dir()

    if mode == WEATHER_MODE_COARSE:
        kwargs: dict[str, Any] = {}
        if cache_file is not None:
            kwargs["cache_file"] = cache_file
        if max_workers is not None:
            kwargs["max_workers"] = max_workers
        patcher = WeatherPatcher(**kwargs)
        logger.info("Weather patching (coarse / origin-dest average): %s", target)
        if is_folder:
            return patcher.patch_folder(target)
        return patcher.patch_file(target)

    # mode == "fine" — explicit opt-in only
    kwargs = {
        "raw_telematics_dir": raw_telematics_dir,
        "min_sample_interval_s": min_sample_interval_s,
        "headers": headers,
    }
    if cache_file is not None:
        kwargs["cache_file"] = cache_file
    if max_workers is not None:
        kwargs["max_workers"] = max_workers
    patcher = FineGrainedWeatherPatcher(**kwargs)
    logger.info("Weather patching (fine-grained / in-trip multi-sample): %s", target)
    if is_folder:
        return patcher.patch_folder(
            target, overwrite=overwrite, force_repatch=force_repatch
        )
    return patcher.patch_file(target, overwrite=overwrite, force_repatch=force_repatch)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "JOLT weather patcher. Default is coarse (origin/destination average, "
            "quota-friendly); pass --fine-grained for in-trip multi-sampling."
        )
    )
    parser.add_argument(
        "target",
        help="An xlsx report file or a folder containing jolt_report_*.xlsx.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--mode",
        choices=WEATHER_MODES,
        default=DEFAULT_WEATHER_MODE,
        help="coarse (default, quota-friendly) or fine (explicit high-resolution).",
    )
    mode_group.add_argument(
        "--fine-grained",
        "--fine",
        dest="fine_grained",
        action="store_true",
        help="Shortcut for --mode fine (in-trip GPS multi-sampling).",
    )
    parser.add_argument(
        "--diesel",
        action="store_true",
        help="Use DIESEL_HEADERS column layout (fine-grained mode only).",
    )
    parser.add_argument(
        "--raw-telematics-dir",
        default=None,
        help="Fine-grained only: directory of raw_*.csv (auto-located if omitted).",
    )
    parser.add_argument(
        "--min-sample-interval-s",
        type=int,
        default=60,
        help="Fine-grained only: in-trip down-sample interval in seconds.",
    )
    parser.add_argument(
        "--force-repatch",
        action="store_true",
        help="Fine-grained only: rewrite all trip-like rows, not just missing cells.",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Fine-grained only: write *_fineweather.xlsx instead of overwriting.",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(".env")
    except Exception:  # pragma: no cover - dotenv is optional at runtime
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )

    mode = WEATHER_MODE_FINE if args.fine_grained else args.mode
    headers = DIESEL_HEADERS if args.diesel else HEADERS

    logger.info("Weather patch mode: %s (target=%s)", mode, args.target)
    result = patch_weather(
        args.target,
        mode=mode,
        headers=headers,
        raw_telematics_dir=args.raw_telematics_dir,
        min_sample_interval_s=args.min_sample_interval_s,
        force_repatch=args.force_repatch,
        overwrite=not args.no_overwrite,
    )
    logger.info("Weather patch finished: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
