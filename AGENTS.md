# Agent instructions

**Role: core repo** — the JOLT Excel report generator (`report_generator`), its offline
test suite and its documentation. It is consumed by the JOLT workspace (`../JOLT_Report`),
which holds the report database, the deliverables and the research; never add experiment,
deliverable or paper material here. Every change — code, a new vehicle, a re-tune — is a
feature branch (ideally in a worktree `../jolt-report-generator.wt-<desc>`) merged into
`main` through a reviewed pull request; the workspace generates canonical data only from a
clean `main` or a tag.

## Project-specific rules ／项目专属规则

See `.claude/rules/project.md` (runtime, public API, change and release rules, what a
configuration PR may touch, abbreviation register). The generated global conventions follow.

<!-- BEGIN GLOBAL RULES (generated) -->
<!-- GENERATED FILE - DO NOT EDIT.
     Source of truth: ~/.claude/rules/{repo-roles,naming,code-style,git-workflow,housekeeping}.md @ 908d3b4 (2026-09-23)
     To change these rules: edit the source files, then run /sync-agent-rules in Claude Code
     (or: powershell -File ~/.claude/rules/tools/build-agents-md.ps1). -->
# Repository Roles (Global) ／仓库角色与职责划分

> Personal defaults for ALL projects. Project rules override.
> Pick one role per repository and state it in the first line of its README. ／每个仓库只承担一种角色，README 首行写明。
> Roles bind NEW repositories; existing ones are not relabelled retroactively.

## 0. Three roles ／三种角色

The deciding question is **what the repository holds**, never what it is about ／判定看仓库装什么，不看主题：

- **standalone** — **one shippable artefact**: a tool, an app, a one-off project. Runnable or deployable as it
  stands. **Most repositories are this** ／多数仓库属于此类: utilities, web/video apps, 业余 projects.
- **core repo** — **one reusable algorithm or library** plus its tests and small demos. Shareable, SemVer.
- **workspace** — **where work is carried out**: it accumulates working material (experiments, drafts, notes,
  figures), commits only the code and prose that generate that material, carries no version number, and routes
  every path through `paths.py` (§3) ／干活的地方：产物不入库，路径统一路由.

Meeting and conference material is **not** a repository — it lives in `MEETING-CONFERENCE/<conf>-YYYY-MM`
(naming.md §1). A manuscript *project* is different: it is a workspace (§1).

## 1. Workspace kinds ／工作区的形态

A workspace is defined by its internal shape, **not** by what it consumes; a core repo behind it is optional
／核心仓库是可选的，不是工作区的前提. All kinds share the unified route (§3), the per-unit zones, and the rule
that **one unit never reads another unit's outputs** ／单元之间不互相读取产物.

| Kind ／形态 | A unit of work is | Typical zones | Core repo |
|---|---|---|---|
| **experiment** ／实验 | one experiment (notebooks) | `data results figures summary` | usually |
| **writing** ／写作 | one manuscript, report or review | `outline reading manuscript figures` | rarely |
| **production** ／生产 | one deliverable (a video, a talk) | as the medium needs | no |

- Units may be **parallel** (several experiments, several videos) or a **single subject** worked through in
  phases (one manuscript). Parallel units take their zones from `experiment_paths()`; a single-subject
  workspace uses the root zones directly. The helper's name is historical: a "unit" is an experiment, a
  manuscript or a deliverable.
- **Only the experiment kind normally pairs with a core repo.** When it does, the workspace is named
  `<core-repo-name>-workspace` and sits **next to** it (§2); otherwise its name stands alone.
- A **writing workspace** has no `src/` discipline to enforce. It commits prose, outlines and the scripts that
  produce its figures; the literature cache, compiled PDFs and bibliography exports stay out of git. Every
  figure must be reproducible from a committed script (naming.md §3), deliverables are versioned as files
  (`YYYYMMDD_<type>_vX.Y.ext`), and a reviewer-response cycle lives with the manuscript it answers.

## 2. The pair ／一对仓库 (experiment workspace + core repo)

| | `<name>` — **core repo** ／主开发仓库 | `<name>-workspace` — **workspace** ／实验工作区 |
|---|---|---|
| Holds | one algorithm / library + its unit tests + small demos | experiments, large-scale studies, figures for talks and papers |
| Audience | shareable, often public | private, my own research work |
| Medium | modules with docstrings and tests | notebooks |
| Tracked in git | source + documentation only | experiment code + documentation only |
| Versioned | SemVer in ONE place, tagged `vX.Y.Z` | none — git log + `changelogs/` are the record |
| Commit types | `feat:` `fix:` `refactor:` `docs:` `test:` | mostly `exp:` |
| Skeleton | `<pkg>/ demo/ tests/ tmp/` | `paths.py <unit>/ data/ changelogs/ archive/ tmp/` |

- **Not packaged by default** ／默认不打包: personal research code is *imported*, not `pip install`-ed. Keep the
  package directory at the repository root (flat layout), version it in `<pkg>/version.py`, and add
  `[build-system]`/`[project]` tables only when someone outside actually needs `pip install`. `pyproject.toml`
  otherwise holds tool config only.

## 3. Division of responsibility ／职责划分 (when a workspace has a core repo)

- An algorithm change belongs in the **core repo**, on a feature branch, with a test. **Never copy core code into
  a workspace** ／绝不把核心代码复制进工作区.
- **Rule of three**: logic used by 3+ units and already stable → promote it into the core package (SemVer minor
  bump), do not keep copying it. Without a core repo, promote it into the workspace's own `_shared/` instead.
- Model parameters and presets that describe the *system* live in the core repo and are read through its public
  loaders — never by file path. Work-specific configuration stays in the workspace.
- The core repo's own data zone holds only small fixtures for its tests and demos; bulk measured data belongs to
  the workspace.

## 4. The unified route ／统一路由 (every workspace)

- **Exactly one module — `paths.py` at the workspace root — knows where everything is.** A hard-coded absolute
  path anywhere else is a defect. Moving a repository must be a one-line change. ／别处出现绝对路径即为缺陷。
- With a core repo, it resolves it from the env var `<NAME>_ROOT`, else the sibling `../<name>`; `bootstrap()`
  prepends it to `sys.path`. **Without one, `bootstrap()` only loads the API keys** (`.env` in the workspace →
  core repo if any → a shared key folder) and returns `None`.
- Per-unit zones come from a helper (`experiment_paths(<unit>, <key>)` → `data` / `results` / `figures` /
  `summary`), all gitignored and created on demand.
- Each unit directory is a **self-contained reproducible archive**: standard library + pip packages + the core
  package if any + its own files; it never reads another unit's outputs.
- The only path boilerplate allowed anywhere, at any depth:

  ```python
  import sys
  from pathlib import Path

  _cwd = Path.cwd()
  WORKSPACE_ROOT = next(p for p in [_cwd, *_cwd.parents] if (p / "paths.py").exists())
  sys.path.insert(0, str(WORKSPACE_ROOT))

  from paths import bootstrap, experiment_paths      # noqa: E402
  bootstrap()
  ```

# Naming Conventions (Global) ／全局命名规范

> Personal defaults for ALL projects. Project-level rules override this file.
> Apply to NEW artefacts only — never mass-rename existing files (grandfathering).
> Outside a code repository, only §1, §5, §6 apply.
> ／项目级规则优先；只约束新建物，存量不追溯改名；非仓库目录仅 §1/§5/§6 生效。

## 1. Project & top-level folders ／项目与顶层文件夹

- New project folders: **kebab-case** — `duty-cycle-predictor`, `srf-api-ai-agent`.
- Meeting/conference folders: `<会议名>-YYYY-MM` (zero-padded month) — `ITSC-2026-09`.
- Chinese folder names ONLY for admin/inbox buckets outside repos (`例会`, `未归档`) — never for code repos.
- Git worktree variants: `<repo>.wt-<desc>` suffix.

## 2. Repository skeleton ／仓库骨架

- In-repo subfolders: lowercase **snake_case** — `extended_eval`, `trip_level_metrics`.
- Standard zones: `src/ data/ doc/ tests/ tmp/ archive/ changelogs/` (cleanup policy → housekeeping.md).
- Grandfathering: existing non-conforming paths stay; renames are deliberate, separate commits.

## 3. Files by type ／按类型的文件命名

- Scripts: verb-prefix `generate_* / analyse_* / calibrate_* / extract_* / build_*` — **British spelling** (`analyse`, not `analyze`).
- One-off scripts: `_tmp_*.py`, live in `tmp/`, gitignored, deleted after use.
- Notebooks: snake_case, prefixed by model/task — `calibrate_engine_efficiency.ipynb`.
- Data files: compact `YYYYMMDD` in file/dir names; period = `YYYYMMDD_YYYYMMDD`.
- Figures: `<model>_<what>.png`, reproducible from a committed script (no plot-and-delete); slide assets `fig_<content>.png`.
- Deliverables (slides/poster/speech): `YYYYMMDD_<type>_[<desc>_]vX.Y.ext` — `20260812_slides_v1.1.pptx`; type ∈ slides/poster/speech/program/agenda/CFP/notes.
- Docs: `README.md` is the authoritative English version; `*.zh.md` is the gitignored Chinese reading copy.
- Skills / agents / commands: **kebab-case** — `sync-agent-rules`.

## 4. Identifiers in code ／代码内标识符

→ see `code-style.md` (PEP 8 table, unit suffixes, abbreviation register policy, TS rules).

## 5. Language rules ／中英文使用

- Everything committed is **English**: code, comments, docstrings, README, commit messages, changelogs.
- Chinese ONLY for: interactive chat, gitignored notes (`*.zh.md`, local working docs), admin folders outside repos.
- British English spelling throughout.

## 6. Dates & versions ／日期与版本

- ISO 8601 everywhere: `YYYY-MM-DD` in prose/data fields; compact `YYYYMMDD` inside file/dir names (sorts chronologically).
- Code releases: SemVer tags `vX.Y.Z` (→ git-workflow.md). Deliverable files: `vX.Y`; `vFinal` allowed for the delivered copy.

# Code Style (Global) ／全局代码风格

> Personal defaults for ALL projects. Project rules override.
> Each project's own `code-style.md` holds its abbreviation register, environment names and version ceilings.

## Python — PEP 8

| Object | Style | Example |
|---|---|---|
| Package / module file | `snake_case` | `data_fetcher.py`, `models.py` |
| Function / method / variable | `snake_case` | `calculate_wheel_power` |
| Class | `PascalCase` | `ElevationCache` |
| Constant / module-level config | `UPPER_SNAKE_CASE` | `AIR_DENSITY`, `BASE_DIR` |
| Internal / private | leading underscore | `_safe_num()` |
| Test files | `test_<module>.py` | `test_models.py` |

- **Quantities carrying physical units state the unit in the name** — `mass_kg`, `velocity_mps`, `gradient_degrees`, `wheel_power_kW`, `fuel_rate_L_hr`. The unit keeps its own casing. Never drop the suffix. ／物理量标识符必须带单位后缀，单位保留原大小写。
- Identifiers always in English; abbreviations must be **registered before use** in the project's own `code-style.md` (e.g. `lvd` = longitudinal vehicle dynamics). No unregistered abbreviations. ／缩写先在项目规则里登记后再使用。
- Prefer `pathlib.Path`; f-strings for formatting.
- Secrets from `.env` via `python-dotenv` — never hard-code keys, never echo their values.
- Notebooks: reusable logic promoted into `src/` and imported (`%autoreload 2`); clear bulky cell outputs before committing.
- Legacy Chinese comments/docstrings: migrate to English opportunistically when a function is touched — do not mass-rewrite.

## TypeScript / React

- Components: `PascalCase.tsx`; entry/config lowercase (`index.ts`, `remotion.config.ts`).
- Functions / variables: `camelCase`; types / interfaces / components: `PascalCase`.

# Git Workflow (Global) ／全局 Git 约定

> Personal defaults for ALL projects. Project rules override (e.g. release-line specifics).

## Commit messages — Conventional Commits

- `feat:` / `fix:` / `refactor:` / `docs:` / `chore:` / `test:` / `exp:` (notebook experiment iteration).
- Meaningless messages ("update", "wip", "checkpoint") are forbidden. Written in English.

## Branches

- `main` is the stable mainline — do not develop directly on main.
- Feature branches: `feat/<description>` / `fix/<description>` / `exp/<description>`.

## Versioning

- Projects with a release line: **SemVer** tags `vX.Y.Z`; the version lives in ONE source of truth (e.g. `pyproject.toml`). Notebook-centric repos have no version numbers — git log + changelog is the record.

## Pushing requires user consent (mandatory) ／push 必须先征得同意

- **Every `git push` must first obtain explicit consent — never push autonomously.** Consent is per-push, not standing authorisation. Commit / branch / local merge may proceed as usual.

## Changelog

- Repos with a `changelogs/` zone: weekly file `changelog_YYYYMMDD_YYYYMMDD.md` (Mon–Sun), English, Q&A format; append to the current week's file.

## Not committed

- `.env`, caches, `data/`, `results/`, `tmp/`, `archive/`, `*.zh.md` — gitignored by default (seeded by the project template).

# Housekeeping (Global) ／全局整理约定

> Personal defaults for ALL projects. Project rules override (e.g. cache cost warnings).

## Zones ／分区

| Zone | Purpose | Cleanup |
|---|---|---|
| `tmp/` | one-off scratch: logs, intermediate CSVs, debug figures, `_tmp_*.py` | clean after use |
| `archive/` | recycle bin for superseded artefacts | **in only, never out**; root scratch → `archive/root_scratch_<YYYYMMDD>/` |
| `cache/` | expensive-to-rebuild API caches | do not clean lightly — check the project's rules for cost warnings |

- **Keep the repository root clean** — no stray scripts, logs or screenshots at root.
- **Prefer archiving over deletion** ／宁归档不删除: move into `archive/` to preserve traceability; only delete genuinely reproducible waste.

## OneDrive specifics ／OneDrive 注意事项

- Repos under OneDrive hold code, docs and small figures only. Large rebuildable artefacts go to a machine-local root `D:\<project>_local\` (not in git, not synced) — never scattered in `D:\tmp`.
- Open Office files (`~$*.pptx` lock files) break moves/renames — close them first.
- Avoid symlinks inside OneDrive-synced trees.
<!-- END GLOBAL RULES (generated) -->
