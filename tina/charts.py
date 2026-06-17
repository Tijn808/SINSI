"""Generate matplotlib charts for TINA. All functions return a PNG BytesIO."""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

import config

_BG    = config.CHART_BG
_TEXT  = config.CHART_TEXT
_GREEN = config.CHART_GREEN
_RED   = config.CHART_RED
_BLUE  = config.CHART_BLUE
_GREY  = config.CHART_GREY

_PALETTE = [
    "#5865f2", "#57f287", "#fee75c", "#eb459e", "#ed4245",
    "#3498db", "#e67e22", "#9b59b6", "#1abc9c", "#e91e63",
    "#2ecc71", "#f39c12", "#d35400", "#27ae60", "#8e44ad",
]


def _dark(fig, ax) -> None:
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.tick_params(colors=_TEXT, labelsize=8)
    ax.xaxis.label.set_color(_TEXT)
    ax.yaxis.label.set_color(_TEXT)
    ax.title.set_color(_TEXT)
    for spine in ax.spines.values():
        spine.set_edgecolor(_GREY)


def _save(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=config.CHART_DPI, bbox_inches="tight", facecolor=_BG)
    buf.seek(0)
    plt.close(fig)
    return buf


def _label(h: dict) -> str:
    return (h.get("ticker") or h.get("name", "?"))[:12]


# ── Pie chart ─────────────────────────────────────────────────────────────────

def pie_chart(fund_name: str, quarter: str, holdings: list[dict], top_n: int = None) -> io.BytesIO:
    """Portfolio allocation pie. holdings: [{name, ticker, value_usd}]"""
    n        = min(top_n or config.CHART_TOP_N, len(holdings))
    sorted_h = sorted(holdings, key=lambda x: x["value_usd"], reverse=True)
    top      = sorted_h[:n]
    other    = sum(h["value_usd"] for h in sorted_h[n:])

    labels = [_label(h) for h in top]
    values = [h["value_usd"] for h in top]
    colors = _PALETTE[: len(top)]

    if other > 0:
        labels.append("Other")
        values.append(other)
        colors.append(_GREY)

    fig, ax = plt.subplots(figsize=(10, 7))
    _dark(fig, ax)

    wedges, _, autotexts = ax.pie(
        values,
        autopct=lambda p: f"{p:.1f}%" if p > 2.5 else "",
        colors=colors,
        startangle=90,
        pctdistance=0.82,
        wedgeprops={"linewidth": 1.5, "edgecolor": _BG},
    )
    for t in autotexts:
        t.set_color(_BG)
        t.set_fontsize(7)
        t.set_fontweight("bold")

    patches = [mpatches.Patch(color=colors[i], label=labels[i]) for i in range(len(labels))]
    ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1, 0.5),
              fontsize=8, facecolor=_BG, edgecolor=_GREY, labelcolor=_TEXT)

    total = sum(values)
    ax.set_title(
        f"{fund_name}\n{quarter} Holdings — ${total/1e9:.2f}B total",
        color=_TEXT, fontsize=12, fontweight="bold", pad=15,
    )
    return _save(fig)


# ── Performance chart ─────────────────────────────────────────────────────────

def performance_chart(fund_name: str, quarter: str, positions: list[dict]) -> io.BytesIO | None:
    """% return per position since filing date.
    positions: [{ticker, name, entry_price, current_price, value_usd}]
    """
    valid = [p for p in positions if p.get("entry_price") and p.get("current_price")]
    if not valid:
        return None

    valid   = sorted(valid, key=lambda x: x["value_usd"], reverse=True)[: config.CHART_TOP_N]
    tickers = [p.get("ticker") or p["name"][:10] for p in valid]
    returns = [(p["current_price"] - p["entry_price"]) / p["entry_price"] * 100 for p in valid]
    colors  = [_GREEN if r >= 0 else _RED for r in returns]

    fig, ax = plt.subplots(figsize=(max(10, len(tickers) * 0.8), 5))
    _dark(fig, ax)

    bars = ax.bar(range(len(tickers)), returns, color=colors, edgecolor=_BG, linewidth=0.5)

    for bar, ret in zip(bars, returns):
        h      = bar.get_height()
        offset = 0.4 if h >= 0 else -0.4
        va     = "bottom" if h >= 0 else "top"
        ax.text(bar.get_x() + bar.get_width() / 2, h + offset,
                f"{ret:+.1f}%", ha="center", va=va, fontsize=7, color=_TEXT, fontweight="bold")

    ax.axhline(0, color=_GREY, linewidth=1)
    ax.set_xticks(range(len(tickers)))
    ax.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8, color=_TEXT)
    ax.set_ylabel("Return since filing (%)", color=_TEXT, fontsize=9)
    ax.set_title(f"{fund_name} — Performance Since {quarter} Filing",
                 color=_TEXT, fontsize=12, fontweight="bold")
    ax.grid(axis="y", alpha=0.2, color=_GREY)
    return _save(fig)


# ── Summary chart ─────────────────────────────────────────────────────────────

def summary_chart(fund_performances: list[dict]) -> io.BytesIO | None:
    """Weighted-average portfolio return per followed fund."""
    if not fund_performances:
        return None

    data    = sorted(fund_performances, key=lambda x: x["weighted_return"], reverse=True)
    names   = [f["name"][:22] for f in data]
    returns = [f["weighted_return"] for f in data]
    colors  = [_GREEN if r >= 0 else _RED for r in returns]

    fig, ax = plt.subplots(figsize=(max(8, len(names) * 1.6), 6))
    _dark(fig, ax)

    bars = ax.bar(range(len(names)), returns, color=colors, edgecolor=_BG)

    for bar, ret, f in zip(bars, returns, data):
        h      = bar.get_height()
        offset = 0.3 if h >= 0 else -0.3
        va     = "bottom" if h >= 0 else "top"
        aum    = f["total_value_usd"] / 1e9
        ax.text(bar.get_x() + bar.get_width() / 2, h + offset,
                f"{ret:+.1f}%\n${aum:.1f}B", ha="center", va=va, fontsize=8, color=_TEXT)

    ax.axhline(0, color=_GREY, linewidth=1)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=9, color=_TEXT)
    ax.set_ylabel("Weighted portfolio return (%)", color=_TEXT)
    ax.set_title("TINA — Institutional Performance Summary",
                 color=_TEXT, fontsize=13, fontweight="bold")
    ax.grid(axis="y", alpha=0.2, color=_GREY)
    return _save(fig)


# ── Consensus chart ───────────────────────────────────────────────────────────

def consensus_chart(ticker_data: list[dict]) -> io.BytesIO | None:
    """Bar chart ranking tickers by total institutional conviction.

    ticker_data: [{ticker, total_value_usd, fund_count, funds}]
    """
    if not ticker_data:
        return None

    data    = sorted(ticker_data, key=lambda x: x["total_value_usd"], reverse=True)[:20]
    tickers = [d["ticker"] for d in data]
    values  = [d["total_value_usd"] / 1e6 for d in data]
    counts  = [d["fund_count"] for d in data]

    fig, ax = plt.subplots(figsize=(max(10, len(tickers) * 0.9), 5))
    _dark(fig, ax)

    bars = ax.bar(range(len(tickers)), values, color=_BLUE, edgecolor=_BG, alpha=0.85)

    for bar, val, count in zip(bars, values, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"${val:.0f}M\n{count} fund{'s' if count > 1 else ''}",
                ha="center", va="bottom", fontsize=6.5, color=_TEXT)

    ax.set_xticks(range(len(tickers)))
    ax.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8, color=_TEXT)
    ax.set_ylabel("Total institutional value ($M)", color=_TEXT, fontsize=9)
    ax.set_title("TINA — Institutional Consensus (Top Holdings Across All Funds)",
                 color=_TEXT, fontsize=12, fontweight="bold")
    ax.grid(axis="y", alpha=0.2, color=_GREY)
    return _save(fig)


# ── Compare chart ─────────────────────────────────────────────────────────────

def compare_chart(name1: str, name2: str, shared: list[dict]) -> io.BytesIO | None:
    """Grouped bar chart comparing two funds' positions in shared stocks.

    shared: [{ticker, value1, value2}]
    """
    if not shared:
        return None

    shared  = sorted(shared, key=lambda x: x["value1"] + x["value2"], reverse=True)[:15]
    tickers = [s["ticker"] for s in shared]
    v1      = [s["value1"] / 1e6 for s in shared]
    v2      = [s["value2"] / 1e6 for s in shared]
    x       = range(len(tickers))
    w       = 0.35

    fig, ax = plt.subplots(figsize=(max(10, len(tickers) * 1.0), 6))
    _dark(fig, ax)

    ax.bar([i - w/2 for i in x], v1, w, label=name1[:20], color=_PALETTE[0], edgecolor=_BG)
    ax.bar([i + w/2 for i in x], v2, w, label=name2[:20], color=_PALETTE[1], edgecolor=_BG)

    ax.set_xticks(list(x))
    ax.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8, color=_TEXT)
    ax.set_ylabel("Position value ($M)", color=_TEXT, fontsize=9)
    ax.set_title(f"Shared Holdings — {name1[:18]} vs {name2[:18]}",
                 color=_TEXT, fontsize=12, fontweight="bold")
    ax.legend(facecolor=_BG, edgecolor=_GREY, labelcolor=_TEXT, fontsize=9)
    ax.grid(axis="y", alpha=0.2, color=_GREY)
    return _save(fig)


# ── Ticker institutional history chart ───────────────────────────────────────

def ticker_history_chart(ticker: str, fund_series: list[dict]) -> io.BytesIO | None:
    """Line chart showing each fund's holding in a ticker over time.

    fund_series: [{name, quarters: ['2024-03-31', ...], values: [1_200_000, ...]}]
    quarters are oldest→newest, values aligned.
    """
    if not fund_series:
        return None

    all_quarters = sorted({q for f in fund_series for q in f["quarters"]})
    if len(all_quarters) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 5))
    _dark(fig, ax)

    for i, f in enumerate(fund_series):
        q_map  = dict(zip(f["quarters"], f["values"]))
        values = [q_map.get(q, 0) / 1e6 for q in all_quarters]
        ax.plot(all_quarters, values, marker="o", label=f["name"][:22],
                color=_PALETTE[i % len(_PALETTE)], linewidth=2, markersize=5)

    ax.set_ylabel("Position value ($M)", color=_TEXT, fontsize=9)
    ax.set_title(f"${ticker} — Institutional Holdings Over Time",
                 color=_TEXT, fontsize=12, fontweight="bold")
    ax.legend(facecolor=_BG, edgecolor=_GREY, labelcolor=_TEXT, fontsize=8)
    ax.tick_params(axis="x", rotation=30)
    ax.grid(alpha=0.2, color=_GREY)
    ax.set_ylim(bottom=0)
    return _save(fig)


# ── Pile-in chart ────────────────────────────────────────────────────────────

def pile_in_chart(results: list[dict]) -> io.BytesIO | None:
    """Bar chart for small-cap institutional convergence.

    results: [{ticker, market_cap, fund_count, total_value, funds: [{fund, value}]}]
    """
    if not results:
        return None

    data    = results[:20]
    tickers = [r["ticker"] for r in data]
    values  = [r["total_value"] / 1e6 for r in data]
    counts  = [r["fund_count"] for r in data]
    colors  = [_PALETTE[min(c - 1, len(_PALETTE) - 1)] for c in counts]

    fig, ax = plt.subplots(figsize=(max(10, len(tickers) * 1.0), 6))
    _dark(fig, ax)

    bars = ax.bar(range(len(tickers)), values, color=colors, edgecolor=_BG, alpha=0.9)

    for bar, count, r in zip(bars, counts, data):
        cap = r["market_cap"]
        cap_str = f"${cap/1e6:.0f}M" if cap < 1e9 else f"${cap/1e9:.1f}B"
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max(values) * 0.01,
            f"{count} fund{'s' if count > 1 else ''}\n{cap_str}",
            ha="center", va="bottom", fontsize=6.5, color=_TEXT,
        )

    ax.set_xticks(range(len(tickers)))
    ax.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8, color=_TEXT)
    ax.set_ylabel("Total institutional position ($M)", color=_TEXT, fontsize=9)
    ax.set_title("TINA — Small-Cap Institutional Convergence",
                 color=_TEXT, fontsize=12, fontweight="bold")
    ax.grid(axis="y", alpha=0.2, color=_GREY)

    # Legend: color = number of funds
    seen_counts = sorted(set(counts))
    patches = [
        mpatches.Patch(color=_PALETTE[min(c - 1, len(_PALETTE) - 1)], label=f"{c} fund{'s' if c > 1 else ''}")
        for c in seen_counts
    ]
    ax.legend(handles=patches, loc="upper right", facecolor=_BG,
              edgecolor=_GREY, labelcolor=_TEXT, fontsize=8)

    return _save(fig)


# ── History chart ─────────────────────────────────────────────────────────────

def history_chart(fund_name: str, quarters: list[str], top_tickers: list[str],
                  values_by_ticker: dict[str, list[float]]) -> io.BytesIO | None:
    """Line chart showing top positions across multiple quarters.

    quarters: ['2024-03-31', '2023-12-31', ...]  (newest first)
    top_tickers: tickers to plot
    values_by_ticker: {ticker: [value_q0, value_q1, ...]} aligned with quarters
    """
    if not quarters or not top_tickers:
        return None

    quarters_display = list(reversed(quarters))  # oldest first for x-axis

    fig, ax = plt.subplots(figsize=(10, 5))
    _dark(fig, ax)

    for i, ticker in enumerate(top_tickers[:8]):
        vals = list(reversed(values_by_ticker.get(ticker, [0] * len(quarters))))
        vals_m = [v / 1e6 for v in vals]
        ax.plot(quarters_display, vals_m, marker="o", label=ticker,
                color=_PALETTE[i % len(_PALETTE)], linewidth=2, markersize=5)

    ax.set_ylabel("Position value ($M)", color=_TEXT, fontsize=9)
    ax.set_title(f"{fund_name} — Top Position History",
                 color=_TEXT, fontsize=12, fontweight="bold")
    ax.legend(facecolor=_BG, edgecolor=_GREY, labelcolor=_TEXT, fontsize=8,
              loc="upper left")
    ax.tick_params(axis="x", rotation=30)
    ax.grid(alpha=0.2, color=_GREY)
    return _save(fig)
