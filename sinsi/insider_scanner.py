"""
SEC EDGAR Form 4 insider trade scanner → Discord webhook.

HOW IT WORKS
------------
Two scanning modes depending on whether watchlist.json has tickers:

  Watchlist mode (recommended):
    For each ticker in watchlist.json, the scanner:
      1. Resolves the ticker to an EDGAR CIK (Central Index Key) using
         https://www.sec.gov/files/company_tickers.json — a public mapping
         file EDGAR maintains of all listed companies.
      2. Polls https://data.sec.gov/submissions/CIK{cik}.json — a JSON
         endpoint that lists every filing a company has ever made, sorted
         newest first.
      3. Filters for form type "4" (insider transactions) filed in the last
         N days.
      4. Posts each unseen filing to Discord.

  General mode (fallback when watchlist is empty):
    Polls EDGAR's "current filings" Atom feed, which is a rolling list of
    the 40 most recent Form 4s filed across ALL companies. This catches
    everything but can't be filtered by company in advance.

Seen filings are tracked in seen_filings.json so restarts don't re-post.

Usage:
  python insider_scanner.py              # run the scanner
  python insider_scanner.py --add AAPL  # add ticker to watchlist
  python insider_scanner.py --remove AAPL
  python insider_scanner.py --list      # show current watchlist with CIKs
"""

import argparse
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SEEN_FILE = Path("seen_filings.json")
WATCHLIST_FILE = Path("watchlist.json")
POLL_INTERVAL = 300  # seconds between full scans (5 min)
LOOKBACK_DAYS = 7    # how far back to look for new filings on first run

HEADERS = {
    # EDGAR requires a descriptive User-Agent with contact info
    "User-Agent": "InsiderScanner/1.0 tijnsaes@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

# ── Watchlist helpers ──────────────────────────────────────────────────────────

def load_watchlist() -> dict:
    if WATCHLIST_FILE.exists():
        return json.loads(WATCHLIST_FILE.read_text())
    return {"tickers": []}


def save_watchlist(wl: dict) -> None:
    WATCHLIST_FILE.write_text(json.dumps(wl, indent=2))


def resolve_ticker_to_cik(ticker: str) -> tuple[str, str] | None:
    """Return (cik_padded, company_title) for a ticker, or None if not found."""
    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    ticker_upper = ticker.upper()
    for entry in resp.json().values():
        if entry["ticker"] == ticker_upper:
            cik = str(entry["cik_str"]).zfill(10)  # EDGAR CIKs are zero-padded to 10 digits
            return cik, entry["title"]
    return None


# ── Seen-filings tracker ───────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)))


# ── EDGAR fetchers ─────────────────────────────────────────────────────────────

def fetch_filings_for_company(cik: str, ticker: str) -> list[dict]:
    """
    Fetch recent Form 4 filings for one company via the EDGAR submissions API.

    EDGAR returns a JSON blob per company at:
      https://data.sec.gov/submissions/CIK{cik}.json

    The 'filings.recent' key contains parallel arrays (one entry per index
    position) with form type, filing date, accession number, etc. We zip
    them together and filter for form type "4".
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    recent = data["filings"]["recent"]
    company_name = data.get("name", ticker)
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)

    filings = []
    for form, filed, accession in zip(
        recent["form"],
        recent["filingDate"],
        recent["accessionNumber"],
    ):
        if form not in ("4", "4/A"):
            continue
        if date.fromisoformat(filed) < cutoff:
            break  # results are newest-first; stop once we're past the window
        dash_accession = accession  # already formatted as "0001234567-26-000001"
        edgar_link = (
            "https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=10"
        )
        filings.append({
            "accession": dash_accession,
            "company": company_name,
            "ticker": ticker,
            "filed": filed,
            "link": edgar_link,
        })

    return filings


EDGAR_GENERAL_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&dateb=&owner=include&count=40&output=atom"
)


def fetch_all_recent_form4s() -> list[dict]:
    """
    Fallback: fetch the 40 most recent Form 4s across all companies from
    EDGAR's public Atom feed. Used when the watchlist is empty.
    """
    resp = requests.get(EDGAR_GENERAL_FEED, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    entries = []
    seen_accessions: set[str] = set()
    text = resp.text

    for block in text.split("<entry>")[1:]:
        end = block.find("</entry>")
        block = block[:end]

        accession = _between(block, "accession-number=", "<")
        if not accession or accession in seen_accessions:
            continue
        seen_accessions.add(accession)

        title = _between(block, "<title>", "</title>") or ""
        company = title.removeprefix("4 - ").split("(")[0].strip() or "Unknown"

        summary = _between(block, "<summary", "</summary>") or ""
        filed = _between(summary, "Filed:&lt;/b&gt; ", " &lt;b&gt;") or "?"

        link = _between(block, 'href="', '"') or "https://www.sec.gov/"

        entries.append({
            "accession": accession,
            "company": company,
            "ticker": None,
            "filed": filed,
            "link": link,
        })

    return entries


def _between(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i == -1:
        return None
    i += len(start)
    j = text.find(end, i)
    if j == -1:
        return None
    return text[i:j].strip()


# ── Discord ────────────────────────────────────────────────────────────────────

def post_to_discord(filing: dict) -> None:
    label = f"{filing['ticker']} — {filing['company']}" if filing["ticker"] else filing["company"]
    embed = {
        "title": f"Form 4 — {label}",
        "description": f"Filed: **{filing['filed']}**",
        "url": filing["link"],
        "color": 0x5865F2,
        "footer": {"text": f"Accession: {filing['accession']}"},
    }
    resp = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


# ── Scanner loop ───────────────────────────────────────────────────────────────

def run_scan(seen: set, watchlist: dict) -> None:
    tickers = watchlist.get("tickers", [])

    if tickers:
        all_filings = []
        for entry in tickers:
            ticker = entry["ticker"]
            cik = entry["cik"]
            try:
                filings = fetch_filings_for_company(cik, ticker)
                all_filings.extend(filings)
            except Exception as exc:
                print(f"  Error fetching {ticker}: {exc}")
        print(f"Watchlist mode: {len(tickers)} companies → {len(all_filings)} recent Form 4s found.")
    else:
        all_filings = fetch_all_recent_form4s()
        print(f"General mode: {len(all_filings)} Form 4s found across all companies.")

    new = [f for f in all_filings if f["accession"] not in seen]
    print(f"  {len(new)} new (unseen) filings to post.")

    for filing in new:
        post_to_discord(filing)
        seen.add(filing["accession"])
        time.sleep(1)  # stay under Discord's 5 requests/sec rate limit


# ── CLI ────────────────────────────────────────────────────────────────────────

def cmd_add(ticker: str) -> None:
    wl = load_watchlist()
    ticker = ticker.upper()
    if any(e["ticker"] == ticker for e in wl["tickers"]):
        print(f"{ticker} is already in the watchlist.")
        return
    result = resolve_ticker_to_cik(ticker)
    if result is None:
        print(f"Could not find {ticker} on EDGAR. Check the ticker symbol.")
        return
    cik, title = result
    wl["tickers"].append({"ticker": ticker, "cik": cik, "name": title})
    save_watchlist(wl)
    print(f"Added {ticker} ({title}) with CIK {cik}.")


def cmd_remove(ticker: str) -> None:
    wl = load_watchlist()
    ticker = ticker.upper()
    before = len(wl["tickers"])
    wl["tickers"] = [e for e in wl["tickers"] if e["ticker"] != ticker]
    if len(wl["tickers"]) == before:
        print(f"{ticker} was not in the watchlist.")
    else:
        save_watchlist(wl)
        print(f"Removed {ticker}.")


def cmd_list() -> None:
    wl = load_watchlist()
    tickers = wl.get("tickers", [])
    if not tickers:
        print("Watchlist is empty. Add tickers with: python insider_scanner.py --add AAPL")
        return
    print(f"{'Ticker':<8} {'CIK':<12} Name")
    print("-" * 50)
    for e in tickers:
        print(f"{e['ticker']:<8} {e['cik']:<12} {e['name']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SEC EDGAR Form 4 scanner")
    parser.add_argument("--add", metavar="TICKER", help="Add a ticker to the watchlist")
    parser.add_argument("--remove", metavar="TICKER", help="Remove a ticker from the watchlist")
    parser.add_argument("--list", action="store_true", help="Show the current watchlist")
    args = parser.parse_args()

    if args.add:
        cmd_add(args.add)
        return
    if args.remove:
        cmd_remove(args.remove)
        return
    if args.list:
        cmd_list()
        return

    # Run scanner
    wl = load_watchlist()
    mode = "watchlist" if wl.get("tickers") else "general"
    print(f"Starting SEC EDGAR scanner in {mode} mode. Polling every {POLL_INTERVAL}s.")

    seen = load_seen()
    while True:
        try:
            run_scan(seen, wl)
            save_seen(seen)
            wl = load_watchlist()  # reload so you can add tickers without restarting
        except Exception as exc:
            print(f"Scan error: {exc}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
