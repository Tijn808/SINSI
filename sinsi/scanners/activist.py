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
import tina_bridge
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


# ── 13D/G fast lane for TINA institutional holdings ───────────────────────────

def run_tina(watchlist: dict, state: dict) -> None:
    """Scan for 13D/13G filings on any ticker held by TINA-followed institutions.

    Skips tickers already on the SINSI watchlist (they're covered by run() above).
    Only posts if a filing is genuinely new (not in seen_filings) and uses a separate
    composite key "activist-tina:{accession}" to avoid re-posting as regular activist.
    """
    try:
        tina_tickers = tina_bridge.get_all_tina_tickers()
    except Exception as e:
        print(f"  [activist-tina] Could not read TINA state: {e}")
        return

    if not tina_tickers:
        return

    # Build set of SINSI watchlist tickers to skip (they get the normal alert)
    wl_tickers = {e["ticker"].upper() for e in watchlist.get("tickers", [])}

    # Sample up to 150 held tickers to check per cycle (sorted by total value desc)
    # — avoids scanning 4k+ Two Sigma positions on every poll cycle
    ranked = sorted(
        tina_tickers.items(),
        key=lambda x: sum(h["value_usd"] for h in x[1]),
        reverse=True,
    )[:150]

    new_count = 0
    for ticker, institutions in ranked:
        if ticker in wl_tickers:
            continue

        try:
            filings = edgar.fetch_recent_activist(ticker, config.LOOKBACK_DAYS)
        except Exception as e:
            print(f"  [activist-tina] {ticker}: fetch error — {e}")
            time.sleep(0.3)
            continue

        for filing in filings:
            key = f"activist-tina:{filing['accession']}"
            if st.is_seen(state, key):
                continue

            st.mark_seen(state, key)
            st.mark_seen(state, filing["accession"])  # also mark base accession seen
            new_count += 1
            company = filing.get("company", ticker)
            print(
                f"  [activist-tina] {ticker}: new {filing['form_type']} "
                f"by {filing['filer']} — {len(institutions)} inst. holder(s)"
            )
            discord_bot.post_activist_tina(ticker, company, filing, institutions)
            time.sleep(1)

        time.sleep(0.3)

    if new_count:
        print(f"  [activist-tina] {new_count} new filing(s) posted")
