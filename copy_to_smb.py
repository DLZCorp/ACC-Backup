#!/usr/bin/env python3
"""
Download files from Autodesk OSS (via signed S3 URLs) and write them to a
mounted SMB share (or any locally-visible path). Reads copylist.pq produced
by sweep.py. Tracks per-file status, retries prior failures, sanitizes
illegal Windows path characters, and handles long UNC paths.

Concurrency model: one main thread mints APS signed URLs serially (respecting
the ~60/min APS rate limit) and dispatches work to N download workers. The
workers run S3 downloads in parallel so slow files don't block the URL queue.

Usage:
    python copy_to_smb.py                        # full copy
    python copy_to_smb.py --dry-run              # print what would be written
    python copy_to_smb.py --preview              # write first 16 KB of each file
    python copy_to_smb.py --dest \\\\srv\\share  # override SMB_DEST from .env
    python copy_to_smb.py --workers 8            # override worker count
"""

import argparse
import os
import pathlib
import queue
import sys
import threading
import time
from collections import defaultdict

import pandas as pd
import requests
from dotenv import load_dotenv

from auth import load_token

load_dotenv(pathlib.Path(__file__).parent / ".env")

HERE = pathlib.Path(__file__).parent
COPYLIST_FILE = HERE / "cache" / "copylist.pq"
FAILURES_FILE = HERE / "cache" / "failures.pq"
OSS_BASE = "https://developer.api.autodesk.com"
PREVIEW_BYTES = 16 * 1024  # 16 KB

# APS OSS signed URL endpoint allows ~60 req/min per token.
# We generate one URL immediately before each download so it never expires
# mid-queue. Sleep between URL requests to stay within the rate limit.
_RATE_LIMIT_SLEEP = 60 / 50  # 1.2 s — slightly under the 60/min hard limit

# Retry config
_RETRY_PAUSE = 5  # seconds to wait before the single retry attempt

# How often to flush the copylist status column to parquet
_FLUSH_EVERY = 25

# Threading
_DEFAULT_WORKERS = 4
# Bounded work queue. Cap = workers + small buffer so URL minting blocks
# (gives natural backpressure) and signed URLs don't sit too long.
_WORK_QUEUE_BUFFER = 4

# Windows path handling
_ILLEGAL_WIN_CHARS = '<>:"/\\|?*'
_LONG_PATH_THRESHOLD = 240  # chars; prefix with \\?\UNC\ above this

# Status column values
STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

# Single print lock so worker-result prints don't interleave
_print_lock = threading.Lock()


def _log(msg: str) -> None:
    """Thread-safe print. Falls back to replacement chars if the console's
    encoding can't handle something in the message (e.g. cp1252 console
    encountering exotic Unicode in a file path)."""
    with _print_lock:
        try:
            print(msg)
        except UnicodeEncodeError:
            enc = (sys.stdout.encoding or "ascii")
            print(msg.encode(enc, errors="replace").decode(enc))


# ── Path sanitization ────────────────────────────────────────────────────────

def sanitize_path_component(name: str) -> str:
    """Replace Windows-forbidden characters in a single path component.

    Replaces <>:"/\\|?* with underscore, then strips trailing spaces and dots
    (also forbidden by Windows at the end of a name).

    Args:
        name: A single path component (project, folder, or file name).

    Returns:
        Sanitized string. Empty input returns an empty string.
    """
    if not name:
        return ""
    cleaned = "".join("_" if c in _ILLEGAL_WIN_CHARS else c for c in name)
    return cleaned.rstrip(" .")


def sanitize_relative_path(rel: str) -> str:
    """Sanitize each component of a forward-slash-separated relative path."""
    parts = [sanitize_path_component(p) for p in rel.split("/") if p]
    return "/".join(parts)


def to_long_path(p: pathlib.Path) -> pathlib.Path:
    """Prefix UNC/local paths over MAX_PATH with \\?\\ so Windows allows
    ~32K-char paths. No-op on short paths.
    """
    s = str(p)
    if len(s) <= _LONG_PATH_THRESHOLD:
        return p
    if s.startswith("\\\\?\\"):
        return p
    if s.startswith("\\\\"):
        return pathlib.Path("\\\\?\\UNC\\" + s.lstrip("\\"))
    return pathlib.Path("\\\\?\\" + s)


# ── URN helpers ───────────────────────────────────────────────────────────────

def parse_storage_urn(urn: str) -> tuple[str, str]:
    """Extract (bucket_key, object_key) from an OSS storage URN."""
    tail = urn.rsplit(":", 1)[-1]
    bucket_key, _, object_key = tail.partition("/")
    return bucket_key, object_key


# ── APS signed-URL request (main thread only) ────────────────────────────────

def get_signed_download_url(bucket_key: str, object_key: str) -> str | None:
    """Mint one S3 signed download URL via APS OSS.

    Called only by the main thread; URL minting is single-threaded to respect
    the APS rate limit and avoid token-refresh races. Returns None on failure.
    """
    url = f"{OSS_BASE}/oss/v2/buckets/{bucket_key}/objects/batchsigneds3download"
    body = {"requests": [{"objectKey": object_key}]}
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {load_token()}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )
    except Exception as e:
        _log(f"  !! OSS signed URL request raised: {type(e).__name__}: {e}")
        return None

    if not r.ok:
        _log(f"  !! OSS signed URL {r.status_code}: {r.text[:300]}")
        return None

    results = r.json().get("results", {})
    info = results.get(object_key, {})
    status = info.get("status")
    signed = info.get("url")
    if status in ("complete", "fallback") and signed:
        return signed
    _log(f"  WARN: no signed URL for {object_key} (status={status})")
    return None


# ── Stream download (called from worker threads) ─────────────────────────────

def stream_to_file(signed_url: str, dest_path: pathlib.Path,
                   preview: bool = False) -> None:
    """Stream a download to dest_path. Safe to call from worker threads."""
    headers: dict[str, str] = {}
    if preview:
        headers["Range"] = f"bytes=0-{PREVIEW_BYTES - 1}"

    long_dest = to_long_path(dest_path)
    long_dest.parent.mkdir(parents=True, exist_ok=True)

    resp = requests.get(signed_url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    with long_dest.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=256 * 1024):
            fh.write(chunk)


# ── Worker ───────────────────────────────────────────────────────────────────

_STOP = object()  # sentinel pushed onto work_queue to stop a worker


def _download_worker(work_queue: "queue.Queue", result_queue: "queue.Queue",
                     stop_event: threading.Event) -> None:
    """Consume (idx, dest_path, signed_url, preview) tuples; push results.

    On failure, sleeps _RETRY_PAUSE and retries once with the same signed URL
    (S3 signed URLs are valid for hours, so reuse is fine).
    """
    while True:
        job = work_queue.get()
        if job is _STOP:
            work_queue.task_done()
            return

        idx, dest_path, signed_url, preview = job
        last_err = ""
        success = False
        for attempt in (1, 2):
            if stop_event.is_set():
                last_err = "interrupted"
                break
            try:
                stream_to_file(signed_url, dest_path, preview=preview)
                success = True
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt == 1:
                    stop_event.wait(_RETRY_PAUSE)

        result_queue.put((idx, success, last_err))
        work_queue.task_done()


# ── Copylist / failures persistence ──────────────────────────────────────────

def ensure_status_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add a 'status' column initialized to 'pending' if missing."""
    df = df.copy()
    if "status" not in df.columns:
        df["status"] = STATUS_PENDING
    else:
        df["status"] = df["status"].fillna(STATUS_PENDING)
    return df


def merge_prior_failures(copylist_df: pd.DataFrame) -> pd.DataFrame:
    """Prepend prior-run failed rows to the new copylist, dropping any whose
    item_id is already in the fresh copylist (sweep will re-detect those).
    """
    if not FAILURES_FILE.exists():
        return copylist_df

    try:
        prior = pd.read_parquet(FAILURES_FILE)
    except Exception as e:
        _log(f"  WARN: could not read {FAILURES_FILE.name}: {e}")
        return copylist_df

    if prior.empty:
        return copylist_df

    if "item_id" in copylist_df.columns and "item_id" in prior.columns:
        fresh_ids = set(copylist_df["item_id"])
        prior = prior[~prior["item_id"].isin(fresh_ids)]

    if prior.empty:
        return copylist_df

    prior = prior.copy()
    prior["status"] = STATUS_PENDING
    _log(f"  Retrying {len(prior)} file(s) that failed on a previous run")
    return pd.concat([prior, copylist_df], ignore_index=True)


def flush_copylist(df: pd.DataFrame) -> None:
    """Write the copylist back to disk so an interrupted run is recoverable."""
    try:
        df.to_parquet(COPYLIST_FILE)
    except Exception as e:
        _log(f"  WARN: could not flush copylist: {e}")


def write_failures(df: pd.DataFrame) -> None:
    """Persist failed rows of this run to failures.pq, or remove a stale one."""
    failed = df[df["status"] == STATUS_FAILED]
    FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)

    if failed.empty:
        if FAILURES_FILE.exists():
            try:
                FAILURES_FILE.unlink()
            except OSError as e:
                _log(f"  WARN: could not remove stale failures.pq: {e}")
        return

    try:
        failed.to_parquet(FAILURES_FILE)
        _log(f"  Wrote {len(failed)} failed row(s) to {FAILURES_FILE.name}")
    except Exception as e:
        _log(f"  WARN: could not write failures.pq: {e}")


# ── Result handling ──────────────────────────────────────────────────────────

def _apply_result(idx: int, success: bool, err: str, df: pd.DataFrame,
                  total: int, dest_paths: list, counters: dict) -> None:
    """Apply one worker result to df + counters, print a status line, and
    flush the copylist if the per-25 threshold is reached.

    The progress counter shows the row index (idx + 1) so resumed runs
    print where they actually are in the copylist instead of restarting
    at [1/N].
    """
    if success:
        df.at[idx, "status"] = STATUS_SUCCEEDED
        counters["uploaded"] += 1
        _log(f"  [{idx + 1}/{total}] OK  {dest_paths[idx]}")
    else:
        df.at[idx, "status"] = STATUS_FAILED
        counters["errors"] += 1
        _log(f"  [{idx + 1}/{total}] ERR {dest_paths[idx]}: {err}")

    counters["completed"] += 1
    counters["since_flush"] += 1
    if counters["since_flush"] >= _FLUSH_EVERY:
        flush_copylist(df)
        counters["since_flush"] = 0


def _drain_available(result_queue: "queue.Queue", df: pd.DataFrame,
                     total: int, dest_paths: list, counters: dict) -> None:
    """Apply all currently-available results without blocking."""
    while True:
        try:
            idx, success, err = result_queue.get_nowait()
        except queue.Empty:
            return
        _apply_result(idx, success, err, df, total, dest_paths, counters)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Make stdout tolerant of Unicode that the OEM console encoding (cp1252
    # on US Windows) can't represent. File paths from ACC sometimes contain
    # invisible formatting characters like \u200e (LRM); without this the
    # first such path would crash the run via print().
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass  # already configured, or running under a transport that can't

    parser = argparse.ArgumentParser(
        description="Copy copylist files to a mounted SMB share via APS signed URLs"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written, no downloads")
    parser.add_argument("--preview", action="store_true",
                        help="Write only first 16 KB of each file")
    parser.add_argument("--dest", default=os.environ.get("SMB_DEST", ""),
                        help="Destination root (default: SMB_DEST from .env)")
    parser.add_argument("--prefix", default="",
                        help="Sub-directory prepended to destination paths")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS,
                        help=f"Parallel download workers (default: {_DEFAULT_WORKERS})")
    args = parser.parse_args()

    # ── Validate ──────────────────────────────────────────────────────────
    if not args.dry_run and not args.dest:
        print("ERROR: SMB_DEST not set. Pass --dest or set SMB_DEST in .env")
        raise SystemExit(1)
    if not COPYLIST_FILE.exists():
        print(f"ERROR: {COPYLIST_FILE} not found. Run sweep.py first.")
        raise SystemExit(1)
    if args.workers < 1:
        print("ERROR: --workers must be >= 1")
        raise SystemExit(1)

    dest_root = pathlib.Path(args.dest) if args.dest else pathlib.Path(".")

    # ── Load + merge ──────────────────────────────────────────────────────
    df = pd.read_parquet(COPYLIST_FILE)
    df = df[df["storage_urn"].astype(str).str.strip().ne("")]
    df = ensure_status_column(df)
    df = merge_prior_failures(df)
    df = df.reset_index(drop=True)
    print(f"Copylist: {len(df)} files with storage URNs")

    # Resume summary — count what's already done before we start
    already_succeeded = int((df["status"] == STATUS_SUCCEEDED).sum())
    remaining = len(df) - already_succeeded
    if already_succeeded > 0:
        print(f"  Resuming: {already_succeeded} already succeeded, "
              f"{remaining} remaining")

    if df.empty:
        print("Nothing to copy.")
        return

    load_token()  # warm-up

    # ── Compute destination paths up front ────────────────────────────────
    dest_paths: list[pathlib.Path] = []
    bucket_keys: list[str] = []
    object_keys: list[str] = []
    sizes: list[int] = []
    for _, row in df.iterrows():
        bucket_key, object_key = parse_storage_urn(row["storage_urn"])
        project = sanitize_path_component(str(row["project_name"]))
        sub = sanitize_relative_path(str(row["path"]).lstrip("/"))
        rel = f"{project}/{sub}"
        if args.prefix:
            rel = f"{sanitize_path_component(args.prefix)}/{rel}"
        dest_paths.append(dest_root / pathlib.PurePosixPath(rel))
        bucket_keys.append(bucket_key)
        object_keys.append(object_key)
        sizes.append(int(row.get("size_bytes", 0)))

    # ── Dry-run ────────────────────────────────────────────────────────────
    if args.dry_run:
        groups: dict[str, list[tuple[pathlib.Path, int]]] = defaultdict(list)
        for bk, dp, sz in zip(bucket_keys, dest_paths, sizes):
            groups[bk].append((dp, sz))
        total_bytes = 0
        for bucket_key, items in groups.items():
            print(f"\n  OSS bucket: {bucket_key} ({len(items)} files)")
            for dp, size in items:
                total_bytes += size
                print(f"    {dp}  ({size:,} bytes)")
        print(f"\nTotal: {len(df)} files, {total_bytes:,.0f} bytes")
        print("(dry-run — nothing downloaded or written)")
        return

    # ── Set up threading ──────────────────────────────────────────────────
    mode = "preview (16 KB)" if args.preview else "full"
    print(f"Mode:        {mode}")
    print(f"Destination: {dest_root}")
    print(f"Workers:     {args.workers}\n")

    total = len(df)
    counters = {"uploaded": 0, "errors": 0, "skipped": 0,
                "completed": 0, "since_flush": 0}

    work_queue: queue.Queue = queue.Queue(maxsize=args.workers + _WORK_QUEUE_BUFFER)
    result_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    workers = [
        threading.Thread(
            target=_download_worker,
            args=(work_queue, result_queue, stop_event),
            daemon=True,
            name=f"dl-{i}",
        )
        for i in range(args.workers)
    ]
    for w in workers:
        w.start()

    try:
        for i in range(total):
            # Skip rows already completed in a prior interrupted run
            if df.at[i, "status"] == STATUS_SUCCEEDED:
                counters["skipped"] += 1
                continue

            # Show completed results in roughly real time
            _drain_available(result_queue, df, total, dest_paths, counters)

            # Mint signed URL (rate-limited)
            signed_url = get_signed_download_url(bucket_keys[i], object_keys[i])

            if not signed_url:
                # Count as an immediate failure; no worker involvement
                df.at[i, "status"] = STATUS_FAILED
                counters["errors"] += 1
                counters["completed"] += 1
                counters["since_flush"] += 1
                _log(f"  [{i + 1}/{total}] ERR {dest_paths[i]}: no signed URL")
                if counters["since_flush"] >= _FLUSH_EVERY:
                    flush_copylist(df)
                    counters["since_flush"] = 0
                time.sleep(_RATE_LIMIT_SLEEP)
                continue

            # Dispatch to a worker (blocks when queue is full → backpressure)
            work_queue.put((i, dest_paths[i], signed_url, args.preview))

            time.sleep(_RATE_LIMIT_SLEEP)

        # All URLs minted. Drain whatever workers have already finished;
        # the remaining wait happens via work_queue.join() below.
        _drain_available(result_queue, df, total, dest_paths, counters)

    except KeyboardInterrupt:
        _log("\nInterrupted. Stopping workers and flushing state...")
        stop_event.set()
        # Drain pending results
        _drain_available(result_queue, df, total, dest_paths, counters)
        # Empty the work queue so workers can hit task_done() and exit
        try:
            while True:
                work_queue.get_nowait()
                work_queue.task_done()
        except queue.Empty:
            pass
        flush_copylist(df)
        write_failures(df)
        raise

    # ── Wait for all dispatched work to complete ──────────────────────────
    # work_queue.join() blocks until every put() has been task_done()'d.
    # That guarantees every dispatched job has been processed and its
    # result is already on result_queue (workers put result BEFORE task_done).
    work_queue.join()
    _drain_available(result_queue, df, total, dest_paths, counters)

    # Tell workers to exit cleanly
    for _ in workers:
        work_queue.put(_STOP)
    for w in workers:
        w.join(timeout=5)

    # Final flush + failures.pq
    flush_copylist(df)
    write_failures(df)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("COPY SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Mode:                 {mode}")
    print(f"  Destination:          {dest_root}")
    print(f"  Workers:              {args.workers}")
    print(f"  Written this run:     {counters['uploaded']}")
    print(f"  Skipped (already OK): {counters['skipped']}")
    print(f"  Errors:               {counters['errors']}")
    print(f"  Total in copylist:    {total}")


if __name__ == "__main__":
    main()