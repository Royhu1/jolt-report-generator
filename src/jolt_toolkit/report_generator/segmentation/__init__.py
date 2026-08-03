"""
segmentation — charge/discharge segment-detection sub-package.

Behaviour-preserving decomposition of the former monolithic
``segment_algorithms.py``. Every public and internally-consumed name is
re-exported here so that ``from jolt_toolkit.report_generator.segmentation
import X`` works, and the legacy ``segment_algorithms`` facade module re-exports
the same surface for full backward compatibility.

Module map:
  constants           column-name constants, mass thresholds, the single
                      VEHICLE_CONFIG / PIPELINE_CONFIGS load site
  timeutil            _to_utc timestamp coercion
  mass_aggregation    configurable robust per-segment mass aggregation
  soc_detection       SOC-based charge / discharge segmentation
  speed_detection     speed-based trip / discharge segmentation
  mass_clustering     mass-cluster split / merge + energy-anchor recomputation
  detection           run_segment_detection orchestrator (paints figures only via
                      an external ``figure_hook`` — see the note below)

Now owned by the report-visuals skill
---------------------------------------------------------
The per-leg validation-figure painter and its helpers left the package together
with matplotlib. The following names are NO LONGER importable from
``segmentation`` (or the ``segment_algorithms`` facade); the report-visuals skill
carries them and passes its own painter to ``run_segment_detection(figure_hook=...)``:
``plot_leg_validation``, ``_export_overlay_boxes``, ``_build_energy_series``,
``_overlay``, ``_mark_anchors_stored``, ``_annotate_overlay_energy_delta``,
``_parse_box_gid``, ``_HAS_MPL``, ``_TEXT_BBOX`` and the figure style constants
(``_CHARGE_COLOR`` / ``_DISCHARGE_COLOR`` / ``_FIGURE_SIZE`` / ``_DPI`` /
``_LABEL_FONT`` / ``_TICK_FONT`` / ``_LEGEND_FONT`` / ``_DSOC_FONT`` / ``_DATE_FMT``).
"""

from __future__ import annotations

from .constants import (
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
from .detection import run_segment_detection
from .mass_aggregation import (
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
from .mass_clustering import (
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
from .soc_detection import (
    find_charge_segments_by_soc,
    find_discharge_segments_by_soc,
)
from .speed_detection import (
    _extend_trip_endpoint_to_zero,
    find_discharge_segments_by_speed,
    find_speed_trips,
)
from .timeutil import _to_utc

__all__ = [
    # constants
    "TIME_COL",
    "SOC_COL",
    "AC_COL",
    "DC_COL",
    "ODO_COL",
    "MOVING_COL",
    "TOTAL_ENERGY_COL",
    "MASS_COL",
    "RECUP_COL",
    "MIN_CLUSTER_GAP_KG",
    "TRACTOR_ONLY_MAX_KG",
    "MOVING_SPEED_THRESHOLD_KMH",
    "_ANCHOR_PRIVATE_KEYS",
    "_load_json",
    "VEHICLE_CONFIG",
    "PIPELINE_CONFIGS",
    # timeutil
    "_to_utc",
    # mass_aggregation
    "_MASS_AGG_METHODS",
    "_MAD_K",
    "_TRIM_FRAC",
    "_iqr_inliers",
    "_mad_inliers",
    "_trimmed_inliers",
    "_coerce_seconds",
    "_time_weighted_mean",
    "_mad_tw_value",
    "_agg_mass",
    "resolve_mass_agg",
    # soc_detection
    "find_charge_segments_by_soc",
    "find_discharge_segments_by_soc",
    # speed_detection
    "_extend_trip_endpoint_to_zero",
    "find_speed_trips",
    "find_discharge_segments_by_speed",
    # mass_clustering
    "cluster_mass_data",
    "_get_seg_dominant_cluster",
    "_split_point_has_zero_speed",
    "_detect_cluster_transitions",
    "_split_seg_at_times",
    "split_discharge_by_mass",
    "_recompute_anchors",
    "_merge_two_discharge_segs",
    "merge_discharge_by_mass",
    "_enforce_anchor_ordering",
    # detection
    "run_segment_detection",
]
