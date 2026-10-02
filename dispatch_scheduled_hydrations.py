"""
dispatch_scheduled_hydrations.py — sends scheduled territories to the
hydration queue once they're due. Added 28 Sep 2026.

Pairs with admin_hydration.html (bulk scheduling) and
sql/13_scheduled_hydration.sql. Runs on the scraper VM from cron, every
15 minutes:

    */15 * * * * cd /path/to/makebank && /usr/bin/python3 dispatch_scheduled_hydrations.py >> dispatch.log 2>&1

Each run:
  1. Finds active territories whose hydrate_after has passed and that
     haven't been dispatched yet.
  2. Claims each one atomically (hydrate_dispatched_at set only if still
     null), so the INSERT-webhook relay, the admin "Hydrate now" button and
     this script can never send the same territory twice.
  3. POSTs it to the VM's own hydration webhook on localhost, which applies
     the existing 3-wide queue exactly as for any other new territory.
     A failed POST releases the claim, so the next run retries it.

Posts locally, not via Railway: this runs on the same VM as the hydration
service, so there's no reason to add Railway as a dependency.

Env (VM .env, already present for the hydrate service):
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    HYDRATE_WEBHOOK_SECRET
    HYDRATE_LOCAL_URL   optional, default http://127.0.0.1:8000/webhook/new-territory

    python3 dispatch_scheduled_hydrations.py            # dispatch everything due
    python3 dispatch_scheduled_hydrations.py --dry-run  # just list what's due
"""
import os
import sys
import argparse
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

from scraper import get_supabase  # noqa: E402  (after load_dotenv)

log = logging.getLogger("dispatch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

LOCAL_URL = os.environ.get("HYDRATE_LOCAL_URL", "http://127.0.0.1:8000/webhook/new-territory")
WEBHOOK_SECRET = os.environ.get("HYDRATE_WEBHOOK_SECRET", "")


def due_territories(sb) -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    return (
        sb.table("lgas").select("*")
        .eq("active", True)
        .lte("hydrate_after", now_iso)
        .is_("hydrate_dispatched_at", "null")
        .order("hydrate_after")
        .order("id")
        .execute()
    ).data


def claim(sb, lga_id: int) -> bool:
    now_iso = datetime.now(timezone.utc).isoformat()
    return bool(
        sb.table("lgas").update({"hydrate_dispatched_at": now_iso})
        .eq("id", lga_id).is_("hydrate_dispatched_at", "null")
        .execute().data
    )


def release(sb, lga_id: int):
    sb.table("lgas").update({"hydrate_dispatched_at": None}).eq("id", lga_id).execute()


def main():
    ap = argparse.ArgumentParser(description="Dispatch scheduled territory hydrations that are now due")
    ap.add_argument("--dry-run", action="store_true", help="List what's due without dispatching")
    args = ap.parse_args()

    sb = get_supabase()
    due = due_territories(sb)
    if not due:
        log.info("Nothing due.")
        return 0

    log.info(f"{len(due)} territory(ies) due: " + ", ".join(f"{r['id']} {r['name']}" for r in due))
    if args.dry_run:
        return 0

    sent = failed = 0
    for row in due:
        if not claim(sb, row["id"]):
            log.info(f"LGA {row['id']} already dispatched by another path — skipping")
            continue
        try:
            resp = requests.post(
                LOCAL_URL,
                json={"type": "INSERT", "table": "lgas", "record": row},
                headers={"Authorization": f"Bearer {WEBHOOK_SECRET}"},
                timeout=10,
            )
            resp.raise_for_status()
            log.info(f"Dispatched LGA {row['id']} ({row['name']}): {resp.json()}")
            sent += 1
        except Exception as e:
            release(sb, row["id"])
            log.error(f"Dispatch failed for LGA {row['id']} ({row['name']}) — claim released, will retry next run: {e}")
            failed += 1

    log.info(f"Done — {sent} dispatched, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
