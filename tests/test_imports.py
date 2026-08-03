"""Import contract for the v3.0.0 architecture.

The v3.0.0 refactor split the two monoliths (``segment_algorithms.py`` and
``report_builder.py``) into sub-packages while keeping the original module paths
as facades. This test locks in the §0.2 compatibility surface: every core module
and facade must import, every historically-importable public/private name must
still resolve on its ORIGINAL import path, and the single shared ``VEHICLE_CONFIG``
object must be identical across the modules that load / re-export it.

No network / no SRF key: importing a module never issues an API request (the SRF
client is only built inside ``JOLTReportGenerator.__init__``), so this is offline.
"""

import importlib

import pytest

# ── Core modules + facades that must import cleanly ──────────────────────────
CORE_MODULES = [
    "jolt_toolkit",
    "jolt_toolkit.configs",
    "jolt_toolkit.analysis",
    "jolt_toolkit.analysis.counters",
    "jolt_toolkit.analysis.physics",
    "jolt_toolkit.analysis.stats",
    "jolt_toolkit.report_generator",
    "jolt_toolkit.report_generator._generator",
    "jolt_toolkit.report_generator.capacity",
    "jolt_toolkit.report_generator.capacity_backfill",
    "jolt_toolkit.report_generator.charger_patcher",
    "jolt_toolkit.report_generator.logger_patcher",
    "jolt_toolkit.report_generator.weather_patcher",
    "jolt_toolkit.report_generator.weather_patch",
    "jolt_toolkit.report_generator.diesel_pipeline",
    "jolt_toolkit.report_generator.data_fetcher",
    "jolt_toolkit.report_generator.data_class",
    "jolt_toolkit.report_generator.operators",
    "jolt_toolkit.report_generator.pedal_histogram",
    "jolt_toolkit.report_generator.paths",
    "jolt_toolkit.report_generator.cli",
    "jolt_toolkit.report_generator.xlsx_patch_common",
    # report_builder split + facade (v3.1.0: html_viewer moved to the
    # report-visuals skill)
    "jolt_toolkit.report_generator.report_builder",
    "jolt_toolkit.report_generator.columns",
    "jolt_toolkit.report_generator.charts",
    "jolt_toolkit.report_generator.row_builder",
    "jolt_toolkit.report_generator.excel_writer",
    # segmentation split + facade (v3.1.0: validation_figure moved to the
    # report-visuals skill)
    "jolt_toolkit.report_generator.segment_algorithms",
    "jolt_toolkit.report_generator.segmentation",
    "jolt_toolkit.report_generator.segmentation.constants",
    "jolt_toolkit.report_generator.segmentation.timeutil",
    "jolt_toolkit.report_generator.segmentation.mass_aggregation",
    "jolt_toolkit.report_generator.segmentation.soc_detection",
    "jolt_toolkit.report_generator.segmentation.speed_detection",
    "jolt_toolkit.report_generator.segmentation.mass_clustering",
    "jolt_toolkit.report_generator.segmentation.detection",
    # weather infra
    "jolt_toolkit.report_generator.weather_fetcher",
    "jolt_toolkit.report_generator.weather_fetcher.openweather",
    "jolt_toolkit.report_generator.weather_fetcher.fine_grained_patcher",
]

# ── §0.2 compatibility surface: name -> original import module ───────────────
# These names are imported by external consumers (skills, finetune, dashboards,
# recompute, patchers). They must keep resolving on the facade module path.
SEGMENT_ALGORITHMS_NAMES = [
    "run_segment_detection",
    "resolve_mass_agg",
    "cluster_mass_data",
    "find_speed_trips",
    "find_discharge_segments_by_speed",
    "find_charge_segments_by_soc",
    "find_discharge_segments_by_soc",
    "merge_discharge_by_mass",
    "split_discharge_by_mass",
    "_agg_mass",
    "_ANCHOR_PRIVATE_KEYS",
    "_recompute_anchors",
    "_enforce_anchor_ordering",
    "_detect_cluster_transitions",
    "_to_utc",
    "VEHICLE_CONFIG",
    "PIPELINE_CONFIGS",
    # column-name constants (the *_COL surface)
    "TIME_COL",
    "SOC_COL",
    "MASS_COL",
    "MOVING_COL",
    "RECUP_COL",
    "AC_COL",
    "DC_COL",
    "TOTAL_ENERGY_COL",
    "MOVING_SPEED_THRESHOLD_KMH",
]

REPORT_BUILDER_NAMES = [
    "HEADERS",
    "DIESEL_HEADERS",
    "is_trip_leg",
    "_row_col_index",
    "_is_nan",
    "_leg_is_charge",
    "_leg_is_stop",
    "_seg_to_row",
    "_insert_stop_rows",
    "_stop_row_from_neighbours",
    "_write_excel_report",
    "_write_na",
    "_find_overlap",
    "_build_logger_url",
    "_build_telematics_url",
    "_build_charger_url",
    "_kinetics_corrected_energy_perf",
    "_get_trip_speed_array",
    "_get_postcode",
    "CHART_SPECS_EV",
    "CHART_SPECS_DIESEL",
    "chart_specs_for",
    # telematics col-name constants re-exported from columns
    "_WEIGHT_COL",
    "_SPEED_COL",
    "_RECUP_COL",
    "_PROPULSION_COL",
]

# capacity names re-exported for capacity_backfill / recompute back-compat
CAPACITY_NAMES = [
    "_correct_effective_capacity",
    "_persist_effective_capacity",
    "_period_capacity_from_rows",
    "_recompute_weighted_capacity",
    "_cap_is_valid",
    "_soc_weighted_cap",
    "_IDX_CAP",
    "MIN_DONORS",
    "CAP_WINDOW_HALF_DAYS",
]


@pytest.mark.parametrize("modname", CORE_MODULES)
def test_module_imports(modname):
    assert importlib.import_module(modname) is not None


@pytest.mark.parametrize("name", SEGMENT_ALGORITHMS_NAMES)
def test_segment_algorithms_facade_name(name):
    mod = importlib.import_module("jolt_toolkit.report_generator.segment_algorithms")
    assert hasattr(mod, name), f"segment_algorithms.{name} no longer resolves"


@pytest.mark.parametrize("name", REPORT_BUILDER_NAMES)
def test_report_builder_facade_name(name):
    mod = importlib.import_module("jolt_toolkit.report_generator.report_builder")
    assert hasattr(mod, name), f"report_builder.{name} no longer resolves"


@pytest.mark.parametrize("name", CAPACITY_NAMES)
def test_capacity_backcompat_name(name):
    # Importable both from capacity (new home) and _generator (re-export).
    cap = importlib.import_module("jolt_toolkit.report_generator.capacity")
    gen = importlib.import_module("jolt_toolkit.report_generator._generator")
    assert hasattr(cap, name), f"capacity.{name} missing"
    assert hasattr(gen, name), f"_generator re-export of {name} missing"


def test_correct_effective_capacity_staticmethod_identity():
    """recompute_from_cache calls JOLTReportGenerator._correct_effective_capacity;
    it must be the same object as the capacity module function."""
    cap = importlib.import_module("jolt_toolkit.report_generator.capacity")
    gen = importlib.import_module("jolt_toolkit.report_generator._generator")
    assert (
        gen.JOLTReportGenerator._correct_effective_capacity
        is cap._correct_effective_capacity
    )
    assert (
        gen.JOLTReportGenerator._persist_effective_capacity
        is cap._persist_effective_capacity
    )


def test_vehicle_config_object_identity():
    """VEHICLE_CONFIG / PIPELINE_CONFIGS are loaded once and shared by reference
    across the facade, the segmentation constants module, and report_builder."""
    sa = importlib.import_module("jolt_toolkit.report_generator.segment_algorithms")
    rb = importlib.import_module("jolt_toolkit.report_generator.report_builder")
    sc = importlib.import_module("jolt_toolkit.report_generator.segmentation.constants")
    assert sa.VEHICLE_CONFIG is sc.VEHICLE_CONFIG
    assert sa.VEHICLE_CONFIG is rb.VEHICLE_CONFIG
    assert sa.PIPELINE_CONFIGS is sc.PIPELINE_CONFIGS


# ── v3.1.0 slim: the rendering surface left the package ──────────────────────
# The validation-figure painter (+ overlay helpers, style constants) and the
# inspect-HTML viewer moved to the report-visuals skill together with matplotlib.
# These names must NO LONGER resolve on their former facade paths.
SEGMENT_ALGORITHMS_REMOVED = [
    "plot_leg_validation",
    "_export_overlay_boxes",
    "_build_energy_series",
    "_overlay",
    "_mark_anchors_stored",
    "_annotate_overlay_energy_delta",
    "_parse_box_gid",
    "_HAS_MPL",
    "_TEXT_BBOX",
    "_CHARGE_COLOR",
    "_DISCHARGE_COLOR",
    "_FIGURE_SIZE",
    "_DPI",
    "_LABEL_FONT",
    "_TICK_FONT",
    "_LEGEND_FONT",
    "_DSOC_FONT",
    "_DATE_FMT",
]

REPORT_BUILDER_REMOVED = [
    "_write_html_viewer",
    "_compute_active_dates_from_xlsx",
    "_group_paths_by_date",
    "_clear_day_validation_figures",
]

# Diesel data-processing surface the skills import (rendering half removed).
DIESEL_KEPT_NAMES = [
    "process_diesel_leg",
    "_finalise_logger_df",
    "_segments_from_df",
    "_build_logger_df",
    "_logger_df_from_csv",
    "_trip_metrics",
    "_diesel_seg_to_row",
    "DEFAULT_SPEED_COL",
    "DEFAULT_FUEL_COL",
    "DEFAULT_DISTANCE_COL",
    "DEFAULT_MASS_COL",
    "DEFAULT_ALTITUDE_COL",
    "DEFAULT_AMBIENT_TEMP_COL",
    "DEFAULT_DIESEL_LHV_KWH_PER_L",
    "DEFAULT_MIN_TRIP_DISTANCE_KM",
]

DIESEL_REMOVED_NAMES = [
    "plot_diesel_leg_validation",
    "regenerate_diesel_validation",
    "_logger_day_df_from_csvs",
    "_XLSX_RE",
    "_LOGGER_DATE_RE",
]


@pytest.mark.parametrize("name", SEGMENT_ALGORITHMS_REMOVED)
def test_segment_algorithms_removed_surface(name):
    sa = importlib.import_module("jolt_toolkit.report_generator.segment_algorithms")
    seg = importlib.import_module("jolt_toolkit.report_generator.segmentation")
    assert not hasattr(sa, name), f"segment_algorithms.{name} should be gone in v3.1.0"
    assert not hasattr(seg, name), f"segmentation.{name} should be gone in v3.1.0"


@pytest.mark.parametrize("name", REPORT_BUILDER_REMOVED)
def test_report_builder_removed_surface(name):
    rb = importlib.import_module("jolt_toolkit.report_generator.report_builder")
    assert not hasattr(rb, name), f"report_builder.{name} should be gone in v3.1.0"


@pytest.mark.parametrize("name", DIESEL_KEPT_NAMES)
def test_diesel_pipeline_kept_name(name):
    mod = importlib.import_module("jolt_toolkit.report_generator.diesel_pipeline")
    assert hasattr(mod, name), f"diesel_pipeline.{name} must stay (skills import it)"


@pytest.mark.parametrize("name", DIESEL_REMOVED_NAMES)
def test_diesel_pipeline_removed_name(name):
    mod = importlib.import_module("jolt_toolkit.report_generator.diesel_pipeline")
    assert not hasattr(mod, name), f"diesel_pipeline.{name} should be gone in v3.1.0"


def test_run_segment_detection_accepts_figure_hook():
    """The v3.1.0 figure-painting seam: run_segment_detection must expose a
    keyword-only ``figure_hook`` parameter (the report-visuals skill passes its
    own painter here)."""
    import inspect

    sa = importlib.import_module("jolt_toolkit.report_generator.segment_algorithms")
    sig = inspect.signature(sa.run_segment_detection)
    assert "figure_hook" in sig.parameters
    p = sig.parameters["figure_hook"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is None


def test_package_import_does_not_require_matplotlib():
    """v3.1.0 platform contract: importing the report-generation package must NOT
    pull in matplotlib (it left with the rendering code). Run in a fresh
    subprocess so an unrelated test having already imported matplotlib cannot mask
    a regression."""
    import os
    import subprocess
    import sys

    code = (
        "import sys; "
        "import jolt_toolkit.report_generator._generator; "
        "assert 'matplotlib' not in sys.modules, "
        "'importing the package pulled in matplotlib'"
    )
    # Propagate the parent's resolved sys.path so the subprocess imports the SAME
    # jolt_toolkit as this test session (pytest prepends the worktree ``src`` via
    # the pyproject ``pythonpath`` option, which is not otherwise inherited).
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"matplotlib leaked into the package import:\n{result.stdout}\n{result.stderr}"
    )
