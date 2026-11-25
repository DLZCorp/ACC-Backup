# ACC Hub Backup

**Back up an entire Autodesk Construction Cloud (ACC) hub to S3 — detect changes via DM API rollup timestamps, then stream files through signed URLs to your own bucket.**

No webhooks required. Runs as a single command.

![System Overview](docs/system-overview.webp)

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [The Sweep System](#the-sweep-system)
4. [The Copy System](#the-copy-system)
5. [Project Structure](#project-structure)
6. [Installation](#installation)
7. [CLI Reference](#cli-reference)
8. [Why Webhooks Aren't Needed](#why-webhooks-arent-needed)
9. [Future Work: Handling Moves & Deletes](#future-work-handling-moves--deletes)
10. [Examples](#examples)

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials (copy sample.env → .env, fill in values)
cp sample.env .env

# 3. Dry run — see what would be backed up
python backup.py --dry-run

# 4. Preview — upload first 16 KB of each file (quick test)
python backup.py --preview --bucket my-backup-bucket

# 5. Full backup
python backup.py --bucket my-backup-bucket
```

---

## How It Works

`backup.py` orchestrates two subsystems in sequence:

1. **Sweep** — detect what changed in the ACC hub since the last run
2. **Copy** — download changed files and upload them to S3

The handoff between them is `cache/copylist.pq` — a parquet file listing every changed file with its storage URN. The sweep produces it, the copy consumes it.

S3 key structure mirrors the hub:
```
s3://{bucket}/{hub_name}/{project_name}/Project Files/{folder}/{file}
```

---

## The Sweep System

![Sweep System — Cache & Dirty-Branch Pruning](docs/build-cache-with-sweep.webp)

`sweep.py` scans all projects in a hub using the DM API's `lastModifiedTimeRollup` timestamp as a pruning signal.

### How rollup pruning works

Every folder in ACC has a `lastModifiedTimeRollup` — a timestamp that propagates upward when any child folder or file changes. By caching this value and comparing it on the next run, the sweep can skip entire project trees with a single API call.

```
                    Hub
                     |
          ┌──────────┼──────────┐
       Project A  Project B  Project C
       rollup=T1  rollup=T2  rollup=T2
       cached=T1  cached=T1  (new)
          |          |          |
        CLEAN      DIRTY      DIRTY
       (skip!)    (walk)      (walk)
                     |
              ┌──────┼──────┐
           Folder   Folder  Folder
           X        Y       Z
           T2       T1      T2
           T1       T1      (new)
            |        |        |
          DIRTY    CLEAN    DIRTY
          (walk)  (skip!)   (walk)
            |                 |
         [files]           [files]  →  copylist.pq
```

### Four phases

| Phase | What it does | API calls |
|-------|-------------|-----------|
| **0 — Screen** | Compare each project's root rollup vs cache | 1 per project |
| **1 — Walk** | Recurse into dirty branches, prune clean ones | Only dirty folders |
| **2 — Collect** | Fetch file items from dirty folders, extract storage URNs | Only dirty folders |
| **3 — Persist** | Merge folder state → `cache/cache.pq`, write `cache/copylist.pq` | 0 (local I/O) |

### Cache files (`cache/`)

| File | Purpose |
|------|---------|
| `cache.pq` | Folder state snapshot — persists across runs. Stores `lastModifiedTimeRollup`, `objectCount`, `hidden`, folder path for every scanned folder. |
| `copylist.pq` | Changed files from the most recent sweep. Full path, filename, storage URN, file size, `hidden`/`reserved` flags, `lastModifiedTime`. |

### Three-day scenario (5 projects, 1 changes on day 2)

| Day | Dirty | Folders walked | API calls | Copylist |
|-----|-------|----------------|-----------|----------|
| 1 (cold start) | 5 | 106 | 229 | 311 files |
| 2 (1 project changes) | 1 | ~20 | ~30 | ~15 files |
| 3 (nothing changes) | 0 | 0 | 7 | 0 files |

Day 3 uses only **7 API calls** to confirm nothing changed.

### Why this works: `lastModifiedTime` vs `lastModifiedTimeRollup`

The DM API exposes two modification timestamps on folders:

| Field | What it means |
|-------|---------------|
| `lastModifiedTime` | When **this folder itself** was last modified |
| `lastModifiedTimeRollup` | When **this folder or any descendant** was last modified |

The rollup propagates upward. A file uploaded deep in `Project Files/A/B/C/` updates the rollup on `Project Files` immediately.

**Without rollup** (brute force): Call `GET contents` on every folder in every project. 1,000 projects x 100 folders = **100,000 API calls/day**.

**With rollup** (this approach): Screen each project with 1 call. Skip unchanged. Prune unchanged branches. Typical day at 5% dirty = **~6,500 API calls**. That's a **15x reduction**.

### Performance & rate limits (1,000 projects, daily run)

| | All dirty (worst) | 5% dirty (typical) | Nothing changed |
|--|-------------------|--------------------|--------------------|
| DM API calls | ~100,000 | ~6,000 | ~1,007 |
| OSS batch calls | ~10,000 | ~500 | 0 |
| **Total APS calls** | **~110,000** | **~6,500** | **~1,007** |

| Approach | API calls | Time at 300 rpm |
|----------|-----------|----------------|
| Brute force (all folders) | ~100,000 | 5.5 hours |
| Rollup pruning (5% dirty) | ~6,500 | 22 minutes |
| Rollup pruning (nothing changed) | ~1,007 | 3.4 minutes |

S3 downloads are direct-to-S3 via signed URLs and **do not count** against APS rate limits.

---

## The Copy System

![Copy System — Signed URL Batches to S3](docs/copy-to-s3-system.webp)

`copy_to_s3.py` reads the copylist, downloads files via APS signed S3 URLs, and streams them directly to your S3 bucket.

### How it works

1. **Parse storage URNs** — each copylist row has a URN like `urn:adsk.objects:os.object:wip.dm.prod/abc-123.rvt`. Split into `bucketKey` and `objectKey`.

2. **Batch signed URLs** — group by bucket key, request up to 20 signed download URLs per batch via `POST /oss/v2/buckets/{bucketKey}/objects/batchsigneds3download`.

3. **Stream to S3** — for each signed URL, open a streaming HTTP GET and pipe `response.raw` directly into `boto3.upload_fileobj()`. No temp files on disk.

### Preview mode

`--preview` adds a `Range: bytes=0-16383` header to download only the first 16 KB of each file. Useful for validating the pipeline without transferring full file sizes. Zero-byte files (views, links) are downloaded without the Range header.

### Authentication

| Service | Credential source |
|---------|------------------|
| APS (Autodesk) | SSA 3LO token via `auth.py` — reads from `.env` |
| AWS S3 | Default boto3 chain (`~/.aws/credentials`, env vars, IAM role) |

---

## How Versions Are Handled

ACC files are versioned — each upload creates a new version (v1, v2, v3...). The backup always copies the **latest tip version** of each file.

The copylist records two version fields per file:

| Field | Example | What it tells you |
|-------|---------|-------------------|
| `version_number` | `7` | Human-readable version (v7 = 7th upload of this file) |
| `tip_version_id` | `urn:adsk.wipprod:fs.file:vf.abc123?version=7` | Exact version URN — uniquely identifies this version in the DM API |

Combined with the `item_id` (stable lineage ID across all versions), you can reconstruct the full picture: "On March 3rd, we backed up v7 of `Structural.rvt` (item `urn:...`), which was 28 MB."

### Manifest as audit trail

Each backup run uploads the copylist to S3 as a date-stamped manifest under `_manifests/`:

```
s3://{bucket}/{hub_name}/_manifests/2026-03-03.parquet
```

This is the same parquet file that drives the copy — it contains every backed-up file's path, version number, tip version URN, size, and timestamps. To find out what version of a file was on ACC when a backup ran:

```bash
# What version of Structural.rvt did we back up on March 3rd?
aws s3 cp s3://my-bucket/My\ Hub/_manifests/2026-03-03.parquet /tmp/m.pq
python -c "
import pandas as pd
df = pd.read_parquet('/tmp/m.pq')
print(df[df.file_name.str.contains('Structural')][['path','version_number','tip_version_id','size_bytes']])
"
```

Over time, the `_manifests/` folder builds a history of every backup run — one parquet file per day. You can compare manifests across days to see which files changed and what their version numbers were.

### What happens when a file is updated in ACC

When a file gets a new version in ACC (e.g. `Structural.rvt` goes from v5 to v7), the next sweep detects the folder as dirty (rollup changed), and the copy uploads the new tip version. Since the S3 key is based on the file path (`{hub}/{project}/{path}`), **the file on S3 is overwritten** with the new version.

By default, the previous version on S3 is lost — but this is where **S3 Bucket Versioning** helps.

### Using S3 Bucket Versioning

If you enable [S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html) on your backup bucket, every overwrite creates a new S3 version instead of replacing the object. This means:

- The backup script doesn't need any code changes — it just overwrites the same key
- S3 silently preserves every previous version of every file
- You get a full version history on S3 mirroring what was on ACC, without any extra API calls

```bash
# Enable versioning on the bucket
aws s3api put-bucket-versioning \
  --bucket my-bucket-backup-acc-test \
  --versioning-configuration Status=Enabled

# List all S3 versions of a specific file
aws s3api list-object-versions \
  --bucket my-bucket-backup-acc-test \
  --prefix "My Hub/My Project/Project Files/Structural.rvt"
```

Pair this with the date-stamped manifests to correlate: "The S3 version from March 3rd corresponds to ACC tip v7 (per the manifest), and the S3 version from February 28th was ACC tip v5."

**Cost tip**: Add an [S3 Lifecycle rule](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html) to automatically expire old S3 versions (e.g. delete non-current versions after 90 days) or transition them to Glacier, so storage costs don't grow unbounded.

### What this does NOT do

- **Version history from ACC**: Only the tip (latest) version is backed up. Older ACC versions are not downloaded. To back up all versions, you'd need to page through each item's version history via `GET /data/v1/projects/{pid}/items/{itemId}/versions`.
- **Version diffing**: The backup doesn't compare version numbers across runs. If a file was at v5 yesterday and v7 today, intermediate versions (v6) are not individually captured — only v7 (the current tip) is backed up.

For most backup use cases this is the right trade-off: you get the latest state of every file, with enough metadata to know exactly which version you have. Enable S3 versioning to retain previous backups automatically.

---

## Project Structure

```
acc-hub-backup/
├── backup.py              # Entry point — orchestrates sweep + copy
├── sweep.py               # Sweep system — change detection with rollup cache
├── copy_to_s3.py          # Copy system — signed URL batches → S3
├── webhook_listener.py    # Webhook listener — move/delete → removals.pq
├── auth.py                # SSA 3LO token generation (shared by all scripts)
│
├── cache/                 # Runtime artifacts (gitignored)
│   ├── cache.pq           #   Folder state snapshot (persists across runs)
│   ├── copylist.pq        #   Changed files from last sweep
│   └── removals.pq        #   Move/delete events from webhook listener
│
├── docs/                  # Documentation & diagrams
│   ├── diagrams.md        #   System diagram descriptions
│   ├── system-overview.webp
│   ├── build-cache-with-sweep.webp
│   └── copy-to-s3-system.webp
│
├── examples/           # Exploratory scripts & API experiments
│   ├── scan_all_projects.py    # Rank all hubs/projects by activity
│   ├── heatmap.py              # Treemap heatmap of folder activity
│   ├── probe_dm_dates.py       # DM vs AECDM timestamp comparison
│   ├── ls_project.py           # ls -R via AECDM GraphQL API
│   ├── aecdm_client.py         # AECDM GraphQL wrapper
│   ├── probe_folder_dates.py   # Test AECDM folder date fields
│   └── depth_test.py           # AECDM query depth vs point budget
│
├── requirements.txt    # Python dependencies
├── sample.env          # Template .env with placeholder values
├── .env                # Credentials (gitignored)
└── .gitignore
```

---

## Installation

```bash
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `requests` | DM / OSS API calls |
| `pandas` + `pyarrow` | Parquet I/O for cache and copylist |
| `boto3` | S3 uploads |
| `PyJWT` + `cryptography` | SSA JWT token generation |
| `python-dotenv` | `.env` file loading |
| `matplotlib` + `squarify` | Heatmap rendering (optional, for examples) |

### Credentials (`.env`)

Copy `sample.env` to `.env` and fill in your values:

```env
# APS / Autodesk Platform Services
APS_CLIENT_ID=your-client-id
APS_CLIENT_SECRET=your-client-secret
APS_SERVICE_ACCOUNT_ID=your-service-account-id
APS_KEY_ID=your-private-key-id
APS_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
APS_SCOPES=account:read data:read data:create data:write

# AWS S3 destination bucket
S3_BUCKET=your-s3-bucket-name
```

The APS service account must be added to the ACC account. AWS credentials use the default boto3 chain.

---

## CLI Reference

### `backup.py` — Full hub backup

```bash
python backup.py                                     # sweep default hub → copy to S3
python backup.py --hub "My Hub Name"                  # specific hub
python backup.py --dry-run                            # sweep + print what would be uploaded
python backup.py --preview                            # sweep + upload first 16 KB per file
python backup.py --bucket my-bucket                   # override S3_BUCKET from .env
python backup.py --skip-sweep                         # skip sweep, reuse existing copylist.pq
```

### `sweep.py` — Change detection only

```bash
python sweep.py                                      # sweep default hub
python sweep.py --hub "My Hub Name"                   # specific hub by name
python sweep.py --hub b.489c5e7a-c6c0-4212-81f3-...  # by hub ID
```

### `copy_to_s3.py` — Copy only (uses existing copylist)

```bash
python copy_to_s3.py --dry-run                       # print what would be uploaded
python copy_to_s3.py --preview --bucket my-bucket    # upload 16 KB previews
python copy_to_s3.py --bucket my-bucket              # full upload
python copy_to_s3.py --prefix "Hub Name" --bucket b  # prepend hub name to S3 keys
```

---

## Failure Alerts

`backup.py` raises a Jira ticket when a run goes wrong, and `check_staleness.py` raises one when no run has completed recently. Both post JSON to a Jira Automation rule with an **Incoming webhook** trigger; the rule creates the ticket.

| Alert `kind` | Raised by | Meaning |
|--------------|-----------|---------|
| `failure` | `backup.py` | A phase exited non-zero or an unexpected exception occurred. Nothing was stamped. |
| `stale` | `check_staleness.py` | No completed run within `--max-age-hours` (default 26). |

A run that completes is a success even if individual files failed to download: those are recorded in `cache/failures.pq`, retried on the next run, and do not raise a ticket. The counts are printed at the end of the run and stored in `cache/last_run.json`.

The request is `POST <JIRA_ALERT_WEBHOOK_URL>` with header `X-Automation-Webhook-Token: <JIRA_ALERT_WEBHOOK_TOKEN>` and this body:

```json
{
  "source": "acc-backup",
  "kind": "failure",
  "summary": "ACC backup failed: sweep.py exited with code 1",
  "details": "Command: ...\n\nLast output:\n...",
  "host": "server-name",
  "timestamp": "2026-01-01T02:00:00+00:00"
}
```

### Jira rule

1. In the Jira project, create an automation rule with the **Incoming webhook** trigger, set so the rule runs without work items supplied in the request. Copy the URL and secret into `.env`.
2. Add a **Lookup work items** action: `project = <KEY> AND labels = acc-backup-alert AND statusCategory != Done`.
3. If the lookup returns nothing, **Create work item** with summary `{{webhookData.summary}}`, description built from `{{webhookData.details}}`, `{{webhookData.host}}` and `{{webhookData.timestamp}}`, and label `acc-backup-alert`.
4. Otherwise, **Comment** on the found work item with the same fields, so a backup that fails every night keeps one ticket open instead of opening one per night.

Test it with the rule's audit log open: run `python check_staleness.py --max-age-hours 0`, which always reports stale.

### Scheduling the staleness check

Run `check_staleness.py` daily as its own scheduled task, a few hours after the backup normally finishes. It exits 0 when fresh, 1 when stale and alerted, 2 when stale and the alert could not be sent. Because it runs on the same machine as the backup, it does not cover that machine being down.

---

## Why Webhooks Aren't Needed

For a **daily backup cycle**, polling with the rollup trick is simpler and sufficient:

| Factor | Webhooks | Rollup polling |
|--------|----------|----------------|
| Setup | Register callbacks, handle retries, manage endpoint uptime | Single script, runs on a cron |
| Missed events | Delivery failures require replay logic | Stateless — next poll catches everything |
| Infrastructure | Public HTTPS endpoint 24/7 | Runs anywhere — laptop, CI, Lambda |
| Cost at scale | Free but complex | ~6,500 API calls/day for 1,000 projects |

### Known limitation: deletions

`lastModifiedTimeRollup` does **not** update when files or folders are deleted. For deletion detection, consider a hybrid approach: rollup polling for adds/modifies + delete-only webhooks (`dm.folder.deleted`, `dm.version.deleted`) for the small number of active projects.

---

## Future Work: Handling Moves & Deletes

The rollup-based sweep detects **what appeared** (new or modified files) but not **what disappeared or moved**. When a file is deleted or a folder is moved in ACC, the rollup on the *destination* (or parent of the deleted item) changes — so the sweep picks up files at their new location. But it has no way to know that the old S3 path is now stale.

### The core insight: moves and deletes are the same problem

Both are "something left this path" events:

- **Delete** → file disappears from `Project Files/A/B/model.rvt`, nothing appears elsewhere
- **Move** → file disappears from `Project Files/A/B/model.rvt`, appears at `Project Files/C/model.rvt`

In both cases, the sweep naturally handles the *arrival* side (the new location triggers a dirty rollup and gets backed up). The gap is the *departure* side — cleaning up the orphaned S3 object at the old path.

### Prototype: `webhook_listener.py`

`webhook_listener.py` is a working prototype that subscribes to four departure-side webhook events and appends them to `cache/removals.pq`:

| Event | When it fires |
|-------|--------------|
| `dm.version.moved.out` | A file is moved *out of* a folder |
| `dm.version.deleted` | A file version is deleted |
| `dm.folder.moved.out` | A folder is moved *out of* its parent |
| `dm.folder.deleted` | A folder is deleted |

### Where webhooks are scoped

APS webhooks are scoped to a **folder URN**. When you register a webhook on a folder, it fires for events in that folder and **all descendants** — subfolders, sub-subfolders, files at any depth.

The natural attachment point is each project's **"Project Files" root folder** — the same folder that `sweep.py` already discovers via the `topFolders` API. This gives full coverage of the project tree with a single scope per event:

```
Hub
├── Project A  →  register 4 webhooks on "Project Files" folder URN
├── Project B  →  register 4 webhooks on "Project Files" folder URN
└── Project C  →  (cold project — skip, no webhooks needed)
```

That means **4 events x N projects** = 4N webhook subscriptions total. For a hub with 10 active projects, that's 40 subscriptions — well within APS limits.

The `--register` flag creates all four subscriptions for one or more folders in a single command:

```bash
# Register on a single project's root folder (4 POSTs)
python webhook_listener.py --register \
  --folder-urn "urn:adsk.wipprod:fs.folder:co.xxx" \
  --callback-url "https://your-endpoint.com/callback"

# Register on multiple projects at once (4 POSTs per folder)
python webhook_listener.py --register \
  --folder-urn "urn:...folderA" "urn:...folderB" "urn:...folderC" \
  --callback-url "https://your-endpoint.com/callback"
```

All subscriptions point to the **same callback URL** (one Lambda / one endpoint). The listener distinguishes projects by the `project_id` in the webhook payload.

### Registration rate limits

The APS Webhooks API allows **50 POST requests/minute** for creating hooks. At 4 events per project, you can register **12 projects/minute** before hitting the limit. The `--register` command handles 429 responses automatically — it reads the `Retry-After` header and waits before retrying.

| Projects | Subscriptions | Time at 50 req/min |
|----------|--------------|-------------------|
| 10 | 40 | ~48 seconds |
| 100 | 400 | ~8 minutes |
| 1,000 | 4,000 | ~80 minutes |

In practice, you'd only register on hot projects per the [tiered approach](#tiered-approach) — not the entire hub. A hub with 500 projects but only 20 actively edited ones needs just 80 subscriptions (~96 seconds).

APS also enforces a **quota of 1,000 hooks per event type per scope** — more than enough for any realistic hub size.

### How the listener works

Each event is parsed into a removal row with the old path reconstructed from the payload's `ancestors` array. Duplicate deliveries (APS retries) are deduplicated by `x-adsk-delivery-id`. The HMAC-SHA1 signature is verified when `WEBHOOK_SECRET` is set in `.env`.

It runs as an **AWS Lambda** (API Gateway POST → `lambda_handler()`) or a **local Flask dev server** (`python webhook_listener.py`) for testing with ngrok.

### Cleanup pass at backup time

`backup.py` has a placeholder (`TODO`) for a cleanup pass that would run before the sweep:

1. Read `cache/removals.pq`
2. For each entry, delete the corresponding S3 prefix (`s3://{bucket}/{hub}/{project}/{old_path}`)
3. Remove the folder from `cache.pq` (so the sweep doesn't think it's still there)
4. Truncate the removals log

The rest of the pipeline runs unchanged — the sweep detects moved items at their new location and backs them up normally.

### Why this is safe

- **S3 Bucket Versioning** makes cleanup mistakes recoverable. If a removal event is misinterpreted, the "deleted" S3 object is still retrievable as a non-current version.
- **Moves are self-correcting**: even without the cleanup pass, the file exists at the new path after the next sweep. The cleanup just removes the orphan at the old path.
- **Deletes are intentional**: if a user deleted a file in ACC, removing it from the backup mirrors their intent. S3 versioning preserves it if needed.

### Tiered approach

Not every project needs real-time webhook coverage. A practical rollout:

1. **Hot projects** (actively edited) → subscribe to webhooks, get move/delete coverage
2. **Cold projects** (rarely touched) → rollup polling only, accept orphaned paths
3. **Periodic full reconciliation** — compare S3 keys against a fresh full sweep to find and clean up any orphans missed by webhooks

Even dormant projects benefit from webhook coverage — a file deletion on a cold project does **not** update `lastModifiedTimeRollup`, so the sweep will never notice it. A webhook is the only way to catch it.

This keeps infrastructure minimal while covering the cases that matter most.

---

## Examples

Exploratory scripts in `examples/` — used during development to investigate the DM and AECDM APIs. All use `auth.py` for token generation.

| Script | Purpose |
|--------|---------|
| `scan_all_projects.py` | Scan all hubs/projects, rank by busyness score |
| `heatmap.py` | Treemap heatmap of folder activity (PNG output) |
| `probe_dm_dates.py` | Side-by-side DM vs AECDM timestamp comparison |
| `ls_project.py` | `ls -R` of an ACC project via AECDM GraphQL |
| `aecdm_client.py` | AECDM GraphQL API wrapper |
| `probe_folder_dates.py` | Test AECDM folder date fields |
| `depth_test.py` | Test AECDM query depth vs 1,000 point budget |

### AECDM vs DM API (findings)

| Capability | AECDM (GraphQL) | DM (REST) |
|------------|-----------------|-----------|
| File types listed | Revit only | All file types |
| `lastModifiedTimeRollup` | No | Yes — the key signal |
| `objectCount` | Returns -1 (broken) | Working |
| Storage URN for download | No | Yes |
| Query point budget | 1,000 per query | No limit |

**Conclusion**: AECDM is useful for Revit-specific queries but cannot replace the DM API for change detection or general file listing.

---

## Parquet Schemas

### cache/cache.pq

| Column | Type | Description |
|--------|------|-------------|
| `hub_id` | str | Hub identifier |
| `project_id` | str | Project identifier |
| `project_name` | str | Human-readable project name |
| `folder_id` | str | DM folder URN |
| `path` | str | Full folder path (`Project Files/subfolder/...`) |
| `lastModifiedTimeRollup` | str | ISO timestamp — propagates up from any child change |
| `objectCount` | int | Number of items in the folder |
| `hidden` | bool | Folder hidden in ACC UI |
| `scanned_at` | str | When this row was last scanned |

### cache/copylist.pq

| Column | Type | Description |
|--------|------|-------------|
| `hub_id` | str | Hub identifier |
| `project_id` | str | Project identifier |
| `project_name` | str | Human-readable project name |
| `folder_id` | str | Parent folder URN |
| `path` | str | Full path including filename |
| `item_id` | str | DM item lineage ID (stable across versions) |
| `file_name` | str | Display filename |
| `storage_urn` | str | `urn:adsk.objects:os.object:{bucket}/{objectKey}` |
| `size_bytes` | int | File size |
| `tip_version_id` | str | Version URN of the tip (e.g. `urn:adsk.wipprod:fs.file:vf.xxx?version=7`) |
| `version_number` | int | Human-readable version number (e.g. `7`) |
| `hidden` | bool | File hidden in ACC UI |
| `reserved` | bool | File checked out / locked by a user |
| `lastModifiedTime` | str | When the file was last modified |
| `detected_at` | str | When the sweep detected this change |

### cache/removals.pq

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | str | ISO8601 UTC when the webhook event was received |
| `event_type` | str | `dm.version.deleted`, `dm.version.moved.out`, `dm.folder.deleted`, or `dm.folder.moved.out` |
| `resource_urn` | str | URN of the deleted/moved resource |
| `resource_type` | str | `version` (file) or `folder` |
| `old_path` | str | Reconstructed path from webhook `ancestors` array (e.g. `Project Files/Structural/model.rvt`) |
| `project_id` | str | ACC project ID from webhook payload |
| `delivery_id` | str | APS delivery ID — used for deduplication across retries |
