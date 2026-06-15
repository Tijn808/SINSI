"""
Dilution scanner — watches watchlist companies for share offering filings.

Fires a warning alert when a company files:
  S-1 / S-1/A    — new or secondary offering registration
  S-3 / S-3/A    — shelf registration (can sell shares anytime in next 3 years)
  424B*           — active prospectus (shares are being sold NOW)

These are bear signals for squeeze setups: a company printing stock into
high short interest kills the thesis overnight.
"""

import time

import config
import discord_bot
import state as st
from data import edgar

DILUTION_FORMS = {
    "S-1", "S-1/A",
    "S-3", "S-3/A",
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7",
}

_ACTIVE_OFFERING = {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7"}
_SHELF           = {"S-3", "S-3/A"}


def run(watchlist: dict, state: dict) -> None:
    tickers = watchlist.get("tickers", [])
    if not tickers:
        return

    for entry in tickers:
        ticker = entry["ticker"]
        cik    = entry["cik"]
        try:
            _scan_company(ticker, cik, entry.get("name", ticker), state)
        except Exception as e:
            print(f"  [dilution] Error scanning {ticker}: {e}")
        time.sleep(0.3)


def _scan_company(ticker: str, cik: str, company: str, state: dict) -> None:
    filings = edgar.fetch_recent_filings(cik, DILUTION_FORMS, config.LOOKBACK_DAYS)
    new = [f for f in filings if not st.is_seen(state, f["accession"])]
    if not new:
        return

    print(f"  [dilution] {ticker}: {len(new)} new offering filing(s)")

    for filing in new:
        st.mark_seen(state, filing["accession"])

        cik_int    = int(cik)
        acc_nodash = filing["accession"].replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_int}/{acc_nodash}/"
        )

        print(f"    → {filing['form']} filed {filing['filed']}")
        discord_bot.post_dilution_warning(ticker, company, filing, url)
        time.sleep(1)
