#!/usr/bin/env python3
"""
Download files from Autodesk OSS (via signed S3 URLs) and upload to a
destination S3 bucket.  Reads copylist.pq produced by sweep.py.

Usage:
    python copy_to_s3.py                  # full backup
    python copy_to_s3.py --dry-run        # print what would be uploaded
    python copy_to_s3.py --preview        # upload first 16 KB of each file
    python copy_to_s3.py --bucket mybkt   # override S3_BUCKET from .env
"""

import argparse, os, pathlib, requests
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

from auth import load_token

load_dotenv(pathlib.Path(__file__).parent / ".env")

HERE = pathlib.Path(__file__).parent
COPYLIST_FILE = HERE / "cache" / "copylist.pq"
OSS_BASE = "https://developer.api.autodesk.com"
BATCH_SIZE = 20
PREVIEW_BYTES = 16 * 1024  # 16 KB


# ── URN helpers ──────────────────────────────────────────────────────────────

def parse_storage_urn(urn: str):
    """Extract (bucket_key, object_key) from an OSS storage URN.

    Example URN:
      urn:adsk.objects:os.object:wip.dm.prod/95373400-ab32-405b-bbbb-013f358aaf06.rvt
    Returns:
      ("wip.dm.prod", "95373400-ab32-405b-bbbb-013f358aaf06.rvt")
    """
    # Everything after the last colon is "bucketKey/objectKey"
    tail = urn.rsplit(":", 1)[-1]
    bucket_key, _, object_key = tail.partition("/")
    return bucket_key, object_key


# ── APS signed-URL batch request ─────────────────────────────────────────────

def batch_signed_download_urls(token, bucket_key, object_keys):
    """POST /oss/v2/buckets/{bucketKey}/objects/batchsigneds3download

    Returns dict {object_key: signed_url} for keys that succeeded.
    """
    url = f"{OSS_BASE}/oss/v2/buckets/{bucket_key}/objects/batchsigneds3download"
    body = {"requests": [{"objectKey": k} for k in object_keys]}
    r = requests.post(url,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json=body, timeout=60)
    if not r.ok:
        print(f"  !! OSS batch signed URL {r.status_code}: {r.text[:300]}")
        r.raise_for_status()

    results = r.json().get("results", {})
    out = {}
    for key, info in results.items():
        status = info.get("status")
        signed = info.get("url")
        if status in ("complete", "fallback") and signed:
            out[key] = signed
        else:
            print(f"  WARN: no signed URL for {key} (status={status})")
    return out


# ── Stream download → S3 upload ──────────────────────────────────────────────

def stream_to_s3(signed_url, s3_client, bucket, s3_key,
                 preview=False, size_bytes=0):
    """Download from signed_url and upload to S3, streaming in memory."""
    headers = {}
    if preview and size_bytes > 0:
        headers["Range"] = f"bytes=0-{PREVIEW_BYTES - 1}"

    resp = requests.get(signed_url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    s3_client.upload_fileobj(resp.raw, bucket, s3_key)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Upload copylist files to S3 via APS signed URLs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be uploaded, no downloads/uploads")
    parser.add_argument("--preview", action="store_true",
                        help="Upload only first 16 KB of each file")
    parser.add_argument("--bucket",
                        default=os.environ.get("S3_BUCKET", ""),
                        help="Destination S3 bucket (default: S3_BUCKET from .env)")
    parser.add_argument("--prefix", default="",
                        help="Prefix prepended to all S3 keys (e.g. hub name)")
    args = parser.parse_args()

    # ── Validate ─────────────────────────────────────────────────────────
    if not args.dry_run and not args.bucket:
        print("ERROR: S3_BUCKET not set. Pass --bucket or set S3_BUCKET in .env")
        raise SystemExit(1)

    if not COPYLIST_FILE.exists():
        print(f"ERROR: {COPYLIST_FILE} not found. Run sweep.py first.")
        raise SystemExit(1)

    # ── Read copylist ────────────────────────────────────────────────────
    df = pd.read_parquet(COPYLIST_FILE)
    df = df[df["storage_urn"].astype(str).str.strip().ne("")]
    print(f"Copylist: {len(df)} files with storage URNs")

    if df.empty:
        print("Nothing to copy.")
        return

    # ── Group by bucket key ──────────────────────────────────────────────
    groups = defaultdict(list)  # bucket_key → [(object_key, row)]
    for _, row in df.iterrows():
        bucket_key, object_key = parse_storage_urn(row["storage_urn"])
        s3_key = f"{row['project_name']}/{row['path'].lstrip('/')}"
        if args.prefix:
            s3_key = f"{args.prefix}/{s3_key}"
        groups[bucket_key].append((object_key, s3_key, row))

    # ── Dry-run: just print ──────────────────────────────────────────────
    if args.dry_run:
        total_bytes = 0
        for bucket_key, items in groups.items():
            print(f"\n  OSS bucket: {bucket_key} ({len(items)} files)")
            for obj_key, s3_key, row in items:
                size = int(row.get("size_bytes", 0))
                total_bytes += size
                print(f"    {s3_key}  ({size:,} bytes)")
        print(f"\nTotal: {len(df)} files, {total_bytes:,.0f} bytes")
        print("(dry-run — nothing downloaded or uploaded)")
        return

    # ── Set up clients ───────────────────────────────────────────────────
    import boto3
    token = load_token()
    s3 = boto3.client("s3")
    mode = "preview (16 KB)" if args.preview else "full"
    print(f"Mode: {mode}")
    print(f"Destination: s3://{args.bucket}/\n")

    uploaded = 0
    errors = 0

    for bucket_key, items in groups.items():
        # Batch into groups of BATCH_SIZE
        object_keys = [obj_key for obj_key, _, _ in items]
        key_to_s3 = {obj_key: (s3_key, int(row.get("size_bytes", 0)))
                     for obj_key, s3_key, row in items}

        for i in range(0, len(object_keys), BATCH_SIZE):
            batch_keys = object_keys[i : i + BATCH_SIZE]
            try:
                signed = batch_signed_download_urls(token, bucket_key, batch_keys)
            except Exception as e:
                print(f"  ERR batch signed URLs for {bucket_key}: {e}")
                errors += len(batch_keys)
                continue

            for obj_key in batch_keys:
                s3_key, size_bytes = key_to_s3[obj_key]
                url = signed.get(obj_key)
                if not url:
                    errors += 1
                    continue
                try:
                    stream_to_s3(url, s3, args.bucket, s3_key,
                                 preview=args.preview, size_bytes=size_bytes)
                    uploaded += 1
                    print(f"  OK  s3://{args.bucket}/{s3_key}")
                except Exception as e:
                    errors += 1
                    print(f"  ERR {s3_key}: {e}")

    # ── Upload manifest ──────────────────────────────────────────────────
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest_key = f"{args.prefix}/_manifests/{run_date}.parquet" if args.prefix else f"_manifests/{run_date}.parquet"
    try:
        s3.upload_file(str(COPYLIST_FILE), args.bucket, manifest_key)
        print(f"\n  MANIFEST  s3://{args.bucket}/{manifest_key}")
    except Exception as e:
        print(f"\n  ERR manifest upload: {e}")

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"COPY SUMMARY")
    print(f"{'='*60}")
    print(f"  Mode:     {mode}")
    print(f"  Bucket:   {args.bucket}")
    print(f"  Uploaded: {uploaded}")
    print(f"  Errors:   {errors}")
    print(f"  Total:    {len(df)} files in copylist")
    print(f"  Manifest: {manifest_key}")


if __name__ == "__main__":
    main()
