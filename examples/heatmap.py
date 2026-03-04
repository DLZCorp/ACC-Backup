#!/usr/bin/env python3
"""
Heatmap treemap of ACC project folder activity.

Renders a PNG where:
  - Each rectangle = a folder
  - Size = objectCount (number of items)
  - Color = recency of lastModifiedTimeRollup (red=hot/recent, blue=cold/stale)

Usage:
    python heatmap.py [PROJECT_ID]     # uses SSA 3LO token via auth.py
"""

import sys, pathlib, requests
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from auth import load_token
from datetime import datetime, timezone
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import squarify

DM_PROJECT_ID = sys.argv[1] if len(sys.argv) > 1 else "63a3e38e-ff78-46d4-97f3-3b8714da05f8"
DM_BASE = "https://developer.api.autodesk.com"
OUT_FILE = pathlib.Path(__file__).parent / "heatmap.png"

TOKEN = load_token()
NOW = datetime.now(timezone.utc)


def dm_get(path):
    r = requests.get(f"{DM_BASE}{path}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def days_ago(iso_str):
    """Convert ISO timestamp to days-ago float."""
    if not iso_str:
        return 999
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (NOW - dt).total_seconds() / 86400


def collect_folders(project_id, folder_id, prefix="", depth=0, max_depth=3):
    """Recursively collect folder metadata via DM API."""
    contents = dm_get(f"/data/v1/projects/{project_id}/folders/{folder_id}/contents")
    folders = []
    for item in contents["data"]:
        a = item["attributes"]
        name = a.get("name", "?")
        path = f"{prefix}/{name}" if prefix else name

        if item["type"] == "folders":
            obj_count = a.get("objectCount", 0) or 0
            rollup = a.get("lastModifiedTimeRollup")
            mod_time = a.get("lastModifiedTime")
            folders.append({
                "id": item["id"],
                "name": name,
                "path": path,
                "objectCount": max(obj_count, 1),  # min 1 for treemap sizing
                "rollup": rollup,
                "modTime": mod_time,
                "daysAgo": days_ago(rollup),
            })
            if depth < max_depth:
                folders.extend(collect_folders(project_id, item["id"], path, depth + 1, max_depth))
    return folders


# ── Find hub & root folder ──────────────────────────────────────────────────

print("Finding hub...")
hubs = dm_get("/project/v1/hubs")["data"]
root_folder_id = None
for h in hubs:
    try:
        top = dm_get(f"/project/v1/hubs/{h['id']}/projects/b.{DM_PROJECT_ID}/topFolders")
        hub_name = h["attributes"]["name"]
        for f in top["data"]:
            if f["attributes"]["name"] == "Project Files":
                root_folder_id = f["id"]
                break
        if root_folder_id:
            print(f"  Hub: {hub_name}")
            break
    except Exception:
        continue

if not root_folder_id:
    print("ERROR: could not find Project Files root")
    sys.exit(1)


# ── Collect folder tree ─────────────────────────────────────────────────────

print("Scanning folder tree (DM API)...")
folders = collect_folders(f"b.{DM_PROJECT_ID}", root_folder_id, max_depth=2)
print(f"  Found {len(folders)} folders")

if not folders:
    print("No folders to render.")
    sys.exit(0)


# ── Render treemap ──────────────────────────────────────────────────────────

sizes = [f["objectCount"] for f in folders]
days = [f["daysAgo"] for f in folders]

# Color: red (recent) → yellow → blue (stale)
max_days = min(max(days), 365)  # cap at 1 year
norm = mcolors.Normalize(vmin=0, vmax=max_days)
cmap = plt.cm.RdYlBu  # Red=hot(recent) → Blue=cold(stale)
colors = [cmap(norm(min(d, max_days))) for d in days]

# Sort largest first so big folders get labels, small ones don't
ranked = sorted(zip(sizes, days, folders, colors), key=lambda x: -x[0])
sizes = [r[0] for r in ranked]
days = [r[1] for r in ranked]
folders = [r[2] for r in ranked]
colors = [r[3] for r in ranked]

# Label every folder
labels = []
for f in folders:
    d = int(f["daysAgo"])
    tag = f"{d}d" if d < 365 else "1y+"
    labels.append(f"{f['name']}\n{tag} | {f['objectCount']} items")

n = len(folders)
fig_h = max(12, n * 0.06)
fig, ax = plt.subplots(figsize=(24, fig_h))
squarify.plot(sizes=sizes, label=labels, color=colors, alpha=0.85, ax=ax,
              text_kwargs={"fontsize": 6.5, "wrap": True})
ax.set_title(f"ACC Folder Activity Heatmap — {hub_name}\n"
             f"Size=objectCount, Color=recency (red=recent, blue=stale)  |  {n} folders",
             fontsize=14, fontweight="bold")
ax.axis("off")

# Colorbar
sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
sm.set_array([])
cbar = fig.colorbar(sm, ax=ax, shrink=0.4, pad=0.01)
cbar.set_label("Days since last change (rollup)", fontsize=10)

fig.tight_layout()
fig.savefig(OUT_FILE, dpi=180, bbox_inches="tight")
print(f"\nSaved: {OUT_FILE}")
