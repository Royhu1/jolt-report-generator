"""The external capacity ledger (``JOLT_CAPACITY_LEDGER``): write-back and backfill.

With the variable set, the effective-capacity ledger — the two keys
``effective_capacity_kwh`` / ``effective_capacity_quarterly`` — is machine-written
state kept OUT of ``vehicles.json``: ``_persist_effective_capacity`` and
``capacity_backfill`` write the ledger file, and ``vehicles.json`` is only ever
read. Without it the write-back lands in ``vehicles.json`` exactly as it always
has; those semantics are pinned in ``test_capacity_ledger.py`` and re-checked
byte for byte at the bottom of this module.

The numbers themselves must not depend on the write target, so the two modes are
also run side by side on the same inputs and compared. The ledger file itself is
replaced atomically, never rewritten in place, so an interrupted write cannot
destroy the capacity history it holds.
"""

from __future__ import annotations

import copy
import datetime
import errno
import json
import os
import stat

import pytest

from report_generator import capacity_backfill as cb
from report_generator import configs
from report_generator.capacity import (
    _IDX_CAP,
    _IDX_DISTANCE,
    _IDX_ESOURCE,
    _IDX_SOC_CHANGE,
    _merge_period_capacity,
    _persist_effective_capacity,
)
from report_generator.columns import HEADERS, _row_col_index
from report_generator.general_pipeline import is_runtime_config
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


def _make_config_dir(path, monkeypatch, payload=PAYLOAD):
    path.mkdir(parents=True)
    (path / "vehicles.json").write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
    (path / "pipelines.json").write_text("{}\n", "utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(path))
    # Mirror the file into the in-memory config the way a real run has it;
    # monkeypatch removes the aliases again at teardown.
    for reg, cfg in payload.items():
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


def test_a_first_write_is_seeded_from_vehicles_json_not_from_memory(
    config_dir, ledger_path, monkeypatch
):
    # The in-memory entry is deliberately different from the file on disk — as
    # after an earlier report wrote into another ledger — to show which is used:
    # what is written never depends on memory.
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
        "20241001_20250101",
        "20250101_20250401",
    }
    # (300*10 + 400*10) / 20 = 350.0
    assert entry["effective_capacity_kwh"] == 350.0


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
    # Merging the new period never reaches back into the history the in-memory
    # config held: that dict is replaced by the merged one, not changed.
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


def test_an_entry_holding_only_the_scalar_keeps_the_quarterly_history(
    tmp_path, monkeypatch
):
    # The reports read this vehicle's quarterly history from vehicles.json: the
    # ledger entry carries only the scalar, and the overlay is key by key.
    two_periods = {
        "20240701_20241001": {"kwh": 300.0, "n": 10},
        "20241001_20250101": {"kwh": 900.0, "n": 2},  # sparse
    }
    payload = {
        REG: {
            "srf_reg": REG,
            "nominal_kwh": 540,
            "effective_capacity_kwh": 300.0,
            "effective_capacity_quarterly": two_periods,
        }
    }
    config = _make_config_dir(tmp_path / "configs", monkeypatch, payload)
    ledger = tmp_path / "state" / "capacity_ledger.json"
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(ledger))
    _write(ledger, {REG: {"effective_capacity_kwh": 410.0}})
    before = (config / "vehicles.json").read_bytes()

    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")

    entry = _read(ledger)[REG]
    assert set(entry["effective_capacity_quarterly"]) == {
        "20240701_20241001",
        "20241001_20250101",
        "20250101_20250401",
    }
    # Over the reliable periods only: (300*10 + 400*10) / 20 = 350.0 — not the
    # new period alone (400.0), which is what a merge from nothing would give.
    assert entry["effective_capacity_kwh"] == 350.0
    # The sparse period is excluded from the average and backfilled to it.
    assert entry["effective_capacity_quarterly"]["20241001_20250101"] == {
        "kwh": 350.0,
        "n": 2,
    }
    assert (config / "vehicles.json").read_bytes() == before


@pytest.mark.parametrize(
    "ledger_entry",
    [
        None,  # no entry yet
        {"effective_capacity_kwh": 410.0},
        {
            "effective_capacity_quarterly": {
                "20240101_20240401": {"kwh": 420.0, "n": 20}
            }
        },
        {
            "effective_capacity_kwh": 420.0,
            "effective_capacity_quarterly": {
                "20240101_20240401": {"kwh": 420.0, "n": 20}
            },
        },
        # A key the entry carries is used as it is, even when it is null.
        {"effective_capacity_kwh": 410.0, "effective_capacity_quarterly": None},
    ],
    ids=["absent", "scalar-only", "quarterly-only", "full", "null-quarterly"],
)
def test_the_merge_continues_what_the_loader_shows_the_reports(
    config_dir, ledger_path, monkeypatch, ledger_entry
):
    if ledger_entry is not None:
        _write(ledger_path, {REG: ledger_entry})
    # What a report started now reads: vehicles.json with the ledger overlaid.
    loaded = configs.load_vehicle_configs()[REG]
    expected = {k: copy.deepcopy(loaded[k]) for k in configs.LEDGER_KEYS if k in loaded}
    _merge_period_capacity(expected, 400.0, 10, "20250101_20250401")
    # Whatever memory holds — here another ledger's history — changes nothing.
    monkeypatch.setitem(constants.VEHICLE_CONFIG, REG, copy.deepcopy(_STALE))

    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")

    assert _read(ledger_path)[REG] == expected


# ── The ledger changes while the process runs ────────────────────────────────

_STALE = {
    "srf_reg": REG,
    "effective_capacity_kwh": 450.0,
    "effective_capacity_quarterly": {
        "20240401_20240701": {"kwh": 450.0, "n": 20},
        "20240701_20241001": {"kwh": 460.0, "n": 20},
    },
}
_LEDGER_A = {
    REG: {
        "effective_capacity_kwh": 450.0,
        "effective_capacity_quarterly": {"20240401_20240701": {"kwh": 450.0, "n": 20}},
    }
}


def _report_start():
    """What ``JOLTReportGenerator.generate_report`` does before it reads a config."""
    configs.apply_capacity_ledger(constants.VEHICLE_CONFIG, skip=is_runtime_config)


def _run_a_report_on_ledger_a(ledger):
    """Report 1: ledger A holds its own history, and the report adds a period."""
    _write(ledger, _LEDGER_A)
    _report_start()
    _persist_effective_capacity(REG, 460.0, 20, "charge", "20240701_20241001")
    # Memory now holds ledger A's history, which vehicles.json does not have.
    assert constants.VEHICLE_CONFIG[REG]["effective_capacity_quarterly"] == (
        _STALE["effective_capacity_quarterly"]
    )


def _assert_report_2_continues_vehicles_json(ledger):
    """Report 2 on a ledger holding only the scalar: vehicles.json's history."""
    _report_start()
    live = constants.VEHICLE_CONFIG[REG]
    assert live["effective_capacity_kwh"] == 410.0
    assert live["effective_capacity_quarterly"] == HISTORY

    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")

    entry = _read(ledger)[REG]
    # vehicles.json's history plus the new period — none of ledger A's periods.
    assert set(entry["effective_capacity_quarterly"]) == {
        "20241001_20250101",
        "20250101_20250401",
    }
    # (300*10 + 400*10) / 20 = 350.0
    assert entry["effective_capacity_kwh"] == 350.0


def test_a_ledger_named_later_never_receives_the_history_of_the_last(
    config_dir, tmp_path, monkeypatch
):
    ledger_a = tmp_path / "a" / "capacity_ledger.json"
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(ledger_a))
    _run_a_report_on_ledger_a(ledger_a)
    a_after_report_1 = ledger_a.read_bytes()

    ledger_b = tmp_path / "b" / "capacity_ledger.json"
    _write(ledger_b, {REG: {"effective_capacity_kwh": 410.0}})
    monkeypatch.setenv("JOLT_CAPACITY_LEDGER", str(ledger_b))
    _assert_report_2_continues_vehicles_json(ledger_b)

    assert ledger_a.read_bytes() == a_after_report_1


def test_a_ledger_edited_down_to_the_scalar_between_two_reports(
    config_dir, ledger_path
):
    _run_a_report_on_ledger_a(ledger_path)
    _write(ledger_path, {REG: {"effective_capacity_kwh": 410.0}})
    _assert_report_2_continues_vehicles_json(ledger_path)


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


# ── Writing the ledger file: atomically, and byte for byte as before ─────────

_LEDGER = {
    REG: {
        "effective_capacity_kwh": 350.0,
        "effective_capacity_quarterly": {
            "20241001_20250101": {"kwh": 300.0, "n": 10},
            "20250101_20250401": {"kwh": 400.0, "n": 10},
        },
    },
    OTHER: {"effective_capacity_kwh": None, "note": "Zürich – dépôt"},  # non-ASCII
}


def _direct_write(path, ledger):
    """The in-place writer the atomic one replaced: the byte-format reference."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _temporaries(directory):
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def _refused(attempts):
    """An ``os.replace`` stand-in that is always refused, as Windows may be."""

    def replace(source, target):
        attempts.append((source, target))
        raise PermissionError(errno.EACCES, "The process cannot access the file")

    return replace


def test_the_ledger_file_holds_the_bytes_a_direct_write_gives(tmp_path):
    reference, written = tmp_path / "reference.json", tmp_path / "ledger.json"
    _direct_write(reference, _LEDGER)
    configs._write_capacity_ledger(written, _LEDGER)
    assert written.read_bytes() == reference.read_bytes()
    assert _temporaries(tmp_path) == []


def test_rewriting_a_longer_ledger_leaves_nothing_of_it(tmp_path):
    reference, written = tmp_path / "reference.json", tmp_path / "ledger.json"
    longer = {f"REG{i:02d}": _LEDGER[REG] for i in range(20)}
    configs._write_capacity_ledger(written, longer)
    configs._write_capacity_ledger(written, _LEDGER)
    _direct_write(reference, _LEDGER)
    assert written.read_bytes() == reference.read_bytes()


def test_a_write_that_fails_part_way_leaves_the_old_ledger_whole(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    _direct_write(path, _LEDGER)
    before = path.read_bytes()

    def dump_part_then_fail(_obj, fh, **_kwargs):
        fh.write('{\n  "LEDGER01": {\n    "effective_capa')  # part of the file ...
        raise OSError(errno.ENOSPC, "No space left on device")  # ... then a full disk

    monkeypatch.setattr(configs.json, "dump", dump_part_then_fail)
    with pytest.raises(OSError, match="No space left"):
        configs._write_capacity_ledger(path, {REG: {"effective_capacity_kwh": 1.0}})

    assert path.read_bytes() == before
    assert _temporaries(tmp_path) == []


def test_a_write_back_that_fails_part_way_keeps_the_history(
    config_dir, ledger_path, monkeypatch
):
    _write(ledger_path, _LEDGER)
    before = ledger_path.read_bytes()
    in_memory = copy.deepcopy(constants.VEHICLE_CONFIG[REG])

    def dump_part_then_fail(_obj, fh, **_kwargs):
        fh.write("{")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(configs.json, "dump", dump_part_then_fail)
    with pytest.raises(OSError):
        _persist_effective_capacity(REG, 480.0, 20, "charge", "20250401_20250701")

    assert ledger_path.read_bytes() == before
    assert _temporaries(ledger_path.parent) == []
    # Nothing was persisted, so nothing is mirrored into memory either.
    assert constants.VEHICLE_CONFIG[REG] == in_memory


def test_a_replace_refused_twice_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    _direct_write(path, {"OLD01": {"effective_capacity_kwh": 1.0}})
    real_replace = os.replace
    attempts, pauses = [], []

    def flaky_replace(source, target):
        attempts.append((source, target))
        if len(attempts) <= 2:
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, target)

    monkeypatch.setattr(configs.os, "replace", flaky_replace)
    monkeypatch.setattr(configs.time, "sleep", pauses.append)
    configs._write_capacity_ledger(path, _LEDGER)

    assert len(attempts) == 3
    assert pauses == [configs._LEDGER_REPLACE_DELAY_S] * 2
    assert json.loads(path.read_text(encoding="utf-8")) == _LEDGER
    assert _temporaries(tmp_path) == []


def test_a_replace_that_stays_refused_raises_and_leaves_no_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "ledger.json"
    _direct_write(path, _LEDGER)
    before = path.read_bytes()
    attempts = []
    monkeypatch.setattr(configs.os, "replace", _refused(attempts))
    monkeypatch.setattr(configs.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        configs._write_capacity_ledger(path, {REG: {"effective_capacity_kwh": 1.0}})

    assert len(attempts) == configs._LEDGER_REPLACE_ATTEMPTS == 5
    assert path.read_bytes() == before
    assert _temporaries(tmp_path) == []


def test_any_other_replace_error_is_raised_at_once(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    attempts = []

    def busy(source, target):
        attempts.append((source, target))
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(configs.os, "replace", busy)
    with pytest.raises(OSError, match="busy"):
        configs._write_capacity_ledger(path, _LEDGER)
    assert len(attempts) == 1
    assert not path.exists()
    assert _temporaries(tmp_path) == []


def test_a_rewritten_ledger_keeps_its_permission_bits(tmp_path):
    path = tmp_path / "ledger.json"
    _direct_write(path, _LEDGER)
    os.chmod(path, 0o640)  # on Windows only the read-only flag maps onto these
    before = stat.S_IMODE(os.stat(path).st_mode)
    configs._write_capacity_ledger(path, {REG: {"effective_capacity_kwh": 1.0}})
    assert stat.S_IMODE(os.stat(path).st_mode) == before


def test_a_new_ledger_gets_the_permission_bits_of_a_direct_write(tmp_path):
    reference, written = tmp_path / "reference.json", tmp_path / "ledger.json"
    _direct_write(reference, _LEDGER)
    configs._write_capacity_ledger(written, _LEDGER)
    assert stat.S_IMODE(os.stat(written).st_mode) == stat.S_IMODE(
        os.stat(reference).st_mode
    )


def test_a_refused_write_over_a_read_only_ledger_leaves_no_temporary(
    tmp_path, monkeypatch
):
    # The temporary file takes the ledger's read-only mode; Windows cannot
    # delete a read-only file, so the clean-up has to make it writable first.
    path = tmp_path / "ledger.json"
    _direct_write(path, _LEDGER)
    os.chmod(path, stat.S_IREAD)
    try:
        monkeypatch.setattr(configs.os, "replace", _refused([]))
        monkeypatch.setattr(configs.time, "sleep", lambda _seconds: None)
        with pytest.raises(PermissionError):
            configs._write_capacity_ledger(path, {REG: {}})
        assert _temporaries(tmp_path) == []
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


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


def test_backfill_writes_a_partial_ledger_entry_out_in_full(
    config_dir, ledger_path, tmp_path
):
    # The rebuild does not merge into the entry it finds: it replaces the
    # history with the one reconstructed from the report library, so an entry
    # holding only the scalar comes back with both keys — the vehicles.json
    # path's result.
    _write(ledger_path, {REG: {"effective_capacity_kwh": 333.3}})
    cb.main(["--report-db", str(_report_db(tmp_path))])
    assert _read(ledger_path)[REG] == _REBUILT


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
