"""
Ventura County Motivated Seller Lead Scraper
============================================
Clerk portal  : https://clerkrecorderselfservice.venturacounty.gov
Assessor data : https://assessor.venturacounty.gov/assessor-data/property-search/

Playwright (async) handles the clerk portal (disclaimer + search).
requests + BeautifulSoup handle any static pages.
dbfread handles the assessor bulk parcel DBF.

Outputs
  dashboard/records.json
  data/records.json
  data/ghl_export.csv
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

RECORDS_JSON = DATA_DIR / "records.json"
DASHBOARD_JSON = DASHBOARD_DIR / "records.json"
GHL_CSV = DATA_DIR / "ghl_export.csv"

# ── Config ────────────────────────────────────────────────────────────────────
CLERK_BASE = "https://clerkrecorderselfservice.venturacounty.gov"
DISCLAIMER_URL = f"{CLERK_BASE}/web/user/disclaimer"
CLERK_SEARCH_URL = f"{CLERK_BASE}/web/guest/search"
ASSESSOR_SEARCH_URL = "https://assessor.venturacounty.gov/assessor-data/property-search/"
DEBUG_MODE = os.getenv("DEBUG", "false").lower() == "true"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
MAX_PAGES = 100
REQUEST_DELAY = 1.2
RETRY_ATTEMPTS = 3

# Document type codes → (category key, human label)
DOC_TYPE_MAP: dict[str, tuple[str, str]] = {
    "LP":       ("lis_pendens",    "Lis Pendens"),
    "NOFC":     ("foreclosure",    "Notice of Foreclosure"),
    "TAXDEED":  ("tax_deed",       "Tax Deed"),
    "JUD":      ("judgment",       "Judgment"),
    "CCJ":      ("judgment",       "Certified Judgment"),
    "DRJUD":    ("judgment",       "Domestic Judgment"),
    "LNCORPTX": ("tax_lien",       "Corp Tax Lien"),
    "LNIRS":    ("tax_lien",       "IRS Lien"),
    "LNFED":    ("tax_lien",       "Federal Lien"),
    "LN":       ("lien",           "Lien"),
    "LNMECH":   ("mechanic_lien",  "Mechanic Lien"),
    "LNHOA":    ("lien",           "HOA Lien"),
    "MEDLN":    ("lien",           "Medicaid Lien"),
    "PRO":      ("probate",        "Probate"),
    "NOC":      ("notice",         "Notice of Commencement"),
    "RELLP":    ("release",        "Release Lis Pendens"),
}


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Record:
    doc_num: str = ""
    doc_type: str = ""
    filed: str = ""
    cat: str = ""
    cat_label: str = ""
    owner: str = ""
    grantee: str = ""
    amount: Optional[float] = None
    legal: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = "CA"
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""
    clerk_url: str = ""
    flags: list = field(default_factory=list)
    score: int = 0


# ── Retry helper ──────────────────────────────────────────────────────────────
def with_retry(fn, *args, attempts: int = RETRY_ATTEMPTS, delay: float = 2.0, **kwargs):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            log.warning("Attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay * attempt)
    log.error("All %d attempts failed. Last error: %s", attempts, last_exc)
    return None


# ════════════════════════════════════════════════════════════════════════════
# ASSESSOR: Bulk parcel DBF download + owner lookup
# ════════════════════════════════════════════════════════════════════════════

def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def _name_variants(raw: str) -> list[str]:
    """
    Generate lookup variants for a raw owner name string.
    Handles: 'JOHN SMITH', 'SMITH JOHN', 'SMITH, JOHN'
    """
    n = raw.strip().upper()
    variants = [_normalize_name(n)]
    # Remove comma variant: "SMITH, JOHN" → "SMITH JOHN"
    no_comma = n.replace(",", "").strip()
    variants.append(_normalize_name(no_comma))
    # If two tokens, try reversed
    parts = no_comma.split()
    if len(parts) == 2:
        variants.append(_normalize_name(f"{parts[1]} {parts[0]}"))
    return list(dict.fromkeys(v for v in variants if v))  # dedupe, preserve order


def download_assessor_dbf() -> Optional[dict[str, dict]]:
    """
    Attempt to download bulk parcel data from the assessor portal.
    Returns a dict keyed by normalized owner name → parcel info.
    Falls back gracefully if download fails.
    """
    try:
        from dbfread import DBF
    except ImportError:
        log.warning("dbfread not installed — skipping parcel enrichment")
        return {}

    log.info("Fetching assessor parcel data from %s", ASSESSOR_SEARCH_URL)
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    })

    # ── Step 1: GET the search page to find the download link / __doPostBack ─
    resp = with_retry(session.get, ASSESSOR_SEARCH_URL, timeout=30)
    if resp is None or not resp.ok:
        log.warning("Could not load assessor page — skipping parcel enrichment")
        return {}

    soup = BeautifulSoup(resp.text, "lxml")

    # Look for a direct .zip or .dbf download link
    dbf_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(dbf|zip)(\?|$)", href, re.I):
            dbf_url = urljoin(ASSESSOR_SEARCH_URL, href)
            break

    # If no direct link, try __doPostBack pattern
    if not dbf_url:
        for inp in soup.find_all("input", {"type": "submit"}):
            val = inp.get("value", "").lower()
            if any(k in val for k in ["download", "dbf", "bulk", "export"]):
                event_target = inp.get("name", "")
                if event_target:
                    viewstate = soup.find("input", {"name": "__VIEWSTATE"})
                    eventval  = soup.find("input", {"name": "__EVENTVALIDATION"})
                    payload = {
                        "__EVENTTARGET":     event_target,
                        "__EVENTARGUMENT":   "",
                        "__VIEWSTATE":       viewstate["value"] if viewstate else "",
                        "__EVENTVALIDATION": eventval["value"] if eventval else "",
                    }
                    log.info("Trying __doPostBack for assessor download")
                    r2 = with_retry(session.post, ASSESSOR_SEARCH_URL,
                                    data=payload, timeout=60)
                    if r2 and r2.ok:
                        # If response is a file, treat as download
                        ct = r2.headers.get("Content-Type", "")
                        if "zip" in ct or "octet" in ct or "dbf" in ct:
                            return _parse_dbf_bytes(r2.content)
                    break  # stop after first submit attempt

    # Direct URL download
    if dbf_url:
        log.info("Downloading assessor DBF/ZIP from %s", dbf_url)
        r = with_retry(session.get, dbf_url, timeout=120, stream=True)
        if r and r.ok:
            return _parse_dbf_bytes(r.content)

    log.warning("No assessor DBF download found — parcel addresses will be empty")
    return {}


def _parse_dbf_bytes(raw: bytes) -> dict[str, dict]:
    """
    Parse a raw bytes blob (either a .dbf file or a .zip containing one)
    into an owner-name → parcel-info lookup dict.
    """
    from dbfread import DBF

    lookup: dict[str, dict] = {}

    # Unzip if needed
    if raw[:2] == b"PK":  # ZIP magic
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
            if not dbf_names:
                log.warning("No .dbf file found inside ZIP")
                return lookup
            raw = zf.read(dbf_names[0])

    # Write to a temp file because dbfread needs a path
    tmp = DATA_DIR / "_parcel_tmp.dbf"
    tmp.write_bytes(raw)

    try:
        table = DBF(str(tmp), lowernames=True, ignore_missing_memofile=True)
        col_map = _detect_assessor_columns(table.field_names)
        log.info("Assessor DBF columns detected: %s", col_map)

        for row in table:
            try:
                owner_raw = str(row.get(col_map.get("owner", ""), "") or "").strip()
                if not owner_raw:
                    continue
                parcel = {
                    "prop_address": str(row.get(col_map.get("site_addr", ""), "") or "").strip(),
                    "prop_city":    str(row.get(col_map.get("site_city", ""), "") or "").strip(),
                    "prop_zip":     str(row.get(col_map.get("site_zip",  ""), "") or "").strip(),
                    "mail_address": str(row.get(col_map.get("mail_addr", ""), "") or "").strip(),
                    "mail_city":    str(row.get(col_map.get("mail_city", ""), "") or "").strip(),
                    "mail_state":   str(row.get(col_map.get("mail_state",""), "") or "").strip(),
                    "mail_zip":     str(row.get(col_map.get("mail_zip",  ""), "") or "").strip(),
                }
                for variant in _name_variants(owner_raw):
                    lookup.setdefault(variant, parcel)
            except Exception:
                continue

        log.info("Assessor lookup built: %d owner entries", len(lookup))
    finally:
        tmp.unlink(missing_ok=True)

    return lookup


def _detect_assessor_columns(field_names: list[str]) -> dict[str, str]:
    """
    Map logical column roles to actual DBF field names.
    Handles naming variations across assessor exports.
    """
    fnames = [f.lower() for f in field_names]

    def pick(*candidates) -> str:
        for c in candidates:
            if c in fnames:
                return c
        return ""

    return {
        "owner":      pick("owner", "own1", "ownername", "owner_name"),
        "site_addr":  pick("site_addr", "siteaddr", "prop_addr", "propaddr", "address"),
        "site_city":  pick("site_city", "sitecity", "prop_city", "propcity"),
        "site_zip":   pick("site_zip",  "sitezip",  "prop_zip",  "propzip"),
        "mail_addr":  pick("addr_1", "mailadr1", "mail_addr", "mailaddr", "mailing"),
        "mail_city":  pick("city",   "mailcity", "mail_city"),
        "mail_state": pick("state",  "mailstate","mail_state"),
        "mail_zip":   pick("zip",    "mailzip",  "mail_zip"),
    }


def enrich_with_parcel(rec: Record, lookup: dict[str, dict]) -> Record:
    """Look up owner name in parcel table and fill address fields."""
    if not lookup or not rec.owner:
        return rec
    for variant in _name_variants(rec.owner):
        parcel = lookup.get(variant)
        if parcel:
            rec.prop_address = rec.prop_address or parcel.get("prop_address", "")
            rec.prop_city    = rec.prop_city    or parcel.get("prop_city",    "")
            rec.prop_zip     = rec.prop_zip     or parcel.get("prop_zip",     "")
            rec.mail_address = rec.mail_address or parcel.get("mail_address", "")
            rec.mail_city    = rec.mail_city    or parcel.get("mail_city",    "")
            rec.mail_state   = rec.mail_state   or parcel.get("mail_state",   "")
            rec.mail_zip     = rec.mail_zip     or parcel.get("mail_zip",     "")
            break
    return rec


# ════════════════════════════════════════════════════════════════════════════
# CLERK PORTAL: Playwright async scraper
# ════════════════════════════════════════════════════════════════════════════

async def _accept_disclaimer(page) -> bool:
    """
    Accept the Tyler Technologies disclaimer.
    Must be called while the page is already on the disclaimer URL.
    Returns True if accepted successfully.
    """
    from playwright.async_api import TimeoutError as PWTimeout

    # Wait for the JS-rendered "I Accept" button to appear
    try:
        await page.wait_for_selector(
            "#submitDisclaimerAccept, button:has-text('I Accept'), input[value*='Accept' i]",
            timeout=10_000,
        )
    except PWTimeout:
        log.warning("Disclaimer accept button never appeared")
        if DEBUG_MODE:
            (DATA_DIR / "debug_disclaimer.html").write_text(await page.content())
        return False

    for selector in [
        "#submitDisclaimerAccept",
        "button:has-text('I Accept')",
        "text=I Accept",
        "input[value*='Accept' i]",
        "button:has-text('Agree')",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                log.info("Disclaimer accepted via: %s", selector)
                # Wait for redirect away from disclaimer
                await page.wait_for_function(
                    "() => !window.location.href.includes('disclaimer')",
                    timeout=15_000,
                )
                await page.wait_for_load_state("networkidle", timeout=15_000)
                log.info("Post-disclaimer URL: %s", page.url)
                return True
        except PWTimeout:
            continue
        except Exception as exc:
            log.debug("Selector %s failed: %s", selector, exc)

    log.warning("Could not click disclaimer accept button")
    return False


async def clerk_scrape(doc_codes: list[str], date_from: str, date_to: str) -> list[Record]:
    """
    Use Playwright to navigate the Ventura County clerk portal,
    accept the disclaimer, and scrape each document type.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("playwright not installed — run: pip install playwright && playwright install chromium")
        return []

    records: list[Record] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # ── 1. Accept disclaimer ─────────────────────────────────────────────
        # Navigate to the SEARCH page first — the portal auto-redirects to
        # disclaimer with the correct ?redirect= param, so after acceptance
        # we land back on the search page (not /web/errors).
        log.info("Navigating to search page (portal will redirect through disclaimer)")
        try:
            await page.goto(CLERK_SEARCH_URL, timeout=30_000, wait_until="networkidle")
            log.info("Landed on: %s", page.url)

            if "disclaimer" in page.url.lower():
                accepted = await _accept_disclaimer(page)
                if not accepted:
                    log.error("Disclaimer not accepted — aborting scrape")
                    await browser.close()
                    return []
                log.info("After disclaimer, now at: %s", page.url)
            else:
                log.info("No disclaimer redirect — already on search page")

        except Exception as exc:
            log.error("Failed to load search page: %s", exc)
            await browser.close()
            return []

        # ── 2. Scrape each doc type ──────────────────────────────────────────
        for code in doc_codes:
            cat, cat_label = DOC_TYPE_MAP.get(code, ("other", code))
            log.info("Scraping doc type: %s (%s)", code, cat_label)

            page_records = await _scrape_doc_type(
                page, code, cat, cat_label, date_from, date_to
            )
            log.info("  → %d records for %s", len(page_records), code)
            records.extend(page_records)

        await browser.close()

    log.info("Total clerk records collected: %d", len(records))
    return records


async def _scrape_doc_type(page, code: str, cat: str, cat_label: str,
                            date_from: str, date_to: str) -> list[Record]:
    """Navigate to search, fill form, paginate, parse all results."""
    from playwright.async_api import TimeoutError as PWTimeout

    records: list[Record] = []

    # ── Navigate to the search page ──────────────────────────────────────────
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            await page.goto(CLERK_SEARCH_URL, timeout=30_000, wait_until="networkidle")
            # If disclaimer reappears (session expired), re-accept
            if "disclaimer" in page.url.lower():
                log.warning("Session expired mid-scrape — re-accepting disclaimer")
                accepted = await _accept_disclaimer(page)
                if not accepted:
                    log.error("Could not re-accept disclaimer for %s — skipping", code)
                    return records
            break
        except PWTimeout:
            log.warning("Timeout navigating to search page for %s (attempt %d)", code, attempt)
            if attempt == RETRY_ATTEMPTS:
                return records
            await asyncio.sleep(2 * attempt)

    # ── Wait for JS-rendered search form ─────────────────────────────────────
    # Tyler Technologies Self-Service renders the form via jQuery Mobile after
    # networkidle — we must wait for an actual form element to be present.
    form_appeared = False
    for form_sel in [
        "form", "select[name]", "input[name]",
        "[id*='searchForm']", "[id*='SearchForm']",
        "[class*='search']", "[data-role='page']",
    ]:
        try:
            await page.wait_for_selector(form_sel, timeout=8_000)
            form_appeared = True
            log.debug("Form detected via selector: %s", form_sel)
            break
        except PWTimeout:
            continue

    if not form_appeared:
        log.warning("No search form detected for %s — dumping page for debug", code)
        if DEBUG_MODE:
            (DATA_DIR / f"debug_search_{code}.html").write_text(await page.content())
        else:
            # Always save first failure for diagnosis
            debug_f = DATA_DIR / "debug_search_notype.html"
            if not debug_f.exists():
                debug_f.write_text(await page.content())

    # ── Try to fill the search form ───────────────────────────────────────────
    form_filled = False
    try:
        # Doc type field
        for dt_sel in [
            "select[name*='DocType' i]", "select[name*='doctype' i]",
            "select[id*='DocType' i]", "select[id*='docType' i]",
            "input[name*='DocType' i]", "input[name*='doctype' i]",
            "#DocType", "#docType",
        ]:
            el = page.locator(dt_sel).first
            try:
                if await el.is_visible(timeout=2_000):
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    if tag == "select":
                        await el.select_option(value=code)
                    else:
                        await el.fill(code)
                    log.debug("Filled doc type '%s' via %s", code, dt_sel)
                    form_filled = True
                    break
            except PWTimeout:
                continue

        # Date from
        for df_sel in [
            "input[name*='DateFrom' i]", "input[name*='dateFrom' i]",
            "input[name*='StartDate' i]", "input[name*='startDate' i]",
            "input[id*='DateFrom' i]", "#DateFrom", "#StartDate",
        ]:
            el = page.locator(df_sel).first
            try:
                if await el.is_visible(timeout=2_000):
                    await el.fill(date_from)
                    log.debug("Filled date_from '%s' via %s", date_from, df_sel)
                    break
            except PWTimeout:
                continue

        # Date to
        for dt_sel2 in [
            "input[name*='DateTo' i]", "input[name*='dateTo' i]",
            "input[name*='EndDate' i]", "input[name*='endDate' i]",
            "input[id*='DateTo' i]", "#DateTo", "#EndDate",
        ]:
            el = page.locator(dt_sel2).first
            try:
                if await el.is_visible(timeout=2_000):
                    await el.fill(date_to)
                    log.debug("Filled date_to '%s' via %s", date_to, dt_sel2)
                    break
            except PWTimeout:
                continue

        # Submit
        for btn_sel in [
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Search')", "a:has-text('Search')",
            "[id*='search' i][type='button']",
        ]:
            btn = page.locator(btn_sel).first
            try:
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                    log.debug("Clicked search button via %s", btn_sel)
                    break
            except PWTimeout:
                continue

    except Exception as exc:
        log.warning("Form fill error for %s: %s", code, exc)
        if DEBUG_MODE:
            (DATA_DIR / f"debug_form_{code}.html").write_text(await page.content())

    if not form_filled:
        log.warning("Could not fill search form for doc type %s — no matching selectors", code)
        if DEBUG_MODE:
            (DATA_DIR / f"debug_nofill_{code}.html").write_text(await page.content())

    # ── Paginate ─────────────────────────────────────────────────────────────
    page_num = 1
    while page_num <= MAX_PAGES:
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")
        page_records = _parse_clerk_table(soup, code, cat, cat_label)

        if not page_records:
            log.debug("No records on page %d for %s", page_num, code)
            break

        records.extend(page_records)
        log.info("  Page %d: %d records", page_num, len(page_records))

        # Try to click "Next" pagination button
        next_found = False
        for next_sel in [
            "a:has-text('Next')", "a:has-text('>')", "a:has-text('→')",
            "[aria-label='Next page']", ".pagination-next", "#nextPage",
            "li.next a", "a.page-link:has-text('Next')",
        ]:
            try:
                btn = page.locator(next_sel).first
                if await btn.is_visible(timeout=1_500) and await btn.is_enabled():
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    next_found = True
                    break
            except Exception:
                continue

        if not next_found:
            break
        page_num += 1

    return records


def _parse_clerk_table(soup: BeautifulSoup, code: str, cat: str, cat_label: str) -> list[Record]:
    """
    Parse the results table from the clerk portal HTML.
    Detects column positions dynamically.
    """
    records: list[Record] = []

    table = (
        soup.find("table", {"class": re.compile(r"result|search|record", re.I)})
        or soup.find("table", {"id": re.compile(r"result|search|record", re.I)})
        or soup.find("table")
    )
    if not table:
        return records

    rows = table.find_all("tr")
    if len(rows) < 2:
        return records

    headers = [
        th.get_text(" ", strip=True).lower()
        for th in rows[0].find_all(["th", "td"])
    ]

    def col(*keywords):
        for i, h in enumerate(headers):
            if any(k in h for k in keywords):
                return i
        return None

    c_docnum  = col("doc", "instrument", "number", "book")
    c_date    = col("date", "record", "filed")
    c_grantor = col("grantor", "seller", "from", "owner")
    c_grantee = col("grantee", "buyer",  "to",   "lender")
    c_legal   = col("legal",   "descr",  "parcel")
    c_amount  = col("amount",  "debt",   "value", "consideration")

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        try:
            def cell(idx) -> str:
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx].get_text(" ", strip=True)

            # Pull detail link
            link = row.find("a", href=True)
            clerk_url = urljoin(CLERK_BASE, link["href"]) if link else ""

            # Parse amount
            amount_raw = cell(c_amount)
            amount = None
            if amount_raw:
                m = re.search(r"[\d,]+\.?\d*", amount_raw.replace(",", ""))
                if m:
                    try:
                        amount = float(m.group().replace(",", ""))
                    except ValueError:
                        pass

            rec = Record(
                doc_num   = cell(c_docnum),
                doc_type  = code,
                filed     = cell(c_date),
                cat       = cat,
                cat_label = cat_label,
                owner     = cell(c_grantor),
                grantee   = cell(c_grantee),
                amount    = amount,
                legal     = cell(c_legal),
                clerk_url = clerk_url,
            )
            records.append(rec)
        except Exception as exc:
            log.warning("Row parse error: %s — skipping", exc)
            continue

    return records


# ════════════════════════════════════════════════════════════════════════════
# SCORING
# ════════════════════════════════════════════════════════════════════════════

def _is_new_this_week(filed_str: str) -> bool:
    if not filed_str:
        return False
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            d = datetime.strptime(filed_str.strip(), fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - d).days <= 7
        except ValueError:
            continue
    return False


def score_record(rec: Record) -> Record:
    """
    Assign a seller distress score (0-100) and flags list.
    Mutates in place.
    """
    score = 30  # base
    flags: list[str] = []
    cat = rec.cat
    doc = rec.doc_type.upper()

    if cat == "lis_pendens":
        flags.append("Lis pendens")
        score += 10
    if cat == "foreclosure":
        flags.append("Pre-foreclosure")
        score += 10
    if cat == "lis_pendens" and any(
        r.cat == "foreclosure" and r.owner == rec.owner
        for r in []  # cross-check done after all records collected
    ):
        score += 20  # LP+FC combo — handled post-collection

    if cat == "judgment":
        flags.append("Judgment lien")
        score += 10
    if cat == "tax_lien":
        flags.append("Tax lien")
        score += 10
    if cat == "mechanic_lien":
        flags.append("Mechanic lien")
        score += 10
    if cat == "probate":
        flags.append("Probate / estate")
        score += 10

    # LLC / corp owner detection
    if re.search(r"\b(LLC|INC|CORP|LP|LLP|TRUST|ESTATE)\b", rec.owner.upper()):
        flags.append("LLC / corp owner")
        score += 10

    # Amount bonuses
    if rec.amount and rec.amount > 100_000:
        score += 15
    elif rec.amount and rec.amount > 50_000:
        score += 10

    # New this week
    if _is_new_this_week(rec.filed):
        flags.append("New this week")
        score += 5

    # Has property address
    if rec.prop_address:
        flags.append("Has address")
        score += 5

    rec.flags = flags
    rec.score = min(score, 100)
    return rec


def apply_lp_fc_combo_bonus(records: list[Record]) -> list[Record]:
    """
    After all records collected: +20 to any LP record
    where the same owner also has a foreclosure record.
    """
    fc_owners = {
        r.owner.upper()
        for r in records
        if r.cat == "foreclosure" and r.owner
    }
    for rec in records:
        if rec.cat == "lis_pendens" and rec.owner.upper() in fc_owners:
            if "Pre-foreclosure" not in rec.flags:
                rec.flags.append("Pre-foreclosure")
            rec.score = min(rec.score + 20, 100)
    return records


# ════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ════════════════════════════════════════════════════════════════════════════

def save_records_json(records: list[Record], date_from: str, date_to: str) -> None:
    with_address = sum(1 for r in records if r.prop_address)
    payload = {
        "fetched_at":    datetime.utcnow().isoformat() + "Z",
        "source":        "Ventura County Clerk-Recorder",
        "date_range":    {"from": date_from, "to": date_to},
        "total":         len(records),
        "with_address":  with_address,
        "records":       [asdict(r) for r in records],
    }
    json_str = json.dumps(payload, indent=2, default=str)
    RECORDS_JSON.write_text(json_str, encoding="utf-8")
    DASHBOARD_JSON.write_text(json_str, encoding="utf-8")
    log.info("Saved records.json (%d records, %d with address)", len(records), with_address)


def save_ghl_csv(records: list[Record]) -> None:
    """GoHighLevel-compatible CSV export."""
    GHL_COLS = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]

    def split_name(full: str) -> tuple[str, str]:
        parts = full.strip().split()
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], ""
        return " ".join(parts[:-1]), parts[-1]

    with open(GHL_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_COLS)
        writer.writeheader()
        for r in records:
            first, last = split_name(r.owner)
            writer.writerow({
                "First Name":             first,
                "Last Name":              last,
                "Mailing Address":        r.mail_address,
                "Mailing City":           r.mail_city,
                "Mailing State":          r.mail_state or "CA",
                "Mailing Zip":            r.mail_zip,
                "Property Address":       r.prop_address,
                "Property City":          r.prop_city,
                "Property State":         r.prop_state or "CA",
                "Property Zip":           r.prop_zip,
                "Lead Type":              r.cat_label,
                "Document Type":          r.doc_type,
                "Date Filed":             r.filed,
                "Document Number":        r.doc_num,
                "Amount/Debt Owed":       f"{r.amount:.2f}" if r.amount else "",
                "Seller Score":           r.score,
                "Motivated Seller Flags": " | ".join(r.flags),
                "Source":                 "Ventura County Clerk-Recorder",
                "Public Records URL":     r.clerk_url,
            })
    log.info("Saved GHL CSV → %s", GHL_CSV)


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  Ventura County Motivated Seller Lead Scraper    ║")
    log.info("╚══════════════════════════════════════════════════╝")

    # Date range
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    date_from = start.strftime("%m/%d/%Y")
    date_to   = today.strftime("%m/%d/%Y")
    log.info("Date range: %s → %s", date_from, date_to)

    # ── 1. Assessor parcel lookup ────────────────────────────────────────────
    log.info("Step 1/4: Building assessor parcel lookup …")
    parcel_lookup = download_assessor_dbf() or {}

    # ── 2. Clerk scrape ──────────────────────────────────────────────────────
    log.info("Step 2/4: Scraping clerk portal …")
    doc_codes = list(DOC_TYPE_MAP.keys())
    records = await clerk_scrape(doc_codes, date_from, date_to)

    # ── 3. Enrich + score ────────────────────────────────────────────────────
    log.info("Step 3/4: Enriching and scoring %d records …", len(records))
    for rec in records:
        enrich_with_parcel(rec, parcel_lookup)
        score_record(rec)
    records = apply_lp_fc_combo_bonus(records)
    records.sort(key=lambda r: r.score, reverse=True)

    # Deduplicate by doc_num
    seen: set[str] = set()
    unique: list[Record] = []
    for r in records:
        key = r.doc_num or f"{r.owner}:{r.filed}:{r.doc_type}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    log.info("After dedup: %d unique records", len(unique))

    # ── 4. Save outputs ──────────────────────────────────────────────────────
    log.info("Step 4/4: Saving outputs …")
    save_records_json(unique, date_from, date_to)
    save_ghl_csv(unique)

    # ── Summary ──────────────────────────────────────────────────────────────
    high   = [r for r in unique if r.score >= 70]
    medium = [r for r in unique if 40 <= r.score < 70]
    low    = [r for r in unique if r.score < 40]
    with_addr = [r for r in unique if r.prop_address]

    log.info("─" * 52)
    log.info("  Total records    : %d", len(unique))
    log.info("  High score ≥70   : %d", len(high))
    log.info("  Medium 40-69     : %d", len(medium))
    log.info("  Low <40          : %d", len(low))
    log.info("  With address     : %d", len(with_addr))
    if unique:
        log.info("  Top lead         : %s [score=%d]", unique[0].owner, unique[0].score)
    log.info("─" * 52)

    # GitHub Actions job summary
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Ventura County Leads Scraper Results\n\n")
            f.write("| Metric | Value |\n|--------|-------|\n")
            f.write(f"| Total records | {len(unique)} |\n")
            f.write(f"| High score (≥70) | {len(high)} |\n")
            f.write(f"| Medium score (40-69) | {len(medium)} |\n")
            f.write(f"| Low score (<40) | {len(low)} |\n")
            f.write(f"| With address | {len(with_addr)} |\n")
            f.write(f"| Date range | {date_from} → {date_to} |\n")
            f.write(f"| Generated at | {datetime.utcnow().isoformat()}Z |\n")


if __name__ == "__main__":
    asyncio.run(main())
