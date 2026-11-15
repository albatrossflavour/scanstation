#!/usr/bin/env python3
"""One-time Paperless setup for the scan station. Idempotent, and safe to re-run.

Creates:
  - user `scanstation`, allowed only to add documents and read documents, tags and its tasks
  - a workflow that hands every `scanstation-*` upload to the owner and lets `scanstation` view it
  - view rights for `scanstation` on the paperless-gpt tag, if that tag exists
Then gives the station's API token, printed or written to --token-file.

Dry run by default; pass --apply to change Paperless. Needs an admin's API token in
$PAPERLESS_ADMIN_TOKEN, used for this run only.

    PAPERLESS_ADMIN_TOKEN=... tools/paperless-setup.py --url http://paperless.lan:8000 --owner alice --apply
"""
import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

API = None  # set from --url
USER = "scanstation"
PERMISSIONS = ["add_document", "view_document", "view_tag", "view_paperlesstask"]
WORKFLOW = "Scan station owner"


def call(method, path, body=None, token=None, auth=True):
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Accept", "application/json; version=9")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", f"Token {token or os.environ['PAPERLESS_ADMIN_TOKEN']}")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def main(apply, owner, tag, token_file=None):
    owner = owner.strip()
    if not owner:
        sys.exit("give the Paperless username that should own the scans")
    owners = [u for u in call("GET", f"/users/?username__iexact={urllib.parse.quote(owner)}")["results"]
              if u["username"].lower() == owner.lower()]
    if not owners:
        sys.exit(f"no Paperless user called {owner!r} to own the scans")
    owner_id = owners[0]["id"]
    users = call("GET", f"/users/?username__iexact={USER}")["results"]
    password = secrets.token_urlsafe(32)
    if users:
        user = users[0]
        print(f"user {USER} exists (id {user['id']})")
        if sorted(user.get("user_permissions", [])) != sorted(PERMISSIONS):
            print(f"  permissions differ: {user.get('user_permissions')} -> {PERMISSIONS}")
            if apply:
                call("PATCH", f"/users/{user['id']}/", {"user_permissions": PERMISSIONS})
    else:
        print(f"create user {USER} with {PERMISSIONS}")
        user = {"id": None}
        if apply:
            user = call("POST", "/users/", {"username": USER, "password": password, "is_active": True,
                                            "is_staff": False, "is_superuser": False,
                                            "user_permissions": PERMISSIONS})

    flows = [w for w in call("GET", "/workflows/?page_size=100")["results"] if w["name"] == WORKFLOW]
    if flows:
        print(f"workflow '{WORKFLOW}' exists (id {flows[0]['id']})")
    else:
        print(f"create workflow '{WORKFLOW}': on Document Added, filename scanstation-*, owner -> {owner} (id {owner_id})")
        if apply:
            call("POST", "/workflows/", {
                "name": WORKFLOW, "order": 1, "enabled": True,
                "triggers": [{"type": 2, "sources": [], "filter_filename": "scanstation-*",
                              "matching_algorithm": 0, "is_insensitive": True}],
                "actions": [{"type": 1, "assign_owner": owner_id, "assign_view_users": [user["id"]]}],
            })

    # The station checks for the paperless-gpt tag to know classification has finished, so it needs to see it.
    tags = call("GET", f"/tags/?name__iexact={urllib.parse.quote(tag)}&full_perms=true")["results"]
    if tags and user.get("id"):
        view = tags[0]["permissions"]["view"]
        if user["id"] not in view["users"]:
            print(f"give {USER} view on tag {tag}")
            if apply:
                perms = tags[0]["permissions"]
                perms["view"]["users"] = view["users"] + [user["id"]]
                call("PATCH", f"/tags/{tags[0]['id']}/", {"set_permissions": perms})

    if apply:
        # Paperless only issues a token to the user it belongs to, so log in as the station once.
        if users:
            call("PATCH", f"/users/{user['id']}/", {"password": password})
        token = call("POST", "/token/", {"username": USER, "password": password}, auth=False)["token"]
        if token_file:
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            print(f"\nstation token written to {token_file}")
        else:
            print("\nStation token, for PAPERLESS_TOKEN in the station's config:")
            print(token)
    else:
        print("\ndry run: nothing changed. Re-run with --apply.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Create the scan station's Paperless user and workflow.")
    ap.add_argument("--url", required=True, help="Paperless base URL, e.g. http://paperless.lan:8000")
    ap.add_argument("--owner", required=True, help="Paperless username that should own scanned documents")
    ap.add_argument("--tag", default="paperless-gpt-auto", help="paperless-gpt's processing tag")
    ap.add_argument("--token-file", help="write the station token here (mode 600) instead of printing it")
    ap.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    args = ap.parse_args()
    API = args.url.rstrip("/") + "/api"
    if "PAPERLESS_ADMIN_TOKEN" not in os.environ:
        sys.exit("set PAPERLESS_ADMIN_TOKEN to an admin's API token")
    try:
        main(args.apply, args.owner, args.tag, args.token_file)
    except urllib.error.HTTPError as e:
        sys.exit(f"Paperless said {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"couldn't reach Paperless at {args.url}: {e.reason}")
