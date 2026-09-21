"""
HTML builders for MakeBank's free-trial email sequence. Mirrors
build_daily_email.py's visual style (navy header, orange CTA, Arial,
table-based layout for email-client compatibility) so these read as the same
product, not a separate system.

Four templates:
  - build_day1_spoken_email_html      -- trial start, consent_basis='spoken'
  - build_day1_published_email_html   -- trial start, consent_basis='published_email'
  - build_day25_warning_email_html    -- ~5 days before trial_ends_at
  - build_feedback_email_html         -- 1 week after cancellation

Every dashboard link includes both `recipient` (the real, server-checked
gate -- see /api/dashboard-access in main.py) and a cosmetic `Day=` value
that ONLY affects copy/display, never access.
"""
import urllib.parse

LOGO_URL = "https://makebank.com.au/assets/makebank-logo-white.png"
ORANGE = "#F68408"
NAVY = "#14243D"
FONT = "Arial,Helvetica,sans-serif"


def _wrap(header_kicker, header_title, header_sub, body_html, footer_html):
    return f'''<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F1F2F4;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F2F4;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <tr><td style="background:{NAVY};border-radius:14px 14px 0 0;padding:28px 32px;">
    <img src="{LOGO_URL}" width="150" alt="makebank!" style="display:block;border:0;">
    <div style="font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,0.35);margin-top:18px;">{header_kicker}</div>
    <div style="font-family:{FONT};font-size:20px;font-weight:700;color:#ffffff;margin-top:4px;">{header_title}</div>
    {f'<div style="font-family:{FONT};font-size:12px;color:rgba(255,255,255,0.45);margin-top:4px;">{header_sub}</div>' if header_sub else ''}
  </td></tr>

  <tr><td style="background:#ffffff;padding:28px 32px;">
    {body_html}
  </td></tr>

  <tr><td style="background:#ffffff;border-radius:0 0 14px 14px;padding:0 32px 28px;">
    {footer_html}
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>'''


def _cta(url, label):
    return f'''<table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0;"><tr>
      <td style="background:{ORANGE};border-radius:9px;">
        <a href="{url}" style="display:inline-block;padding:14px 30px;font-family:{FONT};font-size:14px;font-weight:700;color:#ffffff;text-decoration:none;">{label}</a>
      </td>
    </tr></table>'''


def _standard_footer(unsubscribe_link, extra_legal_html=""):
    return f'''<div style="font-family:{FONT};font-size:11px;color:#6B6B6B;line-height:1.7;">
      Powered by <a href="https://makebank.com.au" style="color:{ORANGE};text-decoration:none;">MakeBank</a>
      &middot; part of the <a href="https://referrer.com.au" style="color:{ORANGE};text-decoration:none;">referrer.com.au</a> network
      <br><a href="{unsubscribe_link}" style="color:{ORANGE};text-decoration:none;">Unsubscribe</a>
    </div>
    {extra_legal_html}'''


def _dashboard_link(dashboard_base_url, lga_id, lga_name, recipient_id, day):
    # `Day=` is cosmetic only -- see the big comment in dashboard.html next to
    # CONFIG.recipientId. The real gate is `recipient`, checked server-side.
    return (f"{dashboard_base_url}?lga={lga_id}&lgaName={urllib.parse.quote(lga_name)}"
            f"&recipient={recipient_id}&Day={day}")


def _renew_link(order_base_url, name, email, phone, region_url, recipient_id):
    # recipient_id is what lets order.html flip this free_recipients row to
    # 'converted' once they submit -- without it, nothing connects an early
    # conversion back to the trial that spawned it, and the day-25/expiry/
    # feedback stages would keep firing against a customer who already paid.
    params = urllib.parse.urlencode({
        "name": name, "email": email, "phone": phone, "region_url": region_url,
        "recipient_id": recipient_id,
    })
    return f"{order_base_url}?{params}"


# ── Day 1: trial started ─────────────────────────────────────────────────────

def _day1_common(name, lga_name, dashboard_link, unsubscribe_link, intro_html, business_name, business_address, abn, extra_legal_html=""):
    body = f'''
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;">Hi {name},</div>
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;margin-top:12px;">{intro_html}</div>
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;margin-top:14px;">
        Each morning you'll get new listings, sales, expiring agreements, and withdrawn-but-unsold properties for
        <strong>{lga_name}</strong> — plus a live dashboard you can check any time.
      </div>
      {_cta(dashboard_link, "View Full Dashboard &rarr;")}
      <div style="font-family:{FONT};font-size:13px;color:#1a1a1a;line-height:1.7;margin-top:8px;">
        <strong>Worth sharing:</strong> forward the dashboard link to your team — the leaderboards are useful for
        your management, and the hot lists (expiring/withdrawn listings) are built for whoever's actually prospecting.
      </div>
      <div style="font-family:{FONT};font-size:13px;color:#1a1a1a;line-height:1.7;margin-top:14px;">
        <strong>Not useful to you?</strong> Genuinely — <a href="{unsubscribe_link}" style="color:{ORANGE};">unsubscribe</a>.
        We'd rather you opt out than have this sit unread in your inbox.
      </div>'''
    legal = f'''<div style="font-family:{FONT};font-size:10px;color:#9a9a9a;line-height:1.6;margin-top:18px;padding-top:14px;border-top:1px solid #eee;">
      {extra_legal_html}
      Sender: {business_name}, {business_address}, ABN {abn}. You can withdraw consent at any time via the unsubscribe link above.
    </div>'''
    return body, legal


def build_day1_spoken_email_html(name, lga_name, sender_name, dashboard_base_url, lga_id, recipient_id,
                                  unsubscribe_link, business_name, business_address, abn):
    dashboard_link = _dashboard_link(dashboard_base_url, lga_id, lga_name, recipient_id, day=1)
    intro = (f"As discussed — you're now getting MakeBank's free daily property intelligence for "
              f"<strong>{lga_name}</strong>. This isn't spam and there's no card on file: it's genuinely "
              f"free for 30 days, starting today.")
    body, legal = _day1_common(name, lga_name, dashboard_link, unsubscribe_link, intro,
                                business_name, business_address, abn,
                                extra_legal_html="You were added to this list directly by " + sender_name + " at MakeBank. ")
    return _wrap("MakeBank Trial Started", f"{sender_name} added you to MakeBank", "", body,
                 _standard_footer(unsubscribe_link, legal))


def build_day1_published_email_html(name, agency, lga_name, source_url, dashboard_base_url, lga_id, recipient_id,
                                     unsubscribe_link, business_name, business_address, abn):
    dashboard_link = _dashboard_link(dashboard_base_url, lga_id, lga_name, recipient_id, day=1)
    intro = (f"MakeBank is now available in <strong>{lga_name}</strong> and I wanted to share exactly what this "
              f"means and why it should be genuinely useful for <strong>{agency}</strong> — daily property "
              f"intelligence for {lga_name}: new listings, sales, expiring agreements, and withdrawn-but-unsold "
              f"properties. Emailed to you every day, plus a live dashboard. No card, no catch, free for 30 days.")
    extra_legal = (f'This email is sent under the inferred-consent provision for conspicuously published business '
                    f'contacts (Spam Act 2003, Schedule 2, clause 4). Your address is published at '
                    f'<a href="{source_url}" style="color:#9a9a9a;">{source_url}</a> in a context relevant to real '
                    f'estate work, with no stated refusal of unsolicited commercial messages. ')
    body, legal = _day1_common(name, lga_name, dashboard_link, unsubscribe_link, intro,
                                business_name, business_address, abn, extra_legal_html=extra_legal)
    return _wrap("MakeBank", f"MakeBank is now in {lga_name} — free daily data for {agency}", "", body,
                 _standard_footer(unsubscribe_link, legal))


# ── Day 3: region-boundary explainer ─────────────────────────────────────────
# The self-serve answer to "I listed/sold more than that" before they ever
# have to ask — explains the boundary concept early, and offers creating
# their own region as the fix. Deliberately no "contact us" anywhere: the
# whole point is this needs zero of Rick's time to resolve.

def build_day3_region_email_html(name, lga_name, dashboard_base_url, lga_id, recipient_id,
                                  order_base_url, email, phone, region_url, unsubscribe_link):
    dashboard_link = _dashboard_link(dashboard_base_url, lga_id, lga_name, recipient_id, day=3)
    create_region_link = _renew_link(order_base_url, name, email, phone, region_url, recipient_id)
    body = f'''
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;">Hi {name},</div>
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;margin-top:12px;">
        A few days in, one thing worth knowing: MakeBank only counts listings inside <strong>{lga_name}</strong>'s
        defined search area — not just nearby, the exact boundary set up for this region. If a number ever looks
        lower than you'd expect, that's almost always why — a listing outside the boundary simply won't show, no
        matter how close it is.
      </div>
      {_cta(dashboard_link, "View Full Dashboard &rarr;")}
      <div style="font-family:{FONT};font-size:13px;color:#1a1a1a;line-height:1.7;margin-top:8px;">
        If most of your work actually falls outside {lga_name}, or spans a wider patch, you can set up your own
        region covering exactly where you work — takes about a minute.
      </div>
      <div style="margin-top:12px;">
        <a href="{create_region_link}" style="display:inline-block;padding:12px 20px;font-family:{FONT};font-size:13px;font-weight:600;color:{NAVY};background:#F1F2F4;border-radius:7px;text-decoration:none;">Create your own region &rarr;</a>
      </div>'''
    return _wrap("MakeBank", f"One thing worth knowing about {lga_name}", "", body, _standard_footer(unsubscribe_link))


# ── Day 25: trial ending warning ─────────────────────────────────────────────

def build_day25_warning_email_html(name, lga_name, trial_ends_display, dashboard_base_url, lga_id, recipient_id,
                                    order_base_url, email, phone, region_url, unsubscribe_link):
    dashboard_link = _dashboard_link(dashboard_base_url, lga_id, lga_name, recipient_id, day=25)
    renew_link = _renew_link(order_base_url, name, email, phone, region_url, recipient_id)
    body = f'''
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;">Hi {name},</div>
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;margin-top:12px;">
        Your free trial for <strong>{lga_name}</strong> ends <strong>{trial_ends_display}</strong>. After that your
        dashboard link stops working — the data doesn't disappear, it's just locked until you continue.
      </div>
      {_cta(renew_link, "Continue for $55/month &rarr;")}
      <div style="font-family:{FONT};font-size:13px;color:#1a1a1a;line-height:1.7;">
        Nothing to do if you'd rather let it lapse — no card's on file, nothing gets charged automatically.
      </div>
      <div style="font-family:{FONT};font-size:11px;color:#9a9a9a;margin-top:16px;">
        <a href="{dashboard_link}" style="color:{ORANGE};">View your dashboard</a> while it's still open.
      </div>'''
    return _wrap("MakeBank", f"5 days left on your {lga_name} trial", "", body, _standard_footer(unsubscribe_link))


# ── Week-later feedback ask (post-cancellation) ──────────────────────────────

def build_feedback_email_html(name, lga_name, dashboard_base_url, lga_id, recipient_id,
                               order_base_url, email, phone, region_url, unsubscribe_link, reply_to_hint):
    dashboard_link = _dashboard_link(dashboard_base_url, lga_id, lga_name, recipient_id, day=37)
    renew_link = _renew_link(order_base_url, name, email, phone, region_url, recipient_id)
    body = f'''
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;">Hi {name},</div>
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;margin-top:12px;">
        Your {lga_name} trial ended a week ago and we haven't heard from you — totally fine, MakeBank isn't for
        everyone.
      </div>
      <div style="font-family:{FONT};font-size:14px;color:#1a1a1a;line-height:1.7;margin-top:12px;">
        If you've got 30 seconds, we'd genuinely like to know: what would've made the dashboard more useful or
        relevant to how you actually work? Wrong area, wrong data, wrong format — anything.
        {f'Just reply to this email ({reply_to_hint}).' if reply_to_hint else 'Just reply to this email.'}
      </div>
      <div style="font-family:{FONT};font-size:13px;color:#1a1a1a;line-height:1.7;margin-top:18px;">Changed your mind instead?</div>
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:8px;"><tr>
        <td style="padding-right:10px;">
          <a href="{dashboard_link}" style="display:inline-block;padding:12px 20px;font-family:{FONT};font-size:13px;font-weight:600;color:{NAVY};background:#F1F2F4;border-radius:7px;text-decoration:none;">See what you're missing &rarr;</a>
        </td>
        <td>
          <a href="{renew_link}" style="display:inline-block;padding:12px 20px;font-family:{FONT};font-size:13px;font-weight:700;color:#ffffff;background:{ORANGE};border-radius:7px;text-decoration:none;">Continue for $55/month &rarr;</a>
        </td>
      </tr></table>'''
    return _wrap("MakeBank", f"Before you go — {lga_name}", "", body, _standard_footer(unsubscribe_link))
