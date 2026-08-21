"""Fixture -> segments -> rows -> Stop insertion -> workbook, for EV and diesel.

This is the deliverable a deployer actually ships, so the workbook is opened back
up with openpyxl and inspected: the sheet set, the header row, the ``=NA()``
empty-cell convention, the row colouring, the Stop rows in their gaps and the
row/column counts.

Everything is written into ``tmp_path``; no SRF client is built (``srf_data=None``
means postcode lookup returns ``None`` without a request).
"""

from __future__ import annotations

import copy
import datetime
import re

import openpyxl
import pandas as pd
import pytest

from report_generator.columns import (
    DIESEL_HEADERS,
    HEADERS,
    _row_col_index,
)
from report_generator.report_builder import (
    _insert_stop_rows,
    _seg_to_row,
    _write_excel_report,
)
from report_generator.segment_algorithms import _ANCHOR_PRIVATE_KEYS

CHARGE_RE = re.compile(r"^(AC|DC|Charge|Mix|estimated)", re.IGNORECASE)


def _naive(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert(None) if ts.tzinfo is not None else ts


def _build_ev_rows(alias, frame, charge, discharge, frozen_configs):
    """Reproduce ``_generator._process_fps_legs``'s row building, offline."""
    cfg = frozen_configs["vehicles"][alias]
    mass_agg = cfg.get("mass_agg", "mean")
    rows = []
    cumulative_km = 0.0
    for seg in charge:
        clean = {k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS}
        row, _ = _seg_to_row(
            clean,
            "charge",
            "https://data.example.org/api/legs/leg-1",
            [],
            [],
            frame,
            cumulative_km,
            None,
            srf_data=None,
            altitude_col=cfg.get("altitude_col"),
            speed_col=cfg.get("speed_col", "wheel_based_speed"),
            operator="WJF",
            mass_agg=mass_agg,
        )
        rows.append((seg["start_time"], list(row)))
    for seg in discharge:
        clean = {k: v for k, v in seg.items() if k not in _ANCHOR_PRIVATE_KEYS}
        row, cumulative_km = _seg_to_row(
            clean,
            "discharge",
            "https://data.example.org/api/legs/leg-1",
            [],
            [],
            frame,
            cumulative_km,
            None,
            srf_data=None,
            altitude_col=cfg.get("altitude_col"),
            speed_col=cfg.get("speed_col", "wheel_based_speed"),
            operator="WJF",
            mass_agg=mass_agg,
        )
        rows.append((seg["start_time"], list(row)))
    rows.sort(key=lambda pair: _naive(pair[0]))
    return [row for _, row in rows]


@pytest.fixture
def ev_workbook(
    tmp_path, frozen_configs, load_raw_telematics, run_fixture_segmentation
):
    alias = "EVSPD01"
    frame = load_raw_telematics(alias)
    charge, discharge = run_fixture_segmentation(alias)
    sorted_rows = _build_ev_rows(alias, frame, charge, discharge, frozen_configs)
    with_stops = _insert_stop_rows(sorted_rows, headers=HEADERS)

    out_path = tmp_path / f"jolt_report_{alias}_20250627_20250628.xlsx"
    _write_excel_report(
        with_stops,
        alias,
        datetime.date(2025, 6, 27),
        datetime.date(2025, 6, 28),
        out_path,
        headers=HEADERS,
    )
    return out_path, sorted_rows, with_stops


# ── EV workbook ──────────────────────────────────────────────────────────────


def test_ev_workbook_has_the_expected_sheets(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    wb = openpyxl.load_workbook(out_path)
    assert wb.sheetnames == ["Report", "Graphs", "GraphsData", "Definitions"]
    assert wb["GraphsData"].sheet_state == "hidden"


def test_ev_header_row_is_exactly_headers(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    header = tuple(ws.cell(1, c).value for c in range(1, ws.max_column + 1))
    assert header == HEADERS


def test_ev_row_and_column_counts(ev_workbook):
    out_path, _rows, with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    assert ws.max_column == len(HEADERS)
    assert ws.max_row == len(with_stops) + 1  # + header


def test_ev_leg_numbers_are_sequential(ev_workbook):
    out_path, _rows, with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    numbers = [ws.cell(r, 1).value for r in range(2, ws.max_row + 1)]
    assert numbers == list(range(1, len(with_stops) + 1))


def test_ev_empty_numeric_cells_use_the_na_formula(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path, data_only=False)["Report"]
    col = HEADERS.index("CO2 level (g/kWh)") + 1  # always NaN in this pipeline
    values = {ws.cell(r, col).value for r in range(2, ws.max_row + 1)}
    assert values == {"=NA()"}


def test_ev_na_cells_read_back_as_blank_not_zero(ev_workbook):
    """The cached result of ``=NA()`` is EMPTY on purpose.

    xlsxwriter defaults a formula's cached value to 0; a data_only reader would
    then see a spurious numeric 0 (and, for Vehicle Mass, mis-classify a no-GVM
    leg as mass-bearing). ``_write_na`` passes an empty cached result instead.
    """
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path, data_only=True)["Report"]
    col = HEADERS.index("CO2 level (g/kWh)") + 1
    for r in range(2, ws.max_row + 1):
        assert ws.cell(r, col).value in (None, "")


def test_ev_stop_rows_land_in_the_gaps(ev_workbook):
    out_path, sorted_rows, with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    leg_col = HEADERS.index("Leg Type") + 1
    leg_types = [ws.cell(r, leg_col).value for r in range(2, ws.max_row + 1)]

    # A Stop never sits first or last, and two Stops never touch.
    assert leg_types[0] != "Stop"
    assert leg_types[-1] != "Stop"
    assert not any(
        a == "Stop" and b == "Stop" for a, b in zip(leg_types, leg_types[1:])
    )
    assert leg_types.count("Stop") == len(with_stops) - len(sorted_rows)
    assert leg_types.count("Stop") > 0


def test_ev_stop_rows_bridge_their_neighbours_exactly(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    leg_col = HEADERS.index("Leg Type") + 1
    start_col = HEADERS.index("Start Time (UTC)") + 1
    end_col = HEADERS.index("End Time (UTC)") + 1
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, leg_col).value != "Stop":
            continue
        assert ws.cell(r, start_col).value == ws.cell(r - 1, end_col).value
        assert ws.cell(r, end_col).value == ws.cell(r + 1, start_col).value


def test_ev_rows_are_time_ordered(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    start_col = HEADERS.index("Start Time (UTC)") + 1
    starts = [ws.cell(r, start_col).value for r in range(2, ws.max_row + 1)]
    assert starts == sorted(starts)


def test_ev_row_background_colour_encodes_the_leg_class(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    leg_col = HEADERS.index("Leg Type") + 1
    seen = set()
    for r in range(2, ws.max_row + 1):
        leg_type = ws.cell(r, leg_col).value
        rgb = ws.cell(r, 1).fill.start_color.rgb
        if leg_type == "Stop":
            expected = "FFFFFFFF"  # white
        elif CHARGE_RE.match(leg_type or ""):
            expected = "FFFFC7CE"  # red
        else:
            expected = "FFC6EFCE"  # green
        assert rgb == expected, f"row {r} ({leg_type}) has fill {rgb}"
        seen.add(expected)
    assert len(seen) == 3  # the fixture exercises all three classes


def test_ev_graphsdata_holds_the_filtered_chart_points(ev_workbook):
    from report_generator.charts import CHART_SPECS_EV

    out_path, _rows, with_stops = ev_workbook
    wb = openpyxl.load_workbook(out_path)
    data_ws = wb["GraphsData"]
    for gi, spec in enumerate(CHART_SPECS_EV):
        assert data_ws.cell(1, 2 * gi + 1).value == spec["x_hdr"]
        assert data_ws.cell(1, 2 * gi + 2).value == spec["y_hdr"]


def test_ev_definitions_sheet_is_the_electric_glossary(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    ws = openpyxl.load_workbook(out_path)["Definitions"]
    text = "\n".join(str(ws.cell(r, 1).value) for r in range(1, ws.max_row + 1))
    assert "State of Charge" in text
    assert "soc_fallback" in text
    assert "Fuel Used (L)" not in text


def test_ev_workbook_metadata_names_the_vehicle_and_period(ev_workbook):
    out_path, _rows, _with_stops = ev_workbook
    props = openpyxl.load_workbook(out_path).properties
    assert "EVSPD01" in props.title
    assert props.subject == "2025-06-27 to 2025-06-28"


def test_ev_every_data_row_has_the_row_tuple_length(ev_workbook):
    _out, sorted_rows, with_stops = ev_workbook
    for row in with_stops:
        assert len(row) == len(HEADERS) - 1


# ── Diesel workbook ──────────────────────────────────────────────────────────


@pytest.fixture
def diesel_workbook(tmp_path, diesel_fixture_frame):
    from report_generator import diesel_pipeline as dp

    frame, cfg = diesel_fixture_frame
    _trips, seg_metrics = dp._segments_from_df(frame, cfg, source="fixture")
    assert seg_metrics, "the DSL01 fixture must yield at least one trip"

    # The fixture is a single ~9-minute logger leg, so synthesise a SECOND trip
    # two hours later from the same metrics. That is the only way to exercise
    # diesel Stop insertion on real numbers; every field is a copy of a genuine
    # measured trip, only shifted in time.
    later = copy.deepcopy(seg_metrics[0])
    later["start_time"] = seg_metrics[0]["start_time"] + pd.Timedelta(hours=2)
    later["end_time"] = seg_metrics[0]["end_time"] + pd.Timedelta(hours=2)

    rows = []
    cumulative_km = 0.0
    for seg in [*seg_metrics, later]:
        row, cumulative_km = dp._diesel_seg_to_row(
            seg,
            "https://data.example.org/api/legs/logger-1",
            cumulative_km,
            srf_data=None,
            operator="WJF",
        )
        rows.append((seg["start_time"], list(row)))
    rows.sort(key=lambda pair: _naive(pair[0]))
    sorted_rows = [row for _, row in rows]
    with_stops = _insert_stop_rows(sorted_rows, headers=DIESEL_HEADERS)

    out_path = tmp_path / "jolt_report_DSL01_20251007_20251008.xlsx"
    _write_excel_report(
        with_stops,
        "DSL01",
        datetime.date(2025, 10, 7),
        datetime.date(2025, 10, 8),
        out_path,
        headers=DIESEL_HEADERS,
    )
    return out_path, sorted_rows, with_stops


def test_diesel_workbook_has_the_expected_sheets(diesel_workbook):
    out_path, _rows, _with_stops = diesel_workbook
    wb = openpyxl.load_workbook(out_path)
    assert wb.sheetnames == ["Report", "Graphs", "GraphsData", "Definitions"]


def test_diesel_header_row_is_exactly_diesel_headers(diesel_workbook):
    out_path, _rows, _with_stops = diesel_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    header = tuple(ws.cell(1, c).value for c in range(1, ws.max_column + 1))
    assert header == DIESEL_HEADERS
    # The diesel layout is NARROWER and carries no electric columns.
    assert ws.max_column == len(DIESEL_HEADERS) < len(HEADERS)
    assert "Battery Capacity (kWh)" not in header
    assert "Start SOC (%)" not in header


def test_diesel_row_and_column_counts(diesel_workbook):
    out_path, sorted_rows, with_stops = diesel_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    assert len(sorted_rows) == 2
    assert len(with_stops) == 3  # one Stop bridges the two trips
    assert ws.max_row == len(with_stops) + 1


def test_diesel_leg_types_and_energy_source(diesel_workbook):
    out_path, _rows, _with_stops = diesel_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    leg_col = DIESEL_HEADERS.index("Leg Type") + 1
    src_col = DIESEL_HEADERS.index("Energy Source") + 1
    leg_types = [ws.cell(r, leg_col).value for r in range(2, ws.max_row + 1)]
    assert leg_types == ["In Transit", "Stop", "In Transit"]
    sources = [ws.cell(r, src_col).value for r in range(2, ws.max_row + 1)]
    assert sources[0] == "lfc_fuel" and sources[2] == "lfc_fuel"


def test_diesel_fuel_columns_carry_real_numbers(diesel_workbook):
    out_path, _rows, _with_stops = diesel_workbook
    ws = openpyxl.load_workbook(out_path)["Report"]
    fuel_col = DIESEL_HEADERS.index("Fuel Used (L)") + 1
    cons_col = DIESEL_HEADERS.index("Fuel Consumption (L/100km)") + 1
    assert ws.cell(2, fuel_col).value > 0
    assert 0 < ws.cell(2, cons_col).value < 200


def test_diesel_definitions_sheet_is_the_diesel_glossary(diesel_workbook):
    out_path, _rows, _with_stops = diesel_workbook
    ws = openpyxl.load_workbook(out_path)["Definitions"]
    text = "\n".join(str(ws.cell(r, 1).value) for r in range(1, ws.max_row + 1))
    assert "Fuel Used (L)" in text
    assert "lfc_fuel" in text
    assert "State of Charge" not in text


def test_diesel_graphsdata_uses_the_diesel_chart_specs(diesel_workbook):
    from report_generator.charts import CHART_SPECS_DIESEL

    out_path, _rows, _with_stops = diesel_workbook
    data_ws = openpyxl.load_workbook(out_path)["GraphsData"]
    for gi, spec in enumerate(CHART_SPECS_DIESEL):
        assert data_ws.cell(1, 2 * gi + 1).value == spec["x_hdr"]
        assert data_ws.cell(1, 2 * gi + 2).value == spec["y_hdr"]


def test_diesel_stop_row_carries_mass_and_blank_soc_columns(diesel_workbook):
    out_path, _rows, with_stops = diesel_workbook
    stop = with_stops[1]
    assert stop[_row_col_index("Leg Type", DIESEL_HEADERS)] == "Stop"
    assert stop[_row_col_index("Distance (km)", DIESEL_HEADERS)] == 0.0
    assert stop[_row_col_index("Vehicle Mass (kg)", DIESEL_HEADERS)] > 0
    assert len(stop) == len(DIESEL_HEADERS) - 1
