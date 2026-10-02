# Stock Portal — data engine

Free, automated morning snapshot for the Stock Portal artifact.

| Data | Source | Cost |
|---|---|---|
| Prices, 1y daily bars, SMA20/50/200, RSI14, ATR14, rel. volume, 52w range | Yahoo Finance via `yfinance` | free, no key |
| Analyst mean target, # analysts, earnings date, short % float, fwd P/E | Yahoo Finance | free |
| Company news (last ~30h), recommendation trend, earnings calendar, quote fallback | Finnhub free tier (60 calls/min) | free key |
| News fallback | Google News RSS | free |
| Macro: SPY, QQQ, SOX, VIX, 10Y, DXY, COMEX copper, URA, gold, WTI | Yahoo Finance | free |

## Setup (10 minutes, once)
1. Create a **public** GitHub repo (e.g. `stock-portal-data`) and push this folder.
2. Get a free Finnhub key at finnhub.io → repo **Settings → Secrets → Actions → New secret** `FINNHUB_KEY`.
   (Optional — without it the job falls back to Yahoo + Google News.)
3. Actions tab → **daily-snapshot → Run workflow** once to test. It then runs every weekday ~6:40 AM CT.
4. Your snapshot lives at `https://raw.githubusercontent.com/<you>/stock-portal-data/main/data/snapshot.json`.

## Adding tickers
Add them to `config/universe.json` (`user_added` or a phase list). The portal's **Export universe** button produces this file for you.

## Materiality rules (computed here, shown in the portal)
±2% day move · analyst mean target change ≥10% vs. previous run · contract/deal keywords · major-headline keywords.
