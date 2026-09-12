#!/usr/bin/env python3
"""
REA Scraper — single pass, Supabase-backed
Phase 1: requests-based URL collection from search results (fast)
Phase 2: Playwright detail scrape for each new URL (gets address, agent, agency)

Usage:
    python scraper.py                              # all active LGAs
    python scraper.py --lga 1                      # specific LGA
    python scraper.py --lga 1 --max-pages 2        # test run
    python scraper.py --lga 1 --type listings      # listings only
    python scraper.py --lga 1 --type sold          # sold only
    python scraper.py --lga 1 --reconcile-only     # reconcile only
    python scraper.py --import-csv Listing.csv --lga 1
"""

import os, re, sys, csv, json, time, random, argparse, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('scraper.log', encoding='utf-8'),
    ]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
COOKIES_FILE = os.getenv('COOKIES_FILE', 'cookies.json')
DELAY_MIN    = float(os.getenv('DELAY_MIN', '1.0'))
DELAY_MAX    = float(os.getenv('DELAY_MAX', '3.0'))
MAX_PAGES    = int(os.getenv('MAX_PAGES')) if os.getenv('MAX_PAGES') else None


# ── Supabase ──────────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(url, key)


# ── Cookie slot coordination ──────────────────────────────────────────────────
# See cookie_slots_schema.sql. Both the cron's own --parallel dispatch and
# webhook-triggered hydrations claim slots through this same mechanism, so
# they can safely coexist even if they happen to run at the same moment —
# neither one has to assume it's the only thing using cookies right now.
import time as _time  # local alias — module-level `time` may already be imported elsewhere


def claim_cookie_slot(sb: Client, runner: str, max_wait_seconds: int = 300, poll_seconds: int = 5) -> int:
    """Blocks until a slot is free (or max_wait_seconds elapses), returns the
    claimed slot index. Raises if none became available in time."""
    sb.rpc('release_stale_cookie_slots').execute()  # free anything abandoned by a crashed run first
    waited = 0
    while waited <= max_wait_seconds:
        result = sb.rpc('claim_cookie_slot', {'runner': runner}).execute()
        slot = result.data
        if slot is not None:
            log.info(f"{runner}: claimed cookie slot {slot}")
            return slot
        log.info(f"{runner}: all cookie slots busy, waiting {poll_seconds}s...")
        _time.sleep(poll_seconds)
        waited += poll_seconds
    raise RuntimeError(f"{runner}: no cookie slot became free within {max_wait_seconds}s")


def release_cookie_slot(sb: Client, slot: int, runner: str):
    sb.rpc('release_cookie_slot', {'idx': slot}).execute()
    log.info(f"{runner}: released cookie slot {slot}")


# ── Cookie pool ───────────────────────────────────────────────────────────────
class CookiePool:
    def __init__(self, path: str, slot_index: int = None, slots_total: int = 5):
        """
        slot_index/slots_total: if given, this pool only ever uses the cookies
        belonging to that one slot (a fixed, non-overlapping chunk of the full
        set) — not the whole pool. This is what makes concurrent scrapes safe:
        each concurrent run gets its own slot via claim_cookie_slot() (see
        cookie_slots_schema.sql) and only touches its own 6 cookies, so two
        concurrent scrapes can never collide on the same cookie the way a
        single shared round-robin pool could.
        Omit slot_index for the normal single-run case — unchanged behaviour,
        uses the whole pool.
        """
        self.cookies: list[str] = []
        self.agents:  list[str] = []
        self._index = 0

        data = self._load_from_supabase() or self._load_from_file(path)
        if not data:
            raise RuntimeError("No cookies available. Run cookies.py to refresh the pool.")

        if slot_index is not None:
            chunk = len(data) // slots_total
            start = slot_index * chunk
            end = start + chunk if slot_index < slots_total - 1 else len(data)  # last slot absorbs any remainder
            data = data[start:end]
            if not data:
                raise RuntimeError(f"Slot {slot_index} of {slots_total} has no cookies — pool too small to partition this way.")
            log.info(f"Cookie pool: using slot {slot_index}/{slots_total} ({len(data)} cookies)")

        self.cookies = [d['cookie']     for d in data if 'cookie'     in d]
        self.agents  = [d['user_agent'] for d in data if 'user_agent' in d]
        log.info(f"Loaded {len(self.cookies)} cookie(s)")

    def _load_from_supabase(self) -> list:
        url = os.getenv('SUPABASE_URL')
        key = os.getenv('SUPABASE_SERVICE_KEY')
        if not url or not key:
            return []
        try:
            sb = create_client(url, key)
            result = sb.table('cookies').select('cookie, user_agent').eq('is_active', True).execute()
            if result.data:
                log.info(f"Cookie pool: loaded {len(result.data)} slots from Supabase")
                return result.data
        except Exception as e:
            log.warning(f"Cookie pool: Supabase load failed ({e}) — falling back to local file")
        return []

    def _load_from_file(self, path: str) -> list:
        if not os.path.exists(path):
            log.warning(f"Cookie pool: no local file at {path}")
            return []
        with open(path) as f:
            data = json.load(f)
        log.info(f"Cookie pool: loaded {len(data)} slots from {path}")
        return data

    def next(self) -> dict:
        h = {'Cookie': self.cookies[self._index], 'User-Agent': self.agents[self._index]}
        self._index = (self._index + 1) % len(self.cookies)
        return h

    def current(self) -> dict:
        idx = self._index % max(len(self.cookies), 1)
        return {'Cookie': self.cookies[idx], 'User-Agent': self.agents[idx]}

    def burn(self, index: int):
        if 0 <= index < len(self.cookies):
            log.warning(f"Burning cookie #{index} (429)")
            self.cookies.pop(index)
            self.agents.pop(index)
            if self._index >= len(self.cookies) and self.cookies:
                self._index = 0

    @property
    def empty(self) -> bool:
        return len(self.cookies) == 0

    def reload(self):
        data = self._load_from_supabase()
        if data:
            self.cookies = [d['cookie']     for d in data if 'cookie'     in d]
            self.agents  = [d['user_agent'] for d in data if 'user_agent' in d]
            self._index  = 0
            log.info(f"Cookie pool reloaded: {len(self.cookies)} slots")


# ── Phase 1: URL collection via Playwright ───────────────────────────────────────────
class URLCollector:
    def __init__(self, pool: CookiePool, max_pages=None):
        self.pool      = pool
        self.max_pages = max_pages
        self._browser  = None
        self._pw       = None

    def _start_browser(self):
        from playwright.sync_api import sync_playwright
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )

    def _stop_browser(self):
        if self._browser:
            try: self._browser.close()
            except: pass
        if self._pw:
            try: self._pw.stop()
            except: pass

    def _parse_cookies(self, cookie_str):
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append({
                    "name": name.strip(), "value": value.strip(),
                    "domain": ".realestate.com.au", "path": "/",
                })
        return cookies

    def _get_soup(self, url, retries=3):
        for attempt in range(retries):
            if self.pool.empty:
                return None
            idx        = random.randrange(len(self.pool.cookies))
            cookie_str = self.pool.cookies[idx]
            user_agent = self.pool.agents[idx]
            ctx        = self._browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1366, "height": 768},
                locale="en-AU",
                timezone_id="Australia/Sydney",
            )
            ctx.add_cookies(self._parse_cookies(cookie_str))
            page = ctx.new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response and response.status == 429:
                    log.warning(f"Burning cookie #{idx} (429)")
                    self.pool.burn(idx)
                    page.close(); ctx.close()
                    time.sleep(random.uniform(3, 8))
                    continue
                if response and response.status == 200:
                    content = page.content()
                    page.close(); ctx.close()
                    return BeautifulSoup(content, "html.parser")
                page.close(); ctx.close()
                time.sleep(random.uniform(2, 5))
            except Exception as e:
                log.warning(f"Playwright error on {url}: {e}")
                try: page.close(); ctx.close()
                except: pass
                time.sleep(random.uniform(2, 5))
        return None

    def get_total_pages(self, base_url):
        soup = self._get_soup(base_url)
        if not soup:
            return 0
        try:
            return int(soup.select("nav[aria-label='Pagination Navigation'] a")[-2].text)
        except Exception:
            pass
        return 1

    def _page_has_old_sold(self, soup, cutoff_days: int = 30) -> bool:
        """Check if any listing on the page has a sold date older than cutoff_days.
        Parses the ArgonautExchange JSON blob embedded in the page script tag — reliable,
        structured, not affected by ads or other page content."""
        import json as _json
        cutoff = datetime.now() - timedelta(days=cutoff_days)

        # Find the script tag containing ArgonautExchange
        script = soup.find('script', string=lambda t: t and 'ArgonautExchange' in t)
        if not script:
            log.warning("  ArgonautExchange script not found — falling back to span check")
            # Fallback: article span check
            for article in soup.select('article'):
                for span in article.select('span'):
                    text = span.get_text(strip=True)
                    if not text.startswith('Sold on'):
                        continue
                    date_str = text.replace('Sold on', '').strip()
                    for fmt in ('%d %B %Y', '%d %b %Y'):
                        try:
                            d = datetime.strptime(date_str, fmt)
                            if d < cutoff:
                                log.info(f"  Found sold date {d.date()} older than {cutoff_days} days — stopping")
                                return True
                        except ValueError:
                            continue
            return False

        # Extract sold dates directly from raw script string
        dates_found = re.findall(
            r'dateSold.{0,80}?(\d{1,2} [A-Za-z]+ \d{4})',
            script.string
        )

        for date_str in dates_found:
            for fmt in ('%d %B %Y', '%d %b %Y'):
                try:
                    d = datetime.strptime(date_str, fmt).date()
                    if d < cutoff.date():
                        log.info(f"  Found sold date {d} older than {cutoff_days} days — stopping")
                        return True
                except ValueError:
                    continue
        return False

    def collect_urls(self, base_url: str, sold_cutoff_days: int = 0, known_urls: set = None) -> list[str]:
        if 'list-1' not in base_url:
            raise ValueError("base_url must contain 'list-1'")
        if known_urls is None:
            known_urls = set()
        self._start_browser()
        try:
            total = self.get_total_pages(base_url)
            if self.max_pages:
                total = min(total, self.max_pages)
            log.info(f"Pages to collect: up to {total}")
            url_template = base_url.replace('list-1', 'list-{}')
            all_live_urls = []
            new_urls      = []
            for page in range(1, total + 1):
                page_url = url_template.format(page)
                log.info(f"  Page {page}/{total}")
                soup = self._get_soup(page_url)
                if not soup:
                    continue
                found = ['https://www.realestate.com.au' + a.get('href')
                         for a in soup.select('h2.residential-card__address-heading > a')]
                page_new = [u for u in found if u not in known_urls]
                log.info(f"  Found {len(found)} URLs ({len(page_new)} new)")
                all_live_urls.extend(found)
                new_urls.extend(page_new)
                if sold_cutoff_days > 0 and self._page_has_old_sold(soup, sold_cutoff_days):
                    log.info(f"  Reached {sold_cutoff_days}-day cutoff — stopping")
                    break
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            log.info(f"Total URLs collected: {len(all_live_urls)} ({len(new_urls)} new)")
        finally:
            self._stop_browser()
        # Store all live URLs on self so run_scrape can compute removals
        self._last_all_live_urls = set(all_live_urls)
        return new_urls


# ── Phase 2: Detail scrape via Playwright ─────────────────────────────────────
ROTATE_EVERY = 1  # New browser context every page — full cookie rotation

def scrape_details_playwright(urls: list[str], pool: CookiePool) -> list[dict]:
    """Visit each URL with Playwright, extract address/agent/agency.
    Rotates cookie and browser context every ROTATE_EVERY pages."""
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        def parse_cookie_str(cookie_str):
            cookies = []
            for part in cookie_str.split(';'):
                part = part.strip()
                if '=' in part:
                    name, _, value = part.partition('=')
                    cookies.append({
                        'name': name.strip(), 'value': value.strip(),
                        'domain': '.realestate.com.au', 'path': '/',
                    })
            return cookies

        def new_context():
            # Random cookie selection — new context per listing
            idx        = random.randrange(len(pool.cookies))
            cookie_str = pool.cookies[idx]
            user_agent = pool.agents[idx]
            ctx = browser.new_context(
                user_agent=user_agent,
                viewport={'width': 1366, 'height': 768},
                locale='en-AU',
                timezone_id='Australia/Sydney',
            )
            ctx.add_cookies(parse_cookie_str(cookie_str))
            return ctx, ctx.new_page()

        for i, url in enumerate(urls, 1):
            context, page = new_context()

            log.info(f"  [{i}/{len(urls)}] {url}")
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                try:
                    page.wait_for_selector(
                        'a[href*="/agent/"], a[href*="/agency/"], a[href*="/home-builders/"], [class*="NonLink"]',
                        timeout=8000)
                except Exception:
                    pass
                html = page.content()
                detail = parse_detail(html, url)
                if detail:
                    results.append(detail)
                    log.info(f"    → {detail['agent']} / {detail['agency']}")
            except Exception as e:
                log.warning(f"  Error on {url}: {e}")
            finally:
                context.close()
            time.sleep(random.uniform(3, 8))

        browser.close()

    return results


def parse_detail(html: str, url: str) -> Optional[dict]:
    """Parse address/agent/agency from rendered HTML."""
    soup = BeautifulSoup(html, 'html.parser')

    # Address
    try:
        address = soup.select_one("h1.property-info-address").text.strip()
    except AttributeError:
        return None

    # Agent — try link first, then NonLink div
    agent = ''
    try:
        tag = soup.select_one('a[href*="/agent/"]')
        if tag:
            agent = tag.text.strip()
            if not agent:
                m = re.search(r'/agent/([^?/]+)', tag.get('href', ''))
                if m:
                    slug = re.sub(r'-\d+$', '', m.group(1))
                    agent = ' '.join(p.capitalize() for p in slug.split('-'))
    except Exception:
        pass
    if not agent:
        try:
            tag = soup.select_one('[class*="AgentOrConsultantNameNonLink"], [class*="agentNameNonLink"]')
            if tag and tag.text.strip():
                agent = tag.text.strip()
        except Exception:
            pass

    # Agency — try link, home-builders, NonLink
    agency = ''
    for selector in ['a[href*="/agency/"]', 'a[href*="/home-builders/"]']:
        try:
            tag = soup.select_one(selector)
            if tag:
                agency = tag.text.strip()
                if not agency:
                    m = re.search(r'/(agency|home-builders)/([^?/]+)', tag.get('href', ''))
                    if m:
                        slug = m.group(2)
                        parts = slug.split('-')
                        if parts and re.match(r'^[A-Z0-9]{4,}$', parts[-1]):
                            parts = parts[:-1]
                        agency = ' '.join(p.capitalize() for p in parts)
                if agency:
                    break
        except Exception:
            pass

    if not agency:
        try:
            tag = soup.select_one('[class*="NonLink"]')
            if tag and tag.text.strip():
                agency = tag.text.strip().rstrip(' -').strip()
        except Exception:
            pass

    # Sold date — from property-info__middle-content
    sold_date = None
    try:
        for container in soup.select('div.property-info__middle-content'):
            for span in container.select('span'):
                text = span.get_text(strip=True)
                if text.startswith('Sold on'):
                    date_str = text.replace('Sold on', '').strip()
                    for fmt in ('%d %B %Y', '%d %b %Y'):
                        try:
                            sold_date = datetime.strptime(date_str, fmt).date().isoformat()
                            break
                        except ValueError:
                            continue
                if sold_date:
                    break
    except Exception:
        pass

    return {'address': address, 'agent': agent, 'agency': agency, 'url': url, 'sold_date': sold_date}


# ── Supabase store ────────────────────────────────────────────────────────────
class LGAStore:
    def __init__(self, sb: Client, lga_id: int):
        self.sb     = sb
        self.lga_id = lga_id

    def get_active_urls(self, table: str) -> set[str]:
        urls = set()
        page_size = 1000
        offset = 0
        while True:
            resp = (self.sb.table(table)
                    .select('url')
                    .eq('lga_id', self.lga_id)
                    .eq('status', 'active')
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = [r['url'] for r in resp.data]
            urls.update(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return urls

    def get_all_urls(self, table: str) -> set[str]:
        urls = set()
        page_size = 1000
        offset = 0
        while True:
            resp = (self.sb.table(table)
                    .select('url')
                    .eq('lga_id', self.lga_id)
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = [r['url'] for r in resp.data]
            urls.update(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return urls

    def insert_new(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        records = [{
            'lga_id':     self.lga_id,
            'address':    r['address'],
            'agent':      r['agent'],
            'agency':     r['agency'],
            'url':        r['url'],
            'status':     'active',
            'first_seen': r.get('first_seen') or now,  # CSV import can supply a real historical date; normal scrapes always omit it and get now
            'last_seen':  now,
            **({'sold_date': r['sold_date']} if r.get('sold_date') else {}),
        } for r in rows]
        inserted = 0
        for i in range(0, len(records), 500):
            chunk = records[i:i+500]
            self.sb.table(table).upsert(chunk, on_conflict='url,lga_id').execute()
            inserted += len(chunk)
        return inserted

    def mark_removed(self, table: str, urls: set[str]) -> int:
        if not urls:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        url_list = list(urls)
        updated = 0
        for i in range(0, len(url_list), 100):
            chunk = url_list[i:i+100]
            self.sb.table(table).update({
                'status':     'removed',
                'removed_at': now,
                'last_seen':  now,
            }).in_('url', chunk).execute()
            updated += len(chunk)
            log.info(f"  Marked removed: {updated}/{len(url_list)}")
        return updated

    def delete_urls(self, table: str, urls: set[str]) -> int:
        """Hard-delete rows by URL. Used for 'sold' — the 30-day window means
        aged-out rows should be purged, not marked removed (there's no
        'removed' status concept for sold records)."""
        if not urls:
            return 0
        url_list = list(urls)
        deleted = 0
        for i in range(0, len(url_list), 100):
            chunk = url_list[i:i+100]
            self.sb.table(table).delete().eq('lga_id', self.lga_id).in_('url', chunk).execute()
            deleted += len(chunk)
            log.info(f"  Deleted: {deleted}/{len(url_list)}")
        return deleted

    def get_removed_urls(self, table: str = 'listings') -> set[str]:
        resp = (self.sb.table(table)
                .select('url')
                .eq('lga_id', self.lga_id)
                .eq('status', 'removed')
                .execute())
        return {r['url'] for r in resp.data}

    def get_inactive_urls(self, table: str = 'listings') -> set[str]:
        """URLs currently marked removed OR removed_not_sold — candidates for
        reactivation if they show up live again."""
        resp = (self.sb.table(table)
                .select('url')
                .eq('lga_id', self.lga_id)
                .in_('status', ['removed', 'removed_not_sold'])
                .execute())
        return {r['url'] for r in resp.data}

    def reactivate(self, table: str, urls: set[str]) -> int:
        """A previously-removed listing is live again. Flip status back to
        active. NEVER touches first_seen — same URL means same original
        listing, not a new one."""
        if not urls:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        url_list = list(urls)
        reactivated = 0
        for i in range(0, len(url_list), 100):
            chunk = url_list[i:i+100]
            self.sb.table(table).update({
                'status':     'active',
                'removed_at': None,
                'last_seen':  now,
            }).eq('lga_id', self.lga_id).in_('url', chunk).execute()
            reactivated += len(chunk)
        return reactivated

    def get_sold_urls(self) -> set[str]:
        return self.get_all_urls('sold')

    def log_run(self, run_type, new_count, removed_count, updated_count, status, error_msg, duration_secs):
        self.sb.table('runs').insert({
            'lga_id':        self.lga_id,
            'run_type':      run_type,
            'new_count':     new_count,
            'removed_count': removed_count,
            'updated_count': updated_count,
            'status':        status,
            'error_msg':     error_msg,
            'duration_secs': round(duration_secs, 1),
        }).execute()


# ── Import CSV ────────────────────────────────────────────────────────────────
def import_csv(path: str, table: str, lga_id: int, sb: Client):
    store = LGAStore(sb, lga_id)
    rows = []
    dated = 0
    with open(path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            first_seen = None
            raw = (row.get('First Seen') or '').strip()
            if raw:
                try:
                    # Accept a plain date (YYYY-MM-DD) or a full ISO timestamp
                    parsed = datetime.fromisoformat(raw)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    first_seen = parsed.isoformat()
                    dated += 1
                except ValueError:
                    log.warning(f"Could not parse First Seen '{raw}' for {row.get('URL')} — falling back to today's date")
            rows.append({
                'address': row.get('Address', ''),
                'agent':   row.get('Agent', ''),
                'agency':  row.get('Agency', ''),
                'url':     row.get('URL', ''),
                **({'first_seen': first_seen} if first_seen else {}),
            })
    inserted = store.insert_new(table, rows)
    log.info(f"Imported {inserted} rows from {path} into {table} ({dated} with a real First Seen date, {inserted - dated} defaulted to today)")


# ── Core scrape ───────────────────────────────────────────────────────────────
def run_scrape(sb: Client, pool: CookiePool, lga: dict, run_type: str, max_pages: Optional[int]):
    lga_id   = lga['id']
    lga_name = lga['name']
    table    = 'listings' if run_type == 'listings' else 'sold'
    base_url = lga['search_url_listings'] if run_type == 'listings' else lga['search_url_sold']

    # Reload cookie pool from Supabase before each scrape type — ensures fresh cookies
    pool.reload()

    log.info(f"{'='*60}")
    log.info(f"LGA: {lga_name} ({lga_id})  |  Type: {run_type}")
    log.info(f"{'='*60}")

    store     = LGAStore(sb, lga_id)
    collector = URLCollector(pool, max_pages=max_pages)
    t_start   = time.time()
    new_count = removed_count = 0
    status    = 'ok'
    error_msg = ''

    try:
        # Load known URLs from Supabase BEFORE Phase 1.
        # IMPORTANT: for listings this must be ALL previously-seen URLs
        # (any status), not just active ones. If a withdrawn listing's URL
        # were excluded here, it would look "new" to Phase 1 and go through
        # insert_new() below — which sets first_seen = now on every row,
        # silently wiping the listing's real first_seen on restoration.
        known_urls   = store.get_all_urls(table)
        inactive_urls = store.get_inactive_urls(table) if table == 'listings' else set()
        log.info(f"Known URLs in Supabase: {len(known_urls)} ({len(inactive_urls)} currently inactive)")

        # Phase 1: Collect URLs — filter known ones per page
        log.info("Phase 1: Collecting URLs...")
        cutoff    = 30 if run_type == 'sold' else 0
        live_urls = set(collector.collect_urls(base_url, sold_cutoff_days=cutoff, known_urls=known_urls))
        log.info(f"Live URLs: {len(live_urls)}")

        new_urls        = set(live_urls)  # collect_urls already filtered known ones — genuinely new URLs only
        # Only ACTIVE known URLs that disappeared count as a fresh removal.
        # Excluding already-inactive URLs here matters: without it, a listing
        # gone for weeks would get re-marked 'removed' (and removed_at
        # refreshed to today) on every single run, permanently corrupting the
        # daily/weekly "removed today" figures.
        removed_urls     = (known_urls - collector._last_all_live_urls) - inactive_urls
        reactivated_urls = collector._last_all_live_urls & inactive_urls  # known + inactive + live again = restored

        log.info(f"New: {len(new_urls)}  |  Removed: {len(removed_urls)}  |  Reactivated: {len(reactivated_urls)}")

        # ── Safety check ─────────────────────────────────────────────────────
        # If Phase 1 collected zero or very few URLs vs what we know exists,
        # something went wrong (cookies burned, REA blocked, network error).
        # Never mark removals in this case — abort to protect existing data.
        all_live_count = len(collector._last_all_live_urls)
        if known_urls and all_live_count < max(10, len(known_urls) * 0.1):
            log.error(
                f"SAFETY ABORT: Phase 1 collected only {all_live_count} URLs "
                f"vs {len(known_urls)} known. Possible scrape failure — "
                f"skipping removal step to protect existing data."
            )
            removed_urls = set()  # Clear removals — do not mark anything

        # Phase 2: Playwright detail scrape for new URLs only
        if new_urls:
            log.info(f"Phase 2: Playwright scraping {len(new_urls)} new listings...")
            new_rows  = scrape_details_playwright(list(new_urls), pool)
            new_count = store.insert_new(table, new_rows)
            log.info(f"Inserted {new_count} new rows")
        else:
            log.info("No new listings")

        # Reactivate listings that were removed/removed_not_sold and are live
        # again — status flips back to active, first_seen is left untouched.
        if reactivated_urls:
            log.info(f"Reactivating {len(reactivated_urls)} restored listings...")
            reactivated_count = store.reactivate(table, reactivated_urls)
            log.info(f"Reactivated {reactivated_count} rows")

        # Mark removed (listings) or hard-delete (sold — 30-day rolling window)
        if removed_urls:
            if table == 'sold':
                log.info(f"Deleting {len(removed_urls)} aged-out sold records...")
                removed_count = store.delete_urls(table, removed_urls)
            else:
                log.info(f"Marking {len(removed_urls)} as removed...")
                removed_count = store.mark_removed(table, removed_urls)
        else:
            log.info("No removals")

    except Exception as e:
        status    = 'error'
        error_msg = str(e)
        log.error(f"Run failed: {e}", exc_info=True)

    duration = time.time() - t_start
    store.log_run(run_type, new_count, removed_count, 0, status, error_msg, duration)
    log.info(f"Complete in {duration:.1f}s — new:{new_count} removed:{removed_count}")
    return status


# ── Reconcile ─────────────────────────────────────────────────────────────────
def run_reconcile(sb: Client, lga: dict):
    lga_id = lga['id']
    log.info(f"{'='*60}")
    log.info(f"Reconcile: {lga['name']} ({lga_id})")
    log.info(f"{'='*60}")
    t_start = time.time()
    store   = LGAStore(sb, lga_id)

    removed_listing_urls = store.get_removed_urls('listings')
    sold_urls_raw        = store.get_sold_urls()
    # Listing URLs don't have /sold/ — normalise sold URLs to match before comparing
    sold_urls            = {u.replace('realestate.com.au/sold/', 'realestate.com.au/') for u in sold_urls_raw}
    removed_not_sold     = removed_listing_urls - sold_urls
    removed_sold         = removed_listing_urls & sold_urls

    log.info(f"Removed from listings: {len(removed_listing_urls)}")
    log.info(f"  → Confirmed sold:     {len(removed_sold)}")
    log.info(f"  → Removed NOT sold:   {len(removed_not_sold)}  ← dashboard hot list")

    if removed_not_sold:
        url_list = list(removed_not_sold)
        for i in range(0, len(url_list), 50):
            chunk = url_list[i:i+50]
            sb.table('listings').update({'status': 'removed_not_sold'}).in_('url', chunk).execute()

    duration = time.time() - t_start
    store.log_run('reconcile', 0, len(removed_not_sold), len(removed_sold), 'ok', '', duration)
    log.info(f"Reconcile complete in {duration:.1f}s")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='REA Scraper')
    parser.add_argument('--lga',           type=int, nargs='+')
    parser.add_argument('--type',          choices=['listings', 'sold', 'both'], default='both')
    parser.add_argument('--max-pages',     type=int)
    parser.add_argument('--reconcile-only',action='store_true')
    parser.add_argument('--import-csv',    type=str)
    parser.add_argument('--import-table',  choices=['listings', 'sold'], default='listings')
    parser.add_argument('--parallel',      type=int, default=1,
                        help='Number of LGAs to scrape in parallel (default: 1)')
    args = parser.parse_args()

    sb = get_supabase()

    if args.import_csv:
        if not args.lga:
            parser.error("--import-csv requires --lga")
        import_csv(args.import_csv, args.import_table, args.lga[0], sb)
        return

    if args.parallel > 5:
        log.warning(f"--parallel {args.parallel} requested, but there are only 5 cookie slots — "
                    f"anything past 5 concurrent LGAs will just queue and wait for a slot to free up.")

    query = sb.table('lgas').select('*').eq('active', True)
    if args.lga:
        query = query.in_('id', args.lga)
    lgas = query.execute().data

    if not lgas:
        log.error("No active LGAs found")
        sys.exit(1)

    log.info(f"Running {len(lgas)} LGA(s): {[l['name'] for l in lgas]}")
    max_pages = args.max_pages or MAX_PAGES

    def process_lga(lga):
        if args.reconcile_only:
            run_reconcile(sb, lga)
            return

        # Every LGA run claims its own cookie slot — even in sequential mode,
        # since a webhook-triggered hydration could be running concurrently
        # on the same Railway service and needs to coordinate against this
        # too, not just against other cron threads.
        runner = f"lga-{lga['id']}"
        slot = claim_cookie_slot(sb, runner)
        try:
            pool = CookiePool(COOKIES_FILE, slot_index=slot)
            if args.type in ('listings', 'both'):
                run_scrape(sb, pool, lga, 'listings', max_pages)
            if args.type in ('sold', 'both'):
                run_scrape(sb, pool, lga, 'sold', max_pages)
            if args.type == 'both':
                run_reconcile(sb, lga)
        finally:
            release_cookie_slot(sb, slot, runner)

    if args.parallel > 1:
        log.info(f"Running {args.parallel} LGAs in parallel")
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {executor.submit(process_lga, lga): lga['name'] for lga in lgas}
            for future in as_completed(futures):
                lga_name = futures[future]
                try:
                    future.result()
                except Exception as e:
                    log.error(f"{lga_name} failed: {e}")
    else:
        for lga in lgas:
            process_lga(lga)

    log.info("All LGAs complete.")


if __name__ == '__main__':
    main()
