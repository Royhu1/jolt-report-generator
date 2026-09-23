# jolt-report-generator

Generates the JOLT Excel report for a vehicle over a date range: it pulls the vehicle's
legs and raw telematics from the SRF platform, segments them into trips / charges / stops,
computes the energy and mass metrics, and writes a formatted `.xlsx`.

This repository is the **deployable surface only**. It is extracted from the JOLT research
project, which additionally carries dashboards, figure rendering, post-processing tools and
research workspaces — none of which are needed to produce a report, and none of which are
here.

## What you get

```
report_generator/          # the package — this is the whole deliverable
├── _generator.py          # the fetch → segment → correct → write pipeline
├── cli.py                 # python -m report_generator.cli
├── segmentation/          # trip / charge detection, mass clustering
├── weather_fetcher/       # optional OpenWeather back-fill
├── configs/               # vehicles.json (+ capacity ledger) / pipelines.json
├── version.py             # __version__ + DATA_NAMESPACE
└── …                      # capacity model, row builder, Excel writer, patchers
doc/
├── architecture.md        # module map, config schema, data model, pipeline walkthrough
├── deployment.md          # ← START HERE for deployment: env vars, state, caches, contracts
└── versions.md            # version history
tests/                     # offline test suite (no network, no API key needed)
├── unit/                  # pure functions, hand-computed expectations
├── integration/           # multi-module runs over anonymised real telematics
└── fixtures/              # the anonymised CSVs, frozen configs and golden snapshots
```

## Not a pip package

`report_generator` is a **vendored code workspace**, deliberately not installable: there is
no `[project]` table, no wheel, no console script. Copy the folder and put its parent
directory on the import path. `pyproject.toml` here carries tool configuration only
(black / isort / pytest).

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate     # or conda create -n jolt python=3.11
pip install -r requirements.txt

cp .env.example .env                              # then fill in SRF_API_KEY

# generate one report (run from the repository root, which is on the import path)
python -m report_generator.cli \
    -veh YK73WFN -ds 2025-03-01 -de 2025-06-01 --out-dir ./out
```

Or from your own code:

```python
from report_generator import JOLTReportGenerator

gen = JOLTReportGenerator(report_output_folder="./out")
gen.generate_report("YK73WFN", "2025-03-01", "2025-06-01")
```

Output: `<out_dir>/<REG>/jolt_report_<REG>_<start>_<end>.xlsx`.

From anywhere other than the repository root, put the root on the import path —
`PYTHONPATH=/path/to/jolt-report-generator`, or a `.pth` file holding that absolute path
dropped into your environment's `site-packages`.

## Verify the install

```bash
pip install -r requirements-dev.txt
pytest                     # ~45 s, ~1040 tests, fully offline
```

No `SRF_API_KEY`, no network and no writable state outside the temp directory:
outbound sockets are blocked for the whole session, and everything that would
talk to SRF, OpenWeather or a geocoder is either injected or mocked.

The suite covers the data-processing logic, not just imports: the segmentation
branches, mass aggregation, the effective-capacity model, the diesel pipeline,
Excel writing and the capacity ledger all run against committed **anonymised real
telematics** and are compared field by field against frozen golden snapshots.
See `tests/README.md` for the layout and `tests/fixtures/README.md` for what the
fixtures contain and how they were de-identified.

## Vehicles that are not configured

`report_generator/configs/vehicles.json` holds the tuned parameters for the known fleet. A
registration that is **not** in it still produces a report: the generator resolves the
vehicle from the SRF platform, auto-detects its telematics columns and falls back to
generic segmentation parameters. Both electric and diesel vehicles are covered, and the
only hard failure is a registration that does not exist on the SRF platform at all. Report
quality is generic rather than tuned — see [doc/architecture.md](doc/architecture.md) for
what degrades.

## Before you deploy

Read **[doc/deployment.md](doc/deployment.md)**. The points most likely to bite:

- **Writable state** — the effective-capacity ledger is persisted back into
  `configs/vehicles.json`. Point `JOLT_CONFIG_DIR` at a writable copy of `configs/`.
- **Caches** — set `JOLT_CACHE_DIR` to a persistent volume; the SRF raw-data cache makes
  re-runs dramatically cheaper, and the weather cache protects a paid API quota.
- **No paid API calls by default** — report generation uses the SRF logger's own weather
  channel. The OpenWeather back-fill is a separate, optional, quota-consuming post-step.
- **Do not "fix" the known quirks** listed at the end of `doc/deployment.md` (the EV vs
  diesel column layouts, the append-only column contract, the `=NA()` empty-cell
  convention).

## Licence

Source code: **Apache License 2.0** (see `LICENSE`). Note the scope limit in `NOTICE` — the
licence covers the code, **not** the fleet configuration data in
`report_generator/configs/` (real registrations, measured capacities, commercial operator
names) nor the reports this software produces; those belong to the JOLT project and its
industrial partners.

The version constants live in `report_generator/version.py`; the history is in
[doc/versions.md](doc/versions.md).
