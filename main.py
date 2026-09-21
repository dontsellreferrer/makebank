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
from pathlib import Path

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


@app.post("/webhook/new-territory")
async def relay_new_territory(request: Request):
    """
    Relays the Supabase webhook payload to the scraper VM instead of running
    the hydration here. This endpoint's job is only: check the secret,
    forward the payload, return the VM's response.
    """
    if WEBHOOK_SECRET:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    if not VM_URL:
        raise HTTPException(status_code=500, detail="HYDRATE_VM_URL not configured")

    payload = await request.json()

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
        secret_bytes = base64.b64decode(RESEND_WEBHOOK_SECRET.split("_")[-1]) \
            if RESEND_WEBHOOK_SECRET.startswith("whsec_") else RESEND_WEBHOOK_SECRET.encode()
        signed_content = f"{svix_id}.{svix_timestamp}.{body.decode()}".encode()
        expected = base64.b64encode(_hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode()
        provided_sigs = [s.split(",", 1)[1] for s in svix_signature.split(" ") if "," in s]
        if expected not in provided_sigs:
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
    tags = data.get("tags") or []
    recipient_id = next((t.get("value") for t in tags if t.get("name") == "recipient_id"), None)
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


# Serves /order.html, /dashboard.html, /export.html, /admin.html exactly as
# named in public/ -- e.g. makebank.com.au/order.html
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
