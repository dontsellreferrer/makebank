-- Scheduled / bulk hydration (added 28 Sep 2026)
--
-- Lets admin_hydration.html load a batch of new territories at any time of
-- day and have them start hydrating later (e.g. 06:00 next morning) instead
-- of the moment the row is inserted.
--
--   hydrate_after          null  = hydrate immediately (unchanged behaviour —
--                                  order.html and admin.html never set it)
--                          value = don't hydrate before this time
--   hydrate_dispatched_at  null  = not yet sent to the VM
--                          value = sent to the VM's hydration queue at this
--                                  time (set atomically by whichever path
--                                  sends it first, so a region is never
--                                  dispatched twice)
--
-- Who sends a scheduled region to the VM:
--   * main.py's /webhook/new-territory relay — skips the INSERT webhook when
--     hydrate_after is still in the future; forwards it if it's already due
--   * dispatch_scheduled_hydrations.py — VM cron every 15 min, sends
--     anything now due that hasn't been dispatched yet
--   * admin_hydration.html "Hydrate now" / "Retry" button (via main.py)
--
-- Run in Supabase -> SQL Editor. Safe to re-run.

alter table lgas
  add column if not exists hydrate_after timestamptz,
  add column if not exists hydrate_dispatched_at timestamptz;

comment on column lgas.hydrate_after is
  'Scheduled hydration start. null = hydrate immediately on insert (default, unchanged). Set by admin_hydration.html for bulk batches.';
comment on column lgas.hydrate_dispatched_at is
  'When this territory was sent to the VM hydration queue. Claimed atomically (update ... where hydrate_dispatched_at is null) so it is never dispatched twice.';

-- Dispatcher looks these up every 15 minutes.
create index if not exists lgas_hydrate_due_idx
  on lgas (hydrate_after)
  where hydrate_after is not null and hydrate_dispatched_at is null;
