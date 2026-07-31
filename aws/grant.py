#!/usr/bin/env python3
"""
grant.py -- admin CLI for the helt_entitlements table (API_SECURITY_SPEC.md §3).

Onboarding a customer is: create their Cognito user (setup.sh step7 pattern or
admin-create-user), run `grant.py grant`, send them their API_ACCESS document.

Shells out to the aws CLI so it uses YOUR current `aws login` session (the
query Lambda only ever reads this table; all writes happen here, with your
credentials, and are attributed via granted_by).

    source config.env    # for REGION + COGNITO_POOL_ID
    ./grant.py grant  --user customer-a@example.com --pack SANDBOX-01,SANDBOX-02 --groups ALL
    ./grant.py grant  --user customer-b@example.com --pack SANDBOX-04 --groups core,health,ops
    ./grant.py grant  --user helt-ops@example.com   --pack '*' --groups ALL
    ./grant.py revoke --user customer-b@example.com --pack SANDBOX-04
    ./grant.py revoke --user customer-b@example.com              # every row
    ./grant.py list
    ./grant.py list   --user customer-a@example.com

Field groups: core, health, location, ops -- or ALL (grant.py stores the
literal ALL; the Lambda expands it). Pack '*' = every pack (internal ops).
Entitlement changes take up to 60 s to apply (Lambda-side cache).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

GROUPS = {"core", "health", "location", "ops"}
SAFE_PACK = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

TABLE = os.environ.get("ENTITLEMENTS_TABLE", "helt_entitlements")
REGION = os.environ.get("REGION", "us-east-1")
POOL = os.environ.get("COGNITO_POOL_ID", "")


def aws(*args):
    cmd = ["aws", *args, "--region", REGION, "--output", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAILED: aws {args[0]} {args[1]}\n{r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def sub_for(email):
    """Cognito email -> stable sub (the entitlement key)."""
    if not POOL:
        sys.exit("COGNITO_POOL_ID is not set -- `source config.env` first")
    out = aws("cognito-idp", "list-users", "--user-pool-id", POOL,
              "--filter", f'email = "{email}"')
    users = out.get("Users", [])
    if not users:
        sys.exit(f"no Cognito user with email {email}")
    attrs = {a["Name"]: a["Value"] for a in users[0].get("Attributes", [])}
    return attrs.get("sub") or users[0]["Username"]


def parse_groups(s):
    gs = {g.strip() for g in s.split(",") if g.strip()}
    if gs == {"ALL"}:
        return ["ALL"]
    bad = gs - GROUPS
    if bad or not gs:
        sys.exit(f"invalid groups {sorted(bad) or s!r} -- "
                 f"use ALL or a comma list of {sorted(GROUPS)}")
    return sorted(gs)


def cmd_grant(a):
    sub = sub_for(a.user)
    groups = parse_groups(a.groups)
    packs = [p.strip() for p in a.pack.split(",") if p.strip()]
    granted_by = aws("sts", "get-caller-identity").get("Arn", "?")
    granted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for pack in packs:
        if pack != "*" and not SAFE_PACK.match(pack):
            sys.exit(f"invalid pack_id {pack!r}")
        item = {
            "user_id":      {"S": sub},
            "pack_id":      {"S": pack},
            "field_groups": {"SS": groups},
            "user_email":   {"S": a.user},       # convenience for `list`
            "granted_by":   {"S": granted_by},
            "granted_at":   {"S": granted_at},
        }
        aws("dynamodb", "put-item", "--table-name", TABLE,
            "--item", json.dumps(item))
        print(f"granted  {a.user}  {pack}  {','.join(groups)}")


def _rows_for(sub):
    out = aws("dynamodb", "query", "--table-name", TABLE,
              "--key-condition-expression", "user_id = :u",
              "--expression-attribute-values", json.dumps({":u": {"S": sub}}))
    return out.get("Items", [])


def cmd_revoke(a):
    sub = sub_for(a.user)
    packs = ([p.strip() for p in a.pack.split(",") if p.strip()] if a.pack
             else [i["pack_id"]["S"] for i in _rows_for(sub)])
    if not packs:
        print(f"nothing to revoke for {a.user}")
        return
    for pack in packs:
        key = {"user_id": {"S": sub}, "pack_id": {"S": pack}}
        aws("dynamodb", "delete-item", "--table-name", TABLE,
            "--key", json.dumps(key))
        print(f"revoked  {a.user}  {pack}")


def cmd_list(a):
    if a.user:
        items = _rows_for(sub_for(a.user))
    else:
        items = aws("dynamodb", "scan", "--table-name", TABLE).get("Items", [])
    if not items:
        print("(no entitlements)")
        return
    items.sort(key=lambda i: (i.get("user_email", {}).get("S", ""),
                              i["pack_id"]["S"]))
    for i in items:
        print(f'{i.get("user_email", {}).get("S", i["user_id"]["S"]):<28} '
              f'{i["pack_id"]["S"]:<12} '
              f'{",".join(sorted(i["field_groups"]["SS"])):<26} '
              f'{i.get("granted_at", {}).get("S", "")}')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    g = sp.add_parser("grant", help="grant pack access to a user")
    g.add_argument("--user", required=True, help="Cognito email")
    g.add_argument("--pack", required=True, help="pack id(s), comma-separated, or '*'")
    g.add_argument("--groups", default="ALL",
                   help="ALL or comma list of core,health,location,ops")
    g.set_defaults(fn=cmd_grant)

    r = sp.add_parser("revoke", help="revoke pack access (no --pack = all)")
    r.add_argument("--user", required=True)
    r.add_argument("--pack", help="pack id(s), comma-separated; omit for all")
    r.set_defaults(fn=cmd_revoke)

    l = sp.add_parser("list", help="list entitlements")
    l.add_argument("--user", help="filter to one user's rows")
    l.set_defaults(fn=cmd_list)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
