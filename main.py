"""
Single Railway service that serves everything makebank.com.au needs to point
at: the static dashboards (website, order form, dashboard, export tool,
admin) AND relays the new-territory hydration webhook to the scraper VM
(Onidel, Sydney) instead of running the scrape itself — Railway's own IP is
flagged by realestate.com.au (see SCRAPER_ARCHITECTURE_DAILY.md), so all
actual scraping now happens on the VM, which has a genuine Australian IP.
Railway's job here is just: receive the webhook, check the secret, forward
the payload to the VM, return its response.

Directory layout expected:
    main.py
    hydrate_new_territory.py   (unchanged — kept for reference/local testing,
                                 its own /webhook route is NOT mounted here)
    scraper.py                 (import_csv() reused directly, see below)
    public/
        index.html    (the website — served at /)
        order.html    (the order form — served at /order.html)
        dashboard.html
        export.html
        admin.html    (internal — has the CSV import form)

Run locally:
    pip install fastapi uvicorn requests python-multipart --break-system-packages
    uvicorn main:app --host 0.0.0.0 --port 8000

Railway start command (Settings -> Deploy):
    uvicorn main:app --host 0.0.0.0 --port $PORT

Env vars needed (Railway):
    HYDRATE_WEBHOOK_SECRET   -- shared secret, must match both the Supabase
                                webhook header AND the VM's own .env value
    HYDRATE_VM_URL           -- e.g. http://155.103.51.81:8000/webhook/new-territory
"""
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from scraper import import_csv, get_supabase  # reuses the already-proven import logic, not reinvented here

app = FastAPI()

WEBHOOK_SECRET = os.environ.get("HYDRATE_WEBHOOK_SECRET", "")
VM_URL = os.environ.get("HYDRATE_VM_URL", "")  # e.g. http://155.103.51.81:8000/webhook/new-territory
RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "")  # Svix signing secret from the Resend dashboard

PUBLIC_DIR = Path(__file__).parent / "public"


@app.get("/")
def home():
    return FileResponse(PUBLIC_DIR / "index.html")


def _parse_ts(value) -> Optional[datetime]:
    """Supabase/Postgres timestamptz -> aware datetime. Accepts '...Z',
    '...+00:00', '...+00' and a space instead of 'T'."""
    if not value:
        return None
    s = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():   # '+00' -> '+00:00'
        s = s + ":00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _claim_dispatch(sb, lga_id: int, stale_before: Optional[datetime] = None) -> bool:
    """Atomically marks a territory as dispatched. Only succeeds if nobody has
    dispatched it yet (or, for a retry, if its last dispatch is older than
    stale_before) -- so the INSERT webhook, the VM's 15-minute dispatcher and
    the admin "Hydrate now" button can never send the same region twice."""
    now_iso = datetime.now(timezone.utc).isoformat()
    q = sb.table("lgas").update({"hydrate_dispatched_at": now_iso}).eq("id", lga_id)
    if stale_before:
        q = q.or_(f"hydrate_dispatched_at.is.null,hydrate_dispatched_at.lt.{stale_before.isoformat()}")
    else:
        q = q.is_("hydrate_dispatched_at", "null")
    return bool(q.execute().data)


def _unclaim_dispatch(sb, lga_id: int):
    sb.table("lgas").update({"hydrate_dispatched_at": None}).eq("id", lga_id).execute()


def _forward_to_vm(payload: dict) -> dict:
    if not VM_URL:
        raise HTTPException(status_code=500, detail="HYDRATE_VM_URL not configured")
    try:
        resp = requests.post(
            VM_URL,
            json=payload,
            headers={"Authorization": f"Bearer {WEBHOOK_SECRET}"},
            timeout=10,  # VM's own handler returns immediately (fires a background subprocess) -- this isn't waiting for the whole scrape
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"VM relay failed: {e}")
    return resp.json()


@app.post("/webhook/new-territory")
async def relay_new_territory(request: Request):
    """
    Relays the Supabase webhook payload to the scraper VM instead of running
    the hydration here. This endpoint's job is only: check the secret,
    forward the payload, return the VM's response.

    Scheduled hydration (added 28 Sep 2026, see sql/13_scheduled_hydration.sql):
    a row with hydrate_after in the future is NOT forwarded -- the VM's
    dispatch_scheduled_hydrations.py cron sends it once it's due. Rows with
    no hydrate_after (order form, admin.html) behave exactly as before.
    """
    if WEBHOOK_SECRET:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    if not VM_URL:
        raise HTTPException(status_code=500, detail="HYDRATE_VM_URL not configured")

    payload = await request.json()
    record = payload.get("record") or {}
    hydrate_after = _parse_ts(record.get("hydrate_after"))

    if hydrate_after is not None:
        lga_id = record.get("id")
        if hydrate_after > datetime.now(timezone.utc):
            return {"status": "scheduled", "lga_id": lga_id, "hydrate_after": hydrate_after.isoformat()}
        # Already due -- claim it first so the 15-minute dispatcher can't send it again.
        sb = get_supabase()
        if not _claim_dispatch(sb, lga_id):
            return {"status": "already_dispatched", "lga_id": lga_id}
        try:
            return _forward_to_vm(payload)
        except HTTPException:
            _unclaim_dispatch(sb, lga_id)   # let the dispatcher pick it up on its next pass
            raise

    return _forward_to_vm(payload)


@app.get("/health")
def health():
    return {"status": "ok", "relay_target": VM_URL or "not configured"}


@app.post("/api/send-day1-email")
async def send_day1_email(request: Request):
    """Called by admin.html right after it inserts a new free_recipients
    row — fires the Day-1 email in this same request rather than waiting
    for the next cron run. send_trial_emails.py's day1 stage is deliberately
    NOT scheduled (see that file); this is the only normal path a Day-1
    email goes out through."""
    payload = await request.json()
    recipient_id = payload.get("recipient_id")
    if not recipient_id:
        raise HTTPException(status_code=400, detail="recipient_id required")
    try:
        from send_trial_emails import send_day1_immediate
        send_day1_immediate(recipient_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "sent"}


@app.post("/api/import-csv")
async def import_csv_endpoint(lga_id: int = Form(...), file: UploadFile = File(...)):
    """
    Backs admin.html's CSV-import form. Deliberately reuses import_csv() from
    scraper.py rather than re-parsing CSVs in JavaScript -- same date-parsing,
    same ISO-format validation, same behaviour whether triggered here or from
    the command line.
    """
    if file_ext := Path(file.filename or "").suffix.lower():
        if file_ext != ".csv":
            raise HTTPException(status_code=400, detail="File must be a .csv")

    contents = await file.read()
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        sb = get_supabase()
        import_csv(tmp_path, "listings", lga_id, sb)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)

    return {"status": "imported", "lga_id": lga_id}


# ── Dashboard access gate (free-trial paywall) ───────────────────────────────
# dashboard.html calls this BEFORE loading any listings/sold data whenever the
# link carries a `recipient` param (free-trial links only -- paid/house
# dashboards have no recipient param and skip this check entirely, unchanged
# from today's behaviour). Runs server-side with the service-role key so the
# check can't be defeated by editing the page's own JS or the URL's cosmetic
# Day= value -- trial_ends_at on the DB row is the only thing that matters.
@app.get("/api/dashboard-access")
def dashboard_access(lga: int, recipient: str):
    sb = get_supabase()

    rec_rows = (
        sb.table("free_recipients")
        .select("id,lga_id,principal_name,email,phone,unsubscribed_at,status,trial_ends_at")
        .eq("id", recipient)
        .eq("lga_id", lga)
        .limit(1)
        .execute()
    ).data
    if not rec_rows:
        # Unknown/mismatched id -- fail closed. A `recipient` param being
        # present at all means this link is supposed to be gated.
        return {"access": False, "reason": "not_found"}

    r = rec_rows[0]
    lga_rows = (
        sb.table("lgas").select("name,search_url_listings").eq("id", lga).limit(1).execute()
    ).data
    region_url = lga_rows[0]["search_url_listings"] if lga_rows else ""
    lga_name = lga_rows[0]["name"] if lga_rows else ""

    if r["unsubscribed_at"] is not None:
        access, reason = False, "unsubscribed"
    elif r["status"] == "converted":
        access, reason = True, "converted"
    elif r["status"] == "cancelled":
        access, reason = False, "cancelled"
    else:  # status == 'trial'
        import datetime as _dt
        trial_ends = _dt.datetime.fromisoformat(r["trial_ends_at"].replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        access = now < trial_ends
        reason = "trial_active" if access else "trial_expired"

    return {
        "access": access,
        "reason": reason,
        "principal_name": r["principal_name"],
        "email": r["email"],
        "phone": r["phone"] or "",
        "region_url": region_url,
        "lga_name": lga_name,
        "trial_ends_at": r["trial_ends_at"],
    }


# ── Resend webhook receiver (email engagement tracking) ─────────────────────
# Logs delivered/opened/clicked/bounced events against the free_recipients
# row that email was sent to, via a `recipient_id` tag set at send time (see
# build_trial_emails.py / send_trial_emails.py / send_daily_emails.py) --
# powers the engagement admin page. Signature verification follows the
# standard Svix webhook scheme Resend uses (svix-id.svix-timestamp.body,
# HMAC-SHA256, base64) -- written against Resend/Svix's published spec since
# this repo has no live webhook to test against yet; verify against a real
# delivery before relying on it in production.
@app.post("/webhook/resend-events")
async def resend_events(request: Request):
    body = await request.body()

    if RESEND_WEBHOOK_SECRET:
        import base64
        import hashlib
        import hmac as _hmac

        svix_id = request.headers.get("svix-id", "")
        svix_timestamp = request.headers.get("svix-timestamp", "")
        svix_signature = request.headers.get("svix-signature", "")
        try:
            # Bug found 23 Sep 2026: .split("_")[-1] only correctly strips
            # the "whsec_" prefix when the secret itself has no other
            # underscores in it -- if it does, this silently grabs the wrong
            # substring, feeds invalid base64 into b64decode(), and throws
            # an unhandled exception (a bare 500 with no detail, in
            # production). Explicit prefix-length slicing is correct
            # regardless of what characters the secret has.
            secret_bytes = base64.b64decode(RESEND_WEBHOOK_SECRET.strip()[len("whsec_"):]) \
                if RESEND_WEBHOOK_SECRET.startswith("whsec_") else RESEND_WEBHOOK_SECRET.strip().encode()
            signed_content = f"{svix_id}.{svix_timestamp}.{body.decode()}".encode()
            expected = base64.b64encode(_hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode()
            provided_sigs = [s.split(",", 1)[1] for s in svix_signature.split(" ") if "," in s]
            verified = expected in provided_sigs
        except Exception as e:
            # Fail closed but cleanly -- a 401 with a real reason beats an
            # unhandled crash, which is exactly what happened here (bare 500,
            # no detail) before this was wrapped. Plain print(), not a
            # logger -- main.py has never set one up, and log.error() here
            # would itself throw NameError and mask the real error.
            print(f"Resend webhook signature check failed to run: {e}")
            raise HTTPException(status_code=401, detail="Signature verification error") from e
        if not verified:
            raise HTTPException(status_code=401, detail="Invalid signature")

    import json
    payload = json.loads(body)

    event_type_map = {
        "email.delivered": "delivered",
        "email.opened": "opened",
        "email.clicked": "clicked",
        "email.bounced": "bounced",
        "email.complained": "complained",
    }
    event_type = event_type_map.get(payload.get("type", ""))
    if not event_type:
        return {"status": "ignored"}  # some other event type we don't track

    data = payload.get("data", {})
    # Confirmed via Resend's own docs and the actual crash (23 Sep 2026):
    # data.tags is a plain {key: value} object, e.g. {"recipient_id": "xxx"}
    # -- NOT an array of {name, value} pairs. The array-shaped version is
    # only how tags look when you SEND an email; the webhook payload
    # represents them differently. Iterating the old (wrong) way iterated
    # the dict's keys (strings), and calling .get() on a string is exactly
    # the AttributeError that was crashing every single delivery.
    tags = data.get("tags") or {}
    recipient_id = tags.get("recipient_id")
    if not recipient_id:
        return {"status": "ignored", "reason": "no recipient_id tag"}

    sb = get_supabase()
    sb.table("email_events").insert({
        "recipient_id": recipient_id,
        "resend_email_id": data.get("email_id"),
        "event_type": event_type,
    }).execute()

    return {"status": "logged"}


@app.post("/api/mark-converted")
async def mark_converted(request: Request):
    """Called by order.html right after it creates the order, whenever the
    signup arrived via a renew link (carries recipient_id — see
    build_trial_emails.py's _renew_link and dashboard.html's paywall CTA).
    Flips that free_recipients row to 'converted' so the day-25/expiry/
    feedback stages in send_trial_emails.py stop firing against a customer
    who already paid. Optimistic, same as the rest of the order flow (the
    territory/order themselves are created before Stripe payment confirms
    too) -- not gated on payment success, since that reconciliation webhook
    doesn't exist yet either (see HANDOVER doc)."""
    payload = await request.json()
    recipient_id = payload.get("recipient_id")
    if not recipient_id:
        raise HTTPException(status_code=400, detail="recipient_id required")
    try:
        sb = get_supabase()
        sb.table("free_recipients").update({"status": "converted"}).eq("id", recipient_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "converted"}


@app.get("/api/recipient-detail")
def recipient_detail(id: str):
    """Full contact detail for one free_recipients row -- served through the
    service-role key rather than widening the anon SELECT grant (which
    deliberately excludes email/phone/agency/source_url -- see
    6_trial_lifecycle_schema.sql). Keeps PII off the anon key entirely
    instead of exposing it to anyone who copies that key out of any page's
    source, which is trivial to do. Used by admin_engagement.html's
    row-expand."""
    sb = get_supabase()
    rows = (
        sb.table("free_recipients")
        .select("id,principal_name,agency,email,phone,consent_basis,source_url,unsubscribe_token")
        .eq("id", id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status_code=404, detail="Not found")
    return rows[0]


# ── Bulk / scheduled hydration (admin_hydration.html) ───────────────────────
# Added 28 Sep 2026. Everything runs through the service-role key here rather
# than widening anon grants on `lgas`. Deliberately narrow: the only rows
# these endpoints can cancel or re-dispatch are scheduled ones (hydrate_after
# set) that haven't finished hydrating -- nothing live can be touched.
STUCK_AFTER_HOURS = 18   # dispatched this long ago and still not dated = assume lost (e.g. VM restart dropped its in-memory queue)


def _with_sort(url: str, sort_value: str) -> str:
    # Python twin of admin.html's withSort() -- forces REA's newest-first sort,
    # which checkpoint dating and the sold cutoff both depend on.
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["activeSort"] = sort_value
    return urlunsplit(parts._replace(query=urlencode(q)))


def _derive_sold_url(buy_url: str) -> str:
    return _with_sort(buy_url.replace("/buy/", "/sold/").replace("/buy?", "/sold?"), "solddate")


def _validate_region_url(url: str) -> Optional[str]:
    if "realestate.com.au" not in url:
        return "not a realestate.com.au link"
    if "/buy/" not in url and "/buy?" not in url:
        return "needs to be a /buy/ search URL"
    if "list-1" not in url:
        return "missing 'list-1' (open page 1 of the search results and copy that URL)"
    return None


@app.get("/api/hydration-queue")
def hydration_queue(limit: int = 300):
    sb = get_supabase()
    rows = (
        sb.table("lgas")
        .select("id,name,created_at,hydrate_after,hydrate_dispatched_at,dated,client_ready,active")
        .not_.is_("hydrate_after", "null")
        .order("hydrate_after", desc=True)
        .order("id", desc=True)
        .limit(min(max(limit, 1), 1000))
        .execute()
    ).data
    return {"rows": rows, "stuck_after_hours": STUCK_AFTER_HOURS, "server_time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/hydration-queue")
async def schedule_hydrations(request: Request):
    """Body: {"hydrate_after": ISO timestamp, "regions": [{"name": ..., "url": ...}, ...]}
    Inserts each region as a normal territory with hydrate_after set. The
    Supabase INSERT webhook still fires, and the relay above ignores it until
    it's due. Regions whose listings URL already exists are skipped."""
    payload = await request.json()
    when = _parse_ts(payload.get("hydrate_after"))
    regions = payload.get("regions") or []
    if when is None:
        raise HTTPException(status_code=400, detail="hydrate_after required")
    if not regions:
        raise HTTPException(status_code=400, detail="No regions supplied")
    if len(regions) > 200:
        raise HTTPException(status_code=400, detail="Max 200 regions per batch")

    rows, errors = [], []
    for i, r in enumerate(regions, 1):
        name = (r.get("name") or "").strip()
        url = (r.get("url") or "").strip()
        problem = "missing name" if not name else _validate_region_url(url)
        if problem:
            errors.append(f"Line {i} ({name or url or 'blank'}): {problem}")
            continue
        listings_url = _with_sort(url, "list-date")
        rows.append({
            "name": name[:200],
            "active": True,
            "search_url_listings": listings_url,
            "search_url_sold": _derive_sold_url(listings_url),
            "hydrate_after": when.isoformat(),
        })
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    sb = get_supabase()
    existing = {
        e["search_url_listings"]: e for e in (
            sb.table("lgas").select("id,name,search_url_listings")
            .in_("search_url_listings", [r["search_url_listings"] for r in rows])
            .execute()
        ).data
    }
    seen, to_insert, skipped = set(), [], []
    for r in rows:
        dup = existing.get(r["search_url_listings"])
        if dup:
            skipped.append(f"{r['name']} (already exists as LGA {dup['id']} — {dup['name']})")
        elif r["search_url_listings"] in seen:
            skipped.append(f"{r['name']} (duplicate URL in this batch)")
        else:
            seen.add(r["search_url_listings"])
            to_insert.append(r)

    inserted = sb.table("lgas").insert(to_insert).execute().data if to_insert else []
    return {
        "scheduled": [{"id": r["id"], "name": r["name"]} for r in inserted],
        "skipped": skipped,
        "hydrate_after": when.isoformat(),
    }


@app.post("/api/hydration-queue/{lga_id}/now")
def hydrate_now(lga_id: int):
    """Sends a scheduled region to the VM immediately. Also the Retry path for
    a region that was dispatched but never hydrated (lost from the VM's
    in-memory queue) -- allowed only once its dispatch is STUCK_AFTER_HOURS old."""
    sb = get_supabase()
    rows = sb.table("lgas").select("*").eq("id", lga_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Region not found")
    row = rows[0]
    if row.get("hydrate_after") is None:
        raise HTTPException(status_code=400, detail="Not a scheduled region — use the normal hydration path")
    if row.get("dated"):
        raise HTTPException(status_code=409, detail="Already hydrated")

    stale_before = datetime.now(timezone.utc) - timedelta(hours=STUCK_AFTER_HOURS)
    if not _claim_dispatch(sb, lga_id, stale_before=stale_before):
        raise HTTPException(status_code=409, detail=f"Already dispatched — can only retry once it's been {STUCK_AFTER_HOURS}h with no hydration")
    try:
        result = _forward_to_vm({"type": "INSERT", "table": "lgas", "record": row})
    except HTTPException:
        _unclaim_dispatch(sb, lga_id)
        raise
    return {"status": "dispatched", "lga_id": lga_id, "vm": result}


@app.delete("/api/hydration-queue/{lga_id}")
def cancel_scheduled(lga_id: int):
    """Removes a scheduled region that hasn't been sent to the VM yet. Nothing
    has been scraped for it at that point, so deleting the row is clean."""
    sb = get_supabase()
    deleted = (
        sb.table("lgas").delete()
        .eq("id", lga_id)
        .not_.is_("hydrate_after", "null")
        .is_("hydrate_dispatched_at", "null")
        .or_("dated.is.null,dated.eq.false")
        .execute()
    ).data
    if not deleted:
        raise HTTPException(status_code=409, detail="Can only cancel regions that haven't been dispatched yet")
    return {"status": "cancelled", "lga_id": lga_id}


# Serves /order.html, /dashboard.html, /export.html, /admin.html exactly as
# named in public/ -- e.g. makebank.com.au/order.html
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
