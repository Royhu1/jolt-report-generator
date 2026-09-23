# Test fixtures

Everything the offline test suite reads: the anonymised raw feeds, their frozen
vehicle/pipeline configs, the golden segmentation snapshots derived from them, and
the tools that make and refresh them. The four original fixtures are listed below;
every further one is added per onboarded vehicle with `make_fixture.py` (see the last
section) and registered in `raw_fixtures.json`.

```
fixtures/
├── raw/                     # anonymised real telematics / logger CSVs (the inputs), one directory per alias
│   ├── EVSPD01/raw_2025-06-27_0000.csv      # EV, speed branch, full AC/DC counters
│   ├── EVSOC01/raw_2026-04-24_0000.csv      # EV, SOC branch (no energy counters)
│   ├── EVMAD01/raw_2025-07-29_0000.csv      # EV, mad_tw_mean mass + merge_by_mass=false
│   └── DSL01/logger_2025-10-07_0000.csv     # diesel SRF logger leg (real J1939 names)
├── raw_fixtures.json        # the registry: alias -> {"path", "kind": "ev" | "diesel"}
├── configs/                 # FROZEN vehicles.json / pipelines.json for the aliases
├── expected/                # golden segmentation snapshots (JSON), one per registered fixture
├── make_fixture.py          # raw artefact -> anonymised fixture + registry entry + frozen config
├── regenerate_goldens.py    # regenerates the goldens under expected/ (all, or --alias)
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

These are **real** telematics rows, anonymised before being committed. This is the
procedure `make_fixture.py` applies — nothing else is changed:

- **GPS**: every latitude/longitude pair (`latitude`/`longitude`,
  `gnss_latitude`/`gnss_longitude`, the logger's `2 latitude`/`2 longitude`) is put
  through one **rigid transform** onto a synthetic origin of `(0.5, 0.5)`: a rotation
  of the sphere that carries the track's centroid onto `(0.5, 0.5)`, followed by a
  spin about that point by a **random, unrecorded angle**. The angle is drawn from the
  operating system's entropy and is never printed, logged or stored, so the transform
  cannot be undone from the fixture. Relative geometry — every great-circle distance,
  every angle between two directions, the shape of the route — is preserved
  **exactly**, which is what the home-point detection, the `Point(lat lon)` formatting
  and the origin/destination logic actually depend on. A GPS cell that is present but
  not a number, or half of a pair, is blanked rather than left with its real value.
- **Headings** (`gnss_heading`, the logger's `2 bearing`) are turned by the same
  rotation, at the position they were measured. Left untouched, a heading minus the
  rotated track's own bearing would give the angle straight back.
- **Driver identity**: every driver column is dropped — the telematics `driver1_*`
  family and the logger's `DI driver <n> identification` / `TCO1 driver <n> working
  state`. (The J1939 signal "driver's demand engine percent torque" is not a driver
  column and is kept.)
- **Vehicle identity**: the registration is replaced by an alias in the file path, and
  the `vehicleId` and `VIN vehicle identification number` values by the alias. The
  aliases do not appear in the live `report_generator/configs/vehicles.json`. The tool
  refuses to write a fixture in which the registration still appears anywhere — in any
  case, and however it is split: any run of spaces (including tabs and no-break
  spaces), zero-width characters, hyphens, dashes or underscores between its
  characters still counts (`ABC 1234`, `A12-BCD`, `ab12_cde`), so every UK plate
  layout is caught. A comma or a line break does not, so two adjacent CSV cells never
  read as a registration. The file name loses every such spelling too.
- Everything else — timestamps, SOC, energy counters, odometer, mass, speed,
  altitude, weather — is **verbatim real data**, character for character. That is the
  point: the numbers the tests assert on are numbers the pipeline really produces in
  the field.

The four original fixtures above were anonymised before the heading rule existed:
their `gnss_heading` / `2 bearing` columns are verbatim, which gives away their spin
angle (not their location). No code reads those columns.

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
- A frozen entry carries no identity, provenance or machine-written state: no `vin`,
  `description`, `operator` / `operators` or `effective_capacity_quarterly`, and
  `srf_reg` is the alias.

The configs are injected into the shared `VEHICLE_CONFIG` / `PIPELINE_CONFIGS`
dictionaries with `monkeypatch.setitem` by the `frozen_configs` fixture, so the
live fleet entries stay present and untouched and the aliases disappear again at
teardown.

## Golden files (`expected/`)

One per registered fixture: `segments_<ALIAS>.json` for an EV fixture (every field of
every charge and discharge segment, including the private `_anchor_*` fields and each
discharge segment's `ep_audit` diagnostics), `diesel_segments_<ALIAS>.json` for a
diesel one (every trip's full metrics dict). The four originals:

| File | What it pins |
|------|--------------|
| `segments_EVSPD01.json` | All 4 charge + 12 discharge segments from the speed branch. |
| `segments_EVSOC01.json` | All 3 charge + 6 discharge segments from the SOC branch. |
| `segments_EVMAD01.json` | All 3 charge + 10 discharge segments with `merge_by_mass: false`. |
| `diesel_segments_DSL01.json` | The single diesel trip's full 20-key metrics dict. |

Each file records the `alias` and the `source` fixture path so a golden can never
drift onto a different input. Timestamps are ISO strings that keep their offset
(the naive/aware split is itself part of the behaviour), floats are rounded to 6
decimal places and NaN is written as the string `"NaN"`. A nested dict — the
`ep_audit` key every EV discharge segment carries — is written field by field under
the same rules, so each measured diagnostic is pinned individually.

Every registered fixture is checked against its golden by
`tests/integration/test_registered_fixtures.py`, together with a consumer contract
(required keys, chronology, sign convention, allowed energy sources), determinism,
the de-identification rules above, and agreement between the registry, the files, the
frozen configs and the goldens. The four originals are additionally pinned by
hand-written expectations in `test_segmentation_fixtures.py` and
`test_diesel_pipeline_fixture.py`.

### Regenerating

```bash
python tests/fixtures/regenerate_goldens.py                  # every registered fixture
python tests/fixtures/regenerate_goldens.py --alias EVSPD01  # just one
```

Runs fully offline (no `SRF_API_KEY`, no network).

**Only regenerate when a behaviour change is intended** — or, for one alias, to write
a newly added fixture's first golden. The goldens exist so that an unintended change
to the segmentation maths shows up as a diff. Read the JSON diff before committing
it, and say in the commit message which change caused it.

## Adding a fixture for a newly onboarded vehicle

Each onboarded vehicle gets one fixture, so the offline suite guards every vehicle of
the fleet. From the repository root, with a report generated for the vehicle with
`--debug` (which persists the raw artefacts):

1. **Pick one raw file with trips in it** — for an EV its raw telematics,
   `<out>/<REG>/raw_telematics/raw_<date>_<idx>.csv`; for a diesel vehicle its logger
   CSV, `<out>/<REG>/raw_logger_v<N>/logger_<date>_<idx>.csv`. Logger CSVs are
   one row per second and large: keep a window of rows.
2. **Make the fixture** under a new alias (`EV…` / `DSL…` plus a number, never a
   registration):

   ```bash
   python tests/fixtures/make_fixture.py <raw file> --alias EVSPD02 --config-from <REG>
   python tests/fixtures/make_fixture.py <logger file> --alias DSL02 --rows 0:6000 --config-from <REG>
   ```

   This writes `raw/<ALIAS>/<file>`, registers it in `raw_fixtures.json` and, with
   `--config-from`, adds the alias's frozen config: a copy of the live entry (and, for
   an EV, its pipeline as `<alias>_<branch>`) without identity or ledger fields. The
   registration is taken from the artefact path (or `--registration`) and the tool
   refuses to write if it survives anywhere. Nothing is written if a check fails;
   `--force` replaces an existing alias.
3. **Write its first golden**:

   ```bash
   python tests/fixtures/regenerate_goldens.py --alias EVSPD02
   ```

   It warns when the fixture yields no trip — such a fixture guards nothing and the
   suite rejects it; pick another file or a wider `--rows` window.
4. **Run `pytest`** — `test_registered_fixtures.py` now covers the new alias — and
   add its row to the fixture table above (the tool prints a starting line for it).
   Commit the fixture, the registry, the frozen config and the golden together.
