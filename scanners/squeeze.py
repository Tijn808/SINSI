"""
Short squeeze scanner.

For each watchlist ticker:
  1. Pull market data from Finviz (short float %, DTC, float size, insider own %).
  2. Pull borrow rate from iborrowdesk.
  3. Compute composite squeeze score (0-100).
  4. If score >= SQUEEZE_ALERT_SCORE and not on cooldown → squeeze alert.
  5. If borrow rate alone >= BORROW_ALERT_PCT and not on cooldown → high borrow alert.
"""

import time

import config
import discord_bot
import state as st
from data.market import calc_squeeze_score, get_borrow_rate, get_market_data


def run(watchlist: dict, state: dict) -> None:
    tickers = watchlist.get("tickers", [])
    if not tickers:
        return

    for entry in tickers:
        ticker = entry["ticker"]
        try:
            _scan_ticker(ticker, state)
        except Exception as e:
            print(f"  [squeeze] Error scanning {ticker}: {e}")
        time.sleep(1.5)  # Finviz rate limit


def _scan_ticker(ticker: str, state: dict) -> None:
    market      = get_market_data(ticker)
    borrow_rate = get_borrow_rate(ticker)

    if not market:
        print(f"  [squeeze] No market data for {ticker}")
        return

    score, factors = calc_squeeze_score(market, borrow_rate)
    print(
        f"  [squeeze] {ticker}: score={score}/100 "
        f"short={market.get('short_pct_float', 0) or 0:.0%} "
        f"dtc={market.get('short_ratio') or '?'} "
        f"borrow={f'{borrow_rate:.0%}' if borrow_rate else '?'}"
    )

    # Squeeze composite alert
    if score >= config.SQUEEZE_ALERT_SCORE:
        key = f"squeeze:{ticker}"
        if not st.is_on_cooldown(state, key, config.SQUEEZE_COOLDOWN_HOURS):
            print(f"  [squeeze] Firing squeeze alert for {ticker} (score {score})")
            discord_bot.post_squeeze(ticker, score, market, borrow_rate, factors)
            st.set_cooldown(state, key)
            time.sleep(1)

    # Standalone high borrow alert
    if borrow_rate and borrow_rate >= config.BORROW_ALERT_PCT:
        key = f"borrow:{ticker}"
        if not st.is_on_cooldown(state, key, config.BORROW_COOLDOWN_HOURS):
            print(f"  [squeeze] Firing high borrow alert for {ticker} ({borrow_rate:.1%})")
            discord_bot.post_high_borrow(ticker, borrow_rate, market)
            st.set_cooldown(state, key)
            time.sleep(1)
