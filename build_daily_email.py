import urllib.parse

def build_daily_email_html(
    lga_name, lga_id, date_str, display_date,
    new_listings, new_sales, hot_leads, expiring_soon, newly_expired, new_fsbo,
    dashboard_base_url="https://app.makebank.com.au/daily-brief.html",
    logo_url="https://app.makebank.com.au/assets/makebank-logo-white.png",
    unsubscribe_token=None,
    unsubscribe_base_url="https://app.makebank.com.au/unsubscribe.html",
    show_cddready_promo=True,  # the only cross-sell in this email right now — deliberately just one, not the whole ecosystem
):
    link = (f"{dashboard_base_url}?date={date_str}"
            f"&lga={lga_id}&lgaName={urllib.parse.quote(lga_name)}")
    unsubscribe_link = (f"{unsubscribe_base_url}?token={unsubscribe_token}"
                         if unsubscribe_token else "#")

    def stat_cell(num, label, colour="#0A0A0A"):
        return f'''<td width="33%" style="padding:6px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F2F4;border-radius:10px;">
            <tr><td style="padding:16px 10px;text-align:center;">
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:26px;font-weight:700;color:{colour};line-height:1;">{num}</div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:9px;font-weight:700;color:#6B6B6B;text-transform:uppercase;letter-spacing:0.4px;margin-top:6px;">{label}</div>
            </td></tr>
          </table>
        </td>'''

    cddready_block = ""
    if show_cddready_promo:
        cddready_block = '''
  <!-- CDDREADY PROMO — the only cross-sell in this email right now -->
  <tr><td style="background:#ffffff;padding:0 26px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FFF3E6;border-radius:10px;border:1px solid #F9D9B0;">
      <tr><td style="padding:16px 18px;">
        <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#B45309;margin-bottom:4px;">Compliance that pays you</div>
        <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#3a3a3a;line-height:1.6;margin-bottom:12px;">CDDReady captures buyer ID at every open home automatically — compliant AUSTRAC verification that builds your buyer database while you work the room.</div>
        <a href="https://cddready.com.au" style="display:inline-block;background:#F68408;color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:700;padding:9px 18px;border-radius:7px;text-decoration:none;">Try CDDReady Free</a>
      </td></tr>
    </table>
  </td></tr>
'''

    html = f'''<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F1F2F4;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F2F4;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <!-- HEADER -->
  <tr><td style="background:#14243D;border-radius:14px 14px 0 0;padding:28px 32px;">
    <img src="{logo_url}" width="150" alt="makebank!" style="display:block;border:0;">
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,0.35);margin-top:18px;">MakeBank Daily Brief</div>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:700;color:#ffffff;margin-top:4px;">Good morning, {lga_name}</div>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:rgba(255,255,255,0.45);margin-top:4px;">{display_date}</div>
  </td></tr>

  <!-- STATS -->
  <tr><td style="background:#ffffff;padding:24px 26px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      {stat_cell(new_listings, "New Listings")}
      {stat_cell(new_sales, "New Sales")}
      {stat_cell(hot_leads, "Hot Leads")}
    </tr></table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;"><tr>
      {stat_cell(expiring_soon, "Expiring Soon")}
      {stat_cell(newly_expired, "Newly Expired")}
      {stat_cell(new_fsbo, "New FSBO")}
    </tr></table>
  </td></tr>

  <!-- CTA -->
  <tr><td style="background:#ffffff;padding:8px 26px 28px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="background:#F68408;border-radius:9px;">
        <a href="{link}" style="display:inline-block;padding:14px 30px;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;color:#ffffff;text-decoration:none;">View Full Dashboard &rarr;</a>
      </td>
    </tr></table>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;color:#9a9a9a;margin-top:10px;">This link shows {lga_name}&rsquo;s numbers for {display_date} &mdash; not whatever day you open it.</div>
  </td></tr>
{cddready_block}
  <!-- FOOTER -->
  <tr><td style="background:#ffffff;border-radius:0 0 14px 14px;padding:0 26px 28px;" align="center">
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#6B6B6B;">
      Powered by <a href="https://makebank.com.au" style="color:#F68408;text-decoration:none;">MakeBank</a>
      &middot; part of the <a href="https://referrer.com.au" style="color:#F68408;text-decoration:none;">referrer.com.au</a> network
      <br><a href="{unsubscribe_link}" style="color:#F68408;text-decoration:none;">Unsubscribe</a>
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>'''
    return html

if __name__ == "__main__":
    html = build_daily_email_html(
        lga_name="Newcastle", lga_id=1,
        date_str="2026-09-10", display_date="Thursday, 10 September 2026",
        new_listings=12, new_sales=6, hot_leads=4,
        expiring_soon=9, newly_expired=2, new_fsbo=1,
        unsubscribe_token="preview-token-123",
    )
    with open("daily_email_preview.html", "w") as f:
        f.write(html)
    print("written")
