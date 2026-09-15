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


# Serves /order.html, /dashboard.html, /export.html, /admin.html exactly as
# named in public/ -- e.g. makebank.com.au/order.html
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
