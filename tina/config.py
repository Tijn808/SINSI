"""TINA — Tracking INstitutional Activity. Configuration."""

# ── Scanning ──────────────────────────────────────────────────────────────────

POLL_INTERVAL         = 3600   # seconds between scan cycles (13F filings are slow)
WEEKLY_SUMMARY_DAY    = 6      # Sunday (0=Mon … 6=Sun)
WEEKLY_SUMMARY_HOUR   = 18     # UTC hour to post weekly summary

# ── Thresholds ────────────────────────────────────────────────────────────────

MIN_POSITION_VALUE_USD = 50_000   # ignore positions smaller than this
MIN_PCT_CHANGE         = 10.0     # minimum % change to be considered notable
MIN_USD_CHANGE         = 100_000  # OR minimum dollar change

# ── Scoring ───────────────────────────────────────────────────────────────────

MIN_SCORE          = 20    # minimum score to post an individual change alert
NEW_POSITION_BONUS = 20    # extra points for brand-new positions
EXIT_BONUS         = 15    # extra points for full exits

# ── Charts ────────────────────────────────────────────────────────────────────

CHART_TOP_N     = 15       # max positions to show per chart
CHART_DPI       = 150
CHART_BG        = "#2b2d31"  # Discord dark
CHART_TEXT      = "#dbdee1"
CHART_GREEN     = "#57f287"
CHART_RED       = "#ed4245"
CHART_BLUE      = "#5865f2"
CHART_GREY      = "#4e5058"

# ── EDGAR ─────────────────────────────────────────────────────────────────────

EDGAR_BASE      = "https://data.sec.gov"
REQUEST_DELAY   = 0.15
REQUEST_TIMEOUT = 20

# ── Discord embed colors ──────────────────────────────────────────────────────

COLOR_NEW      = 0x57f287   # green
COLOR_INCREASE = 0x3498db   # blue
COLOR_DECREASE = 0xe67e22   # orange
COLOR_EXIT     = 0xed4245   # red
COLOR_INFO     = 0x5865f2   # blurple
