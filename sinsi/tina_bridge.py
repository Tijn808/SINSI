"""Read TINA's state to enrich SINSI alerts with institutional context.

Called from SINSI scanners — never imports anything from SINSI to avoid cycles.
TINA's state.json is a plain JSON file; we read it directly.
"""

import json
from pathlib import Path

_TINA_STATE = Path("../tina/state/state.json")


def _load() -> dict:
    try:
        return json.loads(_TINA_STATE.read_text()) if _TINA_STATE.exists() else {}
    except Exception:
        return {}


def get_institutional_holdings(ticker: str) -> list[dict]:
    """Return [{fund, value_usd, weight_pct, quarter}] for every TINA fund holding this ticker.

    Sums across multiple positions in the same fund (e.g. shares + options under the same ticker).
    Empty list if TINA holds no position or state is unavailable.
    """
    st     = _load()
    ticker = ticker.upper()

    # {fund_name: {value_usd, weight_pct, quarter, style}}
    by_fund: dict[str, dict] = {}

    for cik, fund_data in st.get("funds", {}).items():
        fund_name   = fund_data.get("name", cik)
        quarter     = fund_data.get("latest_quarter", "")
        positions   = fund_data.get("positions", {})
        total_value = sum(p.get("value_usd", 0) for p in positions.values())
        style       = (fund_data.get("style") or {}).get("style", "unknown")

        for pos in positions.values():
            if pos.get("ticker", "").upper() != ticker:
                continue
            val = pos.get("value_usd", 0)
            if fund_name not in by_fund:
                by_fund[fund_name] = {
                    "fund":       fund_name,
                    "value_usd":  0,
                    "weight_pct": 0.0,
                    "quarter":    quarter,
                    "style":      style,
                    "_total":     total_value,
                }
            by_fund[fund_name]["value_usd"] += val

    for entry in by_fund.values():
        total = entry.pop("_total", 0)
        entry["weight_pct"] = round(entry["value_usd"] / total * 100 if total else 0, 2)

    return sorted(by_fund.values(), key=lambda x: x["value_usd"], reverse=True)


def get_all_tina_tickers() -> dict[str, list[dict]]:
    """Return {ticker: [{fund, value_usd, weight_pct, quarter}]} for all TINA positions.

    Deduplicates per-fund (summing shares + options under the same ticker).
    Used by the 13D/G fast-lane scanner.
    """
    st     = _load()
    # ticker → fund_name → {value_usd, weight_pct, quarter, style, _total}
    ticker_fund: dict[str, dict[str, dict]] = {}

    for cik, fund_data in st.get("funds", {}).items():
        fund_name   = fund_data.get("name", cik)
        quarter     = fund_data.get("latest_quarter", "")
        positions   = fund_data.get("positions", {})
        total_value = sum(p.get("value_usd", 0) for p in positions.values())
        style       = (fund_data.get("style") or {}).get("style", "unknown")

        for pos in positions.values():
            ticker = pos.get("ticker", "").upper()
            if not ticker:
                continue
            val    = pos.get("value_usd", 0)
            by_fund = ticker_fund.setdefault(ticker, {})
            if fund_name not in by_fund:
                by_fund[fund_name] = {
                    "fund":      fund_name,
                    "value_usd": 0,
                    "quarter":   quarter,
                    "style":     style,
                    "_total":    total_value,
                }
            by_fund[fund_name]["value_usd"] += val

    result: dict[str, list[dict]] = {}
    for ticker, by_fund in ticker_fund.items():
        entries = []
        for entry in by_fund.values():
            total = entry.pop("_total", 0)
            entry["weight_pct"] = round(entry["value_usd"] / total * 100 if total else 0, 2)
            entries.append(entry)
        result[ticker] = sorted(entries, key=lambda x: x["value_usd"], reverse=True)
    return result
