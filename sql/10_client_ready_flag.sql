-- Separates "cron-eligible" from "safe to show a client" -- these got
-- conflated by the 23 Sep 2026 change that made hydration set dated=true
-- immediately (so cron tracks a territory from day one, not just once
-- dating is done). That's correct for cron, but it also meant a brand-new,
-- still-today-dated territory would be immediately eligible for daily
-- emails and dashboard viewing, before Rick's ever actually verified the
-- data -- which he was never going to accept sending to a real client.

alter table lgas
  add column if not exists client_ready boolean not null default false;

comment on column lgas.client_ready is
  'true only once the dated CSV has actually been imported (see import_csv() in scraper.py) -- this, not dated, is what daily/trial emails and any client-facing surface should gate on. dated alone just means cron is tracking it.';
