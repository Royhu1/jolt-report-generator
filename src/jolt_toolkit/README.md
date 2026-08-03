# jolt_toolkit — workspace architecture documentation

> Developer-facing architecture reference for the `jolt_toolkit` workspace — a
> **vendored code workspace**, not an installable package: no wheel, no
> `pip install`, no console script; runtime deps in
> [`requirements.txt`](requirements.txt), version constant in `__init__.py`.
> Project overview & repo-wide usage → [root README.md](../../README.md) |
> deployment contract → [DEPLOYMENT.md](DEPLOYMENT.md) | version history →
> [versions.md](versions.md).

The workspace generates a formatted Excel report for a vehicle over a date range from
SRF telematics/logger/charger data: user supplies `REG + start/end` → `.xlsx`. That
report-generation surface (plus the optional weather back-fill post-step and the
shared `analysis/` helpers) is **all** it contains — validation-figure rendering,
inspect HTML, dashboards, fine-tuning and C_rr/C_dA parameter identification are
deliberately outside it and consume it read-only.

## Setup and usage

### Setup

The workspace is vendored, not installed — put `src/` on the import path and install
its runtime deps:

```bash
pip install -r src/jolt_toolkit/requirements.txt   # the workspace's own runtime deps
export PYTHONPATH=/path/to/repo/src                # or a .pth file in site-packages
# (for the repo dev checkout: `pip install -r requirements.txt` adds the test/lint
#  toolchain, and the jolt conda env carries a .pth pointing at <repo>/src.)
```

Provide credentials in a `.env` in the working directory (or export them):

```
SRF_API_KEY=your_api_key_here
OPENWEATHER_API_KEYS=key1,key2     # optional, only for the weather post-step
```

### Generate a report

```bash
python -m jolt_toolkit.report_generator.cli -veh KY24LHT -ds 2025-01-01 -de 2025-01-31 [--debug] [--fast] [--raw-only] [--out-dir DIR]
```

The report lands at `<out-dir>/<REG>/jolt_report_<REG>_<start>_<end>.xlsx`
(default `<out-dir>` = `./excel_report_database/<version>`).

| Flag | Meaning |
|------|---------|
| `-veh` / `--vehicle_registration` | Registration; a `configs/vehicles.json` entry is optional (see the general fallback pipeline) |
| `-ds` / `--date_start`, `-de` / `--date_end` | `YYYY-MM-DD`; `date_end` is **inclusive** |
| `--debug` | Also persist raw artefacts: `raw_telematics/` CSVs + raw logger/charger CSVs. No figures / inspect HTML — those are rendered outside this workspace |
| `--raw-only` | Alias of `--debug` (both persist raw artefacts only) |
| `--fast` | Skip SRF Logger + Charger fetch; FPS telematics only (fast iteration) |
| `--out-dir` / `--report-output-folder` | Output folder override |

The `cli.main()` fails fast with a clear message + exit code **2** if `SRF_API_KEY`
is unset or a required argument is missing (instead of building a client with a null
key and failing obscurely on the first request).

### Public API

```python
from jolt_toolkit.report_generator import JOLTReportGenerator, generate_report, patch_logger
gen = JOLTReportGenerator(report_output_folder="./excel_report_database",
                          debug_mode=True, fast_mode=False)  # save_figures is a no-op
gen.generate_report("AV24LXK", "2024-06-01", "2024-09-01")   # returns the xlsx path or None

# shared analysis helpers (used by sub-projects; sub-project independence)
from jolt_toolkit.analysis import build_interp, delta, to_utc, ols, ols_hc1, vif, fit_block, eta_bat
```

## Package structure

```
src/jolt_toolkit/
├── __init__.py                    # __version__ (plain constant, read from source)
├── configs/                       # shared config (accessed via get_config_path(); JOLT_CONFIG_DIR override)
│   ├── __init__.py                # get_config_path() — honours env JOLT_CONFIG_DIR, else the workspace configs/ dir
│   ├── vehicles.json  pipelines.json  plot_config.json
├── report_generator/              # the report-generation pipeline (REG + dates → xlsx)
│   ├── _generator.py              # JOLTReportGenerator — fetch → segment → correct → write orchestration
│   ├── general_pipeline.py        # general fallback for un-onboarded regs: SRF-registration spacing resolution + runtime VEHICLE_CONFIG assembly (build_runtime_vehicle_config)
│   ├── capacity.py                # effective-capacity model: _correct_effective_capacity / _persist_effective_capacity + donor helpers
│   ├── capacity_backfill.py       # rebuild the capacity ledger from existing xlsx (no SRF)
│   ├── data_fetcher.py            # fetch_events() — SRF legs + charging events → ServerData
│   ├── data_class.py              # ServerData dataclass
│   ├── operators.py               # derive_leg_operator() — per-leg operator code (SRF cascade)
│   ├── diesel_pipeline.py         # process_diesel_leg() — SRFLOGGER_V1 Logger-only path (fuel_type=="DIESEL")
│   ├── pedal_histogram.py         # accelerator/brake pedal position histograms
│   ├── paths.py                   # get_cache_dir() / get_srf_api_root() — env-overridable roots
│   ├── cli.py                     # module CLI entry point (python -m …report_generator.cli; argparse main())
│   ├── xlsx_patch_common.py       # shared patcher scaffolding: make_srf_client + filename/cell/timestamp helpers
│   ├── charger_patcher.py         # ChargerPatcher — backfill Charger Link + energy (EV)
│   ├── logger_patcher.py          # LoggerPatcher — backfill Logger Link + weather/mass (EV)
│   ├── weather_patcher.py         # WeatherPatcher — coarse origin/dest OpenWeather (default)
│   ├── weather_patch.py           # patch_weather() + CLI — coarse (default) / fine (opt-in) dispatch
│   ├── segmentation/              # unified charge/discharge segmentation sub-package (EV path)
│   │   ├── constants.py           # column-name constants + THE single VEHICLE_CONFIG / PIPELINE_CONFIGS load
│   │   ├── timeutil.py            # _to_utc
│   │   ├── mass_aggregation.py    # _agg_mass + the eight mass-aggregation methods, resolve_mass_agg
│   │   ├── soc_detection.py       # find_charge_segments_by_soc / find_discharge_segments_by_soc
│   │   ├── speed_detection.py     # find_speed_trips / find_discharge_segments_by_speed
│   │   ├── mass_clustering.py     # cluster_mass_data, split/merge/anchor functions
│   │   └── detection.py           # run_segment_detection (the unified entry point; figure_hook seam)
│   ├── segment_algorithms.py      # FACADE re-exporting every name above (flat import path)
│   ├── columns.py                 # HEADERS / DIESEL_HEADERS, leg-type predicates, _row_col_index, _is_nan
│   ├── charts.py                  # CHART_STYLE, CHART_SPECS_EV/DIESEL, chart_specs_for
│   ├── row_builder.py             # _seg_to_row + metric helpers, URL builders, postcode cache, Stop synthesis
│   ├── excel_writer.py            # _write_na, _write_excel_report (report/graphs/definitions sheets)
│   ├── report_builder.py          # FACADE re-exporting the four modules above (flat import path)
│   └── weather_fetcher/
│       ├── openweather.py         # shared KeyManager / WeatherCache / WeatherFetcher (coarse + fine consume it)
│       └── fine_grained_patcher.py# FineGrainedWeatherPatcher — in-trip multi-sample (opt-in)
└── analysis/                      # versioned shared analysis helpers (counters / stats / physics)
```

### Scope

Every module above is part of the deployed report path (`REG + dates → xlsx`); they
are English-commented, style-normalised (black/isort) and have public-surface type
annotations. Validation-figure and inspect-HTML rendering, the data-availability
dashboards, interactive segmentation fine-tuning and C_rr/C_dA parameter
identification are **not** part of this workspace: they live outside it and consume
it read-only through its public API (e.g.
`run_segment_detection(figure_hook=...)`, and for diesel by re-driving
`diesel_pipeline._segments_from_df`), which is why matplotlib is not a dependency
here. The `analysis/` helpers do live here — shared machinery for the repo's
sub-projects.

### `analysis/` — shared analysis machinery

Stable helpers promoted verbatim from `data_analysis_workspace` sub-projects so each
sub-project depends only on the versioned toolkit (never on another sub-project):

| Module | Public API |
|--------|-----------|
| `analysis/counters.py` | `build_interp`, `delta`, `to_utc`, `COL_TOTAL`, `COL_PROP`, `COL_RECUP`, `MIN_DIST_KM` |
| `analysis/stats.py` | `ols`, `ols_hc1`, `vif`, `fit_block`, `demean_within`, `MASS_SPREAD_MIN_KG` |
| `analysis/physics.py` | `eta_bat` (Arrhenius battery-efficiency model) |

## Report generation pipeline overview

`JOLTReportGenerator.generate_report(reg, date_start, date_end)` is an orchestrator
over private methods:

```
generate_report(reg, date_start, date_end)
  ├─ [reg not in VEHICLE_CONFIG]            → build_runtime_vehicle_config() (general fallback; injects a runtime cfg)
  ├─ fetch_events()                         → ServerData (SRF legs + charging events)
  ├─ _collect_charger_windows(server_data)  → charge (start,end,uri,energy) windows + raw objects
  ├─ _collect_legs(...)                     → split SRFLOGGER (logger) vs FPS legs
  ├─ _preload_logger_channels(...)          → speed / mass / pedal channel frames (debug/EV)
  ├─ _preload_charger_meter(...)            → charger meter frame (debug figures)
  ├─ if diesel:  _process_diesel_legs(...)  → rows via process_diesel_leg() (DIESEL_HEADERS)
  ├─ _process_fps_legs(...)                 → per FPS leg: run_segment_detection() → _seg_to_row() (HEADERS)
  ├─ _reclassify_home_charging(...)         → relabel Away→Home charges within 0.5 km of the home point
  ├─ _finalize_rows(...)                    → _correct_effective_capacity() (EV) + non-discharge EP scrub
  │                                            + per-period capacity + _insert_stop_rows()
  └─ _write_outputs(...)                    → _persist_effective_capacity() + _write_excel_report()
                                               + [EV,non-fast] ChargerPatcher → LoggerPatcher
                                               + [debug] raw artefacts only (no figures / inspect HTML)
```

Diesel vehicles (`fuel_type=="DIESEL"`) skip the FPS loop, capacity correction and the
patchers; EV is the default branch.

### General fallback pipeline (un-onboarded registrations)

A registration that is **not** in `vehicles.json` still generates a report.
Before the pipeline runs, `generate_report` builds a **runtime** vehicle config via
`general_pipeline.build_runtime_vehicle_config(reg, reg_input, ds, de, srf_data=…)` and
injects it into the in-memory `VEHICLE_CONFIG` (by reference — never written to
`vehicles.json`). Steps:

1. **SRF resolution** — `resolve_srf_vehicle()` tries `reg_spacing_variants()` (verbatim
   no-space form → UK `LLNN LLL` spacing → NI alpha/digit split → generic short-form
   split) via `srf_data.vehicles.get(obj_id=…)`. The SRF-stored `registration` becomes
   `srf_reg` (used verbatim for the leg / transaction filters). If **no** spelling
   resolves → `VehicleNotFoundError` (the one legitimate failure: no data exists on SRF;
   the CLI reports it as a single-line error, exit code 3, no traceback).
2. **Fuel-type routing** — SRF `vehicle.fuel` `ELECTRIC` → EV, `DIESEL` → diesel; unknown
   → probe the fetched legs (an FPS leg's raw telematics carrying the SOC column → EV,
   else SRFLOGGER_V1 legs present → diesel, else EV with degradation).
3. **Runtime config** — EV: `detect_ev_columns()` auto-detects the energy / speed / mass /
   altitude column names against the fleet candidate sets from the first fetched leg's raw
   telematics; a missing column degrades exactly as it already does for a configured
   vehicle lacking it (no mass split/merge without a mass column; `soc_estimate` energy
   without counters; no elevation correction without altitude). Pipeline = `default_soc`
   when the SOC column is present, else `default_speed`. Capacity = the SRF vehicle's
   `fuel_capacity` (→ `srf_capacity_kwh`) if exposed, else `None` (`soc_estimate` energies
   stay NaN rather than crashing — see the `_correct_effective_capacity` all-fallback
   guards). Diesel: the standard SRFLOGGER_V1 channel set + `diesel_pipeline`'s `DEFAULT_*`
   constants + SRF `weight_class` (tonnes) as `weight_class_t`.

Guarantees: **both** fuel types always produce a structurally valid xlsx; the
`effective_capacity` write-back is **skipped** for a runtime config (the computed capacity
is logged, never persisted — no invented `vehicles.json` entries); zero paid OpenWeather
calls (weather columns only ever come from the SRF Logger channel, as for onboarded
vehicles); and zero usable segments still writes a structurally complete header-only
report rather than failing. The runtime config carries an internal `_runtime_fallback`
marker (`is_runtime_config()`) so a repeat generation in the same process is still treated
as a fallback.

### Module responsibilities

| Module | Responsibility |
|--------|----------------|
| `_generator.py` | orchestrates fetch → segment → correct → write; EV / `is_diesel` branch switch; un-onboarded regs → general fallback |
| `data_fetcher.py` | `fetch_events()` — SRF legs + charging events; `date_end` inclusive |
| `general_pipeline.py` | general fallback for un-onboarded regs: SRF-registration spacing resolution, EV column auto-detection, runtime-config assembly (`build_runtime_vehicle_config()`), `VehicleNotFoundError` |
| `segmentation/` | unified charge/discharge segmentation (SOC + speed detection, mass cluster/merge/split, energy-source cascade); `run_segment_detection()` is the entry point (paints figures only via an external `figure_hook`) |
| `segment_algorithms.py` | facade re-exporting the whole `segmentation/` surface (public + internally-used privates) on the flat import path |
| `capacity.py` | effective-capacity post-processing `_correct_effective_capacity()`, ledger persistence `_persist_effective_capacity()`, donor helpers; also re-exposed as `JOLTReportGenerator` staticmethods |
| `diesel_pipeline.py` | `process_diesel_leg()` — SRFLOGGER_V1 channels → diesel rows |
| `columns.py` | `HEADERS`/`DIESEL_HEADERS`, leg-type predicates, `_row_col_index`, `_is_nan` |
| `row_builder.py` | `_seg_to_row()` + metric helpers, URL builders, postcode geocode cache, `_stop_row_from_neighbours` / `_insert_stop_rows` |
| `charts.py` | `CHART_SPECS_EV`/`CHART_SPECS_DIESEL` + `CHART_STYLE` (fixed-axis chart specs) |
| `excel_writer.py` | `_write_na()` (=NA() contract), `_write_excel_report()` (report/graphs/definitions sheets) |
| `report_builder.py` | facade re-exporting `columns`/`charts`/`row_builder`/`excel_writer` on the flat import path |
| `operators.py` | `derive_leg_operator()` — per-leg `Operator` code from the SRF cascade |
| `pedal_histogram.py` | EEC2 accelerator / EBC1 brake pedal histograms (discharge, distance > 10 km) |
| `charger_patcher.py` / `logger_patcher.py` | EV post-write backfill of Charger Link / Logger Link + weather + mass |
| `weather_patcher.py` / `weather_patch.py` / `weather_fetcher/` | coarse (default) + fine (opt-in) OpenWeather patching |
| `xlsx_patch_common.py` | `make_srf_client()` (shared `SeparateBodyFileCache` client) + filename/cell/timestamp helpers |
| `paths.py` | `get_cache_dir()` (env `JOLT_CACHE_DIR`) / `get_srf_api_root()` (env `SRF_API_ROOT`) |

## Environment variables

All defaults resolve relative to the repository root, so nothing needs setting for a
repo-root run:

| Variable | Purpose | Default |
|----------|---------|---------|
| `SRF_API_KEY` | SRF platform API key (required to fetch) | — (CLI fails fast, rc 2) |
| `OPENWEATHER_API_KEYS` | comma-separated OpenWeather keys (weather post-step only) | — (weather skipped) |
| `JOLT_CONFIG_DIR` | directory holding the three config JSONs; **also where the capacity ledger is written back** | the workspace's own `configs/` dir |
| `JOLT_CACHE_DIR` | cache root (`srf_http/`, `srf_raw/`, weather, postcode) | `./cache` |
| `SRF_API_ROOT` | SRF REST API root | `https://data.csrf.ac.uk/api/` |
| `WEATHER_CACHE_FILE` / `WEATHER_CACHE_FILE_FINE` | override the coarse / fine weather cache file paths | `<cache>/.weather_cache.json` / `<cache>/weather/.weather_cache_fine.json` |

A config file missing at `get_config_path()` **fails loudly** (`FileNotFoundError`
naming the workspace `configs/` + the `JOLT_CONFIG_DIR` remedy) rather than degrading
to an empty config.

## Configuration files

`VEHICLE_CONFIG` / `PIPELINE_CONFIGS` are loaded **once** in `segmentation/constants.py`
and shared by reference across the package (a single load site; object identity is a
maintained invariant — see the import test).

### `configs/vehicles.json`

Each vehicle entry:

| Field | Type | Description |
|-------|------|-------------|
| `srf_reg` | `str` | **required** — registration in the SRF API (e.g. `"KY24 LHT"`) |
| `nominal_kwh` | `float` | manufacturer nominal battery capacity (kWh); sets the effective-capacity validity range (`nominal × 0.5 … 2.0`) |
| `srf_capacity_kwh` | `float` | SRF-registered capacity (API `fuel_capacity`); ultimate fallback for effective capacity + SOC estimate |
| `effective_capacity_kwh` | `float\|null` | donor-count-weighted average over all reliable periods of `effective_capacity_quarterly` (`Σ(kwh·n)/Σn`, reliable = `n ≥ MIN_DONORS`); maintained by `_persist_effective_capacity()` / `capacity_backfill` |
| `effective_capacity_quarterly` | `dict\|absent` (EV) | per-period ledger `{"YYYYMMDD_YYYYMMDD": {"kwh", "n"}}`; `n` = donor count. Sparse periods (`n < MIN_DONORS`=5) are excluded from the average and their `kwh` back-filled to it |
| `soc_energy_fallback` | `bool` (optional, EV) | opt-in: in the ±1σ step-2 outlier pass, re-derive a counter-sourced outlier's energy from ΔSOC×capacity when the dual-gate fires (see capacity model). Off by default |
| `make` / `model` | `str` | manufacturer / model |
| `pipeline` | `str` | key into `pipelines.json` (EV); diesel uses the `daf_diesel_logger` dispatch marker (not a pipelines.json key) |
| `mass_agg` | `str` (optional) | per-vehicle mass-aggregation override; takes precedence over the pipeline value |
| `speed_col` | `str` | speed column (`"wheel_based_speed"` / `"speed"`) |
| `ac_col` / `dc_col` / `total_energy_col` / `moving_energy_col` | `str` | telematics energy counter columns (some may be absent, e.g. Mercedes SOC-only) |
| `mass_col` | `str` | vehicle-mass column |
| `altitude_col` | `str` | altitude column for elevation-corrected EP |
| `min_cluster_gap_kg` | `float` | minimum mass-clustering gap for `merge_discharge_by_mass()` |
| `split_long_stops_min` | `float` (optional) | refuse to merge same-mass trips separated by a stop ≥ this many minutes |

**Diesel-only fields** (`fuel_type=="DIESEL"`): `weight_class_t` (**required**),
`leg_source` (`"SRFLOGGER_V1"`), `fuel_energy_col`, `fuel_rate_col`, `distance_col`,
`diesel_lhv_kwh_per_l` (default 10.0), `speed_col_fallback`, `ambient_temp_col` —
example under §Diesel pipeline.

#### SRF telematics energy counters

All energy columns are **cumulative Wh counters**; the adjacent-row difference is the
interval energy. Empirically (YK73WFN):
`total_electric_energy_used = electric_energy_propulsion + auxiliary − electric_energy_recuperation_watthours`.

| Counter | Meaning | Regen deducted |
|---------|---------|----------------|
| `electric_energy_propulsion` | motor drive energy | no (forward drive only) |
| `electric_energy_recuperation_watthours` | regen recovered energy | — |
| `total_electric_energy_used(_plugged_in_included)` | net battery consumption | **yes** |
| `electric_energy_wheelbased_speed_over_zero` | net consumption while moving | **yes** |

`Energy Change (kWh)` / `Energy Performance (kWh/km)` use `total_energy_col`, i.e. the
**net-of-regen** value. `Propulsion Energy (kWh)` comes from `electric_energy_propulsion`
(interpolated to the trip window, differenced; does **not** deduct regen, does **not**
include aux). `EP_exclude_aux = (propulsion − recuperation) / distance` (net traction
efficiency; needs both counters non-empty). Charge/Stop rows write NaN for these.

The trailing EV columns are `… Propulsion Energy (kWh)` (48), `EP_exclude_aux` (49),
`Operator` (50); diesel's trailing columns are `… Energy Source` and `Operator` (26).
`Operator` is last in **both** sets, so every hardcoded patcher column index (≤ 48 for
EV) is unaffected — this is the append-only column contract.

#### Three-tier battery-capacity model

| Capacity | Source | Use | Field |
|----------|--------|-----|-------|
| Nominal | datasheet | range validation (`nominal × 0.5…2.0`) | `nominal_kwh` |
| SRF platform | SRF API `fuel_capacity` | ultimate fallback | `srf_capacity_kwh` |
| Effective | telematics analysis | SOC-estimate seed + the value used in-report | `effective_capacity_kwh` (weighted avg) + `effective_capacity_quarterly` (ledger) |

**`_correct_effective_capacity()` (in `capacity.py`)** replaces each `soc_estimate`
segment's capacity from the donor capacities in a **±`CAP_WINDOW_HALF_DAYS` (15-day)
time-local window** around its start (charge donors preferred over discharge; the window
widens by doubling until donors appear, else falls back to `srf_capacity`). Within a
donor set the estimator is the **ΔSOC-weighted / combined-ratio mean**
`C_eff = 100·Σ|ΔEᵢ| / Σ|ΔSOCᵢ|` (`_soc_weighted_cap()`), which removes the small-ΔSOC
upward bias of a plain mean. A step-2 ±1σ pass rejects outliers; by default a rejected
segment keeps its counter energy (MODE A), but a vehicle with `soc_energy_fallback:true`
re-derives that outlier's energy from `ΔSOC/100 × replacement_cap` and marks
`Energy Source = 'soc_fallback'` when the dual gate fires (`|ΔSOC| ≥ 10` **and**
`|orig−repl|/repl ≥ 0.30`).

**Persistence (ledger, `_persist_effective_capacity()`)**: after generation the period's
donor capacity `(kwh, n)` — from `_period_capacity_from_rows()` on the corrected rows,
**before** Stop insertion — is merged into `effective_capacity_quarterly[period_key]`,
then `effective_capacity_kwh` is recomputed as the donor-count-weighted average over
reliable periods (`_recompute_weighted_capacity()`). Written only when the source is a
`charge`/`discharge` donor (never a fallback), guarded by a `filelock.FileLock` so
parallel runs cannot clobber. `capacity_backfill` reproduces the identical ledger from
existing xlsx without re-running (it reads the `Battery Capacity`/`SOC Change`/`Energy
Source` columns; the `=NA()` Stop cells read back as 0 and are dropped by the donor
guard).

### `configs/pipelines.json`

| Group | Parameter | Description |
|-------|-----------|-------------|
| top level | `merge_by_mass` | bool, default `true`; `false` skips `merge_discharge_by_mass` (mass signal locked / same-bucket load). Per-vehicle override in `vehicles.json` wins |
| top level | `trip_endpoint_anchor` | `"first_motion"` (default) or `"zero_speed"` (extend trip ends to the nearest v==0 within `max_extend_minutes`, for low-rate telematics) |
| top level | `max_extend_minutes` | float, default 5.0; the zero_speed extension cap |
| top level | `mass_agg` | per-segment mass-aggregation method, default `"mean"`; one of `mean` / `median` / `iqr_median` / `mad_median` / `iqr_mean` / `mad_mean` / `mad_tw_mean` / `trimmed_mean`. Each = a fence (Tukey IQR / median±3·MAD / 20 % trim) then an estimator (median / mean / time-weighted mean). The value feeds the Excel `Vehicle Mass (kg)` column and is re-used by the external figure / fine-tuning tooling. Vehicle-level override wins |
| `charge_params` | `plateau_window_min` / `min_soc_rise` / `min_energy_kwh` | charge merge window + SOC-rise + energy thresholds |
| `discharge_params` | `plateau_window_min` / `soc_rise_abort_pct` / `min_soc_drop` / `min_energy_kwh` | discharge merge window + SOC-recovery abort + drop/energy thresholds |
| `speed_params` | `speed_threshold_kmh` / `min_stop_duration_min` / `min_trip_duration_min` / `min_soc_drop` / `min_energy_kwh` | speed-branch trip boundaries + lenient SOC/energy checks |

> Which fence suits a given vehicle depends on its GCVW channel (a bursty or
> over-reading channel wants a robust fence). The schema and loader live here; the
> per-vehicle `mass_agg` **value** is a tuning parameter, owned by the repository's
> parameter-tuning workflow.

## Segmentation algorithms

`run_segment_detection(df_raw, reg, …)` (in `segmentation/detection.py`) is the unified
entry point; parameters come from `PIPELINE_CONFIGS[pipeline]`:

```
run_segment_detection
  ├─ branch=="soc":   find_charge_segments_by_soc + find_discharge_segments_by_soc
  ├─ branch=="speed": find_charge_segments_by_soc + find_discharge_segments_by_speed
  │                    (→ find_speed_trips; falls back to SOC if the speed column is missing/all-zero)
  ├─ cluster_mass_data → mass_cluster column
  ├─ split_discharge_by_mass  (split where the cluster label changes)
  ├─ merge_discharge_by_mass  (merge adjacent same-cluster; skipped when merge_by_mass=false)
  └─ _enforce_anchor_ordering (post-pass: clamp energy anchors so anchor_end(i) ≤ start(i+1))
```

- **Charge (`find_charge_segments_by_soc`)**: detect rising-SOC blocks, merge blocks
  ≤ `plateau_window_min` apart with no drop, validate `ΔSOC ≥ min_soc_rise` /
  `Δenergy ≥ min_energy_kwh` / capacity in `[cap_lo, cap_hi]`. Energy: AC+DC diff
  (`energy_source='ac_dc'`) else `soc_estimate`. Type: AC & DC ≥ 0.5 kWh = `Mix`, else
  `AC`/`DC`/`estimated`.
- **Discharge — SOC branch (`find_discharge_segments_by_soc`)**: dropping-SOC blocks,
  merged unless the in-gap SOC recovery ≥ `soc_rise_abort_pct`. Energy-source cascade:
  `total_energy` → `moving_energy` → `soc_estimate`.
- **Discharge — speed branch (`find_discharge_segments_by_speed`)**: trip boundaries from
  `find_speed_trips()` (drive blocks with v > `speed_threshold_kmh`, bridge stops
  < `min_stop_duration_min`, drop trips < `min_trip_duration_min`); SOC/energy used only
  for metrics. Both branches emit an identical segment schema.

Mass: `cluster_mass_data` filters to valid (>0), moving-only (`speed > MOVING_SPEED_THRESHOLD_KMH`)
samples, then `_agg_mass` applies the configured method; the same value feeds the Excel
column and the externally-rendered validation figure.

## Excel output

**Report worksheet** — one segment per row, columns = `HEADERS` (EV, 50) / `DIESEL_HEADERS`
(diesel, 26). Green = discharge trip, red = charge, white = Stop. Timestamps
`yyyy-mm-dd hh:mm:ss`; durations `[hh]:mm:ss` (fractional days); SRF links are clickable
hyperlinks. Empty numeric cells are the `=NA()` formula written with an **empty cached
value** (`_write_na`): Excel recalculates them to `#N/A`, while non-recalculating readers
(openpyxl `data_only=True`, `pandas.read_excel`) see a blank → NaN. Downstream readers
must guard with a safe-number helper that tolerates both.

**Graphs worksheet** — fixed-axis scatter + linear-fit charts from `CHART_SPECS_EV` /
`CHART_SPECS_DIESEL` (selected by `chart_specs_for(headers)`) + one `CHART_STYLE`, so every
report looks identical. Three panels: the performance metric vs Mass (0–45000 kg) / Average
Temperature (−5…30 °C) / Average Speed (0–90 km/h) — y = Energy Performance (0–3 kWh/km)
for EV, Fuel Consumption (0–60 L/100km) for diesel.

**Definitions worksheet** — a column glossary.

**Leg types**: `In Transit` / charge (`AC`/`DC`/`Mix`/`estimated`) / `Stop`. Stop rows are
synthesised by `_stop_row_from_neighbours` for gaps > 60 s between trip/charge (carrying
mass / cumulative distance / SOC endpoints from the previous segment; the three EP columns
are NaN), inserted **after** capacity correction.

## SRF Logger data channels

Fetched via `leg.get_data_frame(type, resolution='1s')`. Column names are stable:

| Type | Columns | Notes |
|------|---------|-------|
| `2` | `2 longitude/latitude/altitude/bearing/speed` | GPS; `2 speed` in m/s |
| `6` | `6 charge/temperature/capacity/charging` | SOC %, battery temp °C |
| `7` | `7 temperature/pressure/humidity/wind speed/wind direction/cloud cover` | OpenWeather snapshot |
| `AMB` | `AMB ambient air temperature` | J1939 ambient temp °C |
| `CCVS` | `CCVS wheel based vehicle speed`, `… cruise control set speed/active`, `… brake/clutch switch` | J1939 speed km/h + boolean flags |
| `CVW` | `CVW gross combination vehicle weight` | GCVW total mass (diesel + SRFLOGGER_V2 EV); only broadcast while moving |
| `VDHR` | `VDHR hr total vehicle distance` | cumulative distance km (CAN legs only) |
| `LFC` / `LFE` | `LFC engine total fuel used` / `LFE fuel rate` | diesel fuel counter / rate |
| `EEC2` / `EBC1` | accelerator / brake pedal position | pedal histograms |

**SRFLOGGER_V2** (first seen on LN25 NKE, a DAF XD **electric**) exposes a larger J1939 set
but the consumed column names are **identical to V1**, so a V2 EV is config-only to onboard.
Both `SRFLOGGER_V1`/`V2` match `startswith("SRFLOGGER")`. Boolean J1939 fields (`"true"`/
`"false"`) are mapped to 1/0 by `_logger_to_numeric` (else the CCVS cruise/brake/clutch
columns would be all-NaN).

## Diesel pipeline

`process_diesel_leg()` gives a Logger-only path for `fuel_type=="DIESEL"` vehicles:

| Step | EV | Diesel |
|------|----|--------|
| main-loop leg source | `FPS` | `SRFLOGGER_V1` |
| speed | `wheel_based_speed`/`speed` | `CCVS wheel based vehicle speed` (+ `2 speed`×3.6 GPS fallback) |
| energy | AC/DC → total → SOC estimate | `LFC engine total fuel used` × LHV |
| distance | GNSS odometer | `VDHR hr total vehicle distance` diff |
| mass | telematics GCVW | Logger `CVW …` (three-level fallback: CVW trip median → prev-trip carry → `weight_class_t`×1000) |
| temperature | Logger Ch 7 / OpenWeather | `AMB ambient air temperature` |
| segmentation | speed + SOC check | `find_speed_trips()` only |
| charge events / capacity / patchers | detected / corrected / run | empty / skipped / skipped |

Weather for diesel is aggregated at trip granularity directly by the pipeline from Logger
Channel 7 (not `LoggerPatcher`, which serves EV only). `_trip_metrics` conventions: LFC
fuel delta must be strictly > 0 to record (a moving-trip delta of 0 = counter didn't tick →
`fuel_l` NaN, not 0); CVW `0 kg` (stationary broadcast) filtered before aggregating; only
`mass_source=='cvw_trip'` may feed the carry-over slot. Trips are dropped if
`distance_km < min_trip_distance_km` (1.0) or if `fuel_l`/`veh_mass`/`temp_avg` are all NaN.
Diesel validation figures (Speed / cumulative fuel / cumulative distance / GCVW) are
painted outside this workspace by an external diesel painter that re-drives
`_segments_from_df` here — `process_diesel_leg()` itself draws nothing, in any mode.

Example diesel entry:

```jsonc
"WU70GLV": {
  "srf_reg": "WU70 GLV", "make": "DAF", "model": "XF 450",
  "fuel_type": "DIESEL", "pipeline": "daf_diesel_logger",
  "leg_source": "SRFLOGGER_V1", "weight_class_t": 44.0, "diesel_lhv_kwh_per_l": 10.0,
  "speed_col": "CCVS wheel based vehicle speed", "speed_col_fallback": "2 speed",
  "fuel_energy_col": "LFC engine total fuel used", "fuel_rate_col": "LFE fuel rate",
  "distance_col": "VDHR hr total vehicle distance",
  "mass_col": "CVW gross combination vehicle weight",
  "altitude_col": "2 altitude", "ambient_temp_col": "AMB ambient air temperature"
}
```

## Weather patching

`patch_weather(target, *, mode="coarse", …)` (+ a `python -m …weather_patch` CLI) is the
single dispatch point. **Coarse** (`WeatherPatcher`, default) averages each trip's origin +
destination — quota-friendly, ~2 lookups/trip. **Fine** (`FineGrainedWeatherPatcher`,
`--fine-grained`, opt-in) multi-samples in-trip GPS at 60 s with circular wind averaging
(~17k calls/vehicle → OpenWeather 429 at fleet scale, so not the default). Both patch
**driving rows only** (`is_trip_leg`), share `weather_fetcher/openweather.py`
(`KeyManager`/`WeatherCache`/`WeatherFetcher`), and quantise the cache key to
`f"{lat:.2f},{lon:.2f},{(dt//3600)*3600}"` (~1 km × 1 h). Coarse writes
`<cache>/.weather_cache.json` 6-tuples; fine writes `<cache>/weather/.weather_cache_fine.json`.
`WeatherPatcher` refuses a diesel-layout workbook (its hardcoded EV indices would corrupt it).

> The coarse patcher averages wind direction **arithmetically** (so 359°/1° averages to
> 180°), where the fine patcher uses sin/cos averaging. The coarse behaviour is retained
> deliberately: changing it would move already-published historical numbers.

## SRF API caching

| Tier | Location | Content | Hit condition |
|------|----------|---------|---------------|
| HTTP | `<cache>/srf_http/` | SRF REST responses | URL + headers (`SeparateBodyFileCache`) |
| Raw data | `<cache>/srf_raw/` | FPS leg raw telematics CSV | `leg.uri` hash |
| Postcode | `<cache>/postcode_cache.json` | GPS → postcode | coordinate precision 0.001° |

`<cache>` = `JOLT_CACHE_DIR` (default `./cache`). The HTTP client is built once by
`xlsx_patch_common.make_srf_client()` and shared across `_generator` + the patchers. Caches
are safe to persist between runs and hit deterministically.

## Debug mode (raw artefacts only — rendering lives outside the workspace)

With `--debug` (or `--raw-only`) the generator persists **raw artefacts only**:
`raw_telematics/*.csv` per FPS leg plus raw logger/charger CSVs. It draws **no**
validation figures and writes **no** inspect HTML; a log line points at the external
renderer, which paints the canonical one-figure-per-day overlay PNGs (+
`<stem>.boxes.json` sidecars) and (re)writes the `inspect_*.html` viewer from those
persisted raw artefacts. It plugs its EV painter into
`run_segment_detection(figure_hook=...)` (the `figure_hook` seam; contract in
`segmentation/detection.py`'s docstring) and re-drives
`diesel_pipeline._segments_from_df` for diesel.

## Version history

Per-release change history lives in [`versions.md`](versions.md) — the single place
where it is recorded. Every other document here describes the current state only.
