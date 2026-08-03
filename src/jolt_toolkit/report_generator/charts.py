"""
report_generator.charts
=======================
``Graphs`` worksheet chart specifications and geometry: the shared
``CHART_STYLE`` aesthetic, EV / diesel ``CHART_SPECS_*``, the EMU/row geometry
helpers, the fit-subtitle builder and the shared point-filtering routine. The
same specs drive both render paths (the xlsxwriter generator here and the
openpyxl ``patch_graphs_2_2_3.py``).

Split out of report_builder.py, which re-exports these names for backward
compatibility.
"""

from __future__ import annotations

from math import ceil

from jolt_toolkit.report_generator.columns import (
    _is_nan,
    _leg_is_charge,
    _leg_is_stop,
)

# =============================================================================
# Graphs worksheet chart specifications (single source of truth)
# =============================================================================
# One spec per scatter chart on the ``Graphs`` worksheet. Both rendering paths
# share this config so EV / Diesel reports look identical across every file:
#   - the generator (xlsxwriter, ``_write_excel_report``) reads it to set fixed
#     axis bounds and to point each series at a pre-filtered helper block;
#   - the in-place patch script (openpyxl) imports the very same list to rebuild
#     the ``Graphs`` sheet on existing workbooks.
#
# Every axis is a FIXED constant (never per-file autoscaling), so the same chart
# type uses the exact same scale on all vehicles and all reports. Basis for each
# bound is documented inline:
#   - Energy Performance y-axis 0–3, Vehicle Mass x-axis 0–45000 and the ambient
#     Temperature x-axis −5..30 follow the FIXED PDF-briefing / paper conventions
#     (PERF_YLIM / MASS_XLIM / TEMP_XLIM in generate_pdf_report + plot-figure
#     SKILL), so the Excel charts use the same scales the PDF briefing does.
#   - The remaining axes (speed, fuel consumption) were fixed from robust
#     fleet-wide percentiles over the entire report set (1st/99th percentile
#     of driving-leg values, then rounded outwards to a clean tick), so ~99 % of
#     real data fits in-frame and the rare telematics spikes (e.g. speed 5299 km/h,
#     power 96 MW) are clipped rather than blowing up the scale.
#
# Each spec is a dict with:
#   x_hdr / y_hdr           — column headers (must exist in HEADERS/DIESEL_HEADERS).
#                             These are the DATA references: the series / categories
#                             and the filter logic both look the column up by this
#                             exact Report-sheet header, so it must never be renamed
#                             to a display alias.
#   x_title / y_title       — optional axis DISPLAY title, decoupled from the data
#                             header above. When absent the axis title falls back to
#                             x_hdr / y_hdr. Use this to show a friendlier / paper-
#                             consistent axis label (e.g. "Vehicle gross weight (kg)")
#                             while the series keeps pointing at the real column
#                             ("Vehicle Mass (kg)"). It changes ONLY the printed
#                             label — not the data, axis range or any other style.
#   x_min/x_max/x_major     — fixed x-axis bounds + major unit
#   y_min/y_max/y_major     — fixed y-axis bounds + major unit
#   x_filter / y_filter     — optional (lo, hi) inclusive value windows applied
#                             before a point is written to the helper block
#                             (None ⇒ no value window for that axis)
#   driving_only            — if True, charge / Stop rows are excluded (used for
#                             every Energy-Performance / Fuel-Consumption chart)
#   title                   — short chart title that occupies the top band only
#                             (kept distinct from the verbose "y vs x" so it does
#                             not crowd the plot / grid lines)
#   series_name             — concise legend label (NOT identical to the title) so
#                             the right-aligned legend stays compact
#
# Visual style is intentionally NOT per-spec: every chart shares ``CHART_STYLE``
# (single steelblue scatter colour + alpha, marker size, legend position, chart
# size, whether the fit equation / R² are shown). Both rendering paths
# (xlsxwriter generator + openpyxl patch) read these same fields, so the two can
# never drift apart.
#
# Filtering note: xlsxwriter / openpyxl charts reference cell ranges and cannot
# "filter" in place, so the generator and patcher both write the filtered
# (x, y) pairs to a hidden ``GraphsData`` block and point the scatter series AND
# the linear trendline at that clean block — keeping the fit consistent with the
# paper's convention (fit computed on clean data only).

# Shared chart aesthetic for every Graphs chart, EV and diesel alike. Keeping a
# single source of truth here means the xlsxwriter generator and the openpyxl
# patch script render an identical look. Colours are RGB hex WITHOUT the leading
# '#': openpyxl wants the 6-char form, and xlsxwriter accepts '#RRGGBB' so the
# generator prepends '#'.
CHART_STYLE: dict = {
    "scatter_rgb": "1F77B4",  # steelblue — the muted single colour for all points
    "scatter_opacity": 72,  # marker fill OPACITY (%); soft like the paper.
    # openpyxl uses it directly as the DrawingML alpha
    # (alpha = opacity × 1000); xlsxwriter wants the
    # complementary transparency (100 − opacity).
    "marker_size": 6,  # points are slightly larger now the charts are bigger
    "legend_pos_xlsxwriter": "top_right",  # xlsxwriter legend position keyword
    "legend_pos_openpyxl": "tr",  # openpyxl LegendPosition (top-right)
    # Chart size — enlarged so the bigger fonts (below) have room and the axis
    # titles sit clear of the tick labels instead of overlapping them. BOTH render
    # paths now size the chart explicitly from ``chart_width_cm`` /
    # ``chart_height_cm`` (xlsxwriter via ``set_size``, openpyxl via ``chart.width``
    # / ``chart.height``) instead of the old xlsxwriter ``x_scale`` / ``y_scale``,
    # so the two paths render at the SAME physical size and one shared row step
    # (``chart_row_step``) places them identically without overlap.
    "chart_width_cm": 24.0,  # explicit chart width  (both paths)
    "chart_height_cm": 13.0,  # explicit chart height (both paths)
    # Vertical gap (in worksheet rows) left BELOW each chart before the next one
    # starts. The per-chart row step = ceil(chart height in rows) + this gap; see
    # ``chart_row_step``. A 13 cm chart is ≈ 24.6 default rows, so a gap of 2 rows
    # gives a clean ~2-row strip between charts. Bumping the chart height or font
    # sizes only requires re-deriving the step from these constants — never hand-
    # tuning per-path anchors again (the old 22-row xlsxwriter / 24-row openpyxl
    # anchors were smaller than the chart and made adjacent charts overlap).
    "chart_gap_rows": 2,
    # Plot-area manual layout — fractions of the CHART area, shared by both render
    # paths so the inner plot box sits in the same place everywhere. Without a
    # manual layout Excel auto-sizes the plot area as large as it can; at the
    # enlarged plot-figure font sizes (axis-title 14 / tick 12) that pushed the
    # rotated y-axis title on top of the y tick numbers and the x-axis title on
    # top of the x tick numbers. Reserving fixed margins keeps every axis title
    # OUTSIDE its tick labels: ``plot_x`` (left) clears the rotated y title + y
    # ticks, ``1 - plot_y - plot_h`` (bottom) clears the x title + x ticks,
    # ``plot_y`` (top) clears the two-line title band, ``1 - plot_x - plot_w``
    # (right) leaves a little room for the top-right legend. xlsxwriter
    # ``set_plotarea({'layout': {x, y, width, height}})`` and openpyxl
    # ``ManualLayout(xMode='edge', yMode='edge', x, y, w, h)`` use the same
    # edge-anchored fraction semantics, so one set of numbers drives both.
    "plot_x": 0.11,  # left margin: rotated y-axis title + y tick labels
    "plot_y": 0.17,  # top margin: two-line title band (title + fit subtitle)
    "plot_w": 0.85,  # plot width  (right margin 1−x−w ≈ 0.04 for the legend)
    "plot_h": 0.71,  # plot height (bottom margin 1−y−h ≈ 0.12 for x title+ticks)
    # Font sizes (pt) — aligned with the plot-figure skill Style constants so the
    # Excel charts read at the same scale as the published matplotlib figures.
    "title_font_size": 14,  # pt — chart title band (FS_TITLE)
    "subtitle_font_size": 10,  # pt — the fit-equation / R² subtitle line
    "axis_title_font_size": 14,  # pt — x / y axis titles (FS_LABEL)
    "tick_font_size": 12,  # pt — axis tick labels (FS_TICK)
    "legend_font_size": 9,  # pt — legend text (FS_LEGEND)
    # Major gridlines — light grey + high transparency so the plot area reads
    # cleanly (the paper uses GRID_ALPHA = 0.3). ``grid_rgb`` is the 6-char hex
    # WITHOUT '#'; ``grid_opacity`` is the line OPACITY (%) — openpyxl writes it as
    # the DrawingML alpha (alpha = opacity × 1000) and xlsxwriter as the
    # complementary transparency (100 − opacity). Minor gridlines are switched off.
    "grid_rgb": "D9D9D9",  # light grey
    "grid_opacity": 30,  # 30 % opacity ≈ paper GRID_ALPHA 0.3
    "grid_width_emu": 9525,  # 0.75 pt hairline (EMU); xlsxwriter uses pt below
    "show_fit_in_subtitle": True,  # put "y = ... , R² = ..." in a 2nd title line,
    # NOT as overlapping floating data labels
    # Graceful-degradation panel — when a chart has ZERO filtered data points (e.g.
    # a vehicle whose ``mass_col`` is null for the whole period, so the EP-vs-Mass
    # chart can plot nothing), the empty scatter frame is replaced by a centred
    # text note occupying the chart's footprint. The note is a merged-cell panel
    # (NOT an xlsxwriter textbox / openpyxl drawing) because merged cells are the
    # ONE primitive both render paths write identically — keeping the two paths in
    # lockstep. These keys style that panel; colours are 6-char hex WITHOUT '#'.
    "empty_note_font_size": 12,  # pt — note text size
    "empty_note_rgb": "808080",  # grey italic note text
    "empty_note_fill_rgb": "F2F2F2",  # very light grey panel background
    "empty_note_border_rgb": "BFBFBF",  # thin grey panel border
}

# Default Excel row height is 15 pt; 1 pt = 12 700 EMU, so one un-resized row is
# 190 500 EMU tall. A chart of ``chart_height_cm`` (cm → EMU via 360 000 EMU/cm)
# therefore spans ``chart_height_cm * 360000 / 190500`` rows. Both the Graphs
# sheets keep the default row height, so this constant converts the shared chart
# height into the worksheet-row span used to stack the charts. The matching
# column constant (default column ≈ 64 px = 609 600 EMU for Calibri 11 at width
# 8.43) converts the shared chart WIDTH into a worksheet-column span — used only
# to size the graceful-degradation note panel so it roughly fills the footprint a
# real chart would occupy.
_EMU_PER_CM = 360_000
_EMU_PER_DEFAULT_ROW = 15 * 12_700  # 15 pt default row height, in EMU
_EMU_PER_DEFAULT_COL = 64 * 9_525  # 64 px default column width, in EMU


def _chart_height_rows() -> int:
    """Worksheet-row span of one Graphs chart (its own height, excluding the gap)."""
    return ceil(CHART_STYLE["chart_height_cm"] * _EMU_PER_CM / _EMU_PER_DEFAULT_ROW)


def chart_col_span() -> int:
    """Worksheet-column span of one Graphs chart.

    Used only to size the graceful-degradation note panel (a merged-cell block)
    so it roughly matches the footprint a real chart would occupy. Derived from
    the shared ``chart_width_cm`` and the default column width, so both render
    paths size the panel identically.
    """
    return ceil(CHART_STYLE["chart_width_cm"] * _EMU_PER_CM / _EMU_PER_DEFAULT_COL)


def chart_row_step() -> int:
    """Return the worksheet-row step between successive Graphs charts.

    The step is ``ceil(chart height in default rows) + chart_gap_rows`` using the
    shared ``CHART_STYLE`` chart-size / gap constants, so the i-th chart is
    anchored at row ``i * chart_row_step()`` (0-based) and the next chart always
    starts in the clear strip BELOW it. Both render paths (the xlsxwriter
    generator here and the openpyxl ``patch_graphs_2_2_3.py``) import this single
    helper, so adjacent charts can never overlap and the two paths stay in
    lockstep. Returns a plain ``int`` (xlsxwriter / openpyxl anchors are 1-based
    cell references built from it).
    """
    return _chart_height_rows() + CHART_STYLE["chart_gap_rows"]


def empty_chart_note(spec: dict) -> str:
    """Text shown in place of a chart that has zero filtered data points.

    Returns the spec's ``empty_note`` (e.g. the EP/Fuel-vs-Mass charts say "No
    vehicle mass data available for this vehicle") or a generic fallback. Shared
    by both render paths so the degraded panel reads the same message everywhere.
    """
    return spec.get("empty_note", "No data available for this chart")


def empty_note_extent(gi: int) -> tuple[int, int, int, int]:
    """0-based ``(first_row, first_col, last_row, last_col)`` for chart ``gi``'s
    no-data note panel.

    The panel occupies the same row band and roughly the same width the real
    chart would (anchor ``gi * chart_row_step()``, height ``_chart_height_rows()``
    rows, width ``chart_col_span()`` columns), so replacing a chart with a note
    leaves the remaining charts' positions untouched. Both render paths build
    their merge range from this single helper.
    """
    first_row = gi * chart_row_step()
    last_row = first_row + _chart_height_rows() - 1
    first_col = 0
    last_col = chart_col_span() - 1
    return first_row, first_col, last_row, last_col


def _chart_subtitle(pts) -> str:
    """Build the "y = a·x + b   ·   R² = r" subtitle from filtered points.

    Returns an empty string when fewer than two points (a line cannot be fit).
    Computing the least-squares fit here lets us render the equation + R² as a
    single non-overlapping title sub-line instead of two floating labels that
    Excel stacks on top of each other over the data.
    """
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    if n < 2:
        return ""
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return ""
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    syy = sum((y - mean_y) ** 2 for y in ys)
    r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.0
    sign = "+" if intercept >= 0 else "−"
    return f"y = {slope:.4g}x {sign} {abs(intercept):.4g}" f"    ·    R² = {r2:.4f}"


# EV charts. Only Energy-Performance-vs-{Mass, Temperature, Speed} are kept; the
# former Battery-Power-vs-Energy-Change and SOC-Change-vs-Energy-Change pairs were
# dropped so EV and diesel report the same three-panel
# "{performance metric} vs {Mass / Temp / Speed}" set, matching the paper's convention.
CHART_SPECS_EV: tuple[dict, ...] = (
    {
        "x_hdr": "Vehicle Mass (kg)",
        # Display the x-axis as "Vehicle gross weight (kg)" to match the paper
        # figures; the series still reads the real "Vehicle Mass (kg)" column.
        "x_title": "Vehicle gross weight (kg)",
        "y_hdr": "Energy Performance (kWh/km)",
        "title": "EP vs Vehicle Mass",
        "series_name": "EP",
        # Shown instead of an empty chart frame when this vehicle has no mass data
        # at all (e.g. its ``mass_col`` is null for the whole period).
        "empty_note": "No vehicle mass data available for this vehicle",
        # x: paper MASS_XLIM 0–45000 kg. y: paper PERF_YLIM 0–3 kWh/km.
        "x_min": 0,
        "x_max": 45000,
        "x_major": 5000,
        "y_min": 0,
        "y_max": 3,
        "y_major": 0.5,
        "x_filter": (1.0, None),
        "y_filter": (0.1, 3.0),
        "driving_only": True,
    },
    {
        "x_hdr": "Average Temperature (C)",
        "y_hdr": "Energy Performance (kWh/km)",
        "title": "EP vs Average Temperature (°C)",
        "series_name": "EP",
        "empty_note": "No temperature data available for this vehicle",
        # x: ambient temperature −5..30 °C — the fixed PDF-briefing convention
        # (generate_pdf_report TEMP_XLIM = (-5, 30)), so the Excel and PDF temp
        # axes match. Fleet temp p1≈-0.1 / p99≈26.9 (rare spikes above 30 °C are
        # clipped to the frame, exactly as the PDF does via set_xlim).
        "x_min": -5,
        "x_max": 30,
        "x_major": 5,
        "y_min": 0,
        "y_max": 3,
        "y_major": 0.5,
        "x_filter": None,
        "y_filter": (0.1, 3.0),
        "driving_only": True,
    },
    {
        "x_hdr": "Average Speed (km/h)",
        "y_hdr": "Energy Performance (kWh/km)",
        "title": "EP vs Average Speed",
        "series_name": "EP",
        "empty_note": "No speed data available for this vehicle",
        # x: fleet speed p1≈3.8 / p99≈76.8 (max 5299 is a telematics spike) → 0..90.
        "x_min": 0,
        "x_max": 90,
        "x_major": 10,
        "y_min": 0,
        "y_max": 3,
        "y_major": 0.5,
        "x_filter": (0.0, 90.0),
        "y_filter": (0.1, 3.0),
        "driving_only": True,
    },
)

# Diesel charts. Fuel-Consumption-vs-{Mass, Temperature, Speed} only; the former
# Distance-vs-Fuel-Used pair was dropped to mirror the EV three-panel set.
CHART_SPECS_DIESEL: tuple[dict, ...] = (
    {
        "x_hdr": "Vehicle Mass (kg)",
        # Display the x-axis as "Vehicle gross weight (kg)" to match the paper
        # figures; the series still reads the real "Vehicle Mass (kg)" column.
        "x_title": "Vehicle gross weight (kg)",
        "y_hdr": "Fuel Consumption (L/100km)",
        "title": "Fuel cons. vs Vehicle Mass",
        "series_name": "Fuel cons.",
        "empty_note": "No vehicle mass data available for this vehicle",
        # x: paper MASS_XLIM 0–45000 kg (fleet mass p99≈44000).
        # y: fleet fuel-cons p1≈25 / p99≈50 (max 64.5) → 0..60.
        "x_min": 0,
        "x_max": 45000,
        "x_major": 5000,
        "y_min": 0,
        "y_max": 60,
        "y_major": 10,
        "x_filter": (1.0, None),
        "y_filter": (0.0, 60.0),
        "driving_only": True,
    },
    {
        "x_hdr": "Average Temperature (C)",
        "y_hdr": "Fuel Consumption (L/100km)",
        "title": "Fuel cons. vs Average Temperature (°C)",
        "series_name": "Fuel cons.",
        "empty_note": "No temperature data available for this vehicle",
        # x: ambient temperature −5..30 °C — shared with the EV temp axis and the
        # fixed PDF-briefing convention (TEMP_XLIM = (-5, 30)). Fleet temp
        # p1≈-0.8 / p99≈29.7; rare spikes above 30 °C are clipped to the frame.
        "x_min": -5,
        "x_max": 30,
        "x_major": 5,
        "y_min": 0,
        "y_max": 60,
        "y_major": 10,
        "x_filter": None,
        "y_filter": (0.0, 60.0),
        "driving_only": True,
    },
    {
        "x_hdr": "Average Speed (km/h)",
        "y_hdr": "Fuel Consumption (L/100km)",
        "title": "Fuel cons. vs Average Speed",
        "series_name": "Fuel cons.",
        "empty_note": "No speed data available for this vehicle",
        # x: fleet speed p1≈7 / p99≈70.5 (max 81) → 0..90 (shared with EV speed axis).
        "x_min": 0,
        "x_max": 90,
        "x_major": 10,
        "y_min": 0,
        "y_max": 60,
        "y_major": 10,
        "x_filter": (0.0, 90.0),
        "y_filter": (0.0, 60.0),
        "driving_only": True,
    },
)


def chart_specs_for(headers: tuple) -> tuple[dict, ...]:
    """Return the chart-spec group matching a report's ``headers`` layout.

    Diesel reports are identified by the presence of the
    ``Fuel Consumption (L/100km)`` column (the same discriminator used elsewhere
    in this module); everything else is treated as an EV report.
    """
    return (
        CHART_SPECS_DIESEL
        if "Fuel Consumption (L/100km)" in headers
        else CHART_SPECS_EV
    )


def _filtered_chart_points(rows, col_idx_by_header: dict, spec: dict):
    """Return the cleaned ``(x, y)`` pairs for one chart spec.

    ``rows`` is the list of report-row tuples WITHOUT the leading Leg-Number
    column (i.e. ``row[0]`` is ``Leg Type``); ``col_idx_by_header`` maps a
    header name to its index within those tuples. Applies the spec's
    ``driving_only`` rule (drop charge / Stop rows), the per-axis value windows,
    and finally requires both x and y to be finite numbers. This is the single
    filtering routine shared by the xlsxwriter generator and the openpyxl patch
    script, so both render scatter + trendline on identical clean data.
    """
    xi = col_idx_by_header.get(spec["x_hdr"])
    yi = col_idx_by_header.get(spec["y_hdr"])
    if xi is None or yi is None:
        return []
    x_filter = spec.get("x_filter")
    y_filter = spec.get("y_filter")
    driving_only = spec.get("driving_only", False)

    def _in_window(val, window) -> bool:
        if window is None:
            return True
        lo, hi = window
        if lo is not None and val < lo:
            return False
        if hi is not None and val > hi:
            return False
        return True

    pts: list[tuple[float, float]] = []
    for row in rows:
        if not row:
            continue
        leg_type = row[0]
        if _leg_is_stop(leg_type):
            continue
        if driving_only and _leg_is_charge(leg_type):
            continue
        if xi >= len(row) or yi >= len(row):
            continue
        xv, yv = row[xi], row[yi]
        if not isinstance(xv, (int, float)) or not isinstance(yv, (int, float)):
            continue
        if _is_nan(xv) or _is_nan(yv):
            continue
        if not _in_window(xv, x_filter) or not _in_window(yv, y_filter):
            continue
        pts.append((float(xv), float(yv)))
    return pts
