"""Replay the most recent 13F filing for every followed fund using the
consolidated alert format. Useful for testing or catching up after a format change.

Run from the tina/ directory:
  ../.venv/bin/python replay_filings.py           # all funds
  ../.venv/bin/python replay_filings.py Berkshire  # one fund (name substring)
"""

import json
import sys
import time
from pathlib import Path

if not Path("config.py").exists():
    print("ERROR: Run from the tina/ directory.", file=sys.stderr)
    sys.exit(1)

import discord_bot
import flow
import state as state_mod
from scanners.form13f import _load_sinsi_watchlist

_FUNDS_FILE = Path("funds.json")
FILTER = sys.argv[1].lower() if len(sys.argv) > 1 else ""


def _disp_name(funds_json: list, cik: str, fallback: str) -> str:
    for f in funds_json:
        if f["cik"] == cik:
            return f.get("display_name") or f["name"]
    return fallback


def replay_fund(cik: str, display_name: str, fund_data: dict) -> None:
    positions   = fund_data.get("positions", {})
    history     = fund_data.get("history", [])
    period      = fund_data.get("latest_quarter", "?")
    style_data  = fund_data.get("style", {})

    if not positions:
        print(f"  {display_name}: no positions — skipping")
        return

    prev_positions = history[0]["positions"] if history else {}
    total_value    = sum(p.get("value_usd", 0) for p in positions.values())

    holdings = [
        {
            "cusip":       cusip,
            "name":        p.get("name", ""),
            "ticker":      p.get("ticker", ""),
            "value_usd":   p.get("value_usd", 0),
            "shares":      p.get("shares", 0),
            "option_type": p.get("option_type"),
        }
        for cusip, p in positions.items()
    ]

    events = flow.diff(
        fund_name=display_name,
        quarter=period,
        holdings=holdings,
        prev_positions=prev_positions,
        total_value=total_value,
        is_first=not prev_positions,
    )
    for ev in events:
        ev.fund_style = style_data.get("style", "")
        ev.conviction = style_data.get("conviction", 1.0)

    # SINSI crossref hits
    sinsi_hits: list[dict] = []
    sinsi_watchlist = _load_sinsi_watchlist()
    if sinsi_watchlist:
        for ev in events:
            if ev.ticker and ev.ticker.upper() in sinsi_watchlist:
                sinsi_hits.append({
                    "ticker":      ev.ticker,
                    "change_type": ev.type.lower(),
                    "value":       ev.prev_value_usd if ev.type == "EXIT" else ev.value_usd,
                })

    # New overlap: ENTER events now held by 2+ funds
    st = state_mod.load()
    ticker_funds: dict[str, list[str]] = {}
    for other_cik, other_data in st.get("funds", {}).items():
        other_name = other_data.get("name", other_cik)
        for pos in other_data.get("positions", {}).values():
            t = pos.get("ticker", "")
            if t:
                ticker_funds.setdefault(t, [])
                if other_name not in ticker_funds[t]:
                    ticker_funds[t].append(other_name)

    overlap_enters: list[dict] = []
    for ev in events:
        if ev.type != "ENTER" or not ev.ticker:
            continue
        holders = ticker_funds.get(ev.ticker, [])
        if len(holders) >= 2:
            total_inst = sum(
                p.get("value_usd", 0)
                for od in st.get("funds", {}).values()
                for p in od.get("positions", {}).values()
                if p.get("ticker") == ev.ticker
            )
            overlap_enters.append({
                "ticker":      ev.ticker,
                "funds":       holders,
                "total_value": total_inst,
            })

    n_enter = sum(1 for e in events if e.type == "ENTER")
    n_exit  = sum(1 for e in events if e.type == "EXIT")
    print(f"  {display_name}: {len(holdings)} positions · {n_enter} enters · {n_exit} exits · {len(sinsi_hits)} SINSI hits")

    discord_bot.post_filing_alert(
        fund_name=display_name,
        cik=cik,
        period=period,
        accession="replay",
        holdings=holdings,
        events=events,
        prev_positions=prev_positions,
        total_value=total_value,
        sinsi_hits=sinsi_hits or None,
        overlap_enters=overlap_enters or None,
    )
    time.sleep(1.5)


def main():
    st         = state_mod.load()
    funds_json = json.loads(_FUNDS_FILE.read_text()) if _FUNDS_FILE.exists() else []
    funds      = st.get("funds", {})

    targets = {
        cik: fd for cik, fd in funds.items()
        if not FILTER or FILTER in (fd.get("name", "") + _disp_name(funds_json, cik, "")).lower()
    }

    if not targets:
        print(f"No funds matched '{FILTER}'")
        sys.exit(1)

    print(f"Replaying last filing for {len(targets)} fund(s)…\n")
    for cik, fd in targets.items():
        display = _disp_name(funds_json, cik, fd.get("name", cik))
        print(f"\n{'='*60}\n  {display}\n{'='*60}")
        try:
            replay_fund(cik, display, fd)
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(1.0)

    print("\nDone.")


if __name__ == "__main__":
    main()
