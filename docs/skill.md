# ACC Backup Sweep System — Complete Implementation Spec

> **Purpose**: This document contains everything needed to implement a daily backup change-detection sweep across Autodesk Construction Cloud (ACC) projects. A fresh Claude Code session (or any developer) should be able to implement this in Python or Node.js from this file alone.

---

## 1. Overview

The system polls the Autodesk Data Management (DM) API to detect changed files across all projects in an ACC hub. It uses the `lastModifiedTimeRollup` timestamp — which propagates upward through the folder tree — to prune unchanged branches, minimizing API calls. Output is a copylist of changed files with storage URNs for downstream download.

### Two Output Files

| File | Purpose | Lifecycle |
|---|---|---|
| `cache.pq` | Folder state snapshot — persists across runs | Appended/merged each run |
| `copylist.pq` | Changed files detected this run | Overwritten fresh each run |

### Performance

- **Brute force**: ~100,000 API calls for 1,000 projects
- **With rollup pruning**: ~6,500 API calls on a typical day (5% dirty projects)
- **Cache-hit run** (nothing changed): 7 API calls (1 hubs + 1 list projects + 5 topFolders)

---

## 2. Authentication

Uses **Service Account Authentication (SSA)** with a JWT assertion flow to generate 3LO access tokens automatically. Based on the [official APS SSA walkthrough](https://developer.doc.autodesk.com/bPlouYTd/cloud-platform-ssa-docs-main-612785/ssa/v1/tutorials/getting-started-with-ssa/task3-generate-3-legged-access-token.html).

All DM API fields (including `lastModifiedTimeRollup`, `objectCount`, `hidden`, `reserved`) are available with SSA tokens.

### .env File

Credentials are stored in a `.env` file loaded via `python-dotenv`:

```env
APS_CLIENT_ID=your-client-id
APS_CLIENT_SECRET=your-client-secret
APS_SERVICE_ACCOUNT_ID=your-service-account-id
APS_KEY_ID=your-private-key-id
APS_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
APS_SCOPES=account:read data:read data:create data:write
```

### SSA JWT Flow (2 steps)

**Step 1 — Generate JWT assertion** (signed with RSA private key):
```python
import jwt, time

claims = {
    "iss": CLIENT_ID,                # from .env
    "sub": SERVICE_ACCOUNT_ID,       # from .env
    "aud": "https://developer.api.autodesk.com/authentication/v2/token",
    "exp": int(time.time()) + 300,   # 5 minutes
    "scope": ["data:read", "data:write"],  # array of strings
}
headers = {"alg": "RS256", "kid": KEY_ID}  # KEY_ID from .env
assertion = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers=headers)
```

**Step 2 — Exchange JWT assertion for access token**:
```
POST https://developer.api.autodesk.com/authentication/v2/token
Content-Type: application/x-www-form-urlencoded
Authorization: Basic base64(client_id:client_secret)

grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
&assertion=<jwt_assertion>
&scope=data:read data:write
```

Response:
```json
{
  "access_token": "eyJh...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### Token Caching

The token is cached in memory and reused until it expires (with a 5-minute safety margin). On expiry, a new JWT assertion is generated and exchanged automatically. No manual token management needed.

### Token Usage

All API calls use the Bearer token:
```
Authorization: Bearer <access_token>
```

A CLI argument can override the SSA flow for testing: `python sweep.py <token>`.

---

## 3. API Reference

Base URL: `https://developer.api.autodesk.com`

All DM API responses follow the [JSON:API](https://jsonapi.org/) specification with `data[]`, `attributes`, `relationships`, and `included[]` arrays.

### 3.1 GET /project/v1/hubs

**Purpose**: List all hubs the authenticated user/service account can access.

**Request**:
```
GET /project/v1/hubs
Authorization: Bearer <token>
```

**Response** (200 OK):
```json
{
  "jsonapi": { "version": "1.0" },
  "links": { "self": { "href": "..." } },
  "data": [
    {
      "type": "hubs",
      "id": "b.1234abcd-5678-efgh-ijkl-mnopqrstuvwx",
      "attributes": {
        "name": "My Hub Name",
        "region": "US",
        "extension": {
          "type": "hubs:autodesk.bim360:Account",
          "version": "1.0",
          "data": {}
        }
      },
      "relationships": {
        "projects": {
          "links": { "related": { "href": "/project/v1/hubs/b.1234.../projects" } }
        }
      }
    }
  ]
}
```

**Key fields**:
- `data[].id` — Hub ID, always prefixed with `b.` (e.g., `b.abc123...`)
- `data[].attributes.name` — Human-readable hub name

---

### 3.2 GET /project/v1/hubs/:hub_id/projects

**Purpose**: List all projects in a hub. Supports pagination.

**Request**:
```
GET /project/v1/hubs/{hub_id}/projects
Authorization: Bearer <token>
```

**Response** (200 OK):
```json
{
  "data": [
    {
      "type": "projects",
      "id": "b.63a3e38e-ff78-46d4-97f3-3b8714da05f8",
      "attributes": {
        "name": "My Project"
      },
      "relationships": {
        "rootFolder": {
          "data": { "type": "folders", "id": "urn:adsk.wipprod:fs.folder:..." }
        }
      }
    }
  ],
  "links": {
    "next": { "href": "https://developer.api.autodesk.com/project/v1/hubs/b.../projects?page[number]=1" }
  }
}
```

**Pagination**: If `links.next.href` exists, follow it to get the next page. Continue until no `next` link.

**Key fields**:
- `data[].id` — Project ID, prefixed with `b.`
- `data[].attributes.name` — Project name

---

### 3.3 GET /project/v1/hubs/:hub_id/projects/:project_id/topFolders

**Purpose**: Get top-level folders for a project. The critical entry point — contains `lastModifiedTimeRollup` on the "Project Files" root folder.

**Rate limit**: 300 requests per minute (requesting increase to 1,000).

**Request**:
```
GET /project/v1/hubs/{hub_id}/projects/{project_id}/topFolders
Authorization: Bearer <token>
```

**Response** (200 OK):
```json
{
  "data": [
    {
      "type": "folders",
      "id": "urn:adsk.wipprod:fs.folder:co.abcdef1234",
      "attributes": {
        "name": "Project Files",
        "displayName": "Project Files",
        "objectCount": 42,
        "createTime": "2024-01-15T10:30:00.000Z",
        "lastModifiedTime": "2026-02-20T14:22:33.000Z",
        "lastModifiedTimeRollup": "2026-02-25T09:15:44.000Z",
        "lastModifiedUserName": "john.doe@company.com",
        "hidden": false,
        "extension": {
          "type": "folders:autodesk.bim360:Folder",
          "version": "1.0"
        }
      },
      "relationships": {
        "contents": {
          "links": {
            "related": { "href": "/data/v1/projects/b.63a3.../folders/urn:.../contents" }
          }
        }
      }
    },
    {
      "type": "folders",
      "id": "urn:adsk.wipprod:fs.folder:co.xyz789",
      "attributes": {
        "name": "Plans",
        "objectCount": 5,
        "lastModifiedTimeRollup": "2025-12-01T08:00:00.000Z",
        "hidden": false
      }
    },
    {
      "type": "folders",
      "id": "urn:adsk.wipprod:fs.folder:co.recycle",
      "attributes": {
        "name": "Recycle Bin",
        "hidden": true
      }
    }
  ]
}
```

**Key fields on each folder**:
| Field | Type | Description |
|---|---|---|
| `id` | string | Folder URN (globally unique) |
| `attributes.name` | string | Folder name |
| `attributes.objectCount` | int | Number of direct items in folder |
| `attributes.lastModifiedTime` | string (ISO 8601) | When this folder's metadata was last changed |
| `attributes.lastModifiedTimeRollup` | string (ISO 8601) | **THE KEY FIELD** — latest modification time across this folder AND all descendants. Propagates upward. |
| `attributes.hidden` | boolean | `true` for Recycle Bin and other system folders |

**Important**: Look for the folder named `"Project Files"` — this is the root of user content. Other top folders include "Plans", "Project Health Dashboards", "Recycle Bin", etc. Only "Project Files" is needed for backup.

---

### 3.4 GET /data/v1/projects/:project_id/folders/:folder_id/contents

**Purpose**: List contents of a folder. Use `filter[type]` to get only folders or only items.

**Rate limit**: 300 requests per minute (requesting increase to 1,000).

**Request — Subfolders only**:
```
GET /data/v1/projects/{project_id}/folders/{folder_id}/contents?filter[type]=folders&page[limit]=200
Authorization: Bearer <token>
```

**Request — Items (files) only**:
```
GET /data/v1/projects/{project_id}/folders/{folder_id}/contents?filter[type]=items&page[limit]=200
Authorization: Bearer <token>
```

**Response for folders** (200 OK):
```json
{
  "data": [
    {
      "type": "folders",
      "id": "urn:adsk.wipprod:fs.folder:co.subfolder123",
      "attributes": {
        "name": "Structural",
        "objectCount": 15,
        "lastModifiedTime": "2026-02-20T12:00:00.000Z",
        "lastModifiedTimeRollup": "2026-02-25T09:15:44.000Z",
        "hidden": false
      }
    }
  ],
  "links": {
    "next": { "href": "..." }
  }
}
```

**Response for items** (200 OK) — this is the critical response for building the copylist:
```json
{
  "data": [
    {
      "type": "items",
      "id": "urn:adsk.wipprod:dm.lineage:abcdef123456",
      "attributes": {
        "displayName": "Foundation Plan.rvt",
        "createTime": "2025-06-10T08:00:00.000Z",
        "lastModifiedTime": "2026-02-24T16:30:00.000Z",
        "hidden": false,
        "reserved": false,
        "extension": {
          "type": "items:autodesk.bim360:File",
          "version": "1.0"
        }
      },
      "relationships": {
        "tip": {
          "data": {
            "type": "versions",
            "id": "urn:adsk.wipprod:fs.file:vf.tipversion123?version=3"
          }
        }
      }
    }
  ],
  "included": [
    {
      "type": "versions",
      "id": "urn:adsk.wipprod:fs.file:vf.tipversion123?version=3",
      "attributes": {
        "name": "Foundation Plan.rvt",
        "displayName": "Foundation Plan.rvt",
        "storageSize": 52428800,
        "lastModifiedTime": "2026-02-24T16:30:00.000Z",
        "fileType": "rvt"
      },
      "relationships": {
        "storage": {
          "data": {
            "type": "objects",
            "id": "urn:adsk.objects:os.object:wip.dm.prod/971a36c1-a05e-4c3f-b26e-abcdef123456.rvt"
          }
        }
      }
    }
  ],
  "links": {
    "next": { "href": "..." }
  }
}
```

**Extracting storage URN** (critical for downstream download):
```
item → relationships.tip.data.id (version URN)
  → look up in included[] array by id
    → included[match].relationships.storage.data.id (storage URN)
    → included[match].attributes.storageSize (file size in bytes)
```

The storage URN format is: `urn:adsk.objects:os.object:{bucketKey}/{objectKey}`

**Key item fields**:
| Field | Type | Description |
|---|---|---|
| `data[].id` | string | Item lineage URN |
| `data[].attributes.displayName` | string | Filename |
| `data[].attributes.lastModifiedTime` | string | When file was last modified |
| `data[].attributes.hidden` | boolean | True if in recycle bin |
| `data[].attributes.reserved` | boolean | True if file is checked out |
| `included[].attributes.storageSize` | int | File size in bytes |
| `included[].relationships.storage.data.id` | string | Storage URN for download |

**Pagination**: Follow `links.next.href` until absent. Max 200 items per page (`page[limit]=200`).

---

### 3.5 Signed S3 Download (for downstream file download)

Once you have a storage URN, parse it and get a signed S3 URL for direct download.

**Parse storage URN**:
```
urn:adsk.objects:os.object:wip.dm.prod/971a36c1-a05e-4c3f-b26e-abcdef123456.rvt
                           ^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                           bucketKey    objectKey
```

**Single file**:
```
GET /oss/v2/buckets/{bucketKey}/objects/{objectKey}/signeds3download
Authorization: Bearer <token>
```

**Batch (preferred)**:
```
POST /oss/v2/buckets/{bucketKey}/objects/batchsigneds3download
Authorization: Bearer <token>
Content-Type: application/json

{
  "requests": [
    { "objectKey": "971a36c1-a05e-4c3f-b26e-abcdef123456.rvt" },
    { "objectKey": "another-file-key.dwg" }
  ]
}
```

**Response**:
```json
{
  "results": {
    "971a36c1-a05e-4c3f-b26e-abcdef123456.rvt": {
      "status": "complete",
      "url": "https://s3.amazonaws.com/com.autodesk.oss-persistent/...<signed>",
      "size": 52428800,
      "sha1": "a1b2c3d4e5f6..."
    }
  }
}
```

**Download**: `GET {url}` — direct to S3, no Authorization header needed, no APS rate limits. Supports `Range` headers for chunked downloads.

**Rate limit**: 1,000 rpm shared across all OSS endpoints (requesting increase to 3,000).

---

## 4. Data Schemas

### cache.pq — Folder State Snapshot

| Column | Type | Description |
|---|---|---|
| `hub_id` | str | Hub ID (e.g., `b.abc123...`) |
| `project_id` | str | Project ID (e.g., `b.63a3e38e-...`) |
| `project_name` | str | Human-readable project name |
| `folder_id` | str | Folder URN (globally unique) |
| `path` | str | Full path from root (e.g., `Project Files/Structural/Plans`) |
| `lastModifiedTimeRollup` | str | ISO timestamp — the rollup value at scan time |
| `objectCount` | int | Number of items in this folder |
| `hidden` | bool | Whether folder is hidden (Recycle Bin, etc.) |
| `scanned_at` | str | ISO timestamp of when this row was recorded |

### copylist.pq — Changed Files This Run

| Column | Type | Description |
|---|---|---|
| `hub_id` | str | Hub ID |
| `project_id` | str | Project ID |
| `project_name` | str | Project name |
| `folder_id` | str | Parent folder URN |
| `path` | str | Full path including filename |
| `item_id` | str | DM item lineage URN |
| `file_name` | str | Just the filename |
| `storage_urn` | str | `urn:adsk.objects:os.object:{bucket}/{key}` |
| `size_bytes` | int | File size in bytes |
| `hidden` | bool | True if item is in recycle bin |
| `reserved` | bool | True if file is checked out by a user |
| `lastModifiedTime` | str | ISO timestamp of last file modification |
| `detected_at` | str | ISO timestamp of when this sweep found it |

---

## 5. Algorithm

### Phase 0 — Screen Projects

```
load cache.pq (or empty dataframe if first run)
GET /project/v1/hubs → find hub by name or ID
GET /project/v1/hubs/{hub_id}/projects → list all projects (paginate)

dirty_projects = []
for each project:
    GET /project/v1/hubs/{hub_id}/projects/{project_id}/topFolders
    root = find folder where name == "Project Files"
    if root is None: skip

    live_rollup = root.attributes.lastModifiedTimeRollup
    cached_rollup = cache_lookup(root.id)

    if live_rollup == cached_rollup:
        skip (project is clean)
    else:
        dirty_projects.append(project, root.id, live_rollup)
```

**Cost**: 1 API call per project + 2 overhead (hubs + list projects).

### Phase 1 — Walk Dirty Branches

For each dirty project, recursively walk the folder tree. At each level, compare the subfolder's `lastModifiedTimeRollup` against the cached value. Only descend into dirty subfolders.

```
function walk_folder(project_id, folder_id, path, cache, folder_rows, file_rows):
    GET /data/v1/projects/{project_id}/folders/{folder_id}/contents?filter[type]=folders&page[limit]=200

    for each subfolder in response.data:
        record subfolder in folder_rows (for cache update)

        live_rollup = subfolder.attributes.lastModifiedTimeRollup
        cached_rollup = cache_lookup(subfolder.id)

        if live_rollup == cached_rollup:
            continue  // prune — unchanged branch

        // dirty subfolder — recurse
        walk_folder(project_id, subfolder.id, path + "/" + subfolder.name, ...)

    // this folder is dirty — fetch its file items
    fetch_files(project_id, folder_id, path, file_rows)
```

### Phase 2 — Build Copylist

Called by `walk_folder` at each dirty folder level:

```
function fetch_files(project_id, folder_id, path, file_rows):
    GET /data/v1/projects/{project_id}/folders/{folder_id}/contents?filter[type]=items&page[limit]=200

    // Build lookup: version_id → included object
    included_map = {}
    for each obj in response.included:
        included_map[obj.id] = obj

    for each item in response.data where type == "items":
        file_name = item.attributes.displayName
        tip_version_id = item.relationships.tip.data.id
        tip = included_map[tip_version_id]
        storage_urn = tip.relationships.storage.data.id
        size_bytes = tip.attributes.storageSize

        file_rows.append({
            hub_id, project_id, project_name, folder_id,
            path: path + "/" + file_name,
            item_id: item.id,
            file_name,
            storage_urn,
            size_bytes,
            hidden: item.attributes.hidden,
            reserved: item.attributes.reserved,
            lastModifiedTime: item.attributes.lastModifiedTime,
            detected_at: now()
        })

    // paginate: follow links.next.href until absent
```

### Phase 3 — Persist

```
// Merge cache: keep old rows for clean projects, replace rows for scanned folders
scanned_folder_ids = set(new_folder_rows.folder_id)
kept_from_old = old_cache where folder_id NOT IN scanned_folder_ids
merged_cache = concat(kept_from_old, new_folder_rows)
write merged_cache → cache.pq

// Copylist: fresh each run
write file_rows → copylist.pq

// Print summary
```

---

## 6. Known Limitations

### Deletion Gap

`lastModifiedTimeRollup` does **NOT** propagate when folders or files are deleted:
- Deleting a folder does not update ancestor rollup timestamps
- Deleting a file does not reliably update parent folder rollup
- Deleted items get `hidden=true` but rollup-based pruning may skip them

### Mitigation: Hybrid Strategy

1. **Absent-child detection** (zero extra API calls): When visiting a folder, compare its children against cached children. Any cached child not present in the live response has been deleted.

2. **Delete-only webhooks** for active projects:
   - Register webhooks for `dm.folder.deleted` and `dm.version.deleted` events
   - Only register for "hot" projects (rollup changed in last 30 days) — ~15% of total
   - Webhook listener appends delete events to a local `deletes.pq` file
   - Sweep script merges deletes on each run

3. **Periodic full reconciliation**: Weekly `--full` flag that ignores cache and scans everything as a safety net.

### Webhook Event Payloads

**`dm.folder.deleted`**:
```json
{
  "version": "1.0",
  "resourceUrn": "urn:adsk.wipprod:fs.folder:co.abcdef123456",
  "hook": {
    "hookId": "...",
    "tenant": "...",
    "event": "dm.folder.deleted"
  },
  "payload": {
    "ext": { "type": "folders:autodesk.bim360:Folder" },
    "name": "Deleted Folder Name",
    "ancestors": [
      { "name": "Project Files", "urn": "urn:adsk.wipprod:fs.folder:co.root123" }
    ],
    "project": "b.63a3e38e-ff78-46d4-97f3-3b8714da05f8",
    "tenant": "b.abc123..."
  }
}
```

**`dm.version.deleted`**:
```json
{
  "version": "1.0",
  "resourceUrn": "urn:adsk.wipprod:fs.file:vf.version123?version=3",
  "hook": {
    "hookId": "...",
    "tenant": "...",
    "event": "dm.version.deleted"
  },
  "payload": {
    "ext": { "type": "versions:autodesk.bim360:File" },
    "name": "deleted-file.rvt",
    "lineageUrn": "urn:adsk.wipprod:dm.lineage:abcdef123456",
    "ancestors": [
      { "name": "Structural", "urn": "urn:adsk.wipprod:fs.folder:co.parent123" }
    ],
    "project": "b.63a3e38e-...",
    "tenant": "b.abc123..."
  }
}
```

---

## 7. Rate Limits

| Endpoint | Current Limit | Requested |
|---|---|---|
| `GET topFolders` | 300 rpm | 1,000 rpm |
| `GET folders/:fid/contents` | 300 rpm | 1,000 rpm |
| OSS signed download (all endpoints) | 1,000 rpm shared | 3,000 rpm |

Limits are per `client_id` per endpoint (global to the app, not per-project).

When hitting rate limits, the API returns HTTP 429. Implement exponential backoff with jitter.

---

## 8. CLI Interface

```
python sweep.py                                    # default hub, token from .env SSA flow
python sweep.py --hub "My Hub Name"                # by hub name
python sweep.py --hub b.abc123                     # by hub ID
python sweep.py <token>                            # override with manual token
```

Token is generated automatically from `.env` credentials via SSA JWT flow. A manual token can be passed as a positional argument for testing.

---

## 9. Dependencies

### Python
- `requests` — HTTP client
- `pandas` + `pyarrow` — Parquet I/O and dataframe operations
- `PyJWT` + `cryptography` — JWT assertion generation (RS256 signing)
- `python-dotenv` — Load `.env` credentials

### Node.js (equivalent)
- `axios` or `node-fetch` — HTTP client
- `parquet-wasm` or `apache-arrow` — Parquet I/O (or use JSON/SQLite for simpler persistence)
- `jsonwebtoken` — JWT assertion generation
- `dotenv` — Load `.env` credentials

---

## 10. Error Handling

- **401 Unauthorized**: Token expired. Re-authenticate and retry.
- **403 Forbidden**: No access to this project. Log and skip.
- **404 Not Found**: Project or folder deleted. Log and skip.
- **429 Too Many Requests**: Rate limited. Back off exponentially (1s, 2s, 4s, 8s...) with jitter.
- **5xx Server Error**: Transient. Retry up to 3 times with backoff.
- **Network timeout**: Use 30-second timeout. Retry once.

---

## 11. Testing Checklist

1. **First run** (no cache): Creates both `.pq` files, scans all projects
2. **Immediate re-run**: All projects clean, copylist empty, minimal API calls
3. **Delete cache, re-run**: Should produce same results as first run
4. **Check for duplicates**: `copylist.pq` should have unique `(item_id, path)` pairs
5. **Verify storage URNs**: All non-empty `storage_urn` values should match pattern `urn:adsk.objects:os.object:*/*`
6. **SSA token**: All fields populated identically to 3LO token
7. **Hidden/reserved**: At least some items may have `reserved=True` (checked out files)
