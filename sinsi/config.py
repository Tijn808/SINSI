# ── Thresholds ─────────────────────────────────────────────────────────────────
# Edit these to tune signal sensitivity. All $ values in USD.

# Form 4 — Insider Buys
MIN_BUY_VALUE_USD      = 5_000   # absolute floor — anything below this is noise regardless of score
BIG_BUY_VALUE_USD      = 500_000 # flag as "large buy" above this
MIN_SIGNIFICANCE_SCORE = 30      # significance gate — buys scoring below this are not posted
                                  # 30–45: notable, 45–60: strong, 60+: exceptional
MIN_SELL_SCORE         = 45      # sells are noisier — require stronger signal to post
                                  # 45 = CEO selling 50%+ position, or near 52W high

# Transaction codes that are NOT genuine buying decisions — skip them
# M=option exercise, A=award/grant, G=gift, F=tax withholding, J=other, W=will
SKIP_CODES = {"M", "A", "G", "F", "J", "W"}

# Cluster Buying (multiple insiders, same company, short window)
CLUSTER_WINDOW_DAYS   = 7
CLUSTER_MIN_INSIDERS  = 2
CLUSTER_MIN_TOTAL_USD = 100_000

# Short Squeeze Score (out of 100) — alert when score >= SQUEEZE_ALERT_SCORE
SQUEEZE_ALERT_SCORE     = 50

# Score contributions (stacking where noted)
SCORE_SHORT_PCT_HIGH    = 20   # short interest > SHORT_PCT_HIGH
SCORE_SHORT_PCT_EXTREME = 20   # short interest > SHORT_PCT_EXTREME  (stacks)
SCORE_DTC_HIGH          = 15   # days to cover  > DTC_HIGH
SCORE_DTC_EXTREME       = 15   # days to cover  > DTC_EXTREME        (stacks)
SCORE_SMALL_FLOAT       = 10   # float shares   < FLOAT_SMALL_SHARES
SCORE_INSIDER_FLOAT     = 10   # insider ownership % > INSIDER_FLOAT_PCT
SCORE_HIGH_BORROW       = 10   # borrow rate    > BORROW_HIGH_PCT

# Score thresholds
SHORT_PCT_HIGH        = 0.20   # 20% of float shorted
SHORT_PCT_EXTREME     = 0.40   # 40%
DTC_HIGH              = 5      # days to cover
DTC_EXTREME           = 10
FLOAT_SMALL_SHARES    = 50_000_000
INSIDER_FLOAT_PCT     = 0.10   # 10% of float held by insiders
BORROW_HIGH_PCT       = 0.05   # 5% annualized borrow rate

# High Borrow standalone alert (separate from squeeze score)
BORROW_ALERT_PCT = 0.10        # 10% annualized

# Alert cooldowns — minimum hours between repeat alerts for the same ticker
SQUEEZE_COOLDOWN_HOURS = 24
BORROW_COOLDOWN_HOURS  = 12

# ── Price Action ──────────────────────────────────────────────────────────────
REL_VOL_NOTABLE   = 2.0    # relative volume above this = flag as notable
PERF_WEEK_WEAK    = -0.10  # down 10%+ this week = buying into weakness
PERF_MONTH_WEAK   = -0.20  # down 20%+ this month = buying into weakness
PCT_FROM_52W_LOW  = 0.15   # within 15% of 52W low = near the bottom

# ── Director Buy (special case for micro-caps) ────────────────────────────────
# A director (not just exec) buying into a micro-cap with short interest
# is worth flagging even when the exec-only role filter is active.
DIRECTOR_BUY_MAX_CAP   = 100_000_000  # $100M — micro-cap threshold
DIRECTOR_BUY_MIN_SHORT = 0.15         # 15% short interest minimum

# ── Large cap thresholds ──────────────────────────────────────────────────────
# Tiered minimum buy value — small buys at large caps are noise
LARGE_CAP_THRESHOLD    = 500_000_000   # $500M — above this = large cap
MEGA_CAP_THRESHOLD     = 5_000_000_000 # $5B   — above this = mega cap
MIN_BUY_LARGE_CAP      = 50_000        # $50K minimum for $500M–$5B companies
MIN_BUY_MEGA_CAP       = 100_000       # $100K minimum for $5B+ companies

# Suppress squeeze score on large caps — it's not a relevant signal above this
SQUEEZE_SUPPRESS_CAP   = 2_000_000_000 # $2B

# Fetch more filings for large caps (more insiders filing)
LOOKUP_FILING_LIMIT    = 40            # raised from 15

# ── Polling ───────────────────────────────────────────────────────────────────
POLL_INTERVAL = 300   # seconds between full scans (5 min)
LOOKBACK_DAYS = 7     # how far back to look for new filings on first run
