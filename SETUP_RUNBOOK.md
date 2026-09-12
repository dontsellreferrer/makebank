# MakeBank — Full Setup Runbook

Everything from moving the domain through to the 4 Coffs/Tweed/Wollongong/
Lower North Shore territories actually running, on Railway instead of a
laptop that can't stay awake for 2 hours.

Do these roughly in order — later steps depend on earlier ones being done.

---

## 0. What you need before starting

Accounts: Cloudflare, Railway, Resend, Stripe, GitHub. Supabase is already
set up. OpenAI only needed if/when you use `checkpoint_dater.py`.

Already in hand: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (used today).

---

## 1. Run the SQL migrations (Supabase → SQL Editor)

In order:

1. `sql/1_order_form_schema.sql` — creates `orders`, adds `lgas.created_at`
2. `sql/2_admin_schema.sql` — creates `free_recipients`, the column-scoped
   grants for the admin tool and genuine unsubscribes
3. `sql/3_cookie_slots_schema.sql` — the 5-slot cookie coordination
   (**already run today** — skip if `select * from cookie_slots` already
   returns 5 rows)
4. `sql/5_archive_existing_regions.sql` — pauses the 24 original regions
   (**already run today** if you did this earlier — check the SELECT at
   the bottom of that file)

`sql/4_new_regions_template.sql` isn't a migration — that's the template
you already used to create territories 27–30 today. Nothing more to do
with it unless you're adding more territories later.

---

## 2. Get the domain onto Cloudflare

1. Cloudflare → Add a site → makebank.com.au
2. It scans existing DNS and gives you two nameservers
3. Go to wherever the domain is currently registered, replace its
   nameservers with those two
4. Wait for propagation (minutes to ~24h)

Registration itself can stay wherever it is — this just makes Cloudflare
the DNS host, which is what lets it point at Railway and verify Resend
later.

---

## 3. Push this to GitHub

```bash
git init
git add .
git commit -m "MakeBank — initial deploy"
git remote add origin <your-new-repo-url>
git push -u origin main
```

The folder structure in this zip is already what Railway expects:

```
main.py
scraper.py
hydrate_new_territory.py
send_daily_emails.py
fetch_and_build_daily_email.py
build_daily_email.py
checkpoint_dater.py
requirements.txt
nixpacks.toml         (adapted from the existing weekly-scraper service)
public/
  index.html            (the website)
  order.html
  daily-brief.html
  weekly-report.html
  export.html
  admin.html             (internal — not linked from anywhere public)
  unsubscribe.html
```

---

## 4. Deploy to Railway

1. Railway → New Project → Deploy from GitHub repo → pick this repo
2. **`nixpacks.toml` is included** — adapted directly from the existing
   weekly-scraper service's proven working config (same `[phases.setup]`
   Nix system packages and `[phases.install]` Playwright commands,
   unchanged — that part already works in production, no reason to guess
   at it again). The only thing changed is `[start]`: the original runs
   `bash cron.sh` (a one-shot scraper script), this one runs
   `uvicorn main:app --host 0.0.0.0 --port $PORT` instead, since MakeBank's
   `main.py` is a persistent web server, not a cron job.
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Environment variables (Settings → Variables):
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - `RESEND_API_KEY` (from step 6 below)
   - `HYDRATE_WEBHOOK_SECRET` — make up any random string, you'll reuse it
     in step 7
   - `HYDRATE_EMAIL_FROM` — `reports@makebank.com.au` (once verified in
     step 6)
   - `OPENAI_API_KEY` — only if running `checkpoint_dater.py` from here too

---

## 5. Point the domain at Railway

1. Railway → this service → Settings → Networking → Custom Domain →
   `makebank.com.au`
2. Railway gives you a CNAME target
3. Cloudflare → DNS → add a CNAME record for the root pointing at that
   target (Cloudflare flattens CNAMEs at the apex automatically — this
   works even though a plain CNAME can't normally sit at the root)
4. Turn the proxy on (orange cloud) for free SSL

---

## 6. Verify the domain in Resend

1. Resend → Domains → Add `makebank.com.au` (a fresh domain — not reusing
   referrer.com.au's, since these emails should genuinely come from
   MakeBank)
2. Add the DNS records Resend gives you, in Cloudflare
3. Wait for "Verified"
4. Resend → API Keys → create one → add it as `RESEND_API_KEY` in Railway
   (step 4)

---

## 7. Wire up the Supabase Database Webhook

Now that hydration can fire automatically for territories created *from
now on* (not 27–30, which already exist — see step 9 for those):

1. Supabase → Database → Webhooks → New
2. Table: `lgas`, Event: Insert, Type: HTTP Request
3. URL: `https://makebank.com.au/webhook/new-territory`
4. Header: `Authorization: Bearer <the HYDRATE_WEBHOOK_SECRET from step 4>`

---

## 8. Create the real Stripe Payment Link

1. Stripe Dashboard → Payment Links → New → $39.00 AUD, Recurring, Monthly
2. Copy the resulting `buy.stripe.com` URL
3. In `public/order.html`, replace the placeholder:
   ```html
   <a href="https://buy.stripe.com/REPLACE_WITH_YOUR_PAYMENT_LINK" ...
   ```
4. Commit and push — Railway redeploys automatically on push

---

## 9. Run territories 27–30 (this time, somewhere that doesn't sleep)

Railway CLI → SSH straight into the running service:

```bash
railway ssh
```

Then, inside that shell (this is now running on Railway's infrastructure,
not your laptop):

```bash
python hydrate_new_territory.py --lga-id 27
python hydrate_new_territory.py --lga-id 28
python hydrate_new_territory.py --lga-id 29
python hydrate_new_territory.py --lga-id 30
```

Same commands as today, same ~2–2.5h each — the only thing that's changed
is where they're running. Since `RESEND_API_KEY` will be set in Railway by
now, these will actually email you the CSVs this time instead of saving
locally. Feel free to open 4 separate `railway ssh` sessions to run all 4
concurrently, same as the 4 PowerShell windows today — the cookie slot
system handles the coordination either way.

---

## 10. Update the remaining placeholders

A few things still reference placeholder URLs from before the domain was
live — worth a pass once everything above is done:

- `build_daily_email.py` — `dashboard_base_url` / `logo_url` defaults
- `fetch_and_build_daily_email.py` / `send_daily_emails.py` — same, via
  env vars (`DASHBOARD_BASE_URL`, `LOGO_URL`, `UNSUBSCRIBE_BASE_URL`) —
  set these in Railway rather than editing code
- `hydrate_new_territory.py` — `HYDRATE_EMAIL_FROM` (set in step 4)

---

## 11. Verify

- `https://makebank.com.au` loads the site
- `https://makebank.com.au/order.html`, `/daily-brief.html`,
  `/weekly-report.html`, `/admin.html`, `/unsubscribe.html` all load
- `https://makebank.com.au/health` returns `{"status":"ok"}`
- Territories 27–30 each have their listings CSV emailed and sold data
  hydrated in Supabase
- `select * from cookie_slots` shows all 5 slots free (`claimed_by is
  null`) once nothing's running

---

## Still not built (known gaps, not part of this batch)

- Daily cron itself — nothing triggers `scraper.py` automatically every
  day yet, this whole runbook only covers hosting + the one-off
  4-territory run
- Stripe webhook to reconcile `orders.status` on successful/abandoned
  payment — right now a paid signup's territory goes active before
  payment confirms, and nothing cleans up an abandoned checkout
- `weekly_report.py` (the original audit-email script) hasn't been
  rebuilt to match the Mon–Sun reconciliation logic defined earlier
- Real concurrent-claim testing of the cookie slot system against live
  Postgres under actual load (today's 4-territory run got partway there
  before being killed for the laptop-sleep issue — worth finishing that
  test once this is on Railway)
