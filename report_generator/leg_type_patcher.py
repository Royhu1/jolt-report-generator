"""
report_generator.leg_type_patcher
=================================
Relabel the ``Leg Type`` column of existing report workbooks in place.

Each workbook is labelled from its own rows — the kind, times, positions,
distance and operator of every row — with the same second pass the generator
runs on a new report (:func:`report_generator.depots.assign_leg_types`): the
bases of every operator are found from the report's overnight stays (else its
charge sites, else its trip endpoints), and every trip and charge row is
labelled against them. A report the generator writes therefore comes out of this
patcher unchanged, and so does a report this patcher has already relabelled.

The glossary follows the labels: the ``Definitions`` sheet's ``Leg Type`` entry
(the cell in column A starting ``"Leg Type:"``) is set to the current definition
(:func:`report_generator.depots.leg_type_definition`, EV or diesel as the
``Report`` header says), rewritten in place when it differs — a diesel report's
old entry — or appended after the last entry when the report predates it, as an
EV report written before the labels described the bases does. That is where a
new report has it (the first diesel entry, the last EV one), so a patched
report's glossary is laid out as a new one's. No other ``Definitions`` row is
touched, and a workbook without that sheet keeps none.

Only the ``Leg Type`` cells of the ``Report`` sheet whose label changes and that
one ``Definitions`` entry are written; every other cell, sheet and style is left
as it is (the workbook is opened and saved with openpyxl, as the other patchers
do, and replaced atomically). A workbook that needs no change is not saved at
all. EV and diesel layouts are both handled — any ``Report`` sheet whose header
row starts with ``Leg Number`` and holds the columns the labelling reads. A
workbook open in Excel (its ``~$`` lock file present) is skipped.

Usage::

    python -m report_generator.leg_type_patcher <workbook | vehicle dir | tree dir>
        [--dry-run] [--json SUMMARY.json]

A directory is searched for ``jolt_report_*.xlsx`` (a vehicle directory), or
one level down (a tree of vehicle directories); ``*_finetuned*`` reports and
Excel lock files are skipped. ``--dry-run`` reports what would change — the
label changes, the glossary entry, the bases found per operator with their
position and support, and the trips from one base to another — and writes
nothing; ``--json`` also writes the per-workbook summaries to a file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from copy import copy
from pathlib import Path

from openpyxl import load_workbook

from report_generator.columns import DIESEL_HEADERS, HEADERS, _row_col_index
from report_generator.depots import (
    assign_leg_types,
    describe_bases,
    leg_type_definition,
)
from report_generator.xlsx_patch_common import save_workbook_atomically

logger = logging.getLogger(__name__)

_SHEET = "Report"
_DEFINITIONS_SHEET = "Definitions"
_DEFINITION_PREFIX = "Leg Type:"

#: What happened to the ``Definitions`` sheet's ``Leg Type`` entry (the summary's
#: ``definition``): already current, rewritten, appended, or no sheet to hold it.
DEFINITION_UNCHANGED = "unchanged"
DEFINITION_UPDATED = "updated"
DEFINITION_ADDED = "added"
DEFINITION_NO_SHEET = "no sheet"


def _layout(header: tuple) -> str:
    """``"ev"`` / ``"diesel"`` for the two report layouts (a narrower header —
    a report written before trailing columns were appended — included), else
    ``"other"``."""
    for name, full in (("ev", HEADERS), ("diesel", DIESEL_HEADERS)):
        if len(header) > 2 and header == full[: len(header)]:
            return name
    return "other"


def _header(ws) -> tuple:
    header = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    while header and header[-1] is None:
        header.pop()
    return tuple(header)


def _sync_definition(wb, header: tuple) -> str:
    """Set the ``Definitions`` sheet's ``Leg Type`` entry to the current text.

    The first cell of column A that starts with ``"Leg Type:"`` is rewritten when
    it differs; without one, the entry is appended after the last non-empty
    cell, in that cell's style. Returns one of the ``DEFINITION_*`` values; the
    workbook is changed only in memory (the caller saves it or not).
    """
    if _DEFINITIONS_SHEET not in wb.sheetnames:
        return DEFINITION_NO_SHEET
    ws = wb[_DEFINITIONS_SHEET]
    text = leg_type_definition(diesel="Fuel Consumption (L/100km)" in header)
    last = 0
    for r in range(1, ws.max_row + 1):
        value = ws.cell(r, 1).value
        if isinstance(value, str) and value.startswith(_DEFINITION_PREFIX):
            if value == text:
                return DEFINITION_UNCHANGED
            ws.cell(r, 1).value = text
            return DEFINITION_UPDATED
        if value not in (None, ""):
            last = r
    cell = ws.cell(last + 1, 1)
    cell.value = text
    if last:
        cell._style = copy(ws.cell(last, 1)._style)
    return DEFINITION_ADDED


def patch_workbook(path: str | Path, *, dry_run: bool = False) -> dict:
    """Relabel one workbook in place (or, with ``dry_run``, only report).

    Returns the labelling summary
    (:meth:`report_generator.depots.LegTypeAssignment.summary`) with the
    workbook's ``path``, its ``layout``, what was (or, in a dry run, would be)
    done to the glossary's ``Leg Type`` entry (``definition``: one of the
    ``DEFINITION_*`` values) and whether the workbook was ``written``. Raises
    ``ValueError`` when the workbook has no ``Report`` sheet or its header lacks
    a column the labelling reads.
    """
    path = Path(path)
    wb = load_workbook(str(path))
    try:
        if _SHEET not in wb.sheetnames:
            raise ValueError(f"{path.name}: no '{_SHEET}' sheet")
        ws = wb[_SHEET]
        header = _header(ws)
        excel_rows, rows = [], []
        for excel_row, values in enumerate(
            ws.iter_rows(min_row=2, max_col=len(header), values_only=True), start=2
        ):
            if all(v is None for v in values):
                continue
            excel_rows.append(excel_row)
            rows.append(list(values[1:]))
        result = assign_leg_types(rows, header)  # raises on a missing column
        i_type = _row_col_index("Leg Type", header)
        for excel_row, row, label in zip(excel_rows, rows, result.labels):
            if row[i_type] != label:
                ws.cell(excel_row, i_type + 2).value = label
        definition = _sync_definition(wb, header)
        changed = bool(result.n_changed) or definition in (
            DEFINITION_UPDATED,
            DEFINITION_ADDED,
        )
        written = changed and not dry_run
        if written:
            save_workbook_atomically(wb, path)
    finally:
        wb.close()
    return {
        "path": str(path),
        "layout": _layout(header),
        "written": written,
        "definition": definition,
        **result.summary(),
    }


def collect_workbooks(target: str | Path) -> list[Path]:
    """The report workbooks under ``target``: the file itself, the
    ``jolt_report_*.xlsx`` of a vehicle directory, or those one level down in a
    tree of vehicle directories; ``*_finetuned*`` and ``~$*`` are skipped."""
    target = Path(target)
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []

    def reports_in(d: Path) -> list[Path]:
        return sorted(
            p
            for p in d.glob("jolt_report_*.xlsx")
            if "_finetuned" not in p.name and not p.name.startswith("~$")
        )

    direct = reports_in(target)
    if direct:
        return direct
    found: list[Path] = []
    for sub in sorted(p for p in target.iterdir() if p.is_dir()):
        found.extend(reports_in(sub))
    return found


def _lock_file(path: Path) -> Path:
    return path.with_name("~$" + path.name)


def _print_summary(summary: dict, dry_run: bool) -> None:
    name = Path(summary["path"]).name
    verb = "would change" if dry_run else "changed"
    print(
        f"{name} [{summary['layout']}]: {summary['trips']} trips, "
        f"{summary['charges']} charges; {verb} {summary['changed']}; "
        f"{summary['base_to_base']} base-to-base trips; Leg Type definition "
        f"{summary['definition']}"
    )
    for change, n in summary["changes"].items():
        print(f"    {change}: {n}")
    for line in describe_bases(summary["bases"]):
        print(f"    bases, {line}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code (0 = every workbook
    processed, 1 = nothing found or a workbook failed)."""
    parser = argparse.ArgumentParser(
        prog="python -m report_generator.leg_type_patcher",
        description="Relabel the Leg Type column of report workbooks in place, "
        "from each workbook's own rows: the vehicle's bases per operator, then "
        "every trip and charge against them. Only Leg Type cells are written.",
    )
    parser.add_argument(
        "target",
        help="A jolt_report_*.xlsx, a vehicle directory, or a directory of "
        "vehicle directories.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the label changes and the bases found; write nothing.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Also write the per-workbook summaries to this JSON file.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    workbooks = collect_workbooks(args.target)
    if not workbooks:
        logger.error("No jolt_report_*.xlsx found: %s", args.target)
        return 1

    summaries, failures = [], 0
    for path in workbooks:
        if _lock_file(path).exists():
            logger.error(
                "Skipping %s: open in Excel (%s)", path.name, _lock_file(path).name
            )
            failures += 1
            continue
        try:
            summary = patch_workbook(path, dry_run=args.dry_run)
        except Exception as exc:  # one bad workbook must not stop the rest
            logger.error("Skipping %s: %s", path.name, exc)
            failures += 1
            continue
        summaries.append(summary)
        _print_summary(summary, args.dry_run)

    total_changed = sum(s["changed"] for s in summaries)
    total_b2b = sum(s["base_to_base"] for s in summaries)
    definitions = sum(
        s["definition"] in (DEFINITION_UPDATED, DEFINITION_ADDED) for s in summaries
    )
    print(
        f"{len(summaries)} workbook(s), {total_changed} label(s) and "
        f"{definitions} Leg Type definition(s) "
        f"{'would change' if args.dry_run else 'changed'}, "
        f"{total_b2b} base-to-base trip(s), {failures} skipped"
    )
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"dry_run": args.dry_run, "workbooks": summaries}, indent=2),
            encoding="utf-8",
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
