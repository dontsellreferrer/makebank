-- Adds the dedup flag for the new Day-3 "region boundary" email
-- (build_trial_emails.py's build_day3_region_email_html / send_trial_emails.py's
-- run_day3). Safe to run even if 6_trial_lifecycle_schema.sql already ran.

alter table free_recipients
  add column if not exists day3_sent_at timestamptz;
