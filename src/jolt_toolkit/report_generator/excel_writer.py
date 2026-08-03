"""
report_generator.excel_writer
=============================
Writes the formatted three-sheet Excel report (Report / Graphs / GraphsData /
Definitions) with xlsxwriter, plus the ``=NA()`` no-data helper. The former
monolithic ``_write_excel_report`` is decomposed here into three private
block-extraction helpers (``_write_report_sheet`` / ``_write_graphs_sheet`` /
``_write_definitions_sheet``) in the original statement order.

Split out of report_builder.py, which re-exports these names for backward
compatibility.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd
import xlsxwriter

from jolt_toolkit.report_generator.charts import (
    CHART_STYLE,
    _chart_subtitle,
    _filtered_chart_points,
    chart_row_step,
    chart_specs_for,
    empty_chart_note,
    empty_note_extent,
)
from jolt_toolkit.report_generator.columns import HEADERS, _is_nan

logger = logging.getLogger(__name__)


def _write_na(ws, ri: int, ci: int, cell_format) -> None:
    """Write the ``=NA()`` "no data" formula with an EMPTY cached result.

    xlsxwriter's ``write`` / ``write_formula`` default the cached formula result
    to ``0``. Readers that do not recalculate the workbook — openpyxl
    ``data_only=True`` and ``pandas.read_excel`` — then surface a blank cell as
    ``0`` instead of NaN. For the ``Vehicle Mass (kg)`` column that
    mis-classifies a no-GVM leg (T88RNW / YN75NMA …) as mass-bearing, and more
    generally makes every empty numeric cell read as a spurious ``0``.

    Passing an empty string as the cached result makes those readers see the
    cell as blank (``None`` → NaN), which matches (a) Excel's own recalculated
    ``#N/A`` and (b) the convention of every report that has been round-tripped
    through openpyxl afterwards (weather / logger / charger patchers, the chart
    re-patch) — those re-saves strip xlsxwriter's cached ``0`` to blank, which is
    why the full-regen path never exhibited the ``0`` but the single-pass
    SRF-free recompute did. Excel still recalculates ``NA()`` to ``#N/A`` on
    load, so the on-screen value and the (data_only=False) formula are unchanged.
    """
    ws.write_formula(ri, ci, "=NA()", cell_format, "")


def _write_report_sheet(workbook, ws, rows: list[tuple], headers: tuple) -> None:
    """Populate the ``Report`` worksheet: formats, header row, coloured data
    rows and the frozen header pane. Verbatim block from ``_write_excel_report``."""
    # ── Formats ───────────────────────────────────────────────────────────
    # Green = trip / discharge, Red = charging, White = stop (between events).
    _FMT = {}
    for color_name, bg in [
        ("green", "#C6EFCE"),
        ("red", "#FFC7CE"),
        ("white", "#FFFFFF"),
    ]:
        _FMT[color_name] = {
            "ts": workbook.add_format(
                {
                    "align": "center",
                    "bg_color": bg,
                    "num_format": "yyyy-mm-dd hh:mm:ss",
                    "border": 1,
                    "border_color": "#D0D0D0",
                }
            ),
            "dur": workbook.add_format(
                {
                    "align": "center",
                    "bg_color": bg,
                    "num_format": "[hh]:mm:ss",
                    "border": 1,
                    "border_color": "#D0D0D0",
                }
            ),
            "url": workbook.add_format(
                {
                    "align": "center",
                    "bg_color": bg,
                    "font_color": "#0000FF",
                    "underline": True,
                    "border": 1,
                    "border_color": "#D0D0D0",
                }
            ),
            "def": workbook.add_format(
                {
                    "align": "center",
                    "bg_color": bg,
                    "border": 1,
                    "border_color": "#D0D0D0",
                }
            ),
        }
    header_fmt = workbook.add_format({"bold": True, "align": "center"})

    # ── Headers ───────────────────────────────────────────────────────────
    for ci, h in enumerate(headers):
        ws.write(0, ci, h, header_fmt)
        if h in (
            "Leg Type",
            "Origin (Lat, Lon)",
            "Origin Place",
            "Destination (Lat, Lon)",
            "Destination Place",
            "Telematics Link",
            "Charger Link",
            "SRF Logger Link",
        ):
            ws.set_column(ci, ci, 30)
        elif h in (
            "Energy Performance Corrected by Elevation Difference (kWh/km)",
            "Energy Performance Kinetics Corrected (kWh/km)",
            "Histogram of Accelerator Pedal Position",
            "Histogram of Decelerator Pedal Position",
        ):
            ws.set_column(ci, ci, len(h) + 4)
        else:
            ws.set_column(ci, ci, max(len(h) + 2, 12))

    # ── Data rows ─────────────────────────────────────────────────────────
    for ri, row in enumerate(rows, start=1):
        leg_type = row[0]  # first field after Leg Number
        is_stop = bool(
            leg_type
            and isinstance(leg_type, str)
            and leg_type.strip().lower() == "stop"
        )
        is_charge = (not is_stop) and bool(
            leg_type
            and re.match(r"^(AC|DC|Charge|Mix|estimated)", leg_type, re.IGNORECASE)
        )
        if is_stop:
            cname = "white"
        elif is_charge:
            cname = "red"
        else:
            cname = "green"
        fmt = _FMT[cname]

        ws.write(ri, 0, ri, fmt["def"])  # Leg Number

        for ci_offset, val in enumerate(row):
            ci = ci_offset + 1  # +1 for Leg Number column
            col_name = headers[ci]
            if (
                col_name in ("Telematics Link", "Charger Link", "SRF Logger Link")
                and val
            ):
                ws.write_url(ri, ci, val, string="Link", cell_format=fmt["url"])
            elif col_name == "Duration (HH:MM:SS)":
                if val is not None and not _is_nan(val):
                    ws.write(ri, ci, val, fmt["dur"])
                else:
                    _write_na(ws, ri, ci, fmt["dur"])
            elif col_name in ("Start Time (UTC)", "End Time (UTC)") and isinstance(
                val, pd.Timestamp
            ):
                # xlsxwriter needs a naive datetime value for timestamp formats
                naive = val.tz_localize(None) if val.tzinfo else val
                ws.write_datetime(ri, ci, naive.to_pydatetime(), fmt["ts"])
            elif _is_nan(val):
                _write_na(ws, ri, ci, fmt["def"])
            elif val is None:
                ws.write(ri, ci, "", fmt["def"])
            else:
                ws.write(ri, ci, val, fmt["def"])

    ws.freeze_panes(1, 0)


def _write_graphs_sheet(workbook, rows: list[tuple], headers: tuple) -> None:
    """Add the ``Graphs`` + hidden ``GraphsData`` worksheets (scatter charts /
    trendlines / no-data note panels). Verbatim block from ``_write_excel_report``."""
    # ── Graphs worksheet ──────────────────────────────────────────────────
    # Charts are driven by the shared CHART_SPECS config so every report uses
    # identical fixed axes and identical data filtering. Because xlsxwriter
    # charts reference cell ranges and cannot filter in place, the cleaned
    # (x, y) pairs for each chart are written to a hidden ``GraphsData`` sheet;
    # both the scatter series and its linear trendline point at that clean
    # block (fit on filtered data only, matching the paper's convention).
    graphs_ws = workbook.add_worksheet("Graphs")
    data_ws = workbook.add_worksheet("GraphsData")
    data_ws.hide()

    specs = chart_specs_for(headers)
    # Row-relative index map: ``rows`` tuples omit the leading 'Leg Number'
    # column, so a header's position in a row is its HEADERS index minus one.
    row_col_idx = {h: i - 1 for i, h in enumerate(headers) if i >= 1}

    # Format for the graceful-degradation note panel (created once; harmless if a
    # report never has an empty chart). Centred grey italic text on a light panel
    # with a thin border, so a chart that can plot nothing reads as a deliberate
    # "no data" note rather than a broken empty chart frame.
    empty_note_fmt = workbook.add_format(
        {
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "italic": True,
            "font_size": CHART_STYLE["empty_note_font_size"],
            "font_color": "#" + CHART_STYLE["empty_note_rgb"],
            "bg_color": "#" + CHART_STYLE["empty_note_fill_rgb"],
            "border": 1,
            "border_color": "#" + CHART_STYLE["empty_note_border_rgb"],
        }
    )

    data_col = 0  # next free column pair on GraphsData
    for gi, spec in enumerate(specs):
        x_hdr, y_hdr = spec["x_hdr"], spec["y_hdr"]
        if x_hdr not in row_col_idx or y_hdr not in row_col_idx:
            continue
        pts = _filtered_chart_points(rows, row_col_idx, spec)

        # Write the filtered pairs to the hidden helper block (with a header row
        # so the range is self-describing if anyone unhides the sheet).
        xcol, ycol = data_col, data_col + 1
        data_ws.write(0, xcol, x_hdr)
        data_ws.write(0, ycol, y_hdr)
        for di, (xv, yv) in enumerate(pts, start=1):
            data_ws.write_number(di, xcol, xv)
            data_ws.write_number(di, ycol, yv)
        n_pts = len(pts)
        # Advance the GraphsData column pair now so it stays in lockstep whether
        # this chart is drawn or replaced by a note.
        data_col += 2

        # Graceful degradation: with no plottable points (e.g. a vehicle whose
        # mass column is null for the whole period → EP-vs-Mass has nothing to
        # plot) we replace the empty scatter frame with a centred text note in the
        # chart's footprint, leaving the other charts' positions untouched. A
        # merged cell is used (not a textbox) because it is the one primitive the
        # openpyxl patch path can write identically.
        if n_pts == 0:
            fr, fc, lr, lc = empty_note_extent(gi)
            graphs_ws.merge_range(
                fr, fc, lr, lc, empty_chart_note(spec), empty_note_fmt
            )
            continue

        # Shared single-colour scatter look. A scatter series with no explicit
        # fill makes Excel auto-vary the per-point colour (the "five colours"
        # artefact); pinning one steelblue solidFill + alpha + no border gives
        # the soft uniform look. The fit equation + R² go into a title sub-line
        # (see ``_chart_subtitle``) rather than overlapping floating data labels.
        scatter_hex = "#" + CHART_STYLE["scatter_rgb"]
        title = spec.get("title", f"{y_hdr} vs {x_hdr}")
        subtitle = _chart_subtitle(pts) if CHART_STYLE["show_fit_in_subtitle"] else ""

        chart = workbook.add_chart({"type": "scatter"})
        if n_pts > 0:
            chart.add_series(
                {
                    "name": spec.get("series_name", y_hdr),
                    "categories": ["GraphsData", 1, xcol, n_pts, xcol],
                    "values": ["GraphsData", 1, ycol, n_pts, ycol],
                    "trendline": {
                        "type": "linear",
                        "display_equation": False,
                        "display_r_squared": False,
                        "line": {"color": "#404040", "width": 1.25},
                    },
                    "marker": {
                        "type": "circle",
                        "size": CHART_STYLE["marker_size"],
                        # xlsxwriter transparency = 100 − opacity (so the openpyxl and
                        # xlsxwriter paths render the same soft fill).
                        "fill": {
                            "color": scatter_hex,
                            "transparency": 100 - CHART_STYLE["scatter_opacity"],
                        },
                        "border": {"none": True},
                    },
                }
            )
        # Light-grey, high-transparency major gridlines on BOTH axes; minor
        # gridlines off. xlsxwriter wants the complementary transparency
        # (100 − opacity) and a pt line width, so the openpyxl patch and this
        # path render the same soft grid.
        grid_hex = "#" + CHART_STYLE["grid_rgb"]
        major_gridlines = {
            "visible": True,
            "line": {
                "color": grid_hex,
                "transparency": 100 - CHART_STYLE["grid_opacity"],
                "width": CHART_STYLE["grid_width_emu"] / 12700.0,
            },  # EMU → pt
        }
        minor_gridlines = {"visible": False}
        chart.set_x_axis(
            {
                # Axis DISPLAY title is decoupled from the data header: the series
                # still points at ``x_hdr``; only the printed label may differ.
                "name": spec.get("x_title", x_hdr),
                "name_font": {
                    "size": CHART_STYLE["axis_title_font_size"],
                    "bold": False,
                },
                "num_font": {"size": CHART_STYLE["tick_font_size"]},
                "min": spec["x_min"],
                "max": spec["x_max"],
                "major_unit": spec["x_major"],
                "major_gridlines": major_gridlines,
                "minor_gridlines": minor_gridlines,
            }
        )
        chart.set_y_axis(
            {
                # Axis DISPLAY title decoupled from the data header (mirrors x).
                "name": spec.get("y_title", y_hdr),
                "name_font": {
                    "size": CHART_STYLE["axis_title_font_size"],
                    "bold": False,
                },
                "num_font": {"size": CHART_STYLE["tick_font_size"]},
                "min": spec["y_min"],
                "max": spec["y_max"],
                "major_unit": spec["y_major"],
                "major_gridlines": major_gridlines,
                "minor_gridlines": minor_gridlines,
            }
        )
        # Title on its own top band; fit equation + R² as a smaller 2nd line so
        # the two no longer overlap each other or sit on top of the points.
        title_name = f"{title}\n{subtitle}" if subtitle else title
        chart.set_title(
            {
                "name": title_name,
                "name_font": {"size": CHART_STYLE["title_font_size"], "bold": True},
            }
        )
        # Legend in the top-right corner, not overlaying the plot.
        chart.set_legend(
            {
                "position": CHART_STYLE["legend_pos_xlsxwriter"],
                "font": {"size": CHART_STYLE["legend_font_size"]},
            }
        )
        # Pin the inner plot area with fixed margins (shared CHART_STYLE
        # fractions) so the axis titles always sit outside their tick labels —
        # without this Excel auto-grows the plot box and the big-font axis
        # titles overlap the tick numbers.
        chart.set_plotarea(
            {
                "layout": {
                    "x": CHART_STYLE["plot_x"],
                    "y": CHART_STYLE["plot_y"],
                    "width": CHART_STYLE["plot_w"],
                    "height": CHART_STYLE["plot_h"],
                },
            }
        )
        # Size the chart EXPLICITLY from the shared cm constants (px = cm × 360000
        # EMU/cm ÷ 9525 EMU/px) instead of the old ``x_scale`` / ``y_scale``, so
        # this path renders at the same physical size as the openpyxl patch path
        # and one shared ``chart_row_step`` stacks them identically.
        chart.set_size(
            {
                "width": round(CHART_STYLE["chart_width_cm"] * 360_000 / 9525),
                "height": round(CHART_STYLE["chart_height_cm"] * 360_000 / 9525),
            }
        )
        # Anchor each chart a fixed number of rows below the previous one — the
        # step (chart height in rows + gap) comes from the shared ``chart_row_step``
        # helper, so charts never overlap and both render paths match.
        graphs_ws.insert_chart(f"A{gi * chart_row_step() + 1}", chart)


def _write_definitions_sheet(workbook, headers: tuple) -> None:
    """Add the ``Definitions`` worksheet (EV or diesel field glossary).
    Verbatim block from ``_write_excel_report``."""
    # ── Definitions worksheet ─────────────────────────────────────────────
    defs_ws = workbook.add_worksheet("Definitions")
    def_fmt = workbook.add_format({"text_wrap": True, "valign": "top"})
    if "Fuel Consumption (L/100km)" in headers:
        # Diesel report definitions
        def_texts = [
            'Leg Type: "In Transit" = trip (green); "Stop" = parked/idling gap between trips (white). '
            "Diesel vehicles have no charging events.",
            "Vehicle Mass (kg): Gross combination vehicle weight (GCVW) read from the SRF Logger "
            '"CVW gross combination vehicle weight" channel at 1 Hz; reported value is the per-trip median.',
            "Fuel Used (L): Per-trip fuel consumption, computed from cumulative differences of the "
            '"LFC engine total fuel used" channel over the trip window.',
            "Fuel Consumption (L/100km): Fuel Used (L) / Distance (km) × 100.",
            'Energy Source: "lfc_fuel" = trip energy reconstructed from LFC fuel used × diesel LHV (default 10 kWh/L). '
            "Kept for cross-fuel comparability with the EV report.",
            "SRF Logger Link: URL to the SRF Logger visualisation on the SRF platform.",
            "Average Temperature (C): Per-trip mean of AMB ambient air temperature (Logger 1 Hz).",
            'Elevation Difference (m): End − start of Channel 2 "2 altitude" (GPS altitude) over the trip.',
        ]
    else:
        # Electric report definitions
        def_texts = [
            "SOC: State of Charge. The percentage of the battery capacity that is currently charged.",
            'Energy Source: "ac_dc" = charging energy from AC+DC telematics counters; '
            + '"total_energy" = discharge energy from total_electric_energy_used_plugged_in_included; '
            + '"moving_energy" = discharge energy from electric_energy_wheelbased_speed_over_zero; '
            + '"soc_estimate" = energy estimated from SOC change × nominal capacity; '
            + '"soc_fallback" = discharge energy re-derived from SOC change × effective '
            + "capacity where the energy counter anchor was stale (outlier implied "
            + "capacity with a large, reliable SOC change).",
            "Energy Performance Kinetics Corrected (kWh/km): Elevation + per-second kinetic energy "
            + "corrected energy performance. Uses Logger 1Hz speed data to compute ΔKE per second, "
            + "with 90% regenerative braking efficiency (η_regen = 0.90). "
            + "Net KE = Σ(accel ΔKE) − η_regen × Σ(|braking ΔKE|).",
            "Charger Link / SRF Logger Link: URL to the add-on sensor visualisation on the SRF platform. "
            + "Derived metrics (Peak Charging, Wire Efficiency, Weather, etc.) are reserved for future work.",
            "Battery Capacity (kWh): Effective capacity estimated by the segment algorithm from "
            + "delta_energy_kwh / (delta_soc / 100). Not the nominal manufacturer value.",
            "Energy Performance (kWh/km): |delta_energy_kwh| / distance for discharge trips "
            + "with distance > 0. Only meaningful for discharge segments.",
        ]
    for dr, dt in enumerate(def_texts):
        defs_ws.write(dr, 0, dt, def_fmt)
    defs_ws.set_column(0, 0, max(len(t) for t in def_texts) * 0.9, def_fmt)


def _write_excel_report(
    rows: list[tuple],
    reg: str,
    period_start: date,
    period_end: date,
    out_path: Path,
    headers: tuple = HEADERS,
) -> None:
    """
    Write the report rows to Excel, matching the legacy JOLT formatting:
      - green background:  Trip
      - red background:    Charge
      - white background:  Stop
      - timestamp format:  yyyy-mm-dd hh:mm:ss
      - duration format:   [hh]:mm:ss (stored as a fraction of a day)
      - hyperlinks:        blue underline
      - third worksheet:   field definitions

    ``headers`` controls which column layout is used: pass ``HEADERS`` for EVs
    (the default) and ``DIESEL_HEADERS`` for diesel vehicles. The first column of
    both header sets is 'Leg Number'; the remaining fields map one-to-one to the
    order of the row tuple.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(str(out_path), {"nan_inf_to_errors": True})
    ws = workbook.add_worksheet("Report")
    _write_report_sheet(workbook, ws, rows, headers)
    _write_graphs_sheet(workbook, rows, headers)
    _write_definitions_sheet(workbook, headers)

    workbook.set_properties(
        {
            "title": f"JOLT Segment Report — {reg}",
            "subject": f"{period_start} to {period_end}",
            "author": "Centre for Sustainable Road Freight",
            "comments": "Generated by jolt_toolkit.report_generator",
        }
    )

    workbook.close()
    logger.info("  Excel report: %s", out_path.name)
