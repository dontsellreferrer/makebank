-- 14_runs_burns.sql (2 Oct 2026)
-- Adds runs.burns: number of HTTP 429 responses (cookie burns during URL
-- collection + 429s on detail pages) during that run. Read by the 06:00
-- ops summary email. Safe to re-run.
--
-- Run this in the Supabase SQL Editor BEFORE pulling the matching code to
-- the VM. (log_run() falls back to logging without it if it's missing, so
-- nothing breaks either way -- burns just won't be recorded until it's run.)

alter table runs add column if not exists burns integer not null default 0;
