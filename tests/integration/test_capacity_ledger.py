"""The ``vehicles.json`` capacity ledger: live write-back and xlsx backfill.

``_persist_effective_capacity`` is the ONLY place the package writes state back
to disk, so its contract matters to a deployer: it needs a writable
``JOLT_CONFIG_DIR``, it merges rather than overwrites, and it refuses to write
when the period had no measured donor.

``capacity_backfill.backfill_vehicle`` must reproduce exactly the same numbers
from a finished workbook, so a re-run and a backfill never disagree.
"""

from __future__ import annotations

import datetime
import json

import pytest

from report_generator import capacity_backfill as cb
from report_generator.capacity import (
    _IDX_CAP,
    _IDX_DISTANCE,
    _IDX_ESOURCE,
    _IDX_SOC_CHANGE,
    MIN_DONORS,
    _persist_effective_capacity,
)
from report_generator.columns import HEADERS, _row_col_index
from report_generator.report_builder import _write_excel_report
from report_generator.segmentation import constants

REG = "LEDGER01"


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    """A writable config directory holding a one-vehicle ``vehicles.json``."""
    path = tmp_path / "configs"
    path.mkdir()
    payload = {REG: {"srf_reg": REG, "effective_capacity_kwh": 300.0}}
    (path / "vehicles.json").write_text(json.dumps(payload, indent=2), "utf-8")
    monkeypatch.setenv("JOLT_CONFIG_DIR", str(path))
    # Mirror the on-disk entry into the in-memory config the way a real run has
    # it; monkeypatch removes the alias again at teardown.
    monkeypatch.setitem(constants.VEHICLE_CONFIG, REG, dict(payload[REG]))
    return path


def _read(config_dir):
    with open(config_dir / "vehicles.json", encoding="utf-8") as fh:
        return json.load(fh)


# ── _persist_effective_capacity ──────────────────────────────────────────────


def test_persist_writes_the_quarterly_entry_and_the_weighted_average(config_dir):
    _persist_effective_capacity(REG, 412.34, 12, "charge", "20250101_20250401")
    entry = _read(config_dir)[REG]
    assert entry["effective_capacity_quarterly"] == {
        "20250101_20250401": {"kwh": 412.3, "n": 12}
    }
    assert entry["effective_capacity_kwh"] == 412.3


def test_persist_merges_a_second_quarter_and_reweights(config_dir):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    _persist_effective_capacity(REG, 500.0, 30, "charge", "20250401_20250701")
    entry = _read(config_dir)[REG]
    assert set(entry["effective_capacity_quarterly"]) == {
        "20250101_20250401",
        "20250401_20250701",
    }
    # (400*10 + 500*30) / 40 = 475.0
    assert entry["effective_capacity_kwh"] == 475.0


def test_persist_backfills_a_sparse_quarter_to_the_average(config_dir):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    _persist_effective_capacity(REG, 500.0, 30, "charge", "20250401_20250701")
    _persist_effective_capacity(REG, 900.0, 2, "discharge", "20250701_20251001")
    quarterly = _read(config_dir)[REG]["effective_capacity_quarterly"]
    sparse = quarterly["20250701_20251001"]
    assert sparse["n"] == 2 < MIN_DONORS
    assert sparse["kwh"] == _read(config_dir)[REG]["effective_capacity_kwh"]
    # The sparse quarter is excluded from the average it is backfilled with.
    assert _read(config_dir)[REG]["effective_capacity_kwh"] == 475.0


def test_persist_overwrites_the_same_period_rather_than_appending(config_dir):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    _persist_effective_capacity(REG, 430.0, 11, "charge", "20250101_20250401")
    quarterly = _read(config_dir)[REG]["effective_capacity_quarterly"]
    assert quarterly == {"20250101_20250401": {"kwh": 430.0, "n": 11}}


def test_persist_updates_the_in_memory_config_too(config_dir):
    _persist_effective_capacity(REG, 412.0, 12, "charge", "20250101_20250401")
    live = constants.VEHICLE_CONFIG[REG]
    assert live["effective_capacity_kwh"] == 412.0
    assert live["effective_capacity_quarterly"]["20250101_20250401"]["n"] == 12


@pytest.mark.parametrize(
    "eff_cap, source",
    [(410.0, "fallback"), (None, "charge"), (None, "fallback")],
)
def test_persist_early_returns_without_a_measured_donor(config_dir, eff_cap, source):
    before = _read(config_dir)
    _persist_effective_capacity(REG, eff_cap, 0, source, "20250101_20250401")
    assert _read(config_dir) == before
    assert "effective_capacity_quarterly" not in constants.VEHICLE_CONFIG[REG]


def test_persist_ignores_a_registration_that_is_not_in_the_file(config_dir):
    before = _read(config_dir)
    _persist_effective_capacity("NOTINFILE", 400.0, 10, "charge", "20250101_20250401")
    assert _read(config_dir) == before
    assert "NOTINFILE" not in constants.VEHICLE_CONFIG


def test_persist_leaves_the_other_vehicles_untouched(config_dir):
    payload = _read(config_dir)
    payload["OTHER01"] = {"srf_reg": "OTHER01", "effective_capacity_kwh": 123.4}
    (config_dir / "vehicles.json").write_text(json.dumps(payload, indent=2), "utf-8")

    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    after = _read(config_dir)
    assert after["OTHER01"] == {"srf_reg": "OTHER01", "effective_capacity_kwh": 123.4}


def test_persist_writes_valid_utf8_json_with_a_trailing_newline(config_dir):
    _persist_effective_capacity(REG, 400.0, 10, "charge", "20250101_20250401")
    text = (config_dir / "vehicles.json").read_text(encoding="utf-8")
    assert text.endswith("\n")
    json.loads(text)


# ── capacity_backfill.backfill_vehicle ───────────────────────────────────────


def _blank_row():
    """A NaN-filled EV row with the link columns blanked.

    The link columns must be ``None`` (never NaN): ``_write_report_sheet`` tests
    them for truthiness, and NaN is truthy, so a NaN there would be handed to
    xlsxwriter's ``write_url``. Every production row builder already writes
    ``None`` for an absent link, so this mirrors reality.
    """
    row = [float("nan")] * (len(HEADERS) - 1)
    for link in ("Telematics Link", "Charger Link", "SRF Logger Link"):
        row[_row_col_index(link)] = None
    return row


def _donor_row(cap, soc, source="ac_dc", leg_type="DC Away"):
    row = _blank_row()
    row[_row_col_index("Leg Type")] = leg_type
    row[_IDX_CAP] = cap
    row[_IDX_SOC_CHANGE] = soc
    row[_IDX_ESOURCE] = source
    row[_IDX_DISTANCE] = float("nan")
    return row


def _stop_row():
    row = _blank_row()
    row[_row_col_index("Leg Type")] = "Stop"
    return row


def _write_report(directory, reg, period, rows):
    start, end = period.split("_")
    out = directory / f"jolt_report_{reg}_{start}_{end}.xlsx"
    _write_excel_report(
        rows,
        reg,
        datetime.date(int(start[:4]), int(start[4:6]), int(start[6:])),
        datetime.date(int(end[:4]), int(end[4:6]), int(end[6:])),
        out,
        headers=HEADERS,
    )
    return out


def test_backfill_reads_the_donor_capacity_out_of_a_generated_workbook(tmp_path):
    rows = [_donor_row(400.0, 40.0) for _ in range(6)] + [_stop_row()]
    _write_report(tmp_path, REG, "20250101_20250401", rows)

    entry = {"srf_reg": REG, "effective_capacity_kwh": 300.0}
    summary = cb.backfill_vehicle(REG, tmp_path, entry)

    assert summary["wrote"] is True
    assert summary["n_reliable"] == 1
    assert entry["effective_capacity_quarterly"] == {
        "20250101_20250401": {"kwh": 400.0, "n": 6}
    }
    assert entry["effective_capacity_kwh"] == 400.0


def test_backfill_mutates_the_entry_in_place(tmp_path):
    rows = [_donor_row(400.0, 40.0) for _ in range(6)]
    _write_report(tmp_path, REG, "20250101_20250401", rows)
    entry = {"srf_reg": REG}
    cb.backfill_vehicle(REG, tmp_path, entry)
    assert "effective_capacity_quarterly" in entry


def test_backfill_stop_rows_do_not_become_zero_capacity_donors(tmp_path):
    # A Stop row's Battery Capacity / Energy Source are =NA(); if they were read
    # as 0 they would drag the mean down.
    rows = [_donor_row(400.0, 40.0) for _ in range(6)] + [_stop_row() for _ in range(6)]
    _write_report(tmp_path, REG, "20250101_20250401", rows)
    entry = {}
    cb.backfill_vehicle(REG, tmp_path, entry)
    assert entry["effective_capacity_quarterly"]["20250101_20250401"] == {
        "kwh": 400.0,
        "n": 6,
    }


def test_backfill_weights_two_periods_by_donor_count(tmp_path):
    _write_report(
        tmp_path, REG, "20250101_20250401", [_donor_row(400.0, 40.0) for _ in range(10)]
    )
    _write_report(
        tmp_path, REG, "20250401_20250701", [_donor_row(500.0, 40.0) for _ in range(30)]
    )
    entry = {}
    summary = cb.backfill_vehicle(REG, tmp_path, entry)
    assert summary["n_reliable"] == 2
    assert entry["effective_capacity_kwh"] == 475.0


def test_backfill_prefers_charge_donors(tmp_path):
    rows = [_donor_row(400.0, 40.0) for _ in range(5)]
    rows += [
        _donor_row(900.0, -40.0, source="total_energy", leg_type="In Transit")
        for _ in range(5)
    ]
    _write_report(tmp_path, REG, "20250101_20250401", rows)
    entry = {}
    cb.backfill_vehicle(REG, tmp_path, entry)
    assert entry["effective_capacity_quarterly"]["20250101_20250401"]["kwh"] == 400.0


def test_backfill_skips_non_standard_filenames(tmp_path):
    out = _write_report(
        tmp_path, REG, "20250101_20250401", [_donor_row(400.0, 40.0) for _ in range(6)]
    )
    out.rename(tmp_path / f"jolt_report_{REG}_20250101_20250401_finetuned.xlsx")
    entry = {"effective_capacity_kwh": 300.0}
    summary = cb.backfill_vehicle(REG, tmp_path, entry)
    assert summary["wrote"] is False
    assert entry == {"effective_capacity_kwh": 300.0}  # untouched


def test_backfill_with_no_reports_leaves_the_entry_alone(tmp_path):
    entry = {"effective_capacity_kwh": 300.0}
    summary = cb.backfill_vehicle(REG, tmp_path, entry)
    assert summary["per_period"] == []
    assert summary["wrote"] is False
    assert entry == {"effective_capacity_kwh": 300.0}


def test_backfill_reports_a_donorless_period_as_fallback(tmp_path):
    rows = [_donor_row(400.0, 40.0, source="soc_estimate") for _ in range(6)]
    _write_report(tmp_path, REG, "20250101_20250401", rows)
    entry = {"effective_capacity_kwh": 300.0}
    summary = cb.backfill_vehicle(REG, tmp_path, entry)
    assert summary["per_period"][0][3] == "fallback"
    assert summary["wrote"] is False
    assert entry["effective_capacity_kwh"] == 300.0


def test_backfill_marks_a_sparse_period_and_still_writes(tmp_path):
    _write_report(
        tmp_path, REG, "20250101_20250401", [_donor_row(420.0, 40.0) for _ in range(2)]
    )
    entry = {}
    summary = cb.backfill_vehicle(REG, tmp_path, entry)
    assert (summary["n_reliable"], summary["n_sparse"]) == (0, 1)
    assert entry["effective_capacity_kwh"] == 420.0


def test_backfill_matches_the_live_persist_path(tmp_path, config_dir):
    """A backfill and a re-run must land on the same number."""
    rows = [_donor_row(400.0, 40.0) for _ in range(6)]
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    _write_report(report_dir, REG, "20250101_20250401", rows)

    backfilled = {}
    cb.backfill_vehicle(REG, report_dir, backfilled)

    _persist_effective_capacity(REG, 400.0, 6, "charge", "20250101_20250401")
    live = _read(config_dir)[REG]

    assert backfilled["effective_capacity_quarterly"] == (
        live["effective_capacity_quarterly"]
    )
    assert backfilled["effective_capacity_kwh"] == live["effective_capacity_kwh"]
