"""Backfill historical 13F quarters so ENTER/EXIT diffs work.

Fetches the N-1 and N-2 quarters for each followed fund and stores them in
fund.history without re-seeding or re-resolving the current quarter.

Usage (from the tina/ directory):
  ../.venv/bin/python seed_history.py           # prev quarter only (n=1)
  ../.venv/bin/python seed_history.py --two     # two prior quarters (n=1 and n=2)
"""

import json
import sys
import time
from pathlib import Path

if not Path("config.py").exists():
    print("ERROR: Run from the tina/ directory.", file=sys.stderr)
    sys.exit(1)

import config
import state as state_mod
from data import edgar13f, cusip as cusip_mod, name_ticker as name_ticker_mod

TWO_QUARTERS = "--two" in sys.argv
_FUNDS_FILE  = Path("funds.json")


def _resolve_tickers(holdings: list[dict], st: dict) -> None:
    ticker_cache = state_mod.get_ticker_cache(st)
    unknowns = [h["cusip"] for h in holdings if h.get("cusip") and h["cusip"] not in ticker_cache]
    if unknowns:
        cap = 100
        print(f"    Resolving {min(len(unknowns), cap)}/{len(unknowns)} CUSIPs from EDGAR…")
        ticker_cache = cusip_mod.lookup_batch(unknowns[:cap], ticker_cache)
        for cv, tk in ticker_cache.items():
            state_mod.set_ticker(st, cv, tk)
    for h in holdings:
        h["ticker"] = state_mod.get_ticker_cache(st).get(h.get("cusip", ""), "")

    unresolved = [h["name"] for h in holdings if not h.get("ticker") and h.get("name")]
    if unresolved:
        name_map = name_ticker_mod.lookup_names(unresolved)
        for h in holdings:
            if not h.get("ticker") and h.get("name") in name_map:
                ticker = name_map[h["name"]]
                h["ticker"] = ticker
                if h.get("cusip"):
                    state_mod.set_ticker(st, h["cusip"], ticker)

    resolved = sum(1 for h in holdings if h.get("ticker"))
    print(f"    {resolved}/{len(holdings)} positions resolved")


def fetch_and_store(cik: str, name: str, n: int, st: dict) -> bool:
    filing = edgar13f.fetch_nth_13f(cik, n=n)
    time.sleep(config.REQUEST_DELAY)
    if not filing:
        print(f"  n={n}: no filing found")
        return False

    acc    = filing["accession"]
    period = filing["period"]
    print(f"  n={n}: {acc}  ({period})")

    fund_entry = st.setdefault("funds", {}).setdefault(cik, {})
    history    = fund_entry.get("history", [])
    if any(q["quarter"] == period for q in history):
        print(f"  n={n}: {period} already in history — skipping")
        return False

    seen_key = acc + ":history"
    if state_mod.is_seen(st, seen_key):
        print(f"  n={n}: already seeded (seen key present) — skipping")
        return False

    holdings = edgar13f.parse_holdings(acc, cik)
    time.sleep(config.REQUEST_DELAY)
    if not holdings:
        print(f"  n={n}: no holdings parsed — skipping")
        return False

    print(f"  n={n}: {len(holdings)} positions")
    _resolve_tickers(holdings, st)

    positions = {
        h["cusip"]: {
            "name":        h["name"],
            "ticker":      h.get("ticker", ""),
            "shares":      h["shares"],
            "value_usd":   h["value_usd"],
            "option_type": h.get("option_type"),
            "quarter":     period,
        }
        for h in holdings
        if h.get("cusip")
    }

    # Insert at position n-1 in history (older quarters go deeper)
    insert_at = n - 1
    history.insert(insert_at, {"quarter": period, "positions": positions})
    fund_entry["history"] = history[:4]
    state_mod.mark_seen(st, seen_key)
    print(f"  ✓ Stored {period} in history[{insert_at}]  ({len(positions)} positions)")
    return True


def main():
    st    = state_mod.load()
    funds = json.loads(_FUNDS_FILE.read_text()) if _FUNDS_FILE.exists() else []
    if not funds:
        print("No funds in funds.json")
        sys.exit(1)

    ns = [1, 2] if TWO_QUARTERS else [1]
    print(f"Fetching {len(ns)} historical quarter(s) for {len(funds)} fund(s)…\n")

    for fund in funds:
        cik  = fund["cik"]
        name = fund.get("display_name") or fund["name"]
        print(f"\n{'='*60}\n  {name}  (CIK {cik})\n{'='*60}")
        for n in ns:
            try:
                fetch_and_store(cik, name, n, st)
                state_mod.save(st)
            except Exception as e:
                print(f"  ERROR (n={n}): {e}")
            time.sleep(1.0)

    state_mod.save(st)
    print(f"\n{'='*60}")
    print("Done. Run test_commands.py to verify /entry and /exits now show data.")


if __name__ == "__main__":
    main()
