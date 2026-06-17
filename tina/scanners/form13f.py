"""13F scanner — fund-focused.

For each followed fund:
  1. Fetch their latest 13F-HR filing from EDGAR.
  2. Parse the full information table (all holdings).
  3. Resolve CUSIPs to tickers via OpenFIGI.
  4. Diff against stored previous quarter holdings.
  5. Score each change — post alerts and charts for significant moves.
  6. Update state with new positions.
"""

import json
import time
from pathlib import Path

import config
import fund_type as fund_type_mod
import state as state_mod

_SINSI_WATCHLIST = Path("../sinsi/watchlist.json")


def _load_sinsi_watchlist() -> set[str]:
    try:
        if _SINSI_WATCHLIST.exists():
            data = json.loads(_SINSI_WATCHLIST.read_text())
            return {t.upper() for t in (data if isinstance(data, list) else data.get("tickers", []))}
    except Exception:
        pass
    return set()
import discord_bot
import flow
import score_enter as scorer
from data import edgar13f, cusip as cusip_mod, name_ticker as name_ticker_mod, market as market_mod


def run_scan(funds: list[dict], st: dict) -> None:
    if not funds:
        return

    ticker_cache = state_mod.get_ticker_cache(st)

    for fund in funds:
        cik  = fund["cik"]
        name = fund.get("display_name") or fund["name"]
        print(f"  [tina] Checking {name} (CIK {cik})...")

        filing = edgar13f.fetch_latest_13f(cik)
        time.sleep(config.REQUEST_DELAY)
        if not filing:
            continue

        acc    = filing["accession"]
        period = filing["period"]

        if state_mod.is_seen(st, acc):
            continue

        print(f"  [tina] New 13F from {name}: {acc} ({period})")

        holdings = edgar13f.parse_holdings(acc, cik)
        if not holdings:
            print(f"  [tina] No holdings parsed — skipping")
            state_mod.mark_seen(st, acc)
            continue

        # Resolve CUSIPs to tickers (batched, cached)
        cusips       = [h["cusip"] for h in holdings if h.get("cusip")]
        ticker_cache = cusip_mod.lookup_batch(cusips, ticker_cache)
        for cusip_val, ticker in ticker_cache.items():
            state_mod.set_ticker(st, cusip_val, ticker)

        for h in holdings:
            h["ticker"] = ticker_cache.get(h.get("cusip", ""), "")

        # Name-based fallback for any still-unresolved positions
        unresolved_names = [h["name"] for h in holdings if not h.get("ticker") and h.get("name")]
        if unresolved_names:
            name_map = name_ticker_mod.lookup_names(unresolved_names)
            for h in holdings:
                if not h.get("ticker") and h.get("name") in name_map:
                    ticker = name_map[h["name"]]
                    h["ticker"] = ticker
                    if h.get("cusip"):
                        state_mod.set_ticker(st, h["cusip"], ticker)

        prev_positions = state_mod.get_fund_positions(st, cik)
        total_value    = sum(h["value_usd"] for h in holdings)
        is_first       = not prev_positions

        # ── Fund-type detection ───────────────────────────────────────────────

        existing_style = state_mod.get_fund_style(st, cik) or {}
        if not existing_style.get("overridden"):
            style_data = fund_type_mod.detect(holdings, total_value)
        else:
            style_data = existing_style

        # ── Diff via flow engine ──────────────────────────────────────────────

        events = flow.diff(
            fund_name=name,
            quarter=period,
            holdings=holdings,
            prev_positions=prev_positions,
            total_value=total_value,
            is_first=is_first,
        )

        # Attach fund style + conviction to all events
        for ev in events:
            ev.fund_style = style_data["style"]
            ev.conviction = style_data["conviction"]

        # ── Score ENTER (and ADD) events ──────────────────────────────────────

        enter_tickers = [ev.ticker for ev in events if ev.type in ("ENTER", "ADD") and ev.ticker]
        market_caps: dict[str, float | None] = {}
        if enter_tickers:
            try:
                # Batch-fetch market caps for up to 15 top enters (by value)
                top_tickers = [
                    ev.ticker for ev in
                    sorted((e for e in events if e.type in ("ENTER", "ADD") and e.ticker),
                           key=lambda e: e.value_usd, reverse=True)[:15]
                ]
                market_caps = market_mod.get_market_caps(top_tickers, limit=15)
            except Exception as ex:
                print(f"  [tina] market cap fetch failed: {ex}")

        # Build ticker → [other funds that entered same ticker this quarter] for corroboration.
        # Reads state before this fund's enters are added, so only prior-filed funds appear.
        enters_by_ticker = state_mod.get_enters_by_quarter(st, period)

        sinsi_watchlist = _load_sinsi_watchlist()
        new_positions_cur = {
            h["cusip"]: {
                "value_usd": h["value_usd"],
                "ticker":    h.get("ticker", ""),
                "name":      h["name"],
            }
            for h in holdings if h.get("cusip")
        }
        for ev in events:
            if ev.type not in ("ENTER", "ADD") or not ev.ticker:
                continue
            cap          = market_caps.get(ev.ticker)
            consensus    = [e for e in enters_by_ticker.get(ev.ticker, []) if e.get("fund") != name]
            crossref     = ev.ticker.upper() in sinsi_watchlist
            sc, fctrs    = scorer.score_enter(ev, new_positions_cur, style_data, cap, consensus, crossref)
            ev.score     = sc
            ev.factors   = fctrs

        # ── Emergent consensus tracking ───────────────────────────────────────

        enters_with_ticker = [ev for ev in events if ev.type == "ENTER" and ev.ticker]
        if enters_with_ticker and not is_first:
            state_mod.add_fund_enters(st, name, period, enters_with_ticker)
            for c in state_mod.get_new_consensus(st, min_funds=3):
                discord_bot.post_emergent_consensus(
                    c["ticker"], c["period"], c["funds"], c["total_value"]
                )
                state_mod.mark_consensus_alerted(st, c["ticker"], c["period"])

        # ── Cost-basis enrichment (top 5 ENTER events only) ──────────────────

        top_enters = [e for e in events if e.type == "ENTER" and e.ticker][:5]
        if top_enters:
            try:
                flow.enrich_cost_basis(top_enters, limit=5)
            except Exception as e:
                print(f"  [tina] cost_basis enrichment failed: {e}")

        # ── Collect SINSI crossref hits (all watchlist touches, bundled into filing alert) ──

        sinsi_hits: list[dict] = []
        sinsi_watchlist = _load_sinsi_watchlist()
        if sinsi_watchlist:
            for ev in events:
                if ev.ticker.upper() in sinsi_watchlist:
                    sinsi_hits.append({
                        "ticker":      ev.ticker,
                        "change_type": ev.type.lower(),
                        "value":       ev.prev_value_usd if ev.type == "EXIT" else ev.value_usd,
                    })

        # ── Collect new overlap ENTER events (bundled into filing alert) ──────

        overlap_enters: list[dict] = []
        ticker_funds: dict[str, list[str]] = {}
        for other_cik, other_data in st.get("funds", {}).items():
            other_name = other_data.get("name", other_cik)
            for pos in other_data.get("positions", {}).values():
                t = pos.get("ticker", "")
                if t:
                    ticker_funds.setdefault(t, [])
                    if other_name not in ticker_funds[t]:
                        ticker_funds[t].append(other_name)

        for ev in events:
            if ev.type != "ENTER" or not ev.ticker:
                continue
            holders = ticker_funds.get(ev.ticker, [])
            if len(holders) >= 2:
                total_inst = sum(
                    p.get("value_usd", 0)
                    for other_cik, other_data in st.get("funds", {}).items()
                    for p in other_data.get("positions", {}).values()
                    if p.get("ticker") == ev.ticker
                )
                overlap_enters.append({
                    "ticker":      ev.ticker,
                    "funds":       holders,
                    "total_value": total_inst,
                })

        # ── Single consolidated Discord post per filing ───────────────────────

        discord_bot.post_filing_alert(
            fund_name=name,
            cik=cik,
            period=period,
            accession=acc,
            holdings=holdings,
            events=events,
            prev_positions=prev_positions,
            total_value=total_value,
            sinsi_hits=sinsi_hits or None,
            overlap_enters=overlap_enters or None,
        )

        # ── Persist new positions ─────────────────────────────────────────────

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

        print(f"  [tina] {name}: {len(holdings)} holdings, {len(events)} notable events · style={style_data['style']}")
