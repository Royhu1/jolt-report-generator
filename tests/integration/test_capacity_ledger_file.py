"""The external capacity ledger (``JOLT_CAPACITY_LEDGER``): write-back and backfill.

With the variable set, the effective-capacity ledger — the two keys
``effective_capacity_kwh`` / ``effective_capacity_quarterly`` — is machine-written
state kept OUT of ``vehicles.json``: ``_persist_effective_capacity`` and
``capacity_backfill`` write the ledger file, and ``vehicles.json`` is only ever
read. Without it the write-back lands in ``vehicles.json`` exactly as it always
has; those semantics are pinned in ``test_capacity_ledger.py`` and re-checked
byte for byte at the bottom of this module.

The numbers themselves must not depend on the write target, so the two modes are
also run side by side on the same inputs and compared.
"""

from __future__ import annotations

import datetime
import json

import pytest

from report_generator import capacity_backfill as cb
from report_generator import configs
from report_generator.capacity import (
    _IDX_CAP,
    _IDX_DISTANCE,
    _IDX_ESOURCE,
    _IDX_SOC_CHANGE,
    _persist_effective_capacity,
)
from report_generator.columns import HEADERS, _row_col_index
from report_generator.report_builder import _write_excel_report
from report_generator.segmentation import constants

REG = "LEDGER01"
OTHER = "OTHER01"
DIESEL = "DIESEL01"
HISTORY = {"20241001_20250101": {"kwh": 300.0, "n": 10}}

#: The vehicles.json every test starts from: one EV with a capacity history,
#: one EV without, one diesel.
PAYLOAD = {
    REG: {
        "srf_reg": REG,
        "nominal_kwh": 540,
        "effective_capacity_kwh": 300.0,
        "effective_capacity_quarterly": HISTORY,
    },
    OTHER: {"srf_reg": OTHER, "effective_capacity_kwh": 123.4},
    DIESEL: {"srf_reg": DIESEL, "fuel_type": "DIESEL", "weight_class_t": 44.0},
}


def _make_config_dir(path, monkeypatch):
    path.mkdir(parents=True)
    (path / "vehicles.json").write_text(json.dumps(PAYLOAD, indent=2) + "\n", "utf-8")
    (path / "pipelines.json").write_text("{}\n", "utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(path))
    # Mirror the file into the in-memory config the way a real run has it;
    # monkeypatch removes the aliases again at teardown.
    for reg, cfg in PAYLOAD.items():
        monkeypatch.setitem(constants.VEHICLE_CONFIG, reg, json.loads(json.dumps(cfg)))
    return path


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    """A writable config directory; no external ledger configured."""
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    return _make_config_dir(tmp_path / "configs", monkeypatch)


@pytest.fixture
def ledger_path(monkeypatch, tmp_path, config_dir):
    """``JOLT_CAPACITY_LEDGER`` pointed at a not-yet-existing file in a new dir."""
    path = tmp_path / "state" / "capacity_ledger.json"
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(path))
    return path


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")


# ── _persist_effective_capacity into the ledger ──────────────────────────────


def test_persist_writes_the_ledger_and_never_vehicles_json(config_dir, ledger_path):
    before = (config_dir / "vehicles.json").read_bytes()

    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")

    assert (config_dir / "vehicles.json").read_bytes() == before
    assert _read(ledger_path) == {
        REG: {
            # The seeded history continues: (300*10 + 400*10) / 20 = 350.0
            "effective_capacity_kwh": 350.0,
            "effective_capacity_quarterly": {
                "20241001_20250101": {"kwh": 300.0, "n": 10},
                "20250101_20250401": {"kwh": 400.0, "n": 10},
            },
        }
    }


def test_persist_writes_the_vehicles_json_format(config_dir, ledger_path):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    text = ledger_path.read_text(encoding="utf-8")
    assert text == json.dumps(_read(ledger_path), indent=2, ensure_ascii=False) + "\n"


def test_a_first_write_is_seeded_from_the_in_memory_config(
    config_dir, ledger_path, monkeypatch
):
    # The in-memory entry (vehicles.json as loaded, with any overlay) is what a
    # first ledger write continues from — here deliberately different from the
    # file on disk, to show which one is used.
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        REG,
        {
            "srf_reg": REG,
            "effective_capacity_kwh": 500.0,
            "effective_capacity_quarterly": {
                "20240701_20241001": {"kwh": 500.0, "n": 30}
            },
        },
    )
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    entry = _read(ledger_path)[REG]
    assert set(entry["effective_capacity_quarterly"]) == {
        "20240701_20241001",
        "20250101_20250401",
    }
    # (500*30 + 400*10) / 40 = 475.0
    assert entry["effective_capacity_kwh"] == 475.0


def test_a_vehicle_absent_from_memory_is_seeded_from_vehicles_json(
    config_dir, ledger_path, monkeypatch
):
    # E.g. added to vehicles.json after this process loaded its configs.
    monkeypatch.delitem(constants.VEHICLE_CONFIG, REG)
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    entry = _read(ledger_path)[REG]
    assert set(entry["effective_capacity_quarterly"]) == {
        "20241001_20250101",
        "20250101_20250401",
    }
    assert entry["effective_capacity_kwh"] == 350.0


def test_a_seeded_history_is_copied_not_shared(config_dir, ledger_path):
    # The seed is a copy: merging the new period into it must not reach back
    # into the in-memory dict it was taken from.
    seeded_from = constants.VEHICLE_CONFIG[REG]["effective_capacity_quarterly"]
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    assert seeded_from == HISTORY
    # The in-memory entry is then re-synced to the merged result.
    assert constants.VEHICLE_CONFIG[REG]["effective_capacity_quarterly"] == (
        _read(ledger_path)[REG]["effective_capacity_quarterly"]
    )


def test_an_existing_ledger_entry_is_merged_not_reseeded(
    config_dir, ledger_path, monkeypatch
):
    _write(
        ledger_path,
        {
            REG: {
                "effective_capacity_kwh": 420.0,
                "effective_capacity_quarterly": {
                    "20240101_20240401": {"kwh": 420.0, "n": 20}
                },
            }
        },
    )
    _persist_effective_capacity(REG, 480.0, 20, "charge", "20250101_20250401")
    quarterly = _read(ledger_path)[REG]["effective_capacity_quarterly"]
    # The ledger's own history, not the vehicles.json / in-memory one.
    assert set(quarterly) == {"20240101_20240401", "20250101_20250401"}
    # (420*20 + 480*20) / 40 = 450.0
    assert _read(ledger_path)[REG]["effective_capacity_kwh"] == 450.0


def test_persist_keeps_every_other_ledger_entry(config_dir, ledger_path):
    others = {
        OTHER: {"effective_capacity_kwh": 111.1},
        "RETIRED01": {"effective_capacity_kwh": 222.2},  # no longer configured
    }
    _write(ledger_path, others)
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    ledger = _read(ledger_path)
    assert {k: ledger[k] for k in others} == others
    assert REG in ledger


def test_persist_takes_the_ledger_lock(config_dir, ledger_path, monkeypatch):
    seen = []
    real = configs._ledger_lock

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(configs, "_ledger_lock", spy)
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    assert seen == [ledger_path]
    assert str(real(ledger_path).lock_file) == str(ledger_path) + ".lock"
    # vehicles.json is not written, so it is not locked either.
    assert not (config_dir / "vehicles.json.lock").exists()


def test_persist_ignores_a_registration_that_is_not_in_vehicles_json(
    config_dir, ledger_path, monkeypatch
):
    # Present in memory (as a runtime fallback config would be) but not on disk.
    monkeypatch.setitem(constants.VEHICLE_CONFIG, "NOTINFILE", {"srf_reg": "X"})
    _persist_effective_capacity("NOTINFILE", 400.0, 10, "charge", "20250101_20250401")
    assert not ledger_path.exists()
    assert constants.VEHICLE_CONFIG["NOTINFILE"] == {"srf_reg": "X"}


@pytest.mark.parametrize(
    "eff_cap, source",
    [(410.0, "fallback"), (None, "charge"), (None, "fallback")],
)
def test_persist_without_a_measured_donor_writes_nothing(
    config_dir, ledger_path, eff_cap, source
):
    _persist_effective_capacity(REG, eff_cap, 0, source, "20250101_20250401")
    assert not ledger_path.parent.exists()
    assert constants.VEHICLE_CONFIG[REG]["effective_capacity_quarterly"] == HISTORY


def test_persist_updates_the_in_memory_config(config_dir, ledger_path):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    live = constants.VEHICLE_CONFIG[REG]
    assert live["effective_capacity_kwh"] == 350.0
    assert live["effective_capacity_quarterly"]["20250101_20250401"]["n"] == 10


def test_a_fresh_load_after_persist_sees_the_ledger_values(config_dir, ledger_path):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    reloaded = configs.load_vehicle_configs()
    assert reloaded[REG]["effective_capacity_kwh"] == 350.0
    # ... while the parameters still come from vehicles.json.
    assert reloaded[REG]["nominal_kwh"] == 540


def test_a_damaged_ledger_is_never_overwritten(config_dir, ledger_path):
    ledger_path.parent.mkdir()
    ledger_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    assert ledger_path.read_text(encoding="utf-8") == "[]"


# ── The two write targets compute the same numbers ───────────────────────────

_SEQUENCE = [
    (400.0, 10, "charge", "20250101_20250401"),
    (500.0, 30, "discharge", "20250401_20250701"),
    (900.0, 2, "charge", "20250701_20251001"),  # sparse: backfilled to the average
    (430.0, 11, "charge", "20250101_20250401"),  # the same period again
]


def test_the_ledger_holds_exactly_what_vehicles_json_would_have(tmp_path, monkeypatch):
    # Mode 1: no ledger — the write-back lands in vehicles.json.
    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    plain_dir = _make_config_dir(tmp_path / "plain", monkeypatch)
    for call in _SEQUENCE:
        _persist_effective_capacity(REG, *call)
    in_vehicles_json = {
        k: _read(plain_dir / "vehicles.json")[REG][k] for k in configs.LEDGER_KEYS
    }

    # Mode 2: the same vehicles.json, an external ledger.
    ledger_dir = _make_config_dir(tmp_path / "ledgered", monkeypatch)
    ledger = tmp_path / "ledger.json"
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(ledger))
    for call in _SEQUENCE:
        _persist_effective_capacity(REG, *call)

    assert _read(ledger)[REG] == in_vehicles_json
    assert _read(ledger_dir / "vehicles.json") == PAYLOAD


# ── capacity_backfill into the ledger ────────────────────────────────────────


def _blank_row():
    """A NaN-filled EV row with the link columns blanked (``None``)."""
    row = [float("nan")] * (len(HEADERS) - 1)
    for link in ("Telematics Link", "Charger Link", "SRF Logger Link"):
        row[_row_col_index(link)] = None
    return row


def _donor_row(cap, soc):
    row = _blank_row()
    row[_row_col_index("Leg Type")] = "DC Away"
    row[_IDX_CAP] = cap
    row[_IDX_SOC_CHANGE] = soc
    row[_IDX_ESOURCE] = "ac_dc"
    row[_IDX_DISTANCE] = float("nan")
    return row


def _report_db(tmp_path):
    """A report tree with six 400 kWh donors for LEDGER01 and a diesel report."""
    db = tmp_path / "reports"
    for reg in (REG, DIESEL):
        (db / reg).mkdir(parents=True)
        _write_excel_report(
            [_donor_row(400.0, 40.0) for _ in range(6)],
            reg,
            datetime.date(2025, 1, 1),
            datetime.date(2025, 4, 1),
            db / reg / f"jolt_report_{reg}_20250101_20250401.xlsx",
            headers=HEADERS,
        )
    return db


_REBUILT = {
    "effective_capacity_kwh": 400.0,
    "effective_capacity_quarterly": {"20250101_20250401": {"kwh": 400.0, "n": 6}},
}


def test_backfill_writes_the_rebuilt_entry_into_the_ledger(
    config_dir, ledger_path, tmp_path
):
    db = _report_db(tmp_path)
    before = (config_dir / "vehicles.json").read_bytes()

    cb.main(["--report-db", str(db)])

    assert (config_dir / "vehicles.json").read_bytes() == before
    # The rebuild REPLACES the history (it is reconstructed from the report
    # library), exactly as the vehicles.json path does. The diesel vehicle and
    # the vehicle without reports get no entry.
    assert _read(ledger_path) == {REG: _REBUILT}


def test_backfill_keeps_the_ledger_entries_it_did_not_rebuild(
    config_dir, ledger_path, tmp_path
):
    kept = {
        OTHER: {"effective_capacity_kwh": 111.1},
        "RETIRED01": {"effective_capacity_kwh": 222.2},
    }
    _write(ledger_path, kept)
    cb.main(["--report-db", str(_report_db(tmp_path))])
    assert _read(ledger_path) == {**kept, REG: _REBUILT}


def test_backfill_summaries_start_from_the_ledger_values(
    config_dir, ledger_path, tmp_path
):
    _write(ledger_path, {REG: {"effective_capacity_kwh": 333.3}})
    summaries = cb.main(["--report-db", str(_report_db(tmp_path))])
    (summary,) = [s for s in summaries if s["reg"] == REG]
    assert (summary["old_kwh"], summary["new_kwh"]) == (333.3, 400.0)


def test_backfill_dry_run_writes_nothing(config_dir, ledger_path, tmp_path):
    db = _report_db(tmp_path)
    before = (config_dir / "vehicles.json").read_bytes()

    summaries = cb.main(["--report-db", str(db), "--dry-run"])

    # OTHER01 has no report directory and DIESEL01 is skipped.
    assert [s["reg"] for s in summaries] == [REG]
    assert summaries[0]["new_kwh"] == 400.0  # computed ...
    assert not ledger_path.parent.exists()  # ... but no ledger, lock or dir
    assert (config_dir / "vehicles.json").read_bytes() == before


def test_backfill_into_the_ledger_matches_the_vehicles_json_backfill(
    tmp_path, monkeypatch
):
    db = _report_db(tmp_path)

    monkeypatch.delenv("JOLT_CAPACITY_LEDGER", raising=False)
    plain_dir = _make_config_dir(tmp_path / "plain", monkeypatch)
    cb.main(["--report-db", str(db)])
    in_vehicles_json = {
        k: _read(plain_dir / "vehicles.json")[REG][k] for k in configs.LEDGER_KEYS
    }

    _make_config_dir(tmp_path / "ledgered", monkeypatch)
    ledger = tmp_path / "ledger.json"
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(ledger))
    cb.main(["--report-db", str(db)])

    assert _read(ledger)[REG] == in_vehicles_json == _REBUILT


# ── Without the variable nothing changes ─────────────────────────────────────


def test_without_the_variable_persist_writes_vehicles_json_as_it_always_has(
    config_dir, tmp_path
):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")

    expected = json.loads(json.dumps(PAYLOAD))
    expected[REG]["effective_capacity_quarterly"]["20250101_20250401"] = {
        "kwh": 400.0,
        "n": 10,
    }
    expected[REG]["effective_capacity_kwh"] = 350.0
    text = (config_dir / "vehicles.json").read_text(encoding="utf-8")
    assert text == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
    # No ledger file appears anywhere.
    assert sorted(p.name for p in tmp_path.rglob("*.json")) == [
        "pipelines.json",
        "vehicles.json",
    ]


def test_without_the_variable_backfill_writes_vehicles_json(config_dir, tmp_path):
    cb.main(["--report-db", str(_report_db(tmp_path))])
    entry = _read(config_dir / "vehicles.json")[REG]
    assert {k: entry[k] for k in configs.LEDGER_KEYS} == _REBUILT
    assert not list(tmp_path.rglob("capacity_ledger*"))
