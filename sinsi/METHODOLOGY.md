# SEC Scanner — How Signals Are Scored

This document explains how each alert type is calculated, what thresholds are applied, and why. For usage commands, see [TUTORIAL.md](TUTORIAL.md).

---

## Core philosophy

Signal quality over alert volume. Most insider trackers post every Form 4 they see and let users drown in noise. This bot applies scoring and classification to every filing before it posts anything. The goal is that when an alert fires, it's worth reading.

The bot is tuned for **small and mid caps** ($10M–$500M). At that size, a $200K insider buy is material, short squeezes are possible, and insiders hold a meaningful fraction of the float. The same signals on AAPL or NVDA are noise.

---

## Insider Buy scoring

### Why a score instead of a dollar threshold

A $50K buy by a CEO who just doubled their position in a $30M cap is a far stronger signal than a $500K buy by a director trimming into a $10B cap. A flat dollar floor is the wrong filter. The significance score normalises for company size and personal stake so the bot can compare across the board.

### The three dimensions

Every buy is scored across three independent dimensions that together answer: *how much does this insider believe in this stock, and how much does this buy matter?*

**Conviction (up to 40 pts)** — what the insider is putting on the line relative to themselves

| Factor | Points |
|---|---|
| Position increase >10% | 4 |
| Position increase >25% | 9 |
| Position increase >50% | 14 |
| Position increase >100% (doubled) | 20 |
| Position increase >200% (more than doubled) | 25 |
| First-ever open market purchase (no prior position) | 22 |
| Not a 10b5-1 plan (discretionary buy) | +10 |
| Buying within 20% of 52W low | +5 |
| Buying within 10% of 52W low | +10 |

Position increase is computed from `sharesOwnedFollowingTransaction` in the XML:

```
pct_increase = shares_bought / (shares_owned_after - shares_bought)
```

A first-ever purchase (no prior holdings) gets 22 pts — less than a 200% increase but more than a doubling, because establishing a new position is a clear signal of conviction with no baseline to anchor on.

Buying near the 52-week low is scored positively because it signals the insider is buying into weakness — a stronger commitment than buying into a run.

**Materiality (up to 35 pts)** — how much does this buy matter to the company's size

| Factor | Points |
|---|---|
| Buy = 0.1% of market cap | 4 |
| Buy = 0.5% of market cap | 8 |
| Buy = 1% of market cap | 12 |
| Buy = 2%+ of market cap | 15 |
| Buy = 0.1% of float value | 3 |
| Buy = 0.5% of float value | 7 |
| Buy = 1%+ of float value | 10 |
| Buy = 0.5× average daily volume | 3 |
| Buy = 2× average daily volume | 6 |
| Buy = 5×+ average daily volume | 10 |

Three sub-dimensions are scored independently: market cap ratio, float value ratio, and days-of-volume. A buy can score on all three simultaneously. The cap ratio and float ratio together measure how much of the company the insider is absorbing. Days-of-volume measures how much of the daily liquidity this buy represents — relevant for very thinly traded small caps.

**Role (up to 15 pts)** — how informed is this insider likely to be

| Role | Points |
|---|---|
| CEO / CFO / Principal Executive / Principal Financial | 15 |
| COO / CTO / CRO / President | 11 |
| Director | 7 |
| Other officer | 5 |

Role is determined from the `officerTitle` field in the Form 4 XML. A CEO buying $100K is a different signal from a VP of Sales doing the same.

### Score tiers

| Score | Tier | Alert style |
|---|---|---|
| Below 30 | — | Filtered out silently |
| 30–49 | Notable | ⭐ Insider Buy |
| 50–69 | Strong | 🔥 Strong Insider Buy |
| 70–100 | Exceptional | 💎 Exceptional Insider Buy |

### Score examples

| Scenario | Score | Posts? |
|---|---|---|
| CEO doubles position in $40M cap, discretionary, near 52W low | ~80 | ✅ Exceptional |
| CFO first-ever buy $300K in $35M cap, discretionary | ~62 | ✅ Strong |
| Director +30% position $50K in $200M cap, 10b5-1 plan | ~26 | ❌ Filtered |
| CFO +8% position $500K in $5B cap, discretionary | ~32 | ✅ Notable (barely) |

---

## Insider Sale scoring

### Why sells are harder than buys

Insiders buy for one reason: they think the stock is going up. But they sell for many — taxes, diversification, a mortgage, a divorce, tuition. A sale is a fundamentally noisier signal than a purchase. The bot's entire job on the sell side is separating mechanical dispositions from genuine discretionary ones.

### The disposition taxonomy

Every Form 4 transaction line has a code. Only **S** carries real sentiment. Everything else is filtered before scoring:

**Tier 1 — the real signal**

| Code | What it is |
|---|---|
| **S** | Open-market or private sale — the insider chose to sell. Only this code is scored. |

**Tier 2 — mechanical / no signal**

| Code | What it is | Bot action |
|---|---|---|
| **F** | Shares withheld to cover tax when RSUs vest | Dropped silently. The executive made no decision — the company withheld automatically. This is the #1 false-alarm source for naive trackers. |
| **M** | Option or RSU exercise (acquisition of underlying) | Dropped as standalone. Appears as an acquisition row, not a disposal. |
| **D** | Return or forfeiture to issuer | Dropped — context-dependent, not a market sell. |

**Tier 3 — not a sentiment signal**

| Code | What it is | Bot action |
|---|---|---|
| **G** | Bona fide gift to charity or family | Dropped — no cash received, no opinion on the stock. |
| **U** | Tender in M&A / change-of-control | Dropped — event-driven, not a view on the company. |
| **J** | "Other" — footnote required | Dropped — too ambiguous to score reliably. |

**Special case: exercise-and-dump (M + S pair)**

If a Form 4 contains an option exercise (code M) and then a sale (code S) of similar size in the same filing, the bot flags the S as an exercise-and-sell. These typically occur near option expiry to monetize vesting comp — a scheduled event, not a real-time decision to exit a long position. The score is reduced by −15 points and the alert shows "🔄 Exercise-and-sell."

Detection logic:
```
is_exercise_dump = (exercise_shares_in_filing > 0)
                   and (0.5 ≤ shares_sold / exercise_shares ≤ 2.0)
```

### Two modifiers that apply to every S

**10b5-1 plan** — a pre-arranged trading plan filed months in advance. The insider pre-scheduled this sale; it carries almost no real-time information. The bot still posts 10b5-1 plan sales if the score is high enough (e.g. a CEO selling 70% of their position on a plan still scores around 50). These are clearly labelled "📋 Pre-scheduled."

**% of position sold** — the most important number on the sell side. Trimming 3% is noise; dumping 80% is a signal. The bot reconstructs the pre-trade position from `sharesOwnedFollowingTransaction` in the XML:

```
pct_sold = shares_sold / (shares_owned_after + shares_sold)
```

### Sell significance score (max 100)

**Conviction (up to 40 pts)**

| Factor | Points |
|---|---|
| Sold >10% of position | 9 |
| Sold >25% of position | 14 |
| Sold >50% of position (majority) | 20 |
| Sold >80% of position (nearly all) | 25 |
| Not a 10b5-1 plan (discretionary) | +10 |
| Selling within 20% of 52W high | +5 |
| Selling within 10% of 52W high | +10 |
| Exercise-and-sell detected | −15 |

Selling near the 52-week high is scored as a bearish tell — the insider is taking profits at the top rather than holding through further upside.

**Materiality and Role** — same tables as Insider Buys above.

### Score tiers (same as buys)

| Score | Tier |
|---|---|
| Below 30 | Filtered out |
| 30–49 | Notable ⭐ |
| 50–69 | Strong 🔥 |
| 70–100 | Exceptional 💎 |

---

## Cluster detection

### Cluster Buy

When 2 or more insiders at the same company file qualifying buys within a 7-day window, the bot fires a 🔴 Cluster Buy alert in addition to any individual buy alerts.

Why it matters: a single insider buy can have personal explanations. Multiple insiders buying independently at the same time almost cannot. It rules out the idiosyncratic noise and points to a shared view of the company's near-term prospects.

Threshold: `CLUSTER_MIN_INSIDERS = 2`, `CLUSTER_WINDOW_DAYS = 7`, `CLUSTER_MIN_TOTAL_USD` (combined value floor).

### Cluster Sell

Same logic applied to open-market S-code sales. If 2+ insiders file discretionary sales within the window, the bot fires a 🔴 Cluster Sell. Coordinated selling is far more meaningful than any single sale for exactly the same reason: it rules out personal explanations.

Only S-coded transactions (post-classification) feed the sell cluster tracker — F withholding, gifts, and exercise-and-dumps are excluded.

---

## Squeeze scoring

The squeeze score is a composite of five independent signals. Each can contribute independently.

| Factor | Max pts | What it measures |
|---|---|---|
| Short float % | 25 | How large the short position is relative to float |
| Days to cover (DTC) | 20 | How many days of average volume it would take to cover all shorts |
| Borrow rate | 20 | How expensive it is to maintain a short position |
| Float size | 20 | How tight the float is (smaller = easier to squeeze) |
| Insider ownership | 15 | How much of the float is locked up with insiders |

A score of 50+ triggers a 🟠 Squeeze Setup alert. The score is shown on every insider buy alert as market context so you can cross-reference immediately.

The borrow rate is fetched separately from iborrowdesk.com (Interactive Brokers data) and also triggers its own 💛 High Borrow Rate alert when it crosses `BORROW_ALERT_PCT` (default 10% annualized).

---

## Discovery scanner

The discovery scanner scans **all** Form 4 filings across EDGAR every cycle — not just watchlist tickers. When a filing passes all filters and the significance score clears the threshold, it posts as a 🟢 Discovery.

Purpose: to surface small caps you don't know about yet. The watchlist is for monitoring; discovery is for finding new ideas.

**Default filters:**
- Market cap: $10M – $500M
- Float: under 50M shares
- Price: $1 – $50
- Minimum buy value: $5,000
- Roles: executives only (CEO, CFO, COO, CTO, President)
- Significance score: ≥ `MIN_SIGNIFICANCE_SCORE` (same as watchlist)

The role filter exists because directors at large companies file Form 4s constantly for routine option exercises and RSU vesting. Restricting to executives on discovery reduces noise significantly. Use `sec --filter roles all` to include directors if you want wider coverage.

**Director special case**: micro-cap director buys with high short interest are flagged separately as 🎯 Director Buy within discovery, even under the exec-only filter. The rationale: at very small companies, directors are often operators, not passive board members, and their buying carries more weight.

---

## Data sources and limitations

| Data | Source | Notes |
|---|---|---|
| Insider filings | SEC EDGAR submissions API | Insiders must file within 2 business days of trade. The bot sees filings as soon as they appear. |
| Activist filings | SEC EDGAR full-text search (EFTS) | The EFTS API ignores its own date filter — the bot filters manually after fetching. |
| Short interest | Finviz (FINRA data) | FINRA publishes bi-monthly; Finviz updates on their schedule. Not real-time. |
| Borrow rate | iborrowdesk.com (IBKR data) | Reflects Interactive Brokers' borrow desk, updated throughout the day. Other brokers may differ. |
| Market cap, float, price | Finviz | Near real-time during market hours, stale overnight and weekends. |

**What the bot cannot see:**
- Pre-market or after-hours trades until the Form 4 is filed
- Dark pool or OTC activity
- Derivative positions (the bot parses only `nonDerivativeTransaction` rows from Form 4 XML)
- Non-US exchanges (EDGAR is US-only)
