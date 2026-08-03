"""
segment_algorithms.py
=====================
Unified detection algorithm for charge segments (Volvo / Renault) and discharge
segments (Scania). Per-leg validation figures are painted externally by the
report-visuals skill, which passes a painter via
``run_segment_detection(figure_hook=...)``; this package no longer imports
matplotlib.

Unified output schema (v2)
--------------------------
Both modes output the following key fields:

  delta_soc_pct      : positive = charging (SOC rising); negative = discharging (SOC falling).
  delta_energy_kwh   : positive = charging (energy into the battery); negative = discharging (energy out).
                       Charging source: cumulative AC+DC difference;
                       discharge preferred: total_electric_energy_used_plugged_in_included;
                       fallback: electric_energy_wheelbased_speed_over_zero;
                       last resort: SOC × nominal_kwh estimate.
  energy_source      : 'ac_dc'         — charging, from the AC+DC columns
                       'total_energy'  — discharging, from the total_electric_energy_used_* column
                       'moving_energy' — discharging, from the wheelbased_speed_over_zero column
                       'soc_estimate'  — no energy column available, estimated from SOC × nominal_kwh
  delta_moving_kwh   : cumulative electric_energy_wheelbased_speed_over_zero difference (kWh, >= 0);
                       None when the data is unavailable.

Public functions
----------------
find_charge_segments_by_soc(df_raw, ...)
find_discharge_segments_by_soc(df_raw, ...)
find_speed_trips(df_raw, ...)
find_discharge_segments_by_speed(df_raw, ...)
cluster_mass_data(df_raw, ...)
split_discharge_by_mass(discharge_segs, df_raw, ...)
merge_discharge_by_mass(discharge_segs, df_raw, ...)
run_segment_detection(df_raw, reg, suffix, out_dir,
                      generate_validation_fig=True, ...)

Constants
---------
TIME_COL, SOC_COL, AC_COL, DC_COL, MOVING_COL, ODO_COL, TOTAL_ENERGY_COL

VEHICLE_CONFIG
    Per-vehicle SRF registration name, segmentation mode, nominal capacity,
    manufacturer model and energy-column-name mapping.

_ANCHOR_PRIVATE_KEYS
    Set of temporary field names in the segment dict used only for plotting;
    must be filtered out before saving to CSV.
"""

# =============================================================================
# Backward-compatibility facade
# -----------------------------------------------------------------------------
# The implementation lives in the ``segmentation/`` sub-package (see
# ``segmentation/__init__.py`` for the module map). This module re-exports the
# names that stay in the package so existing consumers (``_generator`` /
# ``report_builder`` / ``diesel_pipeline`` / the patchers / the recompute tool)
# keep working unchanged.
#
# Now owned by the report-visuals skill
# ---------------------------------------------------------
# The validation-figure painter left the package with matplotlib. These names are
# NO LONGER importable from this facade: ``plot_leg_validation``,
# ``_export_overlay_boxes``, ``_build_energy_series``, ``_overlay``,
# ``_mark_anchors_stored``, ``_annotate_overlay_energy_delta``, ``_parse_box_gid``,
# ``_HAS_MPL``, ``_TEXT_BBOX`` and the figure style constants (``_CHARGE_COLOR`` /
# ``_DISCHARGE_COLOR`` / ``_FIGURE_SIZE`` / ``_DPI`` / ``_LABEL_FONT`` /
# ``_TICK_FONT`` / ``_LEGEND_FONT`` / ``_DSOC_FONT`` / ``_DATE_FMT``). The
# report-visuals skill owns them and passes its own painter to
# ``run_segment_detection(figure_hook=...)``. Consequently, importing this module
# NO LONGER imports matplotlib or sets the ``Agg`` backend.
#
# New code should import from ``jolt_toolkit.report_generator.segmentation``
# directly. Prefer explicit re-exports (below) over ``import *`` so the public
# surface stays documented.
# =============================================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────
from jolt_toolkit.report_generator.segmentation.constants import (  # noqa: F401
    _ANCHOR_PRIVATE_KEYS,
    AC_COL,
    DC_COL,
    MASS_COL,
    MIN_CLUSTER_GAP_KG,
    MOVING_COL,
    MOVING_SPEED_THRESHOLD_KMH,
    ODO_COL,
    PIPELINE_CONFIGS,
    RECUP_COL,
    SOC_COL,
    TIME_COL,
    TOTAL_ENERGY_COL,
    TRACTOR_ONLY_MAX_KG,
    VEHICLE_CONFIG,
    _load_json,
)

# ── top-level orchestrator ───────────────────────────────────────────────────
from jolt_toolkit.report_generator.segmentation.detection import (  # noqa: F401
    run_segment_detection,
)

# ── mass aggregation ─────────────────────────────────────────────────────────
from jolt_toolkit.report_generator.segmentation.mass_aggregation import (  # noqa: F401
    _MAD_K,
    _MASS_AGG_METHODS,
    _TRIM_FRAC,
    _agg_mass,
    _coerce_seconds,
    _iqr_inliers,
    _mad_inliers,
    _mad_tw_value,
    _time_weighted_mean,
    _trimmed_inliers,
    resolve_mass_agg,
)

# ── mass clustering / split / merge / anchors ────────────────────────────────
from jolt_toolkit.report_generator.segmentation.mass_clustering import (  # noqa: F401
    _detect_cluster_transitions,
    _enforce_anchor_ordering,
    _get_seg_dominant_cluster,
    _merge_two_discharge_segs,
    _recompute_anchors,
    _split_point_has_zero_speed,
    _split_seg_at_times,
    cluster_mass_data,
    merge_discharge_by_mass,
    split_discharge_by_mass,
)

# ── SOC-based detection ──────────────────────────────────────────────────────
from jolt_toolkit.report_generator.segmentation.soc_detection import (  # noqa: F401
    find_charge_segments_by_soc,
    find_discharge_segments_by_soc,
)

# ── speed-based detection ────────────────────────────────────────────────────
from jolt_toolkit.report_generator.segmentation.speed_detection import (  # noqa: F401
    _extend_trip_endpoint_to_zero,
    find_discharge_segments_by_speed,
    find_speed_trips,
)

# ── timeutil ─────────────────────────────────────────────────────────────────
from jolt_toolkit.report_generator.segmentation.timeutil import (  # noqa: F401
    _to_utc,
)
