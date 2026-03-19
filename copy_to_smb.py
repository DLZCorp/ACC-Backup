#!/usr/bin/env python3
"""
Download files from Autodesk OSS (via signed S3 URLs) and write them to a
mounted SMB share (or any locally-visible path). Reads copylist.pq produced
by sweep.py.  The share must be mounted before running this script.

Usage:
    python copy_to_smb.py                        # full copy
    python copy_to_smb.py --dry-run              # print what would be written
    python copy_to_smb.py --preview              # write first 16 KB of each file
    python copy_to_smb.py --dest \\\\srv\\share  # override SMB_DEST from .env
"""

import argparse
import os
import pathlib

import pandas as pd
import requests
from collections import defaultdict
from dotenv import load_dotenv

from auth import load_token

load_dotenv(pathlib.Path(__file__).parent / ".env")

HERE = pathlib.Path(__file__).parent
COPYLIST_FILE = HERE / "cache" / "copylist.pq"
OSS_BASE = "https://developer.api.autodesk.com"
BATCH_SIZE = 20
PREVIEW_BYTES = 16 * 1024  # 16 KB


# ── URN helpers ───────────────────────────────────────────────────────────────

def parse_storage_urn(urn: str) -> tuple[str, str]:
    """Extract (bucket_key, object_key) from an OSS storage URN.

    Example URN:
      urn:adsk.objects:os.object:wip.dm.prod/95373400-ab32-405b-bbbb-013f358aaf06.rvt
    Returns:
      ("wip.dm.prod", "95373400-ab32-405b-bbbb-013f358aaf06.rvt")
    """
    tail = urn.rsplit(":", 1)[-1]
    bucket_key, _, object_key = tail.partition("/")
    return bucket_key, object_key


# ── APS signed-URL batch request ──────────────────────────────────────────────

def batch_signed_download_urls(token: str, bucket_key: str, object_keys: list[str]) -> dict[str, str]:
    """POST /oss/v2/buckets/{bucketKey}/objects/batchsigneds3download

    Returns dict {object_key: signed_url} for keys that succeeded.
    """
    url = f"{OSS_BASE}/oss/v2/buckets/{bucket_key}/objects/batchsigneds3download"
    body = {"requests": [{"objectKey": k} for k in object_keys]}
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if not r.ok:
        print(f"  !! OSS batch signed URL {r.status_code}: {r.text[:300]}")
        r.raise_for_status()

    results = r.json().get("results", {})
    out: dict[str, str] = {}
    for key, info in results.items():
        status = info.get("status")
        signed = info.get("url")
        if status in ("complete", "fallback") and signed:
            out[key] = signed
        else:
            print(f"  WARN: no signed URL for {key} (status={status})")
    return out


# ── Stream download → local/SMB write ────────────────────────────────────────

def stream_to_file(signed_url: str, dest_path: pathlib.Path, preview: bool = False) -> None:
    """Download from signed_url and write to dest_path, streaming to avoid
    loading the entire file into memory.

    Args:
        signed_url: Pre-signed S3 URL to download from.
        dest_path: Absolute path on the mounted share (or any local path).
        preview: If True, request only the first PREVIEW_BYTES bytes.
    """
    headers: dict[str, str] = {}
    if preview:
        headers["Range"] = f"bytes=0-{PREVIEW_BYTES - 1}"

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    resp = requests.get(signed_url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    with dest_path.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=256 * 1024):
            fh.write(chunk)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy copylist files to a mounted SMB share via APS signed URLs"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written, no downloads or file writes",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Write only first 16 KB of each file",
    )
    parser.add_argument(
        "--dest",
        default=os.environ.get("SMB_DEST", ""),
        help="Destination root path on the mounted share (default: SMB_DEST from .env)",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Sub-directory prepended to all destination paths (e.g. hub name)",
    )
    args = parser.parse_args()

    # ── Validate ──────────────────────────────────────────────────────────
    if not args.dry_run and not args.dest:
        print("ERROR: SMB_DEST not set. Pass --dest or set SMB_DEST in .env")
        raise SystemExit(1)

    if not COPYLIST_FILE.exists():
        print(f"ERROR: {COPYLIST_FILE} not found. Run sweep.py first.")
        raise SystemExit(1)

    dest_root = pathlib.Path(args.dest) if args.dest else pathlib.Path(".")

    # ── Read copylist ──────────────────────────────────────────────────────
    df = pd.read_parquet(COPYLIST_FILE)
    df = df[df["storage_urn"].astype(str).str.strip().ne("")]
    print(f"Copylist: {len(df)} files with storage URNs")

    if df.empty:
        print("Nothing to copy.")
        return

    # ── Group by OSS bucket key ────────────────────────────────────────────
    # Each entry maps object_key → (dest_path, size_bytes)
    groups: dict[str, list[tuple[str, pathlib.Path, int]]] = defaultdict(list)

    for _, row in df.iterrows():
        bucket_key, object_key = parse_storage_urn(row["storage_urn"])
        rel = f"{row['project_name']}/{row['path'].lstrip('/')}"
        if args.prefix:
            rel = f"{args.prefix}/{rel}"
        dest_path = dest_root / pathlib.PurePosixPath(rel)
        groups[bucket_key].append((object_key, dest_path, int(row.get("size_bytes", 0))))

    # ── Dry-run: just print ────────────────────────────────────────────────
    if args.dry_run:
        total_bytes = 0
        for bucket_key, items in groups.items():
            print(f"\n  OSS bucket: {bucket_key} ({len(items)} files)")
            for _, dest_path, size in items:
                total_bytes += size
                print(f"    {dest_path}  ({size:,} bytes)")
        print(f"\nTotal: {len(df)} files, {total_bytes:,.0f} bytes")
        print("(dry-run — nothing downloaded or written)")
        return

    # ── Copy ───────────────────────────────────────────────────────────────
    token = load_token()
    mode = "preview (16 KB)" if args.preview else "full"
    print(f"Mode:        {mode}")
    print(f"Destination: {dest_root}\n")

    uploaded = 0
    errors = 0

    for bucket_key, items in groups.items():
        object_keys = [obj_key for obj_key, _, _ in items]
        key_to_dest: dict[str, tuple[pathlib.Path, int]] = {
            obj_key: (dest_path, size) for obj_key, dest_path, size in items
        }

        for i in range(0, len(object_keys), BATCH_SIZE):
            batch_keys = object_keys[i : i + BATCH_SIZE]
            try:
                signed = batch_signed_download_urls(token, bucket_key, batch_keys)
            except Exception as e:
                print(f"  ERR batch signed URLs for {bucket_key}: {e}")
                errors += len(batch_keys)
                continue

            for obj_key in batch_keys:
                dest_path, _ = key_to_dest[obj_key]
                url = signed.get(obj_key)
                if not url:
                    errors += 1
                    continue
                try:
                    stream_to_file(url, dest_path, preview=args.preview)
                    uploaded += 1
                    print(f"  OK  {dest_path}")
                except Exception as e:
                    errors += 1
                    print(f"  ERR {dest_path}: {e}")

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"COPY SUMMARY")
    print(f"{'='*60}")
    print(f"  Mode:        {mode}")
    print(f"  Destination: {dest_root}")
    print(f"  Written:     {uploaded}")
    print(f"  Errors:      {errors}")
    print(f"  Total:       {len(df)} files in copylist")


if __name__ == "__main__":
    main()