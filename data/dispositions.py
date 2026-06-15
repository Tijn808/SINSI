"""
Insider sale classification — separates meaningful open-market sells from
mechanical dispositions (tax withholding, gifts, option exercises, etc.)

The single most important rule on the sell side: insiders sell for many
reasons (taxes, diversification, mortgages, divorce) but buy for only one
(they think the stock goes up). The bot's job here is filtering out all
the mechanical and forced dispositions to surface only genuine discretionary
open-market sales.

Transaction code reference:
  S — open market or private sale          → the ONLY sentiment signal
  F — shares withheld for tax on vesting   → biggest false-alarm source, always skip
  G — bona fide gift (charity/estate)      → no cash received, skip
  U — tender in M&A / change of control    → event-driven, skip
  D — return / forfeiture to issuer        → context-dependent, skip
  M — option or RSU exercise               → acquisition, not disposal; skip unless
                                              paired with an S of similar size (exercise-and-dump)
  X/C/E — derivative mechanics             → same as M, skip standalone
"""

# Mechanical / non-sentiment dispositions — never fire a sell alert
SKIP_CODES = frozenset({"F", "G", "U", "D"})

# Derivative acquisition codes that can precede an exercise-and-dump S
EXERCISE_CODES = frozenset({"M", "X", "C", "E"})


def classify_dispositions(transactions: list[dict]) -> list[dict]:
    """
    Filter a Form 4 transaction list down to actionable open-market sales only.

    Pipeline:
    1. Drop all acquisition rows (A/acquired=True) — handled by the buy pipeline.
    2. Drop F/G/U/D — mechanical, no sentiment.
    3. Keep only code S (open-market sale).
    4. Detect exercise-and-dump: an S that closely matches an M/X/C/E exercise
       in the same filing is monetizing vesting comp, not exiting a long position.
    5. Pre-compute pct_sold so the score function doesn't re-derive it.

    Returns a list of enriched txn dicts; each has two extra keys:
      pct_sold        : float — fraction of pre-trade position sold (0–1)
      is_exercise_dump: bool  — True when this S follows an M/X exercise of similar size
    """
    # Total exercise acquisitions in this filing (shares acquired via M/X/C/E)
    exercise_shares = sum(
        t.get("shares") or 0.0
        for t in transactions
        if t["code"] in EXERCISE_CODES and t["acquired"]
    )

    results = []
    for txn in transactions:
        if txn["acquired"]:
            continue  # acquisition → buy pipeline

        code = txn["code"]

        if code in SKIP_CODES:
            continue  # mechanical — not a sentiment sell

        if code != "S":
            continue  # J / other — unknown, ignore

        shares      = txn.get("shares")      or 0.0
        owned_after = txn.get("owned_after") or 0.0

        # Reconstruct pre-trade position (owned_after is post-transaction)
        position_before = owned_after + shares
        pct_sold = shares / position_before if position_before > 0 else 0.0

        # Exercise-and-dump: S size is within 50%–200% of same-filing exercise size.
        # Executives routinely exercise expiring options and immediately sell to cover cost.
        # This is vesting comp monetization, not a discretionary bearish view.
        is_exercise_dump = (
            exercise_shares > 0
            and 0.5 <= (shares / exercise_shares) <= 2.0
        )

        results.append({
            **txn,
            "pct_sold":          pct_sold,
            "is_exercise_dump":  is_exercise_dump,
        })

    return results
