"""
report_builder.py
==================
Convert the charge/discharge segment dictionaries produced by
segment_algorithms into Excel report rows, and write a formatted Excel file.

--------------------------------------------------------------------------------
Facade note
--------------------------------------------------------------------------------
This module was split into cohesive sub-modules; it now re-exports
the names that stay in the package so existing ``from ...report_builder import X``
call sites keep working unchanged:

    columns.py       — HEADERS / DIESEL_HEADERS, leg-type predicates, _row_col_index,
                       _is_nan, telematics source-column name constants.
    charts.py        — CHART_STYLE, CHART_SPECS_EV/DIESEL, chart geometry helpers,
                       _chart_subtitle, chart_specs_for, _filtered_chart_points.
    row_builder.py   — URL builders, per-metric telematics helpers, postcode cache,
                       home/leg-type classification, Stop-row synthesis, _seg_to_row.
    excel_writer.py  — _write_na, _write_excel_report (+ per-sheet block helpers).

Now owned by the report-visuals skill
---------------------------------------------------------
The inspect-HTML viewer left the package with the validation-figure rendering.
These names are NO LONGER importable from this facade: ``_write_html_viewer``,
``_compute_active_dates_from_xlsx``, ``_group_paths_by_date``,
``_clear_day_validation_figures`` (the viewer + per-day figure bookkeeping and
the ``assets/inspect_viewer_template.html`` template moved to the report-visuals
skill). Importing this module NO LONGER imports matplotlib or sets the ``Agg``
backend. New code should import from the sub-modules directly; this facade is
kept for backward compatibility.
"""

from __future__ import annotations

import logging

from jolt_toolkit.report_generator.charts import (  # noqa: F401
    _EMU_PER_CM,
    _EMU_PER_DEFAULT_COL,
    _EMU_PER_DEFAULT_ROW,
    CHART_SPECS_DIESEL,
    CHART_SPECS_EV,
    CHART_STYLE,
    _chart_height_rows,
    _chart_subtitle,
    _filtered_chart_points,
    chart_col_span,
    chart_row_step,
    chart_specs_for,
    empty_chart_note,
    empty_note_extent,
)

# ── Re-exports (backward-compatibility facade) ──────────────────────────────
from jolt_toolkit.report_generator.columns import (  # noqa: F401
    _CHARGE_LEG_RE,
    _PROPULSION_COL,
    _RECUP_COL,
    _SPEED_COL,
    _WEIGHT_COL,
    DIESEL_HEADERS,
    HEADERS,
    _is_nan,
    _leg_is_charge,
    _leg_is_stop,
    _row_col_index,
    is_trip_leg,
)
from jolt_toolkit.report_generator.excel_writer import (  # noqa: F401
    _write_definitions_sheet,
    _write_excel_report,
    _write_graphs_sheet,
    _write_na,
    _write_report_sheet,
)
from jolt_toolkit.report_generator.paths import get_cache_dir  # noqa: F401
from jolt_toolkit.report_generator.pedal_histogram import (  # noqa: F401
    EBC1_COL,
    EEC2_COL,
    MIN_DISTANCE_FOR_PEDAL_KM,
    compute_pedal_histogram,
)
from jolt_toolkit.report_generator.row_builder import (  # noqa: F401
    _G,
    _POSTCODE_CACHE_PATH,
    _V_MAX_KMH,
    ETA_DT,
    ETA_REGEN,
    HOME_DETECTION_KM,
    ROUND_TRIP_MIN_KM,
    STOP_MIN_GAP_SECONDS,
    _build_charger_url,
    _build_logger_url,
    _build_telematics_url,
    _corrected_energy_perf,
    _ep_exclude_aux,
    _find_overlap,
    _get_elevation_diff,
    _get_leg_type,
    _get_postcode,
    _get_propulsion_energy,
    _get_recuperation,
    _get_trip_speed_array,
    _get_vehicle_mass,
    _insert_stop_rows,
    _is_home,
    _kinetics_corrected_energy_perf,
    _load_postcode_cache,
    _point_str,
    _postcode_cache,
    _propulsion_at,
    _save_postcode_cache,
    _seg_mask,
    _seg_to_row,
    _stop_row_from_neighbours,
    _to_utc_series,
    _ts_iso,
)

# Domain names historically importable from report_builder (re-exported from
# their owning modules, exactly as before the split).
from jolt_toolkit.report_generator.segment_algorithms import (  # noqa: F401
    _ANCHOR_PRIVATE_KEYS,
    MOVING_SPEED_THRESHOLD_KMH,
    SOC_COL,
    TIME_COL,
    VEHICLE_CONFIG,
    _agg_mass,
    run_segment_detection,
)

logger = logging.getLogger(__name__)

__all__ = [
    # columns
    "HEADERS",
    "DIESEL_HEADERS",
    "_CHARGE_LEG_RE",
    "_leg_is_charge",
    "_leg_is_stop",
    "is_trip_leg",
    "_WEIGHT_COL",
    "_SPEED_COL",
    "_RECUP_COL",
    "_PROPULSION_COL",
    "_row_col_index",
    "_is_nan",
    # charts
    "CHART_STYLE",
    "_EMU_PER_CM",
    "_EMU_PER_DEFAULT_ROW",
    "_EMU_PER_DEFAULT_COL",
    "_chart_height_rows",
    "chart_col_span",
    "chart_row_step",
    "empty_chart_note",
    "empty_note_extent",
    "_chart_subtitle",
    "CHART_SPECS_EV",
    "CHART_SPECS_DIESEL",
    "chart_specs_for",
    "_filtered_chart_points",
    # row_builder
    "_ts_iso",
    "_build_telematics_url",
    "_build_charger_url",
    "_build_logger_url",
    "_find_overlap",
    "_to_utc_series",
    "_seg_mask",
    "_get_vehicle_mass",
    "_get_recuperation",
    "_propulsion_at",
    "_get_propulsion_energy",
    "_ep_exclude_aux",
    "_get_elevation_diff",
    "_G",
    "_corrected_energy_perf",
    "ETA_DT",
    "ETA_REGEN",
    "_V_MAX_KMH",
    "_kinetics_corrected_energy_perf",
    "_get_trip_speed_array",
    "_point_str",
    "_POSTCODE_CACHE_PATH",
    "_load_postcode_cache",
    "_save_postcode_cache",
    "_postcode_cache",
    "_get_postcode",
    "HOME_DETECTION_KM",
    "ROUND_TRIP_MIN_KM",
    "_is_home",
    "_get_leg_type",
    "STOP_MIN_GAP_SECONDS",
    "_stop_row_from_neighbours",
    "_insert_stop_rows",
    "_seg_to_row",
    # excel_writer
    "_write_na",
    "_write_excel_report",
    "_write_report_sheet",
    "_write_graphs_sheet",
    "_write_definitions_sheet",
    # re-exported domain names
    "run_segment_detection",
    "SOC_COL",
    "TIME_COL",
    "MOVING_SPEED_THRESHOLD_KMH",
    "_ANCHOR_PRIVATE_KEYS",
    "_agg_mass",
    "VEHICLE_CONFIG",
    "compute_pedal_histogram",
    "MIN_DISTANCE_FOR_PEDAL_KM",
    "EEC2_COL",
    "EBC1_COL",
    "get_cache_dir",
    "logger",
]
