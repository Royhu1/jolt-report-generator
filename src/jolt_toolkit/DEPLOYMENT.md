# jolt_toolkit — deployment guide

> For an engineer deploying the **Excel-report generation path** (`REG + start/end
> dates → .xlsx`) on the SRF platform. Since v3.1.0 that path is the whole package —
> the developer tooling (rendering, dashboards, fine-tuning, parameter identification)
> is skill/workspace-owned, no longer in the package, and out of platform scope — see
> the last section. Architecture reference → [README.md](README.md).

## What it does

Given a vehicle registration and a date range, `jolt_toolkit` fetches SRF
telematics / logger / charger data, runs charge/discharge segmentation, and writes a
formatted multi-sheet Excel report. It is a pure batch tool: one invocation per
`(vehicle, period)` produces one `.xlsx`. No server, no database, no long-running
process.

## Install (vendoring)

Since v3.2.0 `jolt_toolkit` is a **vendored code workspace**, not an installable
package — there is no wheel, no `pip install`, and no console script. Deploy it by
copying the folder, installing its dependencies, and putting `src/` on the import
path:

```bash
# 1. Vendor the folder into your platform tree (copy src/jolt_toolkit as-is).
# 2. Install the workspace's own runtime dependencies:
pip install -r src/jolt_toolkit/requirements.txt

# 3. Put src/ on the import path — pick ONE:
export PYTHONPATH=/path/to/repo/src            # (a) env var
# (b) a .pth file in the target env's site-packages containing the absolute
#     path to <repo>/src (persistent, no env var needed), or
# (c) sys.path.insert(0, "<repo>/src") in the platform harness before import.
```

Python **≥ 3.10**. The config JSONs live in the folder itself (`configs/`), so the
vendored workspace is self-contained — no repo checkout beyond the copied folder is
required to generate a report. The package is the report-generation surface only: it
does not depend on matplotlib / scikit-learn (the rendering / dashboard / params code
is skill/research-owned — see the last section). For the test/lint toolchain, install
the repo-level `requirements.txt` (a superset that adds `pytest` / `black` / `isort` /
`mypy`).

Verify:

```bash
python -m jolt_toolkit.report_generator.cli --help          # exit 0
python -c "import jolt_toolkit; print(jolt_toolkit.__version__)"
```

## Entry points

The module CLI (identical flags to the skill's `generate_report.py`):

```bash
python -m jolt_toolkit.report_generator.cli -veh KY24LHT -ds 2025-01-01 -de 2025-01-31 [--fast] [--debug] [--raw-only] [--out-dir DIR]
```

Or from Python:

```python
from jolt_toolkit.report_generator import JOLTReportGenerator
gen = JOLTReportGenerator(report_output_folder="/data/reports", fast_mode=False)
path = gen.generate_report("KY24LHT", "2025-01-01", "2025-01-31")   # → str path or None
```

`date_end` is **inclusive**. `cli.main()` returns exit code **2** (with a clear
message) if `SRF_API_KEY` is unset or a required argument is missing, **0** on success.

## Environment variables & secrets

| Variable | Required | Purpose | Default |
|----------|----------|---------|---------|
| `SRF_API_KEY` | **yes** | SRF platform API key (Bearer token) | — (fail fast, rc 2) |
| `OPENWEATHER_API_KEYS` | no | comma-separated OpenWeather keys; only the optional weather post-step uses them | — (weather skipped) |
| `JOLT_CONFIG_DIR` | recommended | directory holding the three config JSONs **and the writable capacity ledger** | the workspace's own `configs/` dir |
| `JOLT_CACHE_DIR` | recommended | cache root | `./cache` (relative to CWD) |
| `SRF_API_ROOT` | no | SRF REST API root | `https://data.csrf.ac.uk/api/` |
| `WEATHER_CACHE_FILE` / `WEATHER_CACHE_FILE_FINE` | no | override the weather cache file paths | under `JOLT_CACHE_DIR` |

**Secrets handling**: the CLI loads a `.env` from the working directory if present
(via `python-dotenv`, `override=False` — it never overwrites an already-set env var),
then reads the environment. On the platform, inject `SRF_API_KEY` (and
`OPENWEATHER_API_KEYS`) through the secret manager / environment rather than committing
a `.env`. Nothing is logged that contains the key.

## Writable state — the capacity ledger (important)

`configs/vehicles.json` is **not purely static config**: after each EV report the
generator writes back that vehicle's measured battery capacity
(`effective_capacity_kwh` + the `effective_capacity_quarterly` ledger). If the
vendored folder sits on a read-only mount, that write-back cannot land, so:

> **Copy `configs/` to a writable location and point `JOLT_CONFIG_DIR` at it.**

```bash
cp -r "$(python -c 'import jolt_toolkit.configs, os; print(os.path.dirname(jolt_toolkit.configs.__file__))')" /var/lib/jolt/configs
export JOLT_CONFIG_DIR=/var/lib/jolt/configs
```

All three JSONs are then read from — and the ledger written to — that directory. The
write-back is guarded by a `filelock.FileLock` on `vehicles.json.lock`, and only ever
appends/updates capacity fields (from `charge`/`discharge` donor segments; fallbacks
are never written). If you prefer immutable config, the report still generates
correctly with a read-only `configs/` — the capacity write-back simply no-ops with a
logged warning, and reports fall back to `srf_capacity_kwh`.

## Caches

Set `JOLT_CACHE_DIR` to a persistent, writable directory. Layout:

| Sub-path | Content | Notes |
|----------|---------|-------|
| `srf_http/` | SRF REST HTTP responses (`SeparateBodyFileCache`) | grows with the number of distinct API calls |
| `srf_raw/` | FPS leg raw telematics CSVs (keyed by `leg.uri`) | the bulk of the growth — one CSV per leg; can reach GBs for a full-fleet history |
| `postcode_cache.json` | GPS → postcode lookups (0.001° precision) | small |
| `.weather_cache.json` / `weather/.weather_cache_fine.json` | OpenWeather results (~1 km × 1 h key) | **should be persisted to protect the OpenWeather quota** |

Caches are **safe to persist between runs** and hit deterministically — re-running the
same `(vehicle, period)` re-uses cached SRF responses and raw CSVs instead of
re-fetching. Persisting the weather cache in particular avoids re-spending the
OpenWeather quota (the fine patcher can otherwise 429 the account). Nothing in the
cache is secret. Growth is roughly proportional to the volume of distinct legs
processed; size it for the full fleet-history you intend to (re)generate.

## Output contract

```
<out_dir>/<REG>/jolt_report_<REG>_<start>_<end>.xlsx
```

`<out_dir>` = `--out-dir` (default `./excel_report_database/<package_version>`);
`<start>`/`<end>` are `YYYYMMDD`. The workbook has three sheets: **Report** (one row
per segment), **Graphs** (fixed-axis scatter charts), **Definitions** (column glossary).

`--debug` additionally persists raw artefacts under `<out_dir>/<REG>/`:
`raw_telematics/*.csv` (per-leg raw data) plus raw logger/charger CSVs. `--raw-only`
is an alias of `--debug`. Since v3.1.0 the package draws **no** validation figures and
writes **no** inspect HTML — render those from the persisted raw data via the
report-visuals skill. Production report runs need none of this — use plain mode (or
`--fast` to skip the Logger/Charger fetch entirely).

## Un-onboarded registrations (general fallback pipeline)

Any registration works — it does **not** need a `vehicles.json` entry. When the reg is
not configured, the generator resolves it on SRF (trying UK/NI registration-spacing
variants), auto-detects the fuel type and (for EV) the telematics column names, and runs
a **generic** pipeline. **Both fuel types — EV and diesel — always produce a report**: a
normal, structurally valid xlsx; only the segmentation quality is generic (onboard the
vehicle for tuned parameters + validation).
Key platform properties:

- **No writable-state side effects**: a runtime (un-onboarded) config is **never** written
  to `vehicles.json` — no invented entries, no capacity-ledger write-back (the computed
  capacity is logged only). Safe on a read-only config mount.
- **Zero paid API calls**: the fallback path makes **zero** OpenWeather calls (same as the
  onboarded default — weather stays empty unless the optional post-step is run).
- **Never an "onboard first" error**: worst case (no usable legs/channels) is a
  structurally complete, header-only report plus log warnings — not a crash.
- **The one hard failure**: a registration that does not exist on SRF at all → a single
  clear error line and a non-zero exit (rc **3**); no stack trace.

## Optional weather post-step

**Platform contract: default generation makes ZERO OpenWeather (paid-API) calls.**
Weather columns are filled during generation only from the SRF Logger weather channel
(EV via `LoggerPatcher`; diesel from Logger Channel 7 in-pipeline) — where no Logger
data exists they simply stay empty. The OpenWeather back-fill below is **optional,
quota-consuming, and excluded from the platform default workflow** — run it only as a
deliberate, separate post-step. The default coarse patcher is quota-friendly
(~2 OpenWeather lookups per trip):

```bash
python -m jolt_toolkit.report_generator.weather_patch <folder-or-xlsx>
# default coarse; add --fine-grained only if you accept the ~17k-calls/vehicle volume
```

Requires `OPENWEATHER_API_KEYS` (without it the patcher loads no keys, logs a warning
and patches nothing). Safe to re-run (cached).

## Concurrency

- The `vehicles.json` capacity write-back is `FileLock`-guarded, so concurrent runs
  cannot corrupt the ledger. Still, **run at most one generation per vehicle at a time**
  — two concurrent runs of the *same* vehicle would race on that vehicle's ledger
  entry (last writer wins) and duplicate SRF fetches.
- Different vehicles can be generated in parallel safely (separate ledger keys, shared
  read-only caches). The SRF client is created per `JOLTReportGenerator` instance.

## Not part of the platform scope

These are in-repo developer/analysis tooling, **not** the deployed report path — do not
wire them into the platform. In v3.1.0 they **left the package** and now live in the
repo skills / research workspace (they consume the package only through its public API,
e.g. `run_segment_detection(figure_hook=...)`):

- **Validation figures + inspect HTML** — the report-visuals skill
  (`.claude/skills/report-visuals/`): EV + diesel painters, overlay-regenerate, viewer.
- **Dashboards** — the generate-data-dashboard skill (offline HTML data-availability views).
- **Fine-tuning** — the report-finetuner skill (interactive segmentation correction →
  `*_finetuned` artefacts; delegates figure/HTML regeneration to report-visuals).
- **Parameter identification** (C_rr/C_dA) — `research_projects/parameter_identify/`
  (the `param-identifier` agent; needs `scikit-learn`, a repo-level dep in
  `requirements.txt`).
- **Migration / maintenance scripts** — re-homed into the owning skills'
  `tools/` / `code/` directories.

## Known quirks — do NOT "fix" these silently

An integrator reading the code may be tempted to "clean up" the following. Each is
deliberate; changing it alters output or breaks a contract.

- **Two header layouts.** EV reports use `HEADERS` (50 columns), diesel uses
  `DIESEL_HEADERS` (26 columns) — diesel is a **distinct** set (no SOC/battery/charging
  columns; carries `Fuel Used (L)` / `Fuel Consumption (L/100km)`), not a truncation of
  EV. `Operator` is the last column in **both**. Do not unify them.
- **Append-only column contract.** Patchers write into **hardcoded 1-based column
  indices** (e.g. temperature = EV column 38). New columns must be appended at the end,
  never inserted. Import-time assertions (`_COL_* == HEADERS.index(<name>)+1` in the
  three patchers, `tests/test_column_contracts.py`) fail loudly if HEADERS is reordered —
  respect them rather than deleting them.
- **`=NA()` cell contract.** Empty EP cells are written as the Excel `=NA()` formula with
  an **empty cached value** (`_write_na`). Under `openpyxl(data_only=True)`/pandas they
  read back as `#N/A` (or, for a single-pass writer, could leak `0`). Downstream readers
  must guard with a safe-number helper; `capacity_backfill` relies on the Stop-row
  `=NA()` cells being droppable. Do not replace `=NA()` with literal blanks or zeros.
- **Coarse wind-direction mean is a legacy quirk.** `WeatherPatcher` averages wind
  direction **arithmetically** (so 359°/1° can average to 180°). This is knowingly
  retained — the fine patcher fixed it with sin/cos averaging, but changing the coarse
  path would alter historical numbers. Leave it.
- **SOC = 0 is invalid, not empty battery.** The segmentation treats a telematics `SOC == 0`
  as a missing reading (set to NaN), not a flat battery. Do not "correct" this.
