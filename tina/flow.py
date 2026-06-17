"""Quarter-over-quarter position diff engine.

Produces FlowEvent objects for every notable change between two consecutive
13F filings. FlowEvent is the single shared type consumed by both the auto-
post scanner and on-demand commands — so the two can never drift apart.

Event types:
  ENTER  — brand-new position (not in previous quarter)
  ADD    — existing position grown in value
  TRIM   — existing position reduced in value
  EXIT   — position fully gone from current quarter

Conviction weighting:
  weight_pct       = position value / fund total AUM × 100 (current quarter)
  prev_weight_pct  = same for previous quarter
  weight_change    = weight_pct − prev_weight_pct

  A fund moving a name from 0.5% → 4% of book is building conviction.
  A 200% ADD at 0.1% of book is noise. Score already captures this, but
  having the raw weights exposed lets commands filter and sort on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import config
from data import score as scoring

FlowType = Literal["ENTER", "ADD", "TRIM", "EXIT"]


@dataclass
class FlowEvent:
    type: FlowType

    ticker: str
    name: str
    cusip: str
    fund_name: str
    quarter: str

    # Current quarter (0 for EXIT)
    value_usd: float
    shares: int
    weight_pct: float       # % of fund AUM this quarter

    # Previous quarter (0 for ENTER)
    prev_value_usd: float
    prev_weight_pct: float

    # Derived deltas
    delta_usd: float        # + added, − trimmed/exited
    delta_pct: float        # % change from prev (None-safe: inf for ENTER, -100 for EXIT)
    weight_change: float    # weight_pct − prev_weight_pct

    # Signal quality
    score: int
    factors: list[str] = field(default_factory=list)

    # Fund-type tagging (attached by scanner after detect(), not set by diff())
    fund_style: str   = "unknown"  # concentrated | focused | diversified | quant
    conviction: float = 1.0        # signal quality multiplier from fund_type

    # Cost-basis enrichment (populated by enrich_cost_basis, not set by diff())
    cost_basis: dict | None = None
    # {low, high, avg_close, close_end, current_price,
    #  pct_from_avg, pct_from_quarter_end, verdict}

    def is_bullish(self) -> bool:
        return self.type in ("ENTER", "ADD")

    def is_bearish(self) -> bool:
        return self.type in ("TRIM", "EXIT")

    def label(self) -> str:
        return {
            "ENTER": "📈 New Position",
            "ADD":   "📊 Position Added",
            "TRIM":  "📉 Position Trimmed",
            "EXIT":  "🚪 Full Exit",
        }[self.type]

    def color(self) -> int:
        return {
            "ENTER": config.COLOR_NEW,
            "ADD":   config.COLOR_INCREASE,
            "TRIM":  config.COLOR_DECREASE,
            "EXIT":  config.COLOR_EXIT,
        }[self.type]


def diff(
    fund_name: str,
    quarter: str,
    holdings: list[dict],
    prev_positions: dict,
    total_value: float,
    *,
    is_first: bool = False,
) -> list[FlowEvent]:
    """Diff two consecutive 13F quarters and return sorted FlowEvents.

    holdings      — current quarter [{cusip, name, ticker, value_usd, shares, option_type}]
    prev_positions — {cusip: {name, ticker, value_usd, shares, ...}} from state
    total_value   — current quarter total AUM (for conviction weighting)
    is_first      — if True, every position above MIN_POSITION_VALUE_USD is an ENTER
                    regardless of score threshold (initial filing baseline)
    """
    events: list[FlowEvent] = []
    curr_cusips: set[str] = set()

    for h in holdings:
        cusip = h.get("cusip", "")
        if not cusip or h["value_usd"] < config.MIN_POSITION_VALUE_USD:
            continue
        curr_cusips.add(cusip)

        prev = prev_positions.get(cusip)

        # Skip tiny moves unless it's a first-filing baseline
        if prev and not is_first:
            pv   = prev.get("value_usd", 0)
            dpct = abs(h["value_usd"] - pv) / pv * 100 if pv else 100
            dusd = abs(h["value_usd"] - pv)
            if dpct < config.MIN_PCT_CHANGE and dusd < config.MIN_USD_CHANGE:
                continue

        score_val, factors = scoring.score_change(prev, h, total_value)
        if score_val < config.MIN_SCORE and not is_first:
            continue

        prev_val = prev.get("value_usd", 0) if prev else 0
        curr_wt  = h["value_usd"] / total_value * 100 if total_value else 0.0
        prev_wt  = prev_val / total_value * 100 if (total_value and prev) else 0.0

        if prev is None:
            ftype   = "ENTER"
            dpct    = float("inf")
            delta_u = h["value_usd"]
        elif h["value_usd"] >= prev_val:
            ftype   = "ADD"
            delta_u = h["value_usd"] - prev_val
            dpct    = delta_u / prev_val * 100 if prev_val else float("inf")
        else:
            ftype   = "TRIM"
            delta_u = h["value_usd"] - prev_val  # negative
            dpct    = delta_u / prev_val * 100 if prev_val else -100.0

        events.append(FlowEvent(
            type=ftype,
            ticker=h.get("ticker", ""),
            name=h["name"],
            cusip=cusip,
            fund_name=fund_name,
            quarter=quarter,
            value_usd=h["value_usd"],
            shares=h["shares"],
            weight_pct=curr_wt,
            prev_value_usd=prev_val,
            prev_weight_pct=prev_wt,
            delta_usd=delta_u,
            delta_pct=dpct,
            weight_change=curr_wt - prev_wt,
            score=score_val,
            factors=factors,
        ))

    # Exits — in prev but gone from current
    for cusip, prev_h in prev_positions.items():
        if cusip in curr_cusips:
            continue
        if prev_h.get("value_usd", 0) < config.MIN_POSITION_VALUE_USD:
            continue

        ghost     = {**prev_h, "shares": 0, "value_usd": 0}
        score_val, factors = scoring.score_change(prev_h, ghost, total_value)
        if score_val < config.MIN_SCORE:
            continue

        prev_val = prev_h.get("value_usd", 0)
        prev_wt  = prev_val / total_value * 100 if total_value else 0.0

        events.append(FlowEvent(
            type="EXIT",
            ticker=prev_h.get("ticker", ""),
            name=prev_h.get("name", ""),
            cusip=cusip,
            fund_name=fund_name,
            quarter=quarter,
            value_usd=0,
            shares=0,
            weight_pct=0.0,
            prev_value_usd=prev_val,
            prev_weight_pct=prev_wt,
            delta_usd=-prev_val,
            delta_pct=-100.0,
            weight_change=-prev_wt,
            score=score_val,
            factors=factors,
        ))

    return sorted(events, key=lambda e: e.score, reverse=True)


def diff_from_state(st: dict, cik: str, fund_name: str) -> list[FlowEvent]:
    """Convenience: diff a fund's current positions against its last saved quarter.

    Used by on-demand commands (/exits, /history diff) to read from cached state
    without re-fetching EDGAR. Returns [] if there's no prior quarter to compare.
    """
    fund_data = st.get("funds", {}).get(cik, {})
    positions = fund_data.get("positions", {})
    history   = fund_data.get("history", [])
    quarter   = fund_data.get("latest_quarter", "?")

    if not positions or not history:
        return []

    prev_positions = history[0]["positions"]
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

    return diff(fund_name, quarter, holdings, prev_positions, total_value)


def enrich_cost_basis(
    events: list[FlowEvent],
    *,
    types: tuple[str, ...] = ("ENTER", "ADD"),
    limit: int = 10,
    delay: float = 0.3,
) -> None:
    """Fetch price history for up to `limit` events and attach cost_basis data.

    Mutates events in-place. Only processes events whose type is in `types`
    and that have a resolved ticker. Skips events already enriched.

    verdict values:
      "early"    — current price ≤ avg entry + 10%   (near or below entry)
      "moderate" — current price between +10% and +40% above avg
      "chasing"  — current price > avg entry + 40%
    """
    import time as _time
    from data.price_history import get_ohlc, get_current_price, quarter_bounds

    candidates = [
        e for e in events
        if e.type in types and e.ticker and e.cost_basis is None
    ][:limit]

    for ev in candidates:
        try:
            start, end = quarter_bounds(ev.quarter)
            ohlc       = get_ohlc(ev.ticker, start, end)
            if not ohlc:
                continue

            current = get_current_price(ev.ticker)
            if not current:
                continue

            avg   = ohlc["avg_close"]
            q_end = ohlc["close_end"]

            pct_from_avg     = (current - avg)   / avg   * 100 if avg   else None
            pct_from_q_end   = (current - q_end) / q_end * 100 if q_end else None

            if pct_from_avg is None:
                verdict = "unknown"
            elif pct_from_avg <= 10:
                verdict = "early"
            elif pct_from_avg <= 40:
                verdict = "moderate"
            else:
                verdict = "chasing"

            ev.cost_basis = {
                "low":              ohlc["low"],
                "high":             ohlc["high"],
                "avg_close":        avg,
                "close_end":        q_end,
                "current_price":    current,
                "pct_from_avg":     pct_from_avg,
                "pct_from_q_end":   pct_from_q_end,
                "verdict":          verdict,
            }
        except Exception as e:
            print(f"  [flow] cost_basis enrichment failed for {ev.ticker}: {e}")

        _time.sleep(delay)
