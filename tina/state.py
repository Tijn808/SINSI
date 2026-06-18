"""Persistent state for TINA. Keeps current positions + 4 quarters of history per fund."""

import json
from pathlib import Path

_STATE_FILE  = Path("state/state.json")
_MAX_HISTORY = 4  # quarters to keep per fund

_EMPTY: dict = {
    "seen_filings": [],
    "funds":        {},   # CIK → {name, latest_quarter, positions, history}
    "ticker_cache": {},   # CUSIP → ticker
    "weekly_posted": "",
}


def load() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {
        k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
        for k, v in _EMPTY.items()
    }


def save(st: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(st, indent=2))


def is_seen(st: dict, accession: str) -> bool:
    return accession in st["seen_filings"]


def mark_seen(st: dict, accession: str) -> None:
    if accession not in st["seen_filings"]:
        st["seen_filings"].append(accession)
    if len(st["seen_filings"]) > 10_000:
        st["seen_filings"] = st["seen_filings"][-10_000:]


def get_fund_positions(st: dict, cik: str) -> dict:
    """Current quarter positions: {cusip: {name, ticker, shares, value_usd, quarter}}"""
    return st["funds"].get(cik, {}).get("positions", {})


def set_fund_positions(st: dict, cik: str, name: str, quarter: str, positions: dict) -> None:
    """Save new positions, rotating the old quarter into history."""
    existing = st["funds"].get(cik, {})
    old_positions = existing.get("positions", {})
    old_quarter   = existing.get("latest_quarter", "")

    # Push old quarter into history before overwriting
    history = list(existing.get("history", []))
    if old_positions and old_quarter and old_quarter != quarter:
        history.insert(0, {"quarter": old_quarter, "positions": old_positions})
        history = history[:_MAX_HISTORY]

    st["funds"][cik] = {
        "name":           name,
        "latest_quarter": quarter,
        "positions":      positions,
        "history":        history,
        "style":          existing.get("style"),  # preserved; overwritten by set_fund_style()
    }


def get_fund_style(st: dict, cik: str) -> dict | None:
    """Return stored style dict for a fund, or None if not yet detected."""
    return st.get("funds", {}).get(cik, {}).get("style")


def set_fund_style(st: dict, cik: str, style_data: dict) -> None:
    """Store (or override) a fund's style data. Call after set_fund_positions()."""
    if cik in st.get("funds", {}):
        st["funds"][cik]["style"] = style_data


def get_fund_history(st: dict, cik: str) -> list[dict]:
    """Returns [{quarter, positions}] from newest to oldest (up to _MAX_HISTORY entries)."""
    return st["funds"].get(cik, {}).get("history", [])


def get_ticker_cache(st: dict) -> dict:
    return st.get("ticker_cache", {})


def set_ticker(st: dict, cusip: str, ticker: str) -> None:
    st.setdefault("ticker_cache", {})[cusip] = ticker


def all_fund_positions(st: dict) -> dict[str, dict]:
    """Returns {cik: positions} for all funds that have data."""
    return {
        cik: data["positions"]
        for cik, data in st.get("funds", {}).items()
        if data.get("positions")
    }


# ── Emergent consensus tracking ───────────────────────────────────────────────

def add_fund_enters(st: dict, fund_name: str, period: str, enters: list) -> None:
    """Record ENTER events for a fund filing so we can detect cross-fund consensus.

    enters: list of FlowEvent with type == 'ENTER'
    Keyed by '{ticker}:{period}' so we only match funds that entered in the SAME quarter.
    """
    bucket = st.setdefault("enters_by_quarter", {})
    for ev in enters:
        if not ev.ticker:
            continue
        key = f"{ev.ticker}:{period}"
        existing = bucket.setdefault(key, [])
        if not any(e["fund"] == fund_name for e in existing):
            existing.append({
                "fund":       fund_name,
                "value":      ev.value_usd,
                "weight_pct": ev.weight_pct,
                "style":      getattr(ev, "fund_style", "unknown"),
                "conviction": getattr(ev, "conviction", 1.0),
            })


def get_enters_by_quarter(st: dict, period: str) -> dict[str, list[dict]]:
    """Return {ticker: [entry_records]} for funds that entered in the given period.

    entry_records: [{fund, value, weight_pct, style, conviction}]
    """
    suffix = f":{period}"
    return {
        key[: -len(suffix)]: entries
        for key, entries in st.get("enters_by_quarter", {}).items()
        if key.endswith(suffix)
    }


def get_new_consensus(st: dict, min_funds: int = 2) -> list[dict]:
    """Return consensus signals not yet posted (for scanner auto-alerts).

    Returns [{ticker, period, funds, total_value}].
    """
    alerted = set(st.get("consensus_alerted", []))
    results = []
    for key, entries in st.get("enters_by_quarter", {}).items():
        if key in alerted or len(entries) < min_funds:
            continue
        ticker, period = key.split(":", 1)
        results.append({
            "ticker":      ticker,
            "period":      period,
            "funds":       entries,
            "total_value": sum(e["value"] for e in entries),
        })
    return results


def get_all_consensus(st: dict, min_funds: int = 2) -> list[dict]:
    """Return all consensus signals including already-alerted ones (for /emerging command)."""
    results = []
    alerted = set(st.get("consensus_alerted", []))
    for key, entries in st.get("enters_by_quarter", {}).items():
        if len(entries) < min_funds:
            continue
        ticker, period = key.split(":", 1)
        results.append({
            "ticker":      ticker,
            "period":      period,
            "funds":       entries,
            "total_value": sum(e["value"] for e in entries),
            "alerted":     key in alerted,
        })
    return sorted(results, key=lambda x: (len(x["funds"]), x["total_value"]), reverse=True)


def mark_consensus_alerted(st: dict, ticker: str, period: str) -> None:
    st.setdefault("consensus_alerted", [])
    key = f"{ticker}:{period}"
    if key not in st["consensus_alerted"]:
        st["consensus_alerted"].append(key)


def prune_enter_events(st: dict, active_periods: set[str]) -> None:
    """Drop enter tracking for quarters no longer active (keeps state lean)."""
    bucket = st.get("enters_by_quarter", {})
    stale  = [k for k in bucket if k.split(":", 1)[1] not in active_periods]
    for k in stale:
        del bucket[k]


# ── Pile-in alert tracking ────────────────────────────────────────────────────

def get_pile_in_alerted(st: dict, period: str) -> set[str]:
    """Return set of tickers already auto-alerted for pile-in in the given period."""
    return set(st.get("pile_in_alerted", {}).get(period, []))


def mark_pile_in_alerted(st: dict, ticker: str, period: str) -> None:
    bucket = st.setdefault("pile_in_alerted", {})
    tickers = bucket.setdefault(period, [])
    if ticker not in tickers:
        tickers.append(ticker)
