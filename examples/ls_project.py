#!/usr/bin/env python3
"""
ls -R of an ACC project's docs structure via the AEC Data Model API.

Usage:
    python ls_project.py <ACCESS_TOKEN> [DM_PROJECT_ID]

Walks the full folder tree of a project, printing every folder and
every ElementGroup (file) found.  If DM_PROJECT_ID is given it will
auto-match the project; otherwise it prompts interactively.
"""

import sys
from aecdm_client import (
    get_hubs,
    get_projects,
    get_top_folders,
    get_subfolders,
    get_element_groups_in_folder,
)


def pick(items: list[dict], label: str) -> dict:
    print(f"\n── {label} ──")
    for i, item in enumerate(items):
        print(f"  [{i}] {item['name']}")
    idx = int(input(f"Pick {label} index: "))
    return items[idx]


def paginate_all(fetch_page, token, *args):
    all_results = []
    cursor = None
    while True:
        page = fetch_page(token, *args, cursor)
        all_results.extend(page["results"])
        cursor = page["pagination"]["cursor"]
        if not cursor:
            break
    return all_results


def find_project_by_dm_id(token: str, dm_project_id: str):
    """Search all hubs for a project matching the DM project ID."""
    # DM API project IDs are prefixed with "b." in the alternativeIdentifiers
    targets = {dm_project_id, f"b.{dm_project_id}"}
    hubs = get_hubs(token)
    for hub in hubs:
        print(f"  scanning hub: {hub['name']} ...")
        projects = get_projects(token, hub["id"])
        for proj in projects:
            dm_id = (proj.get("alternativeIdentifiers") or {}).get(
                "dataManagementAPIProjectId", ""
            )
            if dm_id in targets:
                return hub, proj
    return None, None


def walk_folder(token: str, project_id: str, folder_id: str, path: str):
    print(f"\n{path}/")

    # ElementGroups (design files) in this folder
    files = paginate_all(get_element_groups_in_folder, token, project_id, folder_id)
    for f in files:
        print(f"  📄 {f['name']}")

    if not files:
        print("  (no elementGroups)")

    # Recurse into subfolders
    subfolders = paginate_all(get_subfolders, token, project_id, folder_id)
    for sf in subfolders:
        walk_folder(token, project_id, sf["id"], f"{path}/{sf['name']}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python ls_project.py <ACCESS_TOKEN> [DM_PROJECT_ID]")
        sys.exit(1)

    token = sys.argv[1]
    dm_project_id = sys.argv[2] if len(sys.argv) > 2 else None

    if dm_project_id:
        print(f"Looking for DM project ID: {dm_project_id}")
        hub, project = find_project_by_dm_id(token, dm_project_id)
        if not project:
            print("ERROR: project not found across your hubs.")
            sys.exit(1)
    else:
        hubs = get_hubs(token)
        hub = pick(hubs, "Hubs")
        projects = get_projects(token, hub["id"])
        project = pick(projects, "Projects")

    print(f"\n{'='*60}")
    print(f"Hub:     {hub['name']}")
    print(f"Project: {project['name']}")
    print(f"{'='*60}")

    project_id = project["id"]
    top_folders = paginate_all(get_top_folders, token, project_id)
    for folder in top_folders:
        walk_folder(token, project_id, folder["id"], folder["name"])

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    main()
