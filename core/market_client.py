"""Shared market data client for SINSI and TINA.

Fetches data via the Finviz screener — one request returns 20 rows instead of
1, so 1300 tickers takes ~15 requests-worth of wall time instead of 1300.
Results are cached per-field with TTLs tuned to how fast each metric changes:
price refreshes every minute, short float every 12 hours (FINRA data is
bi-weekly), sector/industry are essentially permanent.

API:
    from core.market_client import get_many

    data = get_many(["AAPL", "TSLA"], fields=["market_cap", "price", "sector"])
    # → {"AAPL": {"market_cap": 3.1e12, "price": 212.0, "sector": "Technology"}, ...}
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/",
}

# Per-field cache TTLs in seconds.
# Short/float come from FINRA bi-weekly prints — no need to refresh more than twice a day.
_TTL: dict[str, float] = {
    "price":             60,
    "rel_volume":        60,
    "volume":            60,
    "market_cap":        4 * 3_600,
    "avg_volume":        4 * 3_600,
    "perf_week":         3_600,
    "perf_month":        3_600,
    "perf_quarter":      3_600,
    "atr":               3_600,
    "week_52_high":      24 * 3_600,
    "week_52_low":       24 * 3_600,
    "short_pct_float":   12 * 3_600,
    "short_ratio":       12 * 3_600,
    "float_shares":      24 * 3_600,
    "held_pct_insiders": 24 * 3_600,
    "sector":            float("inf"),
    "industry":          float("inf"),
}

# Fields this client can return via the screener
SCREENER_FIELDS = frozenset(["market_cap", "price", "sector", "industry"])

_PAGE_SIZE  = 20  # Finviz screener rows per page
_CHUNK_SIZE = 50  # tickers per screener URL (keeps query strings short)
_WORKERS    = 3   # parallel Finviz requests (tested safe against rate limiting)

# Thread-safe cache: {ticker → {field → (value, expires_at)}}
_cache: dict[str, dict[str, tuple]] = {}
_lock  = threading.Lock()


# ── Value parsers ─────────────────────────────────────────────────────────────

def _parse_cap(s: str | None) -> float | None:
    """'2.5B' → 2_500_000_000, '-' → None."""
    if not s or s.strip() in ("-", ""):
        return None
    s = s.strip()
    mults = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    try:
        if s[-1].upper() in mults:
            return float(s[:-1]) * mults[s[-1].upper()]
        return float(s.replace(",", ""))
    except (ValueError, IndexError):
        return None


def _num(s: str | None) -> float | None:
    if not s or s.strip() in ("-", ""):
        return None
    try:
        return float(s.strip().replace(",", ""))
    except ValueError:
        return None


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_get(ticker: str, field: str) -> tuple[bool, object]:
    with _lock:
        entry = _cache.get(ticker, {}).get(field)
        if entry is None:
            return False, None
        value, expires_at = entry
        if expires_at != float("inf") and time.time() > expires_at:
            return False, None
        return True, value


def _cache_set(ticker: str, field: str, value: object) -> None:
    ttl = _TTL.get(field, 300)
    expires = float("inf") if ttl == float("inf") else time.time() + ttl
    with _lock:
        _cache.setdefault(ticker, {})[field] = (value, expires)


# ── Finviz screener fetch + parse ─────────────────────────────────────────────

def _parse_screener_html(html: str) -> dict[str, dict]:
    """Parse Finviz screener HTML (v=111 overview view) → {ticker: {field: value}}."""
    soup = BeautifulSoup(html, "html.parser")

    # Data rows carry one of these classes in the Finviz screener table
    data_rows = [
        tr for tr in soup.find_all("tr")
        if any("screener-body-table" in cls for cls in (tr.get("class") or []))
    ]
    if not data_rows:
        return {}

    # Header row immediately precedes the first data row in the same table
    header_row = None
    for prev_tr in data_rows[0].find_previous_siblings("tr"):
        cells = prev_tr.find_all("td")
        texts = [c.get_text(strip=True).lower() for c in cells]
        if "ticker" in texts:
            header_row = prev_tr
            break
    if not header_row:
        return {}

    headers   = [c.get_text(strip=True).lower() for c in header_row.find_all("td")]
    label_map = {
        "ticker":     "ticker",
        "sector":     "sector",
        "industry":   "industry",
        "market cap": "market_cap",
        "price":      "price",
    }
    col: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h in label_map:
            col[label_map[h]] = i

    if "ticker" not in col:
        return {}

    result: dict[str, dict] = {}
    for row in data_rows:
        cells = row.find_all("td")
        if len(cells) <= col["ticker"]:
            continue
        ticker = cells[col["ticker"]].get_text(strip=True)
        if not ticker:
            continue

        def _get(field: str) -> str | None:
            idx = col.get(field)
            return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else None

        result[ticker] = {
            "market_cap": _parse_cap(_get("market_cap")),
            "price":      _num(_get("price")),
            "sector":     _get("sector") or "",
            "industry":   _get("industry") or "",
        }

    return result


def _fetch_page(chunk: list[str], offset: int) -> dict[str, dict]:
    """One HTTP request: screener page for `chunk` tickers at row offset `offset`."""
    url = (
        f"https://finviz.com/screener.ashx"
        f"?v=111&t={','.join(chunk)}&r={offset}"
    )
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return _parse_screener_html(resp.text)


def _screener_fetch(tickers: list[str]) -> dict[str, dict]:
    """Batch-fetch screener data for all tickers.

    Splits into chunks of _CHUNK_SIZE, generates (chunk, offset) pairs for each
    page within each chunk, then runs all page requests in parallel.
    """
    # Build all (chunk, offset) tasks up front so ThreadPoolExecutor can spread them
    tasks: list[tuple[list[str], int]] = []
    for i in range(0, len(tickers), _CHUNK_SIZE):
        chunk   = tickers[i: i + _CHUNK_SIZE]
        n_pages = -(-len(chunk) // _PAGE_SIZE)  # ceil division
        for p in range(n_pages):
            tasks.append((chunk, 1 + p * _PAGE_SIZE))

    combined: dict[str, dict] = {}

    def _run(args: tuple[list[str], int]) -> dict[str, dict]:
        chunk, offset = args
        try:
            return _fetch_page(chunk, offset)
        except Exception as e:
            print(f"  [market] screener page r={offset} failed: {e}")
            return {}

    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        for page_data in ex.map(_run, tasks):
            combined.update(page_data)

    return combined


# ── Public API ────────────────────────────────────────────────────────────────

def get_many(
    tickers: list[str],
    fields: list[str] | None = None,
) -> dict[str, dict]:
    """Return market data for many tickers, sourcing from cache where possible.

    fields: any subset of SCREENER_FIELDS (market_cap, price, sector, industry).
            Defaults to all four.
    Returns {ticker: {field: value_or_None}} for every ticker in the input list.
    """
    if not tickers:
        return {}
    fields = list(fields or SCREENER_FIELDS)
    screen_fields = [f for f in fields if f in SCREENER_FIELDS]
    if not screen_fields:
        return {t: {} for t in tickers}

    # Which tickers have at least one stale/missing screener field?
    need_fetch = [
        t for t in tickers
        if any(not _cache_get(t, f)[0] for f in screen_fields)
    ]

    if need_fetch:
        fetched = _screener_fetch(need_fetch)
        for ticker in need_fetch:
            data = fetched.get(ticker, {})
            for field in screen_fields:
                _cache_set(ticker, field, data.get(field))  # caches None for misses too

    return {
        t: {f: _cache_get(t, f)[1] for f in screen_fields}
        for t in tickers
    }
