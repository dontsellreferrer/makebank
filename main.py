"""
Single Railway service that serves everything makebank.com.au needs to point
at: the static dashboards (website, order form, Daily Brief, Weekly Report,
export tool) AND the new-territory hydration webhook, all from one app.

This replaces running hydrate_new_territory.py as a standalone service —
its webhook logic is now included here as a router, and static files are
mounted alongside it, so one Railway deployment = one domain to point DNS at.

Directory layout expected:
    main.py
    hydrate_new_territory.py   (unchanged — its router is imported below)
    public/
        index.html             (the website — served at /)
        order.html             (the order form — served at /order.html)
        daily-brief.html
        weekly-report.html
        export.html

Run locally:
    pip install fastapi uvicorn requests --break-system-packages
    uvicorn main:app --host 0.0.0.0 --port 8000

Railway start command (Settings → Deploy):
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from hydrate_new_territory import app as hydrate_app  # reuses its routes as-is

app = FastAPI()

# Mount the hydration webhook's routes (/webhook/new-territory, /health)
# directly onto this app so they live at the same domain.
for route in hydrate_app.routes:
    app.router.routes.append(route)

PUBLIC_DIR = Path(__file__).parent / "public"


@app.get("/")
def home():
    return FileResponse(PUBLIC_DIR / "index.html")


# Serves /order.html, /daily-brief.html, /weekly-report.html, /export.html
# exactly as named in public/ — e.g. makebank.com.au/order.html
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
