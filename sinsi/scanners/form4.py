"""
Form 4 scanner — insider buys, insider sells, and cluster detection.

For each watchlist company:
  Buy pipeline:
    1. Fetch recent Form 4 filings via EDGAR submissions API.
    2. Skip already-seen accession numbers.
    3. Fetch + parse the actual Form 4 XML for new filings.
    4. Score each buy (code P) via the significance engine.
    5. Enrich with squeeze score + borrow rate (no extra Finviz call).
    6. Post one unified InsiderBuyAlert card per qualifying buy.
    7. Record in cluster tracker; if 2+ insiders bought in window → cluster buy alert.

  Sell pipeline (same filings, separate pass):
    1. classify_dispositions() filters each Form 4 transaction list:
       - Drops F (tax withholding), G (gift), U (tender), D (return to issuer)
       - Keeps only S (open-market sale)
       - Detects exercise-and-dump pairs (M exercise + matching S in same filing)
       - Pre-computes pct_sold
    2. Score each sale via calc_insider_sell_score().
    3. Post sell alert if score passes threshold.
    4. Record in sell cluster tracker; if 2+ insiders sold in window → cluster sell alert.
"""

import time

import config
import discord_bot
import state as st
import tina_bridge
from alerts import InsiderBuyAlert
from data import edgar
from data.dispositions import classify_dispositions
from data.market import calc_squeeze_score, get_borrow_rate, get_market_data
from data.score import calc_insider_score, calc_insider_sell_score


def _min_buy_for_cap(market_cap: float | None) -> float:
    if market_cap is None:
        return config.MIN_BUY_VALUE_USD
    if market_cap >= config.MEGA_CAP_THRESHOLD:
        return config.MIN_BUY_MEGA_CAP
    if market_cap >= config.LARGE_CAP_THRESHOLD:
        return config.MIN_BUY_LARGE_CAP
    return config.MIN_BUY_VALUE_USD


def _should_show_squeeze(market_cap: float | None) -> bool:
    if market_cap is None:
        return True
    return market_cap < config.SQUEEZE_SUPPRESS_CAP


def run(watchlist: dict, state: dict) -> None:
    tickers = watchlist.get("tickers", [])
    if not tickers:
        return

    for entry in tickers:
        ticker = entry["ticker"]
        cik    = entry["cik"]
        try:
            _scan_company(ticker, cik, state)
        except Exception as e:
            print(f"  [form4] Error scanning {ticker}: {e}")
        time.sleep(0.5)


def _scan_company(ticker: str, cik: str, state: dict) -> None:
    filings = edgar.fetch_recent_form4s(cik, config.LOOKBACK_DAYS)
    new = [f for f in filings if not st.is_seen(state, f["accession"])]
    if not new:
        return

    print(f"  [form4] {ticker}: {len(new)} new Form 4(s)")

    # Fetch market data once per company (shared across all filings in batch)
    try:
        market = get_market_data(ticker)
    except Exception:
        market = {}

    # Borrow rate fetched once per company, only if we might need it
    _borrow_fetched = False
    _borrow_rate    = None

    for filing in new:
        st.mark_seen(state, filing["accession"])

        details = edgar.fetch_form4_details(cik, filing["accession"], filing["primary_doc"])
        if not details:
            continue

        cik_int      = int(cik)
        acc_nodash   = filing["accession"].replace("-", "")
        filing["link"] = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=5"
        )

        # ── Buy pipeline ───────────────────────────────────────────────────────
        min_buy = _min_buy_for_cap(market.get("market_cap"))
        show_squeeze = _should_show_squeeze(market.get("market_cap"))
        for txn in details["transactions"]:
            if txn["code"] != "P" or not txn["acquired"]:
                continue
            if txn["value"] < min_buy:
                continue

            score, factors = calc_insider_score(txn, details, market)
            if score < config.MIN_SIGNIFICANCE_SCORE:
                print(
                    f"    → Skip buy (score {score}): {details['owner_name']} "
                    f"({details['role']}) ${txn['value']:,.0f}"
                )
                continue

            if not _borrow_fetched:
                try:
                    _borrow_rate = get_borrow_rate(ticker)
                except Exception:
                    pass
                _borrow_fetched = True

            squeeze_score, squeeze_factors = calc_squeeze_score(market, _borrow_rate)
            cluster_window = st.get_cluster_window(state, cik, config.CLUSTER_WINDOW_DAYS)

            alert = InsiderBuyAlert(
                ticker          = ticker,
                company         = details["company"],
                filing          = filing,
                details         = details,
                txn             = txn,
                score           = score,
                sig_factors     = factors,
                market          = market,
                squeeze_score   = squeeze_score if (squeeze_score >= 30 and show_squeeze) else None,
                squeeze_factors = squeeze_factors if show_squeeze else [],
                borrow_rate     = _borrow_rate,
                is_cluster      = len(cluster_window) >= config.CLUSTER_MIN_INSIDERS - 1,
                cluster_buys    = cluster_window,
            )

            print(
                f"    → Buy (score {score}): {details['owner_name']} "
                f"({details['role']}) ${txn['value']:,.0f}"
            )
            alert.post()
            time.sleep(1)

            # SINSI × TINA bridge: check if this ticker is also held institutionally
            try:
                institutions = tina_bridge.get_institutional_holdings(ticker)
                if institutions:
                    key = f"confluence:{filing['accession']}"
                    if not st.is_seen(state, key):
                        st.mark_seen(state, key)
                        print(
                            f"    → Confluence: {ticker} held by "
                            f"{len(institutions)} TINA fund(s)"
                        )
                        discord_bot.post_confluence(
                            ticker=ticker,
                            company=details["company"],
                            filing=filing,
                            details=details,
                            txn=txn,
                            score=score,
                            institutions=institutions,
                        )
                        time.sleep(1)
            except Exception as e:
                print(f"    [bridge] Confluence check failed: {e}")

            st.add_cluster_buy(state, cik, {
                "owner_name": details["owner_name"],
                "role":       details["role"],
                "value":      txn["value"],
                "date":       filing["filed"],
                "accession":  filing["accession"],
            })

        # ── Sell pipeline ───────────────────────────────────────────────────────
        # classify_dispositions() handles: drop F/G/U/D, keep only S, detect
        # exercise-and-dump pairs, pre-compute pct_sold.
        for txn in classify_dispositions(details["transactions"]):
            if txn["value"] < min_buy:
                continue

            score, factors = calc_insider_sell_score(txn, details, market)
            if score < config.MIN_SELL_SCORE:
                print(
                    f"    → Skip sell (score {score}): {details['owner_name']} "
                    f"({details['role']}) ${txn['value']:,.0f}"
                )
                continue

            print(
                f"    → Sell (score {score}): {details['owner_name']} "
                f"({details['role']}) ${txn['value']:,.0f}"
            )
            discord_bot.post_insider_sell(
                ticker      = ticker,
                filing      = filing,
                details     = details,
                txn         = txn,
                market      = market,
                score       = score,
                sig_factors = factors,
            )
            time.sleep(1)

            st.add_cluster_sell(state, cik, {
                "owner_name": details["owner_name"],
                "role":       details["role"],
                "value":      txn["value"],
                "date":       filing["filed"],
                "accession":  filing["accession"],
            })

    _check_cluster(ticker, cik, state)
    _check_sell_cluster(ticker, cik, state)


def _check_cluster(ticker: str, cik: str, state: dict) -> None:
    window = st.get_cluster_window(state, cik, config.CLUSTER_WINDOW_DAYS)

    if len(window) < config.CLUSTER_MIN_INSIDERS:
        return

    total = sum(b["value"] for b in window)
    if total < config.CLUSTER_MIN_TOTAL_USD:
        return

    cid = st.cluster_id([b["accession"] for b in window])
    if st.is_cluster_alerted(state, cid):
        return

    print(f"  [form4] Cluster buy detected: {ticker} ({len(window)} insiders)")
    edgar_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=10"
    )
    discord_bot.post_cluster_buy(ticker, window[0].get("company", ticker), window, edgar_url)
    st.mark_cluster_alerted(state, cid)
    time.sleep(1)


def _check_sell_cluster(ticker: str, cik: str, state: dict) -> None:
    window = st.get_sell_cluster_window(state, cik, config.CLUSTER_WINDOW_DAYS)

    if len(window) < config.CLUSTER_MIN_INSIDERS:
        return

    total = sum(s["value"] for s in window)
    if total < config.CLUSTER_MIN_TOTAL_USD:
        return

    cid = st.cluster_id([s["accession"] for s in window])
    if st.is_sell_cluster_alerted(state, cid):
        return

    print(f"  [form4] Cluster sell detected: {ticker} ({len(window)} insiders)")
    edgar_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=10"
    )
    discord_bot.post_cluster_sell(ticker, window[0].get("company", ticker), window, edgar_url)
    st.mark_sell_cluster_alerted(state, cid)
    time.sleep(1)
