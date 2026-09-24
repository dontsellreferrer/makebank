-- One-time backfill, found 24 Sep 2026: adding client_ready (10_client_ready_flag.sql)
-- defaulted it to false for every EXISTING row, not just future ones -- so
-- every already-live, already-verified region (dated=true from the old
-- process, long before this column existed) silently got client_ready=false
-- too. send_daily_emails.py's new failure-guard correctly requires
-- client_ready=true, so every single live region's daily emails were being
-- held back -- a real outage this caused, confirmed by every region showing
-- in "Skipping inactive LGA(s)" on the next run.
--
-- Anything already dated=true was already verified under the old process --
-- retroactively mark it client_ready=true too. Anything still dated=false
-- (e.g. Geelong, Townsville, not yet CSV-dated) correctly stays
-- client_ready=false -- that's working as intended, not part of this bug.

update lgas set client_ready = true where dated = true and client_ready = false;
