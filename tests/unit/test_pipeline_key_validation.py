"""Load-time validation of the pipeline keys with a checked value.

``load_pipeline_configs()`` — and so the import-time load of ``PIPELINE_CONFIGS``
— checks two opt-in keys: ``soc_event_spike_pct`` at the top level of a pipeline
(a positive number of SOC percentage points) and ``keep_trips_outside_cap_band``
in its ``speed_params`` (``true`` / ``false``). A value of the wrong kind, or
either key where it is not read, fails the load with a ``ValueError`` naming the
pipeline and the key. Absent means off, and a pipeline without either key loads
exactly as written.
"""

from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from report_generator import configs

_SPEED = {
    "branch": "speed",
    "charge_params": {"plateau_window_min": 60, "min_soc_rise": 5.0},
    "discharge_params": {"plateau_window_min": 15, "min_soc_drop": 5.0},
    "speed_params": {"speed_threshold_kmh": 1.0, "min_soc_drop": 1.0},
}


def _pipeline(top: dict | None = None, **groups) -> dict:
    """``_SPEED`` with top-level keys added and parameter groups updated."""
    out = copy.deepcopy(_SPEED)
    out.update(top or {})
    for group, extra in groups.items():
        out.setdefault(group, {}).update(extra)
    return out


@pytest.fixture
def load(monkeypatch, tmp_path):
    """Write a pipelines.json holding ``ut_speed`` and load it."""
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)

    def _load(pipeline: dict) -> dict:
        cfg_dir = tmp_path / "configs"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "pipelines.json").write_text(
            json.dumps({"ut_speed": pipeline}), "utf-8"
        )
        monkeypatch.setenv("JOLT_CONFIG_DIR", str(cfg_dir))
        return configs.load_pipeline_configs()

    return _load


# ── Valid values load as written ─────────────────────────────────────────────


@pytest.mark.parametrize("value", [3, 3.0, 0.5, 12], ids=str)
def test_a_positive_spike_threshold_loads(load, value):
    pipeline = _pipeline({"soc_event_spike_pct": value})
    assert load(pipeline) == {"ut_speed": pipeline}


@pytest.mark.parametrize("value", [True, False], ids=str)
def test_the_capacity_band_switch_loads_as_a_flag(load, value):
    pipeline = _pipeline(speed_params={"keep_trips_outside_cap_band": value})
    assert load(pipeline) == {"ut_speed": pipeline}


def test_both_keys_together_load(load):
    pipeline = _pipeline(
        {"soc_event_spike_pct": 3},
        speed_params={"keep_trips_outside_cap_band": True},
    )
    assert load(pipeline)["ut_speed"] == pipeline


def test_a_pipeline_without_either_key_loads_unchanged(load):
    assert load(copy.deepcopy(_SPEED)) == {"ut_speed": _SPEED}


# ── Values of the wrong kind ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [0, -3, "3", True, None, [3], math.nan],
    ids=["zero", "negative", "text", "flag", "null", "list", "nan"],
)
def test_a_spike_threshold_that_is_not_a_positive_number_is_refused(load, value):
    with pytest.raises(
        ValueError,
        match=r"pipelines\.json: ut_speed: soc_event_spike_pct must be a positive "
        r"number of SOC percentage points",
    ):
        load(_pipeline({"soc_event_spike_pct": value}))


@pytest.mark.parametrize(
    "value", ["true", 1, 0, None, "yes"], ids=["text", "one", "zero", "null", "yes"]
)
def test_a_capacity_band_switch_that_is_not_a_flag_is_refused(load, value):
    with pytest.raises(
        ValueError,
        match=r"pipelines\.json: ut_speed: speed_params\.keep_trips_outside_cap_band "
        r"must be true or false",
    ):
        load(_pipeline(speed_params={"keep_trips_outside_cap_band": value}))


# ── Keys where they are not read ─────────────────────────────────────────────


def test_the_capacity_band_switch_at_the_top_level_is_refused(load):
    # It would otherwise be silently ignored: only speed_params reach the detector.
    with pytest.raises(
        ValueError, match="keep_trips_outside_cap_band belongs in speed_params"
    ):
        load(_pipeline({"keep_trips_outside_cap_band": True}))


@pytest.mark.parametrize("group", ["charge_params", "discharge_params"])
def test_the_capacity_band_switch_in_another_group_is_refused(load, group):
    with pytest.raises(
        ValueError,
        match=f"keep_trips_outside_cap_band belongs in speed_params, not in {group}",
    ):
        load(_pipeline(**{group: {"keep_trips_outside_cap_band": True}}))


@pytest.mark.parametrize("group", ["charge_params", "discharge_params", "speed_params"])
def test_the_spike_threshold_inside_a_group_is_refused(load, group):
    # A parameter group is handed to a detector as keyword arguments, where the
    # key would be an unexpected argument.
    with pytest.raises(
        ValueError,
        match=f"soc_event_spike_pct belongs at the top level of the pipeline, "
        f"not in {group}",
    ):
        load(_pipeline(**{group: {"soc_event_spike_pct": 3}}))


def test_the_error_names_the_offending_pipeline(load, monkeypatch, tmp_path):
    cfg_dir = tmp_path / "named"
    cfg_dir.mkdir()
    pipelines = {
        "fine": copy.deepcopy(_SPEED),
        "broken": _pipeline({"soc_event_spike_pct": -1}),
    }
    (cfg_dir / "pipelines.json").write_text(json.dumps(pipelines), "utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(cfg_dir))
    with pytest.raises(ValueError, match=r"pipelines\.json: broken: "):
        configs.load_pipeline_configs()


def test_the_import_time_load_rejects_a_malformed_key(tmp_path):
    """``PIPELINE_CONFIGS`` is built through the validating loader.

    Run in a fresh interpreter: this session's configs were loaded long ago.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    shipped = Path(configs.CONFIGS_DIR)
    (cfg / "vehicles.json").write_text(
        (shipped / "vehicles.json").read_text("utf-8"), "utf-8"
    )
    pipelines = json.loads((shipped / "pipelines.json").read_text("utf-8"))
    pipelines["ut_broken"] = _pipeline({"soc_event_spike_pct": "three"})
    (cfg / "pipelines.json").write_text(json.dumps(pipelines), "utf-8")
    env = dict(os.environ)
    env.pop("JOLT_CAPACITY_LEDGER", None)
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[2])
        + os.pathsep
        + env.get("PYTHONPATH", ""),
        PYTHONUTF8="1",
        JOLT_CONFIG_DIR=str(cfg),
        JOLT_CACHE_DIR=str(tmp_path / "cache"),
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import report_generator.segmentation.constants"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "ValueError" in proc.stderr
    assert "pipelines.json: ut_broken: soc_event_spike_pct" in proc.stderr
