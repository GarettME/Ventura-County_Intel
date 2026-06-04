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

LOOKBACK_DAYS   = int(os.getenv("LOOKBACK_DAYS", "7"))
MAX_PAGES       = 20        # safety cap
RETRY_ATTEMPTS  = 3

# Exact document type labels as they appear in the portal dropdown
# mapped to (category_key, human_label)
DOC_TYPE_MAP: dict[str, tuple[str, str]] = {
    "LIS PENDENS":              ("lis_pendens",   "Lis Pendens"),
    "NOTICE OF DEFAULT":        ("foreclosure",   "Notice of Default"),
    "NOTICE OF TRUSTEE SALE":   ("foreclosure",   "Notice of Trustee Sale"),
    "JUDGMENT":                 ("judgment",      "Judgment"),
    "ABSTRACT OF JUDGMENT":     ("judgment",      "Abstract of Judgment"),
    "TAX LIEN":                 ("tax_lien",      "Tax Lien"),
    "FEDERAL TAX LIEN":         ("tax_lien",      "Federal Tax Lien"),
    "STATE TAX LIEN":           ("tax_lien",      "State Tax Lien"),
    "MECHANICS LIEN":           ("mechanic_lien", "Mechanics Lien"),
    "LIEN":                     ("lien",          "Lien"),
    "PROBATE":                  ("probate",       "Probate"),
}


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

        # ── Step 2: For each doc type ────────────────────────────────────────
        for doc_label, (cat, cat_label) in DOC_TYPE_MAP.items():
            log.info("Scraping: %s", doc_label)
            recs = await _scrape_one_type(
                page, doc_label, cat, cat_label, date_from, date_to
            )
            log.info("  → %d records for %s", len(recs), doc_label)
            all_records.extend(recs)

        await browser.close()

    log.info("Total records collected: %d", len(all_records))
    return all_records


async def _scrape_one_type(
    page,
    doc_label: str,
    cat: str,
    cat_label: str,
    date_from: str,
    date_to: str,
) -> list[Record]:
    from playwright.async_api import TimeoutError as PWTimeout

    records: list[Record] = []

    # Navigate fresh to the search page each time
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            await page.goto(SEARCH_URL, timeout=30_000, wait_until="networkidle")
            break
        except PWTimeout:
            log.warning("Timeout loading search page (attempt %d/%d)", attempt, RETRY_ATTEMPTS)
            if attempt == RETRY_ATTEMPTS:
                return records
            await asyncio.sleep(3 * attempt)

    # ── Wait for search form to be ready ────────────────────────────────────
    try:
        await page.wait_for_selector("#field_RecordingDateID_DOT_StartDate", timeout=10_000)
    except Exception:
        log.warning("[%s] Search form not ready — skipping", doc_label)
        return records

    # ── Fill Recording Date Start ────────────────────────────────────────────
    try:
        date_start = page.locator("#field_RecordingDateID_DOT_StartDate")
        await date_start.click()
        await date_start.fill(date_from)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.3)
    except Exception as e:
        log.warning("[%s] Could not fill date start: %s", doc_label, e)

    # ── Fill Recording Date End ──────────────────────────────────────────────
    try:
        date_end = page.locator("#field_RecordingDateID_DOT_EndDate")
        await date_end.click()
        await date_end.fill(date_to)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.3)
    except Exception as e:
        log.warning("[%s] Could not fill date end: %s", doc_label, e)

    # ── Select Document Type ─────────────────────────────────────────────────
    # The DOM shows: field_selfservice_documentTypes — a text input that
    # filters a multi-select list. Type the doc label, wait for the dropdown
    # item to appear, then click it.
    try:
        doc_input = page.locator("#field_selfservice_documentTypes")
        await doc_input.click()
        await doc_input.fill(doc_label)
        await asyncio.sleep(1.0)

        # The filter shows a dropdown; items appear in an adjacent list
        # Try multiple selector patterns for the dropdown items
        item_found = False
        for item_sel in [
            f"li:has-text('{doc_label}')",
            f"[role='option']:has-text('{doc_label}')",
            f"[class*='item']:has-text('{doc_label}')",
            f"[class*='option']:has-text('{doc_label}')",
            f"span:has-text('{doc_label}')",
        ]:
            try:
                item = page.locator(item_sel).first
                if await item.is_visible(timeout=2_000):
                    await item.click()
                    log.debug("[%s] Selected via %s", doc_label, item_sel)
                    item_found = True
                    break
            except Exception:
                continue

        if not item_found:
            # Fallback: press Enter to accept the typed value
            await page.keyboard.press("Enter")
            log.debug("[%s] Pressed Enter to select doc type", doc_label)

        await asyncio.sleep(0.5)

    except Exception as e:
        log.warning("[%s] Could not select doc type: %s", doc_label, e)
        return records

    # ── Submit search (Enter key — no dedicated Search button in DOM) ─────────
    try:
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle", timeout=25_000)
        await asyncio.sleep(1.0)
        log.debug("[%s] Search submitted", doc_label)
    except Exception as e:
        log.warning("[%s] Search submit error: %s", doc_label, e)

    # ── Paginate and parse ───────────────────────────────────────────────────
    # ── DEBUG: log result area structure on first doc type ───────────────────
    if doc_label == list(DOC_TYPE_MAP.keys())[0]:
        result_dom = await page.evaluate("""() => {
            const text = document.body.innerText.slice(0, 500);
            const divs = Array.from(document.querySelectorAll('div[class]')).slice(0,20).map(d=>d.className.slice(0,60));
            const lis = Array.from(document.querySelectorAll('li[class]')).slice(0,10).map(l=>l.className.slice(0,60));
            return { body_text: text, div_classes: divs, li_classes: lis };
        }""")
        log.info("RESULT body text: %s", result_dom.get('body_text','')[:300])
        log.info("RESULT div classes: %s", result_dom.get('div_classes',[])[:10])
        log.info("RESULT li classes: %s", result_dom.get('li_classes',[])[:5])

    page_num = 1
    while page_num <= MAX_PAGES:
        html = await page.content()
        page_recs = _parse_result_cards(html, doc_label, cat, cat_label)

        if not page_recs:
            log.debug("No cards on page %d for %s", page_num, doc_label)
            break

        records.extend(page_recs)
        log.info("  Page %d: %d records (running total: %d)", page_num, len(page_recs), len(records))

        # Check for Next page button
        next_found = False
        for next_sel in [
            "button:has-text('Next')", "a:has-text('Next')",
            "[aria-label='Next page']", ".pagination-next",
            "button:has-text('›')", "a:has-text('›')",
        ]:
            try:
                btn = page.locator(next_sel).first
                if await btn.is_visible(timeout=1_500) and await btn.is_enabled():
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    await asyncio.sleep(0.8)
                    next_found = True
                    break
            except Exception:
                continue

        if not next_found:
            break
        page_num += 1

    return records


def _parse_result_cards(html: str, doc_label: str, cat: str, cat_label: str) -> list[Record]:
    """
    Parse the card-based results from the Ventura clerk portal.

    Each result card contains:
      - Document number + type  (e.g. "2026000033864 • NOTICE OF DEFAULT")
      - Recording Date          (e.g. "05/08/2026 09:49 AM")
      - Grantor(s)              (owner/seller)
      - Grantee(s)              (buyer/lender)
    """
    from bs4 import BeautifulSoup

    records: list[Record] = []
    soup = BeautifulSoup(html, "lxml")

    # The results render as a list of card-like divs.
    # Each card has the doc number as a prominent text element.
    # We look for the repeating card container.

    # Strategy: find all doc number patterns (20-digit number + bullet + doc type)
    # then walk up to the card root and extract fields.
    doc_num_pattern = re.compile(r"20\d{10,}")

    # Try to find card containers — common patterns in county SPAs
    cards = (
        soup.find_all("div", class_=re.compile(r"result|record|card|item|row", re.I))
        or soup.find_all("li", class_=re.compile(r"result|record|item", re.I))
    )

    # Filter to only cards that contain a doc number
    cards = [c for c in cards if doc_num_pattern.search(c.get_text())]

    # Deduplicate cards (nested divs may both match)
    seen_texts: set[str] = set()
    unique_cards = []
    for card in cards:
        txt = card.get_text(" ", strip=True)[:80]
        if txt not in seen_texts:
            seen_texts.add(txt)
            unique_cards.append(card)

    log.debug("Found %d result cards for %s", len(unique_cards), doc_label)

    for card in unique_cards:
        text = card.get_text(" ", strip=True)

        # Doc number
        m = doc_num_pattern.search(text)
        doc_num = m.group() if m else ""

        # Recording date — "mm/dd/yyyy hh:mm AM/PM"
        date_m = re.search(r"\d{2}/\d{2}/\d{4}", text)
        filed = date_m.group() if date_m else ""

        # Grantor — label appears as "Grantor" or "Grantor (N)"
        grantor_m = re.search(r"Grantor(?:\s*\(\d+\))?\s+([A-Z][A-Z\s,]+?)(?=Grantee|$)", text)
        owner = grantor_m.group(1).strip() if grantor_m else ""

        # Grantee
        grantee_m = re.search(r"Grantee(?:\s*\(\d+\))?\s+([A-Z][A-Z\s,]+?)(?=Grantor|Recording|$)", text)
        grantee = grantee_m.group(1).strip() if grantee_m else ""

        # Detail link
        link = card.find("a", href=True)
        clerk_url = f"{CLERK_BASE}{link['href']}" if link and link["href"].startswith("/") else (link["href"] if link else "")

        if not doc_num:
            continue

        rec = Record(
            doc_num   = doc_num,
            doc_type  = doc_label,
            filed     = filed,
            cat       = cat,
            cat_label = cat_label,
            owner     = owner,
            grantee   = grantee,
            clerk_url = clerk_url,
        )
        records.append(rec)

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
