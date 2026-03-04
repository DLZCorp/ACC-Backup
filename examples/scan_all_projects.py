#!/usr/bin/env python3
"""Scan all hubs/projects for the busiest one based on topFolder rollup timestamps."""

import sys, pathlib, requests
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from auth import load_token
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TOKEN = load_token()
BASE = "https://developer.api.autodesk.com"
NOW = datetime.now(timezone.utc)


def dm_get(path):
    try:
        r = requests.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        if not r.ok:
            return None
        return r.json()
    except Exception:
        return None


def days_ago(iso_str):
    if not iso_str:
        return 9999
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (NOW - dt).total_seconds() / 86400


def scan_project(hub_id, hub_name, proj):
    pid = proj["id"]
    pname = proj["attributes"]["name"]
    top = dm_get(f"/project/v1/hubs/{hub_id}/projects/{pid}/topFolders")
    if not top:
        return None

    # Find "Project Files" root
    pf = None
    for f in top.get("data", []):
        if f["attributes"].get("name") == "Project Files":
            pf = f
            break
    if not pf:
        # take first folder
        pf = top["data"][0] if top.get("data") else None
    if not pf:
        return None

    a = pf["attributes"]
    rollup = a.get("lastModifiedTimeRollup")
    obj_count = a.get("objectCount", 0) or 0

    # Also get subfolder count + their rollups
    fid = pf["id"]
    raw_pid = pid.replace("b.", "")
    contents = dm_get(f"/data/v1/projects/{pid}/folders/{fid}/contents?filter[type]=folders&page[limit]=200")
    subfolder_count = 0
    total_objects = obj_count
    recent_folders = 0
    if contents:
        for item in contents.get("data", []):
            if item["type"] == "folders":
                subfolder_count += 1
                sa = item["attributes"]
                total_objects += sa.get("objectCount", 0) or 0
                sr = sa.get("lastModifiedTimeRollup")
                if sr and days_ago(sr) < 30:
                    recent_folders += 1

    return {
        "hub": hub_name,
        "hub_id": hub_id,
        "project": pname,
        "project_id": pid,
        "raw_project_id": raw_pid,
        "root_rollup": rollup,
        "root_rollup_days": days_ago(rollup),
        "root_objects": obj_count,
        "subfolder_count": subfolder_count,
        "total_objects": total_objects,
        "recent_folders_30d": recent_folders,
    }


# ── Scan ─────────────────────────────────────────────────────────────────────

print("Fetching hubs...")
hubs_resp = dm_get("/project/v1/hubs")
if not hubs_resp:
    print("ERROR: Failed to fetch hubs. Token may be expired.")
    import sys; sys.exit(1)
hubs = hubs_resp["data"]
print(f"  Found {len(hubs)} hubs\n")

# Collect all projects
all_projects = []
for h in hubs:
    hid = h["id"]
    hname = h["attributes"]["name"]
    pdata = dm_get(f"/project/v1/hubs/{hid}/projects")
    if not pdata:
        continue
    projects = pdata.get("data", [])
    # handle pagination
    while pdata.get("links", {}).get("next", {}).get("href"):
        pdata = dm_get(pdata["links"]["next"]["href"].replace(BASE, ""))
        if pdata:
            projects.extend(pdata.get("data", []))
        else:
            break
    for p in projects:
        all_projects.append((hid, hname, p))
    print(f"  {hname}: {len(projects)} projects")

print(f"\nTotal: {len(all_projects)} projects across {len(hubs)} hubs")
print("Scanning topFolders (this may take a few minutes)...\n")

# Scan in parallel (respect rate limits - modest concurrency)
results = []
with ThreadPoolExecutor(max_workers=5) as pool:
    futures = {pool.submit(scan_project, hid, hname, p): (hname, p["attributes"]["name"])
               for hid, hname, p in all_projects}
    done = 0
    for fut in as_completed(futures):
        done += 1
        if done % 20 == 0:
            print(f"  ...scanned {done}/{len(futures)}")
        r = fut.result()
        if r:
            results.append(r)

# ── Rank by "busyness" ───────────────────────────────────────────────────────
# Score = recent_folders_30d * 10 + total_objects + (1000 if rollup < 7d)
for r in results:
    r["score"] = (r["recent_folders_30d"] * 10
                  + r["total_objects"]
                  + (1000 if r["root_rollup_days"] < 7 else 0)
                  + (500 if r["root_rollup_days"] < 30 else 0))

results.sort(key=lambda r: -r["score"])

print(f"\n{'='*100}")
print(f"TOP 20 BUSIEST PROJECTS (by activity score)")
print(f"{'='*100}")
print(f"{'#':<4} {'Score':<7} {'Rollup':<8} {'Folders':<8} {'Objects':<9} {'Hot30d':<7} {'Hub':<30} {'Project':<30} {'ID'}")
print("-" * 140)

for i, r in enumerate(results[:20]):
    d = r["root_rollup_days"]
    rollup_str = f"{int(d)}d" if d < 365 else "1y+"
    print(f"{i+1:<4} {r['score']:<7} {rollup_str:<8} {r['subfolder_count']:<8} {r['total_objects']:<9} {r['recent_folders_30d']:<7} {r['hub'][:29]:<30} {r['project'][:29]:<30} {r['raw_project_id']}")

print(f"\n{'='*100}")
print("To generate heatmap for any project:")
print("  python heatmap.py <raw_project_id>")
