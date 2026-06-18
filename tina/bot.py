"""TINA Discord slash command bot.

Requires DISCORD_BOT_TOKEN and optionally TINA_BOT_CHANNEL_ID in tina/.env.
Run from the tina/ directory alongside main.py.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

if not Path("config.py").exists():
    print("ERROR: Run this from the tina/ directory.", file=sys.stderr)
    print("  cd tina && ../.venv/bin/python bot.py", file=sys.stderr)
    sys.exit(1)

import discord
from discord import app_commands
from dotenv import load_dotenv

import config
import fund_type as fund_type_mod
import state as state_mod
import discord_bot as tina_discord
import charts
import flow
from data import edgar13f, market

load_dotenv()

TOKEN           = os.environ.get("DISCORD_BOT_TOKEN")
_raw_ch         = os.environ.get("TINA_BOT_CHANNEL_ID", "").strip()
ALLOWED_CHANNEL = int(_raw_ch) if _raw_ch else None
_FUNDS_FILE     = Path("funds.json")

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


def _load_funds() -> list[dict]:
    if _FUNDS_FILE.exists():
        try:
            return json.loads(_FUNDS_FILE.read_text())
        except Exception:
            pass
    return []


def _save_funds(funds: list[dict]) -> None:
    _FUNDS_FILE.write_text(json.dumps(funds, indent=2))


def _in_ch(i: discord.Interaction) -> bool:
    return ALLOWED_CHANNEL is None or i.channel_id == ALLOWED_CHANNEL


# ── /follow ───────────────────────────────────────────────────────────────────

@tree.command(name="follow", description="Follow an institutional fund to track their 13F filings")
@app_commands.describe(fund="Fund name or CIK (start typing to search)")
async def cmd_follow(interaction: discord.Interaction, fund: str):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    funds = _load_funds()

    # If a CIK was passed directly (from autocomplete), use it
    if fund.isdigit():
        cik  = fund
        name = edgar13f.get_fund_name(cik)
    else:
        results = edgar13f.search_fund(fund)
        if not results:
            await interaction.followup.send(
                f"No 13F filers found matching **{fund}**. "
                "Try their exact name or CIK number."
            )
            return
        if len(results) == 1:
            cik, name = results[0]["cik"], results[0]["name"]
        else:
            lines = "\n".join(f"`{r['cik']}` — {r['name']}" for r in results[:5])
            await interaction.followup.send(
                f"Multiple matches for **{fund}**:\n{lines}\n\n"
                "Use `/follow [CIK]` to pick one."
            )
            return

    if any(f["cik"] == cik for f in funds):
        await interaction.followup.send(f"Already following **{name}** (CIK `{cik}`).")
        return

    funds.append({"cik": cik, "name": name, "display_name": name})
    _save_funds(funds)
    await interaction.followup.send(
        f"Now following **{name}** (CIK `{cik}`).\n"
        "TINA will alert when they file a new 13F-HR."
    )


@cmd_follow.autocomplete("fund")
async def follow_ac(_interaction: discord.Interaction, current: str):
    if len(current) < 2:
        return []
    try:
        loop    = asyncio.get_event_loop()
        results = await asyncio.wait_for(
            loop.run_in_executor(None, edgar13f.search_fund, current),
            timeout=2.5,
        )
        return [
            app_commands.Choice(name=f"{r['name'][:80]} (CIK {r['cik']})", value=r["cik"])
            for r in results[:5]
        ]
    except Exception:
        return []


# ── /unfollow ─────────────────────────────────────────────────────────────────

@tree.command(name="unfollow", description="Stop tracking a fund")
@app_commands.describe(fund="Fund to unfollow")
async def cmd_unfollow(interaction: discord.Interaction, fund: str):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    funds = _load_funds()
    # fund may be a CIK (from autocomplete) or a name fragment
    match = next((f for f in funds if f["cik"] == fund), None)
    if not match:
        match = next((f for f in funds if fund.lower() in f["name"].lower()), None)
    if not match:
        await interaction.followup.send(f"No fund matching **{fund}** in your list. Use `/funds` to see what's followed.")
        return
    _save_funds([f for f in funds if f["cik"] != match["cik"]])
    await interaction.followup.send(f"Unfollowed **{match['name']}**.")


@cmd_unfollow.autocomplete("fund")
async def unfollow_ac(_interaction: discord.Interaction, current: str):
    funds = _load_funds()
    return [
        app_commands.Choice(name=f["name"], value=f["cik"])
        for f in funds
        if current.lower() in f["name"].lower()
    ][:25]


# ── /peek ─────────────────────────────────────────────────────────────────────

@tree.command(name="peek", description="Look up the latest 13F holdings of any institution")
@app_commands.describe(
    fund="Institution name (start typing to search EDGAR)",
    min_pct="Only show positions ≥ this % of portfolio, e.g. 0.5 hides noise (default: show all)",
    top="How many top positions to show in the pie chart (default 15, max 30)",
)
async def cmd_peek(interaction: discord.Interaction, fund: str, min_pct: float = 0.0, top: int = 15):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    # fund is a CIK from autocomplete, or a name to search
    if fund.isdigit():
        cik  = fund
        name = edgar13f.get_fund_name(cik) or f"Fund {cik}"
    else:
        results = edgar13f.search_fund(fund)
        if not results:
            await interaction.followup.send(
                f"No 13F filers found matching **{fund}**. Try a more specific name or CIK."
            )
            return
        if len(results) == 1:
            cik, name = results[0]["cik"], results[0]["name"]
        else:
            lines = "\n".join(f"`{r['cik']}` — {r['name']}" for r in results[:5])
            await interaction.followup.send(
                f"Multiple matches for **{fund}**:\n{lines}\n\nUse `/peek [CIK]` to pick one."
            )
            return

    loop    = asyncio.get_event_loop()
    filing  = await loop.run_in_executor(None, edgar13f.fetch_latest_13f, cik)
    if not filing:
        await interaction.followup.send(f"No 13F-HR filings found for **{name}**.")
        return

    holdings = await loop.run_in_executor(None, edgar13f.parse_holdings, filing["accession"], cik)
    if not holdings:
        await interaction.followup.send(f"Could not parse holdings from **{name}**'s latest filing.")
        return

    # Resolve CUSIPs to tickers from state cache, but don't update state
    st           = state_mod.load()
    ticker_cache = state_mod.get_ticker_cache(st)
    for h in holdings:
        h["ticker"] = ticker_cache.get(h.get("cusip", ""), "")

    total_count = len(holdings)
    full_aum    = sum(h["value_usd"] for h in holdings)

    # Apply min_pct filter
    top = max(3, min(top, 30))
    if min_pct > 0:
        holdings = [h for h in holdings if h["value_usd"] / full_aum * 100 >= min_pct]
        if not holdings:
            await interaction.followup.send(
                f"No positions ≥ **{min_pct}%** of portfolio found. Try a lower threshold."
            )
            return

    sorted_h = sorted(holdings, key=lambda x: x["value_usd"], reverse=True)
    top_lines = [
        f"**{h.get('ticker') or h['name'][:20]}** — {_fmt_usd(h['value_usd'])} "
        f"({h['value_usd']/full_aum*100:.1f}%)"
        for h in sorted_h[:10]
    ]

    filter_note = f" · showing ≥{min_pct}% positions" if min_pct > 0 else ""
    shown_note  = f"{len(holdings)} of {total_count}" if min_pct > 0 else str(total_count)

    await interaction.followup.send(embed=discord.Embed(
        title=f"📋 {name} — Latest 13F Holdings",
        description=(
            f"**{filing['period']}** · {_fmt_usd(full_aum)} total AUM · "
            f"{shown_note} positions{filter_note}\n\n" + "\n".join(top_lines)
        ),
        color=config.COLOR_INFO,
    ).set_footer(text=f"Filed {filing['date']} · CIK {cik}"))

    buf = charts.pie_chart(name, filing["period"], holdings, top_n=top)
    await interaction.followup.send(file=discord.File(buf, filename="peek.png"))


@cmd_peek.autocomplete("fund")
async def peek_ac(_interaction: discord.Interaction, current: str):
    if len(current) < 2:
        return []
    try:
        loop    = asyncio.get_event_loop()
        results = await asyncio.wait_for(
            loop.run_in_executor(None, edgar13f.search_fund, current),
            timeout=2.5,
        )
        return [
            app_commands.Choice(name=f"{r['name'][:80]} (CIK {r['cik']})", value=r["cik"])
            for r in results[:5]
        ]
    except Exception:
        return []


# ── /pie ──────────────────────────────────────────────────────────────────────

def _parse_cap(s: str | None) -> float | None:
    """Parse '500M' → 500_000_000, '1B' → 1_000_000_000."""
    if not s:
        return None
    s = s.strip().upper()
    mults = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    try:
        if s[-1] in mults:
            return float(s[:-1]) * mults[s[-1]]
        return float(s.replace(",", ""))
    except (ValueError, IndexError):
        return None


@tree.command(name="pie", description="Filtered holdings pie chart for any institution")
@app_commands.describe(
    fund="Institution name (start typing to search EDGAR)",
    max_cap="Max market cap of stocks to include, e.g. 500M or 2B",
    min_cap="Min market cap of stocks to include, e.g. 50M",
    min_pct="Only include positions ≥ this % of portfolio, e.g. 0.1",
    top="Max slices in the pie (default 15, max 30)",
)
async def cmd_pie(
    interaction: discord.Interaction,
    fund: str,
    max_cap: str = None,
    min_cap: str = None,
    min_pct: float = 0.0,
    top: int = 15,
):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    if fund.isdigit():
        cik  = fund
        name = edgar13f.get_fund_name(cik) or f"Fund {cik}"
    else:
        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, edgar13f.search_fund, fund)
        if not results:
            await interaction.followup.send(f"No 13F filers found matching **{fund}**.")
            return
        if len(results) == 1:
            cik, name = results[0]["cik"], results[0]["name"]
        else:
            lines = "\n".join(f"`{r['cik']}` — {r['name']}" for r in results[:5])
            await interaction.followup.send(
                f"Multiple matches for **{fund}**:\n{lines}\n\nUse `/pie [CIK]` to pick one."
            )
            return

    loop    = asyncio.get_event_loop()
    filing  = await loop.run_in_executor(None, edgar13f.fetch_latest_13f, cik)
    if not filing:
        await interaction.followup.send(f"No 13F-HR filings found for **{name}**.")
        return

    holdings = await loop.run_in_executor(None, edgar13f.parse_holdings, filing["accession"], cik)
    if not holdings:
        await interaction.followup.send(f"Could not parse holdings from **{name}**'s latest filing.")
        return

    st           = state_mod.load()
    ticker_cache = state_mod.get_ticker_cache(st)
    for h in holdings:
        h["ticker"] = ticker_cache.get(h.get("cusip", ""), "")

    full_aum    = sum(h["value_usd"] for h in holdings)
    total_count = len(holdings)
    top         = max(3, min(top, 30))

    # Apply portfolio % filter (no extra API calls)
    if min_pct > 0:
        holdings = [h for h in holdings if h["value_usd"] / full_aum * 100 >= min_pct]

    # Apply market cap filter (requires Finviz lookups for resolved tickers)
    max_cap_v = _parse_cap(max_cap)
    min_cap_v = _parse_cap(min_cap)
    if max_cap_v is not None or min_cap_v is not None:
        resolved = [h for h in holdings if h.get("ticker")]

        cap_label = []
        if max_cap_v:
            cap_label.append(f"≤{max_cap}")
        if min_cap_v:
            cap_label.append(f"≥{min_cap}")
        cap_str = " & ".join(cap_label)

        n_check = len(resolved)
        eta     = round(n_check * 0.15)
        await interaction.followup.send(
            f"Fetching market caps for **{n_check}** resolved tickers (~{eta}s)…"
        )

        caps = await loop.run_in_executor(None, market.get_market_caps, [h["ticker"] for h in resolved])

        def _passes_cap(h: dict) -> bool:
            cap = caps.get(h["ticker"])
            if cap is None:
                return False
            if max_cap_v and cap > max_cap_v:
                return False
            if min_cap_v and cap < min_cap_v:
                return False
            return True

        holdings = [h for h in resolved if _passes_cap(h)]

        if not holdings:
            await interaction.followup.send(
                f"No positions with resolved tickers found in the **{cap_str}** market cap range. "
                "CUSIP→ticker mapping may still be building — try again after the next scan."
            )
            return
    else:
        cap_str = None

    if not holdings:
        await interaction.followup.send("No positions match the filters. Try loosening them.")
        return

    sorted_h  = sorted(holdings, key=lambda x: x["value_usd"], reverse=True)
    shown     = len(holdings)
    top_lines = [
        f"**{h.get('ticker') or h['name'][:20]}** — {_fmt_usd(h['value_usd'])} "
        f"({h['value_usd']/full_aum*100:.2f}% of AUM)"
        for h in sorted_h[:10]
    ]

    filter_parts = []
    if cap_str:
        filter_parts.append(f"cap {cap_str}")
    if min_pct > 0:
        filter_parts.append(f"≥{min_pct}% portfolio")
    filter_note = f" · filters: {', '.join(filter_parts)}" if filter_parts else ""

    await interaction.followup.send(embed=discord.Embed(
        title=f"🥧 {name} — Filtered Holdings",
        description=(
            f"**{filing['period']}** · {_fmt_usd(full_aum)} total AUM · "
            f"{shown} of {total_count} positions{filter_note}\n\n"
            + "\n".join(top_lines)
        ),
        color=config.COLOR_INFO,
    ).set_footer(text=f"Filed {filing['date']} · CIK {cik}"))

    buf = charts.pie_chart(name, filing["period"], holdings, top_n=top)
    await interaction.followup.send(file=discord.File(buf, filename="pie.png"))


@cmd_pie.autocomplete("fund")
async def pie_ac(_interaction: discord.Interaction, current: str):
    if len(current) < 2:
        return []
    try:
        loop    = asyncio.get_event_loop()
        results = await asyncio.wait_for(
            loop.run_in_executor(None, edgar13f.search_fund, current),
            timeout=2.5,
        )
        return [
            app_commands.Choice(name=f"{r['name'][:80]} (CIK {r['cik']})", value=r["cik"])
            for r in results[:5]
        ]
    except Exception:
        return []


# ── /funds ────────────────────────────────────────────────────────────────────

@tree.command(name="funds", description="List all followed funds")
async def cmd_funds(interaction: discord.Interaction):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    funds = _load_funds()
    if not funds:
        await interaction.followup.send("No funds followed yet. Use `/follow Bridgewater` to start.")
        return

    st    = state_mod.load()
    lines = []
    for f in funds:
        fd      = st.get("funds", {}).get(f["cik"], {})
        quarter = fd.get("latest_quarter", "not yet scanned")
        n       = len(fd.get("positions", {}))
        lines.append(f"**{f['name']}** · CIK `{f['cik']}` · {n} positions · last: {quarter}")

    await interaction.followup.send(f"**Following {len(funds)} fund(s):**\n" + "\n".join(lines))


# ── /holdings ─────────────────────────────────────────────────────────────────

def _resolve_cik(fund: str) -> str | None:
    """Resolve a fund name or CIK string to a CIK. Returns None if not found."""
    funds = _load_funds()
    match = next((f for f in funds if f["cik"] == fund), None)
    if not match:
        match = next((f for f in funds if fund.lower() in f["name"].lower()), None)
    return match["cik"] if match else (fund if fund.isdigit() else None)


def _followed_ac(current: str) -> list[app_commands.Choice]:
    return [
        app_commands.Choice(name=f["name"], value=f["cik"])
        for f in _load_funds()
        if current.lower() in f["name"].lower()
    ][:25]


@tree.command(name="holdings", description="Portfolio allocation pie chart for a fund")
@app_commands.describe(fund="Fund name (start typing to search your followed funds)")
async def cmd_holdings(interaction: discord.Interaction, fund: str):
    cik = _resolve_cik(fund)
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    st        = state_mod.load()
    fund_data = st.get("funds", {}).get(cik)
    if not fund_data:
        await interaction.followup.send(f"No data for CIK `{cik}` yet.")
        return

    holdings = [
        {"name": p["name"], "ticker": p.get("ticker", ""), "value_usd": p["value_usd"]}
        for p in fund_data["positions"].values()
        if p.get("value_usd", 0) > 0
    ]
    if not holdings:
        await interaction.followup.send("No holdings data available.")
        return

    buf = charts.pie_chart(
        fund_data.get("name", f"Fund {cik}"),
        fund_data.get("latest_quarter", "?"),
        holdings,
    )
    await interaction.followup.send(file=discord.File(buf, filename="holdings.png"))


@cmd_holdings.autocomplete("fund")
async def holdings_ac(_interaction: discord.Interaction, current: str):
    return _followed_ac(current)


# ── /perf ─────────────────────────────────────────────────────────────────────

@tree.command(name="perf", description="Performance chart — entry price vs current price for a fund")
@app_commands.describe(fund="Fund name (start typing to search your followed funds)")
async def cmd_perf(interaction: discord.Interaction, fund: str):
    cik = _resolve_cik(fund)
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    st        = state_mod.load()
    fund_data = st.get("funds", {}).get(cik)
    if not fund_data:
        await interaction.followup.send(f"No fund matching **{fund}** found. Use `/funds` to see what's followed.")
        return

    positions = fund_data["positions"]
    quarter   = fund_data.get("latest_quarter", "?")
    name      = fund_data.get("name", f"Fund {cik}")

    tickers = list({p["ticker"] for p in positions.values() if p.get("ticker")})
    if not tickers:
        await interaction.followup.send("No ticker data available — CUSIP lookup may still be running.")
        return

    await interaction.followup.send(f"Fetching prices for {len(tickers)} positions…")
    prices = market.get_prices_bulk(tickers[:50])

    perf = []
    for pos in positions.values():
        ticker = pos.get("ticker")
        shares = pos.get("shares", 0)
        val    = pos.get("value_usd", 0)
        if not ticker or not shares or not val:
            continue
        curr = prices.get(ticker)
        if curr:
            perf.append({
                "ticker":        ticker,
                "name":          pos.get("name", ticker),
                "entry_price":   val / shares,
                "current_price": curr,
                "value_usd":     val,
            })

    if not perf:
        await interaction.followup.send("Could not fetch current prices for this fund's holdings.")
        return

    buf = charts.performance_chart(name, quarter, perf)
    if not buf:
        await interaction.followup.send("Not enough price data to generate chart.")
        return

    await interaction.followup.send(file=discord.File(buf, filename="performance.png"))


@cmd_perf.autocomplete("fund")
async def perf_ac(_interaction: discord.Interaction, current: str):
    return _followed_ac(current)


# ── /summary ──────────────────────────────────────────────────────────────────

@tree.command(name="summary", description="Weighted performance summary chart across all followed funds")
async def cmd_summary(interaction: discord.Interaction):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    funds = _load_funds()
    if not funds:
        await interaction.followup.send("No funds followed yet.")
        return

    st = state_mod.load()
    await interaction.followup.send(f"Calculating performance for {len(funds)} fund(s)…")

    fund_perfs = []
    for fund in funds:
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
            ret    = (curr - val / shares) / (val / shares) * 100
            total  += val
            w_ret  += ret * val
            w_sum  += val

        if w_sum > 0:
            fund_perfs.append({
                "name":            name,
                "weighted_return": w_ret / w_sum,
                "total_value_usd": total,
                "n_positions":     len(positions),
            })

    if not fund_perfs:
        await interaction.followup.send("No performance data available yet.")
        return

    buf = charts.summary_chart(fund_perfs)
    await interaction.followup.send(file=discord.File(buf, filename="summary.png"))


def _entry_quarter(st: dict, cik: str, ticker: str) -> str:
    """Return the quarter a fund first held a ticker, or 'since <oldest>+' if it predates history."""
    fund   = st.get("funds", {}).get(cik, {})
    history = fund.get("history", [])  # [Q4 2025, Q3 2025, ...] — newest first

    def _in_positions(positions: dict) -> bool:
        return any(p.get("ticker") == ticker for p in positions.values())

    # Walk history from oldest to newest to find earliest quarter held
    first_seen: str | None = None
    for entry in reversed(history):
        if _in_positions(entry.get("positions", {})):
            first_seen = entry["quarter"]

    if first_seen is None:
        # Not in any history → entered in the current quarter
        current = fund.get("latest_quarter", "")
        return _fmt_quarter(current) if current else "new"

    # Check whether the oldest history entry holds it — if so we don't know the true entry
    oldest = history[-1] if history else None
    if oldest and first_seen == oldest["quarter"] and _in_positions(oldest.get("positions", {})):
        return f"since {_fmt_quarter(first_seen)}+"

    return f"entered {_fmt_quarter(first_seen)}"


def _fmt_quarter(period: str) -> str:
    """'2026-03-31' → 'Q1 2026', '2025-12-31' → 'Q4 2025', etc."""
    try:
        y, m, _ = period.split("-")
        q = (int(m) - 1) // 3 + 1
        return f"Q{q} {y}"
    except Exception:
        return period


# ── /overlap ──────────────────────────────────────────────────────────────────

@tree.command(name="overlap", description="Stocks held by multiple followed funds with optional filters")
@app_commands.describe(
    min_funds="Minimum number of funds that must hold the stock (default 2)",
    min_inst_value="Min combined institutional position, e.g. 10M or 1B — filters out tiny quant positions",
    max_cap="Max market cap, e.g. 500M or 2B (optional)",
    min_cap="Min market cap, e.g. 100M (optional)",
    sector="Sector filter, e.g. Technology or Healthcare (optional)",
    top="Max results to show (default 20, max 30)",
)
async def cmd_overlap(
    interaction: discord.Interaction,
    min_funds: int = 2,
    min_inst_value: str = "",
    max_cap: str = "",
    min_cap: str = "",
    sector: str = "",
    top: int = 20,
):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    st      = state_mod.load()
    all_pos = state_mod.all_fund_positions(st)
    if not all_pos:
        await interaction.followup.send("No holdings data yet.")
        return

    # CIK → friendly display name (from funds.json, fallback to state name)
    _disp = {f["cik"]: f.get("display_name") or f["name"] for f in _load_funds()}

    # Build ticker → [{cik, fund, value}] — one entry per fund, values summed across CUSIPs.
    # A fund can hold the same ticker via multiple CUSIPs (shares + calls + puts), so we
    # deduplicate by CIK and sum the values rather than counting each CUSIP as a separate fund.
    _raw: dict[str, dict[str, dict]] = {}  # ticker → cik → entry
    for cik, positions in all_pos.items():
        fund_name = _disp.get(cik) or st["funds"][cik].get("name", cik)
        for pos in positions.values():
            ticker = pos.get("ticker", "")
            val    = pos.get("value_usd", 0)
            if not ticker or not val:
                continue
            fund_entry = _raw.setdefault(ticker, {}).setdefault(
                cik, {"cik": cik, "fund": fund_name, "value": 0}
            )
            fund_entry["value"] += val
    ticker_map: dict[str, list] = {t: list(e.values()) for t, e in _raw.items()}

    min_funds     = max(2, min_funds)
    min_inst_val  = _parse_cap(min_inst_value) or 0.0

    overlaps = [
        {
            "ticker":          t,
            "fund_count":      len(entries),
            "total_value_usd": sum(e["value"] for e in entries),
            "funds":           sorted(entries, key=lambda e: e["value"], reverse=True),
        }
        for t, entries in ticker_map.items()
        if len(entries) >= min_funds
    ]

    # Pre-filter by minimum combined institutional value — cheap, no API needed.
    # Removes noise from quant funds (Two Sigma / Point72) that hold thousands
    # of tiny positions which mechanically overlap with each other.
    if min_inst_val:
        overlaps = [o for o in overlaps if o["total_value_usd"] >= min_inst_val]

    overlaps.sort(key=lambda x: x["fund_count"] * x["total_value_usd"], reverse=True)

    if not overlaps:
        await interaction.followup.send("No stocks held by the required number of followed funds.")
        return

    # ── Apply market cap / sector filters via Finviz ──────────────────────────
    max_cap_v   = _parse_cap(max_cap)
    min_cap_v   = _parse_cap(min_cap)
    sector_lc   = sector.strip().lower()
    need_screen = bool(max_cap_v or min_cap_v or sector_lc)

    screen: dict[str, dict] = {}
    if need_screen:
        tickers_to_check = [o["ticker"] for o in overlaps]
        n       = len(tickers_to_check)
        eta_sec = max(10, round(n / 3 * 0.16))  # 3 parallel workers, ~0.16s per ticker
        await interaction.followup.send(
            f"Fetching market data for **{n}** ticker(s) (~{eta_sec}s)…"
        )
        loop = asyncio.get_event_loop()
        screen = await loop.run_in_executor(
            None,
            lambda: market.get_ticker_screen(tickers_to_check, need_sector=bool(sector_lc)),
        )

        def _passes(o: dict) -> bool:
            info = screen.get(o["ticker"], {})
            cap  = info.get("market_cap")
            sec  = (info.get("sector") or "").lower()
            if max_cap_v and (cap is None or cap > max_cap_v):
                return False
            if min_cap_v and (cap is None or cap < min_cap_v):
                return False
            if sector_lc and sector_lc not in sec:
                return False
            return True

        overlaps = [o for o in overlaps if _passes(o)]

    if not overlaps:
        filters_desc = " · ".join(filter(None, [
            f"min_funds={min_funds}",
            f"max_cap={max_cap}" if max_cap else "",
            f"min_cap={min_cap}" if min_cap else "",
            f"sector={sector}"   if sector   else "",
        ]))
        await interaction.followup.send(f"No results matching filters: {filters_desc}")
        return

    top = max(1, min(top, 30))
    lines = []
    for o in overlaps[:top]:
        ticker = o["ticker"]

        cap_str = ""
        if need_screen:
            cap = screen.get(ticker, {}).get("market_cap")
            sec = screen.get(ticker, {}).get("sector") or ""
            if cap is not None:
                cap_str = f" · {f'${cap/1e9:.1f}B' if cap >= 1e9 else f'${cap/1e6:.0f}M'} cap"
            if sec:
                cap_str += f" · {sec}"

        fund_parts = []
        for e in o["funds"][:4]:
            entry = _entry_quarter(st, e["cik"], ticker)
            fund_parts.append(f"{e['fund'][:16]} {_fmt_usd(e['value'])} *({entry})*")
        funds_str = "\n  · ".join(fund_parts)
        if len(o["funds"]) > 4:
            funds_str += f"\n  · *+{len(o['funds'])-4} more*"

        lines.append(
            f"**${ticker}** — {o['fund_count']} fund(s) · {_fmt_usd(o['total_value_usd'])} total{cap_str}\n"
            f"  · {funds_str}"
        )

    filter_parts = " · ".join(filter(None, [
        f"{min_funds}+ funds" if min_funds > 2 else "",
        f"cap ≤ {max_cap}"    if max_cap   else "",
        f"cap ≥ {min_cap}"    if min_cap   else "",
        f"sector: {sector}"   if sector    else "",
    ])) or "no filters"

    await interaction.followup.send(
        embed=discord.Embed(
            title=f"📍 Overlap — {len(overlaps)} stock(s) held by {min_funds}+ funds",
            description=f"*{filter_parts}*\n\n" + "\n\n".join(lines[:top]),
            color=config.COLOR_INFO,
        ).set_footer(text=f"Based on latest 13F filings · {len(all_pos)} fund(s) scanned")
    )


# ── /consensus ────────────────────────────────────────────────────────────────

@tree.command(name="consensus", description="Top stocks by total institutional conviction across all followed funds")
async def cmd_consensus(interaction: discord.Interaction):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    st      = state_mod.load()
    all_pos = state_mod.all_fund_positions(st)
    if not all_pos:
        await interaction.followup.send("No holdings data yet.")
        return

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

    ranked = sorted(ticker_map.values(), key=lambda x: x["total_value_usd"], reverse=True)

    buf = charts.consensus_chart(ranked)
    if buf:
        await interaction.followup.send(file=discord.File(buf, filename="consensus.png"))
    else:
        await interaction.followup.send("Not enough data to generate chart.")


# ── /compare ──────────────────────────────────────────────────────────────────

@tree.command(name="compare", description="Side-by-side comparison of two funds' holdings")
@app_commands.describe(fund1="First fund", fund2="Second fund")
async def cmd_compare(interaction: discord.Interaction, fund1: str, fund2: str):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    cik1 = _resolve_cik(fund1)
    cik2 = _resolve_cik(fund2)
    st   = state_mod.load()

    fd1 = st.get("funds", {}).get(cik1)
    fd2 = st.get("funds", {}).get(cik2)
    if not fd1 or not fd2:
        await interaction.followup.send("Could not find data for one or both funds. Use `/funds` to check.")
        return

    name1 = fd1.get("name", fund1)
    name2 = fd2.get("name", fund2)
    pos1  = {p["ticker"]: p for p in fd1["positions"].values() if p.get("ticker")}
    pos2  = {p["ticker"]: p for p in fd2["positions"].values() if p.get("ticker")}

    shared     = [t for t in pos1 if t in pos2]
    only1      = [t for t in pos1 if t not in pos2]
    only2      = [t for t in pos2 if t not in pos1]

    shared_data = [
        {"ticker": t, "value1": pos1[t]["value_usd"], "value2": pos2[t]["value_usd"]}
        for t in shared
    ]

    lines = [
        f"**Shared ({len(shared)}):** {', '.join(f'${t}' for t in sorted(shared)[:15])}",
        f"**Only {name1[:18]} ({len(only1)}):** {', '.join(f'${t}' for t in sorted(only1)[:10])}",
        f"**Only {name2[:18]} ({len(only2)}):** {', '.join(f'${t}' for t in sorted(only2)[:10])}",
    ]
    await interaction.followup.send("\n".join(lines))

    if shared_data:
        buf = charts.compare_chart(name1, name2, shared_data)
        if buf:
            await interaction.followup.send(file=discord.File(buf, filename="compare.png"))


@cmd_compare.autocomplete("fund1")
async def compare_ac1(_interaction: discord.Interaction, current: str):
    return _followed_ac(current)


@cmd_compare.autocomplete("fund2")
async def compare_ac2(_interaction: discord.Interaction, current: str):
    return _followed_ac(current)


# ── /smart-money ──────────────────────────────────────────────────────────────

@tree.command(name="smart-money", description="Show which followed funds hold a specific ticker")
@app_commands.describe(ticker="Stock ticker (e.g. NVDA)")
async def cmd_smart_money(interaction: discord.Interaction, ticker: str):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    ticker  = ticker.upper().strip()
    st      = state_mod.load()
    all_pos = state_mod.all_fund_positions(st)

    holders = []
    for cik, positions in all_pos.items():
        fund_name = st["funds"][cik].get("name", cik)
        quarter   = st["funds"][cik].get("latest_quarter", "?")
        for pos in positions.values():
            if pos.get("ticker", "").upper() == ticker:
                holders.append({
                    "fund":    fund_name,
                    "value":   pos["value_usd"],
                    "shares":  pos["shares"],
                    "quarter": quarter,
                })

    if not holders:
        await interaction.followup.send(
            f"None of your followed funds hold **${ticker}** in their latest 13F.\n"
            "Note: TINA needs a CUSIP→ticker match — if the fund holds it but it's not resolved yet, try again after the next scan."
        )
        return

    holders.sort(key=lambda x: x["value"], reverse=True)
    total = sum(h["value"] for h in holders)

    lines = [
        f"**{h['fund']}** — {_fmt_usd(h['value'])} · {h['shares']:,} shares · {h['quarter']}"
        for h in holders
    ]
    await interaction.followup.send(
        f"**💰 Smart Money in ${ticker}** — {len(holders)} fund(s) · {_fmt_usd(total)} total\n"
        + "\n".join(lines)
    )


# ── /history ──────────────────────────────────────────────────────────────────

@tree.command(name="history", description="How a fund's top positions changed over the last 4 quarters")
@app_commands.describe(fund="Fund name (start typing to search)")
async def cmd_history(interaction: discord.Interaction, fund: str):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    cik = _resolve_cik(fund)
    st  = state_mod.load()
    fd  = st.get("funds", {}).get(cik)
    if not fd:
        await interaction.followup.send(f"No data for **{fund}**. Use `/funds` to check.")
        return

    history   = state_mod.get_fund_history(st, cik)
    fund_name = fd.get("name", fund)

    if not history:
        await interaction.followup.send(
            f"Only one quarter of data for **{fund_name}** so far. "
            "History builds automatically as new 13F filings come in."
        )
        return

    # Build quarters list (current + history)
    all_quarters = [
        {"quarter": fd["latest_quarter"], "positions": fd["positions"]},
        *history,
    ]
    quarter_labels = [q["quarter"] for q in all_quarters]

    # Find top tickers by current value
    top_tickers = [
        pos["ticker"]
        for pos in sorted(fd["positions"].values(), key=lambda x: x.get("value_usd", 0), reverse=True)
        if pos.get("ticker")
    ][:8]

    # Build value series per ticker across quarters
    values_by_ticker: dict[str, list[float]] = {}
    for ticker in top_tickers:
        series = []
        for q in all_quarters:
            match = next(
                (p for p in q["positions"].values() if p.get("ticker") == ticker),
                None,
            )
            series.append(match["value_usd"] if match else 0.0)
        values_by_ticker[ticker] = series

    buf = charts.history_chart(fund_name, quarter_labels, top_tickers, values_by_ticker)
    if not buf:
        await interaction.followup.send("Not enough data to generate history chart.")
        return

    await interaction.followup.send(file=discord.File(buf, filename="history.png"))


@cmd_history.autocomplete("fund")
async def history_ac(_interaction: discord.Interaction, current: str):
    return _followed_ac(current)


# ── /ticker ───────────────────────────────────────────────────────────────────

@tree.command(name="ticker", description="Show which followed institutions hold a ticker and when they bought")
@app_commands.describe(ticker="Stock ticker (e.g. NVDA, AAPL)")
async def cmd_ticker(interaction: discord.Interaction, ticker: str):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    ticker = ticker.upper().strip()
    st     = state_mod.load()

    fund_series = []  # for line chart
    embed_lines = []

    for cik, fd in st.get("funds", {}).items():
        fund_name = fd.get("name", cik)
        history   = state_mod.get_fund_history(st, cik)

        # Build timeline: current + all history quarters
        all_quarters = [
            {"quarter": fd["latest_quarter"], "positions": fd.get("positions", {})}
        ] + history

        timeline = []
        for q in all_quarters:
            match = next(
                (p for p in q["positions"].values() if p.get("ticker", "").upper() == ticker),
                None,
            )
            if match:
                implied_price = match["value_usd"] / match["shares"] if match["shares"] else None
                timeline.append({
                    "quarter":       q["quarter"],
                    "value_usd":     match["value_usd"],
                    "shares":        match["shares"],
                    "implied_price": implied_price,
                })

        if not timeline:
            continue

        # Build line chart series (oldest→newest)
        q_labels = [t["quarter"] for t in reversed(timeline)]
        q_values = [t["value_usd"] for t in reversed(timeline)]
        fund_series.append({"name": fund_name, "quarters": q_labels, "values": q_values})

        # Build embed text
        latest = timeline[0]
        price_str = f"implied ${latest['implied_price']:.2f}/sh" if latest["implied_price"] else ""
        trend_parts = []
        for t in timeline:
            trend_parts.append(f"{t['quarter']}: {_fmt_usd(t['value_usd'])}")
        embed_lines.append(
            f"**{fund_name}**\n"
            f"Latest: {_fmt_usd(latest['value_usd'])} · {latest['shares']:,} shares · {price_str}\n"
            f"History: {' → '.join(reversed([t['quarter'] + ' ' + _fmt_usd(t['value_usd']) for t in timeline]))}"
        )

    if not embed_lines:
        await interaction.followup.send(
            f"None of your followed funds hold **${ticker}** in their recorded history.\n"
            "Note: positions need a CUSIP→ticker match from OpenFIGI to appear here."
        )
        return

    await interaction.followup.send(
        f"**🔍 ${ticker} — Institutional Activity ({len(fund_series)} fund(s))**\n\n"
        + "\n\n".join(embed_lines)
    )

    # Send line chart if we have multi-quarter data
    buf = charts.ticker_history_chart(ticker, fund_series)
    if buf:
        await interaction.followup.send(file=discord.File(buf, filename="ticker_history.png"))


# ── /pile-in ─────────────────────────────────────────────────────────────────

@tree.command(name="pile-in", description="Small-cap stocks that multiple followed institutions are holding")
@app_commands.describe(
    max_cap="Max market cap to include, e.g. 500M or 2B (default: 2B)",
    min_funds="Min number of institutions that must hold the stock (default: 2)",
    top="Max stocks to show (default: 20)",
)
async def cmd_pile_in(
    interaction: discord.Interaction,
    max_cap: str = "2B",
    min_funds: int = 2,
    top: int = 20,
):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    st      = state_mod.load()
    all_pos = state_mod.all_fund_positions(st)

    if not all_pos:
        await interaction.followup.send("No holdings data yet — follow some funds first.")
        return

    # Build ticker → [{fund, value, shares}]
    ticker_map: dict[str, list[dict]] = {}
    for cik, positions in all_pos.items():
        fund_name = st["funds"][cik].get("name", cik)
        for pos in positions.values():
            ticker = pos.get("ticker", "")
            val    = pos.get("value_usd", 0)
            if not ticker or not val:
                continue
            ticker_map.setdefault(ticker, []).append({
                "fund":   fund_name,
                "value":  val,
                "shares": pos.get("shares", 0),
            })

    min_funds = max(1, min_funds)
    candidates = {t: e for t, e in ticker_map.items() if len(e) >= min_funds}

    if not candidates:
        await interaction.followup.send(
            f"No tickers held by {min_funds}+ followed funds. "
            "Follow more funds or lower `min_funds`."
        )
        return

    max_cap_v = _parse_cap(max_cap) or 2_000_000_000

    # Sort by total position value ascending — small positions are more likely to be
    # small-cap, so we prioritize them and cap Finviz lookups at 300.
    _CHECK_LIMIT = 300
    sorted_candidates = sorted(
        candidates.items(), key=lambda x: sum(e["value"] for e in x[1])
    )
    check_list = sorted_candidates[:_CHECK_LIMIT]
    n   = len(check_list)
    eta = round(n * 0.15)
    await interaction.followup.send(
        f"Checking market caps for **{n}** candidate ticker(s) (~{eta}s)…"
    )

    loop = asyncio.get_event_loop()
    caps = await loop.run_in_executor(
        None,
        lambda: market.get_market_caps([t for t, _ in check_list], limit=n),
    )
    candidates = dict(check_list)

    results = []
    for ticker, entries in candidates.items():
        cap = caps.get(ticker)
        if cap is None or cap > max_cap_v:
            continue
        results.append({
            "ticker":      ticker,
            "market_cap":  cap,
            "fund_count":  len(entries),
            "total_value": sum(e["value"] for e in entries),
            "funds":       sorted(entries, key=lambda x: x["value"], reverse=True),
        })

    if not results:
        await interaction.followup.send(
            f"No multi-institution positions found under **{max_cap}** market cap. "
            "Try a higher cap or follow more funds with small-cap holdings."
        )
        return

    results.sort(key=lambda x: (x["fund_count"], x["total_value"]), reverse=True)
    top = max(1, min(top, 30))
    shown = results[:top]

    lines = []
    for r in shown:
        cap     = r["market_cap"]
        cap_str = f"${cap/1e6:.0f}M" if cap < 1e9 else f"${cap/1e9:.1f}B"
        holders = " · ".join(
            f"{e['fund'][:18]} {_fmt_usd(e['value'])}" for e in r["funds"][:4]
        )
        lines.append(
            f"**${r['ticker']}** — {cap_str} cap · {r['fund_count']} institution(s)\n"
            f"  ↳ {holders}"
        )

    cap_label = f"${max_cap_v/1e9:.1f}B" if max_cap_v >= 1e9 else f"${max_cap_v/1e6:.0f}M"
    await interaction.followup.send(
        embed=discord.Embed(
            title=f"📊 Small-Cap Pile-In — {len(results)} stock(s)",
            description=(
                f"Market cap ≤ {cap_label} · held by {min_funds}+ institutions\n\n"
                + "\n\n".join(lines[:15])
            ),
            color=config.COLOR_INFO,
        ).set_footer(text=f"Based on latest 13F filings · {len(all_pos)} fund(s) scanned")
    )

    buf = charts.pile_in_chart(shown)
    if buf:
        await interaction.followup.send(file=discord.File(buf, filename="pile_in.png"))


def _fmt_usd(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# ── /tag ─────────────────────────────────────────────────────────────────────

@tree.command(name="tag", description="Show or override a fund's auto-detected style tag")
@app_commands.describe(
    fund="Fund to inspect or retag (leave blank for all funds)",
    style="Override the detected style: concentrated | focused | diversified | quant",
)
async def cmd_tag(
    interaction: discord.Interaction,
    fund: str = "",
    style: str = "",
):
    if not _in_ch(interaction):
        await interaction.response.send_message("Wrong channel.", ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    style = style.lower().strip()
    if style and style not in fund_type_mod.VALID_STYLES:
        await interaction.followup.send(
            f"Invalid style **{style}**. Choose one of: `" + "`, `".join(fund_type_mod.VALID_STYLES) + "`",
            ephemeral=True,
        )
        return

    st    = state_mod.load()
    funds = _load_funds()
    if not funds:
        await interaction.followup.send("No funds followed yet. Use `/follow` to start.", ephemeral=True)
        return

    # Filter to a specific fund if named
    if fund:
        cik_filter = _resolve_cik(fund)
        funds = [f for f in funds if f["cik"] == cik_filter]
        if not funds:
            await interaction.followup.send(f"No fund matching **{fund}**.", ephemeral=True)
            return

    # Write override
    if style:
        if len(funds) != 1:
            await interaction.followup.send(
                "Specify a single fund when setting a style override.", ephemeral=True
            )
            return
        f    = funds[0]
        cik  = f["cik"]
        name = st.get("funds", {}).get(cik, {}).get("name") or f["name"]
        n    = st.get("funds", {}).get(cik, {}).get("style", {}).get("n_positions", 0)

        override = fund_type_mod._make(style=style, n=n, top10_pct=0.0, hhi=0.0, overridden=True)
        state_mod.set_fund_style(st, cik, override)
        state_mod.save(st)

        meta = fund_type_mod.get_meta(style)
        await interaction.followup.send(
            f"**{name}** manually tagged as **{meta['label']}** (×{meta['conviction']} conviction).\n"
            f"This override will persist across future scans.",
            ephemeral=True,
        )
        return

    # Read-only: show style for all (or filtered) funds
    lines = []
    for f in funds:
        cik       = f["cik"]
        name      = st.get("funds", {}).get(cik, {}).get("name") or f["name"]
        sd        = state_mod.get_fund_style(st, cik)
        if not sd:
            lines.append(f"**{name}** — ❓ Not yet detected (needs first 13F scan)")
            continue

        override_flag = " *(manual override)*" if sd.get("overridden") else ""
        lines.append(
            f"**{name}**{override_flag}\n"
            f"  {sd['label']} · {sd['n_positions']} positions · "
            f"top-10: {sd['top10_pct']}% · HHI: {sd['hhi']} · ×{sd['conviction']} conviction"
        )

    await interaction.followup.send(
        embed=discord.Embed(
            title="🏷️ Fund Style Tags",
            description="\n\n".join(lines) or "No style data yet.",
            color=0x95a5a6,
        ).set_footer(text="HHI > 500 = very concentrated · Auto-detected from 13F position count + concentration · Use /tag fund:X style:Y to override"),
        ephemeral=True,
    )


@cmd_tag.autocomplete("fund")
async def tag_fund_ac(_interaction: discord.Interaction, current: str):
    return _followed_ac(current)


@cmd_tag.autocomplete("style")
async def tag_style_ac(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    descriptions = {
        "concentrated": "≤25 positions or very top-heavy — each bet is a thesis (×1.5)",
        "focused":      "26–150 positions, deliberate stock-pickers (×1.0)",
        "diversified":  "150–500 positions, sector or multi-strat (×0.7)",
        "quant":        "500+ positions, systematic/high-turnover — new bets are noise (×0.4)",
    }
    return [
        app_commands.Choice(name=f"{s} — {descriptions[s]}", value=s)
        for s in fund_type_mod.VALID_STYLES
        if s != "unknown" and current.lower() in s
    ]


# ── Bot lifecycle ─────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    await tree.sync()
    print(f"[tina/bot] Logged in as {client.user}")
    tina_discord.post_status("🟢 **TINA is online.** Institutional scanning active.")


_offline_posted = False

_OFFLINE_MSG = (
    "🔴 **TINA has gone offline.** Scanning paused.\n"
    "Slash commands won't respond until it's back. Spam Tijn to turn his PC on."
)


def _post_offline() -> None:
    global _offline_posted
    if not _offline_posted:
        _offline_posted = True
        tina_discord.post_status(_OFFLINE_MSG)


def _sigterm_handler(signum, frame):
    _post_offline()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)

if not TOKEN:
    raise SystemError("DISCORD_BOT_TOKEN not set in tina/.env")

try:
    client.run(TOKEN)
finally:
    _post_offline()
