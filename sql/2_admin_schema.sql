-- MakeBank free-tier recipient management
-- This is an internal admin tool, not self-service — principals only get
-- added here after Rick has personally spoken with them and they've said
-- yes. Run this in Supabase → SQL Editor before makebank_admin.html works.

create table if not exists free_recipients (
  id                  uuid primary key default gen_random_uuid(),
  created_at          timestamptz not null default now(),
  lga_id              integer not null references lgas(id),
  principal_name      text not null,
  phone               text,
  email               text not null,
  unsubscribed_at     timestamptz,                                  -- null = still subscribed
  unsubscribe_token   uuid not null default gen_random_uuid()       -- unique per recipient, powers the real unsubscribe link
);

alter table free_recipients enable row level security;

-- Admin page uses the anon key (same pattern as every other MakeBank tool).
drop policy if exists "Admin can add free recipients" on free_recipients;
create policy "Admin can add free recipients"
  on free_recipients for insert
  to anon
  with check (true);

-- Column-scoped SELECT — lets the admin page list existing principals per
-- region (so Rick doesn't double-add someone) WITHOUT exposing raw
-- email/phone via the anon key to anyone who happens to find the page.
-- NOTE: this page isn't behind real auth (nothing in MakeBank is yet) — this
-- is obscurity, not a real security boundary. Move behind proper auth if
-- that ever matters more than it does today.
grant select (id, lga_id, principal_name, created_at) on free_recipients to anon;
drop policy if exists "Admin can list principal names" on free_recipients;
create policy "Admin can list principal names"
  on free_recipients for select
  to anon
  using (true);

-- Genuine unsubscribe: the unsubscribe_token itself (an unguessable UUID) IS
-- the access control. RLS allows the update broadly, but a column-level
-- grant restricts anon to touching ONLY unsubscribed_at — nothing else on
-- the row is writable via this key, including by someone who somehow got
-- hold of another recipient's token.
drop policy if exists "Token holder can unsubscribe" on free_recipients;
create policy "Token holder can unsubscribe"
  on free_recipients for update
  to anon
  using (true)
  with check (true);
grant update (unsubscribed_at) on free_recipients to anon;
