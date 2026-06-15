"""
Discord slash command bot.

Runs alongside the scanner (main.py) as a separate process.
Users can type /lookup, /list, /scores, /filter, /add, /remove, /perf directly in Discord.
Right-click any message containing $TICKER to run a lookup.

Requires DISCORD_BOT_TOKEN in .env.
Get one at: https://discord.com/developers/applications
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

_raw_channel = os.environ.get("ALLOWED_CHANNEL_ID", "").strip()
ALLOWED_CHANNEL_ID: int | None = int(_raw_channel) if _raw_channel else None


def _check_channel(interaction: discord.Interaction) -> bool:
    if ALLOWED_CHANNEL_ID is None:
        return True
    return interaction.channel_id == ALLOWED_CHANNEL_ID


def _load_watchlist() -> dict:
    wl_file = Path("watchlist.json")
    return json.loads(wl_file.read_text()) if wl_file.exists() else {"tickers": []}


# ── Autocomplete ───────────────────────────────────────────────────────────────

async def _watchlist_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    tickers = _load_watchlist().get("tickers", [])
    return [
        app_commands.Choice(name=f"{e['ticker']} — {e['name'][:40]}", value=e["ticker"])
        for e in tickers
        if current.upper() in e["ticker"]
    ][:25]


# ── Persistent add-to-watchlist button ────────────────────────────────────────

class AddToWatchlistView(discord.ui.View):
    def __init__(self, ticker: str):
        super().__init__(timeout=None)
        self.ticker = ticker

    @discord.ui.button(label="Add to watchlist", style=discord.ButtonStyle.green)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_event_loop()
        added_by = interaction.user.display_name
        msg = await loop.run_in_executor(None, _do_add, self.ticker, added_by)
        button.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(msg)


# ── Remove: select menu + confirmation ────────────────────────────────────────

class RemoveConfirmView(discord.ui.View):
    def __init__(self, ticker: str):
        super().__init__(timeout=60)
        self.ticker = ticker

    @discord.ui.button(label="Yes, remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _do_remove, self.ticker)
        await interaction.edit_original_response(content="Done.", view=None)
        await interaction.channel.send(msg)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class RemoveSelectView(discord.ui.View):
    def __init__(self, tickers: list[dict]):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=e["ticker"],
                description=e["name"][:100],
                value=e["ticker"],
            )
            for e in tickers[:25]
        ]
        self.select = discord.ui.Select(
            placeholder="Choose a ticker to remove...",
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        ticker = self.select.values[0]
        await interaction.response.edit_message(
            content=f"Remove **${ticker}** from the watchlist?",
            view=RemoveConfirmView(ticker),
        )


# ── Pagination ─────────────────────────────────────────────────────────────────

def _paginate(lines: list[str], per_page: int = 10) -> list[str]:
    pages = []
    for i in range(0, max(len(lines), 1), per_page):
        pages.append("\n".join(lines[i:i + per_page]))
    return pages


class PaginatedView(discord.ui.View):
    def __init__(self, pages: list[str]):
        super().__init__(timeout=120)
        self.pages = pages
        self.current = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    def _content(self) -> str:
        suffix = f"\n\n*Page {self.current + 1} of {len(self.pages)}*" if len(self.pages) > 1 else ""
        return self.pages[self.current] + suffix

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._sync_buttons()
        await interaction.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._sync_buttons()
        await interaction.response.edit_message(content=self._content(), view=self)


# ── Filter modals + view ───────────────────────────────────────────────────────

def _parse_filter_number(s: str) -> float:
    s = s.upper().strip()
    mults = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    if s and s[-1] in mults:
        return float(s[:-1]) * mults[s[-1]]
    return float(s)


def _load_filters() -> dict:
    from scanners.discovery import load_filters
    return load_filters()


def _save_filters(filters: dict) -> None:
    Path("filters.json").write_text(json.dumps(filters, indent=2))


def _format_filters(filters: dict) -> str:
    cap = filters.get("market_cap", {})
    flt = filters.get("float", {})
    prc = filters.get("price", {})
    enabled = filters.get("enabled", True)
    score = filters.get("min_score", 30)
    tier = "Notable" if score < 50 else "Strong" if score < 70 else "Exceptional"
    return (
        f"**Discovery** {'✅ on' if enabled else '⛔ off'}\n\n"
        f"**Scoring & Size**\n"
        f"Min score: `{score}` ({tier}+)\n"
        f"Market cap: `${cap.get('min', 0)/1e6:.0f}M – ${cap.get('max', 0)/1e6:.0f}M`\n"
        f"Max float: `{flt.get('max', 0)/1e6:.0f}M shares`\n\n"
        f"**Price & Buy**\n"
        f"Price: `${prc.get('min', 0)} – ${prc.get('max', 0)}`\n"
        f"Min buy: `${filters.get('min_buy_value', 0):,}`\n\n"
        f"**Roles**\n"
        f"`{filters.get('roles', 'exec')}` "
        f"({'CEO/CFO/COO/CTO/President only' if filters.get('roles', 'exec') == 'exec' else 'all insiders including directors'})"
    )


class ScoringSizeModal(discord.ui.Modal, title="Scoring & Size Filters"):
    def __init__(self, filters: dict):
        super().__init__()
        cap = filters.get("market_cap", {})
        flt = filters.get("float", {})
        self.min_score = discord.ui.TextInput(
            label="Min Score (0–100)",
            default=str(filters.get("min_score", 30)),
            placeholder="30 = Notable+   50 = Strong+   70 = Exceptional only",
        )
        self.min_cap = discord.ui.TextInput(
            label="Min Market Cap",
            default=f"{cap.get('min', 10_000_000)/1e6:.0f}M",
            placeholder="e.g. 10M",
        )
        self.max_cap = discord.ui.TextInput(
            label="Max Market Cap",
            default=f"{cap.get('max', 500_000_000)/1e6:.0f}M",
            placeholder="e.g. 500M",
        )
        self.max_float = discord.ui.TextInput(
            label="Max Float Shares",
            default=f"{flt.get('max', 50_000_000)/1e6:.0f}M",
            placeholder="e.g. 50M",
        )
        for item in (self.min_score, self.min_cap, self.max_cap, self.max_float):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            filters = _load_filters()
            score = int(float(self.min_score.value))
            if not (0 <= score <= 100):
                await interaction.response.send_message("Min score must be 0–100.", ephemeral=True)
                return
            filters["min_score"] = score
            filters.setdefault("market_cap", {})["min"] = int(_parse_filter_number(self.min_cap.value))
            filters.setdefault("market_cap", {})["max"] = int(_parse_filter_number(self.max_cap.value))
            filters.setdefault("float",      {})["max"] = int(_parse_filter_number(self.max_float.value))
            _save_filters(filters)
            tier = "Notable" if score < 50 else "Strong" if score < 70 else "Exceptional"
            await interaction.response.send_message(
                f"Scoring & Size updated. Min score: **{score}** ({tier}+)", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class PriceBuyModal(discord.ui.Modal, title="Price & Buy Filters"):
    def __init__(self, filters: dict):
        super().__init__()
        prc = filters.get("price", {})
        self.min_price = discord.ui.TextInput(
            label="Min Price ($)", default=str(prc.get("min", 1.0)), placeholder="e.g. 1",
        )
        self.max_price = discord.ui.TextInput(
            label="Max Price ($)", default=str(prc.get("max", 50.0)), placeholder="e.g. 50",
        )
        self.min_buy = discord.ui.TextInput(
            label="Min Buy Value ($)", default=str(filters.get("min_buy_value", 25_000)),
            placeholder="e.g. 25000 or 25K",
        )
        for item in (self.min_price, self.max_price, self.min_buy):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            filters = _load_filters()
            filters.setdefault("price", {})["min"] = float(self.min_price.value)
            filters.setdefault("price", {})["max"] = float(self.max_price.value)
            filters["min_buy_value"] = int(_parse_filter_number(self.min_buy.value))
            _save_filters(filters)
            await interaction.response.send_message("Price & Buy filters updated.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class RolesDiscoveryModal(discord.ui.Modal, title="Roles & Discovery"):
    def __init__(self, filters: dict):
        super().__init__()
        self.roles = discord.ui.TextInput(
            label="Roles (exec or all)", default=filters.get("roles", "exec"),
            placeholder="exec = CEO/CFO/COO/CTO/President   all = includes directors",
        )
        self.enabled = discord.ui.TextInput(
            label="Discovery enabled (yes or no)",
            default="yes" if filters.get("enabled", True) else "no",
            placeholder="yes or no",
        )
        for item in (self.roles, self.enabled):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        roles_val   = self.roles.value.strip().lower()
        enabled_val = self.enabled.value.strip().lower()
        if roles_val not in ("exec", "all"):
            await interaction.response.send_message("Roles must be `exec` or `all`.", ephemeral=True)
            return
        if enabled_val not in ("yes", "no"):
            await interaction.response.send_message("Enabled must be `yes` or `no`.", ephemeral=True)
            return
        filters = _load_filters()
        filters["roles"]   = roles_val
        filters["enabled"] = enabled_val == "yes"
        _save_filters(filters)
        status = "enabled" if filters["enabled"] else "disabled"
        await interaction.response.send_message(
            f"Roles set to **{roles_val}**, discovery **{status}**.", ephemeral=True
        )


class FilterView(discord.ui.View):
    def __init__(self, filters: dict):
        super().__init__(timeout=300)
        self.filters = filters

    @discord.ui.button(label="Scoring & Size", style=discord.ButtonStyle.primary)
    async def scoring_size(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScoringSizeModal(self.filters))

    @discord.ui.button(label="Price & Buy", style=discord.ButtonStyle.primary)
    async def price_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PriceBuyModal(self.filters))

    @discord.ui.button(label="Roles & Discovery", style=discord.ButtonStyle.secondary)
    async def roles_discovery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RolesDiscoveryModal(self.filters))

    @discord.ui.button(label="Reset Defaults", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        from scanners.discovery import _default_filters
        _save_filters(_default_filters())
        await interaction.response.send_message("Filters reset to defaults.", ephemeral=True)


# ── Bot client ─────────────────────────────────────────────────────────────────

class SECBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced")

    async def on_ready(self):
        print(f"Discord bot online: {self.user}")
        _post_status("🟢 **SINSI is online.** Alerts are active.")


client = SECBot()


# ── Blocking helpers ───────────────────────────────────────────────────────────

def _do_lookup(ticker: str) -> str:
    from data import edgar
    from data.market import calc_squeeze_score, get_borrow_rate, get_market_data
    import discord_bot as db

    result = edgar.resolve_ticker(ticker)
    if result is None:
        return f"Could not find **${ticker}** on EDGAR."
    cik, title = result

    try:
        market = get_market_data(ticker)
    except Exception:
        market = {}
    try:
        borrow = get_borrow_rate(ticker)
    except Exception:
        borrow = None

    score, factors = calc_squeeze_score(market, borrow)

    txns: list[dict] = []
    try:
        filings = edgar.fetch_recent_form4s(cik, lookback_days=30)
        for f in filings[:15]:
            details = edgar.fetch_form4_details(cik, f["accession"], f["primary_doc"])
            if not details:
                continue
            for txn in details["transactions"]:
                if txn["code"] == "P" and txn["acquired"] and txn["value"] > 0:
                    txns.append({**txn, "date": f["filed"],
                                 "owner_name": details["owner_name"],
                                 "role": details["role"]})
                elif txn["code"] == "S" and not txn["acquired"] and txn["value"] > 0:
                    txns.append({**txn, "date": f["filed"],
                                 "owner_name": details["owner_name"],
                                 "role": details["role"]})
            time.sleep(0.15)
    except Exception:
        pass

    db.post_lookup(ticker, title, market, score, factors, borrow, txns)
    return f"Lookup for **${ticker}** posted to the alerts channel."


def _do_add(ticker: str, added_by: str = "") -> str:
    from datetime import date
    from data import edgar
    from data.market import get_market_data
    import discord_bot as db

    wl_file = Path("watchlist.json")
    wl = json.loads(wl_file.read_text()) if wl_file.exists() else {"tickers": []}

    if any(e["ticker"] == ticker for e in wl["tickers"]):
        return f"**${ticker}** is already on the watchlist."

    result = edgar.resolve_ticker(ticker)
    if result is None:
        return f"Could not find **${ticker}** on EDGAR."
    cik, title = result

    added_price = None
    try:
        added_price = get_market_data(ticker).get("price")
    except Exception:
        pass

    entry = {
        "ticker":      ticker,
        "cik":         cik,
        "name":        title,
        "added_date":  date.today().isoformat(),
        "added_price": added_price,
    }
    if added_by:
        entry["added_by"] = added_by

    wl["tickers"].append(entry)
    wl_file.write_text(json.dumps(wl, indent=2))
    db.update_watchlist_board(wl["tickers"])

    price_info = f" at ${added_price:.2f}" if added_price else ""
    by_info    = f" (added by {added_by})" if added_by else ""
    return f"Added **${ticker}** ({title}){price_info}{by_info}"


def _do_remove(ticker: str) -> str:
    import discord_bot as db

    wl_file = Path("watchlist.json")
    if not wl_file.exists():
        return f"**${ticker}** is not on the watchlist."

    wl = json.loads(wl_file.read_text())
    before = len(wl["tickers"])
    wl["tickers"] = [e for e in wl["tickers"] if e["ticker"] != ticker]

    if len(wl["tickers"]) == before:
        return f"**${ticker}** is not on the watchlist."

    wl_file.write_text(json.dumps(wl, indent=2))
    db.update_watchlist_board(wl["tickers"])
    return f"Removed **${ticker}** from the watchlist."


def _do_perf() -> list[str]:
    from data.market import get_market_data

    wl = _load_watchlist()
    tickers = wl.get("tickers", [])
    if not tickers:
        return ["Watchlist is empty."]

    lines = []
    for entry in tickers:
        ticker      = entry["ticker"]
        added_price = entry.get("added_price")
        added_date  = entry.get("added_date", "?")
        try:
            price = get_market_data(ticker).get("price")
        except Exception:
            price = None

        add_str   = f"${added_price:.2f}" if added_price else "N/A"
        price_str = f"${price:.2f}"       if price       else "N/A"
        perf_str  = f"{(price - added_price) / added_price:+.1%}" if (price and added_price) else "N/A"

        lines.append(f"**${ticker}** — added {added_date} at {add_str}, now {price_str} ({perf_str})")
        time.sleep(1.1)

    return lines


def _do_scores() -> list[dict]:
    from data.market import calc_squeeze_score, get_borrow_rate, get_market_data

    wl = _load_watchlist()
    results = []
    for entry in wl.get("tickers", []):
        ticker = entry["ticker"]
        try:
            market = get_market_data(ticker)
            borrow = get_borrow_rate(ticker)
            score, factors = calc_squeeze_score(market, borrow)
            results.append({"ticker": ticker, "score": score, "factors": factors})
        except Exception:
            results.append({"ticker": ticker, "score": 0, "factors": []})
        time.sleep(1.2)
    return results


# ── Slash commands ─────────────────────────────────────────────────────────────

@client.tree.command(name="lookup", description="Post a snapshot for any ticker to the alerts channel")
@app_commands.describe(ticker="Ticker symbol, e.g. ONDS or AMPG")
@app_commands.autocomplete(ticker=_watchlist_autocomplete)
async def slash_lookup(interaction: discord.Interaction, ticker: str):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    ticker = ticker.upper()
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, _do_lookup, ticker)
    await interaction.followup.send(msg, view=AddToWatchlistView(ticker), ephemeral=True)


@client.tree.command(name="add", description="Add a ticker to the watchlist")
@app_commands.describe(ticker="Ticker symbol, e.g. ONDS")
async def slash_add(interaction: discord.Interaction, ticker: str):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    ticker = ticker.upper()
    added_by = interaction.user.display_name
    await interaction.response.defer(ephemeral=False)
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, _do_add, ticker, added_by)
    await interaction.followup.send(msg)


@client.tree.command(name="remove", description="Remove a ticker from the watchlist")
async def slash_remove(interaction: discord.Interaction):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    tickers = _load_watchlist().get("tickers", [])
    if not tickers:
        await interaction.response.send_message("Watchlist is empty.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Select a ticker to remove:",
        view=RemoveSelectView(tickers),
        ephemeral=True,
    )


@client.tree.command(name="list", description="Show the current watchlist")
async def slash_list(interaction: discord.Interaction):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    tickers = _load_watchlist().get("tickers", [])
    if not tickers:
        await interaction.response.send_message("Watchlist is empty.", ephemeral=True)
        return
    lines = [
        f"**${e['ticker']}** — {e['name']}" +
        (f" · added by {e['added_by']}" if e.get("added_by") else "")
        for e in tickers
    ]
    pages = _paginate(lines, per_page=10)
    pv    = PaginatedView(pages)
    await interaction.response.send_message(pv._content(), view=pv if len(pages) > 1 else None, ephemeral=True)


@client.tree.command(name="perf", description="Show price performance since each ticker was added")
async def slash_perf(interaction: discord.Interaction):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    loop  = asyncio.get_event_loop()
    lines = await loop.run_in_executor(None, _do_perf)
    pages = _paginate(lines, per_page=8)
    pv    = PaginatedView(pages)
    await interaction.followup.send(pv._content(), view=pv if len(pages) > 1 else None, ephemeral=True)


@client.tree.command(name="scores", description="Show squeeze scores for all watchlist tickers")
async def slash_scores(interaction: discord.Interaction):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _do_scores)
    if not results:
        await interaction.followup.send("Watchlist is empty.", ephemeral=True)
        return
    lines = []
    for r in results:
        bar = "█" * (r["score"] // 10) + "░" * (10 - r["score"] // 10)
        lines.append(f"**${r['ticker']}**  {bar}  {r['score']}/100")
        for f in r["factors"]:
            lines.append(f"  • {f}")
        lines.append("")
    pages = _paginate(lines, per_page=15)
    pv    = PaginatedView(pages)
    await interaction.followup.send(pv._content(), view=pv if len(pages) > 1 else None, ephemeral=True)


@client.tree.command(name="filter", description="View and change discovery filter settings")
async def slash_filter(interaction: discord.Interaction):
    if not _check_channel(interaction):
        await interaction.response.send_message(
            "This command only works in the designated alerts channel.", ephemeral=True
        )
        return
    filters = _load_filters()
    await interaction.response.send_message(
        _format_filters(filters), view=FilterView(filters), ephemeral=True,
    )


# ── Context menu: right-click any message containing $TICKER ──────────────────

@client.tree.context_menu(name="Lookup in SINSI")
async def context_lookup(interaction: discord.Interaction, message: discord.Message):
    tickers = re.findall(r'\$([A-Za-z]{1,5})', message.content)
    if not tickers:
        await interaction.response.send_message(
            "No ticker found. Messages need a `$TICKER` mention, e.g. `$ONDS`.",
            ephemeral=True,
        )
        return
    ticker = tickers[0].upper()
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()
    msg  = await loop.run_in_executor(None, _do_lookup, ticker)
    await interaction.followup.send(msg, view=AddToWatchlistView(ticker), ephemeral=True)


# ── Status webhook ─────────────────────────────────────────────────────────────

def _post_status(message: str) -> None:
    import requests
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": message}, timeout=5)
    except Exception:
        pass


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN not set in .env\n"
            "Get one at: https://discord.com/developers/applications"
        )
    try:
        client.run(TOKEN)
    finally:
        _post_status(
            "🔴 **SINSI has gone offline.** Alerts are paused.\n"
            "Slash commands won't respond until it's back. Spam Tijn to turn his PC on."
        )
