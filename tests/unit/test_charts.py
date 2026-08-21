"""Graphs-sheet point filtering, fit subtitle, geometry and spec selection.

``_filtered_chart_points`` is the single routine both render paths share, so its
rules (Stop always dropped, charge dropped for driving-only charts, per-axis
windows, numeric-only) decide what the scatter AND its trendline see.
"""

from __future__ import annotations

import pytest

from report_generator import charts
from report_generator.columns import DIESEL_HEADERS, HEADERS

X_HDR = "Vehicle Mass (kg)"
Y_HDR = "Energy Performance (kWh/km)"

#: Row-tuple index map for the EV layout (rows omit the Leg Number column).
EV_IDX = {h: i - 1 for i, h in enumerate(HEADERS) if i >= 1}

SPEC_DRIVING_ONLY = {
    "x_hdr": X_HDR,
    "y_hdr": Y_HDR,
    "x_filter": (1.0, None),
    "y_filter": (0.1, 3.0),
    "driving_only": True,
}
SPEC_ALL_ROWS = dict(SPEC_DRIVING_ONLY, driving_only=False)


def _row(leg_type, mass, ep):
    """Build a minimal EV row tuple carrying only Leg Type / mass / EP."""
    row = [None] * (len(HEADERS) - 1)
    row[EV_IDX["Leg Type"]] = leg_type
    row[EV_IDX[X_HDR]] = mass
    row[EV_IDX[Y_HDR]] = ep
    return row


def test_filtered_points_keeps_a_clean_trip_row():
    pts = charts._filtered_chart_points(
        [_row("In Transit", 30000.0, 1.4)], EV_IDX, SPEC_DRIVING_ONLY
    )
    assert pts == [(30000.0, 1.4)]


def test_filtered_points_always_drops_stop_rows():
    rows = [_row("Stop", 30000.0, 1.4)]
    assert charts._filtered_chart_points(rows, EV_IDX, SPEC_DRIVING_ONLY) == []
    # Even with driving_only OFF a Stop never reaches a chart.
    assert charts._filtered_chart_points(rows, EV_IDX, SPEC_ALL_ROWS) == []


def test_filtered_points_drops_charge_rows_only_when_driving_only():
    rows = [_row("DC Away", 30000.0, 1.4)]
    assert charts._filtered_chart_points(rows, EV_IDX, SPEC_DRIVING_ONLY) == []
    assert charts._filtered_chart_points(rows, EV_IDX, SPEC_ALL_ROWS) == [
        (30000.0, 1.4)
    ]


@pytest.mark.parametrize(
    "mass, ep, kept",
    [
        (30000.0, 1.4, True),
        (0.5, 1.4, False),  # x below the (1.0, None) window
        (1.0, 1.4, True),  # x window is inclusive
        (30000.0, 0.05, False),  # y below 0.1
        (30000.0, 3.5, False),  # y above 3.0
        (30000.0, 3.0, True),  # y window is inclusive
    ],
)
def test_filtered_points_applies_the_axis_windows(mass, ep, kept):
    pts = charts._filtered_chart_points(
        [_row("In Transit", mass, ep)], EV_IDX, SPEC_DRIVING_ONLY
    )
    assert bool(pts) is kept


@pytest.mark.parametrize(
    "mass, ep",
    [
        (float("nan"), 1.4),
        (30000.0, float("nan")),
        (None, 1.4),
        (30000.0, None),
        ("30000", 1.4),  # a string that looks numeric is still rejected
        (30000.0, "1.4"),
    ],
)
def test_filtered_points_rejects_nan_and_non_numeric(mass, ep):
    rows = [_row("In Transit", mass, ep)]
    assert charts._filtered_chart_points(rows, EV_IDX, SPEC_DRIVING_ONLY) == []


def test_filtered_points_returns_empty_for_a_missing_column():
    spec = dict(SPEC_DRIVING_ONLY, x_hdr="No Such Column")
    rows = [_row("In Transit", 30000.0, 1.4)]
    assert charts._filtered_chart_points(rows, EV_IDX, spec) == []


def test_filtered_points_skips_empty_and_short_rows():
    rows = [[], _row("In Transit", 30000.0, 1.4), ["In Transit"]]
    assert charts._filtered_chart_points(rows, EV_IDX, SPEC_DRIVING_ONLY) == [
        (30000.0, 1.4)
    ]


def test_filtered_points_none_window_means_no_bound():
    spec = dict(SPEC_DRIVING_ONLY, x_filter=None, y_filter=None)
    rows = [_row("In Transit", -5.0, 99.0)]
    assert charts._filtered_chart_points(rows, EV_IDX, spec) == [(-5.0, 99.0)]


def test_filtered_points_coerces_ints_to_float():
    pts = charts._filtered_chart_points(
        [_row("In Transit", 30000, 2)], EV_IDX, SPEC_DRIVING_ONLY
    )
    assert pts == [(30000.0, 2.0)]
    assert all(isinstance(v, float) for v in pts[0])


# ── _chart_subtitle ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("pts", [[], [(1.0, 2.0)]])
def test_chart_subtitle_needs_two_points(pts):
    assert charts._chart_subtitle(pts) == ""


def test_chart_subtitle_zero_x_variance_gives_no_fit():
    assert charts._chart_subtitle([(3.0, 1.0), (3.0, 2.0), (3.0, 5.0)]) == ""


def test_chart_subtitle_perfect_fit():
    # y = 2x + 1 through three points -> slope 2, intercept 1, R^2 = 1.
    assert charts._chart_subtitle([(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)]) == (
        "y = 2x + 1    ·    R² = 1.0000"
    )


def test_chart_subtitle_negative_intercept_uses_a_unicode_minus():
    out = charts._chart_subtitle([(0.0, -1.0), (1.0, 1.0), (2.0, 3.0)])
    assert out.startswith("y = 2x − 1")


def test_chart_subtitle_imperfect_fit_reports_r_squared_below_one():
    out = charts._chart_subtitle([(0.0, 1.0), (1.0, 3.0), (2.0, 4.0)])
    r2 = float(out.rsplit("=", 1)[1])
    assert 0.9 < r2 < 1.0


def test_chart_subtitle_flat_y_reports_zero_r_squared():
    out = charts._chart_subtitle([(0.0, 5.0), (1.0, 5.0), (2.0, 5.0)])
    assert out.endswith("R² = 0.0000")


# ── Geometry helpers ─────────────────────────────────────────────────────────


def test_chart_geometry_is_derived_from_the_shared_style_constants():
    from math import ceil

    style = charts.CHART_STYLE
    expected_rows = ceil(
        style["chart_height_cm"] * charts._EMU_PER_CM / charts._EMU_PER_DEFAULT_ROW
    )
    expected_cols = ceil(
        style["chart_width_cm"] * charts._EMU_PER_CM / charts._EMU_PER_DEFAULT_COL
    )
    assert charts._chart_height_rows() == expected_rows
    assert charts.chart_col_span() == expected_cols
    assert charts.chart_row_step() == expected_rows + style["chart_gap_rows"]


def test_chart_row_step_leaves_a_gap_so_charts_cannot_overlap():
    step = charts.chart_row_step()
    assert isinstance(step, int)
    assert step > charts._chart_height_rows()


@pytest.mark.parametrize("gi", [0, 1, 2, 5])
def test_empty_note_extent_occupies_the_chart_footprint(gi):
    first_row, first_col, last_row, last_col = charts.empty_note_extent(gi)
    assert first_col == 0
    assert first_row == gi * charts.chart_row_step()
    assert last_row - first_row + 1 == charts._chart_height_rows()
    assert last_col - first_col + 1 == charts.chart_col_span()


def test_consecutive_empty_note_panels_do_not_overlap():
    _, _, last_row_0, _ = charts.empty_note_extent(0)
    first_row_1, _, _, _ = charts.empty_note_extent(1)
    assert first_row_1 > last_row_0


# ── Spec selection + no-data note ────────────────────────────────────────────


def test_chart_specs_for_discriminates_ev_and_diesel():
    assert charts.chart_specs_for(HEADERS) is charts.CHART_SPECS_EV
    assert charts.chart_specs_for(DIESEL_HEADERS) is charts.CHART_SPECS_DIESEL


def test_chart_specs_for_uses_the_fuel_consumption_column_as_the_discriminator():
    assert "Fuel Consumption (L/100km)" in DIESEL_HEADERS
    assert "Fuel Consumption (L/100km)" not in HEADERS
    assert charts.chart_specs_for(("Fuel Consumption (L/100km)",)) is (
        charts.CHART_SPECS_DIESEL
    )


@pytest.mark.parametrize(
    "specs, headers",
    [(charts.CHART_SPECS_EV, HEADERS), (charts.CHART_SPECS_DIESEL, DIESEL_HEADERS)],
)
def test_every_spec_references_real_columns_and_a_sane_axis(specs, headers):
    for spec in specs:
        assert spec["x_hdr"] in headers
        assert spec["y_hdr"] in headers
        assert spec["x_min"] < spec["x_max"]
        assert spec["y_min"] < spec["y_max"]
        assert spec["x_major"] > 0 and spec["y_major"] > 0
        assert spec["title"] and spec["series_name"]


def test_empty_chart_note_prefers_the_spec_text():
    assert charts.empty_chart_note({"empty_note": "No mass"}) == "No mass"
    assert charts.empty_chart_note({}) == "No data available for this chart"
