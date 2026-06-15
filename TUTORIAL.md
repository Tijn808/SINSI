# SEC Scanner — Commands

```
sec                      Start the scanner

sec --lookup TICKER      Post an on-demand snapshot to Discord (does not add to watchlist)
sec --add TICKER         Add a ticker to the watchlist
sec --remove TICKER      Remove a ticker from the watchlist
sec --list               Show all tickers currently on the watchlist
sec --perf               Show price performance since each ticker was added
sec --scores             Print current squeeze scores without posting to Discord

sec --filter show        View current discovery filters
sec --filter max-cap     Set market cap ceiling       example: sec --filter max-cap 200M
sec --filter min-cap     Set market cap floor         example: sec --filter min-cap 10M
sec --filter max-float   Set maximum float            example: sec --filter max-float 50M
sec --filter min-price   Set minimum price            example: sec --filter min-price 2
sec --filter max-price   Set maximum price            example: sec --filter max-price 50
sec --filter min-buy     Set minimum buy value        example: sec --filter min-buy 25000
sec --filter roles exec  Executives only (CEO / CFO / COO / CTO / President)
sec --filter roles all   Include directors
sec --filter off         Disable the discovery scanner
sec --filter on          Re-enable the discovery scanner
sec --filter reset       Restore all filter defaults
```

Run in the background:

```
nohup sec > logs.txt 2>&1 &
tail -f logs.txt
pkill -f "sec"
```
