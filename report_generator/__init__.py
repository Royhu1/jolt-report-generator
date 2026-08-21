"""
report_generator — the JOLT Excel report generator.

A vendored code workspace, not an installable package: put the repository root
on the import path and ``import report_generator``.

Public API:
  JOLTReportGenerator   the pipeline class (fetch -> segment -> correct -> write)
  generate_report()     generate a single-vehicle Excel report
  patch_logger()        backfill weather data from the Logger

Constants:
  __version__           code revision
  DATA_NAMESPACE        the report-data tree the defaults write into
"""

from report_generator._generator import JOLTReportGenerator
from report_generator.version import DATA_NAMESPACE, __version__

__all__ = [
    "DATA_NAMESPACE",
    "JOLTReportGenerator",
    "__version__",
    "generate_report",
    "patch_logger",
]


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
    inspect HTML; render them externally from the persisted raw data.
    """
    gen = JOLTReportGenerator(
        report_output_folder=f"./{outputfolder}/{DATA_NAMESPACE}",
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
    from report_generator.logger_patcher import LoggerPatcher

    patcher = LoggerPatcher()
    patcher.patch(vehicle_registration, date_start, date_end, debug=debug)
