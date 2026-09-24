"""
Segmentation shared constants and configuration loading.

Raw-telemetry column-name constants (overridable per vehicle via
``VEHICLE_CONFIG``), the mass-clustering default thresholds, the private
segment keys (the anchor set, the capacity-band marker), and the SINGLE load
site of ``VEHICLE_CONFIG`` /
``PIPELINE_CONFIGS`` (shared by reference across the package — every other
module imports these bindings; do not add a second load site).

Behaviour-preserving split of the former ``segment_algorithms.py``.
"""

from __future__ import annotations

from typing import Any

# ── Raw-telemetry column-name constants (defaults; VEHICLE_CONFIG may override per vehicle) ──
TIME_COL = "eventDatetime"
SOC_COL = "electricBatteryLevelPercent"
AC_COL = "battery_pack_ac_watthours"
DC_COL = "battery_pack_dc_watthours"
ODO_COL = "odometer"
MOVING_COL = "electric_energy_wheelbased_speed_over_zero"
TOTAL_ENERGY_COL = "total_electric_energy_used_plugged_in_included"
MASS_COL = "gross_combination_vehicle_weight"
RECUP_COL = "electric_energy_recuperation_watthours"
# What made the feed send a row: "TIMER" for the periodic readings, an event name
# (ignition, charging status, …) otherwise. Not every feed carries it.
TRIGGER_TYPE_COL = "trigger_type"

# ── Mass-clustering default parameters ──────────────────────────────────────────
MIN_CLUSTER_GAP_KG = 2000.0  # Minimum mass gap between clusters (kg): merge two clusters when their means differ by less than this
TRACTOR_ONLY_MAX_KG = 13000.0  # When cluster 0's mean is below this value it is treated as tractor-only and its mass is ignored
# The J1939 gross-combination-weight is unreliable while stationary
# (load/unload transients / default broadcast) and would contaminate the mass
# clustering. Cluster means are computed only from "moving" readings (speed >
# this threshold, km/h), aligned with each pipeline's speed_threshold_kmh
# convention (default 1.0). A NaN speed is treated as not moving.
MOVING_SPEED_THRESHOLD_KMH = 1.0

# Temporary anchor fields in the segment dict (not written to CSV)
_ANCHOR_PRIVATE_KEYS: frozenset = frozenset(
    {
        "_anchor_start_time",
        "_anchor_end_time",
        "_anchor_start_rel_kwh",
        "_anchor_end_rel_kwh",
    }
)

# Marks a discharge segment kept although its SOC-implied capacity lies outside
# the capacity band (``speed_params.keep_trips_outside_cap_band``), or built by
# the mass split or merge from such a segment. It carries no capacity, and the
# split, the merge and the anchor ordering keep it that way, so it never becomes
# a capacity donor. Only that opt-in path sets it, and ``run_segment_detection``
# removes it once those steps have run.
_CAPACITY_OUTSIDE_BAND_KEY = "_capacity_outside_band"

# ── Config loading (from JSON files) ─────────────────────────────────────────
from report_generator.configs import (
    _load_config_json,
    load_pipeline_configs,
    load_vehicle_configs,
)


def _load_json(name: str) -> dict:
    """Load a JSON config file from the active config directory, as parsed.

    Raises ``FileNotFoundError`` with an actionable message when the file is
    missing, rather than returning ``{}`` and surfacing much later as an empty
    ``VEHICLE_CONFIG`` and a cryptic 'vehicle not registered' error. The raw
    file content: unlike :func:`~report_generator.configs.load_vehicle_configs`
    it applies no capacity-ledger overlay.
    """
    return _load_config_json(name)


# The single load site. ``load_vehicle_configs`` overlays the external capacity
# ledger (``JOLT_CAPACITY_LEDGER``) when one is configured; without it the
# result is exactly the parsed ``vehicles.json``.
VEHICLE_CONFIG: dict[str, dict[str, Any]] = load_vehicle_configs()
PIPELINE_CONFIGS: dict[str, dict] = load_pipeline_configs()
