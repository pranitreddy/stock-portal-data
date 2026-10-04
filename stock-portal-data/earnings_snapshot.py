#!/usr/bin/env python3
"""
Earnings Portal snapshot (free sources only).

  calendar        : Nasdaq earnings calendar API  (fallback: Finnhub /calendar/earnings if FINNHUB_KEY)
  prices / history: Yahoo Finance via yfinance (1y daily bars + 5y bars for past-earnings reactions)
  options         : Yahoo option chains -> implied move, ATM IV, put/call, unusual strikes, sample iron condor
  insiders        : Yahoo insider transactions (fallback: Finnhub /stock/insider-transactions)
  estimates       : Yahoo EPS trend (revisions), earnings/revenue estimates, analyst changes, targets
  news            : Yahoo news (last 7 days)

Writes data/earnings.json. Filters: price >= MIN_PRICE and 20-day average dollar volume >= MIN_DOLLAR_VOL.
"""
import json, os, sys, time, math, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "data/earnings.json"
MIN_PRICE = float(os.environ.get("MIN_PRICE", 70))
MIN_DOLLAR_VOL = float(os.environ.get("MIN_DOLLAR_VOL", 75e6))   # 20-day avg $ volume
MIN_MCAP = 2e9
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip()
SECTOR_ETF = {"Consumer Defensive": "XLP", "Consumer Cyclical": "XLY", "Industrials": "XLI", "Basic Materials": "XLB",
              "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV", "Energy": "XLE", "Utilities": "XLU",
              "Communication Services": "XLC", "Real Estate": "XLRE"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*"}


def log(*a):
    print(*a, file=sys.stderr)


def f(x, d=2):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return None
        return round(float(x), d)
    except Exception:
        return None


def week_days():
    """Mon..Fri of the current week; on Sat/Sun use next week."""
    today = dt.date.today()
    start = today - dt.timedelta(days=today.weekday())
    if today.weekday() >= 5:
        start += dt.timedelta(days=7)
    return [start + dt.timedelta(days=i) for i in range(5)]


def parse_money(s):
    if not s:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except Exception:
        return None


# ---------------- calendar ----------------
def calendar():
    import requests
    rows = []
    for d in week_days():
        try:
            r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", headers=UA, timeout=20)
            data = (r.json().get("data") or {}).get("rows") or []
            for x in data:
                t = x.get("time", "")
                rows.append({"sym": x["symbol"].strip().upper(), "name": x.get("name"), "date": d.isoformat(),
                             "when": "BMO" if "pre" in t else "AMC" if "after" in t else "TBD",
                             "epsEst": parse_money(x.get("epsForecast")), "nEst": x.get("noOfEsts"),
                             "lastYearEps": parse_money(x.get("lastYearEPS")), "mcap": parse_money(x.get("marketCap")),
                             "fq": x.get("fiscalQuarterEnding")})
            time.sleep(1)
        except Exception as e:
            log(f"nasdaq calendar {d}: {e}")
    if not rows and FINNHUB_KEY:
        try:
            days = week_days()
            r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                             params={"from": days[0].isoformat(), "to": days[-1].isoformat(), "token": FINNHUB_KEY}, timeout=20)
            for x in r.json().get("earningsCalendar", []):
                rows.append({"sym": x["symbol"], "name": None, "date": x["date"],
                             "when": {"bmo": "BMO", "amc": "AMC"}.get(x.get("hour"), "TBD"),
                             "epsEst": x.get("epsEstimate"), "nEst": None, "lastYearEps": None, "mcap": None, "fq": None})
        except Exception as e:
            log(f"finnhub calendar: {e}")
    seen, out = set(), []
    for r in rows:
        if r["sym"] not in seen and r["sym"].replace(".", "").isalpha():
            seen.add(r["sym"]); out.append(r)
    return out


# ---------------- technicals ----------------
def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = [max(b - a, 0) for a, b in zip(c[-n - 1:-1], c[-n:])]
    l = [max(a - b, 0) for a, b in zip(c[-n - 1:-1], c[-n:])]
    ag, al = sum(g) / n, sum(l) / n
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)


def tech(df):
    df = df.dropna(subset=["Close"])
    c = [float(x) for x in df["Close"]]; h = [float(x) for x in df["High"]]; lo = [float(x) for x in df["Low"]]
    v = [float(x) for x in df["Volume"]]
    p = c[-1]
    tr = [max(a - b, abs(a - pc), abs(b - pc)) for a, b, pc in zip(h[-14:], lo[-14:], c[-15:-1])]
    atr = sum(tr) / 14 if len(tr) == 14 else None
    sma = lambda n: round(sum(c[-n:]) / n, 2) if len(c) >= n else None
    dv = [a * b for a, b in zip(c[-20:], v[-20:])]
    return {
        "price": round(p, 2), "chg1d": f((p / c[-2] - 1) * 100), "chg5d": f((p / c[-6] - 1) * 100) if len(c) > 5 else None,
        "chg1m": f((p / c[-22] - 1) * 100) if len(c) > 21 else None, "chg3m": f((p / c[-64] - 1) * 100) if len(c) > 63 else None,
        "sma20": sma(20), "sma50": sma(50), "sma200": sma(200), "rsi14": rsi(c),
        "atr14": f(atr), "atrPct": f(atr / p * 100) if atr else None,
        "high52": round(max(h[-252:]), 2), "low52": round(min(lo[-252:]), 2),
        "swingHi20": round(max(h[-20:]), 2), "swingLo20": round(min(lo[-20:]), 2),
        "avgVol20": int(sum(v[-20:]) / 20), "avgDollarVol20": int(sum(dv) / len(dv)),
        "relVol": f(v[-1] / (sum(v[-21:-1]) / 20)) if len(v) > 21 and sum(v[-21:-1]) else None,
        "spark": [round(x, 2) for x in c[-90:]],
    }


# ---------------- past earnings reactions ----------------
def reactions(tk, bars5y):
    out = []
    try:
        ed = tk.get_earnings_dates(limit=20)
    except Exception as e:
        log(f"  earnings dates: {e}"); return out
    if ed is None or ed.empty:
        return out
    closes = bars5y["Close"].dropna(); opens = bars5y["Open"].dropna()
    idx = [d.date() for d in closes.index]
    now = dt.datetime.now(dt.timezone.utc)
    for ts, row in ed.iterrows():
        try:
            if ts.to_pydatetime() > now or (row.get("Reported EPS") is None or math.isnan(row.get("Reported EPS"))):
                continue
            d = ts.date(); hour = ts.hour if ts.tzinfo is None else ts.tz_convert("America/New_York").hour
            amc = hour >= 12
            # reaction day = first session after report for AMC, same day for BMO
            pos = next((i for i, x in enumerate(idx) if (x > d if amc else x >= d)), None)
            if pos is None or pos == 0 or pos + 5 >= len(idx):
                continue
            prev = float(closes.iloc[pos - 1]); c0 = float(closes.iloc[pos]); o0 = float(opens.iloc[pos])
            out.append({"date": d.isoformat(), "when": "AMC" if amc else "BMO",
                        "epsEst": f(row.get("EPS Estimate")), "eps": f(row.get("Reported EPS")),
                        "surprise": f(row.get("Surprise(%)"), 1),
                        "gap": f((o0 / prev - 1) * 100), "move": f((c0 / prev - 1) * 100),
                        "drift5": f((float(closes.iloc[pos + 5]) / c0 - 1) * 100)})
        except Exception:
            continue
    return out[:12]


def reaction_stats(rx):
    if not rx:
        return {}
    mv = [r["move"] for r in rx if r["move"] is not None]
    beats = [r for r in rx if (r["surprise"] or 0) > 0]
    misses = [r for r in rx if (r["surprise"] or 0) <= 0]
    avg = lambda xs: f(sum(xs) / len(xs)) if xs else None
    return {"n": len(mv), "avgAbsMove": avg([abs(x) for x in mv]), "maxAbsMove": f(max(abs(x) for x in mv)) if mv else None,
            "upCount": sum(1 for x in mv if x > 0), "beatRate": f(len(beats) / len(rx) * 100, 0),
            "avgMoveOnBeat": avg([r["move"] for r in beats if r["move"] is not None]),
            "avgMoveOnMiss": avg([r["move"] for r in misses if r["move"] is not None]),
            "avgDrift5": avg([r["drift5"] for r in rx if r["drift5"] is not None]),
            "gapFadeRate": f(sum(1 for r in rx if r["gap"] and r["move"] is not None and abs(r["move"]) < abs(r["gap"])) / len(rx) * 100, 0)}


# ---------------- options ----------------
def mid(row):
    b, a, l = row.get("bid"), row.get("ask"), row.get("lastPrice")
    if b and a and a > 0:
        return (b + a) / 2
    return l or 0


def options(tk, price, edate):
    out = {}
    try:
        exps = tk.options
    except Exception as e:
        log(f"  options list: {e}"); return out
    if not exps:
        return out
    exp = next((e for e in exps if e >= edate), exps[0])
    out["expiry"] = exp
    try:
        ch = tk.option_chain(exp)
    except Exception as e:
        log(f"  chain: {e}"); return out
    calls, puts = ch.calls.fillna(0), ch.puts.fillna(0)
    strikes = sorted(set(calls["strike"]) & set(puts["strike"]))
    if not strikes:
        return out
    atm = min(strikes, key=lambda k: abs(k - price))
    c = calls[calls.strike == atm].iloc[0].to_dict(); p = puts[puts.strike == atm].iloc[0].to_dict()
    straddle = mid(c) + mid(p)
    out.update({"atm": atm, "straddle": f(straddle), "impliedMovePct": f(straddle / price * 100),
                "atmIV": f(((c.get("impliedVolatility") or 0) + (p.get("impliedVolatility") or 0)) / 2 * 100, 1),
                "callVol": int(calls.volume.sum()), "putVol": int(puts.volume.sum()),
                "callOI": int(calls.openInterest.sum()), "putOI": int(puts.openInterest.sum())})
    out["pcVolRatio"] = f(out["putVol"] / out["callVol"]) if out["callVol"] else None
    out["pcOIRatio"] = f(out["putOI"] / out["callOI"]) if out["callOI"] else None
    unusual = []
    for side, df in (("C", calls), ("P", puts)):
        for _, r in df.iterrows():
            if r.volume >= 500 and r.volume > 2 * max(r.openInterest, 1):
                unusual.append({"side": side, "strike": f(r.strike), "vol": int(r.volume), "oi": int(r.openInterest),
                                "iv": f(r.impliedVolatility * 100, 1), "last": f(r.lastPrice)})
    out["unusual"] = sorted(unusual, key=lambda x: -x["vol"])[:6]
    # sample iron condor: shorts just outside the implied move, wings one step further
    try:
        mv = straddle
        sc = min((k for k in strikes if k >= price + mv), default=None)
        sp = max((k for k in strikes if k <= price - mv), default=None)
        if sc and sp:
            lc = min((k for k in strikes if k > sc), default=None)
            lp = max((k for k in strikes if k < sp), default=None)
            if lc and lp:
                g = lambda df, k: mid(df[df.strike == k].iloc[0].to_dict())
                credit = g(calls, sc) - g(calls, lc) + g(puts, sp) - g(puts, lp)
                width = max(lc - sc, sp - lp)
                out["condor"] = {"shortPut": f(sp), "longPut": f(lp), "shortCall": f(sc), "longCall": f(lc),
                                 "credit": f(credit), "maxLoss": f(width - credit), "width": f(width),
                                 "breakevens": [f(sp - credit), f(sc + credit)]}
    except Exception as e:
        log(f"  condor: {e}")
    return out


# ---------------- insiders / estimates / analysts / news ----------------
def insiders(tk, sym):
    out = {"buys": 0, "sells": 0, "buyValue": 0, "sellValue": 0, "rows": []}
    cutoff = dt.date.today() - dt.timedelta(days=180)
    try:
        df = tk.insider_transactions
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                d = r.get("Start Date")
                d = d.date() if hasattr(d, "date") else None
                if not d or d < cutoff:
                    continue
                text = str(r.get("Text") or r.get("Transaction") or "")
                kind = "buy" if "Purchase" in text or "Buy" in text else "sell" if "Sale" in text else "other"
                val = float(r.get("Value") or 0)
                if kind == "buy":
                    out["buys"] += 1; out["buyValue"] += val
                elif kind == "sell":
                    out["sells"] += 1; out["sellValue"] += val
                if kind != "other":
                    out["rows"].append({"date": d.isoformat(), "who": r.get("Insider"), "role": r.get("Position"),
                                        "kind": kind, "shares": int(r.get("Shares") or 0), "value": int(val)})
    except Exception as e:
        log(f"  insiders yahoo: {e}")
    if not out["rows"] and FINNHUB_KEY:
        try:
            import requests
            r = requests.get("https://finnhub.io/api/v1/stock/insider-transactions",
                             params={"symbol": sym, "from": cutoff.isoformat(), "token": FINNHUB_KEY}, timeout=20)
            for x in r.json().get("data", []):
                code = x.get("transactionCode")
                if code not in ("P", "S"):
                    continue
                val = abs((x.get("change") or 0) * (x.get("transactionPrice") or 0))
                kind = "buy" if code == "P" else "sell"
                out["buys" if kind == "buy" else "sells"] += 1
                out["buyValue" if kind == "buy" else "sellValue"] += val
                out["rows"].append({"date": x.get("transactionDate"), "who": x.get("name"), "role": None,
                                    "kind": kind, "shares": abs(int(x.get("change") or 0)), "value": int(val)})
        except Exception as e:
            log(f"  insiders finnhub: {e}")
    out["rows"] = sorted(out["rows"], key=lambda x: x["date"], reverse=True)[:10]
    out["buyValue"], out["sellValue"] = int(out["buyValue"]), int(out["sellValue"])
    return out


def estimates(tk):
    out = {}
    try:
        et = tk.eps_trend
        if et is not None and "0q" in et.index:
            r = et.loc["0q"]
            cur, d30, d90 = r.get("current"), r.get("30daysAgo"), r.get("90daysAgo")
            out["epsTrend"] = {k: f(r.get(k), 3) for k in ("current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo")}
            out["rev30"] = f((cur / d30 - 1) * 100, 1) if cur and d30 else None
            out["rev90"] = f((cur / d90 - 1) * 100, 1) if cur and d90 else None
    except Exception as e:
        log(f"  eps trend: {e}")
    try:
        ee = tk.earnings_estimate
        if ee is not None and "0q" in ee.index:
            r = ee.loc["0q"]
            out["epsAvg"], out["epsLow"], out["epsHigh"] = f(r.get("avg"), 3), f(r.get("low"), 3), f(r.get("high"), 3)
            out["epsYearAgo"], out["epsGrowth"] = f(r.get("yearAgoEps"), 3), f((r.get("growth") or 0) * 100, 1)
    except Exception as e:
        log(f"  eps est: {e}")
    try:
        re_ = tk.revenue_estimate
        if re_ is not None and "0q" in re_.index:
            r = re_.loc["0q"]
            out["revAvg"], out["revGrowth"] = r.get("avg") and int(r.get("avg")), f((r.get("growth") or 0) * 100, 1)
    except Exception as e:
        log(f"  rev est: {e}")
    return out


def analysts(tk, info):
    out = {"targetMean": f(info.get("targetMeanPrice")), "targetHigh": f(info.get("targetHighPrice")),
           "targetLow": f(info.get("targetLowPrice")), "n": info.get("numberOfAnalystOpinions"),
           "recKey": info.get("recommendationKey"), "changes": []}
    try:
        ud = tk.upgrades_downgrades
        if ud is not None and not ud.empty:
            cutoff = dt.datetime.now() - dt.timedelta(days=60)
            for ts, r in ud.iterrows():
                t = ts.to_pydatetime().replace(tzinfo=None)
                if t < cutoff:
                    continue
                out["changes"].append({"date": t.date().isoformat(), "firm": r.get("Firm"), "action": r.get("Action"),
                                       "from": r.get("FromGrade"), "to": r.get("ToGrade"),
                                       "ptFrom": f(r.get("priorPriceTarget")), "pt": f(r.get("currentPriceTarget"))})
            out["changes"] = out["changes"][:8]
    except Exception as e:
        log(f"  upgrades: {e}")
    return out


def news(tk):
    items, cutoff = [], dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    try:
        for n in (tk.news or [])[:20]:
            c = n.get("content", n)
            ts = c.get("pubDate") or c.get("providerPublishTime")
            t = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if isinstance(ts, (int, float)) else \
                dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) else None
            if t and t >= cutoff and c.get("title"):
                items.append({"t": t.isoformat(), "title": c["title"][:180],
                              "src": (c.get("provider") or {}).get("displayName") or c.get("publisher", ""),
                              "url": (c.get("canonicalUrl") or {}).get("url") or c.get("link", "")})
    except Exception as e:
        log(f"  news: {e}")
    return sorted(items, key=lambda x: x["t"], reverse=True)[:8]


def strength(t, est, rs3m, stats):
    """0-100 composite: trend, relative strength, estimate revisions, earnings track record."""
    s, parts = 0, []
    p = t["price"]
    if t["sma50"] and p > t["sma50"]: s += 15; parts.append("above 50-day (+15)")
    if t["sma200"] and p > t["sma200"]: s += 15; parts.append("above 200-day (+15)")
    if t["sma50"] and t["sma200"] and t["sma50"] > t["sma200"]: s += 10; parts.append("50-day above 200-day (+10)")
    if rs3m is not None:
        pts = max(0, min(20, 10 + rs3m)); s += pts; parts.append(f"3-month vs S&P {rs3m:+.1f} pts (+{pts:.0f})")
    if t["high52"] and p >= t["high52"] * 0.9: s += 10; parts.append("within 10% of 52-week high (+10)")
    r30 = est.get("rev30")
    if r30 is not None:
        pts = 10 if r30 > 1 else 5 if r30 >= -1 else 0; s += pts; parts.append(f"EPS estimate 30-day revision {r30:+.1f}% (+{pts})")
    br = stats.get("beatRate")
    if br is not None:
        pts = round(br / 10); s += pts; parts.append(f"beat rate {br:.0f}% (+{pts})")
    return int(min(100, s)), parts


def main():
    import yfinance as yf
    days = week_days()
    cal = calendar()
    log(f"calendar rows: {len(cal)}")
    cand = [r for r in cal if (r["mcap"] or 0) >= MIN_MCAP or r["mcap"] is None]
    syms = [r["sym"] for r in cand]
    etfs = sorted(set(SECTOR_ETF.values()) | {"SPY"})
    bars = {}
    if syms:
        df = yf.download(syms + etfs, period="1y", interval="1d", group_by="ticker", auto_adjust=True, threads=False, progress=False)
        for s in syms + etfs:
            try:
                b = df[s].dropna(subset=["Close"])
                if len(b) > 30:
                    bars[s] = b
            except Exception:
                pass
    spy3 = tech(bars["SPY"])["chg3m"] if "SPY" in bars else None
    out = {"asOf": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "week": [days[0].isoformat(), days[-1].isoformat()],
           "filters": {"minPrice": MIN_PRICE, "minDollarVol": MIN_DOLLAR_VOL, "minMcap": MIN_MCAP},
           "calendar": cal, "tickers": {}, "excluded": [], "errors": [],
           "etf": {e: {k: tech(bars[e])[k] for k in ("price", "chg5d", "chg1m", "chg3m")} for e in etfs if e in bars}}
    for r in cand:
        s = r["sym"]
        try:
            if s not in bars:
                out["excluded"].append({"sym": s, "why": "no price data"}); continue
            t = tech(bars[s])
            if t["price"] < MIN_PRICE:
                out["excluded"].append({"sym": s, "why": f"price ${t['price']}"}); continue
            if t["avgDollarVol20"] < MIN_DOLLAR_VOL:
                out["excluded"].append({"sym": s, "why": f"liquidity ${t['avgDollarVol20']/1e6:.0f}M/day"}); continue
            log(s)
            tk = yf.Ticker(s)
            info = {}
            try:
                info = tk.info or {}
            except Exception as e:
                log(f"  info: {e}")
            b5 = tk.history(period="5y", auto_adjust=True)
            rx = reactions(tk, b5)
            st = reaction_stats(rx)
            est = estimates(tk)
            etf = SECTOR_ETF.get(info.get("sector"))
            rs3 = f(t["chg3m"] - spy3) if t["chg3m"] is not None and spy3 is not None else None
            score, parts = strength(t, est, rs3, st)
            row = {**r, **t, "name": info.get("shortName") or r.get("name"), "sector": info.get("sector"),
                   "industry": info.get("industry"), "mcap": info.get("marketCap") or r.get("mcap"),
                   "fwdPE": f(info.get("forwardPE"), 1), "shortPctFloat": f((info.get("shortPercentOfFloat") or 0) * 100, 1) or None,
                   "shortRatio": f(info.get("shortRatio"), 1), "beta": f(info.get("beta")),
                   "sectorEtf": etf, "rs3m": rs3,
                   "rsSector3m": f(t["chg3m"] - out["etf"][etf]["chg3m"]) if etf in out["etf"] and t["chg3m"] is not None else None,
                   "history": rx, "stats": st, "estimates": est, "analysts": analysts(tk, info),
                   "insiders": insiders(tk, s), "options": options(tk, t["price"], r["date"]), "news": news(tk),
                   "strength": score, "strengthParts": parts,
                   "about": (info.get("longBusinessSummary") or "")[:700], "website": info.get("website"),
                   "employees": info.get("fullTimeEmployees"),
                   "fin": {"revenue": info.get("totalRevenue"), "revGrowth": f((info.get("revenueGrowth") or 0) * 100, 1) if info.get("revenueGrowth") is not None else None,
                           "grossMargin": f((info.get("grossMargins") or 0) * 100, 1) if info.get("grossMargins") is not None else None,
                           "opMargin": f((info.get("operatingMargins") or 0) * 100, 1) if info.get("operatingMargins") is not None else None,
                           "netMargin": f((info.get("profitMargins") or 0) * 100, 1) if info.get("profitMargins") is not None else None,
                           "cash": info.get("totalCash"), "debt": info.get("totalDebt"), "debtToEquity": f(info.get("debtToEquity"), 0),
                           "fcf": info.get("freeCashflow"), "divYield": f(info.get("dividendYield"), 2),
                           "payout": f((info.get("payoutRatio") or 0) * 100, 0) if info.get("payoutRatio") is not None else None}}
            out["tickers"][s] = row
            time.sleep(1)
        except Exception as e:
            import traceback; traceback.print_exc()
            out["errors"].append(f"{s}: {type(e).__name__}: {e}"[:200])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), default=str))
    log(f"wrote {OUT}: {len(out['tickers'])} tickers kept, {len(out['excluded'])} excluded, {len(out['errors'])} errors")


if __name__ == "__main__":
    main()
