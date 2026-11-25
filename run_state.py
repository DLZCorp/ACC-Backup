"""Record and read when the backup last ran to completion.

backup.py writes a small JSON stamp after every full run that finishes both
the sweep and the copy. check_staleness.py reads it to detect the case an
in-process alert cannot: the backup not running, hanging, or being killed.
"""

import json
import pathlib
from datetime import datetime, timezone

STATE_FILE = pathlib.Path(__file__).parent / "cache" / "last_run.json"


def write_last_run(sweep_errors: int, file_errors: int,
                   state_file: pathlib.Path = STATE_FILE) -> None:
    """Stamp the current time as the last completed run.

    Args:
        sweep_errors: Projects or folders the sweep could not read.
        file_errors: Files the copy step failed to write.
        state_file: Where to write the stamp.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sweep_errors": sweep_errors,
        "file_errors": file_errors,
    }
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_last_completed(state_file: pathlib.Path = STATE_FILE) -> datetime | None:
    """Return when the backup last completed, or None if that is unknown.

    A missing, unreadable, or malformed stamp is treated the same as no
    completed run, so a corrupted file surfaces as a staleness alert.

    Args:
        state_file: Where the stamp is stored.

    Returns:
        A timezone-aware UTC datetime, or None.
    """
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        completed = datetime.fromisoformat(state["completed_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return completed
