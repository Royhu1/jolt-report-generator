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
> Two notes on reading it. The entries are the upstream record kept **verbatim**, so
> a section may name a path (`src/jolt_toolkit/…`, a data tree, a sibling tool) that
> belongs to the JOLT research project this repository was extracted from — the
> statement was true of the release it describes. And `__version__` tracks that
> upstream code revision, not this repository's layout: moving the package to the
> root is recorded in git, not as a release here.

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
