"""Score institutional position changes for alert significance."""

import config


def score_change(
    prev: dict | None,
    curr: dict,
    fund_total_value: float = 0,
) -> tuple[int, list[str]]:
    """Return (score 0–100, [factor strings]) for a position change.

    prev=None means brand new position.
    curr with shares=0 means full exit.
    """
    score   = 0
    factors = []

    curr_value  = curr.get("value_usd", 0)
    curr_shares = curr.get("shares", 0)
    prev_value  = prev.get("value_usd", 0) if prev else 0
    prev_shares = prev.get("shares", 0) if prev else 0

    is_new  = prev is None
    is_exit = curr_shares == 0 and prev_shares > 0

    # ── Change type ───────────────────────────────────────────────────────────

    if is_new:
        score += config.NEW_POSITION_BONUS
        factors.append("New position")
    elif is_exit:
        score += config.EXIT_BONUS
        factors.append("Full exit")
    else:
        delta = curr_value - prev_value
        pct   = abs(delta) / prev_value * 100 if prev_value else 0
        verb  = "Increased" if delta > 0 else "Reduced"
        if pct >= 200:
            score += 25; factors.append(f"{verb} {pct:.0f}%")
        elif pct >= 100:
            score += 20; factors.append(f"{verb} {pct:.0f}%")
        elif pct >= 50:
            score += 15; factors.append(f"{verb} {pct:.0f}%")
        elif pct >= 25:
            score += 10; factors.append(f"{verb} {pct:.0f}%")
        elif pct >= 10:
            score += 5;  factors.append(f"{verb} {pct:.0f}%")

    # ── Absolute position size ────────────────────────────────────────────────

    ref_val = curr_value if not is_exit else prev_value
    if ref_val >= 100_000_000:
        score += 20; factors.append(f"${ref_val/1e6:.0f}M position")
    elif ref_val >= 50_000_000:
        score += 15; factors.append(f"${ref_val/1e6:.0f}M position")
    elif ref_val >= 10_000_000:
        score += 10; factors.append(f"${ref_val/1e6:.0f}M position")
    elif ref_val >= 1_000_000:
        score += 5;  factors.append(f"${ref_val/1e6:.1f}M position")

    # ── % of fund portfolio ───────────────────────────────────────────────────

    if fund_total_value > 0 and ref_val > 0:
        pct_of_fund = ref_val / fund_total_value * 100
        if pct_of_fund >= 5:
            score += 15; factors.append(f"{pct_of_fund:.1f}% of portfolio")
        elif pct_of_fund >= 2:
            score += 8;  factors.append(f"{pct_of_fund:.1f}% of portfolio")
        elif pct_of_fund >= 1:
            score += 4;  factors.append(f"{pct_of_fund:.1f}% of portfolio")

    # ── Options ───────────────────────────────────────────────────────────────

    option = curr.get("option_type")
    if option == "CALL":
        score += 5;  factors.append("Call options")
    elif option == "PUT":
        score -= 5;  factors.append("Put options (bearish)")

    return min(score, 100), factors
