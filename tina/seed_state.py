"""One-shot script: fetch and cache the latest 13F for every followed fund.

Run this manually to pre-populate state before the main scanner has seen any filings.
It fetches positions and detects fund style, but posts NOTHING to Discord — so you
won't get a flood of "new filing" alerts for data that isn't actually new.

Usage (from the tina/ directory):
  ../.venv/bin/python seed_state.py
  ../.venv/bin/python seed_state.py --force   # re-seeds even already-seen funds
"""

import json
import sys
import time
from pathlib import Path

if not Path("config.py").exists():
    print("ERROR: Run this from the tina/ directory.", file=sys.stderr)
    sys.exit(1)

import config
import fund_type as fund_type_mod
import state as state_mod
from data import edgar13f, cusip as cusip_mod, name_ticker as name_ticker_mod

_FUNDS_FILE = Path("funds.json")
FORCE       = "--force" in sys.argv


def _load_funds() -> list[dict]:
    return json.loads(_FUNDS_FILE.read_text()) if _FUNDS_FILE.exists() else []


def seed_fund(fund: dict, st: dict) -> bool:
    cik  = fund["cik"]
    name = fund.get("display_name") or fund["name"]

    print(f"\n{'='*60}")
    print(f"  {name}  (CIK {cik})")
    print(f"{'='*60}")

    filing = edgar13f.fetch_latest_13f(cik)
    time.sleep(config.REQUEST_DELAY)
    if not filing:
        print("  No 13F filing found — skipping")
        return False

    acc    = filing["accession"]
    period = filing["period"]
    print(f"  Latest 13F: {acc}  ({period})")

    if state_mod.is_seen(st, acc) and not FORCE:
        print("  Already seeded (use --force to re-seed)")
        return False

    holdings = edgar13f.parse_holdings(acc, cik)
    if not holdings:
        print("  No holdings parsed — skipping")
        state_mod.mark_seen(st, acc)
        return False

    print(f"  {len(holdings)} positions parsed")

    # Resolve CUSIPs: sort by value so top positions get tickers first, cap at 200 per fund.
    # Quant funds (4000+ positions) would take hours to resolve fully — not worth it.
    ticker_cache = state_mod.get_ticker_cache(st)
    sorted_h     = sorted(holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
    unknowns     = [h["cusip"] for h in sorted_h if h.get("cusip") and h["cusip"] not in ticker_cache]
    cap          = 200  # max CUSIPs to fetch per fund in seed mode
    to_fetch     = unknowns[:cap]
    if to_fetch:
        n_batches = (len(to_fetch) + 9) // 10
        print(f"  Resolving {len(to_fetch)}/{len(unknowns)} CUSIPs ({n_batches} batches, top {cap} by value)…")
        ticker_cache = cusip_mod.lookup_batch(to_fetch, ticker_cache)
        for cusip_val, ticker in ticker_cache.items():
            state_mod.set_ticker(st, cusip_val, ticker)
    for h in holdings:
        h["ticker"] = ticker_cache.get(h.get("cusip", ""), "")

    # Name-based fallback for positions that still have no ticker (no rate limit)
    unresolved_names = [h["name"] for h in holdings if not h.get("ticker") and h.get("name")]
    if unresolved_names:
        name_map = name_ticker_mod.lookup_names(unresolved_names)
        if name_map:
            print(f"  Name-lookup matched {len(name_map)} additional tickers")
            for h in holdings:
                if not h.get("ticker") and h.get("name") in name_map:
                    ticker = name_map[h["name"]]
                    h["ticker"] = ticker
                    if h.get("cusip"):
                        state_mod.set_ticker(st, h["cusip"], ticker)

    resolved = sum(1 for h in holdings if h.get("ticker"))
    print(f"  {resolved}/{len(holdings)} positions resolved to tickers")

    # Fund style detection
    total_value = sum(h["value_usd"] for h in holdings)
    style_data  = fund_type_mod.detect(holdings, total_value)
    print(f"  Style: {style_data['label']}  ({style_data['n_positions']} positions, top-10={style_data['top10_pct']}%, HHI={style_data['hhi']})")
    print(f"  Conviction weight: ×{style_data['conviction']}")

    # Persist positions + style
    new_positions = {
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
    state_mod.set_fund_positions(st, cik, name, period, new_positions)
    state_mod.set_fund_style(st, cik, style_data)
    state_mod.mark_seen(st, acc)

    top5 = sorted(holdings, key=lambda h: h["value_usd"], reverse=True)[:5]
    print("  Top 5 holdings:")
    for h in top5:
        ticker = h.get("ticker") or "?"
        print(f"    ${ticker:<8} {h['name'][:30]:<30}  ${h['value_usd']/1e6:.0f}M")

    print(f"  ✓ Seeded Q1 2026. AUM: ${total_value/1e9:.2f}B")

    # Seed the previous quarter (Q4 2025) into history so diff commands work
    prev_filing = edgar13f.fetch_nth_13f(cik, n=1)
    time.sleep(config.REQUEST_DELAY)
    if not prev_filing:
        print("  No previous quarter found — diffs won't work until next filing arrives")
        return True

    prev_acc    = prev_filing["accession"]
    prev_period = prev_filing["period"]
    if not FORCE and state_mod.is_seen(st, prev_acc + ":history"):
        print(f"  Previous quarter already seeded ({prev_period})")
        return True

    print(f"  Fetching previous quarter: {prev_acc} ({prev_period})")
    prev_holdings = edgar13f.parse_holdings(prev_acc, cik)
    time.sleep(config.REQUEST_DELAY)
    if not prev_holdings:
        print(f"  Could not parse previous quarter — skipping history")
        return True

    # Resolve tickers for previous holdings from cache + name fallback
    ticker_cache = state_mod.get_ticker_cache(st)
    unknowns_prev = [h["cusip"] for h in prev_holdings if h.get("cusip") and h["cusip"] not in ticker_cache]
    if unknowns_prev:
        print(f"  Resolving {len(unknowns_prev)} CUSIPs for previous quarter…")
        ticker_cache = cusip_mod.lookup_batch(unknowns_prev[:100], ticker_cache)
        for cusip_val, ticker in ticker_cache.items():
            state_mod.set_ticker(st, cusip_val, ticker)
    for h in prev_holdings:
        h["ticker"] = ticker_cache.get(h.get("cusip", ""), "")
    # Name fallback for remaining unresolved
    unresolved_prev = [h["name"] for h in prev_holdings if not h.get("ticker") and h.get("name")]
    if unresolved_prev:
        name_map = name_ticker_mod.lookup_names(unresolved_prev)
        for h in prev_holdings:
            if not h.get("ticker") and h.get("name") in name_map:
                ticker = name_map[h["name"]]
                h["ticker"] = ticker
                if h.get("cusip"):
                    state_mod.set_ticker(st, h["cusip"], ticker)

    prev_positions = {
        h["cusip"]: {
            "name":        h["name"],
            "ticker":      h.get("ticker", ""),
            "shares":      h["shares"],
            "value_usd":   h["value_usd"],
            "option_type": h.get("option_type"),
            "quarter":     prev_period,
        }
        for h in prev_holdings
        if h.get("cusip")
    }

    # Inject into history directly
    fund_entry = st["funds"][cik]
    history    = list(fund_entry.get("history", []))
    already    = any(q["quarter"] == prev_period for q in history)
    if not already:
        history.insert(0, {"quarter": prev_period, "positions": prev_positions})
        fund_entry["history"] = history[:4]
        state_mod.mark_seen(st, prev_acc + ":history")
        print(f"  ✓ History seeded: {prev_period} ({len(prev_positions)} positions)")
    else:
        print(f"  History for {prev_period} already present")

    return True


def main():
    st    = state_mod.load()
    funds = _load_funds()
    if not funds:
        print("No funds in funds.json — add some with: python main.py --add 'Fund Name'")
        sys.exit(1)

    print(f"Seeding state for {len(funds)} fund(s)…{'  [FORCE mode]' if FORCE else ''}\n")

    seeded = 0
    for fund in funds:
        try:
            if seed_fund(fund, st):
                seeded += 1
                state_mod.save(st)  # save after each fund so partial runs are recoverable
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(1.0)

    state_mod.save(st)
    print(f"\n{'='*60}")
    print(f"Done. Seeded {seeded}/{len(funds)} funds.")
    print("The main scanner will now detect only genuinely NEW filings going forward.")


if __name__ == "__main__":
    main()
