"""
Main entry point — runs all scanners on a polling loop.

Usage:
  python main.py                        # start the scanner
  python main.py --add AAPL             # add ticker to watchlist
  python main.py --remove AAPL          # remove ticker
  python main.py --list                 # show watchlist
  python main.py --scores               # print squeeze scores without posting
  python main.py --filter show          # show discovery filters
  python main.py --filter max-cap 500M  # set max market cap
  python main.py --filter min-cap 10M   # set min market cap
  python main.py --filter max-float 50M # set max float
  python main.py --filter min-price 1   # set min price
  python main.py --filter max-price 50  # set max price
  python main.py --filter roles exec    # exec only (CEO/CFO/COO/CTO/President)
  python main.py --filter roles all     # all insiders including directors
  python main.py --filter min-buy 25000 # min $ value for discovery buys
  python main.py --filter off           # disable discovery scanner
  python main.py --filter on            # enable discovery scanner
  python main.py --filter reset         # reset to defaults
"""

import argparse
import json
import sys
import time
from pathlib import Path

import config
import state as st
from data.edgar import resolve_ticker
from scanners import activist, dilution, discovery, form4, squeeze

WATCHLIST_FILE = Path("watchlist.json")


# ── Watchlist management ───────────────────────────────────────────────────────

def load_watchlist() -> dict:
    if WATCHLIST_FILE.exists():
        return json.loads(WATCHLIST_FILE.read_text())
    return {"tickers": []}


def save_watchlist(wl: dict) -> None:
    WATCHLIST_FILE.write_text(json.dumps(wl, indent=2))


def cmd_add(ticker: str) -> None:
    from datetime import date
    from data.market import get_market_data

    wl = load_watchlist()
    ticker = ticker.upper()
    if any(e["ticker"] == ticker for e in wl["tickers"]):
        print(f"{ticker} is already in the watchlist.")
        return
    result = resolve_ticker(ticker)
    if result is None:
        print(f"Could not find {ticker} on EDGAR.")
        return
    cik, title = result

    added_price = None
    try:
        added_price = get_market_data(ticker).get("price")
    except Exception:
        pass

    wl["tickers"].append({
        "ticker":      ticker,
        "cik":         cik,
        "name":        title,
        "added_date":  date.today().isoformat(),
        "added_price": added_price,
    })
    save_watchlist(wl)
    price_info = f" at ${added_price:.2f}" if added_price else ""
    print(f"Added {ticker} ({title}) — CIK {cik}{price_info}")

    import discord_bot
    discord_bot.update_watchlist_board(wl["tickers"])


def cmd_remove(ticker: str) -> None:
    wl = load_watchlist()
    ticker = ticker.upper()
    before = len(wl["tickers"])
    wl["tickers"] = [e for e in wl["tickers"] if e["ticker"] != ticker]
    if len(wl["tickers"]) == before:
        print(f"{ticker} was not in the watchlist.")
    else:
        save_watchlist(wl)
        print(f"Removed {ticker}.")
        import discord_bot
        discord_bot.update_watchlist_board(wl["tickers"])


def cmd_list() -> None:
    wl = load_watchlist()
    tickers = wl.get("tickers", [])
    if not tickers:
        print("Watchlist is empty.  python main.py --add AAPL")
        return
    print(f"{'Ticker':<8} {'CIK':<12} Name")
    print("─" * 55)
    for e in tickers:
        print(f"{e['ticker']:<8} {e['cik']:<12} {e['name']}")


def _parse_number(s: str) -> float:
    """Parse '500M' → 500_000_000, '50M' → 50_000_000, '1B' → 1_000_000_000."""
    s = s.upper().strip()
    mults = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    if s[-1] in mults:
        return float(s[:-1]) * mults[s[-1]]
    return float(s)


def cmd_filter(args: list[str]) -> None:
    from scanners.discovery import load_filters, _default_filters
    filters = load_filters()
    cmd = args[0].lower()
    val = args[1] if len(args) > 1 else ""

    if cmd == "show":
        cap = filters.get("market_cap", {})
        flt = filters.get("float", {})
        prc = filters.get("price", {})
        print(f"Discovery filters (enabled={filters.get('enabled', True)}):")
        print(f"  Min score  : {filters.get('min_score', 30)}")
        print(f"  Market cap : ${cap.get('min', 0)/1e6:.0f}M – ${cap.get('max', 0)/1e6:.0f}M")
        print(f"  Float      : max {flt.get('max', 0)/1e6:.0f}M shares")
        print(f"  Price      : ${prc.get('min', 0)} – ${prc.get('max', 0)}")
        print(f"  Min buy    : ${filters.get('min_buy_value', 0):,}")
        print(f"  Roles      : {filters.get('roles', 'exec')}")

    elif cmd == "min-score":
        v = int(float(val))
        if not (0 <= v <= 100):
            print("min-score must be between 0 and 100")
            return
        filters["min_score"] = v
        tier = "Notable" if v < 50 else "Strong" if v < 70 else "Exceptional"
        print(f"Min score set to {v} ({tier}+)")
    elif cmd == "max-cap":
        filters.setdefault("market_cap", {})["max"] = int(_parse_number(val))
        print(f"Max market cap set to ${int(_parse_number(val)):,}")
    elif cmd == "min-cap":
        filters.setdefault("market_cap", {})["min"] = int(_parse_number(val))
        print(f"Min market cap set to ${int(_parse_number(val)):,}")
    elif cmd == "max-float":
        filters.setdefault("float", {})["max"] = int(_parse_number(val))
        print(f"Max float set to {int(_parse_number(val)):,} shares")
    elif cmd == "min-price":
        filters.setdefault("price", {})["min"] = float(val)
        print(f"Min price set to ${float(val)}")
    elif cmd == "max-price":
        filters.setdefault("price", {})["max"] = float(val)
        print(f"Max price set to ${float(val)}")
    elif cmd == "min-buy":
        filters["min_buy_value"] = int(_parse_number(val))
        print(f"Min buy value set to ${int(_parse_number(val)):,}")
    elif cmd == "roles":
        if val not in ("exec", "all"):
            print("Roles must be 'exec' or 'all'")
            return
        filters["roles"] = val
        print(f"Roles filter set to '{val}'")
    elif cmd == "on":
        filters["enabled"] = True
        print("Discovery scanner enabled")
    elif cmd == "off":
        filters["enabled"] = False
        print("Discovery scanner disabled")
    elif cmd == "reset":
        filters = _default_filters()
        print("Filters reset to defaults")
    else:
        print(f"Unknown filter command: {cmd}")
        print("Run: python main.py --filter show")
        return

    from pathlib import Path
    import json
    Path("filters.json").write_text(json.dumps(filters, indent=2))


def cmd_perf() -> None:
    """Print performance table for all watchlist tickers since they were added."""
    from data.market import get_market_data

    wl = load_watchlist()
    tickers = wl.get("tickers", [])
    if not tickers:
        print("Watchlist is empty.")
        return

    print(f"\n{'Ticker':<8} {'Added':<12} {'Add $':>8} {'Now $':>8} {'Perf':>9}  {'Cap':>8}  Name")
    print("─" * 78)

    for entry in tickers:
        ticker      = entry["ticker"]
        added_date  = entry.get("added_date", "?")
        added_price = entry.get("added_price")

        market = get_market_data(ticker)
        price  = market.get("price")
        cap    = market.get("market_cap")

        if price and added_price:
            perf     = (price - added_price) / added_price
            perf_str = f"{perf:+.1%}"
        else:
            perf_str = "N/A"

        add_str   = f"${added_price:.2f}" if added_price  else "N/A"
        price_str = f"${price:.2f}"        if price        else "N/A"

        if cap is None:
            cap_str = "N/A"
        elif cap >= 1e12:
            cap_str = f"${cap/1e12:.1f}T"
        elif cap >= 1e9:
            cap_str = f"${cap/1e9:.1f}B"
        else:
            cap_str = f"${cap/1e6:.0f}M"

        print(
            f"{ticker:<8} {added_date:<12} {add_str:>8} {price_str:>8} "
            f"{perf_str:>9}  {cap_str:>8}  {entry['name']}"
        )
        time.sleep(1.1)

    print()


def cmd_lookup(ticker: str) -> None:
    """Fetch a snapshot for any ticker and post it to Discord."""
    from data.market import calc_squeeze_score, get_borrow_rate, get_market_data
    from data import edgar

    ticker = ticker.upper()
    print(f"Looking up {ticker}…")

    result = resolve_ticker(ticker)
    if result is None:
        print(f"Could not find {ticker} on EDGAR.")
        return
    cik, title = result

    try:
        market = get_market_data(ticker)
    except Exception:
        market = {}

    try:
        borrow_rate = get_borrow_rate(ticker)
    except Exception:
        borrow_rate = None

    squeeze_score, squeeze_factors = calc_squeeze_score(market, borrow_rate)

    # Collect recent Form 4 activity (buys P and open-market sells S only)
    recent_txns: list[dict] = []
    try:
        filings = edgar.fetch_recent_form4s(cik, lookback_days=30)
        for filing in filings[:15]:
            details = edgar.fetch_form4_details(cik, filing["accession"], filing["primary_doc"])
            if not details:
                continue
            for txn in details["transactions"]:
                if txn["code"] == "P" and txn["acquired"] and txn["value"] > 0:
                    recent_txns.append({**txn, "date": filing["filed"],
                                        "owner_name": details["owner_name"],
                                        "role": details["role"]})
                elif txn["code"] == "S" and not txn["acquired"] and txn["value"] > 0:
                    recent_txns.append({**txn, "date": filing["filed"],
                                        "owner_name": details["owner_name"],
                                        "role": details["role"]})
            time.sleep(0.15)
    except Exception as e:
        print(f"  Warning: could not fetch Form 4s: {e}")

    import discord_bot
    discord_bot.post_lookup(ticker, title, market, squeeze_score, squeeze_factors,
                            borrow_rate, recent_txns)
    print(f"Posted lookup for {ticker} to Discord.")


def cmd_scores() -> None:
    """Print current squeeze scores for all watchlist tickers without posting."""
    from data.market import calc_squeeze_score, get_borrow_rate, get_market_data
    wl = load_watchlist()
    for entry in wl.get("tickers", []):
        ticker = entry["ticker"]
        market = get_market_data(ticker)
        borrow = get_borrow_rate(ticker)
        score, factors = calc_squeeze_score(market, borrow)
        print(f"\n{ticker}  score={score}/100")
        for f in factors:
            print(f"  • {f}")
        if not factors:
            print("  No squeeze factors triggered")
        time.sleep(1.2)


# ── Scanner loop ───────────────────────────────────────────────────────────────

def run_loop() -> None:
    wl = load_watchlist()
    n  = len(wl.get("tickers", []))

    print(f"SEC EDGAR Scanner starting — {n} ticker(s) on watchlist")
    print(f"Polling every {config.POLL_INTERVAL}s | Thresholds: "
          f"buy>${config.MIN_BUY_VALUE_USD:,} "
          f"squeeze>={config.SQUEEZE_ALERT_SCORE}/100 "
          f"borrow>={config.BORROW_ALERT_PCT:.0%}")
    print("─" * 60)

    if n == 0:
        print("Watchlist is empty — add tickers with:  python main.py --add AAPL")

    state = st.load()

    while True:
        wl = load_watchlist()  # reload each cycle so --add takes effect without restart
        print(f"\n[{_now()}] Running scan ({len(wl.get('tickers', []))} tickers)…")

        try:
            print(" Dilution / offering filings")
            dilution.run(wl, state)
        except Exception as e:
            print(f"  [dilution] Fatal: {e}")

        try:
            print(" Form 4 / insider trades")
            form4.run(wl, state)
        except Exception as e:
            print(f"  [form4] Fatal: {e}")

        try:
            print(" Squeeze + borrow rates")
            squeeze.run(wl, state)
        except Exception as e:
            print(f"  [squeeze] Fatal: {e}")

        try:
            print(" Activist filings (13D/13G)")
            activist.run(wl, state)
        except Exception as e:
            print(f"  [activist] Fatal: {e}")

        try:
            print(" Discovery (small-cap insider buys)")
            discovery.run(state)
        except Exception as e:
            print(f"  [discovery] Fatal: {e}")

        st.prune_old_cluster_data(state, config.CLUSTER_WINDOW_DAYS)
        st.save(state)

        print(f"[{_now()}] Scan complete. Next in {config.POLL_INTERVAL}s.")
        time.sleep(config.POLL_INTERVAL)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SEC EDGAR multi-signal scanner")
    parser.add_argument("--add",    metavar="TICKER", help="Add ticker to watchlist")
    parser.add_argument("--remove", metavar="TICKER", help="Remove ticker from watchlist")
    parser.add_argument("--list",   action="store_true", help="Show watchlist")
    parser.add_argument("--perf",   action="store_true", help="Show performance since each ticker was added")
    parser.add_argument("--scores", action="store_true", help="Print squeeze scores (no posting)")
    parser.add_argument("--lookup", metavar="TICKER", help="Post on-demand snapshot to Discord")
    parser.add_argument("--filter", metavar="CMD [VALUE]", nargs="+", help="Manage discovery filters")
    args = parser.parse_args()

    if args.add:
        cmd_add(args.add)
    elif args.remove:
        cmd_remove(args.remove)
    elif args.list:
        cmd_list()
    elif args.perf:
        cmd_perf()
    elif args.scores:
        cmd_scores()
    elif args.lookup:
        cmd_lookup(args.lookup)
    elif args.filter:
        cmd_filter(args.filter)
    else:
        run_loop()


if __name__ == "__main__":
    main()
