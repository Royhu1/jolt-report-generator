"""CLI contract for the ``python -m jolt_toolkit.report_generator.cli`` entry point.

Exercised via subprocess (a real ``python -m jolt_toolkit.report_generator.cli``
invocation) so the __main__ / argparse / fail-fast path is tested end-to-end.
No network and no SRF key: the ``--help`` path exits before any client build, and
the missing-key path is the documented rc-2 fast-fail (checked before the SRF
client is constructed).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Absolute path to the worktree src, prepended on PYTHONPATH so the subprocess
# imports THIS tree (not any editable install pointing elsewhere).
SRC = str(Path(__file__).resolve().parents[1] / "src")
CLI_MODULE = "jolt_toolkit.report_generator.cli"


def _run(args, *, scrub_key, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if scrub_key:
        # Force the key EMPTY (not absent): the CLI's load_dotenv() walks up from
        # cli.py's own path and would otherwise re-inject a repo .env key. With the
        # var already present, load_dotenv(override=False) leaves it empty, and the
        # CLI treats empty as "not set" -> the documented rc-2 fast-fail.
        env["SRF_API_KEY"] = ""
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
    )


def test_help_exits_zero():
    proc = _run(["--help"], scrub_key=False)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert "usage" in out.lower()
    # The module-form prog string (no console script since v3.2.0).
    assert "python -m jolt_toolkit.report_generator.cli" in out


def test_missing_srf_api_key_exits_2():
    # Run from a temp dir on a different drive so cli's load_dotenv() cannot walk
    # up to the repo's .env, and scrub SRF_API_KEY from the environment.
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(
            ["-veh", "YK73WFN", "-ds", "2025-04-01", "-de", "2025-04-02"],
            scrub_key=True,
            cwd=tmp,
        )
    assert proc.returncode == 2, (
        f"expected rc 2 for missing SRF_API_KEY, got {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "SRF_API_KEY" in (proc.stdout + proc.stderr)
