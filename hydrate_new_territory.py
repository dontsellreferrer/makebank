"""
Webhook endpoint that hydrates a brand-new territory immediately after the
order form creates it.

IMPORTANT — listings are emailed as a CSV, NOT written to Supabase directly.
A first-time scrape has no way to know how long each listing has actually
been on the market — every property would get first_seen = today, which
means Expiring Soon (75-90 days) and Expired (90+ days) would show nothing
real for the first three months of a new territory. So instead:

  1. Scrape every currently-active listing for the new territory (address,
     agent, agency, URL) — but don't insert it.
  2. Email it as a CSV to Rick, with a blank "First Seen" column.
  3. Rick gets the real listing ages dated externally, then re-imports the
     same CSV with that column filled in:

       python3 scraper.py --import-csv dated_file.csv --lga <id> --import-table listings

     (import_csv now reads that column and uses it as the real first_seen —
     see scraper.py. Rows left blank just default to today's date, so a
     partial re-date still works fine.)

Sold data doesn't have this problem (sold_date is already known), so the
sold side of hydration still writes directly to Supabase as normal.

FLOW
  1. Order form inserts a row into `lgas` (see makebank_order.html).
  2. A Supabase Database Webhook fires on that INSERT and POSTs the new row
     here (see SETUP below).
  3. Listings → scraped, emailed as CSV, NOT saved.
     Sold    → scraped and saved directly, same as any normal run.

Deploy alongside the existing scraper (same Railway project, same deps —
imports directly from scraper.py rather than duplicating scraping logic).

SETUP (Supabase dashboard):
  Database → Webhooks → Create a new webhook
    Table:            lgas
    Events:           Insert
    Type:             HTTP Request
    URL:              https://<this-service>.up.railway.app/webhook/new-territory
    HTTP Headers:     Authorization: Bearer <HYDRATE_WEBHOOK_SECRET>

Env vars needed (Railway):
    HYDRATE_WEBHOOK_SECRET   — shared secret, must match the Supabase webhook header
    RESEND_API_KEY           — same one weekly_report.py already uses
    HYDRATE_EMAIL_TO         — defaults to rick@rickjohnson.com.au
    HYDRATE_EMAIL_FROM       — defaults to reports@referrer.com.au

Run with:
    pip install fastapi uvicorn requests --break-system-packages
    uvicorn hydrate_new_territory:app --host 0.0.0.0 --port 8000
"""
import os
import io
import csv
import sys
import base64
import logging
import subprocess

import requests
from fastapi import FastAPI, Request, HTTPException

# Reuses the exact same scraping logic already fixed in scraper.py — no
# separate/duplicate implementation to drift out of sync.
from scraper import (
    get_supabase, CookiePool, URLCollector, scrape_details_playwright,
    run_scrape, COOKIES_FILE, MAX_PAGES, claim_cookie_slot, release_cookie_slot,
)

log = logging.getLogger("hydrate")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

WEBHOOK_SECRET = os.environ.get("HYDRATE_WEBHOOK_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO       = os.environ.get("HYDRATE_EMAIL_TO", "rick@rickjohnson.com.au")
EMAIL_FROM     = os.environ.get("HYDRATE_EMAIL_FROM", "reports@referrer.com.au")


def build_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=['Address', 'Agent', 'Agency', 'URL', 'First Seen'])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            'Address': r.get('address', ''),
            'Agent':   r.get('agent', ''),
            'Agency':  r.get('agency', ''),
            'URL':     r.get('url', ''),
            'First Seen': '',  # left blank — filled in externally, then re-imported
        })
    return buf.getvalue()


def email_csv(lga_name: str, csv_content: str, row_count: int):
    safe_name = lga_name.replace(' ', '_').replace('/', '-')
    if not RESEND_API_KEY:
        # No Resend yet — save it locally instead of just dumping raw CSV
        # text into the log, so today's 4 regions still produce something
        # actually usable. Switch to email once Resend is set up in Railway.
        filename = f"{safe_name}_hydration.csv"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(csv_content)
        log.info(f"RESEND_API_KEY not set — saved to {filename} instead ({row_count} rows).")
        return
    attachment_b64 = base64.b64encode(csv_content.encode('utf-8')).decode('ascii')
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": EMAIL_FROM,
            "to": [EMAIL_TO],
            "subject": f"New territory hydration — {lga_name} ({row_count} listings)",
            "html": (
                f"<p>New territory <strong>{lga_name}</strong> just scraped — "
                f"{row_count} currently active listings attached as CSV, with a blank "
                f"<strong>First Seen</strong> column.</p>"
                f"<p><strong>Dating it (fast, not a slog):</strong></p>"
                f"<ol>"
                f"<li>Open the same search on realestate.com.au, sorted Date (Newest → Oldest).</li>"
                f"<li>First ~7 days show an explicit \"Added X days ago\" badge — copy those straight across, no guesswork.</li>"
                f"<li>Find the row where listings cross 91 days old (open a listing's own detail page to confirm the real date).</li>"
                f"<li>For the middle stretch, open just the first listing on each results page (25/page) as a checkpoint, then fill every row between checkpoints with the earlier checkpoint's date — a step function, not per-row precision.</li>"
                f"<li>Stamp everything past the 91-day row with one date safely past 90 days. <strong>Don't leave it blank</strong> — a blank cell defaults to today's date on import, which undoes this whole exercise.</li>"
                f"<li>Save the First Seen column as plain <code>YYYY-MM-DD</code> text, not a regional date format — a non-ISO value also silently falls back to today.</li>"
                f"</ol>"
                f"<p>Then re-import:</p>"
                f"<pre>python3 scraper.py --import-csv dated_file.csv --lga &lt;id&gt; --import-table listings</pre>"
                f"<p>Full method with worked example in the HANDOVER doc.</p>"
            ),
            "attachments": [{
                "filename": f"{safe_name}_hydration.csv",
                "content": attachment_b64,
            }],
        },
    )
    if resp.status_code >= 300:
        log.error(f"Resend send failed: {resp.status_code} {resp.text}")
    else:
        log.info(f"Hydration CSV emailed to {EMAIL_TO} ({row_count} rows)")


def hydrate(lga: dict):
    """Runs in a background thread — the webhook response doesn't wait on this."""
    sb = get_supabase()
    lga_name = lga.get('name', f"LGA {lga.get('id')}")

    # Claims its own cookie slot, same mechanism the cron uses — a new-territory
    # signup can land at any time, including while the daily cron is mid-run,
    # so this has to coordinate through the same cookie_slots table rather
    # than assume it has the pool to itself.
    runner = f"hydrate-{lga.get('id')}"
    slot = claim_cookie_slot(sb, runner)
    pool = CookiePool(COOKIES_FILE, slot_index=slot)

    try:
        # --- Listings: scrape, email as CSV, do NOT save (see module docstring) ---
        try:
            log.info(f"Hydrating listings for {lga_name} (id={lga['id']}) — CSV export, not saved")
            collector = URLCollector(pool, max_pages=MAX_PAGES)
            live_urls = collector.collect_urls(lga['search_url_listings'], known_urls=set())
            log.info(f"Found {len(live_urls)} active listings")
            rows = scrape_details_playwright(list(live_urls), pool) if live_urls else []
            csv_content = build_csv(rows)
            email_csv(lga_name, csv_content, len(rows))
        except Exception as e:
            log.error(f"Listings hydration failed for {lga_name}: {e}")

        # --- Sold: no dating problem (sold_date is already known) — save as normal ---
        try:
            log.info(f"Hydrating sold data for {lga_name}")
            run_scrape(sb, pool, lga, 'sold', MAX_PAGES)
        except Exception as e:
            log.error(f"Sold hydration failed for {lga_name}: {e}")
    finally:
        release_cookie_slot(sb, slot, runner)

    log.info(f"Hydration complete for {lga_name}")


@app.post("/webhook/new-territory")
async def new_territory(request: Request):
    if WEBHOOK_SECRET:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    payload = await request.json()
    record = payload.get("record")
    if not record or "id" not in record:
        raise HTTPException(status_code=400, detail="Missing record.id")

    # Launched as a genuinely separate OS process, not a background thread.
    # Playwright's sync API (used throughout scraper.py) cannot coexist with
    # an asyncio event loop anywhere in the same process — and this whole
    # app runs on Uvicorn, which *is* an asyncio event loop. A background
    # thread still shares the same process, so it still hit that conflict
    # (found 13 Sep 2026 — "Playwright Sync API inside the asyncio loop").
    # A subprocess has its own separate interpreter and no such loop, so it
    # behaves exactly like running this file's own --lga-id CLI mode by hand
    # from a terminal — which already works correctly.
    # stdout/stderr deliberately NOT redirected to DEVNULL — leaving them
    # inherited from this process means the subprocess's own log lines
    # (cookie slot claims, scrape progress, etc.) still show up in Railway's
    # log viewer exactly as before, just tagged as a separate process.
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--lga-id", str(record["id"])],
    )

    return {"status": "hydration_started", "lga_id": record["id"]}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── CLI — manual trigger, no webhook/Railway needed ──────────────────────────
# For testing before hosting is live: creates no HTTP server, just runs the
# exact same hydrate() function the webhook would, for one LGA at a time.
#
#   python3 hydrate_new_territory.py --lga-id 27
#
# Runs in the foreground (not backgrounded like the webhook does) so you can
# watch it work and see the email send confirmation before moving to the
# next one. Run these ONE AT A TIME, sequentially — the cookie pool isn't
# necessarily safe for multiple concurrent scrapes hitting it at once, and
# with 30 fresh cookies you don't want to risk burning them on a race.
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Manually hydrate one LGA (bypasses the webhook)")
    parser.add_argument("--lga-id", type=int, required=True)
    args = parser.parse_args()

    sb = get_supabase()
    resp = sb.table("lgas").select("*").eq("id", args.lga_id).execute()
    if not resp.data:
        print(f"No LGA found with id {args.lga_id}")
        sys.exit(1)

    lga = resp.data[0]
    print(f"Hydrating {lga['name']} (id={lga['id']}) — this runs in the foreground, watch the log output below.")
    hydrate(lga)
    print("Done.")
