"""
Activist filing scanner — SC 13D and SC 13G.

For each watchlist ticker, searches EDGAR's full-text index for recent
13D/13G filings mentioning that ticker. A 13D signals someone crossed 5%
ownership WITH intent to influence management. A 13G is the passive version.

Both are significant events that frequently precede major price moves.
"""

import time

import config
import discord_bot
import state as st
from data import edgar


def run(watchlist: dict, state: dict) -> None:
    tickers = watchlist.get("tickers", [])
    if not tickers:
        return

    for entry in tickers:
        ticker  = entry["ticker"]
        company = entry["name"]
        try:
            _scan_ticker(ticker, company, state)
        except Exception as e:
            print(f"  [activist] Error scanning {ticker}: {e}")
        time.sleep(0.5)


def _scan_ticker(ticker: str, company: str, state: dict) -> None:
    filings = edgar.fetch_recent_activist(ticker, config.LOOKBACK_DAYS)
    new = [f for f in filings if not st.is_seen(state, f["accession"])]

    if not new:
        return

    print(f"  [activist] {ticker}: {len(new)} new 13D/13G filing(s)")

    for filing in new:
        st.mark_seen(state, filing["accession"])
        discord_bot.post_activist(ticker, company, filing)
        time.sleep(1)
