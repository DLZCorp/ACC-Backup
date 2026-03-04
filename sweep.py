#!/usr/bin/env python3
"""
Hub-level folder sweep with parquet cache + copylist.

Scans all projects in a hub using DM API lastModifiedTimeRollup
for change detection. Prunes unchanged branches. Produces:
  - cache.pq   — folder state snapshot (persists across runs)
  - copylist.pq — changed files detected this run

Usage:
    python sweep.py                                    # default hub
    python sweep.py --hub "Developer Advocacy Support"  # by name
    python sweep.py --hub b.abc123                      # by ID
"""

import argparse, pathlib, requests
from datetime import datetime, timezone
from auth import load_token
import pandas as pd

DM_BASE = "https://developer.api.autodesk.com"
HERE = pathlib.Path(__file__).parent
CACHE_FILE = HERE / "cache" / "cache.pq"
COPYLIST_FILE = HERE / "cache" / "copylist.pq"

TOKEN = load_token()
NOW = datetime.now(timezone.utc).isoformat()

# ── Stats ────────────────────────────────────────────────────────────────────
stats = {"projects_screened": 0, "projects_dirty": 0,
         "folders_walked": 0, "api_calls": 0, "files_found": 0}


def dm_get(path):
    stats["api_calls"] += 1
    r = requests.get(f"{DM_BASE}{path}",
                     headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    if not r.ok:
        print(f"  !! DM HTTP {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return r.json()


# ── Load cache ───────────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        return pd.read_parquet(CACHE_FILE)
    return pd.DataFrame(columns=[
        "hub_id", "project_id", "project_name", "folder_id", "path",
        "lastModifiedTimeRollup", "objectCount", "hidden", "scanned_at",
    ])


def cache_lookup(cache_df, folder_id):
    """Return cached rollup for a folder_id, or None."""
    rows = cache_df.loc[cache_df["folder_id"] == folder_id, "lastModifiedTimeRollup"]
    if len(rows) > 0:
        return rows.iloc[0]
    return None


# ── Hub / project discovery ──────────────────────────────────────────────────

def find_hub(hub_filter):
    """Find hub by name or ID. Returns (hub_id, hub_name)."""
    hubs = dm_get("/project/v1/hubs")["data"]
    for h in hubs:
        hid = h["id"]
        hname = h["attributes"]["name"]
        if hub_filter in (hid, hname):
            return hid, hname
    # fallback: partial match
    for h in hubs:
        if hub_filter.lower() in h["attributes"]["name"].lower():
            return h["id"], h["attributes"]["name"]
    print(f"ERROR: hub '{hub_filter}' not found. Available:")
    for h in hubs:
        print(f"  {h['id']}  {h['attributes']['name']}")
    raise SystemExit(1)


def list_projects(hub_id):
    """Return all projects in a hub (handles pagination)."""
    resp = dm_get(f"/project/v1/hubs/{hub_id}/projects")
    projects = resp.get("data", [])
    while resp.get("links", {}).get("next", {}).get("href"):
        resp = dm_get(resp["links"]["next"]["href"].replace(DM_BASE, ""))
        projects.extend(resp.get("data", []))
    return projects


# ── Phase 0: Screen projects ────────────────────────────────────────────────

def screen_projects(hub_id, projects, cache_df):
    """Return list of (project_id, project_name, root_folder_id, root_rollup)
    for projects whose root rollup changed vs cache."""
    dirty = []
    for p in projects:
        pid = p["id"]
        pname = p["attributes"]["name"]
        stats["projects_screened"] += 1
        try:
            top = dm_get(f"/project/v1/hubs/{hub_id}/projects/{pid}/topFolders")
        except Exception as e:
            print(f"  SKIP {pname}: {e}")
            continue

        # Find "Project Files" root
        root = None
        for f in top.get("data", []):
            if f["attributes"].get("name") == "Project Files":
                root = f
                break
        if not root:
            continue

        fid = root["id"]
        rollup = root["attributes"].get("lastModifiedTimeRollup")
        cached_rollup = cache_lookup(cache_df, fid)

        if rollup and rollup == cached_rollup:
            print(f"  CLEAN  {pname}")
            continue

        print(f"  DIRTY  {pname}  (cached={cached_rollup}, live={rollup})")
        stats["projects_dirty"] += 1
        dirty.append((pid, pname, fid, rollup))

    return dirty


# ── Phase 1+2: Walk dirty branches + build copylist ─────────────────────────

def walk_folder(hub_id, project_id, project_name, folder_id, path,
                cache_df, folder_rows, file_rows, max_depth=10, depth=0):
    """Recursively walk folder tree. Collect folder state + changed files."""

    # Get subfolders
    contents_url = (f"/data/v1/projects/{project_id}/folders/{folder_id}"
                    f"/contents?filter[type]=folders&page[limit]=200")
    try:
        contents = dm_get(contents_url)
    except Exception as e:
        print(f"    ERR walking {path}: {e}")
        return

    has_dirty_subfolder = False
    for item in contents.get("data", []):
        if item["type"] != "folders":
            continue
        a = item["attributes"]
        name = a.get("name", "?")
        fid = item["id"]
        sub_path = f"{path}/{name}"
        rollup = a.get("lastModifiedTimeRollup")
        obj_count = a.get("objectCount", 0) or 0

        stats["folders_walked"] += 1

        # Record folder state for cache
        folder_rows.append({
            "hub_id": hub_id,
            "project_id": project_id,
            "project_name": project_name,
            "folder_id": fid,
            "path": sub_path,
            "lastModifiedTimeRollup": rollup or "",
            "objectCount": obj_count,
            "hidden": a.get("hidden", False),
            "scanned_at": NOW,
        })

        cached_rollup = cache_lookup(cache_df, fid)
        if rollup and rollup == cached_rollup:
            continue  # prune — unchanged branch

        has_dirty_subfolder = True

        # Recurse into dirty subfolder
        if depth < max_depth:
            walk_folder(hub_id, project_id, project_name, fid, sub_path,
                        cache_df, folder_rows, file_rows, max_depth, depth + 1)

    # If this folder is dirty (we were called because parent was dirty),
    # fetch file items to build the copylist
    fetch_files(hub_id, project_id, project_name, folder_id, path, file_rows)


def fetch_files(hub_id, project_id, project_name, folder_id, path, file_rows):
    """Fetch file items from a folder and extract storage URNs."""
    items_url = (f"/data/v1/projects/{project_id}/folders/{folder_id}"
                 f"/contents?filter[type]=items&page[limit]=200")
    try:
        resp = dm_get(items_url)
    except Exception as e:
        print(f"    ERR fetching files {path}: {e}")
        return

    # Build lookup of included storage objects
    included = {}
    for inc in resp.get("included", []):
        included[inc["id"]] = inc

    for item in resp.get("data", []):
        if item["type"] != "items":
            continue
        a = item["attributes"]
        file_name = a.get("displayName", a.get("name", "?"))

        # Get tip version storage from included
        tip_id = (item.get("relationships", {})
                  .get("tip", {}).get("data", {}).get("id"))
        storage_urn = ""
        size_bytes = 0
        version_number = 0
        if tip_id and tip_id in included:
            tip = included[tip_id]
            storage_data = (tip.get("relationships", {})
                           .get("storage", {}).get("data", {}))
            storage_urn = storage_data.get("id", "")
            size_bytes = tip.get("attributes", {}).get("storageSize", 0) or 0
            version_number = tip.get("attributes", {}).get("versionNumber", 0) or 0

        file_rows.append({
            "hub_id": hub_id,
            "project_id": project_id,
            "project_name": project_name,
            "folder_id": folder_id,
            "path": f"{path}/{file_name}",
            "item_id": item["id"],
            "file_name": file_name,
            "storage_urn": storage_urn,
            "size_bytes": size_bytes,
            "tip_version_id": tip_id or "",
            "version_number": version_number,
            "hidden": a.get("hidden", False),
            "reserved": a.get("reserved", False),
            "lastModifiedTime": a.get("lastModifiedTime", ""),
            "detected_at": NOW,
        })
        stats["files_found"] += 1

    # Handle pagination
    while resp.get("links", {}).get("next", {}).get("href"):
        resp = dm_get(resp["links"]["next"]["href"].replace(DM_BASE, ""))
        for inc in resp.get("included", []):
            included[inc["id"]] = inc
        for item in resp.get("data", []):
            if item["type"] != "items":
                continue
            a = item["attributes"]
            file_name = a.get("displayName", a.get("name", "?"))
            tip_id = (item.get("relationships", {})
                      .get("tip", {}).get("data", {}).get("id"))
            storage_urn = ""
            size_bytes = 0
            version_number = 0
            if tip_id and tip_id in included:
                tip = included[tip_id]
                storage_data = (tip.get("relationships", {})
                                .get("storage", {}).get("data", {}))
                storage_urn = storage_data.get("id", "")
                size_bytes = tip.get("attributes", {}).get("storageSize", 0) or 0
                version_number = tip.get("attributes", {}).get("versionNumber", 0) or 0
            file_rows.append({
                "hub_id": hub_id,
                "project_id": project_id,
                "project_name": project_name,
                "folder_id": folder_id,
                "path": f"{path}/{file_name}",
                "item_id": item["id"],
                "file_name": file_name,
                "storage_urn": storage_urn,
                "size_bytes": size_bytes,
                "tip_version_id": tip_id or "",
                "version_number": version_number,
                "hidden": a.get("hidden", False),
                "reserved": a.get("reserved", False),
                "lastModifiedTime": a.get("lastModifiedTime", ""),
                "detected_at": NOW,
            })
            stats["files_found"] += 1


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hub folder sweep → cache.pq + copylist.pq")
    parser.add_argument("--hub", default="Developer Advocacy Support",
                        help="Hub name or ID (default: Developer Advocacy Support)")
    args = parser.parse_args()

    # Find hub
    print(f"Finding hub '{args.hub}'...")
    hub_id, hub_name = find_hub(args.hub)
    print(f"  Hub: {hub_name} ({hub_id})")

    # Load cache
    cache_df = load_cache()
    print(f"  Cache: {len(cache_df)} folder rows loaded")

    # List projects
    print(f"\nListing projects...")
    projects = list_projects(hub_id)
    print(f"  Found {len(projects)} projects")

    # Phase 0: Screen
    print(f"\nPhase 0: Screening projects...")
    dirty = screen_projects(hub_id, projects, cache_df)
    if not dirty:
        print("\nAll projects clean. Nothing to do.")
        # Still write empty copylist
        pd.DataFrame(columns=[
            "hub_id", "project_id", "project_name", "folder_id", "path",
            "item_id", "file_name", "storage_urn", "size_bytes",
            "tip_version_id", "version_number",
            "hidden", "reserved", "lastModifiedTime", "detected_at",
        ]).to_parquet(COPYLIST_FILE)
        print(f"  Wrote empty {COPYLIST_FILE.name}")
        return

    # Phase 1+2: Walk dirty projects
    folder_rows = []
    file_rows = []

    for pid, pname, root_fid, root_rollup in dirty:
        print(f"\nWalking: {pname}...")

        # Record root folder in cache
        folder_rows.append({
            "hub_id": hub_id,
            "project_id": pid,
            "project_name": pname,
            "folder_id": root_fid,
            "path": "Project Files",
            "lastModifiedTimeRollup": root_rollup or "",
            "objectCount": 0,
            "hidden": False,
            "scanned_at": NOW,
        })

        # Walk tree (walk_folder fetches files at each level including root)
        walk_folder(hub_id, pid, pname, root_fid, "Project Files",
                    cache_df, folder_rows, file_rows)

    # Phase 3: Persist
    print(f"\nPhase 3: Persisting...")

    # Merge cache: keep unchanged rows, replace scanned folders
    new_cache = pd.DataFrame(folder_rows)
    if not cache_df.empty:
        scanned_fids = set(new_cache["folder_id"])
        # Keep old rows for folders we didn't re-scan (clean projects)
        kept = cache_df[~cache_df["folder_id"].isin(scanned_fids)]
        merged = pd.concat([kept, new_cache], ignore_index=True)
    else:
        merged = new_cache

    merged.to_parquet(CACHE_FILE)
    print(f"  cache.pq: {len(merged)} folder rows")

    copylist = pd.DataFrame(file_rows)
    copylist.to_parquet(COPYLIST_FILE)
    print(f"  copylist.pq: {len(copylist)} file rows")

    # Summary
    print(f"\n{'='*60}")
    print(f"SWEEP SUMMARY")
    print(f"{'='*60}")
    print(f"  Hub:              {hub_name}")
    print(f"  Projects screened: {stats['projects_screened']}")
    print(f"  Projects dirty:    {stats['projects_dirty']}")
    print(f"  Folders walked:    {stats['folders_walked']}")
    print(f"  Files in copylist: {stats['files_found']}")
    print(f"  API calls:         {stats['api_calls']}")
    print(f"  Cache file:        {CACHE_FILE}")
    print(f"  Copylist file:     {COPYLIST_FILE}")


if __name__ == "__main__":
    main()
