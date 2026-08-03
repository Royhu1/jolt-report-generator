"""
Segmentation shared constants and configuration loading.

Raw-telemetry column-name constants (overridable per vehicle via
``VEHICLE_CONFIG``), the mass-clustering default thresholds, the private
anchor-key set, and the SINGLE load site of ``VEHICLE_CONFIG`` /
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

# ── Config loading (from JSON files) ─────────────────────────────────────────
import json as _json

from jolt_toolkit.configs import get_config_path as _get_config_path


def _load_json(name: str) -> dict:
    """Load a JSON config file from the active config directory.

    Raises ``FileNotFoundError`` with an actionable message when the file is
    missing (previously returned ``{}`` silently, which surfaced much later as
    an empty ``VEHICLE_CONFIG`` and a cryptic 'vehicle not registered' error).
    """
    path = _get_config_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file '{name}' not found at {path}. If jolt_toolkit was "
            f"installed as a wheel, ensure the configs are packaged "
            f"(pyproject 'jolt_toolkit.configs' package-data), or set the "
            f"JOLT_CONFIG_DIR environment variable to a directory containing "
            f"vehicles.json / pipelines.json / plot_config.json."
        )
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


VEHICLE_CONFIG: dict[str, dict[str, Any]] = _load_json("vehicles.json")
PIPELINE_CONFIGS: dict[str, dict] = _load_json("pipelines.json")
