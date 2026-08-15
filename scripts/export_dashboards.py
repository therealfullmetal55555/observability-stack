#!/usr/bin/env python3
"""
Pull dashboards back out of a running Grafana.

Use this after you've been poking at panels in the UI. Without it, provisioning
becomes one-way and the next `make up` silently reverts your afternoon's work —
which is the single most common way a provisioned Grafana setup annoys people.

    python scripts/export_dashboards.py
    python scripts/export_dashboards.py --dry-run
    python scripts/export_dashboards.py --uid token-economics
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "grafana" / "dashboards"

# Keys Grafana adds on read that would churn the diff on every export.
VOLATILE = {
    "id",
    "version",
    "iteration",
    "meta",
    "schemaVersion",
}


def api(url: str, user: str, password: str) -> dict:
    request = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def strip_volatile(dashboard: dict) -> dict:
    return {k: v for k, v in dashboard.items() if k not in VOLATILE}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("GRAFANA_URL", "http://localhost:3000"))
    parser.add_argument("--user", default=os.getenv("GF_SECURITY_ADMIN_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("GF_SECURITY_ADMIN_PASSWORD", "admin"))
    parser.add_argument("--uid", action="append", help="only this dashboard (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="report what changed, write nothing")
    args = parser.parse_args()

    try:
        listing = api(f"{args.url}/api/search?type=dash-db", args.user, args.password)
    except urllib.error.URLError as exc:
        print(f"can't reach Grafana at {args.url} — {exc}", file=sys.stderr)
        return 2

    if args.uid:
        listing = [d for d in listing if d["uid"] in set(args.uid)]
    if not listing:
        print("no dashboards matched", file=sys.stderr)
        return 1

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    changed = written = 0

    for entry in sorted(listing, key=lambda d: d["uid"]):
        uid = entry["uid"]
        detail = api(f"{args.url}/api/dashboards/uid/{uid}", args.user, args.password)
        dashboard = strip_volatile(detail["dashboard"])

        # Match the existing filename so exports land in place instead of
        # creating a second file with the dashboard's title.
        target = next(
            (p for p in DASHBOARD_DIR.glob("*.json")
             if json.loads(p.read_text()).get("uid") == uid),
            DASHBOARD_DIR / f"{uid}.json",
        )

        serialised = json.dumps(dashboard, indent=2) + "\n"
        if target.exists() and target.read_text() == serialised:
            print(f"  {uid:22} unchanged")
            continue

        print(f"  {uid:22} {'would update' if args.dry_run else 'updated'} {target.name}")
        written += 1
        if not args.dry_run:
            changed += 1
            target.write_text(serialised)

    if args.dry_run:
        print(f"\n{written} dashboard(s) differ from the repo")
    else:
        print(f"\nwrote {changed} dashboard(s)")
        if changed:
            print("review the diff, then commit — provisioning reads these on next boot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
