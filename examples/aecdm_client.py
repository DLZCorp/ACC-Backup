"""Thin wrapper around the AEC Data Model GraphQL API."""

import requests

ENDPOINT = "https://developer.api.autodesk.com/aec/graphql"


def _post(token: str, query: str, variables: dict | None = None) -> dict:
    """Send a GraphQL query and return the JSON response."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {"query": query}
    if variables:
        body["variables"] = variables
    r = requests.post(ENDPOINT, json=body, headers=headers, timeout=30)
    if not r.ok:
        print(f"  !! HTTP {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


# ── Queries ──────────────────────────────────────────────────────────────────

def get_hubs(token: str) -> list[dict]:
    q = """
    query {
      hubs {
        pagination { cursor }
        results { id name }
      }
    }"""
    return _post(token, q)["hubs"]["results"]


def get_projects(token: str, hub_id: str) -> list[dict]:
    q = """
    query ($hubId: ID!) {
      projects(hubId: $hubId) {
        pagination { cursor }
        results {
          id
          name
          alternativeIdentifiers {
            dataManagementAPIProjectId
          }
        }
      }
    }"""
    return _post(token, q, {"hubId": hub_id})["projects"]["results"]


def get_top_folders(token: str, project_id: str, cursor: str | None = None) -> dict:
    q = """
    query ($projectId: ID!, $pagination: PaginationInput) {
      foldersByProject(projectId: $projectId, pagination: $pagination) {
        pagination { cursor }
        results {
          id
          name
        }
      }
    }"""
    pag = {"limit": 50}
    if cursor:
        pag["cursor"] = cursor
    data = _post(token, q, {"projectId": project_id, "pagination": pag})
    return data["foldersByProject"]


def get_subfolders(token: str, project_id: str, folder_id: str, cursor: str | None = None) -> dict:
    """Get subfolders of a folder using the Folder.folders nested field."""
    q = """
    query ($projectId: ID!, $folderId: ID!, $pagination: PaginationInput) {
      folder(projectId: $projectId, folderId: $folderId) {
        folders(pagination: $pagination) {
          pagination { cursor }
          results {
            id
            name
          }
        }
      }
    }"""
    pag = {"limit": 50}
    if cursor:
        pag["cursor"] = cursor
    data = _post(token, q, {"projectId": project_id, "folderId": folder_id, "pagination": pag})
    return data["folder"]["folders"]


def get_element_groups_in_folder(
    token: str, project_id: str, folder_id: str, cursor: str | None = None
) -> dict:
    q = """
    query ($projectId: ID!, $folderId: ID!, $pagination: PaginationInput) {
      elementGroupsByFolder(projectId: $projectId, folderId: $folderId, pagination: $pagination) {
        pagination { cursor }
        results {
          id
          name
          alternativeIdentifiers {
            fileUrn
            fileVersionUrn
          }
        }
      }
    }"""
    pag = {"limit": 50}
    if cursor:
        pag["cursor"] = cursor
    data = _post(token, q, {"projectId": project_id, "folderId": folder_id, "pagination": pag})
    return data["elementGroupsByFolder"]
