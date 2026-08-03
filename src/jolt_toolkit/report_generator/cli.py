"""Module entry point for JOLT Excel report generation.

Equivalent to the ``generate-excel-report`` skill's ``generate_report.py``, but
inside the workspace so a platform deploy can run
``python -m jolt_toolkit.report_generator.cli -veh <REG> -ds <start> -de <end>``
without the skill checkout.

Environment (loaded from a ``.env`` in the working directory if present):
  SRF_API_KEY          required — SRF platform API key
  OPENWEATHER_API_KEYS optional — weather patching (post-generation step)
  JOLT_CONFIG_DIR      optional — override the config directory (writable)
  JOLT_CACHE_DIR       optional — override the cache root (default ./cache)
  SRF_API_ROOT         optional — override the SRF API root
"""

from __future__ import annotations

import argparse
import logging
import os

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jolt_toolkit.report_generator.cli",
        description="Generate a JOLT Excel report for a vehicle over a date range.",
    )
    parser.add_argument(
        "-veh",
        "--vehicle_registration",
        type=str,
        help="Vehicle registration, e.g. YK73WFN",
    )
    parser.add_argument("-ds", "--date_start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument(
        "-de", "--date_end", type=str, help="End date (YYYY-MM-DD, inclusive)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug mode: persist raw artefacts (raw telematics CSV "
        "+ raw logger/charger CSVs). The package no longer "
        "draws validation figures or writes inspect HTML here — render "
        "them via the report-visuals skill.",
    )
    parser.add_argument(
        "--raw-only",
        dest="raw_only",
        action="store_true",
        default=False,
        help="Alias of --debug (both persist raw telematics + raw "
        "logger/charger CSVs). No figures or inspect HTML are produced "
        "by the package; render them via the report-visuals skill.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        default=False,
        help="Fast mode: skip SRF Logger and Charger data fetching",
    )
    parser.add_argument(
        "--out-dir",
        "--report-output-folder",
        dest="out_dir",
        type=str,
        default=None,
        help="Output folder for the report. Defaults to "
        "./excel_report_database/<package_version>.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Load .env (if present in the working directory) before reading env vars.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # dotenv is optional at runtime
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )

    # Fail fast with a clear message instead of constructing an SRF client with
    # api_key=None and failing obscurely on the first request.
    if not os.environ.get("SRF_API_KEY"):
        logger.error(
            "SRF_API_KEY is not set. Export it, or add it to a .env file in the "
            "working directory, before generating a report."
        )
        return 2

    missing = [
        name
        for name, val in (
            ("-veh/--vehicle_registration", args.vehicle_registration),
            ("-ds/--date_start", args.date_start),
            ("-de/--date_end", args.date_end),
        )
        if not val
    ]
    if missing:
        logger.error("Missing required argument(s): %s", ", ".join(missing))
        return 2

    from jolt_toolkit import __version__
    from jolt_toolkit.report_generator._generator import JOLTReportGenerator
    from jolt_toolkit.report_generator.general_pipeline import VehicleNotFoundError

    logger.info("JOLT Report Generator v%s", __version__)

    out_dir = args.out_dir or f"./excel_report_database/{__version__}"
    logger.info("Report output folder: %s", out_dir)

    # --raw-only still writes the raw CSV + inspect HTML (needs debug_mode) but
    # skips the baked validation figures.
    debug_mode = args.debug or args.raw_only
    save_figures = not args.raw_only

    generator = JOLTReportGenerator(
        report_output_folder=out_dir,
        overwrite_existing_report=True,
        debug_mode=debug_mode,
        fast_mode=args.fast,
        save_figures=save_figures,
    )
    # An un-onboarded registration no longer errors — the generator falls
    # back to the general pipeline automatically. The ONE clean failure is a
    # registration that does not exist on SRF at all: report it as a single-line
    # error (no traceback spew) with a distinct non-zero exit code.
    try:
        generator.generate_report(
            vehicle_registration=args.vehicle_registration,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    except VehicleNotFoundError as exc:
        logger.error("%s", exc)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
