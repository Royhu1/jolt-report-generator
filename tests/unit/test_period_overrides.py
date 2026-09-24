"""Date-effective vehicle settings (``period_overrides``): validation and resolution.

A vehicle's entry may carry dated windows, each with the segmentation settings
it changes. ``load_vehicle_configs()`` — and so the import-time load — rejects a
malformed list with a ``ValueError`` naming the vehicle and the override, and
``effective_vehicle_config(cfg, when)`` resolves an entry for a date: the base
entry updated with the ``set`` of the window containing it, ``from`` inclusive
and ``to`` exclusive. A leg is resolved for the UTC date of the first valid
timestamp of its telematics frame (``frame_utc_date``).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from report_generator import configs
from report_generator.segmentation import constants
from report_generator.segmentation.mass_aggregation import resolve_mass_agg
from report_generator.segmentation.timeutil import frame_utc_date

REG = "UTVEH01"
FROM = "2026-07-07"
D = dt.date(2026, 7, 7)
_PIPELINES = {
    "ut_soc": {"branch": "soc"},
    "ut_speed": {"branch": "speed", "mass_agg": "iqr_median"},
}
_BASE = {
    "srf_reg": REG,
    "nominal_kwh": 540,
    "pipeline": "ut_soc",
    "effective_capacity_kwh": 350.0,
    "min_cluster_gap_kg": 2000.0,
}
_OVERRIDE = {
    "from": FROM,
    "to": None,
    "reason": "the telematics feed thinned out",
    "set": {"pipeline": "ut_speed", "prefer_logger_speed": True},
}


def _vehicle(*overrides, **base):
    return {**_BASE, **base, "period_overrides": [copy.deepcopy(o) for o in overrides]}


def _override(**fields):
    """``_OVERRIDE`` with fields replaced; a field given as ``...`` is removed."""
    out = copy.deepcopy(_OVERRIDE)
    for key, value in fields.items():
        if value is ...:
            out.pop(key, None)
        else:
            out[key] = value
    return out


@pytest.fixture
def load(monkeypatch, tmp_path):
    """Write a vehicles.json (+ the synthetic pipelines) and load it."""
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)

    def _load(vehicles):
        cfg_dir = tmp_path / "configs"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "vehicles.json").write_text(json.dumps(vehicles), "utf-8")
        (cfg_dir / "pipelines.json").write_text(json.dumps(_PIPELINES), "utf-8")
        monkeypatch.setenv("JOLT_CONFIG_DIR", str(cfg_dir))
        return configs.load_vehicle_configs()

    return _load


# ── Load-time validation ─────────────────────────────────────────────────────


def test_a_valid_list_loads_and_is_kept_as_written(load):
    vehicles = {REG: _vehicle(_OVERRIDE)}
    assert load(vehicles) == vehicles


@pytest.mark.parametrize("value", [None, []], ids=["null", "empty"])
def test_an_unset_or_empty_field_is_valid(load, value):
    vehicles = {REG: {**_BASE, "period_overrides": value}}
    assert load(vehicles) == vehicles


def test_adjacent_windows_do_not_overlap(load):
    first = _override(to="2026-08-01")
    second = _override(**{"from": "2026-08-01", "to": None})
    assert load({REG: _vehicle(first, second)})[REG]["period_overrides"][1] == second


def test_the_allow_list_is_the_per_leg_segmentation_settings():
    assert configs.PERIOD_OVERRIDE_KEYS == (
        "pipeline",
        "prefer_logger_speed",
        "min_stop_duration_min",
        "split_by_mass",
        "merge_by_mass",
        "split_long_stops_min",
        "min_cluster_gap_kg",
        "mass_agg",
    )


@pytest.mark.parametrize(
    "override, message",
    [
        # A key outside the allow-list: a column mapping, the capacity ledger,
        # the fuel type, the identity, the operator, the field itself.
        (_override(set={"speed_col": "speed"}), "may only change"),
        (_override(set={"effective_capacity_kwh": 400.0}), "may only change"),
        (_override(set={"effective_capacity_quarterly": {}}), "may only change"),
        (_override(set={"nominal_kwh": 600}), "may only change"),
        (_override(set={"fuel_type": "DIESEL"}), "may only change"),
        (_override(set={"srf_reg": "OTHER"}), "may only change"),
        (_override(set={"operator": "X"}), "may only change"),
        (_override(set={"period_overrides": []}), "may only change"),
        (_override(set={"soc_energy_fallback": True}), "may only change"),
        # Dates.
        (_override(**{"from": "2026-13-01"}), "'from' must be a date"),
        (_override(**{"from": "07/07/2026"}), "'from' must be a date"),
        (_override(**{"from": "2026-7-7"}), "'from' must be a date"),
        (_override(**{"from": "20260707"}), "'from' must be a date"),
        (_override(**{"from": "2026-02-30"}), "'from' must be a date"),
        (_override(**{"from": 20260707}), "'from' must be a date"),
        (_override(to="2026-99-99"), "'to' must be a date"),
        (_override(to=FROM), "must be after 'from'"),
        (_override(to="2026-07-06"), "must be after 'from'"),
        # The set.
        (_override(set={}), "'set' is empty"),
        (_override(set=...), "'set' is required"),
        (_override(set=["pipeline"]), "'set' must be an object"),
        (_override(set={"pipeline": "not_a_pipeline"}), "is not in pipelines.json"),
        # The override itself.
        (_override(**{"from": ...}), "'from' is required"),
        (_override(form=FROM), "unknown field(s) form"),
        (_override(reason=3), "'reason' must be text"),
        ("2026-07-07", "an override must be an object"),
        # Values the per-leg code would misread.
        (_override(set={"prefer_logger_speed": "false"}), "must be true or false"),
        (_override(set={"merge_by_mass": 0}), "must be true or false"),
        (_override(set={"split_by_mass": None}), "must be true or false"),
        (_override(set={"min_stop_duration_min": 0}), "a positive number of minutes"),
        (_override(set={"min_stop_duration_min": True}), "a positive number"),
        (_override(set={"min_stop_duration_min": None}), "a positive number"),
        (_override(set={"split_long_stops_min": "45"}), "or null"),
        (_override(set={"min_cluster_gap_kg": None}), "a positive number of kg"),
        (_override(set={"pipeline": ""}), "the name of a pipeline"),
        (_override(set={"mass_agg": ""}), "a mass-aggregation method"),
    ],
)
def test_a_malformed_override_names_the_vehicle_and_the_override(
    load, override, message
):
    with pytest.raises(ValueError) as excinfo:
        load({"OTHER01": dict(_BASE, srf_reg="OTHER01"), REG: _vehicle(override)})
    text = str(excinfo.value)
    assert message in text
    assert f"{REG}: period_overrides[0]" in text


@pytest.mark.parametrize(
    "windows",
    [
        [("2026-01-01", "2026-03-01"), ("2026-02-01", None)],
        [("2026-01-01", None), ("2026-06-01", "2026-07-01")],
        [("2026-06-01", "2026-07-01"), ("2026-01-01", None)],  # listed out of order
        [("2026-01-01", "2026-02-01"), ("2026-01-01", "2026-02-01")],
    ],
    ids=["partial", "open-ended first", "out of order", "identical"],
)
def test_overlapping_windows_are_rejected(load, windows):
    overrides = [_override(**{"from": f, "to": t}) for f, t in windows]
    with pytest.raises(ValueError, match=rf"{REG}: period_overrides\[\d\].* overlaps"):
        load({REG: _vehicle(*overrides)})


def test_a_field_that_is_not_a_list_is_rejected(load):
    with pytest.raises(ValueError, match=f"{REG}: period_overrides must be a list"):
        load({REG: {**_BASE, "period_overrides": _OVERRIDE}})


def test_a_diesel_vehicle_may_not_carry_overrides(load):
    diesel = {"srf_reg": "DSLUT01", "fuel_type": "DIESEL", "pipeline": "x"}
    diesel["period_overrides"] = [_override(set={"min_stop_duration_min": 7.0})]
    with pytest.raises(ValueError, match="DSLUT01: period_overrides are not supported"):
        load({"DSLUT01": diesel})


def test_the_import_time_load_rejects_a_malformed_override(tmp_path):
    """``VEHICLE_CONFIG`` is built through the validating loader.

    Run in a fresh interpreter: this session's configs were loaded long ago.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    bad = {REG: _vehicle(_override(set={"speed_col": "speed"}))}
    (cfg / "vehicles.json").write_text(json.dumps(bad), "utf-8")
    (cfg / "pipelines.json").write_text(json.dumps(_PIPELINES), "utf-8")
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
    assert f"{REG}: period_overrides[0] (from {FROM})" in proc.stderr


def test_a_config_without_the_field_reads_no_pipelines(load, monkeypatch):
    real = configs._load_config_json
    read = []

    def spy(name):
        read.append(name)
        return real(name)

    monkeypatch.setattr(configs, "_load_config_json", spy)
    load({REG: dict(_BASE)})
    assert read == ["vehicles.json"]


# ── Resolution ───────────────────────────────────────────────────────────────


def _settings(cfg):
    return {k: cfg.get(k) for k in ("pipeline", "prefer_logger_speed")}


BASE_SETTINGS = {"pipeline": "ut_soc", "prefer_logger_speed": None}
OVERRIDE_SETTINGS = {"pipeline": "ut_speed", "prefer_logger_speed": True}


@pytest.mark.parametrize(
    "when, expected",
    [
        (D - dt.timedelta(days=1), BASE_SETTINGS),  # before 'from'
        (D, OVERRIDE_SETTINGS),  # on 'from': inclusive
        (D + dt.timedelta(days=400), OVERRIDE_SETTINGS),  # open-ended
    ],
    ids=["before", "on", "after"],
)
def test_the_window_opens_on_its_from_date(when, expected):
    assert _settings(configs.effective_vehicle_config(_vehicle(_OVERRIDE), when)) == (
        expected
    )


@pytest.mark.parametrize(
    "when, expected",
    [
        (dt.date(2026, 7, 31), OVERRIDE_SETTINGS),  # the day before 'to'
        (dt.date(2026, 8, 1), BASE_SETTINGS),  # on 'to': exclusive
        (dt.date(2026, 9, 1), BASE_SETTINGS),
    ],
    ids=["before to", "on to", "after to"],
)
def test_the_window_closes_before_its_to_date(when, expected):
    cfg = _vehicle(_override(to="2026-08-01"))
    assert _settings(configs.effective_vehicle_config(cfg, when)) == expected


def test_between_two_windows_the_base_applies():
    cfg = _vehicle(
        _override(to="2026-08-01"),
        _override(**{"from": "2026-09-01"}, set={"merge_by_mass": False}),
    )
    assert configs.effective_vehicle_config(cfg, dt.date(2026, 8, 15))["pipeline"] == (
        "ut_soc"
    )
    later = configs.effective_vehicle_config(cfg, dt.date(2026, 9, 2))
    assert (later["pipeline"], later["merge_by_mass"]) == ("ut_soc", False)


def test_the_result_carries_no_overrides_and_is_otherwise_the_base():
    cfg = _vehicle(_OVERRIDE)
    changed = set(_OVERRIDE["set"])
    for when in (None, D - dt.timedelta(days=1), D):
        result = configs.effective_vehicle_config(cfg, when)
        assert "period_overrides" not in result
        assert {k: v for k, v in result.items() if k not in changed} == {
            k: v for k, v in _BASE.items() if k not in changed
        }


def test_a_vehicle_without_the_field_resolves_to_itself():
    assert configs.effective_vehicle_config(dict(_BASE), D) == _BASE
    assert configs.effective_vehicle_config({**_BASE, "period_overrides": None}, D) == (
        _BASE
    )


def test_resolution_is_idempotent():
    cfg = _vehicle(_override(to="2026-08-01"), _override(**{"from": "2026-09-01"}))
    for when in (None, D, dt.date(2026, 8, 15), dt.date(2026, 9, 1)):
        once = configs.effective_vehicle_config(cfg, when)
        assert configs.effective_vehicle_config(once, when) == once
        # ... and a resolved entry resolved for any other date is itself.
        assert configs.effective_vehicle_config(once, dt.date(2000, 1, 1)) == once


def test_resolution_is_pure():
    cfg = _vehicle(_OVERRIDE)
    before = copy.deepcopy(cfg)
    result = configs.effective_vehicle_config(cfg, D)
    assert cfg == before
    assert result is not cfg
    result["pipeline"] = "changed"
    assert cfg["period_overrides"][0]["set"]["pipeline"] == "ut_speed"


def test_an_override_can_switch_a_setting_off_again():
    cfg = _vehicle(
        _override(set={"split_long_stops_min": None}), split_long_stops_min=45
    )
    assert configs.effective_vehicle_config(cfg, D)["split_long_stops_min"] is None
    assert configs.effective_vehicle_config(cfg, D - dt.timedelta(days=1))[
        "split_long_stops_min"
    ] == (45)


@pytest.mark.parametrize(
    "when, expected",
    [
        # 00:30 in UTC+1 on the 7th is 23:30 UTC on the 6th: the base applies.
        (
            dt.datetime(2026, 7, 7, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=1))),
            BASE_SETTINGS,
        ),
        (dt.datetime(2026, 7, 7, 0, 0, tzinfo=dt.timezone.utc), OVERRIDE_SETTINGS),
        (dt.datetime(2026, 7, 6, 23, 59, 59), BASE_SETTINGS),  # naive: UTC
        (pd.Timestamp("2026-07-07T00:00:00Z"), OVERRIDE_SETTINGS),
        (pd.Timestamp("2026-07-06T23:59:00Z"), BASE_SETTINGS),
        (pd.NaT, BASE_SETTINGS),
        (None, BASE_SETTINGS),
    ],
)
def test_a_datetime_counts_by_its_utc_date(when, expected):
    assert _settings(configs.effective_vehicle_config(_vehicle(_OVERRIDE), when)) == (
        expected
    )


def test_a_date_given_as_text_is_refused():
    with pytest.raises(TypeError, match="'when' must be a date"):
        configs.effective_vehicle_config(_vehicle(_OVERRIDE), FROM)


# ── The leg's date ───────────────────────────────────────────────────────────


def _frame(*stamps):
    return pd.DataFrame({"eventDatetime": list(stamps), "x": range(len(stamps))})


@pytest.mark.parametrize(
    "stamps, expected",
    [
        (["2026-07-06T23:59:00.000Z", "2026-07-07T00:10:00.000Z"], dt.date(2026, 7, 6)),
        (["", "not a time", "2026-07-07T00:10:00.000Z"], dt.date(2026, 7, 7)),
        # In frame order, not the earliest.
        (["2026-07-08T01:00:00.000Z", "2026-07-07T23:00:00.000Z"], dt.date(2026, 7, 8)),
        (["2026-07-07T00:30:00+01:00"], dt.date(2026, 7, 6)),  # to UTC
        (["2026-07-07 00:30:00"], dt.date(2026, 7, 7)),  # naive: UTC
        (["", "nonsense"], None),
    ],
)
# A frame opening with a non-timestamp makes pandas parse element by element.
@pytest.mark.filterwarnings("ignore:Could not infer format")
def test_the_leg_date_is_the_utc_date_of_the_first_valid_timestamp(stamps, expected):
    assert frame_utc_date(_frame(*stamps)) == expected


def test_a_frame_without_a_time_column_has_no_date():
    assert frame_utc_date(pd.DataFrame({"x": [1]})) is None
    assert frame_utc_date(None) is None


# ── resolve_mass_agg for a leg's date ────────────────────────────────────────


@pytest.fixture
def injected(monkeypatch):
    for name, cfg in _PIPELINES.items():
        monkeypatch.setitem(constants.PIPELINE_CONFIGS, name, cfg)

    def _inject(cfg):
        monkeypatch.setitem(constants.VEHICLE_CONFIG, REG, cfg)

    return _inject


def test_the_mass_estimator_follows_an_overridden_pipeline(injected):
    injected(_vehicle(_OVERRIDE))  # ut_soc (no mass_agg) -> ut_speed (iqr_median)
    assert resolve_mass_agg(REG, when=D - dt.timedelta(days=1)) == "mean"
    assert resolve_mass_agg(REG, when=D) == "iqr_median"


def test_the_mass_estimator_follows_an_overridden_mass_agg(injected):
    injected(_vehicle(_override(set={"mass_agg": "median"}), mass_agg="mad_mean"))
    assert resolve_mass_agg(REG, when=D - dt.timedelta(days=1)) == "mad_mean"
    assert resolve_mass_agg(REG, when=D) == "median"
    # A pipeline passed in is still outranked by the vehicle's own estimator.
    assert resolve_mass_agg(REG, _PIPELINES["ut_speed"], when=D) == "median"


def test_without_a_date_the_mass_estimator_is_the_base_one(injected):
    injected(_vehicle(_override(set={"mass_agg": "median"})))
    assert resolve_mass_agg(REG) == "mean"
    assert resolve_mass_agg(REG, when=None) == "mean"
