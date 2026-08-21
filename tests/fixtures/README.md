# Test fixtures

Everything the offline test suite reads. Four anonymised raw feeds, four frozen
vehicle/pipeline configs and the golden segmentation snapshots derived from them.

```
fixtures/
├── raw/                     # anonymised real telematics / logger CSVs (the inputs)
│   ├── EVSPD01/raw_2025-06-27_0000.csv      # EV, speed branch, full AC/DC counters
│   ├── EVSOC01/raw_2026-04-24_0000.csv      # EV, SOC branch (no energy counters)
│   ├── EVMAD01/raw_2025-07-29_0000.csv      # EV, mad_tw_mean mass + merge_by_mass=false
│   └── DSL01/logger_2025-10-07_0000.csv     # diesel SRF logger leg (real J1939 names)
├── configs/                 # FROZEN vehicles.json / pipelines.json for the aliases
├── expected/                # golden segmentation snapshots (JSON)
├── regenerate_goldens.py    # regenerates everything under expected/
└── README.md                # this file
```

## What each raw fixture exercises

| Alias | Rows x cols | Derived from | What it is there for |
|-------|-------------|--------------|----------------------|
| `EVSPD01` | 925 x 69 | a Volvo FM Electric on `volvo_speed_02` | The **speed branch** end to end: trips detected from `wheel_based_speed`, discharge energy from the `total_electric_energy_used_plugged_in_included` counter, charges from the AC/DC counters (`ac_dc`), real mass variation across the day, and a leg that triggers the "anchor overlap clamp skipped" degradation. Also the vehicle that opts in to the SOC-energy fallback. |
| `EVSOC01` | 338 x 15 | a Mercedes eActros 600 on `mercedes_soc` | The **SOC branch**: a deliberately narrow feed with no AC/DC, no moving-energy and no total-energy-plugged-in column, so every leg resolves to `soc_estimate` and the capacity seed drives the energy. Also exercises the pipeline's `min_trip_distance_km` gate. |
| `EVMAD01` | 430 x 62 | a Scania P-series BEV on `scania_speed_00` | The two non-default mass behaviours together: vehicle-level `mass_agg: "mad_tw_mean"` (beating the pipeline's `iqr_median`) and pipeline-level `merge_by_mass: false`. Discharge energy resolves to `moving_energy`, giving a third energy source. |
| `DSL01` | 544 x 20 | a DAF XF 450 diesel | The **diesel logger path**: `index_col=0` timestamps, real J1939 channel names (`LFC engine total fuel used`, `VDHR hr total vehicle distance`, `CVW gross combination vehicle weight`, Channel-7 weather, `EEC2`/`EBC1` pedals). Doubles as the source of realistic Logger channel data for the `LoggerPatcher` tests. |

## De-identification

These are **real** telematics rows, anonymised before being committed:

- **GPS**: every latitude/longitude pair was put through a **rigid transform** onto
  a synthetic origin of `(0.5, 0.5)` with an **unrecorded rotation**. The rotation
  angle and translation were not kept, so the transform is **irreversible** — the
  real route cannot be recovered. Relative geometry (distances, bearings between
  points, the shape of the route) is preserved **exactly**, which is what the
  home-point detection, the `Point(lat lon)` formatting and the origin/destination
  logic actually depend on.
- **Driver identity**: all `driver1_*` columns were dropped.
- **Vehicle identity**: the registration is replaced by an alias (`EVSPD01`,
  `EVSOC01`, `EVMAD01`, `DSL01`) in both the file path and the `vehicleId` column.
  The aliases do not appear in the live `report_generator/configs/vehicles.json`.
- Everything else — timestamps, SOC, energy counters, odometer, mass, speed,
  weather — is **verbatim real data**. That is the point: the numbers the tests
  assert on are numbers the pipeline really produces in the field.

Column NAMES are the real feed's names and must not be renamed: half the value of
these fixtures is that they prove the column-name mapping works against the actual
SRF spelling.

## The frozen configs — do NOT sync them

`configs/vehicles.json` and `configs/pipelines.json` are **frozen copies**, keyed by
the aliases above and derived from the real fleet entries at the time the fixtures
were captured. They are deliberately **not** the live
`report_generator/configs/*.json`, and they must **never** be "kept in sync" with
them.

*Why:* segmentation parameters are tuned per vehicle and get retuned. If the tests
asserted behaviour against the live configs, a legitimate retune of a real vehicle
would turn the suite red for no reason, and — worse — the golden files would have
to be regenerated for a change that has nothing to do with the code. Freezing the
configs means a golden diff always means **the code changed**.

Practical consequences:

- Adding, removing or retuning a real vehicle in `report_generator/configs/` must
  **not** touch these files.
- A change to the config **schema** (a new field the loader requires, a renamed
  key) SHOULD be mirrored here — that is a code change, and the fixtures exist to
  catch it.
- The alias pipelines are inlined under alias-specific names (`evspd01_speed`,
  `evsoc01_soc`, `evmad01_speed`) so the fixture set is self-contained and cannot
  accidentally resolve a live pipeline. `DSL01`'s `"pipeline": "dsl01_diesel_logger"`
  is a **dispatch marker only** (`fuel_type == DIESEL` + `leg_source ==
  SRFLOGGER_V1`), exactly like the real diesel vehicles; it is intentionally not a
  `pipelines.json` key.

The configs are injected into the shared `VEHICLE_CONFIG` / `PIPELINE_CONFIGS`
dictionaries with `monkeypatch.setitem` by the `frozen_configs` fixture, so the
live fleet entries stay present and untouched and the aliases disappear again at
teardown.

## Golden files (`expected/`)

| File | What it pins |
|------|--------------|
| `segments_EVSPD01.json` | Every field of all 4 charge + 12 discharge segments from the speed branch, including the private `_anchor_*` fields. |
| `segments_EVSOC01.json` | All 3 charge + 6 discharge segments from the SOC branch. |
| `segments_EVMAD01.json` | All 3 charge + 10 discharge segments with `merge_by_mass: false`. |
| `diesel_segments_DSL01.json` | The single diesel trip's full 20-key metrics dict. |

Each file records the `alias` and the `source` fixture path so a golden can never
drift onto a different input. Timestamps are ISO strings that keep their offset
(the naive/aware split is itself part of the behaviour), floats are rounded to 6
decimal places and NaN is written as the string `"NaN"`.

### Regenerating

```bash
python tests/fixtures/regenerate_goldens.py
```

Runs fully offline (no `SRF_API_KEY`, no network) and rewrites all four files.

**Only regenerate when a behaviour change is intended.** The goldens exist so that
an unintended change to the segmentation maths shows up as a diff. Read the JSON
diff before committing it, and say in the commit message which change caused it.
