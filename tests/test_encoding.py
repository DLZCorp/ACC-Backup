"""Tests that non-ANSI characters in ACC names survive piped output.

A project name containing U+2502 (box-drawing vertical bar) crashed the sweep
when its stdout was a pipe, because Windows encodes piped output with the ANSI
code page. These tests run real child processes through real pipes.
"""

import os
import pathlib
import subprocess
import sys

import backup

ROOT = pathlib.Path(__file__).parent.parent
BOX_BAR = "│"
PRINT_BOX_BAR = f"print('1234-5678-90 {BOX_BAR} Sample Project')"


def _env_without_encoding_overrides() -> dict[str, str]:
    """Return the environment with Python's encoding overrides removed."""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}


def test_run_passes_non_ansi_child_output_through() -> None:
    """backup.run() launches children that can print any character."""
    output = backup.run([sys.executable, "-u", "-c", PRINT_BOX_BAR])
    assert BOX_BAR in output


def test_use_utf8_stdout_makes_piped_print_safe() -> None:
    """A script that calls use_utf8_stdout() prints safely with no env help."""
    code = f"from console import use_utf8_stdout; use_utf8_stdout(); {PRINT_BOX_BAR}"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, env=_env_without_encoding_overrides())
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert BOX_BAR in proc.stdout.decode("utf-8")


def test_sweep_switches_stdout_to_utf8_before_printing() -> None:
    """sweep.main() calls use_utf8_stdout() before its first print()."""
    source = (ROOT / "sweep.py").read_text(encoding="utf-8")
    main_body = source[source.index("def main() -> None:"):]
    assert "use_utf8_stdout()" in main_body
    assert main_body.index("use_utf8_stdout()") < main_body.index("print(")
