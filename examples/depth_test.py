#!/usr/bin/env python3
"""Test max nesting depth for folder tree in a single AECDM GraphQL query."""

import sys
from aecdm_client import _post, get_hubs, get_projects

TOKEN = sys.argv[1]
DM_PROJECT_ID = "63a3e38e-ff78-46d4-97f3-3b8714da05f8"

# Find project
for hub in get_hubs(TOKEN):
    for proj in get_projects(TOKEN, hub["id"]):
        dm = (proj.get("alternativeIdentifiers") or {}).get("dataManagementAPIProjectId", "")
        if dm in {DM_PROJECT_ID, f"b.{DM_PROJECT_ID}"}:
            PROJECT_ID = proj["id"]
            print(f"Project: {proj['name']}")
            break
    else:
        continue
    break

F = "id name lastModifiedOn"


def build_query(depth, limit):
    """Build nested folder query with pagination limits at each level."""
    inner = F
    for _ in range(depth):
        inner = f"{F} folders(pagination: {{limit: {limit}}}) {{ results {{ {inner} }} }}"
    return f"""
    query ($projectId: ID!, $pagination: PaginationInput) {{
      foldersByProject(projectId: $projectId, pagination: $pagination) {{
        results {{ {inner} }}
      }}
    }}"""


def count_folders(results, depth=0):
    total = 0
    for f in results:
        total += 1
        subs = (f.get("folders") or {}).get("results", [])
        if subs:
            total += count_folders(subs, depth + 1)
    return total


# Test various depth + limit combos
for limit in [5, 10, 20, 50]:
    print(f"\n{'='*40}")
    print(f"Pagination limit per level: {limit}")
    print(f"{'='*40}")
    for d in range(1, 8):
        q = build_query(d, limit)
        try:
            data = _post(TOKEN, q, {"projectId": PROJECT_ID, "pagination": {"limit": limit}})
            folders = data["foldersByProject"]["results"]
            n = count_folders(folders)
            print(f"  Depth {d}: OK — {n} folder nodes")
        except Exception as e:
            msg = str(e)[:120]
            print(f"  Depth {d}: FAILED — {msg}")
            break
