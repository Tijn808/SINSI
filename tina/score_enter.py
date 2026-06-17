"""Significance scoring for institutional ENTER (and ADD) events.

Three-dimensional framework mirroring SINSI's insider-buy scorer so both bots
speak one language and can be combined into a cross-bot convergence score:

  Conviction   — how much does THIS fund believe in it?
  Materiality  — how significant is the position relative to the company?
  Corroboration — do independent signals agree?

Final score: 0–100, multiplicative across dimensions so all three must
contribute for a position to score high. A mega-cap toe-dip from a quant fund
never looks like a concentrated-fund small-cap bet with insider buying.

Usage:
    score, factors = score_enter(ev, all_positions, fund_style, market_cap,
                                 consensus_funds, sinsi_crossref)
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flow import FlowEvent


# ── Tunable weights (adjust without touching logic) ────────────────────────────

# Conviction sub-weights (must sum to 1.0)
_W_WEIGHT    = 0.50   # portfolio weight % — the single best signal
_W_RANK      = 0.20   # position rank within fund (top-5 = strong)
_W_OUTSIZED  = 0.20   # vs fund's own median position
_W_FUNDMULT  = 0.10   # HHI-derived style multiplier

# Saturation thresholds
_WEIGHT_SAT   = 5.0   # % of AUM at which conviction saturates (score=1.0)
_MATERIAL_SAT = 0.05  # 5% of company's market cap saturates materiality

# Exponents for multiplicative combination (sum to 1.0)
_EXP_CONV    = 0.40
_EXP_MAT     = 0.30
_EXP_CORR    = 0.30

# Corroboration boosts (raw additions before cap at 1.0)
_CORR_BASE       = 0.20   # baseline even with no external corroboration
_CORR_SINSI      = 0.50   # SINSI insider-buy crossref — the prize signal
_CORR_CONSENSUS  = 0.25   # 3+ independent funds in same quarter
_CORR_OVERLAP_2  = 0.10   # 2-fund co-holder (weaker)


def score_enter(
    ev: "FlowEvent",
    all_positions: dict,          # {cusip: {value_usd, ticker, ...}} for this fund this quarter
    fund_style: dict,             # {style, conviction, n_positions, ...}
    market_cap: float | None,     # None = unknown; supply from Finviz when available
    consensus_funds: list[dict],  # funds that ALSO entered this ticker this quarter
    sinsi_crossref: bool,         # True if ticker is on SINSI watchlist
) -> tuple[int, list[str]]:
    """Return (score_0_100, factor_lines) for a FlowEvent of type ENTER or ADD."""

    factors: list[str] = []

    # ── 1. Conviction ──────────────────────────────────────────────────────────

    # (a) Portfolio weight — primary signal
    weight_pct     = ev.weight_pct
    weight_factor  = min(weight_pct / _WEIGHT_SAT, 1.0)

    # (b) Rank within fund sorted by value
    values = sorted(
        (p.get("value_usd", 0) for p in all_positions.values()),
        reverse=True,
    )
    rank = next((i + 1 for i, v in enumerate(values) if v <= ev.value_usd), len(values))
    n    = max(len(values), 1)
    if rank <= 3:
        rank_factor = 1.0
    elif rank <= 5:
        rank_factor = 0.85
    elif rank <= 10:
        rank_factor = 0.65
    else:
        rank_factor = max(0.25, 1.0 - rank / n)

    # (c) Outsized relative to fund's own median
    median_weight = statistics.median(
        [p.get("value_usd", 0) / max(sum(p2.get("value_usd", 0) for p2 in all_positions.values()), 1) * 100
         for p in all_positions.values()]
    ) if all_positions else 1.0
    outsized_factor = min(weight_pct / max(median_weight, 0.01), 3.0) / 3.0

    # (d) Fund-type multiplier — already normalized 0.4–1.5
    fund_mult        = fund_style.get("conviction", 1.0)
    fund_mult_factor = fund_mult / 1.5  # normalize to 0–1

    conviction = (
        _W_WEIGHT   * weight_factor   +
        _W_RANK     * rank_factor     +
        _W_OUTSIZED * outsized_factor +
        _W_FUNDMULT * fund_mult_factor
    )
    conviction = min(conviction * fund_mult, 1.0)  # final scale by multiplier, cap at 1

    if weight_pct >= _WEIGHT_SAT:
        factors.append(f"Top conviction: {weight_pct:.1f}% of book")
    elif weight_pct >= 2.0:
        factors.append(f"Meaningful weight: {weight_pct:.1f}% of book")
    if rank <= 5:
        factors.append(f"Top-{rank} holding for this fund")
    if outsized_factor > 0.6:
        factors.append(f"{weight_pct / max(median_weight, 0.01):.1f}× fund's median position")
    if fund_mult >= 1.5:
        factors.append(f"Concentrated fund (×{fund_mult} conviction)")
    elif fund_mult <= 0.4:
        factors.append(f"Quant fund (×{fund_mult} conviction — lower weight)")

    # ── 2. Materiality ─────────────────────────────────────────────────────────

    if market_cap and market_cap > 0:
        pct_of_company = ev.value_usd / market_cap
        materiality    = min(pct_of_company / _MATERIAL_SAT, 1.0)
        if pct_of_company >= 0.05:
            factors.append(f"Absorbed {pct_of_company*100:.1f}% of company")
        elif pct_of_company >= 0.01:
            factors.append(f"Absorbed {pct_of_company*100:.1f}% of company ({_fmt_cap(market_cap)} cap)")
    else:
        # Market cap unknown — use conviction only, neutral materiality
        materiality = 0.5
        factors.append("Market cap unknown — materiality not scored")

    # ── 3. Corroboration ───────────────────────────────────────────────────────

    corroboration = _CORR_BASE

    if sinsi_crossref:
        corroboration += _CORR_SINSI
        factors.append("🔗 SINSI insider crossref")

    if consensus_funds:
        # Weight consensus by quality of entering funds
        avg_mult = (
            sum(f.get("conviction", 1.0) for f in consensus_funds) / len(consensus_funds)
        )
        quality_scale = avg_mult / 1.5  # normalize
        if len(consensus_funds) >= 2:
            corroboration += _CORR_CONSENSUS * quality_scale
            fund_names = ", ".join(f["fund"] for f in consensus_funds[:3])
            factors.append(f"🧠 Consensus: {len(consensus_funds)+1} funds entered this quarter ({fund_names})")
        else:
            corroboration += _CORR_OVERLAP_2 * quality_scale
            factors.append(f"📍 Also held by {consensus_funds[0]['fund']}")

    corroboration = min(corroboration, 1.0)

    # ── Final score ────────────────────────────────────────────────────────────

    # Multiplicative: all three dimensions must contribute for a high score
    score = int(100 * (conviction ** _EXP_CONV) * (materiality ** _EXP_MAT) * (corroboration ** _EXP_CORR))
    score = max(0, min(score, 100))

    return score, factors


def score_add(
    ev: "FlowEvent",
    all_positions: dict,
    fund_style: dict,
    market_cap: float | None,
    consensus_funds: list[dict],
    sinsi_crossref: bool,
) -> tuple[int, list[str]]:
    """Score an ADD event. Reuses score_enter with delta-weighted conviction."""
    # Treat ADD the same as ENTER but use weight_change as the conviction signal
    original_weight = ev.weight_pct
    ev.weight_pct   = ev.weight_change  # temporarily substitute for scoring
    result = score_enter(ev, all_positions, fund_style, market_cap, consensus_funds, sinsi_crossref)
    ev.weight_pct = original_weight
    return result


def _fmt_cap(v: float) -> str:
    if v >= 1e12: return f"${v/1e12:.1f}T"
    if v >= 1e9:  return f"${v/1e9:.1f}B"
    if v >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:.0f}"
