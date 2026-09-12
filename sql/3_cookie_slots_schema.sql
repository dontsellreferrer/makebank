-- Cookie slot coordination — makes "5 wide" concurrency actually safe.
--
-- Instead of every concurrent scrape sharing one round-robin cookie pool
-- (the race condition found 10 Sep 2026), the 30 cookies are split into 5
-- fixed slots of 6 — matching how they're already harvested (5 runs of 6
-- browser windows). Each concurrent scrape claims one whole slot for
-- itself, uses only those 6 cookies, and releases the slot when done.
-- Two different code paths need to share this same coordination:
--   1. The daily cron's own --parallel N (multiple LGAs in one process)
--   2. Webhook-triggered new-territory hydrations (unpredictable timing,
--      could overlap with the cron or with each other)
-- Run this in Supabase → SQL Editor.

create table if not exists cookie_slots (
  slot_index  int primary key,
  claimed_by  text,
  claimed_at  timestamptz
);

insert into cookie_slots (slot_index) values (0),(1),(2),(3),(4)
on conflict (slot_index) do nothing;

-- Atomic claim — FOR UPDATE SKIP LOCKED is the standard Postgres pattern for
-- a safe work-queue claim across concurrent connections/processes, without
-- needing any lock at the application level. Returns the claimed slot index,
-- or null if all 5 are currently in use (caller should wait and retry).
create or replace function claim_cookie_slot(runner text)
returns int
language plpgsql
as $$
declare
  result int;
begin
  select slot_index into result
  from cookie_slots
  where claimed_by is null
  order by slot_index
  limit 1
  for update skip locked;

  if result is not null then
    update cookie_slots
    set claimed_by = runner, claimed_at = now()
    where slot_index = result;
  end if;

  return result;
end;
$$;

create or replace function release_cookie_slot(idx int)
returns void
language sql
as $$
  update cookie_slots set claimed_by = null, claimed_at = null where slot_index = idx;
$$;

-- Safety net: a run that crashes without releasing its slot shouldn't leave
-- that slot permanently stuck. Anything claimed for more than 30 minutes
-- (far longer than a real scrape run) is considered abandoned and freed.
-- Call this at the start of claim attempts, or on a schedule.
create or replace function release_stale_cookie_slots()
returns void
language sql
as $$
  update cookie_slots
  set claimed_by = null, claimed_at = null
  where claimed_at < now() - interval '30 minutes';
$$;
