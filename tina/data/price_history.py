"""Historical OHLC via Yahoo Finance chart API (no auth required).

Used for cost-basis overlay: given a 13F quarter period, fetch the price
range during that quarter so we can compare against today's price.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def quarter_bounds(period: str) -> tuple[date, date]:
    """Parse a 13F period string ('2026-03-31') into (quarter_start, quarter_end).

    13F periods are always quarter-end dates:
      Mar 31 → Q1 (Jan–Mar)
      Jun 30 → Q2 (Apr–Jun)
      Sep 30 → Q3 (Jul–Sep)
      Dec 31 → Q4 (Oct–Dec)
    """
    end   = date.fromisoformat(period)
    month = end.month - 2
    year  = end.year
    if month <= 0:
        month += 12
        year  -= 1
    start = date(year, month, 1)
    return start, end


def get_ohlc(ticker: str, start: date, end: date) -> dict | None:
    """Fetch daily OHLC for ticker between start and end (inclusive).

    Returns:
      low         — quarter low
      high        — quarter high
      avg_close   — mean closing price over the period
      close_start — closing price on (or near) the first trading day
      close_end   — closing price on (or near) the last trading day
      n_days      — number of trading days in the range
    Returns None if the API call fails or no data is available.
    """
    start_ts = int(time.mktime(start.timetuple()))
    end_ts   = int(time.mktime((end + timedelta(days=1)).timetuple()))

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={start_ts}&period2={end_ts}"
    )
    try:
        r = requests.get(url, headers=_HEADERS, timeout=12)
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result")
        if not result:
            return None

        q     = result[0]["indicators"]["quote"][0]
        highs  = [h for h in q.get("high",  []) if h is not None]
        lows   = [l for l in q.get("low",   []) if l is not None]
        closes = [c for c in q.get("close", []) if c is not None]

        if not closes:
            return None

        return {
            "low":         min(lows),
            "high":        max(highs),
            "avg_close":   sum(closes) / len(closes),
            "close_start": closes[0],
            "close_end":   closes[-1],
            "n_days":      len(closes),
        }
    except Exception as e:
        print(f"  [price_history] {ticker} failed: {e}")
        return None


def get_current_price(ticker: str) -> float | None:
    """Fetch today's closing/latest price via Yahoo Finance."""
    today = date.today()
    ohlc  = get_ohlc(ticker, today - timedelta(days=5), today)
    return ohlc["close_end"] if ohlc else None
