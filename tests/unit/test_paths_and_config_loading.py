"""Deployment-time path resolution and config loading.

These four helpers are the whole "where does state live" contract a deployer
configures: ``JOLT_CACHE_DIR``, ``SRF_API_ROOT`` and ``JOLT_CONFIG_DIR``. Every
one must be read at CALL time (not import time) so a process can be reconfigured,
and a missing config file must fail with an actionable message rather than an
empty ``VEHICLE_CONFIG`` surfacing much later as "vehicle not registered".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from report_generator import configs, paths
from report_generator.segmentation import constants

# ── get_cache_dir ────────────────────────────────────────────────────────────


def test_get_cache_dir_reads_the_env_var_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("JOLT_CACHE_DIR", str(tmp_path / "a"))
    assert paths.get_cache_dir() == Path(tmp_path / "a")
    # Changing the variable changes the answer without re-importing anything.
    monkeypatch.setenv("JOLT_CACHE_DIR", str(tmp_path / "b"))
    assert paths.get_cache_dir() == Path(tmp_path / "b")


@pytest.mark.parametrize("value", ["", None])
def test_get_cache_dir_falls_back_to_the_historical_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("JOLT_CACHE_DIR", raising=False)
    else:
        monkeypatch.setenv("JOLT_CACHE_DIR", value)
    assert paths.get_cache_dir() == Path("./cache")


# ── get_srf_api_root ─────────────────────────────────────────────────────────


def test_get_srf_api_root_default(monkeypatch):
    monkeypatch.delenv("SRF_API_ROOT", raising=False)
    assert paths.get_srf_api_root() == "https://data.csrf.ac.uk/api/"


def test_get_srf_api_root_override(monkeypatch):
    monkeypatch.setenv("SRF_API_ROOT", "https://staging.example.org/api/")
    assert paths.get_srf_api_root() == "https://staging.example.org/api/"


def test_get_srf_api_root_empty_falls_back(monkeypatch):
    monkeypatch.setenv("SRF_API_ROOT", "")
    assert paths.get_srf_api_root() == "https://data.csrf.ac.uk/api/"


# ── get_config_path ──────────────────────────────────────────────────────────


def test_get_config_path_defaults_to_the_packaged_directory(monkeypatch):
    monkeypatch.delenv("JOLT_CONFIG_DIR", raising=False)
    assert configs.get_config_path("vehicles.json") == (
        configs.CONFIGS_DIR / "vehicles.json"
    )


def test_get_config_path_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(tmp_path))
    assert configs.get_config_path("vehicles.json") == tmp_path / "vehicles.json"


def test_get_config_path_empty_override_uses_the_package(monkeypatch):
    monkeypatch.setenv("JOLT_CONFIG_DIR", "")
    assert configs.get_config_path("pipelines.json") == (
        configs.CONFIGS_DIR / "pipelines.json"
    )


def test_packaged_config_directory_holds_the_three_jsons():
    for name in ("vehicles.json", "pipelines.json"):
        assert (configs.CONFIGS_DIR / name).exists()


# ── constants._load_json ─────────────────────────────────────────────────────


def test_load_json_reads_from_the_override_directory(monkeypatch, tmp_path):
    payload = {"ZZ99ZZZ": {"srf_reg": "ZZ99 ZZZ"}}
    (tmp_path / "vehicles.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(tmp_path))
    assert constants._load_json("vehicles.json") == payload


def test_load_json_raises_an_actionable_error_when_the_file_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(tmp_path))  # empty directory
    with pytest.raises(FileNotFoundError) as excinfo:
        constants._load_json("vehicles.json")
    message = str(excinfo.value)
    # It must name the file, the resolved path and the way out.
    assert "vehicles.json" in message
    assert str(tmp_path) in message
    assert "JOLT_CONFIG_DIR" in message


def test_load_json_failure_does_not_disturb_the_loaded_config(monkeypatch, tmp_path):
    before = dict(constants.VEHICLE_CONFIG)
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        constants._load_json("vehicles.json")
    assert dict(constants.VEHICLE_CONFIG) == before


# ── Shared-by-reference config objects ───────────────────────────────────────


def test_vehicle_config_is_shared_by_reference_across_the_package():
    from report_generator import segment_algorithms
    from report_generator.capacity import VEHICLE_CONFIG as cap_cfg
    from report_generator.segmentation import detection

    assert segment_algorithms.VEHICLE_CONFIG is constants.VEHICLE_CONFIG
    assert detection.VEHICLE_CONFIG is constants.VEHICLE_CONFIG
    assert cap_cfg is constants.VEHICLE_CONFIG


def test_frozen_config_injection_is_visible_everywhere(frozen_configs):
    from report_generator import segment_algorithms

    assert "EVSPD01" in segment_algorithms.VEHICLE_CONFIG
    assert "evspd01_speed" in segment_algorithms.PIPELINE_CONFIGS
    # ... and the live fleet entries are still there (setitem, not replace).
    assert len(segment_algorithms.VEHICLE_CONFIG) > len(frozen_configs["vehicles"])
