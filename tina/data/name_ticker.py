"""Name-based ticker lookup using EDGAR's company_tickers_exchange.json.

EDGAR publishes a free, unlimited JSON with every exchange-listed ticker and
company name.  We use it to resolve 13F position names (e.g. "APPLE INC") to
tickers ("AAPL") without rate limits, as a fast fallback after CUSIP lookups.

The file is ~1MB and cached locally for 7 days.
"""

import json
import re
import time
from pathlib import Path

import requests

_CACHE_FILE = Path("state/company_tickers_exchange.json")
_CACHE_TTL  = 7 * 86400  # 7 days
_URL        = "https://www.sec.gov/files/company_tickers_exchange.json"
_HEADERS    = {"User-Agent": "TINA Bot tijnsaes@gmail.com"}

# Stripped in sequence (outermost first) before comparison
_STRIP_SEQ = [
    # Legal suffixes
    re.compile(
        r"\s+(CORPORATION|INCORPORATED|COMPANY|CORP|INC|CO|LTD|LLC|LP|PLC|NV|SA|AG|SE)\.?$",
        re.IGNORECASE,
    ),
    # Secondary qualifiers that may follow the legal suffix
    re.compile(
        r"\s+(GROUP|HOLDING|HOLDINGS|TRUST|FUND|ETF|CLASS\s+[ABC]|"
        r"ORDINARY\s+SHARES?|COMMON\s+STK|NEW|ADR|ADS)\.?$",
        re.IGNORECASE,
    ),
]

# Word-level expansions so "INTL" == "INTERNATIONAL", "SYS" == "SYSTEMS", etc.
_EXPAND = {
    "INTL": "INTERNATIONAL",
    "SYS":  "SYSTEMS",
    "SVCS": "SERVICES",
    "TECHS": "TECHNOLOGIES",
    "TECH": "TECHNOLOGY",
    "MGMT": "MANAGEMENT",
    "MFG":  "MANUFACTURING",
    "DEV":  "DEVELOPMENT",
}


def _normalise(name: str) -> str:
    name = name.upper().strip()
    # Strip trailing periods and commas
    name = re.sub(r"[.,]+$", "", name).strip()
    # Apply legal suffix stripping repeatedly (handles "X Corp Inc")
    for _ in range(3):
        prev = name
        for pat in _STRIP_SEQ:
            name = pat.sub("", name).strip()
        if name == prev:
            break
    # Remove remaining punctuation
    name = re.sub(r"[^A-Z0-9\s]", "", name)
    # Expand abbreviations
    words = name.split()
    words = [_EXPAND.get(w, w) for w in words]
    name = " ".join(words)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _load_raw() -> list[list]:
    """Download (and cache) the EDGAR company tickers (exchange) file.

    Returns a list of [cik, name, ticker, exchange] rows.
    """
    if _CACHE_FILE.exists():
        age = time.time() - _CACHE_FILE.stat().st_mtime
        if age < _CACHE_TTL:
            try:
                return json.loads(_CACHE_FILE.read_text())
            except Exception:
                pass

    try:
        r = requests.get(_URL, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("data", [])
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(rows))
        return rows
    except Exception as e:
        print(f"  [name_ticker] Failed to fetch company_tickers_exchange.json: {e}")
        return []


# Preferred US exchanges — prefer these over OTC/foreign
_US_EXCH = {"Nasdaq", "NYSE", "NYSE MKT", "NYSE ARCA"}

_index: dict[str, str] | None = None  # normalised_name → ticker


def _get_index() -> dict[str, str]:
    global _index
    if _index is not None:
        return _index
    rows = _load_raw()
    # Two-pass: US exchange tickers first, then OTC/foreign as fallback
    idx_us:  dict[str, str] = {}
    idx_all: dict[str, str] = {}
    for row in rows:
        if len(row) < 3:
            continue
        _, name, ticker, *rest = row
        exchange = rest[0] if rest else ""
        ticker = str(ticker).strip()
        name   = str(name).strip()
        if not ticker or not name:
            continue
        key = _normalise(name)
        if not key:
            continue
        if exchange in _US_EXCH:
            if key not in idx_us:
                idx_us[key] = ticker
        if key not in idx_all:
            idx_all[key] = ticker

    # Merge: US-exchange tickers override OTC/foreign
    idx_all.update(idx_us)
    _index = idx_all
    return _index


def lookup_names(names: list[str]) -> dict[str, str]:
    """Map a list of 13F company names to tickers.

    Returns {original_name: ticker} for matched names only.
    """
    idx    = _get_index()
    result = {}
    for name in names:
        key = _normalise(name)
        if key in idx:
            result[name] = idx[key]
    return result


def refresh_cache() -> int:
    """Force-refresh the cache and return the number of indexed entries."""
    global _index
    _index = None
    if _CACHE_FILE.exists():
        _CACHE_FILE.unlink()
    return len(_get_index())
