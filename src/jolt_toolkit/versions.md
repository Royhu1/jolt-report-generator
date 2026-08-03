# jolt_toolkit — version history

> The version-history record for the `jolt_toolkit` workspace. It travels **inside**
> the vendored folder, so a copy of `src/jolt_toolkit` carries its own history. This
> is where the "added in vX" / "changed in vX" facts live — code comments state the
> *why* (units, ordering, contracts), this file states the *when*.
>
> **Append-forward discipline**: every release bumps `__version__` in `__init__.py`
> **and** appends a section here, in the same change (release-procedure step, see
> `.claude/rules/git-workflow.md`). Newest at the bottom. The current architecture is
> documented in [README.md](README.md); this file is history only.

## 1.0.0 — initial report generator

- The first Excel report generator: a per-OEM / per-make processor (the legacy
  `data_processor.py`). Reports written to `reports/1.0.0/`.
- The report schema had **no `Weather Type` column** (added later at 2.0.0).
- Predates the project changelogs (which begin 2026-03), so only retrospective
  references survive; a `v1.0.0` git tag exists.

## 2.0.0 — src-layout unification + unified segmentation

- Restructured `jolt_report_generator` into the `src/jolt_toolkit` **src-layout**
  package; the legacy per-make processor moved to `deprecated/`.
- Introduced the **unified speed-based segmentation** (`find_discharge_segments_by_speed`)
  with a mass-clustering post-process, alongside the SOC-based path.
- Added the `Weather Type` report column and the `max_torque_nm` vehicle parameter.
- Full-fleet batch regen validated; tagged `v2.0.0`.

## 2.2.1 — J1939 boolean-field fix

- Fixed the loss of SRF-Logger J1939 **boolean** channels: `_logger_to_numeric` now
  maps the string `"true"`/`"false"` to `1`/`0`. Without it the CCVS cruise-control /
  brake-switch / clutch-switch columns were all-NaN across every leg.

## 2.2.2 — Stop leg type + diesel pipeline

- New **`Stop` leg type** synthesised between trips and charges (exclusion-based:
  not-a-trip and not-a-charge → Stop), inserted by `_insert_stop_rows()` after
  capacity correction; Stop rows carry mass / cumulative distance / SOC endpoints and
  write NaN for the three EP columns.
- New **diesel pipeline** (`diesel_pipeline.py`, developed on WU70GLV / DAF XF 450):
  a Logger-only (`SRFLOGGER_V1`) path with its own **`DIESEL_HEADERS`** column set
  (a distinct set, not a truncation of the EV `HEADERS`) and the `Fuel Consumption
  (L/100km)` column.

## 2.2.3 — Propulsion Energy column + weather patching + finetune

- New EV column **`Propulsion Energy (kWh)`** (from `electric_energy_propulsion`,
  interpolated to the trip window and differenced; does not deduct regen, does not
  include aux) — appended, respecting the append-only column contract.
- **Weather patching** introduced: the coarse `WeatherPatcher` (origin/destination
  average, quota-friendly) plus an opt-in **fine-grained** patcher (in-trip
  multi-sample at 60 s, circular wind averaging).
- New **`finetune`** library (Merge/Split/Delete segmentation ops → `*_finetuned.xlsx`
  with a Finetune Log sheet) and the `report-finetuner` skill.
- The canonical `excel_report_database/<version>/` output path and the unified Graphs
  chart specs (`patch_graphs_2_2_3.py`) date from this line.

## 2.2.4 — EP_exclude_aux column + time-local capacity

- New EV column **`EP_exclude_aux (kWh/km)` = (propulsion − recuperation) / distance**
  (net-traction efficiency; needs both counters), appended after Propulsion Energy.
- **Effective-capacity model, time-local window**: a `soc_estimate` segment's capacity
  is now replaced from donor capacities in a short **time-local (~1 month) window**
  around its start, capturing battery ageing / seasonal temperature drift, instead of
  a whole-period aggregate.
- **Unstable stationary mass ignored**: unreliable stationary mass readings are
  excluded, falling back to stationary data only when no driving data exists.
- **Shared analysis layer founded**: under the sub-project-independence
  convention the `jolt_toolkit.analysis` shared layer was established — the
  battery-efficiency physics model (`eta_bat`) was promoted verbatim from the
  simulation sub-project into `analysis/physics.py` as its canonical home.

## 2.2.5 — per-leg Operator column + zero-speed anchoring

- New **`Operator`** column — resolved **per leg** from the SRF cascade (round-robin
  `trial.description`, else the vehicle organisation). It is the **last** column in
  **both** header sets (EV `HEADERS` = 50, diesel `DIESEL_HEADERS` = 26), so every
  hardcoded patcher column index (≤ 48 for EV) is unaffected.
- **Zero-speed trip-endpoint anchoring** (`trip_endpoint_anchor: "zero_speed"`):
  extend a trip's ends to the nearest `v == 0` within `max_extend_minutes`, for
  low-rate telematics. First drafted in the 2.2.3 window, landed canonically here.
- **Energy-anchor non-overlap fix** to the segmentation anchors.

## 2.2.6 — configurable robust mass + one-figure-per-day + weather rekey + quarterly ledger

- **Configurable robust mass aggregation** (`mass_agg`, per-vehicle override wins over
  the pipeline): a fence (Tukey IQR / median ± 3·MAD / trim) then an estimator
  (median / mean / time-weighted mean). EX74JXW settled on **`mad_tw_mean`** (MAD fence
  + time-weighted mean) to tame its bursty, short dense-lag GCVW clusters; `_agg_mass`
  gained an optional `timestamps` argument that only `mad_tw_mean` reads.
- **One-figure-per-day** validation figures (was per-leg), and the inspect-HTML
  hover-driven single-info-box redesign.
- **Weather-cache re-keying**: cache-key precision **6 → 2 decimals** plus an
  **hour-bucket** time key (`f"{lat:.2f},{lon:.2f},{(dt//3600)*3600}"`, ~1 km × 1 h),
  with a one-off rekey of the existing cache, plus **trip-only** weather → the EV fleet
  regenerates with **zero** OpenWeather API calls.
- **Quarterly capacity ledger**: `effective_capacity_kwh` became a donor-count-weighted
  average, backed by a new **`effective_capacity_quarterly`** per-period ledger
  (`{period: {kwh, n}}`) modelling degradation; `_persist_effective_capacity` changed
  from overwrite to **merge**, and `capacity_backfill` can rebuild the ledger from
  existing xlsx without re-running SRF.

## 2.2.7 — capacity-correction fix + ΔSOC-weighted donor mean + anchor-overlap postpass

- **Capacity-correction fix**: `_correct_effective_capacity` step 2 was overwriting good
  counter energy on short trips (a spurious low-EP band). Introduced the binary
  `energy_source` gate — a counter-sourced leg keeps its counter energy (**MODE A**).
- **ΔSOC-weighted (combined-ratio) donor aggregation** (`_soc_weighted_cap`):
  `C_eff = 100·Σ|ΔEᵢ| / Σ|ΔSOCᵢ|`, replacing a plain donor mean, removing the
  small-ΔSOC upward bias. Applied to the period capacity and the window / inlier means.
- **Anchor-overlap postpass** (`_enforce_anchor_ordering`): clamp energy anchors so
  `anchor_end(i) ≤ start(i+1)` on sparse-counter overlaps (was double-counting).

## 2.2.8 — SOC-fallback energy rewrite + charger fusion

- **Per-vehicle SOC-fallback energy rewrite** (opt-in `soc_energy_fallback: true`): in
  the step-2 ±1σ outlier pass, a counter-sourced outlier whose dual gate fires
  (`|ΔSOC| ≥ 10` **and** `|orig − repl| / repl ≥ 0.30`) has its energy re-derived from
  `ΔSOC/100 × replacement_cap` and is marked `Energy Source = 'soc_fallback'`; otherwise
  MODE A is kept. Enabled on 7 EVs (YK73WFN, EV73SAL, N88GNW, T88RNW, TA70WTL, CMZ6260,
  KY24LHT); AV24LXK / EX74JXW and diesel stay off. `soc_fallback` rows are excluded from
  donor pools.
- **Charger fusion fix**: the `ChargerPatcher` leg filter widened from `AC`/`DC` to the
  generic `Charge` prefix (Charge Home/Away now match); `_find_charger_matches` sums
  energy across **all** overlapping ±4-minute windows (handling dual-gun DC chargers);
  a shared `merge_save_charger_transactions` and an idempotent backfill CLI landed.

## 3.0.0 — behaviour-preserving architecture refactor

- Architecture-only refactor for the SRF-platform handover — **no** data-processing or
  pipeline change. `segment_algorithms.py` (~3,453 lines) → the **`segmentation/`**
  sub-package; `report_builder.py` (~2,688 lines) → `columns` / `charts` /
  `row_builder` / `excel_writer` (+ `html_viewer`, later re-homed at 3.1.0); the
  effective-capacity model extracted to **`capacity.py`**; `generate_report` decomposed
  into an orchestrator + private methods (pure block extractions). ~4,000 lines of dead
  code removed (`deprecated/`, `bootstrap.py`, old weather trio, `LegRecord`/`Link`).
- Core set fully **translated to English** (AST-proven string-constant-only changes),
  black/isort-normalised, public-surface type-annotated; patchers gained import-time
  `_COL_* == HEADERS.index(...)+1` assertions.
- **Facades** (`segment_algorithms.py`, `report_builder.py`) preserve every historical
  import path, locked by `tests/test_imports.py`. **Golden-verified 7/7 vehicles
  cell-identical vs 2.2.8**; tagged `v3.0.0`.

## 3.1.0 — platform slimming + general fallback pipeline

- **Platform slimming** (~12.7k lines left the package, every capability kept working
  from its new home, which consumes the package read-only):
  validation-figure/inspect-HTML rendering → the **report-visuals** skill; dashboards →
  the **generate-data-dashboard** skill; the finetune library → the **report-finetuner**
  skill; the C_rr/C_dA params sub-package → `research_projects/parameter_identify/`; the
  cached-recompute tool → the **generate-excel-report** skill. **matplotlib left the
  package dependencies** (importing the package no longer pulls it in), and the
  `[params]` (scikit-learn) extra was removed.
- **`figure_hook` seam**: `run_segment_detection()` gained a keyword-only
  `figure_hook: Callable | None = None`, invoked at the exact former inline-paint call
  site with the same arguments the old `plot_leg_validation` received. Default `None` →
  no painting; report-visuals passes its own painter (repainted PNGs byte-identical).
- **Facade name drops**: `segment_algorithms` / `segmentation` no longer export
  `plot_leg_validation` (or `_HAS_MPL`); `report_builder` no longer exports
  `_write_html_viewer` / `_compute_active_dates_from_xlsx` / `_group_paths_by_date` /
  `_clear_day_validation_figures`; `diesel_pipeline` lost its plotting half (kept the
  data-processing surface: `process_diesel_leg`, `_finalise_logger_df`,
  `_segments_from_df`, `_build_logger_df`, `_trip_metrics`, all `DEFAULT_*`).
- **General fallback pipeline** (`general_pipeline.py`): any registration — EV or diesel —
  always produces a structurally valid xlsx via a **runtime** config assembled from SRF
  metadata + column auto-detection + default params (never written to `vehicles.json`,
  no capacity write-back, zero paid-weather-API calls). The one hard failure is a
  registration that does not exist on SRF at all → `VehicleNotFoundError`, CLI exit
  code 3, no traceback.
- **`--debug` / `--raw-only`** now persist raw CSVs only — no figures, no inspect HTML.
- Onboarded-vehicle output **golden-identical** to 3.0.0 / 2.2.8; tagged `v3.1.0`.

## 3.2.0 — workspace form

- The toolkit becomes a **vendored code workspace**, not an installable package. The
  `pyproject.toml` `[build-system]` / `[project]` (metadata, dependencies,
  optional-dependencies, scripts) and `[tool.setuptools.*]` tables were removed, leaving
  only `[tool.black]` / `[tool.isort]` / `[tool.pytest.ini_options]` (with
  `pythonpath = ["src"]`, which keeps `pytest` working uninstalled). `pip install .`
  now intentionally fails — put `src/` on the import path instead.
- The `jolt-report` **console script is gone**; the documented entry points are the
  module CLI (`python -m jolt_toolkit.report_generator.cli`) and the library import.
- **Runtime dependencies** moved to `src/jolt_toolkit/requirements.txt` (so they travel
  with the vendored folder); the root `requirements.txt` includes it via `-r` and adds
  the repo-level extras (matplotlib, scikit-learn, the test/lint toolchain), each dep
  declared exactly once.
- `__version__` is now a **plain constant** in `__init__.py` (read straight from source;
  the `importlib.metadata` + pyproject-walking fallback was dropped).
- Version-history narration was stripped from code comments and condensed into this
  file; the README's v3.0.0/v3.1.0 migration-notes sections were replaced by a pointer
  here. Behaviour is unchanged (verified: full test suite + fast EV / short diesel
  smokes 0-diff vs the standing goldens).
