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
src/jolt_toolkit/          # the code workspace (this is the whole deliverable)
├── report_generator/      # fetch → segment → correct → write pipeline + CLI + patchers
├── configs/               # vehicles.json / pipelines.json / plot_config.json
├── analysis/              # shared analysis helpers (counter interpolation, stats, physics)
├── README.md              # architecture reference — module map, config schema, data model
├── DEPLOYMENT.md          # ← START HERE for deployment: env vars, state, caches, contracts
├── versions.md            # version history
└── requirements.txt       # runtime dependencies (travels with the folder)
tests/                     # offline contract tests (no network, no API key needed)
```

## Not a pip package

`jolt_toolkit` is a **vendored code workspace**, deliberately not installable: there is no
`[project]` table, no wheel, no console script. Vendor the folder and put `src/` on the
import path. `pyproject.toml` here carries tool configuration only (black / isort / pytest).

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate     # or conda create -n jolt python=3.11
pip install -r requirements.txt

cp .env.example .env                              # then fill in SRF_API_KEY

# generate one report (PYTHONPATH puts the workspace on the import path)
PYTHONPATH=src python -m jolt_toolkit.report_generator.cli \
    -veh YK73WFN -ds 2025-03-01 -de 2025-06-01 --out-dir ./out
```

Or from your own code:

```python
from jolt_toolkit.report_generator import JOLTReportGenerator

gen = JOLTReportGenerator(report_output_folder="./out")
gen.generate_report("YK73WFN", "2025-03-01", "2025-06-01")
```

Output: `<out_dir>/<REG>/jolt_report_<REG>_<start>_<end>.xlsx`.

Instead of exporting `PYTHONPATH` on every call, you can drop a `.pth` file containing the
absolute path to `src/` into your environment's `site-packages` — then `import jolt_toolkit`
just works.

## Verify the install

```bash
pip install -r requirements-dev.txt
pytest                     # offline: column contracts, config integrity, imports, CLI
```

## Vehicles that are not configured

`configs/vehicles.json` holds the tuned parameters for the known fleet. A registration that
is **not** in it still produces a report: the generator resolves the vehicle from the SRF
platform, auto-detects its telematics columns and falls back to generic segmentation
parameters. Both electric and diesel vehicles are covered, and the only hard failure is a
registration that does not exist on the SRF platform at all. Report quality is generic
rather than tuned — see `src/jolt_toolkit/README.md` for what degrades.

## Before you deploy

Read **`src/jolt_toolkit/DEPLOYMENT.md`**. The points most likely to bite:

- **Writable state** — the effective-capacity ledger is persisted back into
  `configs/vehicles.json`. Point `JOLT_CONFIG_DIR` at a writable copy of `configs/`.
- **Caches** — set `JOLT_CACHE_DIR` to a persistent volume; the SRF raw-data cache makes
  re-runs dramatically cheaper, and the weather cache protects a paid API quota.
- **No paid API calls by default** — report generation uses the SRF logger's own weather
  channel. The OpenWeather back-fill is a separate, optional, quota-consuming post-step.
- **Do not "fix" the known quirks** listed at the end of `DEPLOYMENT.md` (the EV vs diesel
  column layouts, the append-only column contract, the `=NA()` empty-cell convention).

## Provenance

Extracted from the internal JOLT research project at toolkit version `3.2.0`. Documentation
inside `src/jolt_toolkit/` occasionally references internal paths (`.claude/skills/...`) for
capabilities that stayed behind in that project — validation-figure rendering, dashboards,
report post-processing. Those are not part of this repository and are not required to
generate a report.
