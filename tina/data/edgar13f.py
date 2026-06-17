"""SEC EDGAR 13F-HR parsing — fund-focused.

Fund flow:
  search_fund(name)          → [{cik, name}]  (EDGAR full-text search)
  fetch_latest_13f(cik)      → {accession, date, period, cik}
  parse_holdings(acc, cik)   → [{name, cusip, value_usd, shares, share_type, option_type}]
"""

import re
import time
import xml.etree.ElementTree as ET

import requests
import config

_HEADERS = {"User-Agent": "TINA Bot tijnsaes@gmail.com"}


def _get(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [edgar13f] GET failed: {e}")
        return None


# ── Fund search ───────────────────────────────────────────────────────────────

def search_fund(query: str) -> list[dict]:
    """Search EDGAR for 13F filers matching a name. Returns [{cik, name}]."""
    if not query or len(query) < 2:
        return []
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{query}%22&forms=13F-HR"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=2)
        r.raise_for_status()
    except Exception:
        return []

    seen: set[str] = set()
    results = []
    for hit in r.json().get("hits", {}).get("hits", []):
        src   = hit.get("_source", {})
        ciks  = src.get("ciks", [])
        names = src.get("display_names", [])
        if not ciks or not names:
            continue
        cik = ciks[0].lstrip("0") or ciks[0]
        # display_names format: "Fund Name  (CIK 0001234567)"
        raw  = names[0]
        name = re.sub(r"\s*\(CIK[^)]+\)", "", raw).strip()
        if cik and name and cik not in seen:
            seen.add(cik)
            results.append({"cik": cik, "name": name})

    return results[:10]


def get_fund_name(cik: str) -> str:
    """Fetch the official name of a fund from EDGAR submissions."""
    r = _get(f"{config.EDGAR_BASE}/submissions/CIK{int(cik):010d}.json")
    return r.json().get("name", f"Fund {cik}") if r else f"Fund {cik}"


# ── Filing metadata ───────────────────────────────────────────────────────────

def fetch_latest_13f(cik: str) -> dict | None:
    """Get the most recent 13F-HR filing for a fund."""
    return fetch_nth_13f(cik, n=0)


def fetch_nth_13f(cik: str, n: int = 0) -> dict | None:
    """Get the n-th most recent 13F-HR filing (0=latest, 1=previous quarter, etc.)."""
    r = _get(f"{config.EDGAR_BASE}/submissions/CIK{int(cik):010d}.json")
    if not r:
        return None

    recent     = r.json().get("filings", {}).get("recent", {})
    forms      = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates      = recent.get("filingDate", [])
    periods    = recent.get("reportDate", [])

    count = 0
    for form, acc, date, period in zip(forms, accessions, dates, periods):
        if form == "13F-HR":
            if count == n:
                return {"accession": acc, "date": date, "period": period, "cik": cik}
            count += 1

    return None


# ── Holdings parser ───────────────────────────────────────────────────────────

def parse_holdings(accession: str, cik: str) -> list[dict]:
    """Download and parse the 13F information table.

    Returns [{name, cusip, value_usd, shares, share_type, option_type}].
    """
    acc_clean = accession.replace("-", "")
    base_url  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}"

    # Try JSON index first (modern EDGAR)
    r = _get(f"{base_url}/{accession}-index.json")
    time.sleep(config.REQUEST_DELAY)

    table_url = None
    if r:
        for doc in r.json().get("documents", []):
            doc_type = doc.get("type", "").upper()
            filename = doc.get("filename", "")
            if doc_type == "INFORMATION TABLE" or "infotable" in filename.lower():
                table_url = f"{base_url}/{filename}"
                break
        if not table_url:
            # Any XML that isn't the primary document
            for doc in r.json().get("documents", []):
                filename = doc.get("filename", "")
                if filename.endswith(".xml") and doc.get("type", "") not in ("13F-HR", "XML", "COVER"):
                    table_url = f"{base_url}/{filename}"
                    break

    if not table_url:
        table_url = _find_table_from_directory(base_url)

    if not table_url:
        table_url = _find_table_from_htm_index(accession, cik, base_url)

    if not table_url:
        return []

    r2 = _get(table_url)
    time.sleep(config.REQUEST_DELAY)
    return _parse_xml(r2.content) if r2 else []


def _find_table_from_directory(base_url: str) -> str | None:
    """Fetch the raw EDGAR directory listing and find the infotable XML."""
    r = _get(base_url + "/")
    time.sleep(config.REQUEST_DELAY)
    if not r:
        return None

    # Prefer explicit infotable file, skipping XSL-rendered variants
    for m in re.finditer(
        r'href="(/Archives/edgar/data/[^"]*(?:infotable|information.table)[^"]*\.xml)"',
        r.text, re.IGNORECASE,
    ):
        if "/xsl" not in m.group(1).lower():
            return "https://www.sec.gov" + m.group(1)

    # Any XML that isn't the cover page or XSL variant
    for m in re.finditer(r'href="(/Archives/edgar/data/[^"]*\.xml)"', r.text, re.IGNORECASE):
        path = m.group(1).lower()
        if "primary_doc" in path or "xslform" in path or "/xsl" in path:
            continue
        return "https://www.sec.gov" + m.group(1)

    return None


def _find_table_from_htm_index(accession: str, cik: str, base_url: str) -> str | None:
    # Try both .html (modern) and .htm (older filings)
    r = _get(f"{base_url}/{accession}-index.html") or _get(f"{base_url}/{accession}-index.htm")
    time.sleep(config.REQUEST_DELAY)
    if not r:
        return None

    # Prefer explicit infotable file, skipping XSL-rendered variants
    for m in re.finditer(
        r'href="(/Archives/edgar/data/[^"]*(?:infotable|information.table)[^"]*\.xml)"',
        r.text, re.IGNORECASE,
    ):
        if "/xsl" not in m.group(1).lower():
            return "https://www.sec.gov" + m.group(1)

    # Any XML that isn't the cover page or XSL variant
    for m in re.finditer(r'href="(/Archives/edgar/data/[^"]*\.xml)"', r.text, re.IGNORECASE):
        path = m.group(1).lower()
        if "primary_doc" in path or "xslform" in path or "/xsl" in path:
            continue
        return "https://www.sec.gov" + m.group(1)

    return None


def _parse_xml(content: bytes) -> list[dict]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"  [edgar13f] XML parse error: {e}")
        return []

    # Detect namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    entries = root.findall(f".//{ns}infoTable")
    if not entries:
        entries = root.findall(".//infoTable")

    raw = []
    for entry in entries:
        def _txt(tag: str) -> str:
            el = entry.find(f".//{ns}{tag}")
            if el is None:
                el = entry.find(f".//{tag}")
            return (el.text or "").strip() if el is not None else ""

        try:
            value_raw = int(_txt("value") or "0")
            shares    = int(_txt("sshPrnamt") or "0")
            if value_raw <= 0:
                continue
            raw.append({
                "name":        _txt("nameOfIssuer"),
                "cusip":       _txt("cusip"),
                "value_raw":   value_raw,
                "shares":      shares,
                "share_type":  _txt("sshPrnamtType") or "SH",
                "option_type": _txt("putCall") or None,
            })
        except (ValueError, TypeError):
            continue

    # 13F spec: value is in thousands. Some filers report in dollars instead.
    # If any single position exceeds $200B after x1000, it was already in dollars.
    multiplier = 1000
    if raw and max(h["value_raw"] for h in raw) * 1000 > 200_000_000_000:
        multiplier = 1

    return [
        {**{k: v for k, v in h.items() if k != "value_raw"},
         "value_usd": h["value_raw"] * multiplier}
        for h in raw
    ]
