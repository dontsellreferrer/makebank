"""
Drives the free-trial email lifecycle for free_recipients rows. Run once
daily (alongside the existing send_daily_emails.py --all-free-tier-lgas cron
line — see the crontab note at the bottom of this file).

Four things happen each run, in order:
  1. Day-1 "trial started" — sent once, right after a recipient is added
     (whichever of the two consent-basis variants applies)
  2. Day-25 warning — sent once, when trial_ends_at is within 5 days
  3. Expiry cutover — flips status 'trial' -> 'cancelled' once trial_ends_at
     has passed. This status flip IS what /api/dashboard-access in main.py
     checks — it's the actual access cutoff, not just an email trigger.
  4. Week-later feedback ask — sent once, 7 days after a recipient's
     trial_ends_at, only if they're still 'cancelled' (skipped if they
     converted in the meantime)

Every send tags the Resend request with the recipient's id so
/webhook/resend-events (main.py) can log opens/clicks back against them for
the engagement page.

Usage:
    python3 send_trial_emails.py
"""
import argparse
import datetime as dt
import logging

from dotenv import load_dotenv
load_dotenv()

import requests

from fetch_and_build_daily_email import SUPABASE_URL, SUPABASE_KEY
from build_trial_emails import (
    build_day1_spoken_email_html, build_day1_published_email_html,
    build_day3_region_email_html, build_day25_warning_email_html, build_feedback_email_html,
)

log = logging.getLogger("send_trial_emails")
logging.basicConfig(level=logging.INFO)

RESEND_API_KEY = __import__("os").environ.get("RESEND_API_KEY", "")
EMAIL_FROM = __import__("os").environ.get("HYDRATE_EMAIL_FROM", "reports@makebank.com.au")
DASHBOARD_BASE_URL = __import__("os").environ.get("DASHBOARD_BASE_URL", "https://makebank.com.au/dashboard.html")
ORDER_BASE_URL = __import__("os").environ.get("ORDER_BASE_URL", "https://makebank.com.au/order.html")
UNSUBSCRIBE_BASE_URL = __import__("os").environ.get("UNSUBSCRIBE_BASE_URL", "https://makebank.com.au/unsubscribe.html")
# Fill these in before this goes live — used in the Day-1 footer for Spam Act compliance.
BUSINESS_NAME = __import__("os").environ.get("MAKEBANK_BUSINESS_NAME", "REPLACE_WITH_BUSINESS_NAME")
BUSINESS_ADDRESS = __import__("os").environ.get("MAKEBANK_BUSINESS_ADDRESS", "REPLACE_WITH_BUSINESS_ADDRESS")
BUSINESS_ABN = __import__("os").environ.get("MAKEBANK_ABN", "REPLACE_WITH_ABN")


def sb_get(table: str, params: dict) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=params,
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    r.raise_for_status()
    return r.json()


def sb_patch(table: str, row_id: str, fields: dict):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params={"id": f"eq.{row_id}"},
        headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal",
        },
        json=fields,
    )
    r.raise_for_status()


_lga_cache: dict[int, dict] = {}
def get_lga(lga_id: int) -> dict:
    if lga_id not in _lga_cache:
        rows = sb_get("lgas", {"id": f"eq.{lga_id}", "select": "id,name,search_url_listings"})
        if not rows:
            raise ValueError(f"No LGA found with id {lga_id}")
        _lga_cache[lga_id] = rows[0]
    return _lga_cache[lga_id]


def send_one(to_email: str, html: str, subject: str, recipient_id: str):
    if not RESEND_API_KEY:
        log.error("RESEND_API_KEY not set — logging instead of sending.")
        log.info(f"Would send to {to_email}: {subject}")
        return
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": EMAIL_FROM, "to": [to_email], "subject": subject, "html": html,
            # Tag every send with the recipient's id so the Resend webhook can
            # attribute opens/clicks back to a specific free_recipients row.
            "tags": [{"name": "recipient_id", "value": recipient_id}],
        },
    )
    if resp.status_code >= 300:
        log.error(f"Send failed for {to_email}: {resp.status_code} {resp.text}")
    else:
        log.info(f"Sent to {to_email}: {subject}")


def _send_day1(r: dict, now: dt.datetime):
    """Sends the Day-1 email for one free_recipients row and marks it sent.
    Shared by send_day1_immediate() (called right after admin.html inserts a
    new recipient — see main.py's /api/send-day1-email) and run_day1() (the
    manual/backfill path via --only day1, for any row that somehow missed
    the immediate send — NOT part of the default scheduled run, per Rick:
    Day-1 fires on signup, not on the 11am cron)."""
    lga = get_lga(r["lga_id"])
    unsubscribe_link = f"{UNSUBSCRIBE_BASE_URL}?token={r['unsubscribe_token']}"
    if r["consent_basis"] == "spoken":
        html = build_day1_spoken_email_html(
            r["principal_name"], lga["name"], "Rick", DASHBOARD_BASE_URL, r["lga_id"], r["id"],
            unsubscribe_link, BUSINESS_NAME, BUSINESS_ADDRESS, BUSINESS_ABN,
        )
        subject = f"Rick added you to MakeBank — free {lga['name']} data"
    else:
        html = build_day1_published_email_html(
            r["principal_name"], r["principal_name"], lga["name"], r["source_url"] or "",
            DASHBOARD_BASE_URL, r["lga_id"], r["id"],
            unsubscribe_link, BUSINESS_NAME, BUSINESS_ADDRESS, BUSINESS_ABN,
        )
        subject = f"MakeBank is now in {lga['name']} — free daily data"
    send_one(r["email"], html, subject, r["id"])
    sb_patch("free_recipients", r["id"], {"day1_sent_at": now.isoformat()})


def send_day1_immediate(recipient_id: str):
    """Called from main.py right after admin.html inserts a new free
    recipient — fires the Day-1 email within the same request, not on the
    next cron run. Re-checks day1_sent_at itself (idempotent) so a retried
    call, or this recipient also turning up in a --only day1 backfill later,
    can never double-send."""
    rows = sb_get("free_recipients", {
        "id": f"eq.{recipient_id}", "day1_sent_at": "is.null",
        "select": "id,lga_id,principal_name,email,phone,consent_basis,source_url,unsubscribe_token",
    })
    if not rows:
        log.info(f"send_day1_immediate: {recipient_id} not found or already sent — skipping")
        return
    _send_day1(rows[0], dt.datetime.now(dt.timezone.utc))


def run_day1(now: dt.datetime):
    """Backfill/safety-net path only — NOT in the default scheduled run.
    Use `python3 send_trial_emails.py --only day1` to catch any recipient
    whose immediate send (send_day1_immediate, above) never fired."""
    rows = sb_get("free_recipients", {
        "status": "eq.trial", "day1_sent_at": "is.null", "unsubscribed_at": "is.null",
        "select": "id,lga_id,principal_name,email,phone,consent_basis,source_url,unsubscribe_token",
    })
    for r in rows:
        _send_day1(r, now)


def run_day3(now: dt.datetime):
    """3 days after signup — the self-serve region-boundary explainer, sent
    once per recipient (day3_sent_at). Uses created_at, not trial_ends_at,
    since trial_ends_at is a fixed +30 days from signup and doesn't shift if
    a trial's dates ever get adjusted manually."""
    due_by = (now - dt.timedelta(days=3)).isoformat()
    rows = sb_get("free_recipients", {
        "status": "eq.trial", "day3_sent_at": "is.null", "unsubscribed_at": "is.null",
        "created_at": f"lte.{due_by}",
        "select": "id,lga_id,principal_name,email,phone,unsubscribe_token",
    })
    for r in rows:
        lga = get_lga(r["lga_id"])
        html = build_day3_region_email_html(
            r["principal_name"], lga["name"], DASHBOARD_BASE_URL, r["lga_id"], r["id"],
            ORDER_BASE_URL, r["email"], r["phone"] or "", lga["search_url_listings"],
            f"{UNSUBSCRIBE_BASE_URL}?token={r['unsubscribe_token']}",
        )
        send_one(r["email"], html, f"One thing worth knowing about {lga['name']}", r["id"])
        sb_patch("free_recipients", r["id"], {"day3_sent_at": now.isoformat()})


def run_day25(now: dt.datetime):
    warn_by = (now + dt.timedelta(days=5)).isoformat()
    rows = sb_get("free_recipients", {
        "status": "eq.trial", "day25_sent_at": "is.null", "unsubscribed_at": "is.null",
        "trial_ends_at": f"lte.{warn_by}",
        "select": "id,lga_id,principal_name,email,phone,trial_ends_at,unsubscribe_token",
    })
    for r in rows:
        lga = get_lga(r["lga_id"])
        trial_ends = dt.datetime.fromisoformat(r["trial_ends_at"].replace("Z", "+00:00"))
        html = build_day25_warning_email_html(
            r["principal_name"], lga["name"], trial_ends.strftime("%-d %B %Y"),
            DASHBOARD_BASE_URL, r["lga_id"], r["id"],
            ORDER_BASE_URL, r["email"], r["phone"] or "", lga["search_url_listings"],
            f"{UNSUBSCRIBE_BASE_URL}?token={r['unsubscribe_token']}",
        )
        send_one(r["email"], html, f"5 days left on your {lga['name']} trial", r["id"])
        sb_patch("free_recipients", r["id"], {"day25_sent_at": now.isoformat()})


def run_expiry_cutover(now: dt.datetime):
    """The actual access cutoff — flips status once trial_ends_at has
    passed. /api/dashboard-access in main.py checks this status, not just
    the raw date, so this flip is what locks a bookmarked dashboard link."""
    rows = sb_get("free_recipients", {
        "status": "eq.trial", "trial_ends_at": f"lte.{now.isoformat()}",
        "select": "id",
    })
    for r in rows:
        sb_patch("free_recipients", r["id"], {"status": "cancelled"})
        log.info(f"Recipient {r['id']} trial expired — status set to cancelled")


def run_feedback(now: dt.datetime):
    feedback_due_by = (now - dt.timedelta(days=7)).isoformat()
    rows = sb_get("free_recipients", {
        "status": "eq.cancelled", "feedback_sent_at": "is.null", "unsubscribed_at": "is.null",
        "trial_ends_at": f"lte.{feedback_due_by}",
        "select": "id,lga_id,principal_name,email,phone,unsubscribe_token",
    })
    for r in rows:
        lga = get_lga(r["lga_id"])
        html = build_feedback_email_html(
            r["principal_name"], lga["name"], DASHBOARD_BASE_URL, r["lga_id"], r["id"],
            ORDER_BASE_URL, r["email"], r["phone"] or "", lga["search_url_listings"],
            f"{UNSUBSCRIBE_BASE_URL}?token={r['unsubscribe_token']}", EMAIL_FROM,
        )
        send_one(r["email"], html, f"Before you go — {lga['name']}", r["id"])
        sb_patch("free_recipients", r["id"], {"feedback_sent_at": now.isoformat()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["day1", "day3", "day25", "expiry", "feedback"],
                     help="Run just one stage — useful for testing a single piece in isolation")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    stages = {"day1": run_day1, "day3": run_day3, "day25": run_day25, "expiry": run_expiry_cutover, "feedback": run_feedback}
    # Day-1 is deliberately NOT in the default order — it fires immediately
    # on signup via send_day1_immediate() (called from main.py's
    # /api/send-day1-email, triggered right after admin.html's insert), not
    # on this scheduled run. `--only day1` still exists as a manual backfill
    # path for any recipient whose immediate send failed.
    # Expiry runs before feedback in the normal order so a recipient who
    # expires today isn't accidentally skipped by the feedback query's
    # `status=cancelled` filter until tomorrow's run.
    order = ["day3", "day25", "expiry", "feedback"]

    if args.only:
        stages[args.only](now)
    else:
        for stage in order:
            stages[stage](now)


if __name__ == "__main__":
    main()

# Crontab addition (VM, alongside the existing lines — see HANDOVER doc):
#   0 11 * * * ... send_trial_emails.py   (11am — deliberately clear of the
#                                           existing 7am send_daily_emails.py
#                                           line. Day-1 is not part of this
#                                           run — see send_day1_immediate().)
