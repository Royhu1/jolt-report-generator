"""Regenerate the frozen segmentation golden files under ``tests/fixtures/expected/``.

Run it from the repository root, offline:

    python tests/fixtures/regenerate_goldens.py

It re-runs the current code over the committed anonymised raw fixtures with the
FROZEN alias configs (``tests/fixtures/configs/``) and rewrites:

    tests/fixtures/expected/segments_EVSPD01.json     EV, speed branch
    tests/fixtures/expected/segments_EVSOC01.json     EV, SOC branch
    tests/fixtures/expected/segments_EVMAD01.json     EV, mad_tw_mean + no merge
    tests/fixtures/expected/diesel_segments_DSL01.json  diesel logger trips

Only regenerate when a behaviour change is INTENDED. The whole point of the
goldens is that an unintended change to the segmentation maths turns the suite
red; review the JSON diff before committing it.

No network and no ``SRF_API_KEY`` are needed — everything reads the committed
CSVs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

import pandas as pd  # noqa: E402

# Importing the root conftest sets JOLT_CACHE_DIR (and the other offline env
# vars) BEFORE jolt_toolkit is imported — exactly as it does under pytest — and
# gives us the one shared segment serialiser.
import conftest as _conftest  # noqa: E402  (import order is deliberate)
from jolt_toolkit.report_generator import diesel_pipeline as dp  # noqa: E402
from jolt_toolkit.report_generator.segment_algorithms import (  # noqa: E402
    run_segment_detection,
)
from jolt_toolkit.report_generator.segmentation import constants  # noqa: E402

FIXTURES = Path(__file__).resolve().parent
EXPECTED = FIXTURES / "expected"

EV_ALIASES = ("EVSPD01", "EVSOC01", "EVMAD01")


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


def main() -> int:
    vehicles = _load_frozen_configs()
    EXPECTED.mkdir(parents=True, exist_ok=True)

    for alias in EV_ALIASES:
        charge, discharge = ev_segments(alias, vehicles)
        payload = {
            "alias": alias,
            "source": _conftest.RAW_FIXTURES[alias],
            "charge": _conftest.serialise_segments(charge),
            "discharge": _conftest.serialise_segments(discharge),
        }
        out = EXPECTED / f"segments_{alias}.json"
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(
            f"wrote {out.relative_to(REPO_ROOT)}  "
            f"(charge={len(charge)}, discharge={len(discharge)})"
        )

    segs = diesel_segments("DSL01", vehicles)
    payload = {
        "alias": "DSL01",
        "source": _conftest.RAW_FIXTURES["DSL01"],
        "trips": _conftest.serialise_segments(segs),
    }
    out = EXPECTED / "diesel_segments_DSL01.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}  (trips={len(segs)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
