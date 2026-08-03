"""Config schema contract: the three JSONs load and satisfy what the generator reads.

The required-field list is derived from the actual hard ``cfg[...]`` accesses in the
generation path (not invented):
  * every vehicle: ``srf_reg``      (_generator.generate_report)
  * diesel only:   ``weight_class_t`` (diesel_pipeline.process_diesel_leg)
Every EV vehicle's ``pipeline`` must resolve in pipelines.json. Diesel vehicles
carry ``pipeline: "daf_diesel_logger"`` — a dispatch marker (fuel_type==DIESEL +
leg_source==SRFLOGGER_V1), deliberately NOT a pipelines.json entry — so they are
excluded from the pipeline-resolution check.
"""

import json

import pytest

from jolt_toolkit.configs import get_config_path

CONFIG_FILES = ["vehicles.json", "pipelines.json", "plot_config.json"]


def _load(name):
    with open(get_config_path(name), encoding="utf-8") as fh:
        return json.load(fh)


VEHICLES = _load("vehicles.json")
PIPELINES = _load("pipelines.json")


def _is_diesel(cfg):
    return str(cfg.get("fuel_type", "")).upper() == "DIESEL"


@pytest.mark.parametrize("name", CONFIG_FILES)
def test_config_json_loads(name):
    data = _load(name)
    assert isinstance(data, dict)
    assert data, f"{name} is empty"


def test_vehicles_is_non_empty_mapping():
    assert isinstance(VEHICLES, dict) and len(VEHICLES) >= 1


@pytest.mark.parametrize("reg", sorted(VEHICLES))
def test_every_vehicle_has_srf_reg(reg):
    # srf_reg is the one field _generator.generate_report reads via cfg["..."].
    assert "srf_reg" in VEHICLES[reg], f"{reg} missing required 'srf_reg'"


@pytest.mark.parametrize("reg", sorted(VEHICLES))
def test_ev_pipeline_resolves(reg):
    cfg = VEHICLES[reg]
    if _is_diesel(cfg):
        pytest.skip(
            "diesel uses the daf_diesel_logger dispatch marker, not pipelines.json"
        )
    pipeline = cfg.get("pipeline")
    if pipeline is None:
        pytest.skip(f"{reg} declares no pipeline (falls back to default_soc)")
    assert pipeline in PIPELINES, f"{reg} pipeline {pipeline!r} not in pipelines.json"


@pytest.mark.parametrize("reg", sorted(r for r, c in VEHICLES.items() if _is_diesel(c)))
def test_diesel_has_weight_class_t(reg):
    # weight_class_t is read via cfg['weight_class_t'] in diesel_pipeline.
    assert "weight_class_t" in VEHICLES[reg], f"{reg} (diesel) missing 'weight_class_t'"


def test_at_least_one_diesel_and_one_ev():
    diesel = [r for r, c in VEHICLES.items() if _is_diesel(c)]
    ev = [r for r, c in VEHICLES.items() if not _is_diesel(c)]
    assert diesel, "expected at least one diesel vehicle in the fixture set"
    assert ev, "expected at least one EV vehicle in the fixture set"
