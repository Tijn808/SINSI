"""Manual test runner for all TINA Discord outputs.

Simulates what each slash command would post, using real state data.
Run from the tina/ directory:
  ../.venv/bin/python test_commands.py
  ../.venv/bin/python test_commands.py funds     # specific section only
"""

import json
import sys
import time
from pathlib import Path

if not Path("config.py").exists():
    print("ERROR: Run from the tina/ directory.", file=sys.stderr)
    sys.exit(1)

import discord_bot as wb   # webhook poster
import charts
import flow
import fund_type as ft
import state as state_mod
from data import market

SECTION = sys.argv[1] if len(sys.argv) > 1 else "all"
st      = state_mod.load()
FUNDS   = json.loads(Path("funds.json").read_text()) if Path("funds.json").exists() else []

# Pick two real funds from state for examples
_fund_ciks  = [cik for cik in st.get("funds", {}) if st["funds"][cik].get("positions")]
DEMO_CIK    = _fund_ciks[0] if _fund_ciks else None
DEMO_CIK2   = _fund_ciks[1] if len(_fund_ciks) > 1 else None
DEMO_FUND   = st["funds"][DEMO_CIK]  if DEMO_CIK  else {}
DEMO_FUND2  = st["funds"][DEMO_CIK2] if DEMO_CIK2 else {}


def section(name: str) -> bool:
    return SECTION in ("all", name)


def divider(label: str) -> None:
    print(f"\n{'='*60}\n  {label}\n{'='*60}")
    wb._post_json({"content": f"**—— /{label} ——**"})
    time.sleep(0.5)


def _usd(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# ── /funds ────────────────────────────────────────────────────────────────────

if section("funds"):
    divider("funds")
    lines = []
    for f in FUNDS:
        fd      = st.get("funds", {}).get(f["cik"], {})
        quarter = fd.get("latest_quarter", "not yet scanned")
        n       = len(fd.get("positions", {}))
        sd      = fd.get("style", {})
        style   = sd.get("label", "?")
        conv    = sd.get("conviction", "?")
        lines.append(
            f"**{f['name']}** · CIK `{f['cik']}`\n"
            f"  {style} · ×{conv} conviction · {n} positions · {quarter}"
        )
    wb._post_json({"embeds": [{
        "title": f"📋 Following {len(FUNDS)} Fund(s)",
        "description": "\n\n".join(lines) or "None followed yet.",
        "color": 0x3498db,
    }]})


# ── /tag ─────────────────────────────────────────────────────────────────────

if section("tag"):
    divider("tag")
    lines = []
    for cik, fd in st.get("funds", {}).items():
        sd = fd.get("style", {})
        if not sd:
            continue
        lines.append(
            f"**{fd['name']}**\n"
            f"  {sd['label']} · {sd['n_positions']} positions · "
            f"top-10: {sd['top10_pct']}% · HHI: {sd['hhi']} · ×{sd['conviction']} conviction"
        )
    wb._post_json({"embeds": [{
        "title": "🏷️ Fund Style Tags",
        "description": "\n\n".join(lines) or "No style data yet.",
        "color": 0x95a5a6,
        "footer": {"text": "HHI > 500 = very concentrated · use /tag fund:X style:Y to override"},
    }]})


# ── /holdings ────────────────────────────────────────────────────────────────

if section("holdings") and DEMO_CIK:
    divider("holdings")
    holdings = [
        {"name": p["name"], "ticker": p.get("ticker", ""), "value_usd": p["value_usd"]}
        for p in DEMO_FUND["positions"].values()
        if p.get("value_usd", 0) > 0
    ]
    name    = DEMO_FUND.get("name", DEMO_CIK)
    quarter = DEMO_FUND.get("latest_quarter", "?")
    print(f"  Charting {len(holdings)} positions for {name}")
    buf = charts.pie_chart(name, quarter, holdings)
    wb._post_file(buf, "holdings.png", {
        "title": f"Portfolio Allocation — {name}",
        "color": 0x3498db,
        "image": {"url": "attachment://holdings.png"},
        "footer": {"text": quarter},
    })


# ── /peek ────────────────────────────────────────────────────────────────────

if section("peek") and DEMO_CIK:
    divider("peek  (min_pct=1.0, top=15)")
    holdings = [
        {"name": p["name"], "ticker": p.get("ticker", ""), "value_usd": p["value_usd"]}
        for p in DEMO_FUND["positions"].values()
        if p.get("value_usd", 0) > 0
    ]
    total   = sum(h["value_usd"] for h in holdings) or 1
    filtered = [h for h in holdings if h["value_usd"] / total * 100 >= 1.0][:15]
    name    = DEMO_FUND.get("name", DEMO_CIK)
    quarter = DEMO_FUND.get("latest_quarter", "?")
    print(f"  Peek: {len(filtered)} positions after min_pct=1%")
    buf = charts.pie_chart(name, quarter, filtered)
    wb._post_file(buf, "peek.png", {
        "title": f"Portfolio Peek — {name}  (≥1% positions)",
        "color": 0x3498db,
        "image": {"url": "attachment://peek.png"},
        "footer": {"text": f"{quarter} · top 15 · min 1% weight"},
    })


# ── /pie ─────────────────────────────────────────────────────────────────────

if section("pie") and DEMO_CIK:
    divider("pie  (max_cap=50B)")
    # Use name as label fallback when tickers aren't resolved yet
    holdings = [
        {"name": p.get("ticker") or p["name"], "ticker": p.get("ticker", ""), "value_usd": p["value_usd"]}
        for p in DEMO_FUND["positions"].values()
        if p.get("value_usd", 0) > 0
    ]
    # Without live market caps just show the chart as-is (market cap filter skipped in test)
    name    = DEMO_FUND.get("name", DEMO_CIK)
    quarter = DEMO_FUND.get("latest_quarter", "?")
    top20   = sorted(holdings, key=lambda h: h["value_usd"], reverse=True)[:20]
    print(f"  Pie: {len(top20)} positions")
    buf = charts.pie_chart(name, quarter, top20)
    wb._post_file(buf, "pie.png", {
        "title": f"Holdings Pie — {name}",
        "description": f"Top 20 positions by size · no market-cap filter in test",
        "color": 0x2ecc71,
        "image": {"url": "attachment://pie.png"},
        "footer": {"text": f"{quarter} · /pie max_cap=50B would filter here"},
    })


# ── /overlap ─────────────────────────────────────────────────────────────────

if section("overlap"):
    divider("overlap")
    ticker_funds: dict[str, list] = {}
    for cik, fd in st.get("funds", {}).items():
        fname = fd.get("name", cik)
        for pos in fd.get("positions", {}).values():
            t = pos.get("ticker", "")
            v = pos.get("value_usd", 0)
            if t and v:
                ticker_funds.setdefault(t, [])
                ticker_funds[t].append({"fund": fname, "value": v})

    overlap = {t: entries for t, entries in ticker_funds.items() if len(entries) >= 2}
    ranked  = sorted(overlap.items(), key=lambda x: sum(e["value"] for e in x[1]), reverse=True)[:15]
    lines   = []
    for ticker, entries in ranked:
        total     = sum(e["value"] for e in entries)
        fund_list = " · ".join(e["fund"][:18] for e in sorted(entries, key=lambda x: x["value"], reverse=True))
        lines.append(f"**${ticker}** — {len(entries)} funds · {_usd(total)}\n  ↳ {fund_list}")

    wb._post_json({"embeds": [{
        "title": f"📍 Institutional Overlap — {len(ranked)} shared positions",
        "description": "\n\n".join(lines) or "No overlapping positions.",
        "color": 0x9b59b6,
        "footer": {"text": "Tickers held by 2+ followed funds"},
    }]})


# ── /consensus ────────────────────────────────────────────────────────────────

if section("consensus"):
    divider("consensus")
    ticker_data: dict[str, dict] = {}
    for cik, fd in st.get("funds", {}).items():
        fname = fd.get("name", cik)
        for pos in fd.get("positions", {}).values():
            t = pos.get("ticker", "")
            v = pos.get("value_usd", 0)
            if t and v:
                if t not in ticker_data:
                    ticker_data[t] = {"ticker": t, "total": 0, "count": 0, "funds": []}
                ticker_data[t]["total"]  += v
                ticker_data[t]["count"]  += 1
                ticker_data[t]["funds"].append(fname)

    ranked = sorted(ticker_data.values(), key=lambda x: x["total"], reverse=True)[:20]
    # Rename keys to match consensus_chart's expected schema
    chart_data = [{"ticker": r["ticker"], "total_value_usd": r["total"], "fund_count": r["count"], "funds": r["funds"]} for r in ranked]
    lines = [
        f"**${r['ticker']}** — {r['count']} funds · {_usd(r['total'])}"
        for r in ranked
    ]
    buf = charts.consensus_chart(chart_data)
    if buf:
        wb._post_file(buf, "consensus.png", {
            "title": "📊 Top Holdings Across All Followed Funds",
            "color": 0x3498db,
            "image": {"url": "attachment://consensus.png"},
            "footer": {"text": "Sorted by total institutional $ across followed funds"},
        })
    else:
        wb._post_json({"embeds": [{
            "title": "📊 Consensus Holdings",
            "description": "\n".join(lines),
            "color": 0x3498db,
        }]})


# ── /tag (detail for one fund) ────────────────────────────────────────────────

if section("tag-detail") and DEMO_CIK:
    divider("tag-detail  (Berkshire deep-dive)")
    berk_cik = "1067983"
    fd = st["funds"].get(berk_cik, DEMO_FUND)
    sd = fd.get("style", {})
    wb._post_json({"embeds": [{
        "title": f"🏷️ {fd['name']} — Style Detail",
        "fields": [
            {"name": "Style",         "value": sd.get("label", "?"),             "inline": True},
            {"name": "Conviction",    "value": f"×{sd.get('conviction','?')}",   "inline": True},
            {"name": "Positions",     "value": str(sd.get("n_positions", "?")),  "inline": True},
            {"name": "Top-10 weight", "value": f"{sd.get('top10_pct','?')}%",   "inline": True},
            {"name": "HHI",           "value": str(sd.get("hhi", "?")),          "inline": True},
            {"name": "Manual override","value": str(sd.get("overridden", False)),"inline": True},
        ],
        "color": 0x95a5a6,
        "description": (
            "HHI (Herfindahl-Hirschman Index): sum of squared position weights.\n"
            "> 500 = highly concentrated · 100-500 = focused · < 100 = diversified/quant\n\n"
            "Conviction multiplier scales signal quality in alerts and emergent consensus."
        ),
        "footer": {"text": "Use /tag fund:X style:Y to override the auto-detected style"},
    }]})


# ── /exits ───────────────────────────────────────────────────────────────────

if section("exits"):
    divider("exits  (from state diff)")
    all_exits = []
    for f in FUNDS:
        cik       = f["cik"]
        fund_name = st.get("funds", {}).get(cik, {}).get("name") or f["name"]
        events    = flow.diff_from_state(st, cik, fund_name)
        all_exits.extend([e for e in events if e.type in ("EXIT", "TRIM")])

    if not all_exits:
        wb._post_json({"content": "> `/exits` — No exits or trims found (needs 2+ quarters of data per fund)"})
    else:
        all_exits.sort(key=lambda e: e.prev_value_usd, reverse=True)
        lines = []
        for e in all_exits[:15]:
            ticker = f"${e.ticker}" if e.ticker else e.name[:15]
            if e.type == "EXIT":
                lines.append(f"🚪 **{ticker}** — {e.fund_name} fully exited · was {_usd(e.prev_value_usd)} ({e.prev_weight_pct:.2f}% of book)")
            else:
                lines.append(f"➖ **{ticker}** — {e.fund_name} trimmed {abs(e.delta_pct):.0f}% · {_usd(e.prev_value_usd)} → {_usd(e.value_usd)}")
        wb._post_json({"embeds": [{
            "title": "📤 Smart Money Leaving",
            "description": "\n".join(lines),
            "color": 0xe74c3c,
            "footer": {"text": "Based on latest vs. prior 13F quarter · TINA"},
        }]})


# ── /emerging ────────────────────────────────────────────────────────────────

if section("emerging"):
    divider("emerging  (min_funds=2)")
    ticker_quarter: dict[str, dict] = {}
    for f in FUNDS:
        cik       = f["cik"]
        fund_name = st.get("funds", {}).get(cik, {}).get("name") or f["name"]
        events    = flow.diff_from_state(st, cik, fund_name)
        for ev in events:
            if ev.type != "ENTER" or not ev.ticker:
                continue
            key = f"{ev.ticker}:{ev.quarter}"
            if key not in ticker_quarter:
                ticker_quarter[key] = {"ticker": ev.ticker, "quarter": ev.quarter, "entries": []}
            if not any(e["fund"] == fund_name for e in ticker_quarter[key]["entries"]):
                ticker_quarter[key]["entries"].append({"fund": fund_name, "value": ev.value_usd, "weight_pct": ev.weight_pct})

    consensus = [v for v in ticker_quarter.values() if len(v["entries"]) >= 2]
    if not consensus:
        wb._post_json({"content": "> `/emerging` — No emergent consensus yet (needs 2+ quarters per fund so ENTER events can be detected)"})
    else:
        consensus.sort(key=lambda x: (len(x["entries"]), sum(e["value"] for e in x["entries"])), reverse=True)
        lines = []
        for c in consensus[:10]:
            total = sum(e["value"] for e in c["entries"])
            parts = " · ".join(f"{e['fund'][:16]} {_usd(e['value'])} ({e['weight_pct']:.2f}%)" for e in c["entries"])
            lines.append(f"**${c['ticker']}** — {len(c['entries'])} funds · {_usd(total)} combined · {c['quarter']}\n  ↳ {parts}")
        wb._post_json({"embeds": [{
            "title": f"🧠 Emergent Consensus — {len(consensus)} signal(s)",
            "description": "\n\n".join(lines),
            "color": 0x9b59b6,
            "footer": {"text": "Independent entries in the same quarter — not just co-ownership"},
        }]})


# ── /ticker ───────────────────────────────────────────────────────────────────

if section("ticker"):
    # Find a ticker held by multiple funds
    divider("ticker  (example: NVDA or first multi-fund ticker)")
    ticker_map: dict[str, list] = {}
    for cik, fd in st.get("funds", {}).items():
        fname = fd.get("name", cik)
        for pos in fd.get("positions", {}).values():
            t = pos.get("ticker", "")
            if t:
                ticker_map.setdefault(t, []).append({"fund": fname, "value": pos.get("value_usd", 0)})
    best = max(ticker_map.items(), key=lambda x: (len(x[1]), sum(e["value"] for e in x[1])), default=(None, []))
    if best[0]:
        tk    = best[0]
        lines = [f"**{e['fund']}** — {_usd(e['value'])}" for e in sorted(best[1], key=lambda x: x["value"], reverse=True)]
        wb._post_json({"embeds": [{
            "title": f"🔍 ${tk} — Held by {len(best[1])} Followed Fund(s)",
            "description": "\n".join(lines),
            "color": 0x3498db,
            "footer": {"text": "TINA — /ticker command"},
        }]})
    else:
        wb._post_json({"content": "> `/ticker` — No ticker data yet (CUSIPs not resolved yet)"})


# ── /pile-in ─────────────────────────────────────────────────────────────────

if section("pile-in"):
    divider("pile-in  (multi-fund overlap, no market cap filter in test)")
    ticker_map: dict[str, dict] = {}
    for cik, fd in st.get("funds", {}).items():
        fname = fd.get("name", cik)
        for pos in fd.get("positions", {}).values():
            t = pos.get("ticker", "")
            v = pos.get("value_usd", 0)
            if t and v:
                if t not in ticker_map:
                    ticker_map[t] = {"ticker": t, "total_value": 0, "fund_count": 0, "funds": []}
                ticker_map[t]["total_value"] += v
                ticker_map[t]["fund_count"]  += 1
                ticker_map[t]["funds"].append(fname)

    multi = [d for d in ticker_map.values() if d["fund_count"] >= 2]
    if not multi:
        wb._post_json({"content": "> `/pile-in` — No overlapping tickers yet (ticker resolution still in progress)"})
    else:
        multi.sort(key=lambda x: (x["fund_count"], x["total_value"]), reverse=True)
        results = [{**r, "market_cap": 0} for r in multi[:15]]
        buf = charts.pile_in_chart(results)
        if buf:
            wb._post_file(buf, "pile_in.png", {
                "title": f"🏔️ Pile-In — {len(results)} stocks held by 2+ funds",
                "description": f"No market-cap filter in this test. Use `/pile-in max_cap:500M` in Discord.",
                "color": 0xe67e22,
                "image": {"url": "attachment://pile_in.png"},
                "footer": {"text": "TINA /pile-in — sorted by fund count then total value"},
            })
        else:
            lines = [f"**${r['ticker']}** — {r['fund_count']} funds · {_usd(r['total_value'])}" for r in results]
            wb._post_json({"embeds": [{"title": "🏔️ Pile-In", "description": "\n".join(lines), "color": 0xe67e22}]})


# ── /entry ────────────────────────────────────────────────────────────────────

if section("entry"):
    divider("entry  (cost-basis overlay for recent ENTER events)")
    all_enters = []
    for f in FUNDS:
        cik       = f["cik"]
        fund_name = st.get("funds", {}).get(cik, {}).get("name") or f["name"]
        events    = flow.diff_from_state(st, cik, fund_name)
        all_enters.extend([e for e in events if e.type in ("ENTER", "ADD") and e.ticker])

    if not all_enters:
        wb._post_json({"content": "> `/entry` — No ENTER events yet (needs 2+ quarters of data per fund)"})
    else:
        all_enters.sort(key=lambda e: e.value_usd, reverse=True)
        to_enrich = all_enters[:5]
        print(f"  Enriching cost-basis for {len(to_enrich)} ENTER events…")
        flow.enrich_cost_basis(to_enrich, limit=5, delay=0.5)

        lines = []
        for e in to_enrich:
            if e.cost_basis:
                cb      = e.cost_basis
                verdict = {"early": "🟢 Near entry", "moderate": "🟡 Moderate run", "chasing": "🔴 Already ran"}.get(cb["verdict"], "")
                sign    = "+" if cb["pct_from_avg"] >= 0 else ""
                lines.append(
                    f"**${e.ticker}** — {e.fund_name} [{ft.label(e.fund_style)}]\n"
                    f"  Entry range: ${cb['low']:.2f}–${cb['high']:.2f} · avg ${cb['avg_close']:.2f}\n"
                    f"  Now: ${cb['current_price']:.2f} ({sign}{cb['pct_from_avg']:.0f}% from avg) {verdict}"
                )
            else:
                lines.append(f"**${e.ticker}** — {e.fund_name} — *price data unavailable*")

        wb._post_json({"embeds": [{
            "title": "📌 Entry Cost-Basis Overlay",
            "description": "\n\n".join(lines) if lines else "No data.",
            "color": 0x27ae60,
            "footer": {"text": "TINA /entry — were you early or chasing?"},
        }]})


# ── Done ──────────────────────────────────────────────────────────────────────

print("\n✓ All tests posted to Discord.")
