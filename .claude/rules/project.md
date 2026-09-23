> Project-specific deltas for this **core repo** — the global conventions in `AGENTS.md`
> apply; only additions and overrides go here.

## Runtime

- Python 3.11 (conda env `jolt` on the maintainer's machine); `pip install -r requirements.txt`
  (+ `requirements-dev.txt` for the tests).
- No installation step: `report_generator` sits at the repository root and is imported
  directly (`PYTHONPATH=.`, or the consumer's own route — the JOLT workspace uses its
  `paths.py`). `pip install .` fails by design.
- Tests: `python -m pytest -q` — fully offline (sockets blocked), ~1000 tests; CI runs the
  same on every push and pull request.

## Public API

- CLI: `python -m report_generator.cli -veh <REG> -ds <start> -de <end> [--debug] [--fast] [--out-dir DIR]`;
  module CLIs `report_generator.weather_patch`, `report_generator.charger_patcher`.
- Python: `report_generator.JOLTReportGenerator`, `report_generator.generate_report()`,
  `report_generator.__version__`, `report_generator.DATA_NAMESPACE`.
- Configuration: `report_generator.configs.load_vehicle_configs()` /
  `load_pipeline_configs()` / `get_config_path()` / `get_capacity_ledger_path()` /
  `apply_capacity_ledger()` / `effective_vehicle_config()` (a vehicle's date-effective
  settings for a leg, `doc/architecture.md`); state via `JOLT_CAPACITY_LEDGER`, caches
  via `JOLT_CACHE_DIR`, config override via `JOLT_CONFIG_DIR` (`doc/deployment.md`).
- Names the JOLT workspace's skills import (keep them stable, or change them together with
  the workspace): `report_generator._generator.JOLTReportGenerator` and its capacity helpers,
  `report_generator.segmentation.{constants,detection,mass_aggregation,mass_clustering,timeutil}`,
  `report_generator.diesel_pipeline`, `report_generator.report_builder.{HEADERS,DIESEL_HEADERS}`,
  `report_generator.ep_confidence.{audit_key,regrade_rows}`,
  `report_generator.weather_patcher.WeatherPatcher`, `report_generator.paths.default_report_root`,
  and the `figure_hook` keyword of `run_segment_detection`.

## Change and release rules

- **Branch + PR only.** Never commit on `main`. Pushing, opening a PR, commenting on it and
  pushing tags each need the maintainer's explicit consent.
- **Code changes** come with a test. A change that can move a reported cell of any vehicle is
  a behaviour-changing release: bump `__version__`, advance `DATA_NAMESPACE`, append the
  `doc/versions.md` section with the expected change. An output-identical release keeps
  `DATA_NAMESPACE` and cites its 0-cell-difference evidence in `doc/versions.md`.
- **Configuration PRs** (new vehicle / re-tune, produced by the workspace's onboarding run
  tool) may touch only `report_generator/configs/vehicles.json` (the run's registration),
  `report_generator/configs/pipelines.json` (new entries, or an entry no other vehicle uses)
  and `tests/` (the vehicle's anonymised fixture — `tests/fixtures/make_fixture.py` — its
  frozen config entry and golden). No version bump for a config-only onboarding.
- **Fixture configs are frozen**: `tests/fixtures/configs/` is never synced with the live
  configs (`tests/fixtures/README.md`).
- Docs describe the present only; history goes to `doc/versions.md` (append-forward). No
  references to the workspace's internal paths, skills or agents in the package code,
  `README.md` or `doc/` — that is the deployable surface. (These agent-instruction files
  may name the workspace: they tell AI tools how the two repositories fit together.)

## Abbreviation register ／缩写登记表

| Abbr | Meaning |
|---|---|
| `ep` | energy performance (kWh/km) |
| `soc` | state of charge (%) |
| `crr` / `cda` | rolling-resistance coefficient / drag area |
| `gcw` / `cvw` | gross combination weight (telematics) / combination vehicle weight (Logger J1939 CVW) |
| `lfc` / `lfe` / `vdhr` / `ccvs` | J1939 fuel-consumption / fuel-economy / high-resolution distance / cruise-control-speed messages |
| `srf` | Centre for Sustainable Road Freight data platform |
| `fps` | the OEM telematics (fleet-management) feed on SRF |
| `ledger` | the effective-capacity ledger (`effective_capacity_kwh` + `effective_capacity_quarterly`) |
