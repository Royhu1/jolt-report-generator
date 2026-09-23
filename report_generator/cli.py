"""Module entry point for JOLT Excel report generation.

The deployment entry point: ``python -m report_generator.cli
-veh <REG> -ds <start> -de <end>`` generates one report and returns a process
exit code (0 success / 2 bad invocation or missing key / 3 unknown vehicle).

Environment (loaded from a ``.env`` in the working directory if present):
  SRF_API_KEY          required — SRF platform API key
  OPENWEATHER_API_KEYS optional — weather patching (post-generation step)
  JOLT_CAPACITY_LEDGER optional — external capacity-ledger file (recommended:
                       vehicles.json is then never written)
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
        prog="python -m report_generator.cli",
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
        "+ raw logger/charger CSVs). No validation figures or inspect HTML "
        "are produced; render those externally from the persisted raw data.",
    )
    parser.add_argument(
        "--raw-only",
        dest="raw_only",
        action="store_true",
        default=False,
        help="Exact alias of --debug (both persist raw telematics + raw "
        "logger/charger CSVs, and nothing else).",
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
        "./excel_report_database/<data_namespace>.",
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

    # Importing the package (which ``python -m report_generator.cli`` does before
    # this function runs) loaded VEHICLE_CONFIG before the .env above was read,
    # so a JOLT_CAPACITY_LEDGER named only there is applied now, before the
    # generator is built. The report applies it again when it starts; the
    # overlay is idempotent, and a no-op without the variable.
    from report_generator.configs import apply_capacity_ledger
    from report_generator.general_pipeline import (
        VehicleNotFoundError,
        is_runtime_config,
    )
    from report_generator.segmentation.constants import VEHICLE_CONFIG

    ledger_path = apply_capacity_ledger(VEHICLE_CONFIG, skip=is_runtime_config)
    if ledger_path is not None:
        logger.info("Capacity ledger: %s", ledger_path)

    from report_generator import DATA_NAMESPACE, __version__
    from report_generator._generator import JOLTReportGenerator
    from report_generator.paths import default_report_root

    # Dual identity, always logged: the code revision and the data namespace it
    # writes into are two independent facts. Logging the namespace only when it
    # differs from __version__ would make the provenance vanish from the log the
    # day a release happens to advance both together.
    logger.info("JOLT Report Generator v%s", __version__)
    logger.info("Data namespace: %s", DATA_NAMESPACE)

    out_dir = args.out_dir or default_report_root()
    logger.info("Report output folder: %s", out_dir)

    # --raw-only and --debug are equivalent: both persist the raw artefacts and
    # nothing else. save_figures is a no-op kept for call-site compatibility.
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
