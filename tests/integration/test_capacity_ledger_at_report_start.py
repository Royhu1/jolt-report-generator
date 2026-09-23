"""A capacity ledger named after the import is read when a report starts.

``VEHICLE_CONFIG`` is loaded once, at import. A library consumer may import
``report_generator`` first and only then set ``JOLT_CAPACITY_LEDGER`` (by loading
a ``.env`` later, say). The write-back targets whatever the variable names at
that moment, so the capacity the report reads must come from the same file:
every public report-generation entry point re-applies the ledger to the shared
in-memory configs before it reads the vehicle's config — for the EV and the
diesel dispatch alike, which both run inside
``JOLTReportGenerator.generate_report``.

The SRF surface is mocked throughout and the mocked fetch returns no legs, so
each report stops right after its config has been read: nothing is fetched and
nothing is written.
"""

from __future__ import annotations

import copy
import json
import types
from unittest.mock import Mock

import pytest

import report_generator
from report_generator import _generator as gen_mod
from report_generator import configs
from report_generator._generator import JOLTReportGenerator
from report_generator.data_class import ServerData
from report_generator.segmentation import constants

LEDGER_Q = {"20250101_20250401": {"kwh": 432.1, "n": 20}}


class _RecordingConfig(dict):
    """A vehicle config that records every read of its capacity seed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seed_reads: list = []

    def get(self, key, default=None):
        value = super().get(key, default)
        if key == "effective_capacity_kwh":
            self.seed_reads.append(value)
        return value


@pytest.fixture
def offline(monkeypatch):
    """No SRF client, no pages and a fetch that returns no legs."""
    monkeypatch.setattr(gen_mod, "make_srf_client", Mock(return_value=Mock()))
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


@pytest.fixture
def restore_vehicle_config():
    """Undo any registration a report injects into the shared config."""
    before = set(constants.VEHICLE_CONFIG)
    yield
    for key in set(constants.VEHICLE_CONFIG) - before:
        del constants.VEHICLE_CONFIG[key]


def _recording(monkeypatch, frozen_configs, alias):
    """Replace ``alias``'s shared config by a recording copy of its frozen one."""
    cfg = _RecordingConfig(copy.deepcopy(frozen_configs["vehicles"][alias]))
    monkeypatch.setitem(constants.VEHICLE_CONFIG, alias, cfg)
    return cfg


def _name_the_ledger_now(monkeypatch, path, payload):
    """What a .env loaded after the import does: write and name the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(path))


def _forbid_reading_vehicles_json(monkeypatch):
    """The overlay must work on the configs in memory, never re-read the file."""

    def _called(*_args, **_kwargs):
        raise AssertionError("vehicles.json must not be re-read")

    monkeypatch.setattr(configs, "_load_config_json", _called)


@pytest.mark.parametrize("alias", ["EVSPD01", "DSL01"], ids=["ev", "diesel"])
def test_a_ledger_named_after_the_import_is_read_before_the_capacity_seed(
    monkeypatch, tmp_path, offline, frozen_configs, alias
):
    assert frozen_configs["vehicles"][alias].get("effective_capacity_kwh") != 432.1
    cfg = _recording(monkeypatch, frozen_configs, alias)
    _name_the_ledger_now(
        monkeypatch,
        tmp_path / "state" / "capacity_ledger.json",
        {
            alias: {
                "effective_capacity_kwh": 432.1,
                "effective_capacity_quarterly": LEDGER_Q,
            }
        },
    )
    _forbid_reading_vehicles_json(monkeypatch)

    generator = JOLTReportGenerator(report_output_folder=str(tmp_path / "out"))
    assert generator.generate_report(alias, "2025-06-27", "2025-06-28") is None

    # The very first read of the seed already sees the ledger's value ...
    assert cfg.seed_reads[0] == 432.1
    # ... and the shared config holds the ledger's two keys, in place.
    assert constants.VEHICLE_CONFIG[alias] is cfg
    assert cfg["effective_capacity_kwh"] == 432.1
    assert cfg["effective_capacity_quarterly"] == LEDGER_Q


def test_the_convenience_function_reads_a_ledger_named_after_the_import(
    monkeypatch, tmp_path, offline, frozen_configs
):
    cfg = _recording(monkeypatch, frozen_configs, "EVSPD01")
    _name_the_ledger_now(
        monkeypatch,
        tmp_path / "state" / "capacity_ledger.json",
        {"EVSPD01": {"effective_capacity_kwh": 432.1}},
    )
    monkeypatch.chdir(tmp_path)  # it writes under ./<outputfolder>/<namespace>

    report_generator.generate_report(
        "EVSPD01", "2025-06-27", "2025-06-28", mode="fast", outputfolder="out"
    )

    assert cfg.seed_reads[0] == 432.1
    assert constants.VEHICLE_CONFIG["EVSPD01"]["effective_capacity_kwh"] == 432.1


def test_without_the_variable_a_report_start_leaves_the_configs_alone(
    monkeypatch, tmp_path, offline, frozen_configs
):
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    shared = constants.VEHICLE_CONFIG
    before = copy.deepcopy(dict(shared))
    entries = {reg: id(cfg) for reg, cfg in shared.items()}

    def _called(*_args, **_kwargs):
        raise AssertionError("nothing may be read without the variable")

    monkeypatch.setattr(configs, "_read_capacity_ledger", _called)
    _forbid_reading_vehicles_json(monkeypatch)

    generator = JOLTReportGenerator(report_output_folder=str(tmp_path / "out"))
    assert generator.generate_report("EVSPD01", "2025-06-27", "2025-06-28") is None

    assert gen_mod.VEHICLE_CONFIG is shared is constants.VEHICLE_CONFIG
    assert dict(shared) == before
    assert {reg: id(cfg) for reg, cfg in shared.items()} == entries


def test_a_runtime_fallback_config_takes_nothing_from_the_ledger(
    monkeypatch, tmp_path, offline, restore_vehicle_config
):
    """An un-onboarded vehicle generated twice in one process reads no ledger state.

    Its runtime config is injected into ``VEHICLE_CONFIG`` by the first report,
    so the second report's overlay finds it there; a ledger entry for the same
    registration (e.g. a vehicle since removed from ``vehicles.json``) must not
    reach it — the ledger records state for configured vehicles only.
    """
    runtime = {
        "srf_reg": "ZZ99 ZZZ",
        "fuel_type": "EV",
        "nominal_kwh": None,
        "srf_capacity_kwh": None,
        "effective_capacity_kwh": None,
        "pipeline": "default_soc",
        "_runtime_fallback": True,
    }
    builder = Mock(return_value=dict(runtime))
    monkeypatch.setattr(gen_mod, "build_runtime_vehicle_config", builder)
    _name_the_ledger_now(
        monkeypatch,
        tmp_path / "state" / "capacity_ledger.json",
        {"ZZ99ZZZ": {"effective_capacity_kwh": 999.9}},
    )
    generator = JOLTReportGenerator(report_output_folder=str(tmp_path / "out"))
    generator._persist_effective_capacity = Mock()

    for _ in range(2):
        generator.generate_report("ZZ99ZZZ", "2025-04-01", "2025-04-15")

    builder.assert_called_once()  # the second report found the injected config
    assert constants.VEHICLE_CONFIG["ZZ99ZZZ"]["effective_capacity_kwh"] is None
    assert "effective_capacity_quarterly" not in constants.VEHICLE_CONFIG["ZZ99ZZZ"]
    generator._persist_effective_capacity.assert_not_called()
