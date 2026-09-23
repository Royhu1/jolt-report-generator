# report_generator — deployment contract

Integration notes for running the Excel-report generation path on the SRF platform.
Architecture reference → [architecture.md](architecture.md).

## What it does

In: a vehicle registration + an inclusive date range. Out: one formatted multi-sheet
`.xlsx` built from SRF telematics / logger / charger data.

Pure batch — one invocation per `(vehicle, period)`, no server, no database, no
long-running process. The only state written outside the output folder is the capacity
ledger (in its own file when `JOLT_CAPACITY_LEDGER` is set, otherwise inside
`vehicles.json`) and the caches (both below).

## Requirements

- Python **≥ 3.10**, `pip install -r requirements.txt` (`requirements-dev.txt` adds
  pytest for running the suite).
- Not installable: no wheel, no console script. Vendor `report_generator/` as-is (the
  config JSONs travel inside it, so the copied folder is self-contained) and put its
  **parent** directory on the import path — `PYTHONPATH=<root>`, or a `.pth` file in
  the target env's `site-packages` holding the absolute path to `<root>`.
- Smoke: `python -m report_generator.cli --help` (rc 0) and
  `python -c "import report_generator as r; print(r.__version__, r.DATA_NAMESPACE)"`.

## Entry points

```bash
python -m report_generator.cli -veh KY24LHT -ds 2025-01-01 -de 2025-01-31 \
    [--fast] [--debug] [--raw-only] [--out-dir DIR]
```

```python
from report_generator import JOLTReportGenerator
gen = JOLTReportGenerator(report_output_folder="/data/reports", fast_mode=False)
path = gen.generate_report("KY24LHT", "2025-01-01", "2025-01-31")   # → str | None
```

| Flag | Effect |
|------|--------|
| `-veh` / `-ds` / `-de` | registration, `YYYY-MM-DD` start, `YYYY-MM-DD` end (**inclusive**) |
| `--fast` | skip the SRF Logger + Charger fetch (FPS telematics only) |
| `--debug` | additionally persist raw artefacts (see output contract) |
| `--raw-only` | exact alias of `--debug` |
| `--out-dir` / `--report-output-folder` | output root; default `./excel_report_database/<DATA_NAMESPACE>` |

## Environment

| Variable | Required | Default | Controls |
|----------|----------|---------|----------|
| `SRF_API_KEY` | **yes** | — (rc 2) | SRF platform API key (Bearer token) |
| `JOLT_CAPACITY_LEDGER` | recommended | — (the ledger lives in `vehicles.json`) | path of the capacity-ledger JSON file; when set, `vehicles.json` is never written |
| `JOLT_CONFIG_DIR` | no | the vendored `configs/` | directory of the two config JSONs (`vehicles.json`, `pipelines.json`); also the capacity-ledger write target when `JOLT_CAPACITY_LEDGER` is unset |
| `JOLT_CACHE_DIR` | recommended | `./cache` (CWD-relative) | cache root |
| `SRF_API_ROOT` | no | `https://data.csrf.ac.uk/api/` | SRF REST root |
| `OPENWEATHER_API_KEYS` | no | — (weather post-step patches nothing) | comma-separated OpenWeather keys |
| `WEATHER_CACHE_FILE` | no | `<cache>/.weather_cache.json` | coarse weather cache path |
| `WEATHER_CACHE_FILE_FINE` | no | `<cache>/weather/.weather_cache_fine.json` | fine weather cache path |

The CLI loads a `.env` from the working directory if present (`python-dotenv`,
`override=False` — never overwrites an already-set variable), then reads the
environment. Inject secrets through the platform's secret manager; nothing containing
a key is logged.

The package loads the vehicle configs (and fixes the postcode-cache path) when it is
first imported, which for `python -m report_generator.cli` is before `.env` is read.
Export `JOLT_CONFIG_DIR` and `JOLT_CACHE_DIR` in the process environment rather than
relying on `.env` for them; from the Python API, set both before
`import report_generator`. `JOLT_CAPACITY_LEDGER` may be set later, from `.env` or
from code: every report re-applies the ledger when it starts
(`JOLTReportGenerator.generate_report()`, and so `report_generator.generate_report()`
and the CLI), before it reads the vehicle's capacity.

## Writable state — the capacity ledger

After each EV report the generator writes that vehicle's measured battery capacity back
(`effective_capacity_kwh` plus the per-period `effective_capacity_quarterly` history —
together, the capacity ledger), and the vehicle's next report reads it back. It is
state that must persist between runs, and it has two possible homes:

| Mode | Set | The ledger is read from and written to | `vehicles.json` |
|------|-----|-----------------------------------------|-----------------|
| **External ledger** (recommended) | `JOLT_CAPACITY_LEDGER=/state/capacity_ledger.json` | that file, created on the first write | read only, never written |
| In-config (default) | nothing, or `JOLT_CONFIG_DIR` | `vehicles.json` itself | rewritten after every EV report |

**Recommended deployment**: keep `report_generator/configs/` exactly as shipped —
read-only is fine — and point `JOLT_CAPACITY_LEDGER` at a file on a persistent,
writable volume. The tuned parameters then change only through a reviewed update of
this code, and the machine-written state lives apart from them. `JOLT_CONFIG_DIR` is
only needed to run with your own copy of `vehicles.json` / `pipelines.json`.

- **The ledger file** is a JSON object, one entry per registration:
  `{"<REG>": {"effective_capacity_kwh": <float>, "effective_capacity_quarterly":
  {"<YYYYMMDD_YYYYMMDD>": {"kwh": <float>, "n": <int>}}}}`. It is overlaid on
  `vehicles.json` whenever the configs are loaded
  (`report_generator.configs.load_vehicle_configs()`): for a registration in both, a
  key the ledger entry carries replaces the `vehicles.json` value, and a key it does
  not carry keeps it; a registration only in the ledger is ignored; a ledger file that
  does not exist yet means no overlay. A file that is not such an object fails loudly
  instead of being read as empty and overwritten.
- **Starting a ledger.** An empty (or absent) ledger works: a vehicle's first
  write-back seeds its entry from the values in `vehicles.json`, so its capacity history
  continues rather than restarting. An entry that carries only one of the two keys is
  completed the same way, from the `vehicles.json` value the reports were reading for
  the other. To start from a known state instead, write the two keys of every vehicle,
  taken from the `vehicles.json` you run with, into the file — the reports are then
  exactly what they would be without the external ledger.
- The write-back is guarded by a `filelock.FileLock` on `<ledger>.lock` (in the
  default mode, `vehicles.json.lock`), and only ever adds/updates capacity fields from
  `charge`/`discharge` donor segments — fallback values are never written. Only a
  vehicle that is in `vehicles.json` ever gets an entry.
- **One generation per vehicle at a time.** Two concurrent runs of the *same* vehicle
  race on its ledger entry (last writer wins) and duplicate SRF fetches. Different
  vehicles in parallel are safe: separate ledger keys, shared read-only caches, one SRF
  client per `JOLTReportGenerator` instance.
- **The write target must be writable.** A write-back that cannot write — including the
  lock file beside it — raises, and that report is not written. A read-only config
  directory therefore needs the external ledger.
- `python -m report_generator.capacity_backfill --report-db <dir>` rebuilds the ledger
  from finished reports, into whichever of the two homes is active; with `--dry-run` it
  prints the result and writes neither `vehicles.json` nor the ledger (in the external
  mode it does not even create the ledger's lock file or directory).

## Caches

Point `JOLT_CACHE_DIR` at a persistent, writable directory. Nothing cached is secret;
hits are deterministic, so re-running the same `(vehicle, period)` re-uses the cache
instead of re-fetching. Size for the fleet history you intend to (re)generate.

| Sub-path | Content | Why persist it |
|----------|---------|----------------|
| `srf_http/` | SRF REST responses (`SeparateBodyFileCache`) | re-run cost |
| `srf_raw/` | one raw telematics CSV per FPS leg (keyed by `leg.uri`) | the bulk of the growth (GBs over a full fleet history) and the dominant re-fetch cost |
| `postcode_cache.json` | GPS → postcode lookups (0.001° precision) | small |
| `.weather_cache.json`, `weather/.weather_cache_fine.json` | OpenWeather results (~1 km × 1 h key) | **paid quota** — discarding it re-spends it |

## Output contract

```
<out_dir>/<REG>/jolt_report_<REG>_<start>_<end>.xlsx     # <start>/<end> = YYYYMMDD
```

Three sheets: **Report** (one row per segment), **Graphs** (fixed-axis scatter charts),
**Definitions** (column glossary).

`--debug` additionally persists, under `<out_dir>/<REG>/`, `raw_telematics/*.csv` (one
per FPS leg) plus the raw logger/charger CSVs — and nothing else: no figures, no HTML.
Production runs need none of it.

Empty numeric cells are the Excel `=NA()` formula with an **empty cached value**: Excel
recalculates them to `#N/A`, while non-recalculating readers (openpyxl
`data_only=True`, `pandas.read_excel`) see a blank → NaN. Read report cells through a
safe-number helper that tolerates both.

## Un-onboarded registrations

Any registration works — a `configs/vehicles.json` entry is not required. An
unconfigured registration is resolved against SRF (UK/NI registration-spacing variants
are tried), its fuel type and — for EV — its telematics column names are auto-detected,
and a generic pipeline runs.

- **Both fuel types always produce a report**: a structurally valid xlsx; only the
  segmentation quality is generic rather than tuned.
- **No side effects**: the runtime config is never written to `vehicles.json`, no
  capacity write-back, no paid API calls. Safe on a read-only config mount.
- No usable legs or channels still yields a structurally complete, header-only report
  plus log warnings — not a crash.
- **The one hard failure**: a registration that does not exist on SRF → one error line,
  exit code **3**, no traceback.

## Paid APIs

Default generation makes **zero** OpenWeather calls. Weather columns are filled during
generation only from the SRF Logger weather channel (EV via `LoggerPatcher`, diesel from
Logger Channel 7 in-pipeline); without Logger data they stay empty. The OpenWeather
back-fill is a separate, optional, quota-consuming post-step:

```bash
python -m report_generator.weather_patch <folder-or-xlsx>
# coarse (default): ~2 lookups per trip. --fine-grained: ~17k calls per vehicle.
```

Requires `OPENWEATHER_API_KEYS` (without it the patcher logs a warning and patches
nothing). Safe to re-run — cached.

## Not in this workspace

Report generation is all this workspace contains. Validation-figure rendering, the
inspect HTML viewer, data-availability dashboards, interactive segmentation fine-tuning
and C_rr/C_dA parameter identification are separate developer tooling, deliberately kept
out — do not wire them into the platform. They consume this workspace read-only through
its public API (e.g. `run_segment_detection(figure_hook=…)`), which is why the runtime
dependencies include neither matplotlib nor scikit-learn.

## Known quirks — do NOT "fix" these silently

- **Two header layouts.** EV uses `HEADERS` (52 columns), diesel `DIESEL_HEADERS` (26).
  Diesel is a distinct set — no SOC/battery/charging columns, carries `Fuel Used (L)` /
  `Fuel Consumption (L/100km)` — not a truncation of EV. `Operator` is the last column
  both share; EV appends the `EP Confidence` / `EP Confidence Reason` pair after it, and
  diesel is not graded. Do not unify them.
- **Blank, not `=NA()`, where there is nothing to grade.** The two EP-confidence cells of
  a charge row, a Stop row or a trip without an EP value are left truly empty: nothing is
  missing there, there is simply no grade. An EV report written before the pair existed
  stops at `Operator`; the coarse weather patcher accepts both widths.
- **Append-only column contract.** The patchers address **hardcoded 1-based column
  indices** (temperature = EV column 38). New columns go at the end, never inserted.
  Import-time assertions (`_COL_* == HEADERS.index(<name>) + 1`) and
  `tests/test_column_contracts.py` fail loudly on a reorder — that is the guard, keep it.
- **`=NA()` with an empty cache.** A cached `0` (xlsxwriter's default) would make every
  empty numeric cell read back as a real zero — e.g. a no-GVM leg looking mass-bearing.
  `capacity_backfill` also relies on Stop-row `=NA()` cells being droppable. Do not
  substitute literal blanks or zeros.
- **Coarse wind direction is an arithmetic mean** (so 359°/1° averages to 180°). The
  fine patcher does circular averaging; the coarse path is left alone because changing
  it would move already-published historical numbers.
- **SOC = 0 means "missing", not "flat battery".** Segmentation maps a telematics
  `SOC == 0` to NaN. Do not "correct" it.

## Exit codes

| rc | Meaning |
|----|---------|
| 0 | report generated |
| 1 | uncaught exception (traceback printed) |
| 2 | `SRF_API_KEY` unset, or a required argument missing — fails before any fetch |
| 3 | registration does not exist on SRF |
