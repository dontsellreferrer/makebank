"""
Automates the checkpoint-sampling step of dating a new territory's hydration
CSV (see HANDOVER doc's "dating a new territory's hydration CSV" guide for
the manual version this replicates).

WHAT THIS DOES
  1. Reads the hydration CSV (Address, Agent, Agency, URL, First Seen).
  2. Starting after the free "Added X days ago" window (you supply this —
     only the live REA search shows those badges, not this CSV), samples one
     real listed-date checkpoint every N rows (default 25, matching REA's
     page size).
  3. For each checkpoint: gives the OpenAI API just the address and asks it
     to look up property.com.au itself and return the real listed date —
     matches how you've already been doing one-off lookups by hand.
  4. Stops early the moment a checkpoint resolves to >90 days old — no point
     sampling further once "needs precision" becomes "just needs to be old."
  5. Fills every row as a step function between checkpoints (earlier
     checkpoint's date copied down, not interpolated) and stamps everything
     past the last checkpoint with one safely-old date.
  6. Writes a new CSV with First Seen fully populated, ready for:
       python3 scraper.py --import-csv dated_file.csv --lga <id> --import-table listings

RATE LIMITING — deliberate, not a technical limitation of this script:
  you found bulk/systematic lookups get blocked (captchas/429s), one-off
  individual ones don't. This matches that on purpose — one address every
  --interval-minutes (default 3), strictly sequential. ~22 checkpoints ->
  about an hour, matching the estimate this was built around. Don't
  parallelise or shorten the interval without re-testing.

IMPORTANT CAVEAT — this uses OpenAI's web-search-capable API (Responses API
+ the web_search tool) to have the model look up each address itself, the
same way ChatGPT does when it browses. I can't verify the exact current tool
name/parameters below against OpenAI's live docs — API surfaces change and
this is outside what I have reliable knowledge of. Check
https://platform.openai.com/docs for the current tool-calling syntax before
running this for real; if the tool name/shape has changed, only
lookup_checkpoint_date() needs updating — the rate limiting, checkpoint
selection, step-fill and CSV output around it don't depend on those details.

Usage:
    export OPENAI_API_KEY=sk-...
    pip install openai --break-system-packages
    python3 checkpoint_dater.py hydration_file.csv \
        --start-row 82 --interval 25 --interval-minutes 3 \
        --out dated_file.csv
"""
import argparse
import csv
import json
import logging
import time
from datetime import datetime, timedelta

from openai import OpenAI

log = logging.getLogger("checkpoint_dater")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

client = OpenAI()  # reads OPENAI_API_KEY from env


def lookup_checkpoint_date(address: str) -> str | None:
    """Asks OpenAI to look up the address on property.com.au itself and
    return the real listed date. VERIFY the tool name/call shape against
    OpenAI's current docs — see the module docstring."""
    response = client.responses.create(
        model="gpt-4o",
        tools=[{"type": "web_search"}],  # VERIFY: current tool name per OpenAI's docs
        input=(
            f"Look up this property on property.com.au: {address}\n\n"
            f"Find the date it was first listed for sale (a listing history "
            f"entry, \"Listed on\", or similar). Respond with ONLY a JSON "
            f"object on its own, no other text: "
            f'{{"listed_date": "YYYY-MM-DD"}} or {{"listed_date": null}} '
            f"if you can't find one."
        ),
    )
    raw = (response.output_text or "").strip()
    try:
        data = json.loads(raw)
        return data.get("listed_date")
    except (json.JSONDecodeError, AttributeError):
        log.warning(f"Could not parse model response for {address}: {raw!r}")
        return None


def date_csv(in_path: str, out_path: str, start_row: int, interval: int, interval_minutes: int):
    with open(in_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    checkpoints = []  # list of (row_index, date_str)
    days90_cutoff = (datetime.now() - timedelta(days=90)).date()

    i = start_row
    while i < len(rows):
        address = rows[i]["Address"]
        log.info(f"Checkpoint row {i}: {address}")
        try:
            date_str = lookup_checkpoint_date(address)
        except Exception as e:
            log.error(f"Lookup failed for row {i} ({address}): {e}")
            date_str = None

        if date_str:
            checkpoints.append((i, date_str))
            found_date = datetime.fromisoformat(date_str).date()
            if found_date < days90_cutoff:
                log.info(f"Row {i} is already past 90 days ({date_str}) — stopping checkpoint sampling here.")
                break
        else:
            log.warning(f"No date found for row {i} — skipping this checkpoint, continuing.")

        i += interval
        if i < len(rows):
            log.info(f"Waiting {interval_minutes} min before next checkpoint (rate limiting, see module docstring)...")
            time.sleep(interval_minutes * 60)

    if not checkpoints:
        raise RuntimeError("No checkpoints resolved — nothing to date. Check lookup_checkpoint_date first.")

    # Step-fill: every row gets the most recent checkpoint's date at or before it.
    # Rows before the first checkpoint (the free "added X days ago" window) are
    # left as-is — fill those from the live REA badges by hand, same as the
    # manual method.
    tail_date = checkpoints[-1][1]
    checkpoint_idx = 0
    for row_i in range(start_row, len(rows)):
        while (checkpoint_idx + 1 < len(checkpoints)) and (row_i >= checkpoints[checkpoint_idx + 1][0]):
            checkpoint_idx += 1
        current_date = checkpoints[checkpoint_idx][1] if row_i >= checkpoints[0][0] else None
        if row_i > checkpoints[-1][0]:
            current_date = tail_date  # past the last checkpoint — stamp with one safe old date
        if current_date:
            rows[row_i]["First Seen"] = current_date

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Address", "Agent", "Agency", "URL", "First Seen"])
        writer.writeheader()
        writer.writerows(rows)

    dated = sum(1 for r in rows if r.get("First Seen"))
    log.info(f"Wrote {out_path} — {dated}/{len(rows)} rows dated ({len(rows) - dated} in the free window, fill those by hand)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--start-row", type=int, default=0,
                    help="First row needing a real lookup — i.e. how many rows the free 'Added X days ago' badges already covered on the live REA search.")
    ap.add_argument("--interval", type=int, default=25, help="Rows between checkpoints (25 = one REA results page).")
    ap.add_argument("--interval-minutes", type=int, default=3, help="Minutes to wait between lookups — do not shorten without re-testing against the live site.")
    ap.add_argument("--out", default="dated_file.csv")
    args = ap.parse_args()
    date_csv(args.csv_path, args.out, args.start_row, args.interval, args.interval_minutes)


if __name__ == "__main__":
    main()
