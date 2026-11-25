"""Tests for backup.py failure detection and alerting.

Covers a child process exiting non-zero, an unexpected exception, and runs
that complete with per-file download errors, which are expected and must not
alert. No network, share, or Autodesk access is used.
"""

import sys

import pytest

import backup
from alerting import KIND_FAILURE

COPY_OUTPUT_WITH_ERRORS = """\
  [1/3] OK  \\\\srv\\share\\Hub\\P\\a.rvt
  [2/3] ERR \\\\srv\\share\\Hub\\P\\b.rvt: no signed URL
  [3/3] ERR \\\\srv\\share\\Hub\\P\\c.rvt: HTTPError: 403

============================================================
COPY SUMMARY
============================================================
  Written this run:     1
  Skipped (already OK): 0
  Errors:               2
  Total in copylist:    3
"""

SWEEP_OUTPUT_WITH_ERRORS = """\
  Hub: DLZ Corporation (b.123)
  CLEAN  Project A
  SKIP Project B: 403 Client Error
  DIRTY  Project C  (cached=None, live=2026-01-01)
    ERR walking Project Files/X: timeout
"""


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stub out the share, the alert webhook, and the run stamp."""
    calls: dict[str, list] = {"alerts": [], "stamps": []}
    monkeypatch.setattr(backup, "write_ca_bundle", lambda: None)
    monkeypatch.setattr(backup, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        backup, "send_alert",
        lambda kind, summary, details: calls["alerts"].append((kind, summary, details)))
    monkeypatch.setattr(
        backup, "write_last_run",
        lambda sweep_errors, file_errors: calls["stamps"].append((sweep_errors, file_errors)))
    monkeypatch.setattr(sys, "argv", ["backup.py"])
    return calls


def test_run_raises_with_output_when_child_fails() -> None:
    """A non-zero child exit raises BackupError carrying the child's output."""
    code = "import sys; print('boom detail'); sys.exit(3)"
    with pytest.raises(backup.BackupError) as excinfo:
        backup.run([sys.executable, "-u", "-c", code])
    assert "code 3" in str(excinfo.value)
    assert "boom detail" in excinfo.value.output


def test_count_copy_errors_reads_summary() -> None:
    assert backup.count_copy_errors(COPY_OUTPUT_WITH_ERRORS) == 2


def test_count_copy_errors_clean_output() -> None:
    assert backup.count_copy_errors("Copylist: 0 files\nNothing to copy.\n") == 0


def test_find_sweep_errors() -> None:
    lines = backup.find_sweep_errors(SWEEP_OUTPUT_WITH_ERRORS)
    assert lines == ["SKIP Project B: 403 Client Error",
                     "ERR walking Project Files/X: timeout"]


def test_parse_hub_name() -> None:
    assert backup.parse_hub_name(SWEEP_OUTPUT_WITH_ERRORS) == "DLZ Corporation"


def test_parse_hub_name_missing_raises() -> None:
    with pytest.raises(backup.BackupError):
        backup.parse_hub_name("no hub line here")


def test_main_alerts_and_does_not_stamp_when_step_fails(
        harness: dict[str, list], monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_run(cmd: list[str]) -> str:
        raise backup.BackupError("sweep.py exited with code 1",
                                 "Traceback...\nUnicodeEncodeError: boom")

    monkeypatch.setattr(backup, "run", failing_run)
    assert backup.main() == 1
    assert harness["stamps"] == []
    [(kind, summary, details)] = harness["alerts"]
    assert kind == KIND_FAILURE
    assert "sweep.py exited with code 1" in summary
    assert "UnicodeEncodeError: boom" in details


def test_main_alerts_on_unexpected_exception(
        harness: dict[str, list], monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_bundle() -> None:
        raise OSError("share unreachable")

    monkeypatch.setattr(backup, "write_ca_bundle", broken_bundle)
    assert backup.main() == 1
    assert harness["stamps"] == []
    [(kind, summary, details)] = harness["alerts"]
    assert kind == KIND_FAILURE
    assert "share unreachable" in summary
    assert "Traceback" in details


def test_main_completed_run_with_download_errors_is_success(
        harness: dict[str, list], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    outputs = iter([SWEEP_OUTPUT_WITH_ERRORS, COPY_OUTPUT_WITH_ERRORS])
    monkeypatch.setattr(backup, "run", lambda cmd: next(outputs))
    assert backup.main() == 0
    assert harness["stamps"] == [(2, 2)]
    assert harness["alerts"] == []
    assert "2 file(s) not copied" in capsys.readouterr().out


def test_main_clean_run_stamps_and_stays_quiet(
        harness: dict[str, list], monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(["  Hub: DLZ Corporation (b.123)\n  CLEAN  Project A\n",
                    "Copylist: 0 files with storage URNs\nNothing to copy.\n"])
    monkeypatch.setattr(backup, "run", lambda cmd: next(outputs))
    assert backup.main() == 0
    assert harness["stamps"] == [(0, 0)]
    assert harness["alerts"] == []


def test_main_dry_run_does_not_stamp(
        harness: dict[str, list], monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(["  Hub: DLZ Corporation (b.123)\n", "(dry-run)\n"])
    monkeypatch.setattr(backup, "run", lambda cmd: next(outputs))
    monkeypatch.setattr(sys, "argv", ["backup.py", "--dry-run"])
    assert backup.main() == 0
    assert harness["stamps"] == []
