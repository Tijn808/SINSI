"""TINA — Discord webhook posting with chart image support.

Set DISCORD_WEBHOOK_URL in tina/.env.
"""

from __future__ import annotations

import io
import json
import os
from typing import TYPE_CHECKING

import requests
from dotenv import load_dotenv

import config
import charts
import fund_type as fund_type_mod

if TYPE_CHECKING:
    from flow import FlowEvent

load_dotenv()

_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")
_JHEADERS = {"Content-Type": "application/json"}



def _post_json(payload: dict) -> None:
    if not _WEBHOOK:
        print("[tina/discord] DISCORD_WEBHOOK_URL not set — skipping")
        return
    try:
        requests.post(_WEBHOOK, json=payload, headers=_JHEADERS, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[tina/discord] Webhook error: {e}")


def _post_file(buf: io.BytesIO, filename: str, embed: dict) -> None:
    if not _WEBHOOK:
        return
    try:
        requests.post(
            _WEBHOOK,
            files={"file": (filename, buf, "image/png")},
            data={"payload_json": json.dumps({"embeds": [embed]})},
            timeout=30,
        ).raise_for_status()
    except Exception as e:
        print(f"[tina/discord] File post error: {e}")


def _usd(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# ── Filing alert ──────────────────────────────────────────────────────────────

def post_filing_alert(
    fund_name: str,
    cik: str,
    period: str,
    accession: str,
    holdings: list[dict],
    events: list[FlowEvent],
    prev_positions: dict,
    total_value: float,
    sinsi_hits: list[dict] | None = None,
    overlap_enters: list[dict] | None = None,
) -> None:
    """Single consolidated embed for an entire 13F filing — no per-position spam.

    sinsi_hits:     [{ticker, change_type, value}] — SINSI watchlist crossrefs
    overlap_enters: [{ticker, funds, total_value}] — ENTER events now held by 2+ funds
    """
    is_first  = not prev_positions
    edgar_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={cik}&type=13F-HR&dateb=&owner=include&count=1"
    )

    if is_first:
        title = f"📋 Initial Filing — {fund_name}"
        desc  = f"First 13F detected · **{len(holdings)}** positions · **{_usd(total_value)}** AUM"
    else:
        enters = [e for e in events if e.type == "ENTER"]
        adds   = [e for e in events if e.type == "ADD"]
        trims  = [e for e in events if e.type == "TRIM"]
        exits  = [e for e in events if e.type == "EXIT"]
        title  = f"📋 New 13F — {fund_name}"
        desc   = (
            f"**{period}** · {_usd(total_value)} AUM\n"
            f"📈 {len(enters)} new · ➕ {len(adds)} added · "
            f"➖ {len(trims)} trimmed · 🚪 {len(exits)} exited"
        )

    fields = []

    if not is_first:
        # New positions (top 8 by significance score, then value)
        all_enters = [e for e in events if e.type == "ENTER"]
        top_enters = sorted(
            all_enters,
            key=lambda e: (getattr(e, "score", 0) or 0, e.value_usd), reverse=True,
        )[:8]
        if top_enters:
            enter_lines = []
            for e in top_enters:
                sc   = getattr(e, "score", None)
                line = f"**${e.ticker or e.name[:12]}** — {_usd(e.value_usd)} ({e.weight_pct:.1f}%)"
                if sc:
                    line += f" ★ **{sc}**"
                factors = getattr(e, "factors", None)
                if factors:
                    line += f"\n*{factors[0]}*"
                enter_lines.append(line)
            remainder = len(all_enters) - len(top_enters)
            if remainder > 0:
                enter_lines.append(f"*+{remainder} more*")
            fields.append({"name": "🆕 New Positions", "value": "\n".join(enter_lines), "inline": False})

        # Exits (top 5 by prior value)
        top_exits = sorted(
            [e for e in events if e.type == "EXIT"],
            key=lambda e: e.prev_value_usd, reverse=True,
        )[:5]
        if top_exits:
            exit_lines = [
                f"**${e.ticker or e.name[:12]}** — was {_usd(e.prev_value_usd)} ({e.prev_weight_pct:.1f}%)"
                for e in top_exits
            ]
            remainder = len([e for e in events if e.type == "EXIT"]) - len(top_exits)
            if remainder > 0:
                exit_lines.append(f"*+{remainder} more*")
            fields.append({"name": "🚪 Exits", "value": "\n".join(exit_lines), "inline": False})

        # Notable size changes (top 3 adds + top 3 trims combined)
        big_adds  = sorted([e for e in events if e.type == "ADD"],  key=lambda e: abs(e.delta_usd), reverse=True)[:3]
        big_trims = sorted([e for e in events if e.type == "TRIM"], key=lambda e: abs(e.delta_usd), reverse=True)[:3]
        size_events = big_adds + big_trims
        if size_events:
            size_lines = []
            for e in size_events:
                sign = "+" if e.delta_usd >= 0 else ""
                size_lines.append(
                    f"{'➕' if e.type == 'ADD' else '➖'} **${e.ticker or e.name[:12]}** "
                    f"{sign}{_usd(e.delta_usd)} ({sign}{e.delta_pct:.0f}%)"
                )
            fields.append({"name": "📐 Sizing Changes", "value": "\n".join(size_lines), "inline": False})

        # SINSI crossref hits (tickers on SINSI watchlist)
        if sinsi_hits:
            sinsi_lines = []
            for h in sinsi_hits[:6]:
                icon = {"enter": "📈", "exit": "🚪", "add": "➕", "trim": "➖"}.get(h["change_type"], "•")
                sinsi_lines.append(f"{icon} **${h['ticker']}** — {h['change_type']} · {_usd(h['value'])}")
            fields.append({"name": "🔗 SINSI Watchlist Crossref", "value": "\n".join(sinsi_lines), "inline": False})

        # New multi-fund overlap (ENTER events now held by 2+ funds)
        if overlap_enters:
            ov_lines = [
                f"**${o['ticker']}** — {len(o['funds'])} funds · {_usd(o['total_value'])}"
                for o in sorted(overlap_enters, key=lambda x: x["total_value"], reverse=True)[:5]
            ]
            fields.append({"name": "📍 New Overlap", "value": "\n".join(ov_lines), "inline": False})
    else:
        # First filing: just show top 10 holdings
        top10 = sorted(holdings, key=lambda h: h["value_usd"], reverse=True)[:10]
        fields.append({"name": "Top Holdings", "value": "\n".join(
            f"**{h.get('ticker') or h['name'][:12]}** — {_usd(h['value_usd'])}"
            for h in top10
        ), "inline": False})

    fields.append({"name": "Filing", "value": f"[View on EDGAR]({edgar_url})", "inline": False})

    _post_json({"embeds": [{
        "title":       title,
        "description": desc,
        "color":       config.COLOR_INFO,
        "fields":      fields,
        "footer":      {"text": f"TINA 13F Scanner · {accession}"},
    }]})

    # Portfolio pie chart as a follow-up image (still one message)
    if holdings:
        chart_buf = charts.pie_chart(fund_name, period, holdings)
        _post_file(chart_buf, "holdings.png", {
            "title": f"Portfolio — {fund_name} · {period}",
            "color": config.COLOR_INFO,
            "image": {"url": "attachment://holdings.png"},
        })


# ── Flow event alert (shared renderer) ───────────────────────────────────────

def _post_flow_event(ev: FlowEvent) -> None:
    """Post a single FlowEvent embed. Used by filing alerts and exit digests."""
    ticker   = ev.ticker or ev.name[:15]
    ref_val  = ev.prev_value_usd if ev.type == "EXIT" else ev.value_usd
    fields   = []

    if ev.type == "EXIT":
        fields.append({"name": "Position Closed", "value": _usd(ev.prev_value_usd), "inline": True})
        fields.append({"name": "Was", "value": f"{ev.prev_weight_pct:.2f}% of portfolio", "inline": True})
    else:
        fields.append({"name": "Position", "value": _usd(ev.value_usd), "inline": True})
        fields.append({"name": "Portfolio weight", "value": f"{ev.weight_pct:.2f}%", "inline": True})
        if ev.type in ("ADD", "TRIM") and ev.prev_value_usd:
            sign = "+" if ev.delta_usd >= 0 else ""
            fields.append({
                "name":   "Change",
                "value":  f"{sign}{_usd(ev.delta_usd)} ({sign}{ev.delta_pct:.0f}%)",
                "inline": True,
            })
        if ev.weight_change and ev.type != "ENTER":
            sign = "+" if ev.weight_change >= 0 else ""
            fields.append({
                "name":   "Weight shift",
                "value":  f"{sign}{ev.weight_change:.2f}pp",
                "inline": True,
            })

    if ev.factors:
        fields.append({"name": "Signal", "value": " · ".join(ev.factors), "inline": False})

    if ev.cost_basis:
        cb       = ev.cost_basis
        verdict  = {"early": "🟢 Near entry", "moderate": "🟡 Moderate run", "chasing": "🔴 Already ran"}.get(cb["verdict"], "")
        sign_avg = "+" if cb["pct_from_avg"] >= 0 else ""
        sign_qe  = "+" if cb["pct_from_q_end"] >= 0 else ""
        fields.append({
            "name": f"Entry range ({ev.quarter})",
            "value": (
                f"Quarter low/high: **${cb['low']:.2f} – ${cb['high']:.2f}**\n"
                f"Avg close: **${cb['avg_close']:.2f}** · Quarter-end: **${cb['close_end']:.2f}**\n"
                f"Now: **${cb['current_price']:.2f}** "
                f"({sign_avg}{cb['pct_from_avg']:.0f}% from avg, "
                f"{sign_qe}{cb['pct_from_q_end']:.0f}% from Q-end)\n"
                f"{verdict}"
            ),
            "inline": False,
        })

    style_label = fund_type_mod.label(getattr(ev, "fund_style", "unknown"))
    desc        = f"**{ev.fund_name}** · {style_label}"

    _post_json({"embeds": [{
        "title":       f"{ev.label()} — ${ticker}",
        "description": desc,
        "color":       ev.color(),
        "fields":      fields,
        "footer":      {"text": f"Score {ev.score}/100 · ×{getattr(ev, 'conviction', 1.0):.1f} conviction · {ev.quarter}"},
    }]})


# ── Performance chart (on-demand) ─────────────────────────────────────────────

def post_performance_chart(fund_name: str, quarter: str, positions: list[dict]) -> None:
    """positions: [{ticker, name, entry_price, current_price, value_usd}]"""
    buf = charts.performance_chart(fund_name, quarter, positions)
    if not buf:
        return
    _post_file(buf, "performance.png", {
        "title": f"Performance Since {quarter} — {fund_name}",
        "color": config.COLOR_INFO,
        "image": {"url": "attachment://performance.png"},
    })


# ── Weekly summary ─────────────────────────────────────────────────────────────

def post_weekly_summary(fund_performances: list[dict]) -> None:
    buf = charts.summary_chart(fund_performances)
    if not buf:
        return

    lines = [
        f"**{f['name']}** — {'+' if f['weighted_return']>=0 else ''}{f['weighted_return']:.1f}% · {_usd(f['total_value_usd'])}"
        for f in sorted(fund_performances, key=lambda x: x["weighted_return"], reverse=True)
    ]
    _post_file(buf, "summary.png", {
        "title":       "📊 Weekly Institutional Performance Summary",
        "description": "\n".join(lines) or "No data yet.",
        "color":       config.COLOR_INFO,
        "image":       {"url": "attachment://summary.png"},
        "footer":      {"text": "TINA — Tracking INstitutional Activity"},
    })


# ── Auto: emergent consensus alert ───────────────────────────────────────────

def post_emergent_consensus(
    ticker: str,
    period: str,
    funds: list[dict],
    total_value: float,
) -> None:
    """Posted when 2+ followed funds independently entered the same ticker in the same quarter."""
    lines = [
        f"• **{e['fund']}** — {_usd(e['value'])} ({e.get('weight_pct', 0):.2f}% of book)"
        for e in sorted(funds, key=lambda x: x["value"], reverse=True)
    ]
    n = len(funds)
    _post_json({"embeds": [{
        "title":       f"🧠 Emergent Consensus — ${ticker}",
        "description": (
            f"**{n}** followed fund{'s' if n > 1 else ''} independently opened **${ticker}** "
            f"in the same quarter (**{period}**):\n\n"
            + "\n".join(lines)
            + f"\n\n**{_usd(total_value)}** combined exposure"
        ),
        "color":  0x9b59b6,
        "footer": {"text": "TINA — emergent consensus · this quarter's conviction signal"},
    }]})


# ── Auto: new bets digest ─────────────────────────────────────────────────────

def post_new_bets(fund_name: str, quarter: str, enters: list[FlowEvent]) -> None:
    """Auto-posted when a new 13F introduces brand-new positions (ENTER events)."""
    if not enters:
        return
    top = sorted(enters, key=lambda e: e.value_usd, reverse=True)[:10]
    lines = [
        f"**${e.ticker or e.name[:15]}** — {_usd(e.value_usd)} · {e.weight_pct:.2f}% of book"
        for e in top
    ]
    _post_json({"embeds": [{
        "title":       f"🆕 New Bets — {fund_name}",
        "description": f"**{len(enters)}** new position(s) in the {quarter} filing:\n" + "\n".join(lines),
        "color":       config.COLOR_NEW,
        "footer":      {"text": f"TINA · {quarter}"},
    }]})


# ── Auto: new overlap alert ───────────────────────────────────────────────────

def post_new_overlap(ticker: str, funds: list[str], total_value: float) -> None:
    """Auto-posted when a ticker is now held by 2+ followed funds for the first time."""
    _post_json({"embeds": [{
        "title":       f"📍 New Institutional Overlap — ${ticker}",
        "description": (
            f"**{len(funds)}** followed funds now hold **${ticker}**:\n"
            + "\n".join(f"• {f}" for f in funds)
            + f"\n\n**{_usd(total_value)}** total institutional exposure"
        ),
        "color":       config.COLOR_INCREASE,
        "footer":      {"text": "TINA — overlap detected on new 13F filing"},
    }]})


# ── SINSI cross-reference alert ───────────────────────────────────────────────

def post_sinsi_crossref(
    ticker: str,
    fund_name: str,
    change_type: str,
    position_value: float,
    quarter: str,
) -> None:
    """Alert when a 13F position overlaps with the SINSI watchlist."""
    _post_json({"embeds": [{
        "title":       f"🔗 Institutional + Insider Overlap — ${ticker}",
        "description": (
            f"**${ticker}** is on the SINSI watchlist **and** "
            f"**{fund_name}** just reported a **{change_type}** in their 13F.\n"
            f"Position: **{_usd(position_value)}** · Quarter: {quarter}"
        ),
        "color":  0xfee75c,  # yellow — cross-signal
        "footer": {"text": "TINA × SINSI cross-reference"},
    }]})


# ── Auto: pile-in alert ───────────────────────────────────────────────────────

def post_pile_in_alert(
    ticker: str,
    new_fund: str,
    other_funds: list[str],
    total_value: float,
    market_cap: float | None,
    period: str,
) -> None:
    """Auto-posted when a new 13F creates multi-fund convergence on a small-cap stock."""
    all_funds = [new_fund] + [f for f in other_funds if f != new_fund]
    cap_str = ""
    if market_cap:
        cap_str = f" · **${market_cap/1e6:.0f}M** cap" if market_cap < 1e9 else f" · **${market_cap/1e9:.1f}B** cap"

    _post_json({"embeds": [{
        "title":       f"📊 Small-Cap Pile-In — ${ticker}",
        "description": (
            f"**{len(all_funds)}** followed funds converging on **${ticker}**{cap_str}\n\n"
            + "\n".join(f"• {f}" + (" ← new" if f == new_fund else "") for f in all_funds)
            + f"\n\n**{_usd(total_value)}** total institutional exposure"
        ),
        "color":  0x27ae60,
        "footer": {"text": f"TINA — pile-in signal · {period}"},
    }]})


# ── Status ────────────────────────────────────────────────────────────────────

def post_status(msg: str) -> None:
    _post_json({"embeds": [{"title": msg, "color": config.COLOR_INFO}]})
