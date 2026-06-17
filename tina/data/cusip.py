"""CUSIP → ticker mapping via OpenFIGI.

OpenFIGI is a free API that maps financial identifiers to exchange tickers.
No API key required (25 req/min). Batch up to 100 CUSIPs per request.
https://www.openfigi.com/api
"""

import time
import requests

_URL        = "https://api.openfigi.com/v3/mapping"
_HEADERS    = {"Content-Type": "application/json"}
_BATCH_SIZE = 10   # free unauthenticated tier: max 10 items per request

# Prefer US equity exchanges
_US_EXCH = {"US", "UN", "UW", "UA", "UP", "UR", "UU"}


def lookup_batch(cusips: list[str], cached: dict[str, str]) -> dict[str, str]:
    """Map CUSIPs to tickers. Returns updated mapping including cached values.

    cached: existing {cusip: ticker} from state — these are not re-fetched.
    """
    result = dict(cached)

    to_fetch = [c for c in cusips if c and c not in result]
    if not to_fetch:
        return result

    for i in range(0, len(to_fetch), _BATCH_SIZE):
        batch   = to_fetch[i : i + _BATCH_SIZE]
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]

        try:
            r = requests.post(_URL, json=payload, headers=_HEADERS, timeout=20)
            if r.status_code == 429:
                print("  [cusip] OpenFIGI rate limit — sleeping 15s")
                time.sleep(15)
                r = requests.post(_URL, json=payload, headers=_HEADERS, timeout=20)
            r.raise_for_status()

            for cusip, item in zip(batch, r.json()):
                data = item.get("data", [])
                if not data:
                    continue
                # Prefer US equity
                ticker = next(
                    (d["ticker"] for d in data if d.get("exchCode") in _US_EXCH and d.get("ticker")),
                    data[0].get("ticker", ""),
                )
                if ticker:
                    result[cusip] = ticker

        except Exception as e:
            print(f"  [cusip] OpenFIGI batch failed: {e}")

        time.sleep(2.5)  # 25 req/min free tier = 1 req per 2.4s

    return result
