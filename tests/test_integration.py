"""Runs tests/integration/scenario.py inside an isolated headless GNOME Shell.

Slow (~30 s) and needs gnome-shell, so it is marked `integration`:
    python3 -m pytest -m integration -o addopts=""
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run_in_headless_shell(script, marker):
    if not shutil.which("gnome-shell") or not shutil.which("dbus-run-session"):
        pytest.skip("gnome-shell / dbus-run-session not available")
    proc = subprocess.run(
        [str(ROOT / "tests/integration/headless_shell.sh"), "python3", str(ROOT / script)],
        capture_output=True, text=True, timeout=300)
    print(proc.stdout)
    # Lead with the scenario's own progress / failed check, not the noisy session log.
    relevant = [line for line in (proc.stdout + proc.stderr).splitlines()
                if line.startswith(("ok -", "FAIL -", "AssertionError", "Traceback", "  File"))
                or "JS ERROR" in line]
    assert proc.returncode == 0, "\n".join(relevant[-25:])
    assert marker in proc.stdout


@pytest.mark.integration
def test_app_and_extension_end_to_end():
    run_in_headless_shell("tests/integration/scenario.py", "ALL INTEGRATION CHECKS PASSED")


@pytest.mark.integration
def test_shortcut_editor_gui():
    run_in_headless_shell("tests/integration/gui_smoke.py", "ALL GUI CHECKS PASSED")
