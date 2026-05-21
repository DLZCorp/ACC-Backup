#!/usr/bin/env python3
"""
Back up an entire hub to a mounted SMB share.  Runs sweep.py to detect changed
files, then copy_to_smb.py to download them with the hub name as the root
sub-directory on the share.

Destination layout:  {SMB_DEST}/{hub_name}/{project_name}/{path}

Usage:
    python backup.py                                     # default hub
    python backup.py --hub "Developer Advocacy Support"   # specific hub
    python backup.py --dry-run                            # sweep + dry-run
    python backup.py --preview                            # sweep + 16 KB preview
    python backup.py --skip-sweep                         # reuse existing copylist.pq
    python backup.py --dest mount_path                    # override SMB location
    python backup.py --workers 8                          # override worker count
"""

import argparse, re, subprocess, sys, pathlib, ssl, base64, os

bundlePath = r'\\azure-pan2\Revit\ACC-Backup\certs\Zscaler.pem'

ctx = ssl.create_default_context()
certs = ctx.get_ca_certs(binary_form=True)


# Write them all to a PEM bundle
out = pathlib.Path(bundlePath)
with out.open('wb') as f:
    for cert in certs:
        f.write(b'-----BEGIN CERTIFICATE-----\n')
        f.write(base64.encodebytes(cert))
        f.write(b'-----END CERTIFICATE-----\n')

os.environ['REQUESTS_CA_BUNDLE'] = bundlePath

HERE = pathlib.Path(__file__).parent
DEFAULT_HUB = "DLZ Corporation"


def run(cmd):
    """Run a command, streaming output live. Returns stdout as a string.

    The child Python process is launched with `-u` so its stdout/stderr are
    unbuffered — otherwise PIPE-redirected output sits in a 4-8 KB buffer
    and the parent sees long silences while workers download.

    The pipe is read with encoding='utf-8', errors='replace' because the
    child reconfigures its stdout to UTF-8 (to handle exotic chars like
    \\u200e in ACC file paths). Without pinning the parent's side, Python
    defaults to the OS locale (cp1252 on US Windows), which can't decode
    those bytes and crashes mid-stream.
    """
    print(f"\n{'─' * 60}")
    print(f"▶ {' '.join(cmd)}")
    print(f"{'─' * 60}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=HERE, bufsize=1,
                            encoding="utf-8", errors="replace")
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
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

    run(copy_cmd)


if __name__ == "__main__":
    main()