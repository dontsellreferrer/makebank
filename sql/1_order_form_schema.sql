-- MakeBank order-form schema
-- Run this in Supabase → SQL Editor before the order form goes live.

-- 1. New table: one row per order/signup, independent of payment status
create table if not exists orders (
  id                          uuid primary key default gen_random_uuid(),
  created_at                  timestamptz not null default now(),
  agent_name                  text not null,
  agency                      text not null,
  email                       text not null,
  phone                       text,
  recipients                  jsonb not null default '[]'::jsonb,  -- [{"name":"...","email":"..."}]
  region_url                  text not null,                        -- the realestate.com.au URL as submitted
  lga_id                      integer references lgas(id),
  status                      text not null default 'pending_payment',  -- pending_payment | paid | cancelled
  stripe_client_reference_id  text
);

-- 2. RLS: allow the public order form (using the anon key) to INSERT into
--    both tables, without opening up read/update/delete access.
alter table orders enable row level security;

drop policy if exists "Public can create orders" on orders;
create policy "Public can create orders"
  on orders for insert
  to anon
  with check (true);

-- lgas already has broad anon SELECT (used by every dashboard) — this adds
-- INSERT specifically for new-territory creation from the order form.
drop policy if exists "Public can create territories" on lgas;
create policy "Public can create territories"
  on lgas for insert
  to anon
  with check (true);

-- 3. Once you wire up the Stripe webhook (see hydrate_new_territory.py),
--    it should flip orders.status to 'paid' after successful payment —
--    that update needs the service key, not anon, so no anon UPDATE policy
--    is added here deliberately.

-- Needed for the "this territory is still young" banner on the Weekly
-- Report — lets it know how old the territory is without a separate query.
-- Safe to run even if lgas already has this column.
alter table lgas add column if not exists created_at timestamptz not null default now();
