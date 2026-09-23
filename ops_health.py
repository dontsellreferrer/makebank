"""
Shared "did this region's nightly cycle actually succeed" check, against the
`runs` table. Originally lived only in send_ops_summary_email.py; pulled out
here 23 Sep 2026 so send_daily_emails.py can use the exact same logic to
hold back a region's customer-facing email when its scrape failed, instead
of sending a wall of zeros that reads as "the product is broken" rather
than "nothing ran last night" (found the hard way — a dead cookie pool took
out phase1/phase2/reconcile for all 7 live regions in one night).

Two files independently re-implementing "what counts as a failed night"
is exactly the kind of duplication that's caused real bugs this session
(the sold_date column, the runs table's column name) — one definition,
both scripts import it.
"""
import requests

EXPECTED_RUN_TYPES = ['listings_phase1', 'sold_phase1', 'listings_phase2', 'sold_phase2', 'reconcile']


def get_latest_runs(supabase_url: str, supabase_key: str, cutoff_iso: str) -> dict:
    """One query for everything in the window, most-recent-first, then keep
    only the first (= most recent) row per (lga_id, run_type) pair."""
    r = requests.get(
        f"{supabase_url}/rest/v1/runs",
        params={
            "run_at": f"gte.{cutoff_iso}",
            "select": "lga_id,run_type,status,error_msg,run_at",
            "order": "run_at.desc",
            "limit": "3000",
        },
        headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
    )
    r.raise_for_status()
    latest = {}
    for row in r.json():
        key = (row["lga_id"], row["run_type"])
        if key not in latest:
            latest[key] = row
    return latest


def lga_problems(lga_id: int, latest_runs: dict) -> list[str]:
    """Empty list = clean night. Otherwise, one string per expected run
    type that's either missing entirely or logged with status != 'ok'."""
    problems = []
    for run_type in EXPECTED_RUN_TYPES:
        run = latest_runs.get((lga_id, run_type))
        if not run:
            problems.append(f"{run_type}: no run found in the last 26h")
        elif run["status"] != "ok":
            problems.append(f"{run_type}: {run.get('error_msg') or 'failed'}")
    return problems
