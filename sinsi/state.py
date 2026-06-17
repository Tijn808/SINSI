"""Persistent state — seen filings, cluster data, alert cooldowns."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_STATE_FILE = Path("state/state.json")

_EMPTY: dict = {
    "seen_filings":         [],  # list of accession strings already posted
    "cluster_data":         {},  # CIK → list of buy events {owner_name, role, value, date, accession}
    "alerted_clusters":     [],  # buy cluster-id hashes already alerted
    "sell_cluster_data":    {},  # CIK → list of sell events (same shape as cluster_data)
    "alerted_sell_clusters": [], # sell cluster-id hashes already alerted
    "cooldowns":            {},  # "squeeze:TICKER" | "borrow:TICKER" → ISO timestamp
}


def load() -> dict:
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
            # Ensure all keys exist (handles upgrades)
            return {**_EMPTY, **data}
        except Exception:
            pass
    return dict(_EMPTY)


def save(state: dict) -> None:
    _STATE_FILE.parent.mkdir(exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def mark_seen(state: dict, accession: str) -> None:
    if accession not in state["seen_filings"]:
        state["seen_filings"].append(accession)


def is_seen(state: dict, accession: str) -> bool:
    return accession in state["seen_filings"]


# ── Cluster helpers ────────────────────────────────────────────────────────────

def add_cluster_buy(state: dict, cik: str, entry: dict) -> None:
    """Record a buy event for a company's CIK."""
    state["cluster_data"].setdefault(cik, [])
    state["cluster_data"][cik].append(entry)


def get_cluster_window(state: dict, cik: str, window_days: int) -> list[dict]:
    """Return recent buy entries for a CIK within the window."""
    from datetime import date, timedelta
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).isoformat()
    entries = state["cluster_data"].get(cik, [])
    return [e for e in entries if e.get("date", "") >= cutoff]


def cluster_id(buy_accessions: list[str]) -> str:
    """Stable hash for a set of accessions so we don't re-alert the same cluster."""
    key = ",".join(sorted(buy_accessions))
    return hashlib.md5(key.encode()).hexdigest()


def mark_cluster_alerted(state: dict, cid: str) -> None:
    if cid not in state["alerted_clusters"]:
        state["alerted_clusters"].append(cid)


def is_cluster_alerted(state: dict, cid: str) -> bool:
    return cid in state["alerted_clusters"]


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(state: dict, key: str, hours: int) -> bool:
    """Return True if key was last alerted within the last `hours` hours."""
    ts = state["cooldowns"].get(key)
    if not ts:
        return False
    last = datetime.fromisoformat(ts)
    return datetime.now(timezone.utc) - last < timedelta(hours=hours)


def set_cooldown(state: dict, key: str) -> None:
    state["cooldowns"][key] = datetime.now(timezone.utc).isoformat()


# ── Sell cluster helpers ───────────────────────────────────────────────────────

def add_cluster_sell(state: dict, cik: str, entry: dict) -> None:
    state.setdefault("sell_cluster_data", {}).setdefault(cik, []).append(entry)


def get_sell_cluster_window(state: dict, cik: str, window_days: int) -> list[dict]:
    cutoff  = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).isoformat()
    entries = state.get("sell_cluster_data", {}).get(cik, [])
    return [e for e in entries if e.get("date", "") >= cutoff]


def mark_sell_cluster_alerted(state: dict, cid: str) -> None:
    state.setdefault("alerted_sell_clusters", [])
    if cid not in state["alerted_sell_clusters"]:
        state["alerted_sell_clusters"].append(cid)


def is_sell_cluster_alerted(state: dict, cid: str) -> bool:
    return cid in state.get("alerted_sell_clusters", [])


def prune_old_cluster_data(state: dict, window_days: int) -> None:
    """Remove cluster entries older than the window to keep state file small."""
    from datetime import date, timedelta
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).isoformat()

    for bucket in ("cluster_data", "sell_cluster_data"):
        data = state.get(bucket, {})
        for cik in list(data.keys()):
            data[cik] = [e for e in data[cik] if e.get("date", "") >= cutoff]
            if not data[cik]:
                del data[cik]
