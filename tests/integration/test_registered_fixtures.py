"""Every registered raw fixture is guarded — including each onboarded vehicle's.

``tests/fixtures/raw_fixtures.json`` lists every committed raw fixture, and
``tests/fixtures/make_fixture.py`` appends one when a vehicle is onboarded. This
module runs the same checks over all of them, so adding a fixture (plus its
frozen config and its golden) is all it takes to put a new vehicle under the
suite's guard:

* the segmentation of an EV fixture — every field of every charge and discharge
  segment — matches its golden, deterministically, and satisfies the contract a
  consumer relies on (required keys, chronology, sign convention, allowed energy
  sources); a diesel fixture's trips match their golden;
* the fixture is de-identified: no driver column, the vehicle identity is the
  alias, and the alias is not a live registration;
* the registry, the files on disk, the frozen configs and the goldens agree.

The four original fixtures additionally carry hand-written expectations in
``test_segmentation_fixtures.py`` and ``test_diesel_pipeline_fixture.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REGISTRY = json.loads((FIXTURES / "raw_fixtures.json").read_text(encoding="utf-8"))
EV_ALIASES = sorted(a for a, e in REGISTRY.items() if e["kind"] == "ev")
DIESEL_ALIASES = sorted(a for a, e in REGISTRY.items() if e["kind"] == "diesel")

_DRIVER_COLUMN = re.compile(r"^driver\d*_|\bdriver \d+\b", re.IGNORECASE)

CHARGE_KEYS = {
    "start_time",
    "end_time",
    "start_soc",
    "end_soc",
    "delta_soc_pct",
    "delta_energy_kwh",
    "energy_source",
    "effective_capacity_kwh",
    "charge_type",
}
DISCHARGE_KEYS = {
    "start_time",
    "end_time",
    "start_soc",
    "end_soc",
    "delta_soc_pct",
    "delta_energy_kwh",
    "energy_source",
    "effective_capacity_kwh",
    "odo_start_km",
    "odo_end_km",
    "ep_audit",
}
CHARGE_SOURCES = {"ac_dc", "soc_estimate"}
DISCHARGE_SOURCES = {"total_energy", "moving_energy", "soc_estimate"}


def _golden_name(alias: str) -> str:
    kind = REGISTRY[alias]["kind"]
    return f"segments_{alias}.json" if kind == "ev" else f"diesel_segments_{alias}.json"


def _naive(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert(None) if ts.tzinfo is not None else ts


# ── The registry agrees with the tree ────────────────────────────────────────


def test_every_raw_file_is_registered_and_every_entry_exists():
    on_disk = {
        p.relative_to(FIXTURES).as_posix() for p in (FIXTURES / "raw").rglob("*.csv")
    }
    assert on_disk == {e["path"] for e in REGISTRY.values()}
    for alias, entry in REGISTRY.items():
        assert entry["kind"] in {"ev", "diesel"}, alias
        assert entry["path"].startswith(f"raw/{alias}/"), alias


def test_every_registered_fixture_has_a_frozen_config_and_a_golden(
    frozen_config_data,
):
    vehicles = frozen_config_data["vehicles"]
    for alias in REGISTRY:
        assert alias in vehicles, f"{alias}: no frozen config"
        assert (FIXTURES / "expected" / _golden_name(alias)).exists(), alias
    goldens = {p.name for p in (FIXTURES / "expected").glob("*.json")}
    assert goldens == {_golden_name(a) for a in REGISTRY}


@pytest.mark.parametrize("alias", sorted(REGISTRY))
def test_the_fixture_is_de_identified(alias, frozen_config_data):
    from report_generator.configs import _load_config_json

    kind = REGISTRY[alias]["kind"]
    frame = pd.read_csv(
        FIXTURES / REGISTRY[alias]["path"],
        dtype=str,
        keep_default_na=False,
        index_col=0 if kind == "diesel" else None,
    )
    assert not [c for c in frame.columns if _DRIVER_COLUMN.search(c)]
    for col in ("vehicleId", "VIN vehicle identification number"):
        if col in frame.columns:
            assert set(frame[col]) <= {alias, ""}, col
    # An alias is never a live registration, and its frozen config says so.
    assert alias not in _load_config_json("vehicles.json")
    assert frozen_config_data["vehicles"][alias]["srf_reg"] == alias


# ── EV fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(params=EV_ALIASES)
def ev_segments(request, run_fixture_segmentation):
    charge, discharge = run_fixture_segmentation(request.param)
    return request.param, charge, discharge


def test_ev_segmentation_matches_its_golden(
    ev_segments, load_golden, serialise, raw_fixture_map
):
    alias, charge, discharge = ev_segments
    golden = load_golden(_golden_name(alias))
    assert golden["alias"] == alias
    assert golden["source"] == raw_fixture_map[alias]
    assert serialise(charge) == golden["charge"]
    assert serialise(discharge) == golden["discharge"]


@pytest.mark.parametrize("alias", EV_ALIASES)
def test_ev_segmentation_is_deterministic(alias, run_fixture_segmentation, serialise):
    first, second = run_fixture_segmentation(alias), run_fixture_segmentation(alias)
    assert serialise(first[0]) == serialise(second[0])
    assert serialise(first[1]) == serialise(second[1])


def test_ev_segments_satisfy_the_consumer_contract(ev_segments):
    alias, charge, discharge = ev_segments
    assert discharge, f"{alias}: a fixture with no trip guards nothing"
    for seg in charge:
        assert CHARGE_KEYS <= set(seg), CHARGE_KEYS - set(seg)
        assert seg["energy_source"] in CHARGE_SOURCES
        assert seg["delta_soc_pct"] > 0 and seg["delta_energy_kwh"] > 0
    for seg in discharge:
        assert DISCHARGE_KEYS <= set(seg), DISCHARGE_KEYS - set(seg)
        assert seg["energy_source"] in DISCHARGE_SOURCES
        assert seg["delta_soc_pct"] < 0 and seg["delta_energy_kwh"] < 0
    for group in (charge, discharge):
        starts = [_naive(s["start_time"]) for s in group]
        assert starts == sorted(starts)
        for seg in group:
            assert _naive(seg["start_time"]) <= _naive(seg["end_time"])


# ── Diesel fixtures ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", DIESEL_ALIASES)
def test_diesel_trips_match_their_golden(
    alias, frozen_configs, raw_fixture_path, load_golden, serialise, raw_fixture_map
):
    from report_generator import diesel_pipeline as dp

    cfg = frozen_configs["vehicles"][alias]
    path = raw_fixture_path(alias)
    frame = dp._logger_df_from_csv(path, cfg)
    assert frame is not None, f"{alias}: the logger fixture must rebuild"
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source=path.name)
    assert seg_metrics, f"{alias}: a fixture with no trip guards nothing"
    golden = load_golden(_golden_name(alias))
    assert golden["alias"] == alias
    assert golden["source"] == raw_fixture_map[alias]
    assert serialise(seg_metrics) == golden["trips"]
