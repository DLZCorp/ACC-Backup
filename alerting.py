"""Raise a Jira ticket when the backup fails or stops running.

Posts a small JSON document to a Jira Automation "Incoming webhook" rule, which
creates (or comments on) the ticket. The webhook URL and its secret come from
the environment, so the only credential on the backup server is one that can
trigger that single rule. Uses the standard library HTTP client with the
operating system certificate store, so it keeps working when the SMB share or
the generated CA bundle is unavailable.
"""

import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

WEBHOOK_URL_ENV = "JIRA_ALERT_WEBHOOK_URL"
WEBHOOK_TOKEN_ENV = "JIRA_ALERT_WEBHOOK_TOKEN"
TOKEN_HEADER = "X-Automation-Webhook-Token"

KIND_FAILURE = "failure"
KIND_STALE = "stale"

# Jira rejects very large field values; keep the tail, where the error is.
MAX_DETAIL_CHARS = 20_000
_TIMEOUT_SECONDS = 30


def build_payload(kind: str, summary: str, details: str) -> dict[str, str]:
    """Build the JSON body sent to the Jira Automation rule.

    Args:
        kind: KIND_FAILURE or KIND_STALE.
        summary: One-line description, used as the ticket summary.
        details: Supporting text such as the tail of the run output.

    Returns:
        A flat dict of strings the rule reads as webhook data.
    """
    if len(details) > MAX_DETAIL_CHARS:
        details = "[earlier output truncated]\n" + details[-MAX_DETAIL_CHARS:]
    return {
        "source": "acc-backup",
        "kind": kind,
        "summary": summary,
        "details": details,
        "host": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def send_alert(kind: str, summary: str, details: str) -> bool:
    """Send one alert to the Jira Automation webhook.

    Never raises: an alerting problem must not hide the failure being
    reported, so every error is printed and reported through the return value.

    Args:
        kind: KIND_FAILURE or KIND_STALE.
        summary: One-line description, used as the ticket summary.
        details: Supporting text such as the tail of the run output.

    Returns:
        True if Jira accepted the request, False if alerting is not
        configured or the request failed.
    """
    url = os.environ.get(WEBHOOK_URL_ENV, "").strip()
    token = os.environ.get(WEBHOOK_TOKEN_ENV, "").strip()
    if not url or not token:
        print(f"ALERT NOT SENT: set {WEBHOOK_URL_ENV} and {WEBHOOK_TOKEN_ENV} "
              f"in .env to enable Jira alerts")
        return False

    body = json.dumps(build_payload(kind, summary, details)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS,
                                    context=ssl.create_default_context()) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        print(f"ALERT NOT SENT: Jira webhook returned HTTP {e.code}")
        return False
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"ALERT NOT SENT: {type(e).__name__}: {e}")
        return False

    print(f"Alert sent to Jira ({kind}, HTTP {status}): {summary}")
    return True
