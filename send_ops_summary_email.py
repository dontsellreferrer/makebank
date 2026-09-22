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

from dotenv import load_dotenv
load_dotenv()

import requests

from fetch_and_build_daily_email import SUPABASE_URL, SUPABASE_KEY

log = logging.getLogger("send_ops_summary_email")
logging.basicConfig(level=logging.INFO)

RESEND_API_KEY = __import__("os").environ.get("RESEND_API_KEY", "")
EMAIL_FROM = __import__("os").environ.get("HYDRATE_EMAIL_FROM", "reports@makebank.com.au")
DASHBOARD_BASE_URL = __import__("os").environ.get("DASHBOARD_BASE_URL", "https://makebank.com.au/dashboard.html")
# Rick's own inbox — this email never goes to a customer.
OPS_REPORT_EMAIL = __import__("os").environ.get("OPS_REPORT_EMAIL", "REPLACE_WITH_OPS_EMAIL")

EXPECTED_RUN_TYPES = ['listings_phase1', 'sold_phase1', 'listings_phase2', 'sold_phase2', 'reconcile']

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
    """One query for everything, most-recent-first, then keep only the
    first (= most recent) row per (lga_id, run_type) pair — far cheaper
    than a query per region per run type."""
    rows = sb_get("runs", {
        "run_at": f"gte.{cutoff_iso}",
        "select": "lga_id,run_type,status,error_msg,run_at",
        "order": "run_at.desc",
        "limit": "3000",
    })
    latest = {}
    for r in rows:
        key = (r["lga_id"], r["run_type"])
        if key not in latest:
            latest[key] = r
    return latest


def build_region_results(lgas: list[dict], latest_runs: dict) -> list[dict]:
    results = []
    for lga in lgas:
        problems = []
        for run_type in EXPECTED_RUN_TYPES:
            run = latest_runs.get((lga["id"], run_type))
            if not run:
                problems.append(f"{run_type}: no run found in the last 26h")
            elif run["status"] != "ok":
                problems.append(f"{run_type}: {run.get('error_msg') or 'failed'}")
        results.append({"lga": lga, "ok": not problems, "problems": problems})
    return results


def dashboard_link(lga: dict) -> str:
    return f"{DASHBOARD_BASE_URL}?lga={lga['id']}&lgaName={urllib.parse.quote(lga['name'])}"


def build_email_html(results: list[dict]) -> str:
    failed = [r for r in results if not r["ok"]]
    ok = [r for r in results if r["ok"]]

    def failed_row(r):
        detail = "<br>".join(r["problems"])
        return f'''<tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-family:{FONT};font-size:13px;color:#1a1a1a;">
            <span style="color:{RED};font-weight:700;">&#10007;</span>
            <a href="{dashboard_link(r['lga'])}" style="color:{NAVY};font-weight:600;text-decoration:none;">{r['lga']['name']}</a>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-family:{FONT};font-size:11px;color:{RED};">{detail}</td>
        </tr>'''

    def ok_row(r):
        return f'''<tr>
          <td colspan="2" style="padding:6px 12px;border-bottom:1px solid #f3f3f3;font-family:{FONT};font-size:12px;color:#4a4a4a;">
            <span style="color:{GREEN};">&#10003;</span>
            <a href="{dashboard_link(r['lga'])}" style="color:#4a4a4a;text-decoration:none;">{r['lga']['name']}</a>
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

    subject = f"MakeBank ops: {failed_count} region(s) failed last night" if failed_count else "MakeBank ops: all clear last night"
    html = build_email_html(results)
    send_email(html, subject)


if __name__ == "__main__":
    main()

# Crontab addition (VM):
#   0 6 * * * cd /root/makebank && /root/makebank/venv/bin/python3 send_ops_summary_email.py >> /root/makebank/logs/ops_summary.log 2>&1
#   (6am — after the full midnight/3am cycle, before the 7am customer emails)
