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
ENRICH_CACHE_JSON = DATA_DIR  / "enrichment_cache.json"   # owner → parcel, persisted across runs

# ── Config ────────────────────────────────────────────────────────────────────
CLERK_BASE      = "https://clerkrecorderselfservice.venturacounty.gov"
DISCLAIMER_URL  = f"{CLERK_BASE}/web/user/disclaimer"
SEARCH_URL      = f"{CLERK_BASE}/web/search/DOCSEARCH17S3"
RESULTS_URL     = f"{CLERK_BASE}/web/searchResults/DOCSEARCH17S3"

LOOKBACK_DAYS   = int(os.getenv("LOOKBACK_DAYS", "7"))
MAX_PAGES       = 20        # safety cap
RETRY_ATTEMPTS  = 3

# ── ReportAll USA parcel API (address/mailing enrichment by owner name) ───────
# The recorder portal has no property address; we resolve owner name → parcel
# via ReportAll (the dataset behind LandGlide). Set REPORTALL_API_KEY to enable;
# when unset the scraper runs unchanged and just leaves address fields blank.
REPORTALL_API_KEY = os.getenv("REPORTALL_API_KEY", "").strip()
REPORTALL_URL     = "https://reportallusa.com/api/parcels"
REPORTALL_REGION  = os.getenv("REPORTALL_REGION", "Ventura County, CA")
REPORTALL_VERSION = "9"

# ── Ventura County GIS parcel layer (FREE property enrichment by APN) ──────────
# Public ArcGIS REST layer — no key, no quota. Once ReportAll resolves an owner
# to an APN, we pull assessed land/improvement value, last sale price and acreage
# from the county GIS for free (the equity data a paid tier like Regrid charges
# for). Joins the layer's APN10 field to ReportAll's 10-digit parcel_id. Set
# VC_GIS_ENRICH=0 to disable.
VC_GIS_ENRICH     = os.getenv("VC_GIS_ENRICH", "1").strip() not in ("0", "false", "")
VC_GIS_PARCEL_URL = os.getenv(
    "VC_GIS_PARCEL_URL",
    "https://maps.ventura.org/arcgis/rest/services/SDs/Parcels/MapServer/0/query",
)

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
    apn:          str           = ""
    assessed_land:        Optional[float] = None   # county GIS (free)
    assessed_improvement: Optional[float] = None
    assessed_total:       Optional[float] = None
    last_sale_price:      Optional[float] = None
    acreage:              Optional[float] = None
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
# ADDRESS ENRICHMENT — ReportAll USA parcel API (owner name → property/mailing)
# ════════════════════════════════════════════════════════════════════════════

_NAME_STOPWORDS = {
    "LLC", "INC", "CORP", "CO", "LP", "LLP", "TRUST", "TR", "ESTATE", "THE",
    "AND", "ETAL", "ET", "AL", "REVOCABLE", "LIVING", "FAMILY", "A", "AN", "OF",
}


def _name_tokens(name: str) -> set[str]:
    """Significant word tokens of an owner name, minus entity/filler words."""
    toks = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return {t for t in toks if t not in _NAME_STOPWORDS and len(t) > 1}


def _compose_site_address(p: dict) -> str:
    """Prefer the API's ready-made site address; else build from parts."""
    if p.get("address"):
        return p["address"].strip()
    parts = [p.get("addr_number"), p.get("addr_street_prefix"),
             p.get("addr_street_name"), p.get("addr_street_type")]
    return " ".join(x for x in parts if x).strip()


def _reportall_query(session, owner: str) -> tuple[list[dict], Optional[str]]:
    """Query ReportAll for parcels owned by `owner` within the target region.
    Returns (results, error). Retries once on the 429 rate-limit response."""
    params = {
        "client": REPORTALL_API_KEY,
        "v": REPORTALL_VERSION,
        "region": REPORTALL_REGION,
        "owner": owner,
        "rpp": "20",
    }
    last_note = "rate limited"
    for attempt in range(2):
        try:
            r = session.get(REPORTALL_URL, params=params, timeout=25)
        except Exception as e:
            return [], f"request error: {e}"
        if r.status_code == 429:
            # ReportAll returns 429 for BOTH true throttling and a spent parcel
            # quota ("...limit reached"). Distinguish them: retrying won't refill
            # an exhausted quota, so bail immediately with an honest message.
            body = (r.text or "")[:200].replace("\n", " ").strip()
            if "limit" in body.lower():
                return [], "quota/parcel limit reached"
            last_note = "rate limited"
            time.sleep(1.5)
            continue
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {(r.text or '')[:120]}"
        try:
            data = r.json()
        except Exception as e:
            return [], f"bad JSON: {e}"
        if data.get("status") not in (None, "OK"):
            return [], f"api status: {data.get('status')} {data.get('message','')}"
        return data.get("results", []) or [], None
    return [], last_note


def _apply_parcel(rec: Record, p: dict, multi: int) -> None:
    """Fill address/mailing/APN fields on `rec` from a matched parcel `p`."""
    rec.prop_address = _compose_site_address(p)
    rec.prop_city    = (p.get("addr_city") or "").strip()
    rec.prop_zip     = (p.get("addr_zip") or "").strip()
    rec.prop_state   = (p.get("state_abbr") or rec.prop_state or "CA").strip()
    rec.mail_address = (p.get("mail_address1") or "").strip()
    rec.mail_city    = (p.get("mail_placename") or "").strip()
    rec.mail_state   = (p.get("mail_statename") or "").strip()
    rec.mail_zip     = (p.get("mail_zipcode") or "").strip()
    rec.apn          = (p.get("parcel_id") or "").strip()
    if multi > 1:
        rec.flags.append(f"Multiple parcels ({multi}) — verify property")


def _load_enrichment_cache() -> dict:
    """Load the owner → parcel cache persisted across runs. Keyed by the exact
    owner string; each value is {"parcel": dict|None, "n": int, "at": iso}.
    A missing key means "never successfully looked up" (so query it); a present
    key with "parcel": None means a confirmed no-match (so DON'T re-query)."""
    if not ENRICH_CACHE_JSON.exists():
        return {}
    try:
        data = json.loads(ENRICH_CACHE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Enrichment cache unreadable (%s) — starting fresh", e)
        return {}
    owners = data.get("owners") if isinstance(data, dict) else None
    return owners if isinstance(owners, dict) else {}


def _save_enrichment_cache(owners: dict) -> None:
    payload = {
        "region": REPORTALL_REGION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(owners),
        "owners": owners,
    }
    ENRICH_CACHE_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def enrich_addresses(records: list[Record]) -> None:
    """Fill property/mailing address on each record via ReportAll, matching on
    owner name within the region. Results are cached by owner in
    data/enrichment_cache.json so each owner is looked up at most once, ever:
    a daily 7-day-window run re-sees mostly the same owners, and without the
    cache every one is re-billed (which is how a 1,000-record quota vanished in
    days). Only successful lookups are cached — API errors (quota/rate-limit/
    network) are never cached, so they retry on the next run instead of
    poisoning the cache with false no-matches. No-ops when the key is unset."""
    if not REPORTALL_API_KEY:
        log.info("REPORTALL_API_KEY not set — skipping address enrichment")
        return

    cache = _load_enrichment_cache()
    owners = sorted({r.owner.strip() for r in records if r.owner.strip()})
    misses = [o for o in owners if o not in cache]
    log.info("Address enrichment: %d owners (%d cached, %d to look up)",
             len(owners), len(owners) - len(misses), len(misses))

    session = requests.Session()
    session.headers.update({"User-Agent": "Ventura-County-Intel/1.0"})

    looked_up = errors = 0
    for owner in misses:
        results, err = _reportall_query(session, owner)
        if err:
            # Do NOT cache errors — leave the owner uncached so a later run
            # (e.g. after a credit top-up) retries instead of it being stuck.
            log.warning("  ReportAll lookup failed for %r: %s", owner, err)
            errors += 1
            time.sleep(0.1)
            continue

        # Keep only parcels whose owner shares a significant token with the query
        # (guards against a common surname returning unrelated people).
        q = _name_tokens(owner)
        good = [p for p in results if _name_tokens(p.get("owner", "")) & q] if q else results
        cache[owner] = {
            "parcel": good[0] if good else None,
            "n": len(good),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        looked_up += 1
        time.sleep(0.1)     # stay well under the 20 req/s limit

    if looked_up:
        _save_enrichment_cache(cache)

    matched = 0
    for rec in records:
        entry = cache.get(rec.owner.strip())
        if entry and entry.get("parcel"):
            _apply_parcel(rec, entry["parcel"], entry.get("n", 1))
            matched += 1
        elif entry is not None and rec.owner.strip():
            rec.flags.append("No parcel match")
        # entry is None → owner still uncached (only after an API error);
        # leave address blank with no flag so it retries next run.

    log.info("Address enrichment: %d/%d records have a parcel "
             "(%d new lookups, %d errors, cache now %d owners)",
             matched, len(records), looked_up, errors, len(cache))


# ════════════════════════════════════════════════════════════════════════════
# GIS ENRICHMENT — Ventura County public parcel layer (free, by APN)
# ════════════════════════════════════════════════════════════════════════════

def _num(v) -> Optional[float]:
    """Parse a GIS field (declared String but usually numeric) to float, or None."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def enrich_from_gis(records: list[Record]) -> None:
    """Free second-stage enrichment: for records that already have an APN (from
    ReportAll), pull assessed land/improvement value, last sale price and acreage
    from Ventura County's public GIS parcel layer — no key, no quota. Joins the
    layer's APN10 field to ReportAll's 10-digit parcel_id. Best-effort: any GIS
    error just leaves these fields blank and the run continues."""
    if not VC_GIS_ENRICH:
        return

    by_apn: dict[str, list[Record]] = {}
    for r in records:
        apn = (r.apn or "").strip()
        if apn:
            by_apn.setdefault(apn, []).append(r)
    if not by_apn:
        log.info("GIS enrichment: no records have an APN yet — skipping")
        return

    apns = sorted(by_apn)
    log.info("GIS enrichment: querying Ventura parcel layer for %d APNs …", len(apns))
    session = requests.Session()
    session.headers.update({"User-Agent": "Ventura-County-Intel/1.0"})

    enriched = 0
    for i in range(0, len(apns), 100):        # batch to keep each request small
        chunk = apns[i:i + 100]
        in_list = ",".join(f"'{a}'" for a in chunk)
        try:
            resp = session.post(VC_GIS_PARCEL_URL, data={
                "where":          f"APN10 IN ({in_list})",
                "outFields":      "APN10,SITUS,L_V,I_V,SP,ACREAGE",
                "returnGeometry": "false",
                "f":              "json",
            }, timeout=30, verify=False)
            data = resp.json()
        except Exception as e:
            log.warning("  GIS query failed for a batch of %d APNs: %s", len(chunk), e)
            continue
        if "error" in data:
            log.warning("  GIS returned an error: %s", data.get("error"))
            continue

        for feat in data.get("features", []):
            a = feat.get("attributes", {})
            apn = str(a.get("APN10") or "").strip()
            land = _num(a.get("L_V"))
            imp  = _num(a.get("I_V"))
            total = (land or 0) + (imp or 0) if (land is not None or imp is not None) else None
            for rec in by_apn.get(apn, []):
                rec.assessed_land        = land
                rec.assessed_improvement = imp
                rec.assessed_total       = total
                rec.last_sale_price      = _num(a.get("SP"))
                rec.acreage              = _num(a.get("ACREAGE"))
                if not rec.prop_address and a.get("SITUS"):    # backfill situs address
                    rec.prop_address = str(a["SITUS"]).strip()
                enriched += 1

    log.info("GIS enrichment: filled property data on %d records", enriched)


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

    # Equity signal (free from county GIS): more assessed value = more equity to
    # motivate a sale.
    if rec.assessed_total:
        if rec.assessed_total >= 750_000:
            flags.append("High equity (≥$750k assessed)")
            score += 15
        elif rec.assessed_total >= 400_000:
            flags.append("Mid equity (≥$400k assessed)")
            score += 8

    # Absentee owner: mailing zip differs from property zip → owner lives
    # elsewhere, a classic motivated-seller signal. Compare 5-digit zips (both
    # from ReportAll) to avoid street-format false positives.
    if (rec.mail_zip and rec.prop_zip
            and rec.mail_zip.strip()[:5] != rec.prop_zip.strip()[:5]):
        flags.append("Absentee owner")
        score += 10

    # Merge scoring flags with any already set (e.g. enrichment flags like
    # "No parcel match" / "Multiple parcels") rather than clobbering them.
    for f in flags:
        if f not in rec.flags:
            rec.flags.append(f)
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
        "APN",
        "Assessed Value", "Last Sale Price", "Acreage",
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
                "APN":                    r.apn,
                "Assessed Value":         f"{r.assessed_total:.0f}"   if r.assessed_total   else "",
                "Last Sale Price":        f"{r.last_sale_price:.0f}"  if r.last_sale_price  else "",
                "Acreage":                f"{r.acreage:.4f}"          if r.acreage          else "",
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

    # Deduplicate by doc_num FIRST, so we enrich/score each lead only once
    seen: set[str] = set()
    unique: list[Record] = []
    for r in records:
        key = r.doc_num or f"{r.owner}:{r.filed}:{r.doc_type}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    log.info("After dedup: %d unique records", len(unique))

    # Enrich with property / mailing address (ReportAll, by owner name)
    enrich_addresses(unique)

    # Free second stage: assessed value / last sale / acreage from county GIS by APN
    enrich_from_gis(unique)

    # Score (needs addresses in place so the "Has address" bonus applies)
    log.info("Scoring %d records …", len(unique))
    for rec in unique:
        score_record(rec)
    unique = apply_lp_fc_combo_bonus(unique)
    unique.sort(key=lambda r: r.score, reverse=True)

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
