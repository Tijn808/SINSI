"""Fund style detection from 13F holdings data.

Classifies followed funds into four styles based on portfolio concentration
and size. Style is auto-detected on each new 13F filing and cached in state.
Users can override via /tag if the auto-detection is wrong.

Styles:
  concentrated  — ≤25 positions OR top-10 ≥ 75% of AUM. Each bet is a thesis.
                  Examples: Pershing Square, Berkshire, Baupost, Greenlight.
  focused       — 26–150 positions, moderate concentration. Deliberate stock-pickers.
                  Examples: Tiger Global, Viking, Third Point, Lone Pine.
  diversified   — 150–500 positions, top-10 < 40% of AUM. Sector or multi-strat.
  quant         — 500+ positions. Systematic, high turnover. New bets are noise.
                  Examples: Two Sigma, Renaissance, D.E. Shaw, Citadel.

Conviction weights (used to scale signal quality in alerts and scoring):
  concentrated  1.5  — new bet is a real statement
  focused       1.0  — meaningful but not a life's work
  diversified   0.7  — less signal per position
  quant         0.4  — near-noise; only outlier positions matter
"""

from __future__ import annotations

_STYLE_META = {
    "concentrated": {"label": "🎯 Concentrated", "conviction": 1.5},
    "focused":      {"label": "🔬 Focused",      "conviction": 1.0},
    "diversified":  {"label": "📦 Diversified",  "conviction": 0.7},
    "quant":        {"label": "🤖 Quant",         "conviction": 0.4},
    "unknown":      {"label": "❓ Unknown",       "conviction": 1.0},
}


def detect(holdings: list[dict], total_value: float) -> dict:
    """Auto-detect fund style from a full 13F holdings list.

    holdings: [{value_usd, ...}]  (all positions from the filing)
    Returns:
      style          — one of the four style keys
      n_positions    — number of positions filed
      top10_pct      — % of AUM in top 10 positions
      hhi            — Herfindahl-Hirschman Index (0–10000, higher = more concentrated)
      conviction     — signal quality multiplier (0.4–1.5)
      label          — display string with emoji
    """
    n = len(holdings)
    if n == 0:
        return _make(style="unknown", n=0, top10_pct=0.0, hhi=0.0)

    sorted_h   = sorted(holdings, key=lambda x: x.get("value_usd", 0), reverse=True)
    top10_val  = sum(h.get("value_usd", 0) for h in sorted_h[:10])
    top10_pct  = top10_val / total_value * 100 if total_value else 0.0
    hhi        = sum((h.get("value_usd", 0) / total_value * 100) ** 2 for h in holdings) if total_value else 0.0

    if n <= 25 or top10_pct >= 75 or hhi >= 500:
        style = "concentrated"
    elif n >= 500:
        style = "quant"
    elif n >= 150 and top10_pct < 40:
        style = "diversified"
    else:
        style = "focused"

    return _make(style=style, n=n, top10_pct=top10_pct, hhi=hhi)


def _make(style: str, n: int, top10_pct: float, hhi: float, overridden: bool = False) -> dict:
    meta = _STYLE_META.get(style, _STYLE_META["unknown"])
    return {
        "style":       style,
        "n_positions": n,
        "top10_pct":   round(top10_pct, 1),
        "hhi":         round(hhi, 1),
        "conviction":  meta["conviction"],
        "label":       meta["label"],
        "overridden":  overridden,
    }


def get_meta(style: str) -> dict:
    return _STYLE_META.get(style, _STYLE_META["unknown"])


def label(style: str) -> str:
    return _STYLE_META.get(style, _STYLE_META["unknown"])["label"]


def conviction(style: str) -> float:
    return _STYLE_META.get(style, _STYLE_META["unknown"])["conviction"]


VALID_STYLES = list(_STYLE_META.keys())
