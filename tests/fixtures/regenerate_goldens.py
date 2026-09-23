"""Regenerate the frozen segmentation golden files under ``tests/fixtures/expected/``.

Run it from the repository root, offline:

    python tests/fixtures/regenerate_goldens.py                  # every fixture
    python tests/fixtures/regenerate_goldens.py --alias EVSPD02  # just one

It re-runs the current code over the committed anonymised raw fixtures listed in
``tests/fixtures/raw_fixtures.json``, with the FROZEN alias configs
(``tests/fixtures/configs/``), and rewrites one golden per fixture:

    tests/fixtures/expected/segments_<ALIAS>.json         EV raw telematics
    tests/fixtures/expected/diesel_segments_<ALIAS>.json  diesel logger trips

Only regenerate when a behaviour change is INTENDED — or, with ``--alias``, when
a newly added fixture needs its first golden. The whole point of the goldens is
that an unintended change to the segmentation maths turns the suite red; review
the JSON diff before committing it.

No network and no ``SRF_API_KEY`` are needed — everything reads the committed
CSVs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import pandas as pd  # noqa: E402

# Importing the test-suite conftest sets JOLT_CACHE_DIR (and the other offline
# env vars) BEFORE report_generator is imported — exactly as it does under
# pytest — and gives us the one shared segment serialiser and the registry.
import conftest as _conftest  # noqa: E402  (import order is deliberate)
from report_generator import diesel_pipeline as dp  # noqa: E402
from report_generator.segment_algorithms import (  # noqa: E402
    run_segment_detection,
)
from report_generator.segmentation import constants  # noqa: E402

FIXTURES = Path(__file__).resolve().parent
EXPECTED = FIXTURES / "expected"


def _load_frozen_configs() -> dict:
    with open(FIXTURES / "configs" / "vehicles.json", encoding="utf-8") as fh:
        vehicles = json.load(fh)
    with open(FIXTURES / "configs" / "pipelines.json", encoding="utf-8") as fh:
        pipelines = json.load(fh)
    constants.VEHICLE_CONFIG.update(vehicles)
    constants.PIPELINE_CONFIGS.update(pipelines)
    return vehicles


def ev_segments(alias: str, vehicles: dict):
    """Run the EV segmentation for one alias exactly as the generator does."""
    path = FIXTURES / _conftest.RAW_FIXTURES[alias]
    df = pd.read_csv(path, dtype=str)
    nominal = vehicles[alias].get("nominal_kwh")
    return run_segment_detection(
        df,
        reg=alias,
        suffix=_conftest.FIXTURE_SUFFIXES[alias],
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=nominal * 0.5 if nominal else None,
        cap_hi=nominal * 2.0 if nominal else None,
    )


def diesel_segments(alias: str, vehicles: dict):
    """Run the diesel logger segmentation for one alias from the CSV fixture."""
    path = FIXTURES / _conftest.RAW_FIXTURES[alias]
    cfg = vehicles[alias]
    df = dp._logger_df_from_csv(path, cfg)
    _trips, seg_metrics = dp._segments_from_df(df, cfg, source=path.name)
    return seg_metrics


def golden_path(alias: str) -> Path:
    """Where the golden of ``alias`` lives, by its registered kind."""
    kind = _conftest.FIXTURE_KINDS[alias]
    name = f"segments_{alias}.json" if kind == "ev" else f"diesel_segments_{alias}.json"
    return EXPECTED / name


def write_golden(alias: str, vehicles: dict) -> Path:
    """Regenerate the golden of one registered alias; return its path."""
    if alias not in vehicles:
        raise SystemExit(
            f"{alias} has no frozen config in tests/fixtures/configs/vehicles.json"
        )
    out = golden_path(alias)
    if _conftest.FIXTURE_KINDS[alias] == "ev":
        charge, discharge = ev_segments(alias, vehicles)
        payload = {
            "alias": alias,
            "source": _conftest.RAW_FIXTURES[alias],
            "charge": _conftest.serialise_segments(charge),
            "discharge": _conftest.serialise_segments(discharge),
        }
        summary = f"charge={len(charge)}, discharge={len(discharge)}"
        empty = not discharge
    else:
        segs = diesel_segments(alias, vehicles)
        payload = {
            "alias": alias,
            "source": _conftest.RAW_FIXTURES[alias],
            "trips": _conftest.serialise_segments(segs),
        }
        summary = f"trips={len(segs)}"
        empty = not segs
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}  ({summary})")
    if empty:
        print(
            f"warning: {alias} yields no trip, so its golden guards nothing and the "
            "suite rejects it — pick another raw file or a wider --rows window",
            file=sys.stderr,
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the segmentation goldens of the registered fixtures."
    )
    parser.add_argument(
        "--alias",
        action="append",
        default=None,
        help="regenerate only this registered alias (repeatable); default: all",
    )
    args = parser.parse_args(argv)
    aliases = args.alias or list(_conftest.FIXTURE_REGISTRY)
    unknown = [a for a in aliases if a not in _conftest.FIXTURE_REGISTRY]
    if unknown:
        parser.error(
            f"not in tests/fixtures/raw_fixtures.json: {', '.join(unknown)} "
            "(add the fixture with tests/fixtures/make_fixture.py first)"
        )

    vehicles = _load_frozen_configs()
    EXPECTED.mkdir(parents=True, exist_ok=True)
    for alias in aliases:
        write_golden(alias, vehicles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
