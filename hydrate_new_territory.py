"""
Webhook endpoint that hydrates a brand-new territory immediately after the
order form creates it.

Listings are saved immediately (first_seen = hydration time), not left
empty until a dated CSV comes back. Changed 23 Sep 2026 — the original
design left `listings` completely empty and the LGA un-dated (so cron
never ran) until Rick manually re-dated and re-imported the CSV, meaning
anything withdrawn or sold during that gap was lost forever, never tracked.
That's gone now: this territory is cron-eligible from the same night it's
created, so nothing is missed regardless of how long dating actually takes
(handing a freelancer a full day's worth of new territories no longer
means losing days of tracking on all of them while the CSV sits in their
queue).

The tradeoff this accepts: first_seen is initially just "whenever hydration
happened to run", not each listing's true original listing date, so
Expiring Soon / Newly Expired / days-on-market are all approximate for this
territory until the dated CSV corrects it. That's a real, known cost, not
an oversight — accepted deliberately over the alternative (nothing tracked
at all during the gap).

  1. Scrape every currently-active listing for the new territory (address,
     agent, agency, URL) and save it straight away.
  2. Also email it as a CSV to Rick, with a blank "First Seen" column, for
     him to get the real listing ages dated externally.
  3. Once dated, re-import the same CSV:

       python3 scraper.py --import-csv dated_file.csv --lga <id> --import-table listings

     import_csv() (see scraper.py) only corrects first_seen on rows that
     already exist — it never touches status, so anything cron has already
     found sold/removed/withdrawn by then is left exactly as cron set it,
     not silently reset back to active.

Sold data never had this problem (sold_date is already known), so the sold
side of hydration writes directly to Supabase as normal, same as always.

FLOW
  1. Order form inserts a row into `lgas` (see makebank_order.html).
  2. A Supabase Database Webhook fires on that INSERT and POSTs the new row
     here (see SETUP below).
  3. Listings → scraped, saved immediately, AND emailed as CSV for dating.
     Sold    → scraped and saved directly, same as any normal run.

CONCURRENCY — capped at 3 (14 Sep 2026)
  Stress-tested on the production VM: ~5-6 concurrent hydrations ran clean,
  8 concurrent caused realestate.com.au itself to start timing out
  (Playwright "Page.goto: Timeout 30000ms exceeded" — server-side pushback,
  not cookie burns, not VM resource limits). With no signup volume limit on
  how many new-territory webhooks could land at once (e.g. 20 people signing
  up together), nothing previously stopped every one of them firing a
  concurrent hydration at once. Capped to MAX_CONCURRENT_HYDRATIONS = 3 for
  real safety margin under the proven 6-8 ceiling — extra requests queue and
  run as a slot frees up, rather than firing immediately.

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
    HYDRATE_EMAIL_FROM       — defaults to reports@makebank.com.au

Run with:
    pip install fastapi uvicorn requests --break-system-packages
    uvicorn hydrate_new_territory:app --host 0.0.0.0 --port 8000
"""
import os
import io
import csv
import sys
import base64
import asyncio
import logging
import subprocess

import requests
from fastapi import FastAPI, Request, HTTPException

# Reuses the exact same scraping logic already fixed in scraper.py — no
# separate/duplicate implementation to drift out of sync.
from scraper import (
    get_supabase, CookiePool, URLCollector, scrape_details_playwright,
<<<<<<< HEAD
    run_scrape, run_reconcile, LGAStore, COOKIES_FILE, MAX_PAGES,
=======
    run_scrape, run_reconcile, COOKIES_FILE, MAX_PAGES,
>>>>>>> 0dca2e483f94fa05fe2a2e2ec1b85470e5034387
)

log = logging.getLogger("hydrate")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

WEBHOOK_SECRET = os.environ.get("HYDRATE_WEBHOOK_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO       = os.environ.get("HYDRATE_EMAIL_TO", "rick@rickjohnson.com.au")
EMAIL_FROM     = os.environ.get("HYDRATE_EMAIL_FROM", "reports@makebank.com.au")

# ── Concurrency queue ─────────────────────────────────────────────────────
# See the CONCURRENCY note in the module docstring for why this exists and
# how the number 3 was chosen. Each hydration still runs as its own OS
# subprocess (Playwright's sync API can't share this process's asyncio
# loop — see the comment on subprocess.Popen below) — this queue just
# controls how many of those subprocesses are allowed to be alive at once.
MAX_CONCURRENT_HYDRATIONS = 3
_active: list[subprocess.Popen] = []   # currently-running hydration subprocesses
_queue: list[dict] = []                # lga rows waiting for a free slot
_lock = asyncio.Lock()                 # guards both lists — single event loop, but be explicit


def _launch(lga_id: int) -> subprocess.Popen:
    # Same subprocess approach as before, just factored out so both the
    # immediate-launch path and the queue-drain path share one code path.
    # stdout/stderr deliberately NOT redirected to DEVNULL — leaving them
    # inherited means each subprocess's own log lines still show up in
    # `journalctl -u makebank-hydrate`, just tagged with its own PID.
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--lga-id", str(lga_id)],
    )


async def _drain_queue_loop():
    """
    Runs for the lifetime of the app. Every 5 seconds: drops any finished
    processes from _active, then launches queued hydrations to fill any
    freed slots. 5 seconds is deliberately coarse — hydrations run for
    minutes, so there's no need to poll tightly.
    """
    while True:
        await asyncio.sleep(5)
        async with _lock:
            _active[:] = [p for p in _active if p.poll() is None]
            while _queue and len(_active) < MAX_CONCURRENT_HYDRATIONS:
                lga = _queue.pop(0)
                log.info(f"Starting queued hydration for LGA {lga['id']} ({len(_active)+1}/{MAX_CONCURRENT_HYDRATIONS} active, {len(_queue)} still queued)")
                _active.append(_launch(lga['id']))


@app.on_event("startup")
async def _start_queue_drainer():
    asyncio.create_task(_drain_queue_loop())


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


def email_csv(lga_id: int, lga_name: str, csv_content: str, row_count: int, burned_count: int = 0):
    # LGA id is included everywhere here (filename, subject, body, and the
    # re-import command) — with hundreds of regions, the name alone isn't
    # enough to unambiguously match an inbox CSV back to the right --lga
    # number, and typing the id out by hand is exactly the kind of thing
    # worth eliminating once this isn't just a handful of test regions.
    safe_name = lga_name.replace(' ', '_').replace('/', '-')
    filename  = f"{lga_id}_{safe_name}_hydration.csv"

    if not RESEND_API_KEY:
        # No Resend yet — save it locally instead of just dumping raw CSV
        # text into the log, so today's regions still produce something
        # actually usable. Switch to email once Resend is set up in Railway.
        with open(filename, "w", encoding="utf-8") as f:
            f.write(csv_content)
        log.info(f"RESEND_API_KEY not set — saved to {filename} instead ({row_count} rows, {burned_count} cookies burned).")
        return
    attachment_b64 = base64.b64encode(csv_content.encode('utf-8')).decode('ascii')
    burned_note = (
        f"<p style=\"color:#B45309;\"><strong>{burned_count} cookie(s) burned</strong> during this run "
        f"(hit a 429 and got dropped from the pool).</p>"
        if burned_count > 0 else
        "<p>No cookies burned during this run.</p>"
    )
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": EMAIL_FROM,
            "to": [EMAIL_TO],
            "subject": f"New territory hydration — {lga_name} (LGA {lga_id}, {row_count} listings, {burned_count} burned)",
            "html": (
                f"<p>New territory <strong>{lga_name}</strong> (LGA <strong>{lga_id}</strong>) just scraped — "
                f"{row_count} currently active listings attached as CSV, with a blank "
                f"<strong>First Seen</strong> column.</p>"
                f"{burned_note}"
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
                f"<pre>python3 scraper.py --import-csv dated_file.csv --lga {lga_id} --import-table listings</pre>"
                f"<p>Full method with worked example in the HANDOVER doc.</p>"
            ),
            "attachments": [{
                "filename": filename,
                "content": attachment_b64,
            }],
        },
    )
    if resp.status_code >= 300:
        log.error(f"Resend send failed: {resp.status_code} {resp.text}")
    else:
        log.info(f"Hydration CSV emailed to {EMAIL_TO} ({row_count} rows, LGA {lga_id})")


def hydrate(lga: dict):
    """Runs in a background thread — the webhook response doesn't wait on this."""
    sb = get_supabase()
    lga_id   = lga['id']
    lga_name = lga.get('name', f"LGA {lga_id}")

    # Reverted to the shared full 30-cookie pool (13 Sep 2026) — see the
    # matching note in scraper.py's process_lga(). No slot claim/release
    # needed now that every run shares the same pool the old sequential
    # system always used.
    pool = CookiePool(COOKIES_FILE)

    # --- Listings: scrape, save immediately, still email the CSV for dating ---
    # Changed 23 Sep 2026: used to only email the CSV and leave `listings`
    # completely empty until Rick re-imported it dated, which meant this
    # territory sat fully dormant -- no cron, nothing tracked -- for however
    # long dating took, and anything withdrawn/sold in that window was lost
    # forever, never captured. Now: save immediately (first_seen = right
    # now, genuinely imprecise but real) and mark the LGA dated=true straight
    # away too, so cron picks it up from tonight regardless of when the CSV
    # comes back. import_csv() (scraper.py) only corrects first_seen on
    # already-existing rows when the dated CSV lands later -- it no longer
    # touches status, so anything cron has already found sold/removed by
    # then stays exactly as cron left it. Rick can now hand out a full
    # day's worth of new territories to a freelancer and not worry about
    # losing days of tracking while the CSV sits in their queue.
    try:
        log.info(f"Hydrating listings for {lga_name} (id={lga_id})")
        collector = URLCollector(pool, max_pages=MAX_PAGES)
        live_urls = collector.collect_urls(lga['search_url_listings'], known_urls=set())
        log.info(f"Found {len(live_urls)} active listings")
        rows = scrape_details_playwright(list(live_urls), pool) if live_urls else []

        store = LGAStore(sb, lga_id)
        saved = store.insert_new('listings', rows)
        log.info(f"Saved {saved} listing(s) for {lga_name} — first_seen = hydration time until the dated CSV corrects it")

        csv_content = build_csv(rows)
        email_csv(lga_id, lga_name, csv_content, len(rows), pool.burned_count)

        sb.table('lgas').update({'dated': True}).eq('id', lga_id).execute()
        log.info(f"LGA {lga_id} marked dated=true immediately — cron-eligible from tonight, not waiting on CSV dating")
    except Exception as e:
        log.error(f"Listings hydration failed for {lga_name}: {e}")

    # --- Sold: no dating problem (sold_date is already known) — save as normal ---
    try:
        log.info(f"Hydrating sold data for {lga_name}")
        run_scrape(sb, pool, lga, 'sold', MAX_PAGES)
    except Exception as e:
        log.error(f"Sold hydration failed for {lga_name}: {e}")

    # --- Seed the off-market backfill now, not on some later cron night ---
<<<<<<< HEAD
    # `sold` is now fully populated and `listings` already has this
    # territory's initial active snapshot (changed 23 Sep 2026 — see the
    # listings block above). Reconcile's off-market check still works
    # exactly the same: any sold URL with no matching listings row at all
    # (i.e. it wasn't active at hydration time either) gets backfilled as
    # off-market, same logic as every regular nightly reconcile. Doing it
    # here rather than waiting for the first real nightly run just means
    # this territory's dashboard is accurate from minute one instead of
    # showing a confusing one-off spike whenever reconcile first touches it.
=======
    # At this exact point, `sold` is fully populated but `listings` is
    # deliberately empty (the listings CSV above still needs manual dating
    # before it's imported) -- so every sold row here genuinely has no
    # listings counterpart yet, and reconcile's off-market logic will
    # correctly backfill all of it as a one-time seed. Found 23 Sep 2026:
    # without this, that exact backfill happens anyway, just silently on
    # whatever night this region's first real nightly reconcile runs,
    # dumping a confusing one-off spike into that night's "new listings"
    # count instead of being a clean, understood setup step. Once the dated
    # CSV is eventually imported, its real listings rows land on the same
    # (url, lga_id) upsert key as these seeded rows and simply overwrite
    # them with the correct data -- nothing needs cleaning up afterward.
>>>>>>> 0dca2e483f94fa05fe2a2e2ec1b85470e5034387
    try:
        log.info(f"Seeding off-market backfill for {lga_name}")
        run_reconcile(sb, lga)
    except Exception as e:
        log.error(f"Off-market seed reconcile failed for {lga_name}: {e}")

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

    lga_id = record["id"]

    # Launched as a genuinely separate OS process, not a background thread.
    # Playwright's sync API (used throughout scraper.py) cannot coexist with
    # an asyncio event loop anywhere in the same process — and this whole
    # app runs on Uvicorn, which *is* an asyncio event loop. A background
    # thread still shares the same process, so it still hit that conflict
    # (found 13 Sep 2026 — "Playwright Sync API inside the asyncio loop").
    # A subprocess has its own separate interpreter and no such loop, so it
    # behaves exactly like running this file's own --lga-id CLI mode by hand
    # from a terminal — which already works correctly.
    async with _lock:
        _active[:] = [p for p in _active if p.poll() is None]
        if len(_active) < MAX_CONCURRENT_HYDRATIONS:
            _active.append(_launch(lga_id))
            status = "hydration_started"
            log.info(f"Started hydration for LGA {lga_id} immediately ({len(_active)}/{MAX_CONCURRENT_HYDRATIONS} active)")
        else:
            _queue.append(record)
            status = "hydration_queued"
            log.info(f"Queued hydration for LGA {lga_id} — {MAX_CONCURRENT_HYDRATIONS} already running, position {len(_queue)} in queue")

    return {"status": status, "lga_id": lga_id, "active": len(_active), "queued": len(_queue)}


@app.get("/health")
def health():
    return {"status": "ok", "active_hydrations": len(_active), "queued_hydrations": len(_queue), "max_concurrent": MAX_CONCURRENT_HYDRATIONS}


# ── CLI — manual trigger, no webhook/Railway needed ──────────────────────────
# For testing before hosting is live: creates no HTTP server, just runs the
# exact same hydrate() function the webhook would, for one LGA at a time.
#
#   python3 hydrate_new_territory.py --lga-id 27
#
# Runs in the foreground (not backgrounded like the webhook does) so you can
# watch it work and see the email send confirmation before moving to the
# next one. This CLI mode bypasses the queue entirely — it's a direct manual
# trigger, same as always. Run these ONE AT A TIME, sequentially, yourself,
# same caution as before — the queue only protects the webhook path.
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Manually hydrate one LGA (bypasses the webhook and the queue)")
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
