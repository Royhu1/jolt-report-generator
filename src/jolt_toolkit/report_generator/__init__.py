"""
report_generator — report-generation sub-package.

Public API:
  generate_report()   generate a single-vehicle Excel report
  patch_logger()      backfill weather data from the Logger
"""

from jolt_toolkit.report_generator._generator import JOLTReportGenerator


def generate_report(
    vehicle_registration: str,
    date_start: str,
    date_end: str,
    *,
    mode: str = "normal",
    debug: bool = False,
    save_figures: bool = True,
    outputfolder: str = "excel_report_database",
) -> None:
    """Convenience function: generate a single-vehicle Excel report.

    ``debug=True`` persists raw artefacts (raw telematics + raw logger/charger
    CSVs). ``save_figures`` is a **no-op**, kept only for backward-compatible
    call sites — the package no longer paints validation figures or writes the
    inspect HTML; render them via the report-visuals skill.
    """
    from jolt_toolkit import __version__

    gen = JOLTReportGenerator(
        report_output_folder=f"./{outputfolder}/{__version__}",
        overwrite_existing_report=True,
        debug_mode=debug,
        fast_mode=(mode == "fast"),
        save_figures=save_figures,
    )
    gen.generate_report(
        vehicle_registration=vehicle_registration,
        date_start=date_start,
        date_end=date_end,
    )


def patch_logger(
    vehicle_registration: str,
    date_start: str,
    date_end: str,
    *,
    debug: bool = False,
) -> None:
    """Convenience function: backfill weather data from the Logger."""
    from jolt_toolkit.report_generator.logger_patcher import LoggerPatcher

    patcher = LoggerPatcher()
    patcher.patch(vehicle_registration, date_start, date_end, debug=debug)
