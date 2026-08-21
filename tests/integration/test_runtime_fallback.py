"""The un-onboarded path: runtime config construction and the no-ledger-write rule.

A registration absent from ``vehicles.json`` must still produce a workbook. The
platform guarantee has two halves:

* :func:`build_runtime_vehicle_config` resolves the vehicle on SRF, routes it to
  the EV or diesel path and assembles an in-memory config — raising
  ``VehicleNotFoundError`` ONLY when the registration exists nowhere;
* a runtime config NEVER writes a capacity entry back to ``vehicles.json``: the
  package must not invent a fleet member.

The SRF client is a ``unittest.mock.Mock`` throughout; no request is issued.
"""

from __future__ import annotations

import datetime
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from report_generator import _generator as gen_mod
from report_generator import general_pipeline as gp
from report_generator._generator import JOLTReportGenerator
from report_generator.data_class import ServerData
from report_generator.segmentation import constants

DS = datetime.datetime(2025, 4, 1)
DE = datetime.datetime(2025, 4, 15)

FULL_EV_COLUMNS = [
    "electricBatteryLevelPercent",
    "wheel_based_speed",
    "battery_pack_ac_watthours",
    "battery_pack_dc_watthours",
    "total_electric_energy_used_plugged_in_included",
    "electric_energy_wheelbased_speed_over_zero",
    "gross_combination_vehicle_weight",
    "gnss_altitude",
]


@pytest.fixture(autouse=True)
def restore_vehicle_config():
    """Undo any registration the generator injects into the shared config."""
    before = set(constants.VEHICLE_CONFIG)
    yield
    for key in set(constants.VEHICLE_CONFIG) - before:
        del constants.VEHICLE_CONFIG[key]


def _srf_vehicle(**attrs):
    vehicle = Mock()
    for key, value in attrs.items():
        setattr(vehicle, key, value)
    return vehicle


def _srf_data_returning(vehicle):
    srf = Mock()
    srf.vehicles.get.return_value = vehicle
    return srf


# ── resolve_srf_vehicle ──────────────────────────────────────────────────────


def test_resolve_srf_vehicle_uses_the_stored_spelling():
    vehicle = _srf_vehicle(registration="ZZ99 ZZZ")
    srf = _srf_data_returning(vehicle)
    resolved, srf_reg = gp.resolve_srf_vehicle("ZZ99ZZZ", srf)
    assert resolved is vehicle
    assert srf_reg == "ZZ99 ZZZ"
    # The verbatim no-space form is tried first.
    assert srf.vehicles.get.call_args_list[0].kwargs == {"obj_id": "ZZ99ZZZ"}


def test_resolve_srf_vehicle_tries_spacing_variants_in_order():
    vehicle = _srf_vehicle(registration="ZZ99 ZZZ")
    srf = Mock()
    srf.vehicles.get.side_effect = [Exception("404"), None, vehicle]
    _resolved, srf_reg = gp.resolve_srf_vehicle("ZZ99ZZZ", srf)
    assert srf_reg == "ZZ99 ZZZ"
    tried = [c.kwargs["obj_id"] for c in srf.vehicles.get.call_args_list]
    assert tried == gp.reg_spacing_variants("ZZ99ZZZ")[:3]


def test_resolve_srf_vehicle_raises_when_nothing_resolves():
    srf = Mock()
    srf.vehicles.get.side_effect = Exception("404")
    with pytest.raises(gp.VehicleNotFoundError) as excinfo:
        gp.resolve_srf_vehicle("ZZ99ZZZ", srf)
    message = str(excinfo.value)
    assert "ZZ99ZZZ" in message
    assert "ZZ99 ZZZ" in message  # the variants it tried are listed
    assert "onboard" in message.lower()


def test_resolve_srf_vehicle_raises_when_every_lookup_returns_none():
    srf = Mock()
    srf.vehicles.get.return_value = None
    with pytest.raises(gp.VehicleNotFoundError):
        gp.resolve_srf_vehicle("ZZ99ZZZ", srf)


# ── build_runtime_vehicle_config ─────────────────────────────────────────────


def test_runtime_config_for_an_electric_vehicle(monkeypatch):
    vehicle = _srf_vehicle(
        registration="ZZ99 ZZZ",
        fuel="ELECTRIC",
        make="Volvo",
        model="FM Electric",
        fuel_capacity=540.0,
    )
    monkeypatch.setattr(
        gp, "_peek_legs", lambda *a, **k: (True, False, FULL_EV_COLUMNS)
    )

    cfg = gp.build_runtime_vehicle_config(
        "ZZ99ZZZ", "ZZ99ZZZ", DS, DE, srf_data=_srf_data_returning(vehicle)
    )
    assert gp.is_runtime_config(cfg) is True
    assert cfg["fuel_type"] == "EV"
    assert cfg["srf_reg"] == "ZZ99 ZZZ"
    assert cfg["pipeline"] == "default_soc"
    assert cfg["make"] == "Volvo"
    assert cfg["nominal_kwh"] is None
    assert cfg["srf_capacity_kwh"] == 540.0
    assert cfg["total_energy_col"] == ("total_electric_energy_used_plugged_in_included")


def test_runtime_config_for_a_diesel_vehicle(monkeypatch):
    vehicle = _srf_vehicle(
        registration="WW70 WWW",
        fuel="DIESEL",
        make="DAF",
        model="XF 450",
        weight_class=44.0,
    )
    peeked = Mock()
    monkeypatch.setattr(gp, "_peek_legs", peeked)

    cfg = gp.build_runtime_vehicle_config(
        "WW70WWW", "WW70WWW", DS, DE, srf_data=_srf_data_returning(vehicle)
    )
    assert cfg["fuel_type"] == "DIESEL"
    assert cfg["leg_source"] == "SRFLOGGER_V1"
    assert cfg["weight_class_t"] == 44.0
    assert cfg["fuel_energy_col"] == "LFC engine total fuel used"
    # SRF already said DIESEL, so no leg probe was needed.
    peeked.assert_not_called()


def test_runtime_config_probes_the_legs_when_srf_does_not_state_the_fuel(monkeypatch):
    vehicle = _srf_vehicle(
        registration="ZZ99 ZZZ", fuel=None, make=None, model=None, weight_class=None
    )
    monkeypatch.setattr(gp, "_peek_legs", lambda *a, **k: (False, True, None))
    cfg = gp.build_runtime_vehicle_config(
        "ZZ99ZZZ", "ZZ99ZZZ", DS, DE, srf_data=_srf_data_returning(vehicle)
    )
    # No SOC column but SRFLOGGER legs exist -> diesel.
    assert cfg["fuel_type"] == "DIESEL"


def test_runtime_config_degrades_to_ev_when_the_probe_finds_nothing(
    monkeypatch, caplog
):
    vehicle = _srf_vehicle(registration="ZZ99 ZZZ", fuel=None, make=None, model=None)
    monkeypatch.setattr(gp, "_peek_legs", lambda *a, **k: (False, False, None))
    with caplog.at_level("WARNING"):
        cfg = gp.build_runtime_vehicle_config(
            "ZZ99ZZZ", "ZZ99ZZZ", DS, DE, srf_data=_srf_data_returning(vehicle)
        )
    assert cfg["fuel_type"] == "EV"
    assert cfg["pipeline"] == "default_speed"  # no SOC column detected
    assert "defaulting to the EV path" in caplog.text


def test_runtime_config_is_never_written_to_disk(monkeypatch, tmp_path):
    """It is injected into the in-memory config only — the fixture's config
    directory must stay byte-identical."""
    import json

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    payload = {"KNOWN01": {"srf_reg": "KNOWN01"}}
    (cfg_dir / "vehicles.json").write_text(json.dumps(payload), "utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(cfg_dir))

    vehicle = _srf_vehicle(
        registration="ZZ99 ZZZ", fuel="ELECTRIC", make=None, model=None
    )
    monkeypatch.setattr(
        gp, "_peek_legs", lambda *a, **k: (True, False, FULL_EV_COLUMNS)
    )
    gp.build_runtime_vehicle_config(
        "ZZ99ZZZ", "ZZ99ZZZ", DS, DE, srf_data=_srf_data_returning(vehicle)
    )
    assert json.loads((cfg_dir / "vehicles.json").read_text("utf-8")) == payload


# ── generate_report: a runtime config never touches the ledger ───────────────


@pytest.fixture
def offline_generator(monkeypatch, tmp_path):
    """A ``JOLTReportGenerator`` whose SRF surface is entirely mocked."""
    monkeypatch.setattr(gen_mod, "make_srf_client", Mock(return_value=Mock()))
    # paged_items over a Mock would raise; return nothing instead so the legs and
    # charger loops are deterministic rather than exception-driven.
    monkeypatch.setattr(
        gen_mod, "paging", types.SimpleNamespace(paged_items=lambda _obj: [])
    )
    monkeypatch.setattr(
        gen_mod,
        "fetch_events",
        Mock(
            return_value=ServerData(vehicle=Mock(), legs=Mock(), charging_events=Mock())
        ),
    )
    generator = JOLTReportGenerator(
        report_output_folder=str(tmp_path), fast_mode=True, debug_mode=False
    )
    generator._persist_effective_capacity = Mock()
    return generator


RUNTIME_CFG = {
    "srf_reg": "ZZ99 ZZZ",
    "fuel_type": "EV",
    "nominal_kwh": None,
    "srf_capacity_kwh": None,
    "effective_capacity_kwh": None,
    "pipeline": "default_soc",
    "_runtime_fallback": True,
}


def test_generate_report_for_an_un_onboarded_reg_writes_a_report_without_persisting(
    monkeypatch, offline_generator, tmp_path
):
    builder = Mock(return_value=dict(RUNTIME_CFG))
    monkeypatch.setattr(gen_mod, "build_runtime_vehicle_config", builder)

    path = offline_generator.generate_report("ZZ99ZZZ", "2025-04-01", "2025-04-15")

    builder.assert_called_once()
    offline_generator._persist_effective_capacity.assert_not_called()
    assert path is not None
    assert Path(path).exists()
    assert Path(path).name == "jolt_report_ZZ99ZZZ_20250401_20250415.xlsx"


def test_generate_report_injects_the_runtime_config_in_memory_only(
    monkeypatch, offline_generator
):
    monkeypatch.setattr(
        gen_mod, "build_runtime_vehicle_config", Mock(return_value=dict(RUNTIME_CFG))
    )
    offline_generator.generate_report("ZZ99ZZZ", "2025-04-01", "2025-04-15")
    assert constants.VEHICLE_CONFIG["ZZ99ZZZ"]["_runtime_fallback"] is True


def test_an_onboarded_reg_never_takes_the_runtime_builder(
    monkeypatch, offline_generator, frozen_configs
):
    """A configured registration resolves straight out of ``VEHICLE_CONFIG``.

    (The positive control for the write-back itself — that an ONBOARDED config
    reaching ``_write_outputs`` does call ``_persist_effective_capacity`` — lives
    in ``tests/test_general_pipeline.py``, because it needs rows and this mocked
    flow deliberately produces none.)
    """
    builder = Mock()
    monkeypatch.setattr(gen_mod, "build_runtime_vehicle_config", builder)
    offline_generator.generate_report("EVSPD01", "2025-06-27", "2025-06-28")
    builder.assert_not_called()


def test_generate_report_propagates_vehicle_not_found(monkeypatch, offline_generator):
    monkeypatch.setattr(
        gen_mod,
        "build_runtime_vehicle_config",
        Mock(side_effect=gp.VehicleNotFoundError("nope")),
    )
    with pytest.raises(gp.VehicleNotFoundError):
        offline_generator.generate_report("ZZ99ZZZ", "2025-04-01", "2025-04-15")


def test_empty_runtime_report_is_structurally_complete(monkeypatch, offline_generator):
    import openpyxl

    from report_generator.columns import HEADERS

    monkeypatch.setattr(
        gen_mod, "build_runtime_vehicle_config", Mock(return_value=dict(RUNTIME_CFG))
    )
    path = offline_generator.generate_report("ZZ99ZZZ", "2025-04-01", "2025-04-15")
    wb = openpyxl.load_workbook(path)
    assert {"Report", "Graphs", "GraphsData", "Definitions"} <= set(wb.sheetnames)
    ws = wb["Report"]
    assert ws.max_row == 1  # header only
    assert tuple(ws.cell(1, c).value for c in range(1, len(HEADERS) + 1)) == HEADERS


def test_onboarded_reg_with_no_segments_returns_none(
    monkeypatch, offline_generator, frozen_configs, caplog
):
    """An ONBOARDED vehicle with nothing to report skips the workbook (only the
    un-onboarded path is guaranteed an empty-but-valid file)."""
    with caplog.at_level("WARNING"):
        result = offline_generator.generate_report(
            "EVSOC01", "2025-06-27", "2025-06-28"
        )
    assert result is None
    assert "No valid segments detected" in caplog.text
