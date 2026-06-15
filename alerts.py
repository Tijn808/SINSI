"""
Unified alert card — one embed per ticker per trigger event.

Instead of each scanner independently posting to Discord, qualifying signals
are collected into an InsiderBuyAlert and rendered as a single card showing:
  • Significance score + tier
  • Three-dimension breakdown: Conviction / Materiality / Corroboration
  • Transaction details
  • Market context: squeeze score, borrow rate, price action
  • Score breakdown (why this scored what it did)

Standalone signals (squeeze-only, dilution-only, activist-only) still post
independently via discord_bot.py. The Alert is only built when an insider buy
is the primary trigger.
"""

from dataclasses import dataclass, field


@dataclass
class InsiderBuyAlert:
    # ── Primary signal ─────────────────────────────────────────────────────────
    ticker:      str
    company:     str
    filing:      dict        # {accession, filed, link}
    details:     dict        # {owner_name, role, is_10b5_plan, is_officer, is_director}
    txn:         dict        # {shares, price, value, owned_after, acquired}
    score:       int
    sig_factors: list[str]

    # ── Market data (from Finviz) ──────────────────────────────────────────────
    market: dict = field(default_factory=dict)

    # ── Enriched context ───────────────────────────────────────────────────────
    squeeze_score:   int | None   = None
    squeeze_factors: list[str]    = field(default_factory=list)
    borrow_rate:     float | None = None
    is_cluster:      bool         = False
    cluster_buys:    list[dict]   = field(default_factory=list)

    # ── Source flags ───────────────────────────────────────────────────────────
    is_director_special: bool = False
    is_discovery:        bool = False

    # ── Tier ───────────────────────────────────────────────────────────────────

    def tier(self) -> tuple[str, str]:
        """(label, emoji)"""
        if self.score >= 70: return "Exceptional", "💎"
        if self.score >= 50: return "Strong",      "🔥"
        return                      "Notable",     "⭐"

    # ── Three-dimension summaries ──────────────────────────────────────────────

    def _conviction_summary(self) -> str:
        parts = []
        owned_after = self.txn.get("owned_after") or 0
        shares      = self.txn.get("shares")      or 0
        pos_before  = owned_after - shares

        if pos_before > 0:
            pct = shares / pos_before
            parts.append(f"+{pct:.0%} position")
        elif pos_before <= 0 < shares:
            parts.append("New position")

        if not self.details.get("is_10b5_plan"):
            parts.append("Discretionary")

        price  = self.market.get("price")
        low_52 = self.market.get("week_52_low")
        if price and low_52 and low_52 > 0 and (price - low_52) / low_52 <= 0.20:
            parts.append("Near 52W low")

        return " · ".join(parts) or "—"

    def _materiality_summary(self) -> str:
        value    = self.txn.get("value")    or 0
        cap      = self.market.get("market_cap")
        price    = self.market.get("price")
        float_sh = self.market.get("float_shares")
        avg_vol  = self.market.get("avg_volume")

        parts = []
        if cap and cap > 0:
            parts.append(f"{value / cap:.2%} of cap")
        if float_sh and price and float_sh > 0:
            parts.append(f"{value / (float_sh * price):.2%} of float")
        if avg_vol and avg_vol > 0 and price:
            days = value / (avg_vol * price)
            if days >= 0.3:
                parts.append(f"{days:.1f}× avg vol")

        return " · ".join(parts) if parts else _fmt_usd(value)

    def _corroboration_summary(self) -> str:
        parts = [self.details.get("role", "Insider")]
        if self.is_cluster:
            n = len(self.cluster_buys) + 1
            parts.append(f"Cluster ×{n}")
        if self.squeeze_score and self.squeeze_score >= 40:
            parts.append(f"Squeeze {self.squeeze_score}/100")
        if self.borrow_rate and self.borrow_rate >= 0.05:
            parts.append(f"Borrow {self.borrow_rate:.1%}")
        return " · ".join(parts)

    # ── Render ─────────────────────────────────────────────────────────────────

    def render(self) -> tuple[dict, list[dict]]:
        """
        Build (embed dict, buttons list) ready to POST to Discord.
        Color is set by post() based on channel.
        """
        from data.market import price_action_summary

        tier_label, tier_emoji = self.tier()
        is_big  = self.txn.get("value", 0) >= 500_000
        is_plan = self.details.get("is_10b5_plan", False)

        # ── Title ──────────────────────────────────────────────────────────────
        if self.is_director_special:
            title = f"🎯 Director Buy — ${self.ticker}"
        elif tier_label == "Exceptional":
            title = f"💎 Exceptional Insider Buy — ${self.ticker}"
        elif tier_label == "Strong":
            title = f"🔥 Strong Insider Buy — ${self.ticker}"
        else:
            title = f"⭐ Insider Buy — ${self.ticker}"

        # ── Description ────────────────────────────────────────────────────────
        plan_line = (
            "📋 Pre-scheduled 10b5-1 plan"
            if is_plan else
            "✅ Open market purchase — not a 10b5-1 plan"
        )
        if self.is_director_special:
            short = self.market.get("short_pct_float")
            extra = f"\n> 🎯 Micro-cap director · {short:.0%} short interest" if short else "\n> 🎯 Micro-cap director"
        else:
            extra = ""

        description = (
            f"**{self.details['owner_name']}** ({self.details['role']}) bought "
            f"**{_fmt_usd(self.txn['value'])}** of **${self.ticker}**\n"
            f"> {plan_line}{extra}"
        )

        # ── Fields ─────────────────────────────────────────────────────────────
        fields = [
            # Score bar
            {
                "name":   f"Significance  ·  {tier_label}  {tier_emoji}",
                "value":  _score_bar(self.score),
                "inline": False,
            },
            # Three-dimension breakdown
            {"name": "Conviction",    "value": self._conviction_summary(),    "inline": True},
            {"name": "Materiality",   "value": self._materiality_summary(),   "inline": True},
            {"name": "Corroboration", "value": self._corroboration_summary(), "inline": True},
            # Transaction details
            {"name": "Shares", "value": f"{self.txn.get('shares', 0):,.0f}", "inline": True},
            {"name": "Price",  "value": f"${self.txn.get('price', 0):.2f}",  "inline": True},
            {"name": "Value",  "value": f"**{_fmt_usd(self.txn.get('value', 0))}**", "inline": True},
        ]

        # Market context (squeeze + price action on one field)
        ctx_parts = []
        short = self.market.get("short_pct_float")
        dtc   = self.market.get("short_ratio")
        cap   = self.market.get("market_cap")

        if short:     ctx_parts.append(f"Short **{short:.0%}**")
        if dtc:       ctx_parts.append(f"DTC **{dtc:.1f}**")
        if self.borrow_rate:
            ctx_parts.append(f"Borrow **{self.borrow_rate:.1%}**")
        if self.squeeze_score is not None:
            ctx_parts.append(f"Squeeze **{self.squeeze_score}/100**")
        if cap:
            ctx_parts.append(f"Cap **{_fmt_cap(cap)}**")

        pa_lines, pa_notable = price_action_summary(self.market)

        ctx_lines = []
        if ctx_parts:
            ctx_lines.append("  ·  ".join(ctx_parts))
        if pa_lines:
            ctx_lines.append("  ·  ".join(pa_lines))

        if ctx_lines:
            fields.append({
                "name":   "Market Context" + (" ⚡" if pa_notable else ""),
                "value":  "\n".join(ctx_lines),
                "inline": False,
            })

        # Score breakdown (why)
        if self.sig_factors:
            fields.append({
                "name":   "Score Breakdown",
                "value":  "\n".join(f"• {f}" for f in self.sig_factors),
                "inline": False,
            })

        # ── Embed ──────────────────────────────────────────────────────────────
        embed = {
            "author":      {"name": self.company},
            "title":       title,
            "url":         self.filing.get("link", ""),
            "description": description,
            "fields":      fields,
            "footer":      {
                "text": (
                    f"Filed {self.filing.get('filed', '?')} · "
                    f"{self.filing.get('accession', '')}"
                )
            },
        }

        # ── Buttons ────────────────────────────────────────────────────────────
        btns = [
            ("📄 View Filing", self.filing.get("link", "")),
            ("📊 Finviz",      f"https://finviz.com/quote.ashx?t={self.ticker}"),
        ]
        if self.borrow_rate and self.borrow_rate >= 0.05:
            btns.append(("💸 iborrowdesk", f"https://iborrowdesk.com/report/{self.ticker}"))

        return embed, _buttons(*btns)

    def post(self) -> None:
        """Post this alert to Discord."""
        from discord_bot import _post
        embed, components = self.render()
        channel = "discovery" if self.is_discovery else "insider_buy"
        _post(channel, embed, components)


# ── Shared formatting helpers (used by both Alert and discord_bot) ─────────────

def _fmt_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _fmt_cap(cap: float | None) -> str:
    if cap is None:
        return "N/A"
    if cap >= 1e12: return f"${cap/1e12:.1f}T"
    if cap >= 1e9:  return f"${cap/1e9:.1f}B"
    return f"${cap/1e6:.0f}M"


def _score_bar(score: int, total: int = 100) -> str:
    filled = round(score / total * 10)
    return f"`{'█' * filled}{'░' * (10 - filled)}`  **{score}/{total}**"


def _buttons(*links: tuple[str, str]) -> list[dict]:
    return [{
        "type": 1,
        "components": [
            {"type": 2, "style": 5, "label": label, "url": url}
            for label, url in links
            if url
        ],
    }]
