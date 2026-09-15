"""
Sends the MakeBank Daily Brief email to every free-tier recipient of a given
territory. Reuses fetch_and_build_daily_email.py's count logic and
build_daily_email.py's template — this is the actual delivery mechanism for
the free path (principals added via makebank_admin.html), separate from
paid subscribers viewing the dashboard directly.

One email per recipient, each with THEIR OWN unique unsubscribe link — never
a single email sent to a list, so the unsubscribe token stays meaningful.

Usage:
    pip install requests --break-system-packages
    python3 send_daily_emails.py --lga 20
    python3 send_daily_emails.py --all-free-tier-lgas   # run for every LGA that has at least one free recipient
"""
import argparse
import logging

import requests

from fetch_and_build_daily_email import (
    SUPABASE_URL, SUPABASE_KEY, sb_count, sb_rows, is_fsbo, main as _unused,
)
from build_daily_email import build_daily_email_html
import datetime as dt

log = logging.getLogger("send_daily_emails")
logging.basicConfig(level=logging.INFO)

RESEND_API_KEY = __import__("os").environ.get("RESEND_API_KEY", "")
EMAIL_FROM = __import__("os").environ.get("HYDRATE_EMAIL_FROM", "reports@makebank.com.au")
DASHBOARD_BASE_URL = __import__("os").environ.get("DASHBOARD_BASE_URL", "https://makebank.com.au/dashboard.html")
LOGO_URL = __import__("os").environ.get("LOGO_URL", "https://makebank.com.au/assets/makebank-logo-white.png")
UNSUBSCRIBE_BASE_URL = __import__("os").environ.get("UNSUBSCRIBE_BASE_URL", "https://makebank.com.au/unsubscribe.html")


def get_active_recipients(lga_id: int) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/free_recipients",
        params={"lga_id": f"eq.{lga_id}", "unsubscribed_at": "is.null", "select": "principal_name,email,unsubscribe_token"},
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    r.raise_for_status()
    return r.json()


def get_lga(lga_id: int) -> dict:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/lgas",
        params={"id": f"eq.{lga_id}", "select": "id,name"},
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(f"No LGA found with id {lga_id}")
    return rows[0]


def compute_counts(lga_id: int, ref: dt.datetime) -> dict:
    """Mirrors makebank_daily_brief.html's own queries exactly."""
    yesterday = (ref - dt.timedelta(days=1)).isoformat()
    days90 = (ref - dt.timedelta(days=90)).date().isoformat()
    days75 = (ref - dt.timedelta(days=75)).date().isoformat()

    new_listing_rows = sb_rows("listings", f"lga_id=eq.{lga_id}&status=eq.active&first_seen=gte.{yesterday}&select=agency")
    fsbo_count = sum(1 for r in new_listing_rows if is_fsbo(r.get("agency")))
    new_listings_count = len(new_listing_rows) - fsbo_count

    return {
        "new_listings": new_listings_count,
        "new_sales": sb_count("sold", f"lga_id=eq.{lga_id}&first_seen=gte.{yesterday}&select=id"),
        "hot_leads": sb_count("listings", f"lga_id=eq.{lga_id}&status=eq.removed_not_sold&removed_at=gte.{yesterday}&select=id"),
        "newly_expired": sb_count("listings", f"lga_id=eq.{lga_id}&status=eq.active&first_seen=lte.{days90}&select=id"),
        "expiring_soon": sb_count("listings", f"lga_id=eq.{lga_id}&status=eq.active&first_seen=gte.{days90}&first_seen=lte.{days75}&select=id"),
        "new_fsbo": fsbo_count,
    }


def send_one(to_email: str, html: str, subject: str):
    if not RESEND_API_KEY:
        log.error("RESEND_API_KEY not set — logging instead of sending.")
        log.info(f"Would send to {to_email}: {subject}")
        return
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": EMAIL_FROM, "to": [to_email], "subject": subject, "html": html},
    )
    if resp.status_code >= 300:
        log.error(f"Send failed for {to_email}: {resp.status_code} {resp.text}")
    else:
        log.info(f"Sent to {to_email}")


def send_for_lga(lga_id: int):
    lga = get_lga(lga_id)
    recipients = get_active_recipients(lga_id)
    if not recipients:
        log.info(f"No active free recipients for {lga['name']} (id={lga_id}) — nothing to send.")
        return

    ref = dt.datetime.utcnow()
    counts = compute_counts(lga_id, ref)
    date_str = ref.strftime("%Y-%m-%d")
    display_date = ref.strftime("%A, %-d %B %Y")

    log.info(f"Sending to {len(recipients)} recipient(s) for {lga['name']}")
    for r in recipients:
        html = build_daily_email_html(
            lga_name=lga["name"], lga_id=lga_id,
            date_str=date_str, display_date=display_date,
            dashboard_base_url=DASHBOARD_BASE_URL, logo_url=LOGO_URL,
            unsubscribe_token=r["unsubscribe_token"], unsubscribe_base_url=UNSUBSCRIBE_BASE_URL,
            show_cddready_promo=True,
            **counts,
        )
        send_one(r["email"], html, f"MakeBank Daily Brief — {lga['name']} — {display_date}")


def get_all_free_tier_lga_ids() -> list[int]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/free_recipients",
        params={"unsubscribed_at": "is.null", "select": "lga_id"},
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    r.raise_for_status()
    return sorted({row["lga_id"] for row in r.json()})


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--lga", type=int, help="Send for one LGA id")
    group.add_argument("--all-free-tier-lgas", action="store_true", help="Send for every LGA with at least one active free recipient")
    args = ap.parse_args()

    if args.lga:
        send_for_lga(args.lga)
    else:
        for lga_id in get_all_free_tier_lga_ids():
            send_for_lga(lga_id)


if __name__ == "__main__":
    main()
