#!/usr/bin/env python3
"""Probe folder date fields from the AECDM schema to see what's available."""

import sys
from aecdm_client import _post, get_hubs, get_projects

TOKEN = sys.argv[1]
DM_PROJECT_ID = sys.argv[2] if len(sys.argv) > 2 else "63a3e38e-ff78-46d4-97f3-3b8714da05f8"


def find_project(token, dm_id):
    targets = {dm_id, f"b.{dm_id}"}
    for hub in get_hubs(token):
        for proj in get_projects(token, hub["id"]):
            dm = (proj.get("alternativeIdentifiers") or {}).get("dataManagementAPIProjectId", "")
            if dm in targets:
                return hub, proj
    return None, None


hub, project = find_project(TOKEN, DM_PROJECT_ID)
print(f"Hub: {hub['name']}  /  Project: {project['name']}\n")

# Query top-level folders with date/metadata fields (no nesting to stay under query point limit)
q = """
query ($projectId: ID!, $pagination: PaginationInput) {
  foldersByProject(projectId: $projectId, pagination: $pagination) {
    results {
      id
      name
      objectCount
      createdOn
      createdBy { id userName }
      lastModifiedOn
      lastModifiedBy { id userName }
    }
  }
}"""

data = _post(TOKEN, q, {"projectId": project["id"], "pagination": {"limit": 20}})
folders = data["foldersByProject"]["results"]

for f in folders:
    print(f"📁 {f['name']}")
    print(f"   objectCount:    {f.get('objectCount')}")
    print(f"   createdOn:      {f.get('createdOn')}")
    print(f"   createdBy:      {(f.get('createdBy') or {}).get('userName')}")
    print(f"   lastModifiedOn: {f.get('lastModifiedOn')}")
    print(f"   lastModifiedBy: {(f.get('lastModifiedBy') or {}).get('userName')}")
    print()
