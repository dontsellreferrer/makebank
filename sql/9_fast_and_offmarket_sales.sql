-- Adds what's needed to report "fast sales" (sold within 7 days of listing)
-- and "off-market sales" (sold, never appeared as a public listing) on the
-- dashboard. listings previously had no sold_date at all (confirmed 23 Sep
-- 2026 the hard way — a wrong assumption about this broke reconcile for
-- every region) and no way to distinguish an off-market backfilled row from
-- a normally-tracked one.

alter table listings
  add column if not exists sold_date date,
  add column if not exists off_market boolean not null default false;

comment on column listings.off_market is
  'true only for rows backfilled by reconcile''s off-market detection (see scraper.py) -- sold with no listings row ever having existed for it.';
