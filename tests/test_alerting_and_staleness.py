"""Tests for the Jira webhook client, the run stamp, and the staleness check.

The webhook is exercised against a throwaway HTTP server on localhost so the
real request (method, headers, JSON body) is verified without touching Jira.
"""

import http.server
import json
import pathlib
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

import alerting
import check_staleness
import run_state


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records each POST on the server object and answers with its status."""

    def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received.append({
            "path": self.path,
            "token": self.headers.get(alerting.TOKEN_HEADER),
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(self.rfile.read(length)),
        })
        self.send_response(self.server.status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging."""


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch) -> Iterator[http.server.HTTPServer]:
    """Run a local webhook receiver and point the alert settings at it."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.received = []
    server.status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(alerting.WEBHOOK_URL_ENV,
                       f"http://127.0.0.1:{server.server_port}/hook")
    monkeypatch.setenv(alerting.WEBHOOK_TOKEN_ENV, "test-token")
    yield server
    server.shutdown()
    server.server_close()


def test_send_alert_posts_json_with_token_header(
        webhook: http.server.HTTPServer) -> None:
    assert alerting.send_alert(alerting.KIND_FAILURE, "it broke", "trace") is True
    [request] = webhook.received
    assert request["path"] == "/hook"
    assert request["token"] == "test-token"
    assert request["content_type"] == "application/json"
    assert request["body"]["kind"] == "failure"
    assert request["body"]["summary"] == "it broke"
    assert request["body"]["details"] == "trace"
    assert request["body"]["source"] == "acc-backup"


def test_send_alert_returns_false_on_http_error(
        webhook: http.server.HTTPServer) -> None:
    webhook.status = 401
    assert alerting.send_alert(alerting.KIND_FAILURE, "s", "d") is False


def test_send_alert_returns_false_when_not_configured(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(alerting.WEBHOOK_URL_ENV, raising=False)
    monkeypatch.delenv(alerting.WEBHOOK_TOKEN_ENV, raising=False)
    assert alerting.send_alert(alerting.KIND_FAILURE, "s", "d") is False


def test_send_alert_returns_false_when_unreachable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(alerting.WEBHOOK_URL_ENV, "http://127.0.0.1:9/hook")
    monkeypatch.setenv(alerting.WEBHOOK_TOKEN_ENV, "t")
    assert alerting.send_alert(alerting.KIND_FAILURE, "s", "d") is False


def test_build_payload_keeps_tail_of_long_details() -> None:
    details = "start" + "x" * (alerting.MAX_DETAIL_CHARS + 100) + "THE-ERROR"
    payload = alerting.build_payload(alerting.KIND_FAILURE, "s", details)
    assert payload["details"].endswith("THE-ERROR")
    assert "start" not in payload["details"]


def test_run_state_round_trip(tmp_path: pathlib.Path) -> None:
    state_file = tmp_path / "cache" / "last_run.json"
    assert run_state.read_last_completed(state_file) is None
    run_state.write_last_run(1, 2, state_file)
    completed = run_state.read_last_completed(state_file)
    assert completed is not None
    assert datetime.now(timezone.utc) - completed < timedelta(minutes=1)
    assert json.loads(state_file.read_text(encoding="utf-8"))["file_errors"] == 2


def test_run_state_corrupt_file_reads_as_unknown(tmp_path: pathlib.Path) -> None:
    state_file = tmp_path / "last_run.json"
    state_file.write_text("{not json", encoding="utf-8")
    assert run_state.read_last_completed(state_file) is None


def test_describe_staleness() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    assert check_staleness.describe_staleness(now - timedelta(hours=20), now, 36) is None
    stale = check_staleness.describe_staleness(now - timedelta(hours=40), now, 36)
    assert stale is not None and "40 hours" in stale
    missing = check_staleness.describe_staleness(None, now, 36)
    assert missing is not None and "no record" in missing


def test_check_staleness_main_alerts_when_stale(
        webhook: http.server.HTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_staleness, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(check_staleness, "read_last_completed", lambda: None)
    monkeypatch.setattr("sys.argv", ["check_staleness.py"])
    assert check_staleness.main() == 1
    [request] = webhook.received
    assert request["body"]["kind"] == "stale"


def test_check_staleness_main_quiet_when_fresh(
        webhook: http.server.HTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_staleness, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(check_staleness, "read_last_completed",
                        lambda: datetime.now(timezone.utc))
    monkeypatch.setattr("sys.argv", ["check_staleness.py"])
    assert check_staleness.main() == 0
    assert webhook.received == []
