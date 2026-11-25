#!/usr/bin/env python3
"""
Back up an entire hub to a mounted SMB share.  Runs sweep.py to detect changed
files, then copy_to_smb.py to download them with the hub name as the root
sub-directory on the share.  A run that fails to complete raises a Jira ticket
through alerting.py; every completed run is stamped for check_staleness.py.
Individual files that fail to download are expected, are retried on the next
run, and do not count as a failure.

Destination layout:  {SMB_DEST}/{hub_name}/{project_name}/{path}

Usage:
    python backup.py                                     # default hub
    python backup.py --hub "Developer Advocacy Support"   # specific hub
    python backup.py --dry-run                            # sweep + dry-run
    python backup.py --preview                            # sweep + 16 KB preview
    python backup.py --skip-sweep                         # reuse existing copylist.pq
    python backup.py --dest mount_path                    # override SMB location
    python backup.py --workers 8                          # override worker count

Exit codes: 0 completed, 1 failed to complete.
"""

import argparse
import base64
import os
import pathlib
import re
import ssl
import subprocess
import sys
import traceback
from dataclasses import dataclass

from dotenv import load_dotenv

from alerting import KIND_FAILURE, send_alert
from console import use_utf8_stdout
from run_state import write_last_run

bundlePath = r'\\azure-pan2\Revit\ACC-Backup\certs\Zscaler.pem'

HERE = pathlib.Path(__file__).parent
DEFAULT_HUB = "DLZ Corporation"

# Lines of child output attached to an alert.
_ALERT_TAIL_LINES = 60


class BackupError(Exception):
    """A backup step failed.

    Attributes:
        output: Captured output of the failing step, if any.
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


@dataclass
class RunResult:
    """Outcome of a run that got through both phases.

    Attributes:
        sweep_errors: Sweep lines reporting a project or folder it could not read.
        file_errors: Number of files the copy step failed to write.
    """

    sweep_errors: int = 0
    file_errors: int = 0


def write_ca_bundle() -> None:
    """Export the OS trust store to a PEM bundle and point requests at it.

    The proxy's TLS-inspection root is trusted by Windows but not by the CA
    list bundled with requests, so the child processes need this bundle to
    reach the Autodesk APIs.
    """
    ctx = ssl.create_default_context()
    certs = ctx.get_ca_certs(binary_form=True)
    out = pathlib.Path(bundlePath)
    with out.open('wb') as f:
        for cert in certs:
            f.write(b'-----BEGIN CERTIFICATE-----\n')
            f.write(base64.encodebytes(cert))
            f.write(b'-----END CERTIFICATE-----\n')
    os.environ['REQUESTS_CA_BUNDLE'] = bundlePath


def run(cmd: list[str]) -> str:
    """Run a command, streaming output live. Returns stdout as a string.

    The child Python process is launched with `-u` so its stdout/stderr are
    unbuffered — otherwise PIPE-redirected output sits in a 4-8 KB buffer
    and the parent sees long silences while workers download.

    Both ends of the pipe are pinned to UTF-8: the child through
    PYTHONIOENCODING and the parent through encoding='utf-8'. Left to the
    defaults, each side uses the OS locale (cp1252 on US Windows), which
    cannot represent characters that appear in ACC project and file names
    and crashes the child on the first such print().

    Args:
        cmd: The command line to execute.

    Returns:
        Everything the command wrote to stdout and stderr.

    Raises:
        BackupError: The command exited with a non-zero code.
    """
    print(f"\n{'─' * 60}")
    print(f"▶ {' '.join(cmd)}")
    print(f"{'─' * 60}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8:replace"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=HERE, bufsize=1, env=env,
                            encoding="utf-8", errors="replace")
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    proc.wait()
    output = "".join(lines)
    if proc.returncode != 0:
        print(f"\nERROR: command exited with code {proc.returncode}")
        step = pathlib.Path(cmd[2]).name if len(cmd) > 2 else cmd[0]
        raise BackupError(f"{step} exited with code {proc.returncode}", output)
    return output


def parse_hub_name(sweep_output: str) -> str:
    """Extract hub display name from sweep.py stdout.

    sweep.py prints:  '  Hub: Developer Advocacy Support (b.xxx)'

    Raises:
        BackupError: The sweep output has no hub line.
    """
    # "Hub:" then the name (lazy), up to the " (" that opens the hub ID.
    m = re.search(r"Hub:\s+(.+?)\s+\(", sweep_output)
    if not m:
        raise BackupError("could not parse hub name from sweep output",
                          sweep_output)
    return m.group(1).strip()


def find_sweep_errors(sweep_output: str) -> list[str]:
    """Return the sweep lines that report something it could not read.

    sweep.py keeps going when a project or folder request fails and prints
    '  SKIP <project>: <error>' or '    ERR <action> <path>: <error>'. The
    count is recorded with the run stamp.
    """
    return [line.strip() for line in sweep_output.splitlines()
            if line.strip().startswith(("SKIP ", "ERR "))]


def count_copy_errors(copy_output: str) -> int:
    """Return how many files the copy step reported as failed.

    copy_to_smb.py prints '  Errors:  N' in its summary. Failed files are
    written to failures.pq and retried on the next run.
    """
    for line in copy_output.splitlines():
        label, _, value = line.strip().partition(":")
        if label == "Errors" and value.strip().isdigit():
            return int(value.strip())
    return 0


def run_backup(args: argparse.Namespace) -> RunResult:
    """Run the sweep and copy phases.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Counts of the non-fatal errors seen in each phase.

    Raises:
        BackupError: A phase exited non-zero or produced unusable output.
    """
    write_ca_bundle()
    result = RunResult()

    # ── Future: Cleanup pass ─────────────────────────────────────────────
    # TODO: Read cache/removals.pq here (produced by webhook_listener.py),
    # delete stale SMB paths, remove entries from cache.pq, then
    # truncate removals.pq.  See README "Future Work" section.

    # ── Phase 1: Sweep ───────────────────────────────────────────────────
    if args.skip_sweep:
        print("Skipping sweep (--skip-sweep), reusing existing copylist.pq")
        from auth import load_token
        from sweep import find_hub
        load_token()  # ensure token is ready
        _, hub_name = find_hub(args.hub)
        print(f"  Hub: {hub_name}")
    else:
        sweep_cmd = [sys.executable, "-u", str(HERE / "sweep.py"), "--hub", args.hub]
        sweep_output = run(sweep_cmd)
        hub_name = parse_hub_name(sweep_output)
        result.sweep_errors = len(find_sweep_errors(sweep_output))

    # ── Phase 2: Copy to SMB ─────────────────────────────────────────────
    copy_cmd = [sys.executable, "-u", str(HERE / "copy_to_smb.py"),
                "--prefix", hub_name]
    if args.dest:
        copy_cmd += ["--dest", args.dest]
    if args.dry_run:
        copy_cmd.append("--dry-run")
    if args.preview:
        copy_cmd.append("--preview")
    if args.workers is not None:
        copy_cmd += ["--workers", str(args.workers)]

    result.file_errors = count_copy_errors(run(copy_cmd))
    return result


def tail(text: str, max_lines: int = _ALERT_TAIL_LINES) -> str:
    """Return the last max_lines lines of text."""
    return "\n".join(text.splitlines()[-max_lines:])


def main() -> int:
    """Parse arguments, run the backup, and alert if it fails to complete.

    Returns:
        The process exit code: 0 completed, 1 failed to complete.
    """
    use_utf8_stdout()
    load_dotenv(HERE / ".env")

    parser = argparse.ArgumentParser(description="Back up a hub to SMB")
    parser.add_argument("--hub", default=DEFAULT_HUB,
                        help="Hub name or ID (default: %(default)s)")
    parser.add_argument("--dest", default="",
                        help="Destination SMB Share (default: SMB_DEST from .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be uploaded, no downloads/uploads")
    parser.add_argument("--preview", action="store_true",
                        help="Upload only first 16 KB of each file")
    parser.add_argument("--skip-sweep", action="store_true",
                        help="Skip sweep, reuse existing copylist.pq")
    parser.add_argument("--workers", type=int, default=None,
                        help="Override copy_to_smb worker count")
    args = parser.parse_args()

    try:
        result = run_backup(args)
    except BackupError as e:
        print(f"\nERROR: {e}")
        send_alert(KIND_FAILURE, f"ACC backup failed: {e}",
                   f"Command: {' '.join(sys.argv)}\n\n"
                   f"Last output:\n{tail(e.output)}")
        return 1
    except (Exception, SystemExit) as e:
        trace = traceback.format_exc()
        print(f"\nERROR: {trace}")
        send_alert(KIND_FAILURE,
                   f"ACC backup failed: {type(e).__name__}: {e}",
                   f"Command: {' '.join(sys.argv)}\n\n{trace}")
        return 1

    # Only a real, full run counts as a completed backup.
    if not (args.dry_run or args.preview):
        write_last_run(result.sweep_errors, result.file_errors)

    print(f"\nBackup completed: {result.file_errors} file(s) not copied, "
          f"{result.sweep_errors} project/folder read error(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
