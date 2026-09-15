"""
Single Railway service that serves everything makebank.com.au needs to point
at: the static dashboards (website, order form, Daily Brief, Weekly Report,
export tool, admin) AND the new-territory hydration webhook, all from one app.

This replaces running hydrate_new_territory.py as a standalone service —
its webhook logic is now included here as a router, and static files are
mounted alongside it, so one Railway deployment = one domain to point DNS at.

Directory layout expected:
    main.py
    hydrate_new_territory.py   (unchanged — its router is imported below)
    scraper.py                 (import_csv() reused directly, see below)
    public/
        index.html             (the website — served at /)
        order.html             (the order form — served at /order.html)
        daily-brief.html
        weekly-report.html
        export.html
        admin.html             (internal — has the CSV import form)

Run locally:
    pip install fastapi uvicorn requests python-multipart --break-system-packages
    uvicorn main:app --host 0.0.0.0 --port 8000

Railway start command (Settings → Deploy):
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from hydrate_new_territory import app as hydrate_app  # reuses its routes as-is
from scraper import import_csv, get_supabase  # reuses the already-proven import logic, not reinvented here

app = FastAPI()

# Mount the hydration webhook's routes (/webhook/new-territory, /health)
# directly onto this app so they live at the same domain.
for route in hydrate_app.routes:
    app.router.routes.append(route)

PUBLIC_DIR = Path(__file__).parent / "public"


@app.get("/")
def home():
    return FileResponse(PUBLIC_DIR / "index.html")


@app.post("/api/import-csv")
async def import_csv_endpoint(lga_id: int = Form(...), file: UploadFile = File(...)):
    """
    Backs admin.html's CSV-import form. Deliberately reuses import_csv() from
    scraper.py rather than re-parsing CSVs in JavaScript — same date-parsing,
    same ISO-format validation, same behaviour whether triggered here or from
    the command line.

    NOT behind a separate secret — same trust model as the rest of admin.html
    already documented in admin_schema.sql: this page isn't behind real auth,
    its URL just isn't linked anywhere public. A secret here would have to be
    embedded in this page's own client-side JS to be usable, which wouldn't
    actually add protection — so, consistent with the rest of the page,
    obscurity is the only barrier for now. Worth real auth later if that
    changes.
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


# Serves /order.html, /daily-brief.html, /weekly-report.html, /export.html,
# /admin.html exactly as named in public/ — e.g. makebank.com.au/order.html
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")

