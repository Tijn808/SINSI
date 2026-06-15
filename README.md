# SINSI — SEC EDGAR Discord Scanner

A self-hosted Python bot that monitors SEC EDGAR filings and market signals in real time and posts structured alerts to Discord. Built for small and mid cap stocks where insider activity and short squeeze conditions are actually meaningful.

## What it monitors

**Insider filings (Form 4)**
Every Form 4 filed on a watchlist company is fetched, parsed, and scored before any alert is posted. Buys and sells run through separate pipelines with different scoring logic. The bot does not just look at dollar value — it scores each transaction across conviction, materiality, and role to filter out noise.

**Short squeeze conditions**
A composite score across short float, days to cover, borrow rate, float size, and insider ownership. Checked every cycle and included as context on every insider buy alert.

**Activist filings (13D/13G)**
Full-text search across EDGAR for new SC 13D and SC 13G filings on watchlist tickers. These signal that someone has crossed 5% ownership and may intend to push for strategic changes.

**Dilution warnings (S-3, 424B, S-1)**
Shelf registrations and active prospectuses are detected immediately. A 424B filing means shares are being sold into the market right now. The bot parses the actual document to extract offering size, price, proceeds, and discount to market.

**Discovery**
Every Form 4 across all of EDGAR is scanned on each cycle. Small caps with significant executive buys that pass score and filter thresholds are posted as discovery alerts, surfacing companies not on the watchlist.

## Setup

Requirements: Python 3.11+, a Discord webhook URL, and a Discord bot token.

```
git clone <repo>
cd BOT
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url
DISCORD_BOT_TOKEN=your_bot_token_here
ALLOWED_CHANNEL_ID=your_channel_id_here
```

`DISCORD_WEBHOOK_URL` is used by the scanner to post alerts. `DISCORD_BOT_TOKEN` powers the slash command bot. `ALLOWED_CHANNEL_ID` restricts slash commands to one channel (right-click any channel in Discord with Developer Mode on → Copy Channel ID). Leave it blank to allow commands anywhere.

Add your first ticker and start both processes:

```
sec --add ONDS
nohup sec > logs.txt 2>&1 &
nohup python bot.py > bot-logs.txt 2>&1 &
```

## Discord slash commands

All commands are available directly in the designated Discord channel.

| Command | What it does |
|---|---|
| `/lookup TICKER` | Posts a full snapshot for any ticker — price, float, short %, borrow rate, squeeze score, and recent insider activity. Shows an Add to Watchlist button after posting. |
| `/add TICKER` | Adds a ticker to the watchlist. Posts publicly showing who added it and the price at time of adding. |
| `/remove` | Opens a dropdown showing the current watchlist. Select a ticker and confirm to remove it. |
| `/list` | Shows all tickers currently being monitored, including who added each one. |
| `/perf` | Shows price performance for every watchlist ticker since it was added. |
| `/scores` | Shows the current squeeze score (0–100) for every watchlist ticker with contributing factors. |
| `/filter` | Opens a panel showing current discovery filter settings with buttons to edit each category. |

**Right-click context menu**
Right-click any message containing `$TICKER` → Apps → **Lookup in SINSI** to get an instant snapshot without typing a command.

## Alert types

| Alert | Trigger |
|---|---|
| Insider Buy | An insider filed a qualifying open-market purchase scoring above the threshold |
| Strong / Exceptional Buy | Same as above but at higher score tiers (50+ / 70+) |
| Cluster Buy | 2 or more insiders at the same company bought within a 7-day window |
| Insider Sell | An insider made a discretionary open-market sale of a material fraction of their position |
| Cluster Sell | 2 or more insiders at the same company sold within a 7-day window |
| Squeeze Setup | Composite squeeze score crossed 50 |
| High Borrow Rate | Annualized borrow rate crossed 10% |
| Dilution Warning | Company filed an S-1, S-3, or 424B prospectus |
| Activist Filing | New SC 13D or SC 13G — someone crossed 5% ownership |
| Discovery | Significant insider buy on a company not on the watchlist |

## Terminal commands (operator only)

```
sec                      Start the scanner
sec --lookup TICKER      Post an on-demand snapshot (terminal alternative to /lookup)
sec --add TICKER         Add a ticker to the watchlist
sec --remove TICKER      Remove a ticker from the watchlist
sec --list               Show current watchlist
sec --perf               Show price performance since each ticker was added
sec --scores             Print current squeeze scores without posting to Discord

sec --filter show        View current discovery filters
sec --filter min-score   Set minimum significance score (e.g. 50)
sec --filter max-cap     Set market cap ceiling (e.g. 200M)
sec --filter min-cap     Set market cap floor (e.g. 10M)
sec --filter max-float   Set max float (e.g. 50M)
sec --filter min-price   Set minimum price (e.g. 2)
sec --filter max-price   Set maximum price (e.g. 50)
sec --filter min-buy     Set minimum buy value for discovery (e.g. 25000)
sec --filter roles exec  Executives only (CEO/CFO/COO/CTO/President)
sec --filter roles all   Include directors
sec --filter off         Disable discovery scanner
sec --filter on          Re-enable discovery scanner
sec --filter reset       Restore all filter defaults
```

## Configuration

All thresholds are in `config.py`. Edit and restart to apply.

```python
MIN_BUY_VALUE_USD      = 5_000    # absolute floor, trades below this are always ignored
MIN_SIGNIFICANCE_SCORE = 30       # raise to 40-50 to reduce alert volume

SQUEEZE_ALERT_SCORE    = 50       # composite squeeze score needed to post
BORROW_ALERT_PCT       = 0.10     # annualized borrow rate threshold

CLUSTER_WINDOW_DAYS    = 7        # window for cluster detection
CLUSTER_MIN_INSIDERS   = 2        # insiders needed to form a cluster

POLL_INTERVAL          = 300      # seconds between scan cycles
LOOKBACK_DAYS          = 7        # how far back to look on first startup
```

## Insider Buy Scoring

A flat dollar threshold is the wrong filter. A $50K buy by a CEO doubling their position in a $30M cap is a far stronger signal than a $500K buy by a director in a $10B cap. The significance score normalises for company size and personal stake.

Every buy is scored across three dimensions.

**Conviction (up to 40 pts)** — what the insider is risking relative to themselves

| Factor | Points |
|---|---|
| Position increase above 10% | 4 |
| Position increase above 25% | 9 |
| Position increase above 50% | 14 |
| Position increase above 100% (doubled) | 20 |
| Position increase above 200% | 25 |
| First-ever open market purchase | 22 |
| Not a 10b5-1 plan (discretionary) | +10 |
| Buying within 20% of 52-week low | +5 |
| Buying within 10% of 52-week low | +10 |

**Materiality (up to 35 pts)** — how significant is this buy relative to the company

| Factor | Points |
|---|---|
| Buy equals 0.1% of market cap | 4 |
| Buy equals 0.5% of market cap | 8 |
| Buy equals 1% of market cap | 12 |
| Buy equals 2%+ of market cap | 15 |
| Buy equals 0.1% of float value | 3 |
| Buy equals 0.5% of float value | 7 |
| Buy equals 1%+ of float value | 10 |
| Buy equals 0.5x average daily volume | 3 |
| Buy equals 2x average daily volume | 6 |
| Buy equals 5x+ average daily volume | 10 |

**Role (up to 15 pts)** — how informed is this insider likely to be

| Role | Points |
|---|---|
| CEO / CFO / Principal Executive / Principal Financial | 15 |
| COO / CTO / CRO / President | 11 |
| Director | 7 |
| Other officer | 5 |

**Score tiers**

| Score | Tier |
|---|---|
| Below 30 | Filtered, never posted |
| 30 to 49 | Notable |
| 50 to 69 | Strong |
| 70 to 100 | Exceptional |

## Insider Sale Classification

Insiders sell for many reasons — taxes, diversification, a mortgage, a divorce — but only buy for one. Sells are noisier signals. The bot applies a classification pipeline to every Form 4 before scoring a disposal.

Only code S (open-market sale) is scored as a sentiment signal. Everything else is dropped before scoring.

| Code | What it is | Action |
|---|---|---|
| S | Open-market or private sale | Scored |
| F | Shares withheld for tax when RSUs vest | Dropped — the single biggest false-alarm source |
| G | Gift to charity or family | Dropped |
| U | Tender in M&A or change-of-control | Dropped |
| D | Return or forfeiture to issuer | Dropped |
| M + S pair | Option exercise followed by a sale of similar size | Scored with a 15-point penalty |

Exercise-and-dump detection: if a Form 4 contains an option exercise (code M) and then a sale (code S) of similar size in the same filing, the S is flagged. Score is reduced by 15 points.

**Sell significance score (max 100)**

| Factor | Points |
|---|---|
| Sold above 10% of position | 9 |
| Sold above 25% of position | 14 |
| Sold above 50% of position | 20 |
| Sold above 80% of position | 25 |
| Not a 10b5-1 plan | +10 |
| Selling within 20% of 52-week high | +5 |
| Selling within 10% of 52-week high | +10 |
| Exercise-and-sell detected | -15 |

## Cluster Detection

When 2 or more insiders file qualifying trades within a 7-day window the bot fires a cluster alert in addition to any individual alerts. A single insider buy or sell can have personal explanations. Multiple insiders acting independently in the same week almost cannot. Cluster buys and cluster sells are tracked separately.

## Squeeze Scoring

The squeeze score is a composite across five independent factors:

| Factor | Max pts |
|---|---|
| Short float percentage | 25 |
| Days to cover | 20 |
| Annualized borrow rate | 20 |
| Float size | 20 |
| Insider ownership percentage | 15 |

A score of 50+ triggers a Squeeze Setup alert. The score is also shown on every insider buy alert and lookup snapshot.

## Discovery Scanner

Scans all Form 4 filings across EDGAR every cycle, not just watchlist tickers. Default filters: $10M to $500M market cap, float under 50M shares, price $1 to $50, buy value at least $25K, executives only, minimum significance score 30. When a filing passes all filters it posts as a Discovery alert. All filters are adjustable via `/filter` in Discord.

## Data Sources

| Data | Source | Notes |
|---|---|---|
| Insider filings | SEC EDGAR submissions API | Insiders must file within 2 business days of the trade |
| Activist filings | SEC EDGAR full-text search | EFTS API ignores its own date filter — the bot filters manually |
| Dilution filings | SEC EDGAR submissions API | Real-time |
| Short interest and float | Finviz via FINRA | Bi-monthly FINRA data, not intraday |
| Borrow rate | iborrowdesk.com | Interactive Brokers data, updates during trading hours |
| Price, volume, market cap | Finviz | Near real-time during market hours |
