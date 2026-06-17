"""
Market data via Finviz (scraping) and iborrowdesk.com.

Finviz updates stats in near-real-time during market hours — short float %,
float size, insider ownership, avg volume, and days to cover all come from here.

iborrowdesk sources directly from Interactive Brokers' securities lending desk
and is the closest free equivalent to calling the IBKR borrow rate API directly.
It updates throughout the trading day.

Note on short interest: no provider has truly intraday short interest data.
FINRA reports bi-weekly; paid services (Ortex, S3) interpolate between prints.
Finviz reflects the latest FINRA data, so it's as good as any free source.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

_FINVIZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/",
}

_IBORROW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

_last_finviz_req = 0.0


def _finviz_get(ticker: str) -> str:
    """Fetch Finviz quote page with basic rate limiting (1 req/s)."""
    global _last_finviz_req
    elapsed = time.time() - _last_finviz_req
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    url = f"https://finviz.com/quote.ashx?t={ticker}&ty=c&ta=1&p=d"
    resp = requests.get(url, headers=_FINVIZ_HEADERS, timeout=15)
    resp.raise_for_status()
    _last_finviz_req = time.time()
    return resp.text


def _parse_finviz_stats(html: str) -> dict[str, str]:
    """Extract all label→value pairs from Finviz's snapshot stats table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="snapshot-table2")
    if not table:
        return {}
    cells = table.find_all("td")
    stats: dict[str, str] = {}
    for i in range(0, len(cells) - 1, 2):
        label = cells[i].get_text(strip=True)
        value = cells[i + 1].get_text(strip=True)
        stats[label] = value
    return stats


def _pct(s: str | None) -> float | None:
    """'12.50%' → 0.125, '-' → None"""
    if not s or s == "-":
        return None
    try:
        return float(s.rstrip("%")) / 100
    except ValueError:
        return None


def _shares(s: str | None) -> float | None:
    """'1.23B' → 1_230_000_000, '456.78M' → 456_780_000, '-' → None"""
    if not s or s == "-":
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
    if not s or s == "-":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def get_market_data(ticker: str) -> dict:
    """
    Pull key squeeze-relevant metrics from Finviz.

    Returns dict (all values may be None if unavailable):
      short_pct_float    — short interest as fraction of float (e.g. 0.30)
      short_ratio        — days to cover
      float_shares       — tradeable float (int)
      held_pct_insiders  — fraction of shares held by insiders
      avg_volume         — 3-month avg daily volume
      price              — latest price
    """
    try:
        html = _finviz_get(ticker)
        stats = _parse_finviz_stats(html)
        return {
            # Short / squeeze
            "short_pct_float":   _pct(stats.get("Short Float")),
            "short_ratio":       _num(stats.get("Short Ratio")),
            "float_shares":      _shares(stats.get("Shs Float")),
            "held_pct_insiders": _pct(stats.get("Insider Own")),
            "avg_volume":        _shares(stats.get("Avg Volume")),
            # Price / cap
            "price":             _num(stats.get("Price")),
            "market_cap":        _shares(stats.get("Market Cap")),
            "sector":            stats.get("Sector", ""),
            "industry":          stats.get("Industry", ""),
            # Price action
            "rel_volume":        _num(stats.get("Rel Volume")),
            "volume":            _shares(stats.get("Volume")),
            "perf_week":         _pct(stats.get("Perf Week")),
            "perf_month":        _pct(stats.get("Perf Month")),
            "perf_quarter":      _pct(stats.get("Perf Quarter")),
            "week_52_high":      _num(stats.get("52W High")),
            "week_52_low":       _num(stats.get("52W Low")),
            "atr":               _num(stats.get("ATR")),
        }
    except Exception as e:
        print(f"  [market] Finviz fetch failed for {ticker}: {e}")
        return {}


def get_borrow_rate(ticker: str) -> float | None:
    """
    Fetch current annualized borrow fee from iborrowdesk.com's JSON API.
    Returns decimal fraction (e.g. 0.25 = 25%) or None.

    iborrowdesk sources directly from Interactive Brokers' securities lending
    desk — same data as the IBKR API, updated throughout the trading day.
    The API returns 'fee' in percentage points (e.g. 5.0 = 5.0%),
    so we divide by 100 to return a fraction.
    """
    try:
        url  = f"https://iborrowdesk.com/api/ticker/{ticker}"
        resp = requests.get(url, headers=_IBORROW_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", [])
        if not daily:
            return None
        # Daily list is oldest-first; take the last entry for the current rate
        latest = daily[-1]
        fee_pct = latest.get("fee")
        if fee_pct is None:
            return None
        return float(fee_pct) / 100  # convert % → fraction
    except Exception as e:
        print(f"  [market] iborrowdesk fetch failed for {ticker}: {e}")
    return None


def calc_squeeze_score(market: dict, borrow_rate: float | None) -> tuple[int, list[str]]:
    """
    Compute a squeeze readiness score (0–100) and human-readable factors.

    Score breakdown:
      +20  short interest > 20% of float
      +20  short interest > 40% (stacks)
      +15  days to cover  > 5
      +15  days to cover  > 10 (stacks)
      +10  float < 50M shares
      +10  insider ownership > 10% of float
      +10  borrow rate > 5%
    """
    from config import (
        SCORE_SHORT_PCT_HIGH, SCORE_SHORT_PCT_EXTREME,
        SCORE_DTC_HIGH, SCORE_DTC_EXTREME,
        SCORE_SMALL_FLOAT, SCORE_INSIDER_FLOAT, SCORE_HIGH_BORROW,
        SHORT_PCT_HIGH, SHORT_PCT_EXTREME,
        DTC_HIGH, DTC_EXTREME,
        FLOAT_SMALL_SHARES, INSIDER_FLOAT_PCT, BORROW_HIGH_PCT,
    )

    score = 0
    factors: list[str] = []

    short_pct    = market.get("short_pct_float")
    dtc          = market.get("short_ratio")
    float_sh     = market.get("float_shares")
    insider_pct  = market.get("held_pct_insiders")

    if short_pct is not None:
        if short_pct >= SHORT_PCT_HIGH:
            score += SCORE_SHORT_PCT_HIGH
            factors.append(f"Short float {short_pct:.0%} > {SHORT_PCT_HIGH:.0%}")
        if short_pct >= SHORT_PCT_EXTREME:
            score += SCORE_SHORT_PCT_EXTREME
            factors.append(f"Extreme short float > {SHORT_PCT_EXTREME:.0%}")

    if dtc is not None:
        if dtc >= DTC_HIGH:
            score += SCORE_DTC_HIGH
            factors.append(f"Days to cover {dtc:.1f} > {DTC_HIGH}")
        if dtc >= DTC_EXTREME:
            score += SCORE_DTC_EXTREME
            factors.append(f"Extreme DTC > {DTC_EXTREME}")

    if float_sh is not None and float_sh < FLOAT_SMALL_SHARES:
        score += SCORE_SMALL_FLOAT
        factors.append(f"Small float {float_sh:,.0f} < {FLOAT_SMALL_SHARES:,}")

    if insider_pct is not None and insider_pct >= INSIDER_FLOAT_PCT:
        score += SCORE_INSIDER_FLOAT
        factors.append(f"Insiders own {insider_pct:.0%} > {INSIDER_FLOAT_PCT:.0%}")

    if borrow_rate is not None and borrow_rate >= BORROW_HIGH_PCT:
        score += SCORE_HIGH_BORROW
        factors.append(f"Borrow rate {borrow_rate:.1%} > {BORROW_HIGH_PCT:.0%}")

    return score, factors


def price_action_summary(market: dict) -> tuple[list[str], bool]:
    """
    Summarise notable price action signals for an embed field.

    Returns (lines, is_notable) where is_notable=True means at least one
    strong signal is present (high rel volume, buying into weakness, near 52W low).
    """
    from config import REL_VOL_NOTABLE, PERF_WEEK_WEAK, PERF_MONTH_WEAK, PCT_FROM_52W_LOW

    lines: list[str] = []
    notable = False

    rel_vol     = market.get("rel_volume")
    perf_week   = market.get("perf_week")
    perf_month  = market.get("perf_month")
    perf_qtr    = market.get("perf_quarter")
    price       = market.get("price")
    low_52      = market.get("week_52_low")
    high_52     = market.get("week_52_high")

    if rel_vol is not None:
        tag = " 🔥" if rel_vol >= REL_VOL_NOTABLE else ""
        lines.append(f"Rel vol: **{rel_vol:.1f}x**{tag}")
        if rel_vol >= REL_VOL_NOTABLE:
            notable = True

    if perf_week is not None:
        lines.append(f"Week: **{perf_week:+.1%}**")
        if perf_week <= PERF_WEEK_WEAK:
            notable = True

    if perf_month is not None:
        lines.append(f"Month: **{perf_month:+.1%}**")
        if perf_month <= PERF_MONTH_WEAK:
            notable = True

    if perf_qtr is not None:
        lines.append(f"Quarter: **{perf_qtr:+.1%}**")

    if price and low_52 and high_52:
        pct_from_low  = (price - low_52) / low_52 if low_52 else None
        pct_from_high = (high_52 - price) / high_52 if high_52 else None
        if pct_from_low is not None:
            tag = " (near 52W low)" if pct_from_low <= PCT_FROM_52W_LOW else ""
            lines.append(f"52W: ${low_52:.2f} – ${high_52:.2f}{tag}")
            if pct_from_low <= PCT_FROM_52W_LOW:
                notable = True

    return lines, notable
