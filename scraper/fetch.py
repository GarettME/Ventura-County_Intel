"""
Ventura County Motivated Seller Lead Scraper
============================================
Clerk portal  : https://clerkrecorderselfservice.venturacounty.gov
Search page   : /web/search/DOCSEARCH17S3
Disclaimer    : /web/user/disclaimer  (then redirects to action group)

The portal is a JavaScript SPA — Playwright fills the form and parses
the rendered card-based results (NOT a table).

Outputs
  dashboard/records.json
  data/records.json
  data/ghl_export.csv
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).resolve().parent.parent
DATA_DIR      = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

RECORDS_JSON  = DATA_DIR      / "records.json"
DASHBOARD_JSON= DASHBOARD_DIR / "records.json"
GHL_CSV       = DATA_DIR      / "ghl_export.csv"

# ── Config ────────────────────────────────────────────────────────────────────
CLERK_BASE      = "https://clerkrecorderselfservice.venturacounty.gov"
DISCLAIMER_URL  = f"{CLERK_BASE}/web/user/disclaimer"
SEARCH_URL      = f"{CLERK_BASE}/web/search/DOCSEARCH17S3"
RESULTS_URL     = f"{CLERK_BASE}/web/searchResults/DOCSEARCH17S3"

LOOKBACK_DAYS   = int(os.getenv("LOOKBACK_DAYS", "7"))
MAX_PAGES       = 20        # safety cap
RETRY_ATTEMPTS  = 3

# Search terms to submit — must match the EXACT document-type labels as they
# appear in the Ventura portal's autocomplete (confirmed against the live DOM).
# The portal matches "Contains Any", so each term also returns longer labels
# that contain it (e.g. "NOTICE OF DEFAULT" also returns "RESCISSION NOTICE OF
# DEFAULT"); _categorize() below sorts each returned row by its ACTUAL type and
# drops reversals/satisfactions that aren't motivated-seller signals.
SEARCH_TERMS: list[str] = [
    "NOTICE OF DEFAULT",
    "NOTICE OF TRUSTEES SALE",   # county spells it "TRUSTEES" (was "TRUSTEE" — returned 0)
    "NOTICE ACTION",             # Notice of Pendency of Action = lis pendens equivalent
    "JUDGMENT",
    "ABSTRACT OF JUDGMENT",
    "FEDERAL TAX LIEN",
    "TAX LIEN",
    "MECHANICS LIEN",
]

# Document types that REVERSE or SATISFY an obligation — a returned row whose
# actual type contains any of these is not a motivated-seller lead and is skipped.
_EXCLUDE_TOKENS = (
    "RESCISSION", "RELEASE", "SATISFACTION", "SUBSTITUTION", "REQUEST",
    "CERTIFICATE", "REVOCATION", "WITHDRAWAL", "CANCELLATION",
)


def _categorize(doc_type: str) -> tuple[Optional[str], str]:
    """Map an ACTUAL document-type label to (category_key, human_label).

    Returns (None, "") for rows that should be dropped (reversals, satisfactions,
    releases, or types we don't score).
    """
    t = doc_type.upper()
    if any(tok in t for tok in _EXCLUDE_TOKENS):
        return None, ""
    if "TRUSTEE" in t and "SALE" in t and "DEED" not in t:
        return "foreclosure", "Notice of Trustee Sale"
    if "DEFAULT" in t:
        return "foreclosure", "Notice of Default"
    if "ABSTRACT" in t and "JUDG" in t:
        return "judgment", "Abstract of Judgment"
    if "JUDGMENT" in t:
        return "judgment", "Judgment"
    if "FEDERAL TAX LIEN" in t:
        return "tax_lien", "Federal Tax Lien"
    if "TAX LIEN" in t or "TAX COLLECTOR LIEN" in t:
        return "tax_lien", "Tax Lien"
    if "MECHANIC" in t:
        return "mechanic_lien", "Mechanics Lien"
    if "LIS PENDENS" in t or "PENDENCY" in t or "NOTICE ACTION" in t:
        return "lis_pendens", "Lis Pendens"
    if "PROBATE" in t:
        return "probate", "Probate"
    if "LIEN" in t:
        return "lien", "Lien"
    return None, ""


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Record:
    doc_num:      str           = ""
    doc_type:     str           = ""
    filed:        str           = ""
    cat:          str           = ""
    cat_label:    str           = ""
    owner:        str           = ""
    grantee:      str           = ""
    amount:       Optional[float] = None
    legal:        str           = ""
    prop_address: str           = ""
    prop_city:    str           = ""
    prop_state:   str           = "CA"
    prop_zip:     str           = ""
    mail_address: str           = ""
    mail_city:    str           = ""
    mail_state:   str           = ""
    mail_zip:     str           = ""
    clerk_url:    str           = ""
    flags:        list          = field(default_factory=list)
    score:        int           = 0


# ════════════════════════════════════════════════════════════════════════════
# CLERK PORTAL — Playwright scraper
# ════════════════════════════════════════════════════════════════════════════

async def clerk_scrape(date_from: str, date_to: str) -> list[Record]:
    """
    Drive the Ventura County clerk SPA:
      1. Accept disclaimer
      2. Navigate to Document Type Search (/web/search/DOCSEARCH17S3)
      3. For each doc type: fill dates + select type → Search → parse cards
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("playwright not installed")
        return []

    all_records: list[Record] = []

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
            ignore_https_errors=True,   # portal cert isn't trusted by the runner
        )
        page = await context.new_page()

        # ── Step 1: Accept disclaimer via direct HTTP POST ────────────────────
        # The portal's "I Accept" button fires an AJAX POST to the disclaimer
        # URL which sets a session cookie.  The disclaimerForm element whose
        # action URL the jQuery handler reads is never rendered into the DOM
        # (the jQuery handler POSTs to undefined → current page URL).  Rather
        # than fighting the JS, we replicate the POST with requests directly,
        # then inject the resulting session cookie into the Playwright context.
        log.info("Accepting disclaimer via HTTP POST …")
        try:
            http = requests.Session()
            http.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            })
            # GET first to pick up any initial cookies/CSRF tokens
            http.get(DISCLAIMER_URL, timeout=15, verify=False)
            # POST empty body to accept (mirrors the jQuery AJAX call)
            resp = http.post(DISCLAIMER_URL, timeout=15, verify=False)
            log.info("Disclaimer POST → HTTP %d", resp.status_code)

            # Inject all cookies into the Playwright browser context
            pw_cookies = []
            for cookie in http.cookies:
                pw_cookies.append({
                    "name":   cookie.name,
                    "value":  cookie.value,
                    "domain": "clerkrecorderselfservice.venturacounty.gov",
                    "path":   cookie.path or "/",
                })
            if pw_cookies:
                await context.add_cookies(pw_cookies)
                log.info("Injected %d cookie(s) into Playwright: %s",
                         len(pw_cookies), [c["name"] for c in pw_cookies])
            else:
                log.warning("No cookies received from disclaimer POST — session may not be set")

        except Exception as exc:
            log.warning("Disclaimer HTTP POST error (continuing): %s", exc)

        # ── Confirm session and log search page DOM ────────────────────────────
        log.info("Navigating to search page to confirm session …")
        try:
            await page.goto(SEARCH_URL, timeout=30_000, wait_until="networkidle")
            log.info("Landed on: %s", page.url)

            if "disclaimer" in page.url.lower():
                log.error("Search page still redirecting to disclaimer — session not set")
                await browser.close()
                return all_records

            # Log the actual DOM so we can fix selectors if needed
            search_dom = await page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input')).map(i =>
                    (i.id||i.name||'?')+'[ph='+(i.placeholder||'')+'][type='+i.type+']'
                );
                const selects = Array.from(document.querySelectorAll('select')).map(s => s.id||s.name||'?');
                const buttons = Array.from(document.querySelectorAll('button,input[type=submit],input[type=button]')).map(b =>
                    (b.id||b.name||'?')+'|'+(b.textContent||b.value||'').trim().slice(0,40)
                );
                return { inputs, selects, buttons };
            }""")
            log.info("SEARCH inputs:  %s", search_dom.get('inputs', []))
            log.info("SEARCH selects: %s", search_dom.get('selects', []))
            log.info("SEARCH buttons: %s", search_dom.get('buttons', []))

        except Exception as exc:
            log.warning("Search page navigation error (continuing): %s", exc)

        # ── Step 2: For each search term ─────────────────────────────────────
        for doc_label in SEARCH_TERMS:
            log.info("Scraping: %s", doc_label)
            recs = await _scrape_one_type(page, doc_label, date_from, date_to)
            log.info("  → %d records for %s", len(recs), doc_label)
            all_records.extend(recs)

        await browser.close()

    log.info("Total records collected: %d", len(all_records))
    return all_records


async def _dismiss_popups(page) -> None:
    """Dismiss the portal's session-timeout / interstitial popups if present.

    After a few searches the portal shows a "Your session is about to expire —
    Yes, Continue / No, Start Over" popup that overlays the form. Left up, it
    makes every subsequent search see "form not ready". Click Continue / any
    visible dismiss control so the form is reachable again.
    """
    for sel in (
        "[id*='session-continue']",
        "a[onclick*='closeSelectedItemsWarningPopup']",
        ".ui-popup-active a[data-rel='back']",
    ):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await asyncio.sleep(0.4)
        except Exception:
            continue


async def _prepare_search_form(page, doc_label: str) -> bool:
    """Navigate to the search page and get the form ready, retrying through
    transient failures and session popups. Returns True when the date field is
    present and interactable."""
    from playwright.async_api import TimeoutError as PWTimeout

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            await page.goto(SEARCH_URL, timeout=30_000, wait_until="networkidle")
        except PWTimeout:
            log.warning("[%s] Timeout loading search page (attempt %d/%d)",
                        doc_label, attempt, RETRY_ATTEMPTS)
            await asyncio.sleep(3 * attempt)
            continue

        await _dismiss_popups(page)
        try:
            await page.wait_for_selector(
                "#field_RecordingDateID_DOT_StartDate", state="visible", timeout=10_000
            )
            return True
        except Exception:
            log.warning("[%s] Search form not ready (attempt %d/%d) — retrying",
                        doc_label, attempt, RETRY_ATTEMPTS)
            await _dismiss_popups(page)
            await asyncio.sleep(2 * attempt)

    return False


async def _scrape_one_type(
    page,
    doc_label: str,
    date_from: str,
    date_to: str,
) -> list[Record]:
    records: list[Record] = []

    # Navigate fresh each time (resets any prior chip selection) and make sure
    # the form is actually ready — skip this type only after real retries.
    if not await _prepare_search_form(page, doc_label):
        log.warning("[%s] Search form unavailable after retries — skipping", doc_label)
        return records

    # ── Fill Recording Date Start / End ──────────────────────────────────────
    for sel, val, name in (
        ("#field_RecordingDateID_DOT_StartDate", date_from, "start"),
        ("#field_RecordingDateID_DOT_EndDate",   date_to,   "end"),
    ):
        try:
            loc = page.locator(sel)
            await loc.click()
            await loc.fill(val)
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.3)
        except Exception as e:
            log.warning("[%s] Could not fill date %s: %s", doc_label, name, e)

    # ── Select Document Type (autocomplete chip) ─────────────────────────────
    # #field_selfservice_documentTypes is an autocomplete input. Typing filters
    # a dedicated suggestion list (#field_selfservice_documentTypes-aclist) whose
    # <li class="acItem" data-filtertext="..."> items, when CLICKED, add a chip
    # to the holder. The chip is what the server actually filters on.
    #
    # The previous code did `li:has-text(label).first`, which matched an
    # unrelated <li> elsewhere on the page — no chip was added, so the doc-type
    # filter was silently dropped and every search returned the same date-only
    # (unfiltered) result set. That was the 200→20 collapse.
    chip = page.locator(
        f"#field_selfservice_documentTypes-holder li[data-filtertext='{doc_label}']"
    )
    item = page.locator(
        f"#field_selfservice_documentTypes-aclist li.acItem[data-filtertext='{doc_label}']"
    ).first
    doc_input = page.locator("#field_selfservice_documentTypes")

    # The autocomplete list can be slow to populate — retry a couple of times
    # (re-typing) before giving up on this doc type.
    for sel_attempt in range(1, 3 + 1):
        try:
            await doc_input.click()
            await doc_input.fill("")
            await doc_input.type(doc_label, delay=30)  # real keystrokes drive the autocomplete
            await item.wait_for(state="visible", timeout=8_000)
            await item.click()
            await asyncio.sleep(0.4)
            if await chip.count() > 0:
                break
        except Exception as e:
            log.warning("[%s] Doc-type select attempt %d/3 failed: %s",
                        doc_label, sel_attempt, e)
        await asyncio.sleep(1.0)

    if await chip.count() == 0:
        log.warning("[%s] Doc-type chip not added after retries — skipping", doc_label)
        return records

    # ── Submit search (Enter — no dedicated Search button) ───────────────────
    # Pressing Enter fires the AJAX POST to /web/searchPost/... which stores the
    # search criteria in the server-side session. Results are then served, 100
    # per page, from /web/searchResults/...?page=N — which we fetch below.
    try:
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle", timeout=25_000)
        try:
            await page.wait_for_selector(
                "li.ss-search-row, .no-results, .too-many-results-message",
                timeout=15_000,
            )
        except Exception:
            pass
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning("[%s] Search submit error: %s", doc_label, e)

    # ── Paginate: fetch searchResults?page=N until a page is short/empty ─────
    # page.request shares the browser context's cookies + session, so the
    # server returns the same filtered result set the SPA would render.
    records: list[Record] = []
    raw_rows = 0
    for pg in range(1, MAX_PAGES + 1):
        try:
            resp = await page.request.get(
                f"{RESULTS_URL}?page={pg}&_={int(time.time() * 1000)}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": SEARCH_URL},
                timeout=25_000,
            )
        except Exception as e:
            log.warning("[%s] page %d fetch error: %s", doc_label, pg, e)
            break
        if not resp.ok:
            log.warning("[%s] page %d HTTP %d — stopping", doc_label, pg, resp.status)
            break

        html = await resp.text()
        page_recs = _parse_result_rows(html, doc_label)
        n_rows = html.count('class="ss-search-row')
        raw_rows += n_rows
        records.extend(page_recs)

        if n_rows < 100:          # last page (portal serves 100/page)
            break

    log.info("  Parsed %d lead rows for %s (%d raw rows across pages)",
             len(records), doc_label, raw_rows)
    return records


def _parse_result_rows(html: str, searched_label: str) -> list[Record]:
    """
    Parse the Ventura clerk portal search-result rows.

    Each row is:
        <li class="ss-search-row" data-documentid="DOC..." data-href="/web/document/...">
          <h1>2026000052402 • NOTICE OF DEFAULT</h1>
          <ul class="selfServiceSearchResultColumn">
            <li>Recording Date</li><li>07/02/2026 11:51 AM</li>
          </ul>
          <ul class="selfServiceSearchResultColumn">
            <li>Grantor (2)</li><li>SPENCER PETER S</li><li>SPENCER LINDA L</li>
          </ul>
          <ul class="selfServiceSearchResultColumn"><li>Grantee</li>...</ul>
        </li>

    The actual doc type is read from each row (the portal's "Contains Any" match
    returns related types too); rows whose real type isn't a motivated-seller
    signal are dropped by _categorize().
    """
    from bs4 import BeautifulSoup

    records: list[Record] = []
    soup = BeautifulSoup(html, "lxml")

    for row in soup.select("li.ss-search-row"):
        h1 = row.select_one("h1")
        # h1 is "2026000052402 • NOTICE OF DEFAULT" but the raw fragment carries
        # internal whitespace and the bullet may decode as •, ·, or \x95
        # depending on charset — so collapse space and match the separator
        # generically (any non-alphanumerics between the number and the type).
        header = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)) if h1 else ""
        m = re.match(r"(\d{6,})[\s\W]+([A-Za-z].*?)\s*$", header)
        if not m:
            continue
        doc_num, doc_type = m.group(1), m.group(2).strip()

        cat, cat_label = _categorize(doc_type)
        if cat is None:            # reversal / satisfaction / unscored — skip
            continue

        filed, grantors, grantees = "", [], []
        for ul in row.select("ul.selfServiceSearchResultColumn"):
            lis = ul.find_all("li", recursive=False)
            if not lis:
                continue
            label = lis[0].get_text(" ", strip=True).lower()
            vals = [li.get_text(" ", strip=True) for li in lis[1:] if li.get_text(strip=True)]
            if "recording date" in label:
                if vals:
                    dm = re.search(r"\d{2}/\d{2}/\d{4}", vals[0])
                    filed = dm.group() if dm else vals[0]
            elif label.startswith("grantor"):
                grantors = vals
            elif label.startswith("grantee"):
                grantees = vals

        href = row.get("data-href", "")
        clerk_url = f"{CLERK_BASE}{href}" if href.startswith("/") else href

        records.append(Record(
            doc_num   = doc_num,
            doc_type  = doc_type,
            filed     = filed,
            cat       = cat,
            cat_label = cat_label,
            owner     = grantors[0] if grantors else "",
            grantee   = grantees[0] if grantees else "",
            clerk_url = clerk_url,
        ))

    return records


# ════════════════════════════════════════════════════════════════════════════
# SCORING
# ════════════════════════════════════════════════════════════════════════════

def _is_recent(filed_str: str, days: int = 7) -> bool:
    if not filed_str:
        return False
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(filed_str.strip()[:10], fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - d).days <= days
        except ValueError:
            continue
    return False


def score_record(rec: Record) -> Record:
    score = 30
    flags: list[str] = []
    cat = rec.cat

    cat_scores = {
        "lis_pendens":   ("Lis pendens",          10),
        "foreclosure":   ("Pre-foreclosure",       10),
        "judgment":      ("Judgment lien",         10),
        "tax_lien":      ("Tax lien",              10),
        "mechanic_lien": ("Mechanic lien",         10),
        "lien":          ("Lien",                   5),
        "probate":       ("Probate / estate",      10),
    }
    if cat in cat_scores:
        label, pts = cat_scores[cat]
        flags.append(label)
        score += pts

    if re.search(r"\b(LLC|INC|CORP|LP|LLP|TRUST|ESTATE)\b", rec.owner.upper()):
        flags.append("LLC / corp owner")
        score += 10

    if rec.amount and rec.amount > 100_000:
        score += 15
    elif rec.amount and rec.amount > 50_000:
        score += 10

    if _is_recent(rec.filed):
        flags.append("New this week")
        score += 5

    if rec.prop_address:
        flags.append("Has address")
        score += 5

    rec.flags = flags
    rec.score = min(score, 100)
    return rec


def apply_lp_fc_combo_bonus(records: list[Record]) -> list[Record]:
    fc_owners = {r.owner.upper() for r in records if r.cat == "foreclosure" and r.owner}
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
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "Ventura County Clerk-Recorder",
        "date_range":   {"from": date_from, "to": date_to},
        "total":        len(records),
        "with_address": with_address,
        "records":      [asdict(r) for r in records],
    }
    j = json.dumps(payload, indent=2, default=str)
    RECORDS_JSON.write_text(j, encoding="utf-8")
    DASHBOARD_JSON.write_text(j, encoding="utf-8")
    log.info("Saved %d records to records.json (%d with address)", len(records), with_address)


def save_ghl_csv(records: list[Record]) -> None:
    GHL_COLS = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]

    def split_name(full: str):
        parts = full.strip().split()
        if not parts:
            return "", ""
        return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (parts[0], "")

    with open(GHL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GHL_COLS)
        w.writeheader()
        for r in records:
            first, last = split_name(r.owner)
            w.writerow({
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

    today     = datetime.now(timezone.utc).date()
    start     = today - timedelta(days=LOOKBACK_DAYS)
    date_from = start.strftime("%m/%d/%Y")
    date_to   = today.strftime("%m/%d/%Y")
    log.info("Date range: %s → %s", date_from, date_to)

    # Scrape
    records = await clerk_scrape(date_from, date_to)

    # Score
    log.info("Scoring %d records …", len(records))
    for rec in records:
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

    # Save
    save_records_json(unique, date_from, date_to)
    save_ghl_csv(unique)

    # Summary
    high  = [r for r in unique if r.score >= 70]
    med   = [r for r in unique if 40 <= r.score < 70]
    low   = [r for r in unique if r.score < 40]
    log.info("─" * 52)
    log.info("  Total   : %d", len(unique))
    log.info("  High ≥70: %d", len(high))
    log.info("  Med 40-69: %d", len(med))
    log.info("  Low <40 : %d", len(low))
    if unique:
        log.info("  Top lead: %s [score=%d]", unique[0].owner, unique[0].score)
    log.info("─" * 52)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Ventura County Leads Scraper Results\n\n")
            f.write("| Metric | Value |\n|--------|-------|\n")
            f.write(f"| Total records | {len(unique)} |\n")
            f.write(f"| High score (≥70) | {len(high)} |\n")
            f.write(f"| Medium score (40-69) | {len(med)} |\n")
            f.write(f"| Low score (<40) | {len(low)} |\n")
            f.write(f"| Date range | {date_from} → {date_to} |\n")
            f.write(f"| Generated at | {datetime.utcnow().isoformat()}Z |\n")


if __name__ == "__main__":
    asyncio.run(main())
