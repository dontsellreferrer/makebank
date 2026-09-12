-- Archives the 24 originally-established regions (paused, not deleted) while
-- today's bugs (first_seen overwrite on restoration, partial-crawl false
-- positives, sold-reconciliation gap) get reviewed against their historical
-- data. active=false is the existing toggle the cron already respects —
-- nothing in this table gets deleted, and it's fully reversible later.
--
-- IDs below are the 24 from the HANDOVER doc's "Active LGAs" table as of
-- 9 Sep 2026 — deliberately an explicit list, not a blanket
-- "archive everything", so any territory created since (test regions,
-- Coffs Harbour via the admin tool, etc.) is left untouched.

update lgas
set active = false
where id in (1,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26);

-- Sanity check — should return exactly 24 rows, all active = false:
select id, name, active from lgas
where id in (1,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26)
order by id;
