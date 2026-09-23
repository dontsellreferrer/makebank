-- Without this, send_ops_summary_email.py and send_daily_emails.py's new
-- failure-guard (both use the anon key, imported from
-- fetch_and_build_daily_email.py) silently get zero rows back from every
-- query against `runs`, regardless of what's actually there -- scraper.py
-- writes to `runs` using the service-role key, which bypasses RLS entirely,
-- so this gap was invisible until the anon-key readers actually needed to
-- read it back. Found 24 Sep 2026 the hard way: a correctly-successful
-- night got reported as "7 regions failed", and worse -- the brand-new
-- daily-email failure guard would have silently held back every customer
-- email, every night, forever, having been built and deployed the same
-- night this gap was never caught.
--
-- `runs` has no PII in it at all (timestamps, counts, status, error
-- messages) -- same reasoning as email_events' anon grant -- so a broad
-- read grant is fine.

alter table runs enable row level security;

drop policy if exists "Anon can read runs" on runs;
create policy "Anon can read runs"
  on runs for select
  to anon
  using (true);

grant select on runs to anon;
