"""
Discovery scanner — finds small-cap insider buys across ALL companies on EDGAR.

Unlike the watchlist scanner, this monitors the entire EDGAR Form 4 feed and
filters by your screening criteria. Designed to surface companies you didn't
know to watch — where nobody else is looking.

Filter pipeline (cheapest checks first to minimise API calls):
  1. General EDGAR feed   (1 request)           → up to 40 recent Form 4s
  2. Skip seen accessions (0 requests)          → only process new filings
  3. Fetch + parse XML    (1 req per new filing) → filter by code, role, value
  4. Fetch Finviz         (1 req per survivor)   → apply cap/float/price filters
  5. Post to Discord      (only if all pass)

In practice step 3 kills most filings (grants, exercises, tiny buys), so
Finviz is called very rarely — roughly 1–3 times per hour on a busy filing day.
"""

import json
import time
from pathlib import Path

import config
import state as st
from alerts import InsiderBuyAlert
from data import edgar
from data.market import calc_squeeze_score, get_borrow_rate, get_market_data
from data.score import calc_insider_score

FILTERS_FILE = Path("filters.json")

# Officer title keywords that count as "exec" role
_EXEC_KEYWORDS = {
    "chief executive", "ceo",
    "chief financial", "cfo",
    "chief operating", "coo",
    "chief technology", "cto",
    "chief revenue", "cro",
    "president",
    "principal executive",
    "principal financial",
}

EDGAR_GENERAL_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&dateb=&owner=include&count=40&output=atom"
)


# ── Filter loading ─────────────────────────────────────────────────────────────

def load_filters() -> dict:
    if FILTERS_FILE.exists():
        return json.loads(FILTERS_FILE.read_text())
    return _default_filters()


def _default_filters() -> dict:
    return {
        "enabled":       True,
        "market_cap":    {"min": 10_000_000, "max": 500_000_000},
        "float":         {"max": 50_000_000},
        "price":         {"min": 1.0, "max": 50.0},
        "min_buy_value": 25_000,
        "roles":         "exec",
    }


# ── Role check ─────────────────────────────────────────────────────────────────

def _is_qualifying_role(details: dict, role_filter: str) -> bool:
    if role_filter == "all":
        return True
    title = (details.get("role") or "").lower()
    return any(kw in title for kw in _EXEC_KEYWORDS)


# ── Market data filter ─────────────────────────────────────────────────────────

def _passes_market_filters(ticker: str, filters: dict) -> tuple[bool, dict]:
    """
    Fetch Finviz data and apply cap/float/price filters.
    Returns (passes, market_data). Only called after EDGAR pre-filter passes.
    """
    market = get_market_data(ticker)
    if not market:
        return False, {}

    cap_cfg   = filters.get("market_cap", {})
    float_cfg = filters.get("float", {})
    price_cfg = filters.get("price", {})

    market_cap = market.get("market_cap")
    float_sh   = market.get("float_shares")
    price      = market.get("price")

    if market_cap is not None:
        if "min" in cap_cfg and market_cap < cap_cfg["min"]:
            return False, market
        if "max" in cap_cfg and market_cap > cap_cfg["max"]:
            return False, market
    if float_sh is not None:
        if "max" in float_cfg and float_sh > float_cfg["max"]:
            return False, market
    if price is not None:
        if "min" in price_cfg and price < price_cfg["min"]:
            return False, market
        if "max" in price_cfg and price > price_cfg["max"]:
            return False, market

    return True, market


# ── Price action check ────────────────────────────────────────────────────────

def _check_price_action(market: dict) -> tuple[list[str], bool]:
    """Thin wrapper around market.price_action_summary used as a gate."""
    from data.market import price_action_summary
    return price_action_summary(market)


# ── EDGAR feed fetch ───────────────────────────────────────────────────────────

def _fetch_feed() -> list[dict]:
    """Parse the general Form 4 Atom feed into a list of {accession, title} dicts."""
    import requests
    resp = requests.get(
        EDGAR_GENERAL_FEED,
        headers=edgar.HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    text = resp.text

    entries = []
    seen: set[str] = set()
    for block in text.split("<entry>")[1:]:
        end   = block.find("</entry>")
        block = block[:end]

        title = _between(block, "<title>", "</title>") or ""
        # Each filing appears twice: once as (Reporting) and once as (Issuer).
        # We only want the Issuer entry — the company whose stock was traded.
        # Check title BEFORE adding accession to seen, otherwise the Issuer
        # entry gets blocked by the Reporting entry which appears first.
        if "(Reporting)" in title:
            continue

        accession = _between(block, "accession-number=", "<")
        if not accession or accession in seen:
            continue
        seen.add(accession)

        company = title.removeprefix("4 - ").split("(")[0].strip()
        link    = _between(block, 'href="', '"') or ""

        # Extract CIK from the archive URL
        cik = ""
        if "/data/" in link:
            parts = link.split("/data/")
            if len(parts) > 1:
                cik = parts[1].split("/")[0]

        entries.append({
            "accession": accession,
            "company":   company,
            "cik":       cik,
            "link":      link,
        })

    return entries


def _between(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i == -1:
        return None
    i += len(start)
    j = text.find(end, i)
    if j == -1:
        return None
    return text[i:j].strip()


# ── Main scan ──────────────────────────────────────────────────────────────────

def run(state: dict) -> None:
    filters = load_filters()
    if not filters.get("enabled", True):
        return

    try:
        feed = _fetch_feed()
    except Exception as e:
        print(f"  [discovery] Feed fetch failed: {e}")
        return

    new = [f for f in feed if not st.is_seen(state, f["accession"])]
    print(f"  [discovery] Feed: {len(feed)} entries, {len(new)} new")

    for entry in new:
        st.mark_seen(state, entry["accession"])

        cik = entry["cik"]
        if not cik:
            continue

        # ── Stage 1: fetch + parse XML ─────────────────────────────────────
        try:
            filings = edgar.fetch_recent_form4s(cik, lookback_days=3)
        except Exception:
            continue

        filing = next((f for f in filings if f["accession"] == entry["accession"]), None)
        if not filing:
            # Filing too old or wrong type; skip
            continue

        try:
            details = edgar.fetch_form4_details(cik, filing["accession"], filing["primary_doc"])
        except Exception:
            continue

        if not details:
            continue

        ticker = details.get("ticker") or ""
        if not ticker:
            continue

        # Filter: role
        if not _is_qualifying_role(details, filters.get("roles", "exec")):
            continue

        # Filter: at least one qualifying transaction (absolute floor only)
        qualifying_txns = [
            t for t in details["transactions"]
            if t["code"] == "P"
            and t["acquired"]
            and t["value"] >= filters.get("min_buy_value", config.MIN_BUY_VALUE_USD)
        ]
        if not qualifying_txns:
            continue

        # ── Stage 2: Finviz market filter ─────────────────────────────────
        try:
            passes, market = _passes_market_filters(ticker, filters)
        except Exception:
            continue

        best_txn = max(qualifying_txns, key=lambda t: t["value"])

        # Director buy special case: flag even if exec filter would block it,
        # when the company is a micro-cap with meaningful short interest.
        is_director_special = False
        if not passes and details.get("is_director") and not details.get("is_officer"):
            cap   = market.get("market_cap")
            short = market.get("short_pct_float")
            if (
                cap is not None and cap <= config.DIRECTOR_BUY_MAX_CAP
                and short is not None and short >= config.DIRECTOR_BUY_MIN_SHORT
            ):
                passes = True
                is_director_special = True

        if not passes:
            continue

        # ── Significance score ─────────────────────────────────────────────
        # Gate on composite score — replaces crude price-action-only check.
        score, sig_factors = calc_insider_score(best_txn, details, market)
        if score < config.MIN_SIGNIFICANCE_SCORE and not is_director_special:
            print(f"  [discovery] {ticker} score {score} below threshold — skipped")
            continue

        # Enrich with squeeze context (market data already fetched, no extra API call)
        borrow_rate = None
        try:
            borrow_rate = get_borrow_rate(ticker)
        except Exception:
            pass

        squeeze_score, squeeze_factors = calc_squeeze_score(market, borrow_rate)

        print(
            f"  [discovery] {'🎯' if is_director_special else '✓'} {ticker} — "
            f"score {score} · {details['role']} bought ${best_txn['value']:,.0f}"
        )

        alert = InsiderBuyAlert(
            ticker       = ticker,
            company      = details["company"],
            filing       = {
                "accession": entry["accession"],
                "filed":     entry.get("filed", "?"),
                "link":      entry["link"],
            },
            details      = details,
            txn          = best_txn,
            score        = score,
            sig_factors  = sig_factors,
            market       = market,
            squeeze_score   = squeeze_score if squeeze_score >= 30 else None,
            squeeze_factors = squeeze_factors,
            borrow_rate     = borrow_rate,
            is_director_special = is_director_special,
            is_discovery        = True,
        )
        alert.post()
        time.sleep(1)
