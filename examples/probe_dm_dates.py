#!/usr/bin/env python3
"""
Compare folder date signals: DM API vs AECDM API side-by-side.

Usage:
    python probe_dm_dates.py              # uses SSA 3LO token via auth.py
    python probe_dm_dates.py <TOKEN>      # explicit token
"""

import sys, pathlib, requests
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from auth import load_token
from aecdm_client import _post as gql_post, get_hubs as aecdm_get_hubs, get_projects as aecdm_get_projects

DM_PROJECT_ID = "63a3e38e-ff78-46d4-97f3-3b8714da05f8"
DM_BASE = "https://developer.api.autodesk.com"

TOKEN = load_token()


def dm_get(path):
    r = requests.get(
        f"{DM_BASE}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30,
    )
    if not r.ok:
        print(f"  !! DM HTTP {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return r.json()


# ── Step 1: Find hub via DM API ──────────────────────────────────────────────

print("Finding hub via DM API...")
hubs = dm_get("/project/v1/hubs")["data"]
hub_id = None
for h in hubs:
    try:
        top = dm_get(f"/project/v1/hubs/{h['id']}/projects/b.{DM_PROJECT_ID}/topFolders")
        hub_id = h["id"]
        hub_name = h["attributes"]["name"]
        print(f"  Hub: {hub_name} ({hub_id})")
        break
    except Exception:
        continue

if not hub_id:
    print("ERROR: could not find hub for project")
    sys.exit(1)


# ── Step 2: DM API — top folders with date fields ───────────────────────────

print(f"\n{'='*70}")
print("DM API — top folders")
print(f"{'='*70}")

dm_folders = top["data"]
dm_by_name = {}
for f in dm_folders:
    a = f["attributes"]
    name = a["name"]
    dm_by_name[name] = a
    print(f"\n📁 {name}")
    print(f"   objectCount:           {a.get('objectCount')}")
    print(f"   createTime:            {a.get('createTime')}")
    print(f"   lastModifiedTime:      {a.get('lastModifiedTime')}")
    print(f"   lastModifiedTimeRollup:{a.get('lastModifiedTimeRollup')}")
    print(f"   lastModifiedUserName:  {a.get('lastModifiedUserName')}")
    print(f"   hidden:                {a.get('hidden')}")


# ── Step 2b: DM API — drill into "Project Files" subfolders ─────────────────

# Find the "Project Files" folder ID
pf_folder_id = None
for f in dm_folders:
    if f["attributes"]["name"] == "Project Files":
        pf_folder_id = f["id"]
        break

dm_by_name = {}
if pf_folder_id:
    print(f"\n{'='*70}")
    print("DM API — subfolders of 'Project Files'")
    print(f"{'='*70}")

    contents = dm_get(f"/data/v1/projects/b.{DM_PROJECT_ID}/folders/{pf_folder_id}/contents")
    for item in contents["data"]:
        if item["type"] != "folders":
            continue
        a = item["attributes"]
        name = a["name"]
        dm_by_name[name] = a
        print(f"\n📁 {name}")
        print(f"   objectCount:           {a.get('objectCount')}")
        print(f"   lastModifiedTime:      {a.get('lastModifiedTime')}")
        print(f"   lastModifiedTimeRollup:{a.get('lastModifiedTimeRollup')}")
        print(f"   hidden:                {a.get('hidden')}")

    # also show non-folder items (files) to prove DM lists all types
    file_items = [i for i in contents["data"] if i["type"] == "items"]
    if file_items:
        print(f"\n  (also {len(file_items)} file items at this level)")


# ── Step 3: AECDM API — same folders ────────────────────────────────────────

print(f"\n\n{'='*70}")
print("AECDM API — top folders (= subfolders of Project Files)")
print(f"{'='*70}")

# find AECDM project id
aecdm_project_id = None
for hub in aecdm_get_hubs(TOKEN):
    for proj in aecdm_get_projects(TOKEN, hub["id"]):
        dm = (proj.get("alternativeIdentifiers") or {}).get("dataManagementAPIProjectId", "")
        if dm in {DM_PROJECT_ID, f"b.{DM_PROJECT_ID}"}:
            aecdm_project_id = proj["id"]
            break
    if aecdm_project_id:
        break

q = """
query ($projectId: ID!) {
  foldersByProject(projectId: $projectId, pagination: {limit: 50}) {
    results {
      id name objectCount createdOn lastModifiedOn
      lastModifiedBy { userName }
    }
  }
}"""
aecdm_folders = gql_post(TOKEN, q, {"projectId": aecdm_project_id})["foldersByProject"]["results"]

aecdm_by_name = {}
for f in aecdm_folders:
    aecdm_by_name[f["name"]] = f
    print(f"\n📁 {f['name']}")
    print(f"   objectCount:    {f.get('objectCount')}")
    print(f"   createdOn:      {f.get('createdOn')}")
    print(f"   lastModifiedOn: {f.get('lastModifiedOn')}")
    print(f"   lastModifiedBy: {(f.get('lastModifiedBy') or {}).get('userName')}")


# ── Step 4: Side-by-side comparison ─────────────────────────────────────────

print(f"\n\n{'='*70}")
print("COMPARISON: lastModifiedTime (DM) vs lastModifiedOn (AECDM)")
print(f"{'='*70}")
print(f"{'Folder':<30} {'DM lastModified':<28} {'DM Rollup':<28} {'AECDM lastModified':<28}")
print("-" * 114)

all_names = sorted(set(list(dm_by_name.keys()) + list(aecdm_by_name.keys())))
for name in all_names:
    dm_mod = (dm_by_name.get(name) or {}).get("lastModifiedTime", "—")
    dm_roll = (dm_by_name.get(name) or {}).get("lastModifiedTimeRollup", "—")
    aecdm_mod = (aecdm_by_name.get(name) or {}).get("lastModifiedOn", "—")
    print(f"{name:<30} {str(dm_mod):<28} {str(dm_roll):<28} {str(aecdm_mod):<28}")
