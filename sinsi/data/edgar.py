"""
EDGAR API utilities.

All public EDGAR endpoints used here:
  https://www.sec.gov/files/company_tickers.json        — ticker → CIK map
  https://data.sec.gov/submissions/CIK{cik}.json        — all filings per company
  https://www.sec.gov/Archives/edgar/data/...            — actual filing documents
  https://efts.sec.gov/LATEST/search-index?...           — full-text search (for 13D/13G)
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "SECScanner/1.0 tijnsaes@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

_LAST_REQUEST = 0.0
_MIN_INTERVAL = 0.12  # stay well under EDGAR's 10 req/s limit


def _get(url: str, **kwargs) -> requests.Response:
    global _LAST_REQUEST
    elapsed = time.time() - _LAST_REQUEST
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    resp = requests.get(url, headers=HEADERS, timeout=15, **kwargs)
    resp.raise_for_status()
    _LAST_REQUEST = time.time()
    return resp


# ── Ticker / CIK resolution ────────────────────────────────────────────────────

def resolve_ticker(ticker: str) -> tuple[str, str] | None:
    """Return (cik_zero_padded, company_title) or None if not found."""
    data = _get("https://www.sec.gov/files/company_tickers.json").json()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry["ticker"] == ticker_upper:
            return str(entry["cik_str"]).zfill(10), entry["title"]
    return None


# ── Submissions / Filings ──────────────────────────────────────────────────────

def fetch_recent_filings(cik: str, form_types: set[str], lookback_days: int) -> list[dict]:
    """
    Return filings of the specified form types within lookback_days.
    Each dict has: form, accession, primary_doc, filed.
    """
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    data = _get(url).json()
    recent = data["filings"]["recent"]
    cutoff = date.today() - timedelta(days=lookback_days)

    results = []
    for form, filed, accession, primary_doc in zip(
        recent["form"],
        recent["filingDate"],
        recent["accessionNumber"],
        recent["primaryDocument"],
    ):
        if date.fromisoformat(filed) < cutoff:
            break  # newest-first; stop once past window
        if form in form_types:
            results.append({
                "form":        form,
                "accession":   accession,
                "primary_doc": primary_doc,
                "filed":       filed,
            })
    return results


def fetch_recent_form4s(cik: str, lookback_days: int) -> list[dict]:
    """
    Return Form 4 filings for a company filed within the last lookback_days.
    Each dict has: accession, primary_doc, filed.
    """
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    data = _get(url).json()
    recent = data["filings"]["recent"]
    cutoff = date.today() - timedelta(days=lookback_days)

    results = []
    for form, filed, accession, primary_doc in zip(
        recent["form"],
        recent["filingDate"],
        recent["accessionNumber"],
        recent["primaryDocument"],
    ):
        if form not in ("4", "4/A"):
            continue
        if date.fromisoformat(filed) < cutoff:
            break  # newest-first; stop once past window
        results.append({
            "accession": accession,
            "primary_doc": primary_doc,
            "filed": filed,
        })
    return results


def fetch_form4_details(cik: str, accession: str, primary_doc: str) -> dict | None:
    """
    Fetch and parse a Form 4 XML document.

    Returns a dict with:
      ticker, company, owner_name, role, is_officer, is_10b5_plan,
      transactions: list of {code, shares, price, value, acquired, owned_after}

    The submissions API sometimes returns 'xslF345X06/filename.xml' (an XSL
    transformation wrapper path). The raw XML in the archive is just 'filename.xml',
    so we strip any leading path components of that form.
    """
    cik_int = int(cik)
    accession_nodash = accession.replace("-", "")
    # Strip XSL stylesheet prefix (e.g. 'xslF345X06/') if present
    xml_filename = primary_doc.split("/")[-1]
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession_nodash}/{xml_filename}"
    )
    try:
        xml_text = _get(url).text
        return _parse_form4_xml(xml_text)
    except Exception:
        return None


def _parse_form4_xml(xml_text: str) -> dict | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    def txt(path: str) -> str:
        el = root.find(path)
        return (el.text or "").strip() if el is not None else ""

    ticker  = txt(".//issuerTradingSymbol")
    company = txt(".//issuerName")

    owner_name    = txt(".//rptOwnerName")
    is_officer    = txt(".//isOfficer") == "1"
    is_director   = txt(".//isDirector") == "1"
    is_10pct      = txt(".//isTenPercentOwner") == "1"
    officer_title = txt(".//officerTitle")

    if is_officer and officer_title:
        role = officer_title
    elif is_director:
        role = "Director"
    elif is_10pct:
        role = "10% Owner"
    else:
        role = "Insider"

    # Build footnote lookup: id → text
    footnote_map = {
        el.get("id", ""): (el.text or "").lower()
        for el in root.findall(".//footnote")
    }

    def _txn_is_plan(txn_el) -> bool:
        # Check dedicated planName element (newer EDGAR schema)
        plan_el = txn_el.find(".//planName")
        if plan_el is not None and (plan_el.text or "").strip():
            return True
        # Check footnotes referenced by this specific transaction
        for fn_ref in txn_el.findall(".//footnoteId"):
            fn_id   = fn_ref.get("id", "")
            fn_text = footnote_map.get(fn_id, "")
            if "10b5-1" in fn_text and "not pursuant" not in fn_text:
                return True
        return False

    transactions = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        def txn_txt(path):
            el = txn.find(path)
            return (el.text or "").strip() if el is not None else ""

        code     = txn_txt(".//transactionCode")
        shares   = _safe_float(txn_txt(".//transactionShares/value"))
        price    = _safe_float(txn_txt(".//transactionPricePerShare/value"))
        owned    = _safe_float(txn_txt(".//sharesOwnedFollowingTransaction/value"))
        acquired = txn_txt(".//transactionAcquiredDisposedCode/value") == "A"

        transactions.append({
            "code":        code,
            "shares":      shares,
            "price":       price,
            "value":       shares * price,
            "acquired":    acquired,
            "owned_after": owned,
            "is_plan":     _txn_is_plan(txn),
        })

    # Filing-level plan flag: true only if ALL transactions are plans
    is_10b5_plan = bool(transactions) and all(t["is_plan"] for t in transactions)

    return {
        "ticker":       ticker,
        "company":      company,
        "owner_name":   owner_name,
        "role":         role,
        "is_officer":   is_officer,
        "is_director":  is_director,
        "is_10b5_plan": is_10b5_plan,
        "transactions": transactions,
    }


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


# ── Shares outstanding (XBRL) ─────────────────────────────────────────────────

def fetch_shares_outstanding(cik: str) -> int | None:
    """
    Pull the most recent CommonStockSharesOutstanding value from EDGAR XBRL.
    Returns share count as an integer, or None if unavailable.
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        data = _get(url).json()
        units = (
            data.get("facts", {})
                .get("us-gaap", {})
                .get("CommonStockSharesOutstanding", {})
                .get("units", {})
                .get("shares", [])
        )
        # Filter for annual/quarterly filings and return the most recent value
        periodic = [u for u in units if u.get("form") in ("10-K", "10-Q")]
        if not periodic:
            return None
        latest = max(periodic, key=lambda u: u.get("end", ""))
        return int(latest["val"])
    except Exception:
        return None


# ── Offering detail parser (424B / S-3) ───────────────────────────────────────

def parse_offering_details(cik: str, accession: str, primary_doc: str) -> dict:
    """
    Fetch a 424B or S-3 document and extract key offering numbers.

    Returns a dict with whatever could be found (all keys are optional):
      shares_offered   : int   — number of shares being sold
      price_per_share  : float — offering price per share
      gross_proceeds   : float — total dollar amount of the offering
      shelf_amount     : float — max shelf size for S-3 filings
      is_atm           : bool  — at-the-market (continuous) offering
      is_primary       : bool  — company selling new shares (vs secondary)

    Parsing is best-effort: 424B formats vary widely. Unrecognised layouts
    return an empty dict rather than raising.
    """
    cik_int      = int(cik)
    acc_nodash   = accession.replace("-", "")
    doc_filename = primary_doc.split("/")[-1]
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_nodash}/{doc_filename}"
    )

    try:
        resp = _get(url)
        text = BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True)
    except Exception:
        return {}

    result: dict = {}

    # ATM / continuous offering
    result["is_atm"] = bool(re.search(r"at[\s-]the[\s-]market", text, re.I))

    # Primary vs secondary
    result["is_primary"] = bool(re.search(
        r"we\s+are\s+(?:offering|selling)|primary\s+offering|new\s+shares", text, re.I
    ))

    # ── Shares offered ─────────────────────────────────────────────────────────
    for pat in [
        r"offering\s+([\d,]+)\s+shares",
        r"([\d,]+)\s+shares\s+of\s+(?:our\s+)?common\s+stock\b",
        r"sale\s+of\s+([\d,]+)\s+shares",
        r"([\d,]+)\s+shares\s+(?:of\s+common\s+stock\s+)?(?:at|for|in\s+this)",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            try:
                result["shares_offered"] = int(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # ── Price per share ────────────────────────────────────────────────────────
    for pat in [
        r"public\s+offering\s+price\s+of\s+\$([\d,.]+)\s+per\s+share",
        r"price\s+of\s+\$([\d,.]+)\s+per\s+share",
        r"at\s+\$([\d,.]+)\s+per\s+share",
        r"\$([\d,.]+)\s+per\s+share",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            try:
                result["price_per_share"] = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # ── Gross proceeds / aggregate amount ─────────────────────────────────────
    def _parse_dollar(val_str: str, unit_str: str) -> float | None:
        try:
            val  = float(val_str.replace(",", ""))
            unit = (unit_str or "").lower()
            if "billion" in unit:  val *= 1e9
            elif "million" in unit: val *= 1e6
            elif "thousand" in unit: val *= 1e3
            return val
        except ValueError:
            return None

    for pat in [
        r"aggregate\s+(?:gross\s+)?proceeds\s+of\s+(?:approximately\s+)?\$([\d,.]+)\s*(million|billion|thousand)?",
        r"aggregate\s+offering\s+(?:price|amount)\s+of\s+(?:up\s+to\s+)?\$([\d,.]+)\s*(million|billion|thousand)?",
        r"maximum\s+aggregate\s+offering\s+(?:price|amount)\s+of\s+\$([\d,.]+)\s*(million|billion|thousand)?",
        r"gross\s+proceeds\s+of\s+(?:approximately\s+)?\$([\d,.]+)\s*(million|billion|thousand)?",
        r"up\s+to\s+\$([\d,.]+)\s*(million|billion|thousand)?\s+(?:of\s+)?(?:our\s+)?(?:common\s+stock|securities|shares)",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            val = _parse_dollar(m.group(1), m.group(2) if m.lastindex >= 2 else "")
            if val:
                result["gross_proceeds"] = val
                break

    # If we have shares + price but no proceeds, compute it
    if "gross_proceeds" not in result:
        s = result.get("shares_offered")
        p = result.get("price_per_share")
        if s and p:
            result["gross_proceeds"] = s * p

    return result


# ── Activist filings (SC 13D / SC 13G) ───────────────────────────────────────

def fetch_recent_activist(ticker: str, lookback_days: int) -> list[dict]:
    """
    Search EDGAR full-text index for recent SC 13D/13G filings mentioning ticker.
    Returns list of {accession, filed, filer, form_type, link}.

    Note: the EFTS search-index endpoint ignores dateRange params, so we
    filter by date manually after fetching.
    """
    cutoff = date.today() - timedelta(days=lookback_days)

    results = []
    for form in ("SC 13D", "SC 13G"):
        url = (
            "https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{ticker}%22&forms={form.replace(' ', '+')}"
        )
        try:
            data = _get(url).json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits:
                src       = hit.get("_source", {})
                filed_str = src.get("file_date", "")
                if not filed_str:
                    continue
                try:
                    if date.fromisoformat(filed_str) < cutoff:
                        continue
                except ValueError:
                    continue

                accession = src.get("accession_no", "")
                filer = "Unknown"
                display = src.get("display_names")
                if display:
                    filer = display[0] if isinstance(display[0], str) else display[0].get("name", "Unknown")

                results.append({
                    "accession": accession,
                    "filed":     filed_str,
                    "filer":     filer,
                    "form_type": form,
                    "link":      (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{src.get('entity_id', '')}/{accession.replace('-', '')}/"
                    ),
                })
        except Exception:
            continue
    return results
