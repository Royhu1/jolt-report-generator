# Tests

```bash
pip install -r requirements-dev.txt
pytest                       # ~60 s, 1316 tests, fully offline
```

No `SRF_API_KEY`, no network, no writable state outside `tmp_path`.

CI runs exactly this on every push and pull request
(`.github/workflows/tests.yml`: ubuntu-latest, Python 3.11, a clean install from
`requirements.txt` + `requirements-dev.txt`). So a test must not depend on a local
cache, a key, a Windows path or anything else only a developer machine has — if one
does, fix the test.

## Layout

```
tests/
├── conftest.py              # env setup + shared fixtures (see below)
├── test_imports.py          # v3.0.0 import / facade contract (module paths, re-exports)
├── test_column_contracts.py # HEADERS / DIESEL_HEADERS vs the patchers' hard-coded indices
├── test_configs.py          # the LIVE configs load and satisfy what the generator reads
├── test_cli.py              # process-level CLI contract (subprocess: --help, rc-2 fast-fail)
├── test_general_pipeline.py # v3.1.0 general fallback pipeline contract
├── unit/                    # pure functions — no fixture data, no filesystem beyond tmp_path
├── integration/             # multi-module, driven by the committed raw fixtures
│   └── conftest.py          # fixture-driven helpers (segmentation runner, golden loader)
└── fixtures/                # anonymised raw CSVs, frozen configs, goldens — see its README
```

`unit/` holds tests whose input is constructed inline and whose expected value is
hand-computed from the documented formula. `integration/` holds tests that run
several modules together over the real (anonymised) fixture data and assert on
artefacts — segment dicts, xlsx workbooks, the capacity ledger (in `vehicles.json`
and in the external `JOLT_CAPACITY_LEDGER` file).

The five contract files directly under `tests/` predate this suite.

| Area | Tests |
|------|-------|
| Existing contract suite (`tests/*.py`) | 254 |
| `unit/` | 737 |
| `integration/` | 325 |

## The offline guarantee

Four things enforce it, all in the top-level `tests/conftest.py`:

1. **`JOLT_CACHE_DIR` is set at module import time**, before pytest imports any
   test module and therefore before `report_generator` is first imported. This
   matters: `report_generator/row_builder.py` evaluates `get_cache_dir()` at
   IMPORT time to build `_POSTCODE_CACHE_PATH` and immediately loads that cache.
   Setting the variable later would be too late. The directory is a `mkdtemp` and
   is removed in `pytest_sessionfinish`.
2. **`SRF_API_KEY` / `OPENWEATHER_API_KEYS` are forced empty**, so nothing can
   silently authenticate, and **`JOLT_CAPACITY_LEDGER` is removed**, so a ledger
   named in the developer's shell is neither overlaid on the configs the suite reads
   nor written by a test. Tests of the external ledger set it on `tmp_path`, through
   `monkeypatch`; an autouse fixture fails any test that leaves it set, since every
   later write-back of the session would land in that test's ledger.
3. **Outbound sockets are blocked** for the whole session (`socket.connect` and
   `socket.create_connection` raise `NetworkAccessAttempted`). A test that needs
   remote data must inject a `Mock` instead.
4. **Dependency injection everywhere.** `_seg_to_row(srf_data=None)` skips the
   postcode lookup; `ChargerPatcher(srf_data=...)` / `LoggerPatcher(srf_data=...)`
   take a client; both patchers' `patch_file` accept pre-loaded windows / legs;
   `run_segment_detection` is pure. Where a client must exist,
   `report_generator._generator.make_srf_client` is monkeypatched
   (that is the single construction site — `_generator` imports the name into its
   own namespace, so patch it *there*).

## Shared fixtures (`tests/conftest.py`)

| Fixture | Gives you |
|---------|-----------|
| `fixtures_dir` | `tests/fixtures` as a `Path`. |
| `raw_fixture_path` | `alias -> Path` for a committed raw CSV. |
| `load_raw_telematics` | An EV fixture read the way production reads it: `pd.read_csv(path, dtype=str)`. |
| `load_logger_csv` | The diesel fixture read the way production reads it: `pd.read_csv(path, index_col=0)`. |
| `frozen_config_data` | The frozen `vehicles.json` / `pipelines.json` fixture pair, as dicts. |
| `frozen_configs` | The same, **injected** into `VEHICLE_CONFIG` / `PIPELINE_CONFIGS` via `monkeypatch.setitem` (they are shared by reference across the package, so this is the sanctioned injection point — production does the same for the runtime fallback config). |
| `serialise` | The shared segment serialiser used by both the goldens and the tests. |
| `raw_fixture_map` | `alias -> relative fixture path`. |

And in `tests/integration/conftest.py`: `run_fixture_segmentation`, `load_golden`,
`diesel_fixture_frame`.

**Never assert behaviour against the live `report_generator/configs/*.json`.** Use
the frozen fixture configs, or a synthetic entry injected with
`monkeypatch.setitem(constants.VEHICLE_CONFIG, "UTVEH01", cfg)`. A parameter
retune on a real vehicle must not be able to turn this suite red.
(`tests/test_configs.py` is the one deliberate exception: its job *is* to validate
the live configs' schema, and it asserts only structure, never values.)

## Golden files

`tests/integration/test_registered_fixtures.py` compares every field of every
produced segment of every fixture registered in `tests/fixtures/raw_fixtures.json`
against its frozen JSON snapshot in `tests/fixtures/expected/`;
`test_segmentation_fixtures.py` and `test_diesel_pipeline_fixture.py` add
hand-written expectations for the four original fixtures. Regenerate with:

```bash
python tests/fixtures/regenerate_goldens.py [--alias ALIAS]
```

Only do that when a behaviour change is **intended**, and review the diff — the
goldens exist precisely so that an unintended change to the segmentation maths
cannot pass unnoticed. A newly onboarded vehicle gets its own fixture with
`tests/fixtures/make_fixture.py`. Full details in `tests/fixtures/README.md`.

## Coverage

```bash
pytest --cov=report_generator --cov-report=term-missing
```

Currently ~70 % of statements. The report-generation core is well covered
(`charts`, `columns`, `pedal_histogram`, `paths`, `configs`, `energy_correction`,
`xlsx_patch_common` at 100 %; `capacity` 96 %, `excel_writer` 99 %,
`mass_aggregation` 99 %, `cli` 94 %, `speed_detection` 89 %, `mass_clustering`
87 %, `detection` 85 %, `row_builder` 84 %, `soc_detection` 80 %).

### Deliberate coverage gaps

These are **not** oversights; each is a network-bound path with no offline seam
worth faking:

| Area | Why it is left uncovered |
|------|--------------------------|
| `charger_patcher._fetch_charger_windows`, `logger_patcher._fetch_logger_data` | Pure SRF query construction + paging. `patch_file` is covered instead, by injecting the windows/legs those methods would return — which is the interesting half. |
| `weather_fetcher.WeatherFetcher.fetch_single` / `fetch_batch`, `fine_grained_patcher`, `weather_patch` | These exist to spend a paid OpenWeather quota. Mocking `requests` here would test the mock, not the fetcher. The pure helpers (`_parse_point`, `_deg_to_cardinal`, `_cell_needs_patch`, `_to_unix_utc`, `_is_ev_layout`, the whole `WeatherCache`) ARE covered, including the cache key format that protects the quota, and `WeatherPatcher.patch_file` is driven end to end over a fully cached stub (no fetcher at all) in `unit/test_weather_patcher_layout.py`. |
| `_generator._collect_legs` / `_preload_logger_channels` / `_process_fps_legs` / `_save_logger_data` | The SRF iteration half of the orchestrator. The transformation half it drives (`run_segment_detection` -> `_seg_to_row` -> `_insert_stop_rows` -> `_write_excel_report`) is covered end to end on real fixture data in `integration/test_excel_end_to_end.py`, and `generate_report` itself is exercised with a mocked SRF surface in `integration/test_runtime_fallback.py` and `integration/test_capacity_ledger_at_report_start.py`. |
| `data_fetcher.fetch_events` | Six lines of SRF filter construction. |

## Conventions

- English throughout: test names, comments, docstrings.
- One assertion subject per test; the test name states the behaviour, not the
  function.
- Expected values are hand-computed from the documented formula and the
  derivation is written in a comment — never copied back from a run of the code.
  (The golden files are the deliberate exception, and they are labelled as such.)
- Test module basenames are unique across the whole suite: there are no
  `__init__.py` files, so pytest imports each module by basename.
