"""
Discord webhook posting — all alert types post to a single channel.

Set DISCORD_WEBHOOK_URL in .env. Each alert type has a distinct color and
uses link buttons so readers can jump straight to the filing or Finviz page.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from alerts import _fmt_usd, _fmt_cap, _score_bar, _buttons  # shared formatting

load_dotenv()

_WEBHOOK          = os.environ.get("DISCORD_WEBHOOK_URL")
_WATCHLIST_WEBHOOK = os.environ.get("DISCORD_WATCHLIST_WEBHOOK_URL") or _WEBHOOK
_MSG_ID_FILE      = Path("state/watchlist_msg_id.txt")

COLORS = {
    "insider_buy":   0x9B59B6,  # purple
    "insider_sell":  0xFF6348,  # orange-red
    "cluster_buy":   0xE74C3C,  # red
    "cluster_sell":  0xA93226,  # dark crimson
    "squeeze":       0xF39C12,  # amber
    "high_borrow":   0xF1C40F,  # yellow
    "activist":      0x3498DB,  # blue
    "discovery":     0x1DB954,  # green
    "dilution":      0xC0392B,  # dark red
    "lookup":        0x00B4D8,  # cyan
}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _post(channel: str, embed: dict, components: list | None = None) -> None:
    url = _WEBHOOK
    if not url:
        return
    embed["color"]     = COLORS[channel]
    embed["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload: dict = {"embeds": [embed]}
    if components:
        payload["components"] = components
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()


def _pa_field(market: dict, notable_only: bool = False) -> dict | None:
    """Build a Price Action embed field, or None if no data."""
    from data.market import price_action_summary
    lines, notable = price_action_summary(market)
    if not lines:
        return None
    if notable_only and not notable:
        return None
    return {
        "name":   "Price Action" + (" ⚡" if notable else ""),
        "value":  "  ·  ".join(lines),
        "inline": False,
    }


# ── Alert types ────────────────────────────────────────────────────────────────

def post_insider_buy(
    ticker: str,
    filing: dict,
    details: dict,
    txn: dict,
    market: dict | None = None,
    score: int | None = None,
    sig_factors: list[str] | None = None,
) -> None:
    is_big  = txn["value"] >= 500_000
    is_plan = details["is_10b5_plan"]

    plan_line = (
        "📋 Pre-scheduled 10b5-1 plan"
        if is_plan else
        "✅ Open market purchase — not a 10b5-1 plan"
    )

    description = (
        f"**{details['owner_name']}** ({details['role']}) bought "
        f"**{_fmt_usd(txn['value'])}** of **${ticker}**\n"
        f"> {plan_line}"
    )

    fields = [
        {"name": "Shares",  "value": f"{txn['shares']:,.0f}",   "inline": True},
        {"name": "Price",   "value": f"${txn['price']:.2f}",    "inline": True},
        {"name": "Value",   "value": f"**{_fmt_usd(txn['value'])}**{'  🔥' if is_big else ''}", "inline": True},
    ]

    if score is not None:
        fields.insert(0, {
            "name":   "Significance",
            "value":  _score_bar(score),
            "inline": False,
        })
    if sig_factors:
        fields.append({
            "name":   "Why",
            "value":  "\n".join(f"• {f}" for f in sig_factors),
            "inline": False,
        })

    if market:
        pa = _pa_field(market)
        if pa:
            fields.append(pa)

    embed = {
        "author":      {"name": details["company"]},
        "title":       f"{'🔥 Large ' if is_big else ''}Insider Buy — ${ticker}",
        "url":         filing["link"],
        "description": description,
        "fields":      fields,
        "footer":      {"text": f"Filed {filing['filed']} · {filing['accession']}"},
    }
    _post("insider_buy", embed, _buttons(
        ("📄 View Filing", filing["link"]),
        ("📊 Finviz", f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def post_insider_sell(
    ticker: str,
    filing: dict,
    details: dict,
    txn: dict,
    market: dict | None = None,
    score: int | None = None,
    sig_factors: list[str] | None = None,
) -> None:
    is_plan          = details["is_10b5_plan"]
    is_exercise_dump = txn.get("is_exercise_dump", False)
    pct_sold         = txn.get("pct_sold") or 0.0

    if is_exercise_dump:
        note_line = "🔄 Exercise-and-sell — likely vesting comp monetization"
    elif is_plan:
        note_line = "📋 Pre-scheduled 10b5-1 plan — routine, less actionable"
    else:
        note_line = "⚠️ Open market sale — not a 10b5-1 plan"

    pct_str = f" ({pct_sold:.0%} of position)" if pct_sold > 0 else ""

    description = (
        f"**{details['owner_name']}** ({details['role']}) sold "
        f"**{_fmt_usd(txn['value'])}** of **${ticker}**{pct_str}\n"
        f"> {note_line}"
    )

    fields = [
        {"name": "Shares", "value": f"{txn['shares']:,.0f}",         "inline": True},
        {"name": "Price",  "value": f"${txn['price']:.2f}",          "inline": True},
        {"name": "Value",  "value": f"**{_fmt_usd(txn['value'])}**", "inline": True},
    ]

    if score is not None:
        tier_label = "Exceptional" if score >= 70 else "Strong" if score >= 50 else "Notable"
        tier_emoji = "💎" if score >= 70 else "🔥" if score >= 50 else "⭐"
        fields.insert(0, {
            "name":   f"Significance  ·  {tier_label}  {tier_emoji}",
            "value":  _score_bar(score),
            "inline": False,
        })
    if sig_factors:
        fields.append({
            "name":   "Why this matters",
            "value":  "\n".join(f"• {f}" for f in sig_factors),
            "inline": False,
        })

    if market:
        pa = _pa_field(market)
        if pa:
            fields.append(pa)

    embed = {
        "author":      {"name": details["company"]},
        "title":       f"📉 Insider Sale — ${ticker}",
        "url":         filing["link"],
        "description": description,
        "fields":      fields,
        "footer":      {"text": f"Filed {filing['filed']} · {filing['accession']}"},
    }
    _post("insider_sell", embed, _buttons(
        ("📄 View Filing", filing["link"]),
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def post_cluster_sell(ticker: str, company: str, sells: list[dict], edgar_url: str) -> None:
    total = sum(s["value"] for s in sells)

    sell_lines = "\n".join(
        f"• **{s['role']}** {s['owner_name']} — {_fmt_usd(s['value'])} on {s['date']}"
        for s in sells
    )

    embed = {
        "author":      {"name": company},
        "title":       f"🔴 Cluster Sell — ${ticker}",
        "url":         edgar_url,
        "description": (
            f"**{len(sells)} insiders** sold a combined **{_fmt_usd(total)}** "
            f"in open-market transactions within the cluster window.\n\n{sell_lines}"
        ),
        "footer": {"text": f"Cluster detected — {ticker}"},
    }
    _post("cluster_sell", embed, _buttons(
        ("📄 SEC Filings", edgar_url),
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def post_cluster_buy(ticker: str, company: str, buys: list[dict], edgar_url: str) -> None:
    total = sum(b["value"] for b in buys)

    buy_lines = "\n".join(
        f"• **{b['role']}** {b['owner_name']} — {_fmt_usd(b['value'])} on {b['date']}"
        for b in buys
    )

    embed = {
        "author":      {"name": company},
        "title":       f"🔴 Cluster Buy — ${ticker}",
        "url":         edgar_url,
        "description": (
            f"**{len(buys)} insiders** bought a combined **{_fmt_usd(total)}** "
            f"within the cluster window.\n\n{buy_lines}"
        ),
        "footer": {"text": f"Cluster detected — {ticker}"},
    }
    _post("cluster_buy", embed, _buttons(
        ("📄 SEC Filings", edgar_url),
        ("📊 Finviz", f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def post_squeeze(
    ticker: str,
    score: int,
    market: dict,
    borrow_rate: float | None,
    factors: list[str],
) -> None:
    factor_text = (
        "\n".join(f"• {f}" for f in factors)
        if factors else
        "No individual factors triggered"
    )

    short_pct  = market.get("short_pct_float")
    dtc        = market.get("short_ratio")
    float_sh   = market.get("float_shares")
    insiders   = market.get("held_pct_insiders")

    fields = [
        {"name": "Score",         "value": _score_bar(score),                                         "inline": False},
        {"name": "Short Float",   "value": f"{short_pct:.0%}"   if short_pct  else "N/A",             "inline": True},
        {"name": "Days to Cover", "value": f"{dtc:.1f}"         if dtc        else "N/A",             "inline": True},
        {"name": "Borrow Rate",   "value": f"{borrow_rate:.1%}" if borrow_rate else "N/A",            "inline": True},
        {"name": "Float",         "value": f"{float_sh/1e6:.1f}M" if float_sh else "N/A",             "inline": True},
        {"name": "Insider Own",   "value": f"{insiders:.0%}"    if insiders   else "N/A",             "inline": True},
        {"name": "Price",         "value": f"${market['price']:.2f}" if market.get("price") else "N/A", "inline": True},
        {"name": "Factors",       "value": factor_text,                                                "inline": False},
    ]

    pa = _pa_field(market)
    if pa:
        fields.append(pa)

    embed = {
        "title":  f"🟠 Squeeze Setup — ${ticker}",
        "url":    f"https://finviz.com/quote.ashx?t={ticker}",
        "fields": fields,
        "footer": {"text": "Composite short squeeze signal"},
    }
    _post("squeeze", embed, _buttons(
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
        ("💸 iborrowdesk", f"https://iborrowdesk.com/report/{ticker}"),
    ))


def post_high_borrow(ticker: str, rate: float, market: dict) -> None:
    short_pct = market.get("short_pct_float")
    float_sh  = market.get("float_shares")

    embed = {
        "title":       f"💸 High Borrow Rate — ${ticker}",
        "url":         f"https://iborrowdesk.com/report/{ticker}",
        "description": (
            f"Annualized borrow fee for **${ticker}** has spiked to **{rate:.1%}**.\n"
            "> High borrow cost makes short positions expensive to maintain."
        ),
        "fields": [
            {"name": "Borrow Rate", "value": f"**{rate:.1%}**",                                 "inline": True},
            {"name": "Short Float", "value": f"{short_pct:.0%}" if short_pct else "N/A",        "inline": True},
            {"name": "Float",       "value": f"{float_sh/1e6:.1f}M" if float_sh else "N/A",     "inline": True},
        ],
        "footer": {"text": "Source: iborrowdesk.com (Interactive Brokers data)"},
    }
    _post("high_borrow", embed, _buttons(
        ("💸 iborrowdesk", f"https://iborrowdesk.com/report/{ticker}"),
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def post_discovery(
    ticker: str,
    entry: dict,
    details: dict,
    txn: dict,
    market: dict,
    filters: dict,
    is_director_special: bool = False,
    score: int | None = None,
    sig_factors: list[str] | None = None,
) -> None:
    cap     = market.get("market_cap")
    float_  = market.get("float_shares")
    price   = market.get("price")
    short   = market.get("short_pct_float")
    sector  = market.get("sector", "")

    is_plan = details["is_10b5_plan"]
    plan_line = (
        "📋 Pre-scheduled 10b5-1 plan"
        if is_plan else
        "✅ Open market — not a 10b5-1 plan"
    )

    if is_director_special:
        title       = f"🎯 Director Buy — ${ticker}"
        extra_line  = f"\n> 🎯 Micro-cap director buy" + (f" · {short:.0%} short interest" if short else "")
    else:
        title       = f"🔍 Discovery — ${ticker}"
        extra_line  = ""

    description = (
        f"**{details['owner_name']}** ({details['role']}) bought "
        f"**{_fmt_usd(txn['value'])}** of **${ticker}** at {details['company']}.\n"
        f"> {plan_line}{extra_line}"
    )

    fields = [
        {"name": "Market Cap", "value": _fmt_cap(cap),                           "inline": True},
        {"name": "Float",      "value": f"{float_/1e6:.1f}M" if float_ else "N/A", "inline": True},
        {"name": "Price",      "value": f"${price:.2f}"       if price  else "N/A", "inline": True},
    ]

    if short:
        fields.append({"name": "Short Float", "value": f"{short:.0%}", "inline": True})
    if sector:
        fields.append({"name": "Sector",      "value": sector,          "inline": True})

    if score is not None:
        fields.append({
            "name":   "Significance",
            "value":  _score_bar(score),
            "inline": False,
        })
    if sig_factors:
        fields.append({
            "name":   "Why",
            "value":  "\n".join(f"• {f}" for f in sig_factors),
            "inline": False,
        })

    pa = _pa_field(market)
    if pa:
        fields.append(pa)

    embed = {
        "author":      {"name": details["company"]},
        "title":       title,
        "url":         entry["link"],
        "description": description,
        "fields":      fields,
        "footer":      {"text": f"Filed {entry.get('filed', '?')} · {entry['accession']}"},
    }
    _post("discovery", embed, _buttons(
        ("📄 View Filing", entry["link"]),
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def post_dilution_warning(
    ticker: str,
    company: str,
    filing: dict,
    filing_url: str,
    market: dict | None = None,
    offering: dict | None = None,
) -> None:
    form     = filing["form"]
    market   = market   or {}
    offering = offering or {}

    if form in {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7"}:
        title   = f"🚨 Active Offering — ${ticker}"
        why     = "A **424B prospectus** was filed — this means shares are being sold into the market **right now**."
    elif form in {"S-3", "S-3/A"}:
        title   = f"⚠️ Shelf Registration — ${ticker}"
        why     = "An **S-3 shelf registration** was filed — the company can sell shares at any point over the next 3 years. Watch for follow-on 424B filings."
    else:
        title   = f"⚠️ Offering Registration — ${ticker}"
        why     = f"An **{form}** registration statement was filed — a public share offering is likely coming."

    atm_note = " (at-the-market program — sold continuously into open market)" if offering.get("is_atm") else ""
    secondary_note = " (secondary — existing holders selling, **no new dilution**)" if offering.get("is_primary") is False else ""

    description = f"{why}{atm_note}{secondary_note}"

    fields = [
        {"name": "Form",  "value": form,            "inline": True},
        {"name": "Filed", "value": filing["filed"], "inline": True},
    ]

    # Offering size details
    proceeds  = offering.get("gross_proceeds")
    shares    = offering.get("shares_offered")
    price     = offering.get("price_per_share")
    mkt_price = market.get("price")
    cap       = market.get("market_cap")
    float_sh  = market.get("float_shares")
    shares_out = market.get("shares_outstanding") or (float_sh * 1.2 if float_sh else None)

    if proceeds:
        proceeds_str = _fmt_usd(proceeds)
        if cap and cap > 0:
            pct_cap = proceeds / cap
            proceeds_str += f"  ({pct_cap:.1%} of market cap)"
        fields.append({"name": "Gross Proceeds", "value": f"**{proceeds_str}**", "inline": False})

    if shares:
        shares_str = f"{shares:,.0f}"
        if shares_out and shares_out > 0:
            pct_dilution = shares / shares_out
            shares_str += f"  ({pct_dilution:.1%} of shares outstanding)"
        fields.append({"name": "Shares Offered", "value": shares_str, "inline": True})

    if price:
        price_str = f"${price:.2f}"
        if mkt_price and mkt_price > 0:
            discount = (mkt_price - price) / mkt_price
            if abs(discount) > 0.01:
                price_str += f"  ({abs(discount):.1%} {'discount' if discount > 0 else 'premium'} to market)"
        fields.append({"name": "Offering Price", "value": price_str, "inline": True})

    # Significance context
    sig_lines = []
    if proceeds and cap:
        pct = proceeds / cap
        if pct >= 0.30:
            sig_lines.append(f"⚠️ Offering is {pct:.0%} of market cap — **major dilution**")
        elif pct >= 0.10:
            sig_lines.append(f"Offering is {pct:.0%} of market cap — significant dilution")
        else:
            sig_lines.append(f"Offering is {pct:.0%} of market cap — moderate dilution")
    if price and mkt_price:
        discount = (mkt_price - price) / mkt_price
        if discount >= 0.10:
            sig_lines.append(f"⚠️ Offering priced at {discount:.0%} discount to market — aggressive terms")
        elif discount >= 0.05:
            sig_lines.append(f"Offering priced at {discount:.0%} discount to market")
    if offering.get("is_atm"):
        sig_lines.append("ATM program — dilution is gradual, not all-at-once")
    if not proceeds and not shares:
        sig_lines.append("Could not extract offering size — check the filing directly")

    if sig_lines:
        fields.append({
            "name":   "Significance",
            "value":  "\n".join(f"• {l}" for l in sig_lines),
            "inline": False,
        })

    embed = {
        "author":      {"name": company},
        "title":       title,
        "url":         filing_url,
        "description": description,
        "fields":      fields,
        "footer":      {"text": f"Accession: {filing['accession']}"},
    }
    _post("dilution", embed, _buttons(
        ("📄 View Filing", filing_url),
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
    ))


def update_watchlist_board(tickers: list[dict]) -> None:
    """
    Post or silently edit-in-place the watchlist status board.

    Uses a second webhook (DISCORD_WATCHLIST_WEBHOOK_URL) if set, otherwise
    falls back to the main alert webhook. Stores the Discord message ID in
    state/watchlist_msg_id.txt so subsequent calls edit instead of posting new.

    Ask the moderator to pin the message once after first post — after that
    the bot keeps it up to date automatically.
    """
    url = _WATCHLIST_WEBHOOK
    if not url:
        return

    embed = _build_watchlist_embed(tickers)

    # Try to edit the existing message first
    msg_id = _MSG_ID_FILE.read_text().strip() if _MSG_ID_FILE.exists() else ""
    if msg_id:
        try:
            resp = requests.patch(
                f"{url}/messages/{msg_id}",
                json={"embeds": [embed]},
                timeout=10,
            )
            if resp.status_code == 200:
                return
        except Exception:
            pass

    # No stored ID (or message was deleted) — post fresh and save the ID
    resp = requests.post(f"{url}?wait=true", json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()
    new_id = resp.json().get("id", "")
    if new_id:
        _MSG_ID_FILE.parent.mkdir(exist_ok=True)
        _MSG_ID_FILE.write_text(new_id)


def _build_watchlist_embed(tickers: list[dict]) -> dict:
    if not tickers:
        desc = "_No tickers on watchlist._\nAdd one with `python main.py --add TICKER`"
    else:
        lines = []
        for e in tickers:
            added   = e.get("added_date", "")
            ap      = e.get("added_price")
            detail  = f"added {added} at ${ap:.2f}" if (added and ap) else (f"added {added}" if added else "")
            suffix  = f"\n> {detail}" if detail else ""
            lines.append(f"**${e['ticker']}** — {e['name']}{suffix}")
        desc = "\n\n".join(lines)

    return {
        "title":       "📋 Watchlist",
        "description": desc,
        "color":       0x2C2F33,
        "footer":      {"text": f"{len(tickers)} ticker{'s' if len(tickers) != 1 else ''} · auto-updates on add/remove"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


def post_lookup(
    ticker: str,
    company: str,
    market: dict,
    squeeze_score: int,
    squeeze_factors: list[str],
    borrow_rate: float | None,
    recent_txns: list[dict],
) -> None:
    price     = market.get("price")
    cap       = market.get("market_cap")
    float_sh  = market.get("float_shares")
    short_pct = market.get("short_pct_float")
    dtc       = market.get("short_ratio")
    insiders  = market.get("held_pct_insiders")
    sector    = market.get("sector", "")

    snapshot_parts = []
    if price:      snapshot_parts.append(f"${price:.2f}")
    if cap:        snapshot_parts.append(_fmt_cap(cap))
    if float_sh:   snapshot_parts.append(f"Float {float_sh/1e6:.1f}M")
    if short_pct:  snapshot_parts.append(f"Short {short_pct:.0%}")
    if dtc:        snapshot_parts.append(f"DTC {dtc:.1f}")
    if borrow_rate: snapshot_parts.append(f"Borrow {borrow_rate:.1%}")
    if insiders:   snapshot_parts.append(f"Insider own {insiders:.0%}")
    if sector:     snapshot_parts.append(sector)

    fields = [
        {
            "name":   "Squeeze Score",
            "value":  _score_bar(squeeze_score) + (
                "\n" + "\n".join(f"• {f}" for f in squeeze_factors)
                if squeeze_factors else "\nNo squeeze factors triggered"
            ),
            "inline": False,
        }
    ]

    if recent_txns:
        lines = []
        for t in recent_txns[:8]:
            direction = "BUY 🟢" if t["code"] == "P" else "SELL 🔴"
            lines.append(
                f"`{t['date']}`  **{t['role']}** {t['owner_name']}  "
                f"{direction}  {_fmt_usd(t['value'])}"
            )
        fields.append({
            "name":   "Recent Insider Activity (30 days)",
            "value":  "\n".join(lines),
            "inline": False,
        })
    else:
        fields.append({
            "name":   "Recent Insider Activity (30 days)",
            "value":  "No Form 4 filings in the last 30 days",
            "inline": False,
        })

    pa = _pa_field(market)
    if pa:
        fields.append(pa)

    embed = {
        "author":      {"name": company},
        "title":       f"🔍 Lookup — ${ticker}",
        "description": "  ·  ".join(snapshot_parts),
        "fields":      fields,
        "footer":      {"text": "On-demand lookup · not a watchlist ticker"},
    }
    _post("lookup", embed, _buttons(
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
        ("📄 SEC Filings",  f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={ticker}&type=4&dateb=&owner=include&count=10"),
    ))


def post_activist(ticker: str, company: str, filing: dict) -> None:
    embed = {
        "author":      {"name": company},
        "title":       f"🔵 Activist Filing — ${ticker}",
        "url":         filing["link"],
        "description": (
            f"**{filing['filer']}** filed a **{filing['form_type']}** on {company}.\n"
            "> This signals a stake exceeding **5%** of outstanding shares.\n"
            "> Activist investors often push for strategic changes."
        ),
        "fields": [
            {"name": "Form",   "value": filing["form_type"], "inline": True},
            {"name": "Filer",  "value": filing["filer"],     "inline": True},
            {"name": "Filed",  "value": filing["filed"],     "inline": True},
        ],
        "footer": {"text": f"Accession: {filing['accession']}"},
    }
    _post("activist", embed, _buttons(
        ("📄 View Filing", filing["link"]),
        ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={ticker}"),
    ))
