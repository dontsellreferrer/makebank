-- MakeBank free-trial lifecycle, consent basis, and engagement tracking.
-- Run this in Supabase -> SQL Editor before the updated admin.html,
-- dashboard.html paywall, trial email sequence, or engagement page will work.
--
-- NOTE: this was already run directly in Supabase on 19 Sep 2026 -- this
-- file is being committed here after the fact purely so the repo's sql/
-- folder has an accurate, complete history. No need to re-run it.

-- ── Trial lifecycle + consent basis on free_recipients ──────────────────────
alter table free_recipients
  add column if not exists trial_ends_at timestamptz not null default (now() + interval '30 days'),
  add column if not exists status text not null default 'trial'
    check (status in ('trial', 'converted', 'cancelled')),
  add column if not exists consent_basis text not null default 'spoken'
    check (consent_basis in ('spoken', 'published_email')),
  add column if not exists source_url text,
  -- dedup flags so the cron sequence never double-sends a touchpoint
  add column if not exists day1_sent_at timestamptz,
  add column if not exists day25_sent_at timestamptz,
  add column if not exists feedback_sent_at timestamptz;

comment on column free_recipients.trial_ends_at is
  'Real, server-checked expiry. The Day= value shown in dashboard/email links is cosmetic only -- this column is the actual gate.';
comment on column free_recipients.source_url is
  'Required (app-level, not DB-level) when consent_basis = published_email -- kept as evidence per Spam Act Schedule 2 clause 4.';

-- Existing anon SELECT grant only exposed (id, lga_id, principal_name,
-- created_at) -- extend it with the columns the engagement page and the
-- dashboard-access check's own UI need to read via the anon key. Email,
-- phone, source_url stay OUT of this grant -- still not exposed to anon.
grant select (id, lga_id, principal_name, created_at, unsubscribed_at,
              status, trial_ends_at) on free_recipients to anon;

-- ── Engagement tracking (Resend webhook events) ──────────────────────────────
create table if not exists email_events (
  id            uuid primary key default gen_random_uuid(),
  recipient_id  uuid not null references free_recipients(id) on delete cascade,
  resend_email_id text,          -- Resend's own id for the send, for dedup/linking
  event_type    text not null check (event_type in ('delivered', 'opened', 'clicked', 'bounced', 'complained')),
  occurred_at   timestamptz not null default now()
);

create index if not exists email_events_recipient_idx on email_events (recipient_id);

alter table email_events enable row level security;

-- Written only by the webhook receiver (main.py, service-role key) -- no
-- anon insert policy. Read-only for anon, and only the columns the
-- engagement page needs -- no PII in this table at all, so a broad read
-- grant is fine.
drop policy if exists "Admin can read email events" on email_events;
create policy "Admin can read email events"
  on email_events for select
  to anon
  using (true);
grant select (id, recipient_id, event_type, occurred_at) on email_events to anon;
