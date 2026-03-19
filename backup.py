#!/usr/bin/env python3
"""
Back up an entire hub to S3.  Runs sweep.py to detect changed files,
then copy_to_s3.py to upload them with the hub name at the S3 root.

S3 key structure:  {hub_name}/{project_name}/{path}

Usage:
    python backup.py                                     # default hub
    python backup.py --hub "Developer Advocacy Support"   # specific hub
    python backup.py --dry-run                            # sweep + dry-run
    python backup.py --preview                            # sweep + 16 KB preview
    python backup.py --skip-sweep                         # reuse existing copylist.pq
    python backup.py --dest mount_path                    # override SMB location
"""

import argparse, re, subprocess, sys, pathlib

HERE = pathlib.Path(__file__).parent
DEFAULT_HUB = "Developer Advocacy Support"


def run(cmd):
    """Run a command, streaming output live. Returns stdout as a string."""
    print(f"\n{'─'*60}")
    print(f"▶ {' '.join(cmd)}")
    print(f"{'─'*60}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=HERE)
    lines = []
    for line in proc.stdout:
        print(line, end="")
        lines.append(line)
    proc.wait()
    if proc.returncode != 0:
        print(f"\nERROR: command exited with code {proc.returncode}")
        sys.exit(proc.returncode)
    return "".join(lines)


def parse_hub_name(sweep_output):
    """Extract hub display name from sweep.py stdout.

    sweep.py prints:  '  Hub: Developer Advocacy Support (b.xxx)'
    """
    m = re.search(r"Hub:\s+(.+?)\s+\(", sweep_output)
    if not m:
        print("ERROR: could not parse hub name from sweep output")
        sys.exit(1)
    return m.group(1).strip()


def main():
    parser = argparse.ArgumentParser(description="Back up a hub to SMB")
    parser.add_argument("--hub", default=DEFAULT_HUB,
                        help="Hub name or ID (default: %(default)s)")
    parser.add_argument("--dest", default="",
                        help="Destination SMB Share (default: SMB_SHARE from .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be uploaded, no downloads/uploads")
    parser.add_argument("--preview", action="store_true",
                        help="Upload only first 16 KB of each file")
    parser.add_argument("--skip-sweep", action="store_true",
                        help="Skip sweep, reuse existing copylist.pq")
    args = parser.parse_args()

    # ── Future: Cleanup pass ─────────────────────────────────────────────
    # TODO: Read cache/removals.pq here (produced by webhook_listener.py),
    # delete stale SMB paths, remove entries from cache.pq, then
    # truncate removals.pq.  See README "Future Work" section.

    # ── Phase 1: Sweep ───────────────────────────────────────────────────
    if args.skip_sweep:
        print("Skipping sweep (--skip-sweep), reusing existing copylist.pq")
        # Still need hub name for the S3 prefix — resolve it via the API
        from auth import load_token
        from sweep import find_hub
        load_token()  # ensure token is ready
        _, hub_name = find_hub(args.hub)
        print(f"  Hub: {hub_name}")
    else:
        sweep_cmd = [sys.executable, str(HERE / "sweep.py"), "--hub", args.hub]
        sweep_output = run(sweep_cmd)
        hub_name = parse_hub_name(sweep_output)

    # ── Phase 2: Copy to S3 ──────────────────────────────────────────────
    copy_cmd = [sys.executable, str(HERE / "copy_to_smb.py"),
                "--prefix", hub_name]
    if args.bucket:
        copy_cmd += ["--dest", args.dest]
    if args.dry_run:
        copy_cmd.append("--dry-run")
    if args.preview:
        copy_cmd.append("--preview")

    run(copy_cmd)


if __name__ == "__main__":
    main()
