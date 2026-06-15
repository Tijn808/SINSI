"""
Insider buy significance score — 0 to 100.

Replaces the flat dollar threshold. Three dimensions:

  Conviction  (0–40): how much skin is this insider putting in relative to themselves?
  Materiality (0–35): how much does this buy matter to the company's size?
  Role        (0–25): how informed is this insider likely to be?

The score gate lives in config.MIN_SIGNIFICANCE_SCORE. Anything below it is
silently skipped. Anything above posts an alert — the score and contributing
factors are included in the embed so readers can judge for themselves.
"""


def calc_insider_score(
    txn: dict,
    details: dict,
    market: dict,
) -> tuple[int, list[str]]:
    """
    Compute significance score for a single buy transaction.

    txn:     {shares, price, value, owned_after, acquired}
    details: {role, is_officer, is_director, is_10b5_plan}
    market:  Finviz dict — market_cap, float_shares, avg_volume, price,
             week_52_low, week_52_high (all may be None if Finviz failed)

    Returns (score capped at 100, human-readable factor list).
    """
    score   = 0
    factors: list[str] = []

    value        = txn.get("value")   or 0.0
    shares_txn   = txn.get("shares")  or 0.0
    owned_after  = txn.get("owned_after") or 0.0
    is_plan      = details.get("is_10b5_plan", False)
    role_raw     = (details.get("role") or "").lower()
    price        = market.get("price")

    # ── Conviction ─────────────────────────────────────────────────────────────

    # 1. Position increase — the core conviction signal.
    #    sharesOwnedFollowingTransaction is already in the XML we parse.
    position_before = owned_after - shares_txn

    if position_before > 0 and shares_txn > 0:
        pct = shares_txn / position_before
        if pct >= 2.0:
            score += 25
            factors.append(f"Position +{pct:.0%} (more than doubled) 🚀")
        elif pct >= 1.0:
            score += 20
            factors.append(f"Position +{pct:.0%} (doubled)")
        elif pct >= 0.50:
            score += 14
            factors.append(f"Position +{pct:.0%}")
        elif pct >= 0.25:
            score += 9
            factors.append(f"Position +{pct:.0%}")
        elif pct >= 0.10:
            score += 4
            factors.append(f"Position +{pct:.0%}")
    elif position_before <= 0 and shares_txn > 0:
        # First-ever open market purchase — no prior position at all
        score += 22
        factors.append("First open-market purchase (new position)")

    # 2. Discretionary vs scheduled
    if not is_plan:
        score += 10
        factors.append("Discretionary (no 10b5-1 plan)")

    # 3. Price location — buying into weakness signals urgency
    low_52 = market.get("week_52_low")
    if price and low_52 and low_52 > 0:
        pct_from_low = (price - low_52) / low_52
        if pct_from_low <= 0.10:
            score += 10
            factors.append(f"Buying within 10% of 52W low (${low_52:.2f}) 🎯")
        elif pct_from_low <= 0.20:
            score += 5
            factors.append(f"Buying near 52W low (${low_52:.2f})")

    # ── Materiality ────────────────────────────────────────────────────────────

    # 4. Buy value as % of market cap — normalises across company sizes
    market_cap = market.get("market_cap")
    if market_cap and market_cap > 0 and value > 0:
        cap_ratio = value / market_cap
        if cap_ratio >= 0.02:
            score += 15
            factors.append(f"Buy = {cap_ratio:.1%} of market cap")
        elif cap_ratio >= 0.01:
            score += 12
            factors.append(f"Buy = {cap_ratio:.1%} of market cap")
        elif cap_ratio >= 0.005:
            score += 8
            factors.append(f"Buy = {cap_ratio:.2%} of market cap")
        elif cap_ratio >= 0.001:
            score += 4
            factors.append(f"Buy = {cap_ratio:.2%} of market cap")

    # 5. Buy value as % of float value — more relevant for squeeze setups
    float_sh = market.get("float_shares")
    if float_sh and float_sh > 0 and price and value > 0:
        float_value = float_sh * price
        float_ratio = value / float_value
        if float_ratio >= 0.01:
            score += 10
            factors.append(f"Buy = {float_ratio:.1%} of float value")
        elif float_ratio >= 0.005:
            score += 7
            factors.append(f"Buy = {float_ratio:.2%} of float value")
        elif float_ratio >= 0.001:
            score += 3
            factors.append(f"Buy = {float_ratio:.2%} of float value")

    # 6. Days of average daily volume
    avg_vol = market.get("avg_volume")
    if avg_vol and avg_vol > 0 and price and value > 0:
        daily_dollar_vol = avg_vol * price
        days = value / daily_dollar_vol
        if days >= 5:
            score += 10
            factors.append(f"Buy = {days:.1f}× avg daily volume")
        elif days >= 2:
            score += 6
            factors.append(f"Buy = {days:.1f}× avg daily volume")
        elif days >= 0.5:
            score += 3
            factors.append(f"Buy = {days:.1f}× avg daily volume")

    # ── Role ───────────────────────────────────────────────────────────────────

    # 7. Seniority — CFO/CEO most informative, directors next, VPs least
    if any(k in role_raw for k in (
        "chief executive", "ceo", "chief financial", "cfo",
        "principal executive", "principal financial",
    )):
        score += 15
        factors.append("C-suite (CEO / CFO)")
    elif any(k in role_raw for k in (
        "chief operating", "coo", "chief technology", "cto",
        "chief revenue", "cro", "president",
    )):
        score += 11
        factors.append("Senior executive")
    elif "director" in role_raw:
        score += 7
        factors.append("Director")
    elif details.get("is_officer"):
        score += 5
        factors.append("Officer")
    else:
        score += 2

    return min(score, 100), factors


def calc_insider_sell_score(
    txn: dict,
    details: dict,
    market: dict,
) -> tuple[int, list[str]]:
    """
    Compute significance score for a single open-market insider sale (code S).

    A high score means this is a meaningful bearish signal.
    Key factors: % of position sold, discretionary vs 10b5-1, price near 52W high,
    sale size relative to company size, role seniority.
    """
    score   = 0
    factors: list[str] = []

    value            = txn.get("value")           or 0.0
    is_plan          = details.get("is_10b5_plan", False)
    is_exercise_dump = txn.get("is_exercise_dump", False)
    role_raw         = (details.get("role") or "").lower()
    price            = market.get("price")

    # pct_sold is pre-computed by classify_dispositions(); fall back to
    # recomputing if the txn arrived from a path that didn't call it.
    pct_sold = txn.get("pct_sold")
    if pct_sold is None:
        shares      = txn.get("shares")      or 0.0
        owned_after = txn.get("owned_after") or 0.0
        pos_before  = owned_after + shares
        pct_sold    = shares / pos_before if pos_before > 0 else 0.0

    # ── Conviction ─────────────────────────────────────────────────────────────

    # 1. % of position sold — the core bearish signal
    if pct_sold >= 0.80:
        score += 25
        factors.append(f"Sold {pct_sold:.0%} of position (nearly all) 🚨")
    elif pct_sold >= 0.50:
        score += 20
        factors.append(f"Sold {pct_sold:.0%} of position (majority)")
    elif pct_sold >= 0.25:
        score += 14
        factors.append(f"Sold {pct_sold:.0%} of position")
    elif pct_sold >= 0.10:
        score += 9
        factors.append(f"Sold {pct_sold:.0%} of position")
    elif pct_sold > 0:
        score += 4
        factors.append(f"Sold {pct_sold:.0%} of position (partial)")

    # 2. Exercise-and-dump penalty: selling immediately after exercising options is
    #    vesting comp monetization, not a discretionary bearish decision.
    if is_exercise_dump:
        score = max(0, score - 15)
        factors.append("Exercise-and-sell (likely vesting comp monetization)")

    # 3. Discretionary vs scheduled — pre-planned 10b5-1 sales are much less meaningful
    if not is_plan:
        score += 10
        factors.append("Discretionary (no 10b5-1 plan)")
    else:
        factors.append("Pre-scheduled 10b5-1 plan sale")

    # 4. Price location — selling near 52W high is a stronger bearish tell
    high_52 = market.get("week_52_high")
    if price and high_52 and high_52 > 0:
        pct_from_high = (high_52 - price) / high_52
        if pct_from_high <= 0.10:
            score += 10
            factors.append(f"Selling within 10% of 52W high (${high_52:.2f}) 📈")
        elif pct_from_high <= 0.20:
            score += 5
            factors.append(f"Selling near 52W high (${high_52:.2f})")

    # ── Materiality ────────────────────────────────────────────────────────────

    # 4. Sale value as % of market cap
    market_cap = market.get("market_cap")
    if market_cap and market_cap > 0 and value > 0:
        cap_ratio = value / market_cap
        if cap_ratio >= 0.02:
            score += 15
            factors.append(f"Sale = {cap_ratio:.1%} of market cap")
        elif cap_ratio >= 0.01:
            score += 12
            factors.append(f"Sale = {cap_ratio:.1%} of market cap")
        elif cap_ratio >= 0.005:
            score += 8
            factors.append(f"Sale = {cap_ratio:.2%} of market cap")
        elif cap_ratio >= 0.001:
            score += 4
            factors.append(f"Sale = {cap_ratio:.2%} of market cap")

    # 5. Sale value as % of float value
    float_sh = market.get("float_shares")
    if float_sh and float_sh > 0 and price and value > 0:
        float_value = float_sh * price
        float_ratio = value / float_value
        if float_ratio >= 0.01:
            score += 10
            factors.append(f"Sale = {float_ratio:.1%} of float value")
        elif float_ratio >= 0.005:
            score += 7
            factors.append(f"Sale = {float_ratio:.2%} of float value")
        elif float_ratio >= 0.001:
            score += 3
            factors.append(f"Sale = {float_ratio:.2%} of float value")

    # 6. Days of average daily volume
    avg_vol = market.get("avg_volume")
    if avg_vol and avg_vol > 0 and price and value > 0:
        daily_dollar_vol = avg_vol * price
        days = value / daily_dollar_vol
        if days >= 5:
            score += 10
            factors.append(f"Sale = {days:.1f}× avg daily volume")
        elif days >= 2:
            score += 6
            factors.append(f"Sale = {days:.1f}× avg daily volume")
        elif days >= 0.5:
            score += 3
            factors.append(f"Sale = {days:.1f}× avg daily volume")

    # ── Role ───────────────────────────────────────────────────────────────────

    if any(k in role_raw for k in (
        "chief executive", "ceo", "chief financial", "cfo",
        "principal executive", "principal financial",
    )):
        score += 15
        factors.append("C-suite (CEO / CFO)")
    elif any(k in role_raw for k in (
        "chief operating", "coo", "chief technology", "cto",
        "chief revenue", "cro", "president",
    )):
        score += 11
        factors.append("Senior executive")
    elif "director" in role_raw:
        score += 7
        factors.append("Director")
    elif details.get("is_officer"):
        score += 5
        factors.append("Officer")
    else:
        score += 2

    return min(score, 100), factors
