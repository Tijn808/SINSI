"""TINA main scan loop.

Run from the tina/ directory:
  nohup ../.venv/bin/python main.py > scanner-logs.txt 2>&1 &
"""

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if not Path("config.py").exists():
    print("ERROR: Run this from the tina/ directory.", file=sys.stderr)
    print("  cd tina && ../.venv/bin/python main.py", file=sys.stderr)
    sys.exit(1)

# Prevent multiple scanner instances running simultaneously (causes duplicate posts)
_LOCK_FILE = Path("state/.tina-scanner.lock")
_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
_lock_fd = open(_LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except BlockingIOError:
    print("ERROR: Another tina/main.py is already running. Exiting to prevent duplicate posts.")
    sys.exit(1)

import config
import state as state_mod
import discord_bot
from scanners import form13f

_FUNDS_FILE  = Path("funds.json")
_ONLINE_MSG  = "🟢 **TINA is online.** Institutional scanning active."
_OFFLINE_MSG = (
    "🔴 **TINA has gone offline.** Scanning paused.\n"
    "Slash commands won't respond until it's back. Spam Tijn to turn his PC on."
)


def _load_funds() -> list[dict]:
    if _FUNDS_FILE.exists():
        try:
            return json.loads(_FUNDS_FILE.read_text())
        except Exception:
            pass
    return []


def _maybe_post_weekly_summary(st: dict) -> None:
    now = datetime.now(timezone.utc)
    if now.weekday() != config.WEEKLY_SUMMARY_DAY:
        return
    if now.hour < config.WEEKLY_SUMMARY_HOUR:
        return

    today = now.date().isoformat()
    if st.get("weekly_posted") == today:
        return

    from data import market

    fund_perfs = []
    for fund in _load_funds():
        cik       = fund["cik"]
        fd        = st.get("funds", {}).get(cik, {})
        positions = fd.get("positions", {})
        name      = fd.get("name") or fund.get("display_name") or fund["name"]
        if not positions:
            continue

        tickers = list({p["ticker"] for p in positions.values() if p.get("ticker")})
        prices  = market.get_prices_bulk(tickers[:30], delay=0.05)

        total = w_ret = w_sum = 0.0
        for pos in positions.values():
            ticker = pos.get("ticker")
            shares = pos.get("shares", 0)
            val    = pos.get("value_usd", 0)
            if not ticker or not shares or not val:
                continue
            curr = prices.get(ticker)
            if not curr:
                continue
            ret   = (curr - val / shares) / (val / shares) * 100
            total += val
            w_ret += ret * val
            w_sum += val

        if w_sum > 0:
            fund_perfs.append({
                "name":            name,
                "weighted_return": w_ret / w_sum,
                "total_value_usd": total,
                "n_positions":     len(positions),
            })

    if fund_perfs:
        discord_bot.post_weekly_summary(fund_perfs)

    # Auto-post consensus chart alongside weekly summary
    from data import market as market_mod
    all_pos = state_mod.all_fund_positions(st)
    ticker_map: dict[str, dict] = {}
    for cik, positions in all_pos.items():
        fund_name = st["funds"][cik].get("name", cik)
        for pos in positions.values():
            ticker = pos.get("ticker", "")
            val    = pos.get("value_usd", 0)
            if not ticker or not val:
                continue
            if ticker not in ticker_map:
                ticker_map[ticker] = {"ticker": ticker, "total_value_usd": 0, "fund_count": 0, "funds": []}
            ticker_map[ticker]["total_value_usd"] += val
            ticker_map[ticker]["fund_count"]      += 1
            ticker_map[ticker]["funds"].append(fund_name)

    if ticker_map:
        ranked = sorted(ticker_map.values(), key=lambda x: x["total_value_usd"], reverse=True)
        import charts as charts_mod
        buf = charts_mod.consensus_chart(ranked)
        if buf:
            discord_bot._post_file(buf, "consensus.png", {
                "title": "📊 Weekly Institutional Consensus — Top Holdings",
                "color": config.COLOR_INFO,
                "image": {"url": "attachment://consensus.png"},
                "footer": {"text": "TINA weekly auto-summary"},
            })

    st["weekly_posted"] = today


def _apply_name_resolution(st: dict) -> None:
    """Resolve position names → tickers for any position still missing a ticker.

    Uses the EDGAR company_tickers_exchange.json (no rate limit). Called once on
    startup so the state is always fully resolved, even after a scanner restart.
    """
    from data import name_ticker as name_ticker_mod

    all_names: set[str] = set()
    for fund_data in st.get("funds", {}).values():
        for pos in fund_data.get("positions", {}).values():
            if not pos.get("ticker") and pos.get("name"):
                all_names.add(pos["name"])
        for hist in fund_data.get("history", []):
            for pos in hist.get("positions", {}).values():
                if not pos.get("ticker") and pos.get("name"):
                    all_names.add(pos["name"])

    if not all_names:
        return

    matched = name_ticker_mod.lookup_names(list(all_names))
    if not matched:
        return

    applied = 0
    for fund_data in st.get("funds", {}).values():
        for cusip, pos in fund_data.get("positions", {}).items():
            if not pos.get("ticker") and pos.get("name") in matched:
                ticker = matched[pos["name"]]
                pos["ticker"] = ticker
                if cusip:
                    state_mod.set_ticker(st, cusip, ticker)
                applied += 1
        for hist in fund_data.get("history", []):
            for cusip, pos in hist.get("positions", {}).items():
                if not pos.get("ticker") and pos.get("name") in matched:
                    ticker = matched[pos["name"]]
                    pos["ticker"] = ticker
                    if cusip:
                        state_mod.set_ticker(st, cusip, ticker)
                    applied += 1

    if applied:
        print(f"[tina] Name resolution: {applied} new ticker(s) applied from EDGAR registry")


def run_loop() -> None:
    st = state_mod.load()
    _apply_name_resolution(st)
    state_mod.save(st)
    discord_bot.post_status(_ONLINE_MSG)
    print(f"[tina] Started. Cycle every {config.POLL_INTERVAL}s.")

    try:
        while True:
            st = state_mod.load()  # reload each cycle so external tools (seed scripts) aren't clobbered
            funds = _load_funds()
            print(f"[tina] Scanning {len(funds)} fund(s)…")
            form13f.run_scan(funds, st)
            _maybe_post_weekly_summary(st)
            state_mod.save(st)
            print(f"[tina] Done. Sleeping {config.POLL_INTERVAL}s…")
            time.sleep(config.POLL_INTERVAL)
    finally:
        discord_bot.post_status(_OFFLINE_MSG)
        state_mod.save(st)


def _cli_add(query: str) -> None:
    from data import edgar13f
    results = edgar13f.search_fund(query)
    if not results:
        print(f"No results for '{query}'")
        return
    for i, r in enumerate(results[:5]):
        print(f"  [{i}] {r['name']} (CIK {r['cik']})")
    choice = input("Select [0]: ").strip() or "0"
    picked = results[int(choice)]
    funds  = _load_funds()
    if any(f["cik"] == picked["cik"] for f in funds):
        print("Already following.")
        return
    funds.append({"cik": picked["cik"], "name": picked["name"], "display_name": picked["name"]})
    _FUNDS_FILE.write_text(json.dumps(funds, indent=2))
    print(f"Now following {picked['name']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        run_loop()
    elif args[0] == "--add" and len(args) > 1:
        _cli_add(" ".join(args[1:]))
    elif args[0] == "--list":
        for f in _load_funds():
            print(f"{f['name']}  (CIK {f['cik']})")
    else:
        print("Usage: python main.py [--add 'Fund Name' | --list]")
        sys.exit(1)
