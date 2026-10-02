"""
Daily management summary — the "did anything fail last night" email, sent to
Rick only, never to customers. Replaces the old weekly REA-count comparison
report: deliberately NOT that level of detail — just per-region pass/fail
against the `runs` table, with a dashboard link for anything that needs a
closer look.

A region is flagged FAILED if, within the last 26 hours (covers the full
midnight -> 3am cycle plus stagger, with slack for timing drift), any of its
five expected run types is either missing entirely (silent crash — the
run_reconcile() bug fixed 21 Sep 2026 is exactly the kind of thing that used
to cause this) or logged with status='error'.

Usage:
    python3 send_ops_summary_email.py
"""
import datetime as dt
import logging
import urllib.parse
import html

from dotenv import load_dotenv
load_dotenv()

import requests

from ops_health import get_latest_runs as _get_latest_runs, lga_problems

from fetch_and_build_daily_email import SUPABASE_URL, SUPABASE_KEY

log = logging.getLogger("send_ops_summary_email")
logging.basicConfig(level=logging.INFO)

RESEND_API_KEY = __import__("os").environ.get("RESEND_API_KEY", "")
EMAIL_FROM = __import__("os").environ.get("HYDRATE_EMAIL_FROM", "reports@makebank.com.au")
DASHBOARD_BASE_URL = __import__("os").environ.get("DASHBOARD_BASE_URL", "https://makebank.com.au/dashboard.html")
# Rick's own inbox — this email never goes to a customer.
OPS_REPORT_EMAIL = __import__("os").environ.get("OPS_REPORT_EMAIL", "REPLACE_WITH_OPS_EMAIL")

ORANGE = "#F68408"
NAVY = "#14243D"
GREEN = "#16A34A"
RED = "#E05252"
FONT = "Arial,Helvetica,sans-serif"


def sb_get(table: str, params: dict) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=params,
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    r.raise_for_status()
    return r.json()


def get_active_lgas() -> list[dict]:
    return sb_get("lgas", {"active": "eq.true", "dated": "eq.true", "select": "id,name", "order": "name.asc"})


def get_latest_runs(cutoff_iso: str) -> dict:
    return _get_latest_runs(SUPABASE_URL, SUPABASE_KEY, cutoff_iso)


def build_region_results(lgas: list[dict], latest_runs: dict) -> list[dict]:
    results = []
    for lga in lgas:
        problems = lga_problems(lga["id"], latest_runs)
        results.append({"lga": lga, "ok": not problems, "problems": problems})
    return results


def get_hydrations(cutoff_iso: str) -> list[dict]:
    """Every hydrate_listings run in the window (added 2 Oct 2026). A failed
    hydration never sets dated=true, so the per-region section above can't
    see it -- this is the only place it shows up."""
    return sb_get("runs", {
        "run_type": "eq.hydrate_listings", "run_at": f"gte.{cutoff_iso}",
        "select": "lga_id,status,error_msg,new_count,updated_count,run_at",
        "order": "run_at.asc",
    })


def get_burn_runs(cutoff_iso: str) -> list[dict] | None:
    """Runs in the window that hit at least one HTTP 429. None if the
    runs.burns column doesn't exist yet (sql/14_runs_burns.sql not run)."""
    try:
        return sb_get("runs", {
            "burns": "gt.0", "run_at": f"gte.{cutoff_iso}",
            "select": "lga_id,run_type,burns,run_at",
            "order": "burns.desc",
        })
    except requests.HTTPError as e:
        log.warning(f"Couldn't read runs.burns ({e}) — has sql/14_runs_burns.sql been run?")
        return None


def get_lga_names(ids: set) -> dict:
    if not ids:
        return {}
    rows = sb_get("lgas", {"id": f"in.({','.join(str(i) for i in sorted(ids))})", "select": "id,name"})
    return {r["id"]: r["name"] for r in rows}


def dashboard_link(lga: dict) -> str:
    return f"{DASHBOARD_BASE_URL}?lga={lga['id']}&lgaName={urllib.parse.quote(lga['name'])}"


def build_hydration_section(hydrations: list[dict], names: dict) -> str:
    if not hydrations:
        return f'''
      <div style="font-family:{FONT};font-size:11px;font-weight:700;color:#9a9a9a;text-transform:uppercase;letter-spacing:0.05em;margin:28px 0 8px;">Hydrations (last 24h)</div>
      <div style="font-family:{FONT};font-size:12px;color:#9a9a9a;">None ran.</div>'''
    bad = [h for h in hydrations if h["status"] != "ok"]
    heading_color = RED if bad else "#9a9a9a"
    heading = f"Hydrations (last 24h) — {len(bad)} need a look" if bad else f"Hydrations (last 24h) — all {len(hydrations)} clean"

    def row(h):
        ok = h["status"] == "ok"
        mark = f'<span style="color:{GREEN};">&#10003;</span>' if ok else f'<span style="color:{RED};font-weight:700;">&#10007;</span>'
        name = html.escape(names.get(h["lga_id"], f"LGA {h['lga_id']}"))
        counts = f'{h.get("new_count") or 0:,} saved of {h.get("updated_count") or 0:,} collected'
        err = "" if ok else f'<br><span style="color:{RED};">{html.escape(h.get("error_msg") or h["status"])}</span>'
        return f'''<tr>
          <td style="padding:6px 12px;border-bottom:1px solid #f3f3f3;font-family:{FONT};font-size:12px;color:#4a4a4a;">
            {mark} {name} <span style="color:#c0c0c0;">(LGA {h['lga_id']})</span>
          </td>
          <td style="padding:6px 12px;border-bottom:1px solid #f3f3f3;font-family:{FONT};font-size:11px;color:#4a4a4a;text-align:right;">{counts}{err}</td>
        </tr>'''

    return f'''
      <div style="font-family:{FONT};font-size:11px;font-weight:700;color:{heading_color};text-transform:uppercase;letter-spacing:0.05em;margin:28px 0 8px;">{heading}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {''.join(row(h) for h in hydrations)}
      </table>'''


def build_burns_section(burn_runs: list[dict] | None, names: dict) -> str:
    title = f'<div style="font-family:{FONT};font-size:11px;font-weight:700;color:#9a9a9a;text-transform:uppercase;letter-spacing:0.05em;margin:28px 0 8px;">429 cookie burns (last 24h)</div>'
    if burn_runs is None:
        return title + f'<div style="font-family:{FONT};font-size:12px;color:#9a9a9a;">Not tracked yet — run sql/14_runs_burns.sql.</div>'
    if not burn_runs:
        return title + f'<div style="font-family:{FONT};font-size:12px;color:{GREEN};">&#10003; None.</div>'
    total = sum(r["burns"] for r in burn_runs)
    rows = "".join(f'''<tr>
          <td style="padding:6px 12px;border-bottom:1px solid #f3f3f3;font-family:{FONT};font-size:12px;color:#4a4a4a;">
            {html.escape(names.get(r["lga_id"], f"LGA {r['lga_id']}"))} <span style="color:#c0c0c0;">(LGA {r['lga_id']}) · {html.escape(r["run_type"])}</span>
          </td>
          <td style="padding:6px 12px;border-bottom:1px solid #f3f3f3;font-family:{FONT};font-size:12px;color:{ORANGE};font-weight:700;text-align:right;">{r["burns"]}</td>
        </tr>''' for r in burn_runs)
    return title + f'''
      <div style="font-family:{FONT};font-size:13px;color:{ORANGE};font-weight:600;margin-bottom:8px;">{total} total across {len(burn_runs)} run(s)</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'''


def build_email_html(results: list[dict], hydration_section: str = "", burns_section: str = "") -> str:
    failed = [r for r in results if not r["ok"]]
    ok = [r for r in results if r["ok"]]

    def failed_row(r):
        # Bug found 24 Sep 2026: raw exception text like
        # "<ConnectionTerminated error_code:1, ...>" was inserted straight
        # into the HTML with no escaping -- the angle brackets made the
        # email client treat it as an unrecognised tag and silently hide it,
        # so the report showed "reconcile:" with nothing after the colon.
        # Escape each problem string individually, THEN join with <br> --
        # escaping the joined result instead would also escape the <br>
        # tags themselves and collapse every line onto one.
        detail = "<br>".join(html.escape(p) for p in r["problems"])
        return f'''<tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-family:{FONT};font-size:13px;color:#1a1a1a;">
            <span style="color:{RED};font-weight:700;">&#10007;</span>
            <a href="{dashboard_link(r['lga'])}" style="color:{NAVY};font-weight:600;text-decoration:none;">{r['lga']['name']}</a>
            <span style="color:#9a9a9a;font-weight:400;"> (LGA {r['lga']['id']})</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-family:{FONT};font-size:11px;color:{RED};">{detail}</td>
        </tr>'''

    def ok_row(r):
        return f'''<tr>
          <td colspan="2" style="padding:6px 12px;border-bottom:1px solid #f3f3f3;font-family:{FONT};font-size:12px;color:#4a4a4a;">
            <span style="color:{GREEN};">&#10003;</span>
            <a href="{dashboard_link(r['lga'])}" style="color:#4a4a4a;text-decoration:none;">{r['lga']['name']}</a>
            <span style="color:#c0c0c0;"> (LGA {r['lga']['id']})</span>
          </td>
        </tr>'''

    failed_section = f'''
      <div style="font-family:{FONT};font-size:13px;font-weight:700;color:{RED};margin:20px 0 8px;">
        {len(failed)} region(s) need a look
      </div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;">
        {''.join(failed_row(r) for r in failed)}
      </table>''' if failed else f'''
      <div style="font-family:{FONT};font-size:14px;color:{GREEN};font-weight:600;margin:20px 0;">
        &#10003; All {len(results)} region(s) ran clean last night.
      </div>'''

    ok_section = f'''
      <div style="font-family:{FONT};font-size:11px;font-weight:700;color:#9a9a9a;text-transform:uppercase;letter-spacing:0.05em;margin:24px 0 8px;">All clear ({len(ok)})</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {''.join(ok_row(r) for r in ok)}
      </table>''' if ok and failed else ""  # skip the redundant "all clear" list when nothing failed at all

    return f'''<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#F1F2F4;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F2F4;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td style="background:{NAVY};border-radius:14px 14px 0 0;padding:24px 28px;">
    <div style="font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,0.4);">MakeBank Ops</div>
    <div style="font-family:{FONT};font-size:18px;font-weight:700;color:#ffffff;margin-top:4px;">Nightly run summary</div>
  </td></tr>
  <tr><td style="background:#ffffff;border-radius:0 0 14px 14px;padding:8px 28px 28px;">
    {failed_section}
    {ok_section}
    {hydration_section}
    {burns_section}
  </td></tr>
</table>
</td></tr>
</table>
</body></html>'''


def send_email(html: str, subject: str):
    if not RESEND_API_KEY:
        log.error("RESEND_API_KEY not set — logging instead of sending.")
        return
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": EMAIL_FROM, "to": [OPS_REPORT_EMAIL], "subject": subject, "html": html},
    )
    if resp.status_code >= 300:
        log.error(f"Ops summary send failed: {resp.status_code} {resp.text}")
    else:
        log.info("Ops summary sent.")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(hours=26)).isoformat()

    lgas = get_active_lgas()
    latest_runs = get_latest_runs(cutoff)
    results = build_region_results(lgas, latest_runs)
    failed_count = sum(1 for r in results if not r["ok"])

    cutoff_24h = (now - dt.timedelta(hours=24)).isoformat()
    hydrations = get_hydrations(cutoff_24h)
    burn_runs = get_burn_runs(cutoff_24h)
    names = get_lga_names({h["lga_id"] for h in hydrations} | {r["lga_id"] for r in (burn_runs or [])})
    hydration_failed = sum(1 for h in hydrations if h["status"] != "ok")
    total_burns = sum(r["burns"] for r in (burn_runs or []))

    problems = []
    if failed_count:
        problems.append(f"{failed_count} region(s) failed")
    if hydration_failed:
        problems.append(f"{hydration_failed} hydration(s) failed")
    subject = "MakeBank ops: " + (", ".join(problems) if problems else "all clear") + " last night"
    if total_burns:
        subject += f" · {total_burns} cookie burn(s)"

    html_body = build_email_html(results, build_hydration_section(hydrations, names), build_burns_section(burn_runs, names))
    send_email(html_body, subject)


if __name__ == "__main__":
    main()

# Crontab addition (VM):
#   0 6 * * * cd /root/makebank && /root/makebank/venv/bin/python3 send_ops_summary_email.py >> /root/makebank/logs/ops_summary.log 2>&1
#   (6am — after the full midnight/3am cycle, before the 7am customer emails)
