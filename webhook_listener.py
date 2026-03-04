#!/usr/bin/env python3
"""
Webhook listener for ACC move & delete events → removals.pq.

Tracks four "departure" events so backup.py can clean up orphaned S3 keys:
  - dm.version.deleted      (file deleted)
  - dm.version.moved.out    (file moved out of folder)
  - dm.folder.deleted       (folder deleted)
  - dm.folder.moved.out     (folder moved out of folder)

Runs as:
  1. AWS Lambda  — API Gateway POST → lambda_handler()
  2. Local dev   — python webhook_listener.py  (Flask on :5000)
  3. Register    — python webhook_listener.py --register --folder-urn URN --callback-url URL

Each incoming event is appended to cache/removals.pq.  backup.py reads this
file at the start of each run to delete stale S3 prefixes.
"""

import argparse, base64, hashlib, hmac, json, os, pathlib
from datetime import datetime, timezone

import pandas as pd

HERE = pathlib.Path(__file__).parent
REMOVALS_FILE = HERE / "cache" / "removals.pq"

EVENTS_TRACKED = [
    "dm.version.deleted",
    "dm.version.moved.out",
    "dm.folder.deleted",
    "dm.folder.moved.out",
]

WEBHOOKS_API = "https://developer.api.autodesk.com/webhooks/v1"

REMOVALS_COLUMNS = [
    "timestamp", "event_type", "resource_urn", "resource_type",
    "old_path", "project_id", "delivery_id",
]


# ── Signature verification ───────────────────────────────────────────────────

def verify_signature(body_bytes: bytes, signature_header: str) -> bool:
    """Verify HMAC-SHA1 signature per APS webhook docs.

    The x-adsk-signature header is formatted as 'sha1hash=<hex>'.
    """
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        return True  # no secret configured → skip verification

    if not signature_header or not signature_header.startswith("sha1hash="):
        return False

    expected = signature_header[len("sha1hash="):]
    computed = hmac.new(secret.encode(), body_bytes, hashlib.sha1).hexdigest()
    return hmac.compare_digest(computed, expected)


# ── Path reconstruction ──────────────────────────────────────────────────────

def build_old_path(payload: dict) -> str:
    """Reconstruct the old folder path from payload ancestors + resource name.

    Ancestors are bottom-up: [{name: "Subfolder"}, {name: "Project Files"}].
    We reverse them and join with '/'.
    """
    ancestors = payload.get("ancestors", [])
    parts = [a["name"] for a in reversed(ancestors)]

    # The resource name is in payload (varies by event type)
    name = payload.get("name", "")
    if name:
        parts.append(name)

    return "/".join(parts) if parts else ""


# ── Core handler ─────────────────────────────────────────────────────────────

def handle_event(body: dict, delivery_id: str = "") -> dict | None:
    """Parse a webhook callback body into a removal row.

    Returns a dict ready for append_removal(), or None if the event
    is not one we track.
    """
    hook = body.get("hook", {})
    event_type = hook.get("event", "")

    if event_type not in EVENTS_TRACKED:
        return None

    payload = body.get("payload", {})
    resource_urn = body.get("resourceUrn", "")

    # Determine resource type from event name
    if "folder" in event_type:
        resource_type = "folder"
    else:
        resource_type = "version"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "resource_urn": resource_urn,
        "resource_type": resource_type,
        "old_path": build_old_path(payload),
        "project_id": str(payload.get("project", "")),
        "delivery_id": delivery_id,
    }


# ── Parquet append ───────────────────────────────────────────────────────────

def append_removal(row: dict):
    """Append a removal row to removals.pq. Creates the file if missing.

    Deduplicates by delivery_id — skips if already recorded.
    """
    REMOVALS_FILE.parent.mkdir(parents=True, exist_ok=True)

    if REMOVALS_FILE.exists():
        df = pd.read_parquet(REMOVALS_FILE)
        # Deduplicate
        if row["delivery_id"] and row["delivery_id"] in df["delivery_id"].values:
            print(f"  SKIP duplicate delivery_id={row['delivery_id']}")
            return
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row], columns=REMOVALS_COLUMNS)

    df.to_parquet(REMOVALS_FILE)
    print(f"  APPEND {row['event_type']}  {row['old_path']}")


# ── AWS Lambda entry point ───────────────────────────────────────────────────

def lambda_handler(event, context):
    """API Gateway proxy integration entry point.

    Expects event with 'body' (JSON string) and 'headers'.
    """
    headers = event.get("headers", {})
    body_str = event.get("body", "{}")
    body_bytes = body_str.encode("utf-8")

    # Verify signature
    sig = headers.get("x-adsk-signature", "")
    if not verify_signature(body_bytes, sig):
        return {"statusCode": 403, "body": "Invalid signature"}

    body = json.loads(body_str)
    delivery_id = headers.get("x-adsk-delivery-id", "")

    row = handle_event(body, delivery_id)
    if row:
        append_removal(row)

    return {"statusCode": 200, "body": "OK"}


# ── Webhook registration ────────────────────────────────────────────────────

def register_webhooks(token: str, folder_urns: list[str], callback_url: str,
                      secret: str = ""):
    """Create webhook subscriptions for all four departure events on each folder.

    POST /webhooks/v1/systems/data/events/{event}/hooks

    Rate limit: 50 POST requests/minute per the APS Webhooks API.
    Respects 429 Retry-After headers automatically.
    """
    import time
    import requests

    created = 0
    skipped = 0
    errors = 0

    for folder_urn in folder_urns:
        for event in EVENTS_TRACKED:
            url = f"{WEBHOOKS_API}/systems/data/events/{event}/hooks"
            body = {
                "callbackUrl": callback_url,
                "scope": {"folder": folder_urn},
                "autoReactivateHook": True,
            }
            if secret:
                body["hookAttribute"] = {"secret": secret}

            r = requests.post(url,
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                              json=body, timeout=30)

            if r.status_code == 201:
                print(f"  OK      {event}  {folder_urn[:60]}...")
                created += 1
            elif r.status_code == 409:
                print(f"  EXISTS  {event}  (already registered)")
                skipped += 1
            elif r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 30))
                print(f"  RATE-LIMITED — waiting {retry_after}s...")
                time.sleep(retry_after)
                # Retry once
                r = requests.post(url,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json"},
                                  json=body, timeout=30)
                if r.status_code == 201:
                    print(f"  OK      {event}  {folder_urn[:60]}... (after retry)")
                    created += 1
                else:
                    print(f"  ERR     {event}: {r.status_code} {r.text[:200]}")
                    errors += 1
            else:
                print(f"  ERR     {event}: {r.status_code} {r.text[:200]}")
                errors += 1

    print(f"\nRegistration complete: {created} created, {skipped} existing, {errors} errors")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Webhook listener for ACC move/delete events")
    parser.add_argument("--register", action="store_true",
                        help="Register webhooks instead of running the server")
    parser.add_argument("--folder-urn", nargs="+", default=[],
                        help="Folder URN(s) to register webhooks on (one or more)")
    parser.add_argument("--callback-url", default="",
                        help="Callback URL for webhook registration")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port for local dev server (default: 5000)")
    args = parser.parse_args()

    if args.register:
        if not args.folder_urn or not args.callback_url:
            print("ERROR: --folder-urn and --callback-url are required with --register")
            print("       Pass one or more folder URNs (4 events registered per URN)")
            print("       Rate limit: 50 POSTs/min — 429s are retried automatically")
            raise SystemExit(1)

        from auth import load_token
        token = load_token()
        secret = os.environ.get("WEBHOOK_SECRET", "")
        print(f"Registering {len(args.folder_urn)} folder(s) x {len(EVENTS_TRACKED)} events "
              f"= {len(args.folder_urn) * len(EVENTS_TRACKED)} subscriptions")
        print(f"Rate limit: 50 POST requests/minute\n")
        register_webhooks(token, args.folder_urn, args.callback_url, secret)
        return

    # ── Local Flask dev server ────────────────────────────────────────────
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("ERROR: Flask not installed. Run: pip install flask")
        print("       (Flask is only needed for local dev — Lambda uses lambda_handler directly)")
        raise SystemExit(1)

    app = Flask(__name__)

    @app.route("/callback", methods=["POST"])
    def callback():
        body_bytes = request.get_data()
        sig = request.headers.get("x-adsk-signature", "")

        if not verify_signature(body_bytes, sig):
            return jsonify({"error": "Invalid signature"}), 403

        body = request.get_json(force=True)
        delivery_id = request.headers.get("x-adsk-delivery-id", "")

        row = handle_event(body, delivery_id)
        if row:
            append_removal(row)
            return jsonify({"status": "recorded", "path": row["old_path"]}), 200

        return jsonify({"status": "ignored"}), 200

    print(f"Webhook listener running on http://localhost:{args.port}/callback")
    print(f"Removals file: {REMOVALS_FILE}")
    print(f"Events tracked: {', '.join(EVENTS_TRACKED)}")
    app.run(host="0.0.0.0", port=args.port, debug=True)


if __name__ == "__main__":
    main()
