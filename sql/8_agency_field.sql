-- Adds the agency field that was missing entirely -- the Day-1 published-
-- email variant needs it for "why it should be useful for {agency}", and
-- was falling back to the principal's own name in its place, which read
-- wrong (found testing 20 Sep 2026).

alter table free_recipients
  add column if not exists agency text;
