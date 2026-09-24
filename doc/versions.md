# report_generator — version history

> The version-history record for the report generator. This is where the "added in
> vX" / "changed in vX" facts live — code comments state the *why* (units, ordering,
> contracts), this file states the *when*.
>
> **Append-forward discipline**: every release bumps `__version__` in
> `report_generator/version.py` **and** appends a section here, in the same change.
> Newest at the bottom. The current architecture is documented in
> [architecture.md](architecture.md); this file is history only.
>
> Two notes on reading it. The entries up to and including 3.5.1 are the upstream
> record kept **verbatim**, so a section may name a path (`src/jolt_toolkit/…`, a data
> tree, a sibling tool) that belongs to the JOLT research project this repository was
> extracted from — the statement was true of the release it describes. From 3.6.0 the
> code is developed in this repository and its sections are written here. And
> `__version__` numbers code revisions, not this repository's layout: moving the
> package to the root is recorded in git, not as a release here.

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

## 3.2.1 — defect fixes found by the test suite; docs describe the present only

- **Data namespace: unchanged, still `3.2.0/`.** This release cannot alter a reported
  cell, verified by cell-by-cell comparison against the previous release over the EV
  (YK73WFN, fast) and diesel (WU70GLV, full) smoke sets: **0 differing cells** in both.
  The documentation and comment changes are additionally AST-proven to have left every
  syntax tree identical after masking string constants. See the exception in
  `.claude/rules/git-workflow.md` "Line 1".
- Four defects, all surfaced by a new behavioural test suite:
  - `excel_writer` treated a NaN in a hyperlink column as a present link (NaN is truthy),
    crashing in `xlsxwriter.write_url` instead of writing `=NA()`. Unreachable from the
    shipped row builders, which write `None`, but a trap for any new row source.
  - A merged discharge segment could carry a tz-aware `start_time` beside a tz-naive
    `end_time` (and likewise for the anchor pair, which `_recompute_anchors` leaves alone
    when anchors already exist). Both pairs are normalised to tz-aware UTC; the instants
    are unchanged, only the dtype.
  - `_logger_df_from_csv` never renamed the Channel-2 GPS columns to `_lat`/`_lon`, so a
    diesel report regenerated from cached CSVs silently lost the origin/destination
    coordinates the live path produced. Both paths now share the `_GPS_LAT`/`_GPS_LON`
    constants; over WU70GLV 2025-09-01..04, 40/40 legs went from NULL to matching the
    live path to six decimal places.
  - `compute_pedal_histogram` raised `TypeError` on a length-less argument instead of
    returning `None` as documented.
- Documentation now describes only current behaviour: the per-release migration notes and
  the inline "added in / since vX.Y" annotations are gone from `README.md` and
  `DEPLOYMENT.md` (this file remains the sole home of version history), and `DEPLOYMENT.md`
  was rewritten as a terse integration contract. Corrected while doing so: an `=NA()` cell
  shows `#N/A` only when Excel recalculates — non-recalculating readers (openpyxl
  `data_only`, pandas) see an empty cell → NaN; the old "may leak a 0" caveat described
  behaviour predating `_write_na`.
- Comments, docstrings and one log line no longer point at anything outside this
  workspace. Three of the corrected statements were wrong rather than merely stale: the
  `_generator` module docstring claimed it produced validation figures, `detection.py`
  claimed `out_dir` is where figures are written (the package writes none — `out_dir` only
  composes the path handed to `figure_hook`), and `__init__.py` referenced an analysis
  script that no longer exists. `--raw-only` is documented as the exact alias of `--debug`
  that it is.
- Known and deliberately not fixed: post-split discharge segments bypass the
  `cap_lo`/`cap_hi` plausibility guard (36 of 42,860 counter-sourced legs, 0.08 %, no
  effect on fleet statistics). Registered with its evidence as pending issue 007.

## 3.3.0 — elapsed trip-average speed and battery-side elevation correction

- **Trip-average speed** for every EV pipeline is now odometer distance divided by the
  full elapsed segment duration, matching the diesel definition. The previous denominator
  was a moving-duration estimate accumulated from sparse telematics samples, which
  systematically overstated speed on legs where the sampling gaps swallowed stopped time.
  Merged as `fix/elapsed-trip-average-speed` (commit `c18f1b4`). One residual family of
  implausible values above 90 km/h survives this fix — stale odometer anchors across
  telemetry gaps overstate the numerator, not the denominator — registered with its
  evidence as pending issue 008.
- **Elevation-corrected and kinetics-corrected EP** now deduct the *battery-side* energy
  of the net elevation change instead of the raw potential energy. The new
  `report_generator/energy_correction.py` exposes `battery_elevation_energy_kwh()`:
  uphill demand is `m·g·Δh / η`, downhill recovery is `η·m·g·Δh` (negative), at a
  symmetric `η = 0.90` — the same efficiency the kinetics correction already assumed for
  the drivetrain and regenerative braking. Losses are therefore one-directional: the same
  hill costs the battery more to climb than it repays on the descent. The helper is
  applied consistently at every site that previously inlined `m·g·Δh / 3 600 000`:
  `row_builder._corrected_energy_perf`, `row_builder._kinetics_corrected_energy_perf` and
  the EP-rewrite path of `capacity._correct_effective_capacity`, so the generated report
  and the capacity-correction pass can no longer disagree. `Δh = 0` and NaN elevation
  behave exactly as before. A new Definitions-sheet line documents the formula, and
  `tests/test_energy_correction.py` pins the efficiency semantics.
- **Data namespace: new directory `excel_report_database/3.3.0/`**, populated from
  `3.2.0/` with the SRF-free cached-recompute tool
  (`.claude/skills/generate-excel-report/tools/recompute_from_cache.py`). Releases 3.2.0
  and 3.2.1 share the `3.2.0/` directory, 3.2.1 being behaviour-preserving under the
  exception in `.claude/rules/git-workflow.md` "Line 1"; this release changes reported
  numbers, so it opens its own. The migration covered 17/17 vehicles: 14 EVs replayed
  from cached raw telematics, and WU70GLV, YT21EFD (both diesel) plus YN25RSY (its
  `prefer_logger_speed` pipeline needs Logger channels the cache does not hold) copied
  forward verbatim with **0 differing cells**.
- **YN25RSY needed no in-place patch after all.** The verbatim copy already satisfies both
  of this release's changes: its published speeds were already distance ÷ elapsed time
  (the logger-speed pipeline never used the moving-duration denominator), and its
  corrected-EP columns are entirely `=NA()` for want of altitude and mass inputs, so the
  elevation formula cannot move them. Verification did surface a **pre-existing**
  inconsistency in this vehicle, inherited through the verbatim copy-forwards since 2.2.8:
  its GraphsData holds 12 `(speed, EP)` pairs with no corresponding Report row. Registered
  as pending issue 009; not introduced by this release and deliberately not fixed here.
- **Verification — cell-by-cell against `3.2.0/`**, over every matched report file. All
  differences fall inside the expected scope:
  - `Average Speed` — Report column 14 (N), 31,368 cells; its GraphsData mirror (column E),
    31,088 cells.
  - `Energy Performance Corrected by Elevation Difference` — column 31, 26,661 cells;
    `Energy Performance Kinetics Corrected` — column 47, 338 cells (the kinetics column is
    only populated where Logger 1 Hz speed exists).
  - The new Definitions-sheet line, which shifts 5 rows per file.
  - GraphsData's EP-pair column (F) shows 2,031 positional shifts. A multiset check proves
    these are **insertions only** — no EP value was lost or altered: 43 driving legs whose
    former above-90 km/h speeds now fall back inside the chart's 0–90 km/h x-filter and so
    re-enter the series.
  - `Energy Output from Charger (kWh)` (column 33): the replay writes `=NA()` wherever a
    historical backfill was never persisted into the `raw_charger` CSVs, and the
    `charger_patcher` CLI cannot restore them because its idempotence gate is a non-empty
    `Charger Link` cell. 165 such cells were restored by overlaying the published `3.2.0/`
    values keyed by row `Start Time`; charger transactions are immutable, so this is
    equivalent to a fresh SRF backfill. Tool: `tmp/_patch_charger_energy_330.py`.
  - Weather columns 38–43: 18 cells across CMZ6260 / LN25NKE / YN75NMA moved from `=NA()`
    to truly blank, because the start-time overlay skips NA sources. Both encode the same
    missing-ness and `compare_reports.py` treats them as equal.
- **Monitor slice consolidation.** The weekly slices `*_20260601_20260706` and
  `*_20260707_20260818` were merged into `*_20260601_20260818` quarter files for eight
  vehicles (AV24LXJ / AV24LXK / AV24LXL, EV73SAL, N88GNW, T88RNW, TA70WTL, YK73WFN). The
  merged files inherited their weather and link columns through the per-vehicle start-time
  overlay and were included in the charger-column restoration above.

## 3.4.0 — machine-readable data namespace and UTC-normalisation fixes

- **Data namespace: unchanged, still `3.3.0/`** — the release is behaviour-preserving, and
  the cell-by-cell comparison against the 3.3.0 goldens confirms it. All nine YK73WFN
  report files (2024-06 → 2026-08) were regenerated with the 3.4.0 code from the `3.3.0/`
  raw artefacts into a scratch namespace and compared against the goldens: **no numeric
  value produced by the 3.4.0 code differs**, and four of the nine files are
  byte-identical. Only two classes of cell are flagged, neither of them a change in
  computed output:
  - 48 `Energy Output from Charger` cells that the cached replay leaves `=NA()` because
    the underlying transactions are absent from the `raw_charger` CSVs. The per-file counts
    (2 / 4 / 4 / 2 / 36) are exactly the set the 3.3.0 migration restored by start-time
    overlay, so this is a standing property of the replay tool, not an effect of this
    release; the canonical `3.3.0/` values are untouched.
  - 6 weather cells differing only in the `=NA()` ↔ truly-blank encoding of the same
    missing-ness, which `compare_reports.py` treats as equal.

  The scratch namespace was deleted after the comparison. The **diesel** generation path is
  untouched by this release — the `to_utc` conversions are instant-preserving and the
  diesel pipeline carries its own timezone handling — and PR #1's own live smoke had
  already shown 0 differing cells for WU70GLV on its base. Full suite: **281 passed, 2
  skipped**; the skill-registry check is in sync.
- **The active report-data tree is now a machine-readable constant**,
  `jolt_toolkit.DATA_NAMESPACE`, instead of being inferred from `__version__`
  (ADR-005). Its initial value is `3.3.0`, the populated canonical tree. Previously every
  version-defaulted tool built its output path from `__version__`, so a release that
  provably changed no cell — and therefore correctly reused the previous data directory
  under the exception in `.claude/rules/git-workflow.md` "Line 1" — left every default
  consumer one run away from creating and then filling an empty
  `excel_report_database/<toolkit-version>/`. That is exactly the silent drift that
  produced the v3.2.0 divergence, where `__version__` had advanced three releases past the
  newest populated tree. The two facts are now stated separately and can differ on
  purpose.
- **Every default consumer resolves through the constant**: the `JOLTReportGenerator`
  class default, the `report_generator.generate_report()` convenience wrapper, the module
  CLI, the fleet data-collection monitor, the dashboard runner, the PDF briefing
  generator, and the inspect-HTML refresh — plus three consumers that had also hard-coded
  a version-derived literal: `data_analysis_workspace/shared/batch_weather_patch.py`,
  `data_analysis_workspace/shared/generate_figures.py` and `chatbot/build_kb.py`.
- **A new shared resolver, `report_generator.paths.default_report_root()`**, replaces five
  hand-built `./excel_report_database/<…>` literals. It reads the namespace *at call time*,
  so an override or a test monkeypatch is honoured; the generator's
  `report_output_folder` parameter therefore defaults to `None` and resolves inside
  `__init__` rather than freezing the path at import. The module CLI now logs the code
  revision and the data namespace as two unconditional lines — the previous
  log-only-when-they-differ behaviour would have erased the provenance from the log on the
  first release that advanced both together.
- **`_to_utc()` now converts an aware non-UTC timestamp to UTC** instead of returning it
  unchanged, so a non-UTC offset can no longer leak into a comparison against a
  UTC-indexed series. All **three** independent copies of the helper are fixed:
  `segmentation/timeutil._to_utc` (the copy on the segmentation hot path),
  `analysis/counters.to_utc` (counter-endpoint interpolation) and the inner `_ts` of
  `report_generator/operators.py` (time-resolved operator windows). Naive timestamps are
  still localised as UTC, and already-UTC timestamps and their instants are unchanged, so
  no report cell moves. Regression coverage pins merged segment endpoints and their
  private energy-anchor timestamps.
- **Fleet monitor**: the status-output directory is created independently of the PDF step,
  so `--dry-run --no-pdf` works in a fresh repository; the startup log reports the data
  namespace and the toolkit version separately; and the conditional `jolt_toolkit` import
  is restored, so an explicit `--version` keeps the read-only path runnable when the
  package is not on the import path.
- **The no-capacity fallback path no longer raises while formatting its INFO log** when
  the global effective capacity is `None` — the missing value is logged as `nan`, and the
  calculation result is unchanged.
- **New regression tests**: NaN Excel hyperlink cells, cached-diesel GPS column
  normalisation, pedal-histogram invalid input, the `_to_utc` conversions, and the
  namespace defaults themselves (including a check that this file's newest section names
  the active namespace, so the two can no longer drift apart unnoticed).

## 3.5.0 — per-row EP confidence grade

- **Data namespace: unchanged, still `3.3.0/`.** The release adds two columns and changes
  no computed value. Proven by replaying the identical cached raw artefacts of `3.3.0/`
  through the pristine 3.4.0 package and through this one, and comparing the 49
  pre-existing row fields cell by cell: **0 differing cells across 1,116,700 cells /
  21,475 rows over 9 of the 14 replayable EVs** — N88GNW, AV24LXJ, EV73SAL, TA70WTL,
  KY24LHT, CMZ6260, EX74JXW, LN25NKE, YN75NMA. The set is chosen to span both
  segmentation branches (`soc` via YN75NMA, `speed` for the rest), mass-merge on and off,
  and all three energy-source families (counter, `soc_estimate`, `soc_fallback`). Note the
  one consequence of reusing the directory: the two new columns appear
  only on reports **generated from this release onward**, so `3.3.0/` will hold a mix of
  50-column and 52-column files until someone backfills it with
  `python .claude/skills/generate-excel-report/tools/recompute_from_cache.py --src-ver
  3.3.0 --dst-ver <new>` (SRF-free, and the grading pass is wired into that tool for
  exactly this purpose). Consumers that read by column **name** are unaffected either way,
  and the coarse weather patcher accepts both widths (see the hardening bullet below).
- **New EV columns `EP Confidence` and `EP Confidence Reason`** (51 and 52), appended so
  every hard-coded patcher column index (≤ 48) is untouched. Each trip row is graded
  `good` / `caution` / `poor` for its own `Energy Performance (kWh/km)`, with the
  triggered check codes and their measured values beside it. Charge rows, Stop rows and
  trips with no usable distance are left blank — they state no EP, so there is nothing to
  grade. Diesel keeps its own header set and is not graded: its energy comes from the LFC
  fuel counter, whose failure modes (counter resets, coarse quantisation) are different
  ones and are not modelled here.
- **Why the column exists.** EP is a ratio of two independently-anchored quantities, and
  when the telematics counters are sampled sparsely relative to the trips it can be badly
  wrong *while still looking like a plausible number*. Four mechanisms are now measured
  rather than left to the reader:
  1. **Energy double counting on merge** — adjacent discharge segments that fall inside the
     same sparse counter interval each take the whole interval, and
     `merge_discharge_by_mass` sums the parts. The fleet replay finds **127 rows whose
     energy is 1.3× to 2.5× what the counter measured over the same anchor span, most of
     them at a ratio of exactly 2.000000** — N88GNW 57, EV73SAL 31, YK73WFN 17, AV24LXJ 9,
     AV24LXK 7, AV24LXL 4, CMZ6260 2 — with implied capacities of 545–1067 kWh against
     nominals of 360–540. `_enforce_anchor_ordering` repairs the *pairwise* overlap between
     two surviving neighbours; it cannot see an overlap consumed inside a merge.
     **All 127 grade `poor`.**
  2. **Attribution window wider than the trip** — driving or standing time inside the
     counter's anchor window but outside the trip window
     (`pending_issues/008_stale_odometer_anchor_gap.md` is the odometer form of this).
  3. **Insufficient SOC resolution** — LN25NKE's SOC channel steps in 0.4 % and its median
     trip spans about six steps, with 5 % of trips spanning three.
  4. **SOC discontinuity** — separately from resolution, LN25NKE's worst rows come from a
     12-percentage-point drop between two samples 25 s apart during a yard shunt, turning
     0.3 km of manoeuvring into 55 kWh and an EP of 176 kWh/km. The pack did not
     discharge; the signal re-initialised.
  Mechanisms 1 and 2 are pipeline defects and remain fixable; 3 and 4 are properties of the
  source signal and are not. Either way the report no longer presents a number of unknown
  quality as if it were measured.
- **Twelve checks in four groups**, each naming a mechanism and reporting its measured
  value: `DUP_ENERGY` / `SPLIT_ALLOC` / `CAP_INCONS` (energy provenance), `ENERGY_WINDOW` /
  `IDLE_WINDOW` / `DIST_WINDOW` / `DIST_EXTRAP` (attribution window), `SOC_RES` /
  `SOC_STEP` (SOC signal), `SHORT_DIST` / `SPEED` / `EP_RANGE` (scale and plausibility).
  Two properties are load-bearing. The grade is the **worst** finding, not an average — one
  decisive defect is not offset by other checks passing. And it measures **resolution and
  internal consistency, not provenance**: `Energy Source` already records provenance, so a
  well-resolved SOC-derived energy grades `good` rather than being penalised twice.
- **Thresholds calibrated on the fleet, not guessed.** Every threshold was set against a
  replay of all 40,070 discharge segments in `3.3.0/` across the 14 replayable EVs
  (YN25RSY is excluded: it segments on SRF-Logger speed, which a cached replay does not
  reproduce). The resulting mix is **84.4 % good, 10.3 % caution, 5.2 % poor**, and the
  grade separates sharply on the quantity it is about: median EP 1.29 kWh/km for `good`
  (99th percentile 2.28, maximum 3.81) against 2.02 for `poor` (99th percentile 27.4).
  Of the rows above 8 kWh/km, **100 % grade `poor`**; above 5 kWh/km, 98 %; above
  4 kWh/km, 93 % with none left `good`. The residual is 28 rows — 0.08 % of all `good`
  rows — sitting between 3 and 3.81 kWh/km on 3.8–4.4 km winter trips with no detectable
  defect, which is the honest answer for a genuinely short cold-weather leg rather than a
  miss. The per-vehicle mix is also plausible: LN25NKE is the most-flagged (24.9 % poor,
  all SOC-resolution and SOC-step) and T88RNW the most cautioned (22.7 %, almost entirely
  short urban legs), while no vehicle is majority-flagged.
- **Where the code lives.** New module `report_generator/ep_confidence.py` holds the whole
  concern: `attach_ep_audits()` measures the diagnostics inside the segmentation layer
  (called by `run_segment_detection()` after `_enforce_anchor_ordering`, because every
  quantity is read off the counter anchors, which are stripped from the segment dict before
  the row builder sees it) and travels on the segment's public `ep_audit` key;
  `assess_ep_confidence()` is the **single** rule engine; `regrade_rows()` is the
  authoritative final pass in `_finalize_rows`, run after the capacity correction has
  settled the final energy source and once the period's effective capacity is available as
  the counter-versus-SOC referee. `_seg_to_row` writes a provisional grade so any direct
  caller of it still gets one. The measurement is strictly read-only: a regression test
  pins that it never alters a segment.
- **The SOC quantum is measured, not configured**: the greatest common divisor of a leg's
  SOC changes recovers the channel's grid exactly (0.4 % for LN25NKE, 1.0 % for the other
  thirteen) regardless of how densely that leg happens to be sampled, which a modal or
  percentile estimate cannot do. A stray off-grid reading can only drag the estimate down,
  making the check more lenient, never falsely harsh.
- **The cached-recompute migration tool grades too** — `regrade_rows()` is wired into
  `recompute_from_cache.py` at the same point in the sequence, so a recomputed report and a
  freshly-generated one agree.
- **Three defects found in pre-release review and fixed on the branch.**
  1. *The window measurements were dead on pandas 3.* `attach_ep_audits` took its sample
     trace as `df[time_col].astype("int64")` — an int64 view in the *Series'* unit — and
     compared it against segment boundaries from `Timestamp.value`, which are always
     nanoseconds. Since pandas 3, `pd.to_datetime(<ISO strings>, utc=True)` infers
     `datetime64[us]`, so the two sides differed by 1000× with nothing raised:
     `energy_outside_km` read 0.0, `dist_outside_km` NaN, both sample counts 0, the SOC
     step NaN, and `ENERGY_WINDOW` / `DIST_WINDOW` / `DIST_EXTRAP` / `SOC_STEP` — four of
     the twelve checks, including the one that names issue 008 — never fired. All datetime
     conversions in the module now go through one `_ns_view` helper (`as_unit("ns")`,
     matching what `row_builder` already does for the same reason), correct on pandas 2
     and 3 alike. **Open item before release:** if the calibration replay quoted above ran
     on pandas 3, those four checks were dead while it ran, so the 84.4 / 10.3 / 5.2 mix
     and the per-vehicle figures are a lower bound on how much is flagged and must be
     re-measured; the thresholds of the other eight checks are unaffected either way.
  2. *The weather backfill would have patched nothing.* `weather_patcher._is_ev_layout`
     compared the header row against the whole of `HEADERS`, which taking it to 52 turned
     into a rejection of every 50-column report already on disk — reported as
     "diesel/unknown layout", which reads like a correct refusal. It now matches the EV
     header prefix preceding the two new columns (derived from the column's index, not
     written as 50), so both widths of the mixed tree are patched and diesel is still
     refused.
  3. *A grading failure could abort a report.* All four call sites —
     `attach_ep_audits`, the row builder's `assess_ep_confidence`, the per-row call inside
     `regrade_rows` and `regrade_rows` itself — are now wrapped so an exception costs the
     two confidence cells and nothing else, logged as a warning. The grade is a commentary
     on the numbers, not one of them, and `_to_ns` builds a `pd.Timestamp` from raw
     telematics unguarded. A failed audit also has its partial results discarded, so
     half-measured diagnostics cannot drive a grade.
- **New regression suites** — `tests/test_ep_confidence.py` (52 tests): the grading
  contract (a grade exists exactly where an EP value exists; worst-finding wins; a missing
  audit never invents a downgrade), every check at its own threshold, the SOC-quantum
  estimator, the measurement reproducing each fingerprint on synthetic raw frames —
  including the exact 2× double count — the segment-to-row hand-off, and a positive
  control pinning the nanosecond unit on an explicitly `datetime64[us]` frame.
  `tests/test_ep_confidence_resilience.py` (6) injects a failure at each of the four call
  sites; `tests/test_weather_patcher_layout.py` (6) drives the backfill over 50-column,
  52-column and diesel workbooks. Full suite: **346 passed, 2 skipped**.

## 3.5.1 — Co-op operator curation + the configured mass column is honoured

Data namespace: **unchanged, still `3.3.0/`**. Neither change can alter a cell of
any vehicle the tree already held; the proofs are given per change below. The one
vehicle whose numbers do move is MK15BEV, onboarded the same day this release was
written and whose first reports the mass fix exists to correct — correcting a
vehicle's first report is part of onboarding it, not a migration of the established
fleet tree. No directory is created and `DATA_NAMESPACE` stays on `3.3.0`.

- **`COOP` is now a curated operator code.** The Co-operative Group appears in SRF
  on both routes of the operator cascade, so it is curated on both: the
  round-robin token (`trial.description = "JOLT Round Robin: Coop-<OEM>"`, seen on
  YN75NMA since 2026-06 and on the newly-onboarded MK15BEV) maps through
  `_TRIAL_OP_TO_CODE`, and the dedicated comparator's static
  `organisation.name = "Co-op"` maps through `_SRF_ORG_TO_CODE`. The hyphenated
  spelling is accepted on both routes — `_normalize_op_token` turns `Co-op` into
  `CO_OP`, a second code for one company, which the curation rule forbids.
- **What actually changed is the warning, not the cell.** Before this release the
  token was uncurated, so it fell through to `_normalize_op_token("Coop")`, which
  returns the string `"COOP"` — exactly the code now curated — and flagged the leg
  unknown. `is_unknown` is only ever accumulated into `op_acc` for the one-off
  "Operator codes not in KNOWN_OPERATOR_CODES" WARN at the end of a run; it is
  never written to a row. Curation therefore silences the WARN and leaves the
  resolved string identical.
- **Proof, and its limit.** The strict cell-by-cell regeneration was **not** run:
  operator derivation reads `leg.trip.trial.description` live from SRF, and the
  SRF-free replay tool (`recompute_from_cache.py`) does not derive the column at
  all — it copies `Operator` verbatim from the source workbook — so a replay would
  reproduce identical cells whatever this code did, which proves nothing. The
  evidence is instead direct: the only vehicle in `3.3.0/` carrying the token is
  YN75NMA, whose `20260707_20260920` report already reads `COOP` in all 83 rows,
  and the pre-change helpers were measured returning `"COOP"` for both
  `"JOLT Round Robin: Coop-Daimler"` and `"JOLT Round Robin: Coop-Scania"`. The
  stored cells and the new curated value are the same string. The `_SRF_ORG_TO_CODE`
  additions reach no existing vehicle either: a sweep of the newest report of every
  vehicle in `3.3.0/` found no blank `Operator` cell anywhere, so no vehicle is
  currently failing to resolve, and the only two carrying `COOP` — YN75NMA and
  MK15BEV — already resolve through the trial route, which is consulted first.
- **`_get_vehicle_mass` reads the configured `mass_col` instead of the module
  literal.** The segmentation layer has always resolved `cfg["mass_col"]`, falling
  back to the Logger CVW when that column is absent or carries nothing usable. The
  row builder did not: it read `columns._WEIGHT_COL` directly, so a vehicle
  configured *away* from the default name still had its `Vehicle Mass (kg)` cell
  filled from the column its configuration had deliberately rejected. The column
  now threads `cfg.get("mass_col", _WEIGHT_COL)` from `_generate_report` through
  `_process_fps_legs` and `_seg_to_row` into `_get_vehicle_mass`, whose parameter
  keeps the literal as its default.
- **Found on MK15BEV, where the two layers disagreed by a factor of three.** That
  vehicle's FPS `gross_combination_vehicle_weight` is the tractor weight, not the
  combination weight — a moving median of 7.7 t against a Logger CVW of 25-29 t on
  the same days — so its configuration points `mass_col` at a name the feed does
  not carry, which is the supported way to reject a mislabelled signal. The
  segmentation layer duly logged its fallback to the Logger CVW while all 86
  driving rows of the first report were written with the 4-12 t telematics values.
  With the fix the cell is left empty, and `LoggerPatcher` fills it from the Logger
  — the companion behaviour it was already written for. The elevation- and
  kinetics-corrected EP columns are empty on such a vehicle until the mass is
  patched, the same data gap the fleet's other GCVW-less vehicles already show.
- **Proof that no existing cell moves.** Every EV represented in `3.3.0/` sets
  `mass_col` to `"gross_combination_vehicle_weight"` — the literal the function
  used to read — so the resolved column is unchanged for all of them. The diesel
  vehicles configure the Logger's `CVW …` name but never reach `_seg_to_row`, which
  is the EV path only. The general fallback pipeline auto-detects the same literal
  for an un-onboarded vehicle, and where the feed does not carry it the key is
  simply absent, so the parameter default reproduces the previous behaviour exactly.
  MK15BEV is the sole vehicle whose configuration differs, and its two reports —
  both written the same day, before this fix — are the ones the fix corrects: they
  must be regenerated under 3.5.1, after which their `Vehicle Mass (kg)` comes from
  the Logger via `LoggerPatcher` instead of from the tractor-weight signal.
- **Left alone deliberately: the speed column of the same function.** `_seg_to_row`
  already accepts a `speed_col` argument, filled from `cfg["speed_col"]` at both
  call sites — and never uses it, so `_get_vehicle_mass` keeps filtering on the
  literal `wheel_based_speed`. LN25NKE and YN25RSY configure `"speed"` and their
  raw telematics carries no `wheel_based_speed` column at all, so for those two the
  moving-sample filter is silently inactive and the mass is aggregated over all
  positive samples, stationary broadcasts included. Fixing that would change their
  `Vehicle Mass (kg)` / `Vehicle Mass CV` cells and everything derived from them,
  which makes it a behaviour-changing release with its own data namespace, not a
  patch. Recorded here so the next mass-related release decides it deliberately.
- **New regression suites** — `tests/test_operators.py` (17 tests) pins both
  operator routes for Co-op, the spelled-out `Co-op` variant, the curated-set
  membership, and the contract that an *uncurated* operator still reaches the cell
  while flagging unknown. `tests/test_row_builder_mass_column.py` (4) drives
  `_get_vehicle_mass` over a frame whose mass sits only under a non-default
  configured name, over the MK15BEV shape where only the rejected default carries
  data (expecting NaN), over the default-name path (pinning it identical to the
  explicit call), and over the stationary-sample filter under a renamed column.
  Full suite: **371 passed, 3 skipped** (3.5.0 baseline on this worktree: 350
  passed, 3 skipped).

## 3.6.0 — external capacity-ledger file (`JOLT_CAPACITY_LEDGER`)

- **Data namespace: unchanged, still `3.3.0/`.** No reported cell can change, for two
  independent reasons.
  1. *Without the variable nothing changes.* `VEHICLE_CONFIG` is exactly the parsed
     `vehicles.json`, and the write-back and the backfill write `vehicles.json` byte for
     byte as before. Proven by driving the same sequence — a new period, a sparse period,
     the same period again, a no-donor call, a registration not in the file, a backfill
     dry run and a real backfill — through the 3.5.1 code and through this release on a
     copy of the shipped `vehicles.json`: the file and the in-memory entries have
     identical SHA-256 digests after every step.
  2. *With the variable set, a ledger extracted from the same `vehicles.json` gives the
     same numbers.* The overlaid configuration equals the file's own values, and the same
     sequence leaves the ledger holding exactly the entries the unset run wrote into
     `vehicles.json`, while `vehicles.json` stays byte-identical. The only report input
     the ledger supplies — `effective_capacity_kwh`, the capacity seed — is therefore
     the same number either way.
- **Why.** The capacity ledger (`effective_capacity_kwh` + `effective_capacity_quarterly`)
  is machine-written state, rewritten after every EV report, while everything else in
  `vehicles.json` is reviewed, tuned parameters. Keeping both in one file meant every
  report run dirtied the checkout of the code it ran from, and a parameter review could
  not tell the two apart. The variable moves the state into a file of its own.
- **New public loaders in `report_generator.configs`** — the way to read the configs,
  never by file path: `get_capacity_ledger_path() -> Path | None` (the variable, read at
  call time; unset, empty or blank means none), `load_vehicle_configs() -> dict` (a fresh
  read of `vehicles.json` with the ledger overlaid), `load_pipeline_configs() -> dict` and
  `apply_capacity_ledger(vehicles, *, skip=None) -> Path | None` (makes the ledger keys
  of configs already loaded exactly what `load_vehicle_configs()` gives at that moment,
  in place; a strict no-op without the variable),
  plus the constants `CAPACITY_LEDGER_ENV_VAR` and `LEDGER_KEYS`.
  `segmentation.constants` builds `VEHICLE_CONFIG` / `PIPELINE_CONFIGS` through them —
  still the single load site, still shared by reference; `constants._load_json` is kept
  and now delegates.
- **Ledger file.** `{REG: {"effective_capacity_kwh": float, "effective_capacity_quarterly":
  {period_key: {"kwh": float, "n": int}}}}`; only those two keys are read. For a
  registration in both files each ledger key the entry carries replaces the config value
  (a key it does not carry keeps it), a registration only in the ledger is ignored, and a
  file that does not exist yet means no overlay. A blank file reads as empty; any other
  content that is not such an object raises `ValueError`, so a damaged ledger is never
  read as empty and then overwritten. Reads take the ledger lock, falling back to an
  unlocked read only where the lock file cannot be created at all.
- **Write-back** (`capacity._persist_effective_capacity`): with the variable set it writes
  the ledger under `<ledger>.lock`, creating the file and its directory on the first
  write, and only reads `vehicles.json` — for the unchanged membership rule that only a
  vehicle in `vehicles.json` ever gets an entry. The period is merged into exactly what
  the loader shows the reports: each ledger key the vehicle's entry lacks — both, when
  it has no entry yet — is seeded from its `vehicles.json` entry, read fresh, so
  switching a deployment over continues each vehicle's capacity history instead of
  restarting it, and an entry that holds only `effective_capacity_kwh` keeps the
  quarterly history the overlay was showing instead of collapsing the average onto the
  new period. The seed is never the in-memory `VEHICLE_CONFIG`: after the variable is
  pointed at another file, or the file edited, memory can still hold the previous
  ledger's history, and nothing written may depend on it. Both targets share one merge
  function (`_merge_period_capacity`). The ledger file is never rewritten in
  place: the write goes to a temporary file in the same directory, is flushed and
  fsynced, and replaces the ledger in one `os.replace`, retried for up to five attempts
  0.2 s apart while it is refused with `PermissionError` (Windows, while a sync client,
  an editor or a virus scanner holds the file). A kill, a full disk or an interrupted
  sync therefore leaves the previous ledger whole, and any failure removes the
  temporary file. On POSIX the directory is then fsynced as well, so a crash or power
  loss after the write-back has returned cannot lose the rename — best effort: a file
  system that cannot fsync a directory is logged at debug level and the write stands.
  Windows has no directory fsync and is unchanged. The bytes are exactly those of a direct write (`indent=2`,
  `ensure_ascii=False`, trailing newline), and the ledger keeps its permission bits (a
  new one gets those a direct write gives it). The backfill writes the ledger the same
  way.
- **Backfill** (`capacity_backfill`): with the variable set, the rebuilt entries are
  written into the ledger — exactly the entries the `vehicles.json` path would have
  rewritten, both keys in full, whatever the entry held before — every other ledger
  entry is kept, and `vehicles.json` is only read. The summaries start from the ledger
  values. `--dry-run` writes nothing at all in this
  mode, not even the lock file or the ledger's directory.
- **The ledger is read when a report starts.** The package builds `VEHICLE_CONFIG` at
  import — for `python -m report_generator.cli` before `main()` loads `.env` — so a
  `JOLT_CAPACITY_LEDGER` set after the import would have been written by the
  write-back without ever being read. `JOLTReportGenerator.generate_report()` — behind
  `report_generator.generate_report()` and the CLI, for the EV and the diesel dispatch
  alike — now calls `configs.apply_capacity_ledger(VEHICLE_CONFIG)` before it reads the
  vehicle's config, and `main()` also applies it once `.env` is loaded, before the
  generator is built. It makes the two ledger keys of every configured vehicle exactly
  what a fresh `load_vehicle_configs()` gives — the ledger's value, else the
  `vehicles.json` value, else no key — so a variable set after the import, pointed at
  another file or at an edited one, never leaves a capacity or a history in memory
  from a ledger the reports no longer read. It is idempotent, and without the variable
  a strict no-op that reads nothing, neither the ledger nor `vehicles.json`. A
  registration not in `vehicles.json` is left alone, and so is a runtime fallback
  config injected by an earlier report: an un-onboarded vehicle takes no ledger state,
  so generating it twice in one process gives the same report twice. The same
  import-order limit applies, unchanged, to `JOLT_CONFIG_DIR` and to the postcode-cache
  path under `JOLT_CACHE_DIR`; `deployment.md` now says to export those rather than
  rely on `.env`.
- **Documentation correction.** `deployment.md` said a read-only config directory makes
  the write-back no-op with a warning. It does not: the write-back raises
  `PermissionError` — on the lock file or on `vehicles.json` — before the report is
  written (reproduced on 3.5.1 and on this release with a read-only `vehicles.json`).
  The behaviour is unchanged, as the unset path must be; the deployment contract now
  says so and recommends the external ledger for a read-only config directory.
- **Test suite.** The session conftest removes `JOLT_CAPACITY_LEDGER` at import, since a
  value inherited from the developer's shell would both change what the suite reads and
  let a test write into a real ledger, and an autouse fixture fails any test that
  leaves it set behind it, so a leak can never make the outcome depend on test order.
  New: 28 unit tests of the loaders and the overlay semantics (both keys, a partial
  entry, an uncovered vehicle, a ledger-only registration, a missing / blank / damaged
  file, the import-time overlay in a fresh interpreter, and `apply_capacity_ledger` on
  configs already loaded — a strict no-op without the variable that reads nothing, the
  loader's view in place, another ledger named or the file edited restoring every key
  it no longer carries, idempotent, copies, a registration not in `vehicles.json` and
  a skipped config left alone); 48 integration tests of the write side (ledger written
  and `vehicles.json` untouched, the lock, seeding from the file and never from memory,
  merging into an existing entry, a partial entry continuing the history the reports
  read, the merge equal to the loader's view for every entry shape, a ledger named
  later or edited down to its scalar between two reports never receiving the previous
  ledger's history, the membership rule, the no-donor no-op, backfill into the ledger
  — a partial entry written out in full — the dry run, both targets producing the same
  entry, byte-for-byte for the unset path, and the atomic file write: a direct write's
  bytes and permission bits, a failure part-way leaving the old ledger whole, the
  `PermissionError` retry and its limit, no temporary file left behind, and on POSIX
  the directory fsync after a successful replace, never on Windows and never failing
  the write when the file system refuses it); 7 integration
  tests of the ledger read at report start (the EV and the diesel dispatch, the
  convenience function, a ledger changed between two reports — another file or the
  file edited — leaving no stale capacity, the strict no-op without the variable, a
  runtime fallback config left alone); plus 2 CLI tests (a ledger named only in `.env`
  is read before the generator is built; without the variable the configs are left
  alone).
- **Repository tooling** (no effect on the package or on any report):
  - CI — `.github/workflows/tests.yml` runs the offline suite on ubuntu / Python 3.11,
    from a clean install of the two requirements files, on every push and pull request.
  - `doc/versions.md` discipline — a test requires every `## X.Y.Z` heading to be
    SemVer, ascending, newest last, and the last one to equal `__version__`; with the
    namespace test it is what enforces "bump the version, append its section, state
    its data namespace" in one change.
  - A fixture per onboarded vehicle — `tests/fixtures/make_fixture.py` turns a
    `--debug` raw artefact into an anonymised fixture (rigid spherical rotation of every
    GPS position onto (0.5, 0.5) by an unrecorded random angle, headings turned with
    it, driver columns dropped, vehicle identity replaced by the alias, everything
    else verbatim; it refuses to write while the registration survives anywhere, in
    any case and however it is split — any run of spaces of any kind, zero-width
    characters, hyphens, dashes or underscores between its characters, so the 3+4
    and 3+3 plates are caught as well as the 4+3 ones, while two adjacent CSV cells
    never are — and the file name loses every such spelling),
    registers it in `tests/fixtures/raw_fixtures.json` and adds its frozen config;
    `regenerate_goldens.py --alias` writes its first golden; and
    `integration/test_registered_fixtures.py` guards every registered fixture — golden,
    determinism, consumer contract, de-identification (the maker's spelling rule
    included, for every live registration, in the file and its path), registry
    consistency. Tried end
    to end on a real EV telematics file and a real SRFLOGGER_V2 logger file in a
    scratch clone (geodesic step distances preserved to 1e-12 km, headings consistent
    with the rotated track, registration absent, the full suite green with both
    fixtures added). The four original fixtures' heading columns predate the heading
    rule and are verbatim.

  Full suite: **1183 passed, 4 skipped** (3.5.1: 1035 passed, 4 skipped).

## 3.7.0 — date-effective vehicle settings (`period_overrides`)

- **Data namespace: unchanged, still `3.3.0/`.** No vehicle in the shipped
  `vehicles.json` sets the new field, and a vehicle without it is segmented exactly as
  before, so no reported cell can change. Verified offline: every registered fixture's
  segmentation regenerates its golden byte for byte, also with the field set to `null`
  or `[]`; the three EV fixture workbooks built through the generator's own per-leg loop,
  capacity correction, EP grading, Stop insertion and writer match 3.6.0 in all 6511
  cells; and a vehicle without the field never reaches the resolver. On live data, the
  workspace parity smoke set — six vehicles, EV and diesel (YK73WFN, AV24LXK, YN25RSY,
  WU70GLV, MK15BEV, EX74JXW), three days each — generated with this release and the
  external capacity ledger is identical to the same set generated by 3.5.1: 6/6
  vehicles, 0 differing cells, identical capacity ledgers. The 3.6.0 release gives the
  same result on the same set. That set was generated before the charge/trip boundary
  reconciliation below was added; no configured pipeline switches it on, and the
  offline comparisons above include it.
- **Why.** Vehicle settings had no date, so changing a vehicle's segmentation meant
  re-segmenting its whole history. A feed can change character mid-trial — the first
  user will be a SOC-branch vehicle whose telematics speed thinned out from July 2026,
  merging trips across stops, while its Logger kept a good 1 Hz speed — and the
  segmentation before the change must stay exactly as it is. The override itself comes
  with that vehicle's re-tune, not in this release.
- **Schema.** Optional, per vehicle: `"period_overrides": [{"from": "YYYY-MM-DD", "to":
  "YYYY-MM-DD" | null, "reason": "<text>", "set": {<key>: <value>, ...}}]`. `from` is
  required and inclusive, `to` optional and exclusive (`null` or absent: open-ended),
  `reason` documentation only, `set` required and non-empty.
- **What `set` may change** (`configs.PERIOD_OVERRIDE_KEYS`): the vehicle settings the
  per-leg segmentation reads — `pipeline`, `prefer_logger_speed`,
  `min_stop_duration_min`, `split_by_mass`, `merge_by_mass`, `split_long_stops_min`,
  `min_cluster_gap_kg` (all read by `run_segment_detection`) and `mass_agg` (read by
  `resolve_mass_agg`: a vehicle-level estimator outranks the pipeline's, so a window
  that switches the pipeline could not otherwise change it). The column mappings, the
  capacity keys and the ledger, `fuel_type`, `srf_reg`, the operator keys and the field
  itself stay whole-vehicle; the diesel-only trip settings are out of reach, see below.
- **Load-time validation.** `load_vehicle_configs()` — and so the import-time load —
  raises `ValueError` naming the vehicle and the override for a key outside the
  allow-list, a value of the wrong kind (flags must be `true`/`false`, durations and the
  cluster gap positive numbers, `split_long_stops_min` may be `null` to switch it off), a
  missing or empty `set`, a missing `from`, an unknown field, a date not written
  `YYYY-MM-DD`, `to` not after `from`, overlapping windows (adjacent ones are fine), and
  a `pipeline` that is not in `pipelines.json`. A vehicle without the field costs one key
  lookup; `pipelines.json` is read only for an override that sets a pipeline.
- **Resolution.** `configs.effective_vehicle_config(cfg, when)` (new, public) returns the
  entry updated with the `set` of the window containing `when`, as a new dict without
  `period_overrides`; it never modifies `cfg` and resolving its result again changes
  nothing. A leg is resolved for the UTC date of the first valid timestamp of its
  telematics frame (`segmentation.timeutil.frame_utc_date`), so a leg starting at 23:59
  UTC the day before `from` keeps the base settings. `run_segment_detection` resolves
  from the frame it is handed — the generator and an external renderer re-driving it on
  the same frame get the same settings with no change on their side —
  `resolve_mass_agg` takes an optional keyword `when`, and the generator resolves the
  per-segment mass estimator it hands the row builder per leg, from the same frame.
- **Diesel.** A DIESEL vehicle may not carry the field: the diesel pipeline segments its
  legs with its own settings (`speed_threshold_kmh`, `min_trip_duration_min`,
  `min_trip_distance_km`, `min_stop_duration_min`, read straight from the entry), its
  `pipeline` is a dispatch marker rather than a `pipelines.json` key, and no diesel
  vehicle needs it; the load rejects it rather than letting it be silently ignored.
- **Callers outside the package.** One that re-drives `run_segment_detection` needs
  nothing. One that reads a per-leg setting itself — `resolve_mass_agg(reg)` for the mass
  estimator, say — gets the base settings unless it passes the leg's date:
  `resolve_mass_agg(reg, when=frame_utc_date(df))` or
  `effective_vehicle_config(cfg, frame_utc_date(df))`.
- **Fixture maker.** `tests/fixtures/make_fixture.py --config-from` freezes a vehicle with
  overrides as it applies on the fixture's date, so the frozen entry carries no
  overrides and never names a live pipeline.
- **Charge / trip boundary reconciliation (opt-in).** On a sparse telematics feed a
  charge found on the SOC ends at the first sample after the charging, which can be
  taken once the vehicle has already set off — after the start of a trip found on the
  1 Hz Logger speed — so the two overlap, a structural error no parameter removes. A
  pipeline that sets the new top-level key `reconcile_charge_boundaries` (default
  `false`; only JSON `true` switches it on; a vehicle reaches it through its pipeline,
  a `period_overrides` window's included) has `run_segment_detection` clamp such charges
  on the final segments, after the split, merge and anchor ordering and before the EP
  audits and the figure hook: a trip starting inside a charge moves the charge's end to
  the trip's start, a trip ending inside one moves its start to the trip's end. The
  trip's boundary wins, being the higher-rate observation; the charge's SOC values,
  energy, energy source and energy anchors are unchanged, and the new time keeps the
  charge's time-zone form. A trip wholly inside a charge, a charge wholly inside a trip,
  or clamps that would leave no duration leave the charge whole, with a warning; the
  number of clamped boundaries is logged. The charge row's duration and battery power
  follow the clamped times, no Stop row falls between the charge and its trip, and the
  charger fusion still matches the session inside the charge. No configured pipeline
  sets the key.
- **Test suite.** New: 79 unit tests (every validation error, the overlap shapes, the
  import-time load in a fresh interpreter; resolution before, on and after `from`, `to`
  exclusive, between two windows, no field, idempotence, purity, datetimes by their UTC
  date; the leg-date rule; `resolve_mass_agg(when=)`), 16 fixture-driven integration
  tests (a leg on or after `from` runs the override's speed branch on Logger trips and
  matches the flattened config while differing from the base; a leg before it matches
  the golden; a leg starting at 23:59 the day before `from` keeps the base, one at 00:00
  on the day takes the override; a closed window no longer applies; the generator's own
  per-leg call and a direct call on the same frame give the same segments and hand the
  same mass estimator to the rows and to the painter; an unset field leaves every golden
  byte-identical; a vehicle without the field never reaches the resolver), and 3
  fixture-maker tests. For the reconciliation: 18 unit tests (both directions, a charge
  between two trips, trip order, several charges, touching and disjoint trips, the
  trip-inside / charge-inside / identical-window and zero- and negative-duration cases
  with their warnings, the time-zone forms) and 13 fixture-driven integration tests (the
  fixture's real overlap reached through a `period_overrides` window and clamped with
  only the charge's end moved; the overlap left without the key; off equal to absent;
  on without an overlap a no-op; the painter handed the clamped charges; the row
  duration and battery power, the Stop insertion and the charger fusion on the clamped
  charge; every golden byte-identical with the key off and on).

  Full suite: **1312 passed, 4 skipped** (3.6.0: 1183 passed, 4 skipped).

## 3.8.0 — event-row SOC filter and trips kept outside the capacity band (both opt-in)

- **Report output: unchanged for every configured vehicle — both keys are opt-in and no
  pipeline sets them.** Data namespace: unchanged, still `3.3.0/`. Verified offline: the
  three EV fixture workbooks built through `JOLTReportGenerator.generate_report` itself —
  the per-leg loop, capacity correction, EP grading, Stop insertion and writer, over a
  mocked SRF surface — match 3.7.0 in all 6511 cells, with identical capacity ledgers;
  every registered fixture regenerates its golden byte for byte, also with
  `keep_trips_outside_cap_band: false`; and a replay of the segmentation over 7935
  persisted raw telematics legs of the 16 configured EV vehicles (9213 charges, 41726
  trips) gives identical segments under 3.7.0 and 3.8.0.
- **Why.** Two segmentation defects on a telematics feed that sends 60-s periodic rows
  (`trigger_type` `TIMER`) and event rows in between, both confirmed by running the
  package's own functions on the raw frames:
  - *Phantom charges from event-row SOC spikes.* After the vehicle has stood with the
    ignition off, the periodic rows carry no SOC for minutes, and the ignition-on row then
    reports a SOC 4–6 points above the value before and after it (58, four missing, 64,
    60). The SOC charge detector turns each such step into a charge of 0.4 to 28 minutes
    with about 20 kWh of `soc_estimate` energy — 13 of the 55 charges detected on that
    feed. Across its raw data, 51 event rows exceed both neighbouring periodic
    readings by 3 points or more, and no periodic row does.
  - *Genuine trips dropped by the capacity band.* The speed branch drops a trip whose
    SOC-implied capacity `|ΔE| / (|ΔSOC|/100)` falls outside `nominal × [0.5, 2.0]`. On
    this feed the energy is the moving-energy counter, while the integer SOC also falls
    while parked and is quantised, so short and medium trips imply capacities below the
    floor: 23 speed-confirmed trips with good counter energy (275 km, 246 kWh, up to 62 km
    each) vanished from the report, their time turned into Stop rows containing driving.
- **`soc_event_spike_pct`** (new pipeline key, top level; a positive number of SOC
  percentage points, absent = off). `run_segment_detection`, before any detector reads
  the SOC and when the frame has a `trigger_type` column, sets to NaN the SOC of each
  event row (`trigger_type` other than `TIMER`, matched without regard to case) that
  exceeds both the nearest preceding and the nearest following valid `TIMER` SOC (valid:
  a number other than 0) by at least the threshold. `TIMER` rows never change, nor does
  an event row without a valid periodic reading on both sides, one below its neighbours
  or one without a parseable timestamp; neighbours are found in time order. Blanking
  every event row's SOC was tried and rejected: it also deletes real charges whose rise
  sits partly on event rows. The charges, the trips, the EP-confidence diagnostics and the
  painter (`figure_hook`'s `df_raw`) all see the cleaned copy; the caller's frame — and so
  the raw telematics the generator persists — is never modified. The number of readings
  blanked per leg is logged; without a `trigger_type` column the key does nothing, logged
  once per vehicle. On the diagnosed feed, at 3 points, it blanks exactly those 51
  readings: the 13 phantom charges go, every genuine charge stays (one with a two-minute
  pause reads as one session, one that ended on an excursion ends at its last genuine
  reading), five trips the band had dropped for a spike-inflated ΔSOC come back into the
  band, and four trips whose start SOC was a spike get a smaller ΔSOC.
- **`speed_params.keep_trips_outside_cap_band`** (new; bool, default `false`, only `true`
  switches it on). In `find_discharge_segments_by_speed` (new keyword of the same name),
  a trip whose implied capacity falls outside the band and whose energy comes from a
  counter (`total_energy` / `moving_energy`) is kept with `effective_capacity_kwh = None`
  instead of dropped; a trip on `soc_estimate` energy is dropped as before. A trip without
  a capacity is no capacity donor (`_period_capacity_from_rows`, the step-1 donor pools,
  the ±1σ pass, and the backfill, which reads its blank cell as none). A private segment
  marker, `_capacity_outside_band`, makes the mass split, the mass merge and the anchor
  ordering — the three steps that otherwise recompute a segment's capacity — give nothing
  built from a kept trip a capacity either (a merge taking in a kept trip carries none,
  even where the combined figure would fall inside the band); `run_segment_detection`
  removes the marker before the diagnostics, the painter and the caller see the
  segments. The per-leg number of such trips is logged. On the diagnosed feed it keeps
  exactly the 23 trips, 274.8 km and 246.0 kWh, every other trip unchanged; with both keys
  on, 18 trips remain without a capacity.
- **Interaction with `soc_energy_fallback`.** The band key trusts the counter where it
  and ΔSOC disagree; the per-vehicle SOC-energy fallback trusts the SOC. A kept trip has
  no capacity, so the ±1σ pass never judges it and the fallback cannot rewrite it: it
  keeps its counter energy (on the speed fixture, whose vehicle has the fallback on, the
  merged kept trip reports 48.7 kWh from the counter, graded `poor` with `CAP_INCONS`,
  where the fallback wrote 150.3 kWh without the key). Enable the band key on a vehicle
  with the fallback only deliberately.
- **Validation.** `load_pipeline_configs()` — and so the import-time load of
  `PIPELINE_CONFIGS` — now validates these two keys: a threshold that is not a positive
  number, a switch that is not `true`/`false`, the switch anywhere but in `speed_params`,
  or the threshold inside a parameter group (where it would reach a detector as an
  unexpected argument) fails with a `ValueError` naming the pipeline and the key. No other
  pipeline key is validated, as before. A threshold that bypasses the loader (injected at
  run time) is refused by `run_segment_detection` in the same terms.
- **`period_overrides`.** The allow-list is unchanged: the new keys are pipeline settings,
  not vehicle settings, and a window reaches them by switching its `pipeline` to one that
  sets them — as for `reconcile_charge_boundaries`.
- **Callers outside the package.** A renderer re-driving `run_segment_detection` gets the
  same segments as the generator and the cleaned frame in `figure_hook`. New name:
  `segmentation.constants.TRIGGER_TYPE_COL` (also on the `segmentation` and
  `segment_algorithms` surfaces). A segment returned by `find_discharge_segments_by_speed`
  itself may carry the private marker; `run_segment_detection` never returns it.
- **Test suite.** New: 36 unit tests of the event-row filter (the parked ignition-on
  excursion, several event rows of one excursion, the excursion ending a real charge, a
  rise carried by event rows kept, `TIMER` readings never touched, the inclusive
  threshold, one side below it, a reading below its neighbours, no periodic reading on
  one side, a zero periodic SOC skipped as a reference, rows without a timestamp, time
  order over row order, any index, a row without a trigger type, case and padding, a
  frame without the column returned as it is, the caller's frame untouched, the SOC
  column's type, an invalid threshold refused, the once-per-vehicle and per-leg logs, and
  through `run_segment_detection`: the phantom charge with and without the key, the
  painter handed the cleaned frame, no `trigger_type` no change); 25 unit tests of the
  band key (below and above the band, both counters, `soc_estimate` dropped, in-band and
  unbounded trips unchanged, only `true` switching it on, the split, merge and anchor
  clamp of a kept trip and of an ordinary one, the orchestrator keeping it without the
  marker, a kept trip split at a load change, the row, the donor exclusion, the
  generator's `_finalize_rows` and the workbook read back by the backfill); 28 unit tests
  of the validation (valid values, each wrong kind, each misplacement, the pipeline named,
  the import-time load in a fresh interpreter); 10 fixture-driven integration tests (every
  golden with the band key off; the filter trimming the excursion that ends a real charge
  and leaving legs without one, or without the column, as their goldens; the painter
  handed the filtered leg; the band key a no-op where every trip is in the band, and on a
  fixture with a band-dropped trip bringing it back, merged, without a capacity, every
  other trip untouched, through finalisation and the workbook); and 1 live-config
  structure test.

  Full suite: **1412 passed, 4 skipped** (3.7.0: 1312 passed, 4 skipped).
