"""Deployment-time path resolution and config loading.

These helpers are the whole "where does state live" contract a deployer
configures: ``JOLT_CACHE_DIR``, ``SRF_API_ROOT``, ``JOLT_CONFIG_DIR`` and
``JOLT_CAPACITY_LEDGER``. Every one must be read at CALL time (not import time)
so a process can be reconfigured, and a missing config file must fail with an
actionable message rather than an empty ``VEHICLE_CONFIG`` surfacing much later
as "vehicle not registered".

The public loaders (``load_vehicle_configs`` / ``load_pipeline_configs``) are
what every consumer reads the configs through, so their overlay semantics are
pinned here on small synthetic files; the write side of the external ledger is
in ``integration/test_capacity_ledger_file.py``.
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


# ── get_capacity_ledger_path ─────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "   "])
def test_there_is_no_external_ledger_without_the_variable(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    else:
        monkeypatch.setenv("JOLT_CAPACITY_LEDGER", value)
    assert configs.get_capacity_ledger_path() is None


def test_the_ledger_path_is_read_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(tmp_path / "a.json"))
    assert configs.get_capacity_ledger_path() == tmp_path / "a.json"
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(tmp_path / "b.json"))
    assert configs.get_capacity_ledger_path() == tmp_path / "b.json"


def test_the_ledger_contract_constants():
    assert configs.CAPACITY_LEDGER_ENV_VAR == "JOLT_CAPACITY_LEDGER"
    assert configs.LEDGER_KEYS == (
        "effective_capacity_kwh",
        "effective_capacity_quarterly",
    )


# ── load_vehicle_configs / load_pipeline_configs ─────────────────────────────

_Q1 = {"20250101_20250401": {"kwh": 350.0, "n": 10}}
_VEHICLES = {
    "UTVEH01": {
        "srf_reg": "UTVEH01",
        "nominal_kwh": 540,
        "effective_capacity_kwh": 350.0,
        "effective_capacity_quarterly": _Q1,
        "pipeline": "ut_speed",
    },
    "UTVEH02": {"srf_reg": "UTVEH02", "effective_capacity_kwh": 200.0},
}
_PIPELINES = {"ut_speed": {"branch": "speed"}}


@pytest.fixture
def synthetic_config_dir(monkeypatch, tmp_path):
    """A config directory holding a two-vehicle vehicles.json + pipelines.json."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "vehicles.json").write_text(json.dumps(_VEHICLES, indent=2), "utf-8")
    (cfg / "pipelines.json").write_text(json.dumps(_PIPELINES), "utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    return cfg


def _use_ledger(
    monkeypatch, tmp_path, payload=None, text=None, name="capacity_ledger.json"
):
    """Point JOLT_CAPACITY_LEDGER at a ledger file, written unless both are None."""
    path = tmp_path / "state" / name
    if payload is not None or text is not None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text if text is not None else json.dumps(payload), "utf-8")
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(path))
    return path


def test_without_a_ledger_the_vehicle_configs_are_the_file_as_parsed(
    synthetic_config_dir,
):
    loaded = configs.load_vehicle_configs()
    assert loaded == _VEHICLES
    assert list(loaded) == list(_VEHICLES)  # the key order too


def test_the_pipeline_configs_are_the_file_as_parsed(synthetic_config_dir):
    assert configs.load_pipeline_configs() == _PIPELINES


def test_every_load_is_a_fresh_read(synthetic_config_dir):
    first = configs.load_vehicle_configs()
    first["UTVEH01"]["nominal_kwh"] = 1  # a caller mutating its result ...
    # ... never reaches the next caller.
    assert configs.load_vehicle_configs()["UTVEH01"]["nominal_kwh"] == 540


def test_the_ledger_replaces_both_ledger_keys(
    synthetic_config_dir, monkeypatch, tmp_path
):
    q2 = {"20250401_20250701": {"kwh": 400.0, "n": 20}}
    _use_ledger(
        monkeypatch,
        tmp_path,
        {
            "UTVEH01": {
                "effective_capacity_kwh": 400.0,
                "effective_capacity_quarterly": q2,
            }
        },
    )
    entry = configs.load_vehicle_configs()["UTVEH01"]
    assert entry["effective_capacity_kwh"] == 400.0
    # Replaced, not merged: once the ledger holds a history it is the history.
    assert entry["effective_capacity_quarterly"] == q2
    # Parameters are never touched by the overlay.
    assert (entry["nominal_kwh"], entry["pipeline"]) == (540, "ut_speed")


def test_a_key_the_ledger_entry_lacks_keeps_the_config_value(
    synthetic_config_dir, monkeypatch, tmp_path
):
    _use_ledger(monkeypatch, tmp_path, {"UTVEH01": {"effective_capacity_kwh": 410.0}})
    entry = configs.load_vehicle_configs()["UTVEH01"]
    assert entry["effective_capacity_kwh"] == 410.0
    assert entry["effective_capacity_quarterly"] == _Q1


def test_a_vehicle_the_ledger_does_not_cover_keeps_its_config_values(
    synthetic_config_dir, monkeypatch, tmp_path
):
    _use_ledger(monkeypatch, tmp_path, {"UTVEH01": {"effective_capacity_kwh": 410.0}})
    assert configs.load_vehicle_configs()["UTVEH02"] == _VEHICLES["UTVEH02"]


def test_a_ledger_only_registration_is_ignored(
    synthetic_config_dir, monkeypatch, tmp_path
):
    # The ledger records state for configured vehicles; it cannot configure one.
    _use_ledger(monkeypatch, tmp_path, {"NOTAVEH": {"effective_capacity_kwh": 123.0}})
    loaded = configs.load_vehicle_configs()
    assert "NOTAVEH" not in loaded
    assert loaded == _VEHICLES


def test_only_the_two_ledger_keys_are_overlaid(
    synthetic_config_dir, monkeypatch, tmp_path
):
    _use_ledger(
        monkeypatch,
        tmp_path,
        {
            "UTVEH01": {
                "nominal_kwh": 1,
                "pipeline": "not_a_pipeline",
                "effective_capacity_kwh": 400.0,
            }
        },
    )
    entry = configs.load_vehicle_configs()["UTVEH01"]
    assert (entry["nominal_kwh"], entry["pipeline"]) == (540, "ut_speed")
    assert entry["effective_capacity_kwh"] == 400.0


def test_a_ledger_file_that_does_not_exist_yet_means_no_overlay(
    synthetic_config_dir, monkeypatch, tmp_path
):
    path = _use_ledger(monkeypatch, tmp_path)
    assert configs.load_vehicle_configs() == _VEHICLES
    # Reading never creates the ledger, its lock file or its directory.
    assert not path.parent.exists()


def test_a_blank_ledger_file_means_no_overlay(
    synthetic_config_dir, monkeypatch, tmp_path
):
    _use_ledger(monkeypatch, tmp_path, text="  \n")
    assert configs.load_vehicle_configs() == _VEHICLES


@pytest.mark.parametrize("text", ["[]", '{"UTVEH01": 400.0}', "{not json"])
def test_a_damaged_ledger_fails_loudly(
    synthetic_config_dir, monkeypatch, tmp_path, text
):
    # Never read as empty: a write-back would then overwrite what is there.
    _use_ledger(monkeypatch, tmp_path, text=text)
    with pytest.raises(ValueError):
        configs.load_vehicle_configs()


# ── apply_capacity_ledger: configs already loaded, brought up to date ────────

_Q_A = {"20240401_20240701": {"kwh": 450.0, "n": 20}}
_LEDGER_A = {
    "UTVEH01": {"effective_capacity_kwh": 450.0, "effective_capacity_quarterly": _Q_A},
    "UTVEH02": {
        "effective_capacity_kwh": 250.0,
        "effective_capacity_quarterly": {"20240401_20240701": {"kwh": 250.0, "n": 9}},
    },
}


def _loaded_before_the_variable():
    """Configs as a process holds them when the ledger variable is set later."""
    return json.loads(json.dumps(_VEHICLES))


def _ledger_keys(cfg):
    return {k: cfg[k] for k in configs.LEDGER_KEYS if k in cfg}


def _forbid(monkeypatch, *names):
    """Make the named ``configs`` internals fail the test if they are called."""

    def _called(*_args, **_kwargs):
        raise AssertionError("must not be called")

    for name in names:
        monkeypatch.setattr(configs, name, _called)


def test_applying_the_ledger_without_the_variable_is_a_strict_no_op(monkeypatch):
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    vehicles = _loaded_before_the_variable()
    vehicles["UTVEH01"]["effective_capacity_quarterly"] = _Q_A  # stale, and kept
    before = json.loads(json.dumps(vehicles))
    entry = vehicles["UTVEH01"]
    # Neither the ledger nor vehicles.json is read.
    _forbid(monkeypatch, "_read_capacity_ledger", "_load_config_json")

    assert configs.apply_capacity_ledger(vehicles) is None
    assert vehicles == before
    assert vehicles["UTVEH01"] is entry


def test_applying_the_ledger_gives_loaded_configs_the_loader_view(
    synthetic_config_dir, monkeypatch, tmp_path
):
    vehicles = _loaded_before_the_variable()
    entry = vehicles["UTVEH01"]
    q2 = {"20250401_20250701": {"kwh": 432.1, "n": 20}}
    path = _use_ledger(
        monkeypatch,
        tmp_path,
        {
            "UTVEH01": {
                "effective_capacity_kwh": 432.1,
                "effective_capacity_quarterly": q2,
            },
            "NOTAVEH": {"effective_capacity_kwh": 123.0},
        },
    )

    assert configs.apply_capacity_ledger(vehicles) == path
    assert vehicles["UTVEH01"] is entry  # written in place
    assert entry["effective_capacity_kwh"] == 432.1
    assert entry["effective_capacity_quarterly"] == q2
    assert (entry["nominal_kwh"], entry["pipeline"]) == (540, "ut_speed")
    assert vehicles["UTVEH02"] == _VEHICLES["UTVEH02"]
    assert "NOTAVEH" not in vehicles  # the ledger cannot configure a vehicle
    assert vehicles == configs.load_vehicle_configs()


def test_naming_another_ledger_restores_the_keys_it_lacks(
    synthetic_config_dir, monkeypatch, tmp_path
):
    vehicles = _loaded_before_the_variable()
    _use_ledger(monkeypatch, tmp_path, _LEDGER_A, name="a.json")
    configs.apply_capacity_ledger(vehicles)
    assert vehicles["UTVEH01"]["effective_capacity_quarterly"] == _Q_A

    # Ledger B carries only UTVEH01's scalar, and nothing for UTVEH02.
    _use_ledger(
        monkeypatch,
        tmp_path,
        {"UTVEH01": {"effective_capacity_kwh": 410.0}},
        name="b.json",
    )
    configs.apply_capacity_ledger(vehicles)

    # Nothing of ledger A survives: each key is B's, else vehicles.json's.
    assert _ledger_keys(vehicles["UTVEH01"]) == {
        "effective_capacity_kwh": 410.0,
        "effective_capacity_quarterly": _Q1,
    }
    assert vehicles["UTVEH02"] == _VEHICLES["UTVEH02"]
    # UTVEH02 has no quarterly history in vehicles.json, so none is kept at all.
    assert "effective_capacity_quarterly" not in vehicles["UTVEH02"]
    assert vehicles == configs.load_vehicle_configs()


def test_a_ledger_edited_between_two_applications_is_read_again(
    synthetic_config_dir, monkeypatch, tmp_path
):
    vehicles = _loaded_before_the_variable()
    path = _use_ledger(monkeypatch, tmp_path, _LEDGER_A)
    configs.apply_capacity_ledger(vehicles)
    assert _ledger_keys(vehicles["UTVEH01"]) == _LEDGER_A["UTVEH01"]

    path.write_text(json.dumps({"UTVEH01": {"effective_capacity_kwh": 410.0}}), "utf-8")
    configs.apply_capacity_ledger(vehicles)

    assert vehicles["UTVEH01"]["effective_capacity_kwh"] == 410.0
    assert vehicles["UTVEH01"]["effective_capacity_quarterly"] == _Q1
    assert vehicles == configs.load_vehicle_configs()


def test_applying_the_ledger_is_idempotent(synthetic_config_dir, monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path, {"UTVEH01": {"effective_capacity_kwh": 410.0}})
    vehicles = _loaded_before_the_variable()
    configs.apply_capacity_ledger(vehicles)
    once = json.loads(json.dumps(vehicles))
    configs.apply_capacity_ledger(vehicles)
    assert vehicles == once == configs.load_vehicle_configs()


def test_the_applied_values_are_copies(synthetic_config_dir, monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path, _LEDGER_A)
    first, second = _loaded_before_the_variable(), _loaded_before_the_variable()
    configs.apply_capacity_ledger(first)
    configs.apply_capacity_ledger(second)
    first["UTVEH01"]["effective_capacity_quarterly"]["x"] = {"kwh": 1.0, "n": 1}
    assert "x" not in second["UTVEH01"]["effective_capacity_quarterly"]


def test_a_registration_not_in_vehicles_json_is_left_alone(
    synthetic_config_dir, monkeypatch, tmp_path
):
    # E.g. a config only this process holds: the ledger never applies to it.
    vehicles = _loaded_before_the_variable()
    vehicles["MEMONLY01"] = {"srf_reg": "MEMONLY01", "effective_capacity_kwh": 111.1}
    _use_ledger(
        monkeypatch,
        tmp_path,
        {
            "MEMONLY01": {
                "effective_capacity_kwh": 999.9,
                "effective_capacity_quarterly": _Q_A,
            }
        },
    )
    configs.apply_capacity_ledger(vehicles)
    assert vehicles["MEMONLY01"] == {
        "srf_reg": "MEMONLY01",
        "effective_capacity_kwh": 111.1,
    }


def test_applying_the_ledger_leaves_a_skipped_config_alone(
    synthetic_config_dir, monkeypatch, tmp_path
):
    vehicles = _loaded_before_the_variable()
    vehicles["UTVEH02"]["_marker"] = True
    _use_ledger(monkeypatch, tmp_path, _LEDGER_A)
    configs.apply_capacity_ledger(vehicles, skip=lambda cfg: bool(cfg.get("_marker")))
    assert _ledger_keys(vehicles["UTVEH01"]) == _LEDGER_A["UTVEH01"]
    assert vehicles["UTVEH02"] == {**_VEHICLES["UTVEH02"], "_marker": True}


def test_applying_a_damaged_ledger_fails_loudly(
    synthetic_config_dir, monkeypatch, tmp_path
):
    vehicles = _loaded_before_the_variable()
    _use_ledger(monkeypatch, tmp_path, text="[]")
    with pytest.raises(ValueError):
        configs.apply_capacity_ledger(vehicles)
    assert vehicles == _VEHICLES


def test_the_import_time_vehicle_config_goes_through_the_public_loader(tmp_path):
    """``segmentation.constants`` builds ``VEHICLE_CONFIG`` with the overlay applied.

    Run in a fresh interpreter: this session's ``VEHICLE_CONFIG`` was built once,
    at import, with the ledger variable removed by the conftest, and it is shared
    by reference across the package, so it must not be rebuilt here.
    """
    import os
    import subprocess
    import sys

    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "vehicles.json").write_text(json.dumps(_VEHICLES), "utf-8")
    (cfg / "pipelines.json").write_text(json.dumps(_PIPELINES), "utf-8")
    ledger = tmp_path / "capacity_ledger.json"
    ledger.write_text(
        json.dumps({"UTVEH01": {"effective_capacity_kwh": 432.1}}), "utf-8"
    )
    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env.update(
        PYTHONPATH=repo_root + os.pathsep + env.get("PYTHONPATH", ""),
        PYTHONUTF8="1",
        JOLT_CONFIG_DIR=str(cfg),
        JOLT_CAPACITY_LEDGER=str(ledger),
        JOLT_CACHE_DIR=str(tmp_path / "cache"),
    )
    code = (
        "import json\n"
        "from report_generator.segmentation import constants as c\n"
        "v = c.VEHICLE_CONFIG\n"
        "print(json.dumps([v['UTVEH01']['effective_capacity_kwh'],\n"
        "                  v['UTVEH02']['effective_capacity_kwh'],\n"
        "                  sorted(c.PIPELINE_CONFIGS)]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [
        432.1,
        200.0,
        ["ut_speed"],
    ]
