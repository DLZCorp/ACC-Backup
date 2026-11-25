#!/usr/bin/env python3
"""Alert when the backup has not completed recently.

Runs as its own scheduled task, independent of backup.py, and raises a Jira
ticket if the last completed run is older than the allowed age. This covers
the failures backup.py cannot report itself: the task being disabled, the run
hanging or being killed by the scheduler, or Python failing to start.

Usage:
    python check_staleness.py                     # alert if older than 26 h
    python check_staleness.py --max-age-hours 48  # custom threshold

Exit codes: 0 fresh, 1 stale and alert sent, 2 stale and alert not sent.
"""

import argparse
import pathlib
from datetime import datetime, timezone

from dotenv import load_dotenv

from alerting import KIND_STALE, send_alert
from console import use_utf8_stdout
from run_state import STATE_FILE, read_last_completed

HERE = pathlib.Path(__file__).parent
DEFAULT_MAX_AGE_HOURS = 26.0


def describe_staleness(last_completed: datetime | None, now: datetime,
                       max_age_hours: float) -> str | None:
    """Decide whether the backup is stale.

    Args:
        last_completed: When the backup last completed, or None if unknown.
        now: The current time (timezone-aware).
        max_age_hours: Oldest acceptable completed run, in hours.

    Returns:
        A one-line description of the problem, or None if the backup is fresh.
    """
    if last_completed is None:
        return "ACC backup has no record of a completed run"
    age_hours = (now - last_completed).total_seconds() / 3600
    if age_hours <= max_age_hours:
        return None
    return (f"ACC backup has not completed in {age_hours:.0f} hours "
            f"(last: {last_completed.isoformat(timespec='seconds')})")


def main() -> int:
    """Check the last-run stamp and alert if it is too old.

    Returns:
        The process exit code described in the module docstring.
    """
    use_utf8_stdout()
    load_dotenv(HERE / ".env")

    parser = argparse.ArgumentParser(
        description="Alert if the ACC backup has not completed recently")
    parser.add_argument("--max-age-hours", type=float,
                        default=DEFAULT_MAX_AGE_HOURS,
                        help="Oldest acceptable completed run (default: %(default)s)")
    args = parser.parse_args()

    problem = describe_staleness(read_last_completed(),
                                 datetime.now(timezone.utc), args.max_age_hours)
    if problem is None:
        print("OK: backup completed within the allowed window")
        return 0

    print(f"STALE: {problem}")
    details = (f"{problem}.\n\n"
               f"Threshold: {args.max_age_hours:g} hours.\n"
               f"Stamp file: {STATE_FILE}\n\n"
               f"Check the 'ACC Sync' scheduled task history and run "
               f"backup.py by hand to see the error.")
    return 1 if send_alert(KIND_STALE, problem, details) else 2


if __name__ == "__main__":
    raise SystemExit(main())
