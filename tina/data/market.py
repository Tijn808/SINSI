"""Fetch current stock prices and market caps from Finviz."""

import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Allow 'from core.market_client import ...' when running from tina/ directory
_root = Path(__file__).resolve().parent.parent.parent  # tina/data/ → tina/ → BOT/
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from core.market_client import get_many as _core_get_many

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _parse_val(s: str | None) -> float | None:
    """'1.23B' → 1_230_000_000, '456.78M' → 456_780_000, '-' → None"""
    if not s or s.strip() == "-":
        return None
    s = s.strip()
    mults = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    try:
        if s[-1].upper() in mults:
            return float(s[:-1]) * mults[s[-1].upper()]
        return float(s.replace(",", ""))
    except (ValueError, IndexError):
        return None


def _finviz_stats(ticker: str) -> dict[str, str]:
    r = requests.get(
        f"https://finviz.com/quote.ashx?t={ticker}&ty=c&ta=1&p=d",
        headers=_HEADERS, timeout=10,
    )
    r.raise_for_status()
    soup  = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="snapshot-table2")
    if not table:
        return {}
    cells = table.find_all("td")
    return {
        cells[i].get_text(strip=True): cells[i + 1].get_text(strip=True)
        for i in range(0, len(cells) - 1, 2)
    }


def get_price(ticker: str) -> float | None:
    try:
        r = requests.get(
            f"https://finviz.com/quote.ashx?t={ticker}",
            headers=_HEADERS,
            timeout=10,
        )
        m = re.search(r'"price"[^>]*?>([\d.]+)<', r.text)
        if not m:
            m = re.search(r'class="snapshot-td2"[^>]*>\s*([\d.]+)\s*</td>', r.text)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def get_prices_bulk(tickers: list[str], delay: float = 0.12) -> dict[str, float]:
    """Fetch prices for multiple tickers. Returns {ticker: price}."""
    prices = {}
    for ticker in tickers:
        price = get_price(ticker)
        if price:
            prices[ticker] = price
        time.sleep(delay)
    return prices


def get_market_caps(tickers: list[str], limit: int = 80, delay: float = 0.15) -> dict[str, float | None]:
    """Fetch market caps for up to `limit` tickers. Returns {ticker: market_cap_or_None}."""
    result = {}
    for ticker in tickers[:limit]:
        try:
            stats = _finviz_stats(ticker)
            result[ticker] = _parse_val(stats.get("Market Cap"))
        except Exception:
            result[ticker] = None
        time.sleep(delay)
    return result


def get_ticker_screen(
    tickers: list[str],
    limit: int = 10_000,
    need_sector: bool = False,
) -> dict[str, dict]:
    """Fetch market cap (+ optionally sector/industry) for many tickers.

    Delegates to core.market_client which batches via the Finviz screener
    (20 tickers per HTTP request instead of 1) and caches results with per-field
    TTLs (market_cap: 4h, sector: permanent).  First call for N tickers takes
    roughly N/20/3 × 1s wall time; subsequent calls within the TTL are instant.
    Returns {ticker: {market_cap, sector, industry}}.
    """
    fields = ["market_cap", "sector", "industry"] if need_sector else ["market_cap"]
    raw    = _core_get_many(tickers[:limit], fields=fields)
    return {
        ticker: {
            "market_cap": data.get("market_cap"),
            "sector":     data.get("sector", "") if need_sector else "",
            "industry":   data.get("industry", "") if need_sector else "",
        }
        for ticker, data in raw.items()
    }
