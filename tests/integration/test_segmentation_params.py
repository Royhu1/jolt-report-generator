"""``run_segment_detection``'s parameter-merge contract and branch dispatch.

The orchestrator merges four sources into the kwargs each detector receives:
pipeline defaults < caller overrides, with ``cap_lo`` / ``cap_hi`` applied by
``setdefault`` (so a caller override survives) and the COLUMN NAMES force-written
from the vehicle config (so a stale value in a params dict can never point the
detector at the wrong telematics column).

The detectors themselves are replaced by recorders, so these tests observe the
merge directly instead of inferring it from segmentation output.
"""

from __future__ import annotations

import pandas as pd
import pytest

from jolt_toolkit.report_generator.segmentation import constants, detection


@pytest.fixture
def recorded_calls(monkeypatch):
    """Replace the three detectors with recorders that return no segments."""
    calls: dict[str, dict] = {}

    def _recorder(name):
        def _fn(df, **kwargs):
            calls[name] = kwargs
            return []

        return _fn

    monkeypatch.setattr(detection, "find_charge_segments_by_soc", _recorder("charge"))
    monkeypatch.setattr(
        detection, "find_discharge_segments_by_soc", _recorder("discharge_soc")
    )
    monkeypatch.setattr(
        detection, "find_discharge_segments_by_speed", _recorder("discharge_speed")
    )
    return calls


@pytest.fixture
def tiny_frame():
    """A minimal frame: the detectors are mocked, so only the columns matter."""
    return pd.DataFrame(
        {
            "eventDatetime": ["2025-06-27T08:00:00Z", "2025-06-27T09:00:00Z"],
            "electricBatteryLevelPercent": ["90", "80"],
            "wheel_based_speed": ["0", "50"],
        }
    )


def _run(frame, alias, **kwargs):
    return detection.run_segment_detection(
        frame,
        reg=alias,
        suffix="params",
        out_dir=None,
        generate_validation_fig=False,
        **kwargs,
    )


# ── Pipeline defaults reach the detectors ────────────────────────────────────


def test_pipeline_defaults_are_passed_through(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSOC01")
    pipeline = frozen_configs["pipelines"]["evsoc01_soc"]
    charge = recorded_calls["charge"]
    discharge = recorded_calls["discharge_soc"]
    for key, value in pipeline["charge_params"].items():
        assert charge[key] == value
    for key, value in pipeline["discharge_params"].items():
        assert discharge[key] == value


def test_caller_charge_params_beat_the_pipeline(
    frozen_configs, tiny_frame, recorded_calls
):
    assert (
        frozen_configs["pipelines"]["evsoc01_soc"]["charge_params"]["min_soc_rise"]
        == 5.0
    )
    _run(tiny_frame, "EVSOC01", charge_params={"min_soc_rise": 42.0})
    assert recorded_calls["charge"]["min_soc_rise"] == 42.0


def test_caller_discharge_params_beat_the_pipeline(
    frozen_configs, tiny_frame, recorded_calls
):
    assert (
        frozen_configs["pipelines"]["evsoc01_soc"]["discharge_params"][
            "plateau_window_min"
        ]
        == 20
    )
    _run(tiny_frame, "EVSOC01", discharge_params={"plateau_window_min": 99})
    assert recorded_calls["discharge_soc"]["plateau_window_min"] == 99


def test_caller_params_can_add_a_key_the_pipeline_does_not_set(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSOC01", charge_params={"min_energy_kwh": 1.0})
    assert recorded_calls["charge"]["min_energy_kwh"] == 1.0


# ── cap_lo / cap_hi use setdefault ───────────────────────────────────────────


def test_cap_bounds_are_injected_when_the_params_do_not_carry_them(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSOC01", cap_lo=300.0, cap_hi=1200.0)
    for call in (recorded_calls["charge"], recorded_calls["discharge_soc"]):
        assert call["cap_lo"] == 300.0
        assert call["cap_hi"] == 1200.0


def test_cap_bounds_are_setdefault_so_an_explicit_param_wins(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(
        tiny_frame,
        "EVSOC01",
        charge_params={"cap_lo": 111.0, "cap_hi": 222.0},
        cap_lo=300.0,
        cap_hi=1200.0,
    )
    assert recorded_calls["charge"]["cap_lo"] == 111.0
    assert recorded_calls["charge"]["cap_hi"] == 222.0
    # The discharge side, which supplied no override, still gets the injection.
    assert recorded_calls["discharge_soc"]["cap_lo"] == 300.0


def test_no_cap_bounds_when_none_are_supplied(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSOC01")
    assert "cap_lo" not in recorded_calls["charge"]
    assert "cap_hi" not in recorded_calls["discharge_soc"]


# ── Column names are force-overwritten ───────────────────────────────────────


def test_column_names_are_force_overwritten_from_the_vehicle_config(
    frozen_configs, tiny_frame, recorded_calls
):
    cfg = frozen_configs["vehicles"]["EVSOC01"]
    _run(
        tiny_frame,
        "EVSOC01",
        charge_params={
            "ac_col": "stale_ac",
            "dc_col": "stale_dc",
            "moving_energy_col": "stale_mov",
        },
        discharge_params={
            "total_energy_col": "stale_total",
            "moving_energy_col": "stale_mov",
        },
    )
    charge = recorded_calls["charge"]
    discharge = recorded_calls["discharge_soc"]
    assert charge["ac_col"] == cfg["ac_col"]
    assert charge["dc_col"] == cfg["dc_col"]
    assert charge["moving_energy_col"] == cfg["moving_energy_col"]
    assert discharge["total_energy_col"] == cfg["total_energy_col"]
    assert discharge["moving_energy_col"] == cfg["moving_energy_col"]


def test_soc_estimate_capacity_seed_prefers_the_effective_capacity(
    frozen_configs, tiny_frame, recorded_calls
):
    cfg = frozen_configs["vehicles"]["EVSOC01"]
    _run(tiny_frame, "EVSOC01")
    # effective > srf > nominal.
    assert recorded_calls["charge"]["nominal_kwh"] == cfg["effective_capacity_kwh"]
    assert (
        recorded_calls["discharge_soc"]["nominal_kwh"] == cfg["effective_capacity_kwh"]
    )


def test_soc_estimate_capacity_seed_is_a_setdefault(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSOC01", charge_params={"nominal_kwh": 123.0})
    assert recorded_calls["charge"]["nominal_kwh"] == 123.0


def test_min_trip_distance_km_is_injected_from_the_pipeline_top_level(
    frozen_configs, tiny_frame, recorded_calls
):
    # evsoc01_soc carries min_trip_distance_km: 10.0 at the pipeline top level.
    assert frozen_configs["pipelines"]["evsoc01_soc"]["min_trip_distance_km"] == 10.0
    _run(tiny_frame, "EVSOC01")
    assert recorded_calls["discharge_soc"]["min_trip_distance_km"] == 10.0


def test_min_trip_distance_km_is_absent_when_the_pipeline_omits_it(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSPD01")  # speed branch pipeline, no top-level distance
    assert "min_trip_distance_km" not in recorded_calls["discharge_speed"]


# ── Speed branch ─────────────────────────────────────────────────────────────


def test_speed_branch_calls_the_speed_detector_and_still_detects_charges(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVSPD01")
    assert "discharge_speed" in recorded_calls
    assert "charge" in recorded_calls


def test_speed_branch_injects_the_speed_column_and_anchor_settings(
    frozen_configs, tiny_frame, recorded_calls
):
    _run(tiny_frame, "EVMAD01")
    call = recorded_calls["discharge_speed"]
    cfg = frozen_configs["vehicles"]["EVMAD01"]
    pipeline = frozen_configs["pipelines"]["evmad01_speed"]
    assert call["speed_col"] == cfg["speed_col"]
    assert call["total_energy_col"] == cfg["total_energy_col"]
    assert call["trip_endpoint_anchor"] == pipeline["trip_endpoint_anchor"]
    assert call["max_extend_minutes"] == pipeline["max_extend_minutes"]


def test_speed_branch_defaults_to_the_zero_speed_anchor(
    frozen_configs, tiny_frame, recorded_calls
):
    # evspd01_speed declares no trip_endpoint_anchor -> the fleet-wide default.
    assert "trip_endpoint_anchor" not in frozen_configs["pipelines"]["evspd01_speed"]
    _run(tiny_frame, "EVSPD01")
    assert recorded_calls["discharge_speed"]["trip_endpoint_anchor"] == "zero_speed"
    assert recorded_calls["discharge_speed"]["max_extend_minutes"] == 5.0


def test_speed_branch_falls_back_to_soc_when_no_trip_is_found(
    frozen_configs, tiny_frame, recorded_calls
):
    # The speed recorder returns [] -> the SOC discharge detector must run too.
    _run(tiny_frame, "EVSPD01")
    assert "discharge_soc" in recorded_calls


def test_per_vehicle_min_stop_duration_override_reaches_the_speed_detector(
    monkeypatch, frozen_configs, tiny_frame, recorded_calls
):
    cfg = dict(frozen_configs["vehicles"]["EVSPD01"], min_stop_duration_min=17)
    monkeypatch.setitem(constants.VEHICLE_CONFIG, "EVSPD01_LONGSTOP", cfg)
    _run(tiny_frame, "EVSPD01_LONGSTOP")
    assert recorded_calls["discharge_speed"]["min_stop_duration_min"] == 17.0


# ── Branch dispatch ──────────────────────────────────────────────────────────


def test_unknown_branch_raises_value_error(monkeypatch, frozen_configs, tiny_frame):
    monkeypatch.setitem(
        constants.PIPELINE_CONFIGS, "ut_bogus_branch", {"branch": "quantum"}
    )
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        "UTBOGUS1",
        {"srf_reg": "UTBOGUS1", "pipeline": "ut_bogus_branch"},
    )
    with pytest.raises(ValueError) as excinfo:
        _run(tiny_frame, "UTBOGUS1")
    message = str(excinfo.value)
    assert "quantum" in message
    assert "ut_bogus_branch" in message
    assert "UTBOGUS1" in message
    assert "soc, speed" in message


def test_missing_branch_key_defaults_to_soc(
    monkeypatch, frozen_configs, tiny_frame, recorded_calls
):
    monkeypatch.setitem(constants.PIPELINE_CONFIGS, "ut_no_branch", {})
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        "UTNOBRANCH",
        {"srf_reg": "UTNOBRANCH", "pipeline": "ut_no_branch"},
    )
    _run(tiny_frame, "UTNOBRANCH")
    assert "discharge_soc" in recorded_calls
    assert "discharge_speed" not in recorded_calls


def test_unknown_pipeline_name_falls_back_to_default_soc(
    monkeypatch, tiny_frame, recorded_calls
):
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        "UTNOPIPE",
        {"srf_reg": "UTNOPIPE", "pipeline": "no_such_pipeline"},
    )
    _run(tiny_frame, "UTNOPIPE")
    assert "discharge_soc" in recorded_calls


def test_unregistered_registration_uses_the_default_soc_pipeline(
    tiny_frame, recorded_calls
):
    _run(tiny_frame, "NOT_IN_ANY_CONFIG")
    assert "discharge_soc" in recorded_calls
    # Column names fall back to the package defaults.
    assert recorded_calls["charge"]["ac_col"] == constants.AC_COL
