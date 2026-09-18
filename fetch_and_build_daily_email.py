"""
Fetches today's real counts from Supabase and generates a hydrated Daily Email.

Mirrors the exact queries makebank's dashboard.html uses, so the numbers in
the email match what the dashboard shows for the same day.

Usage:
    pip install requests --break-system-packages
    python3 fetch_and_build_daily_email.py --lga 1 --lga-name Newcastle

Run this from a machine that can reach Supabase (this sandbox's network is
restricted and can't reach supabase.co directly, so it can't be run in-chat).
"""
import argparse
import datetime as dt
import requests

from build_daily_email import build_daily_email_html

SUPABASE_URL = "https://axmzkjqywpbhxrvshbkf.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF4bXpranF5d3BiaHhydnNoYmtmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgzMTI1OTMsImV4cCI6MjA5Mzg4ODU5M30.k2DRavwWm6gtg7qPdEAoNa5uYPhdguyW7ILy6fZ0V9w"

FSBO_KEYWORDS = ['property now', 'buymyplace', 'for sale by owner', 'no agent property', 'sold by owner', 'private sale']


def is_fsbo(agency):
    agency = (agency or '').lower()
    return any(k in agency for k in FSBO_KEYWORDS)


def _parse_params(params: str):
    # A dict can't hold two entries with the same key — and PostgREST range
    # queries legitimately need that (e.g. two 'first_seen' bounds, one
    # gte one lte). Using a dict here silently dropped one of the two
    # bounds on any such query, turning a narrow date window into an
    # unbounded one (found 18 Sep 2026 — Expiring Soon reported 445 when
    # the dashboard's own correct count was 64). requests accepts a list
    # of tuples and sends every one of them, dict or not.
    return [tuple(p.split('=', 1)) for p in params.split('&')]


def sb_count(table, params):
    """Returns the exact row count for a filtered query, mirroring the dashboard's sb() helper."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=_parse_params(params),
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer": "count=exact",
            "Range": "0-0",
        },
    )
    r.raise_for_status()
    content_range = r.headers.get("content-range", "")
    return int(content_range.split("/")[-1]) if "/" in content_range else 0


def sb_rows(table, params):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=_parse_params(params),
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    r.raise_for_status()
    return r.json()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lga", type=int, required=True)
    ap.add_argument("--lga-name", required=True)
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today", default=None)
    ap.add_argument("--out", default="daily_email_live.html")
    args = ap.parse_args()

    ref = dt.datetime.strptime(args.date, "%Y-%m-%d") if args.date else dt.datetime.utcnow()
    yesterday = (ref - dt.timedelta(days=1)).isoformat()
    days90 = (ref - dt.timedelta(days=90)).date().isoformat()
    days75 = (ref - dt.timedelta(days=75)).date().isoformat()
    # "Newly Expired" on the dashboard is a narrow ~24h band — listings that
    # crossed 90 days old specifically today, not the whole historical
    # backlog of everything past 90 days (that's what "Expired (All)"
    # would mean, a different metric the dashboard shows in non-daily
    # windows). Matches dashboard.html's days90Start/days90End exactly.
    days90_start = (ref - dt.timedelta(days=91)).isoformat()
    days90_end = (ref - dt.timedelta(days=90)).isoformat()

    lga = args.lga

    # New listings — fetch rows (not just count) so we can split out FSBO, same as the dashboard
    new_listing_rows = sb_rows(
        "listings",
        f"lga_id=eq.{lga}&status=eq.active&first_seen=gte.{yesterday}&select=agency"
    )
    fsbo_count = sum(1 for r in new_listing_rows if is_fsbo(r.get("agency")))
    new_listings_count = len(new_listing_rows) - fsbo_count

    new_sales_count = sb_count("sold", f"lga_id=eq.{lga}&sold_date=gte.{(ref - dt.timedelta(days=1)).date().isoformat()}&select=id")
    hot_leads_count = sb_count("listings", f"lga_id=eq.{lga}&status=eq.removed_not_sold&removed_at=gte.{yesterday}&select=id")
    newly_expired_count = sb_count("listings", f"lga_id=eq.{lga}&status=eq.active&first_seen=gte.{days90_start}&first_seen=lt.{days90_end}&select=id")
    expiring_soon_count = sb_count("listings", f"lga_id=eq.{lga}&status=eq.active&first_seen=gte.{days90}&first_seen=lte.{days75}&select=id")

    display_date = ref.strftime("%A, %-d %B %Y")
    date_str = ref.strftime("%Y-%m-%d")

    html = build_daily_email_html(
        lga_name=args.lga_name, lga_id=lga,
        date_str=date_str, display_date=display_date,
        new_listings=new_listings_count, new_sales=new_sales_count,
        hot_leads=hot_leads_count, expiring_soon=expiring_soon_count,
        newly_expired=newly_expired_count, new_fsbo=fsbo_count,
    )
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out}")
    print(f"  New Listings: {new_listings_count}")
    print(f"  New Sales:    {new_sales_count}")
    print(f"  Hot Leads:    {hot_leads_count}")
    print(f"  Expiring Soon:{expiring_soon_count}")
    print(f"  Newly Expired:{newly_expired_count}")
    print(f"  New FSBO:     {fsbo_count}")


if __name__ == "__main__":
    main()
