#!/usr/bin/env python3
"""
Stock Portal daily snapshot.

Free sources, in order of preference:
  prices / technicals : Yahoo Finance via yfinance (1y daily bars, batched)
  quote fallback      : Finnhub /quote            (only if FINNHUB_KEY is set)
  news                : Finnhub /company-news  -> Yahoo news via yfinance -> Google News RSS
  analysts            : Yahoo targetMeanPrice + Finnhub recommendation trends (if key)
  earnings date       : Yahoo calendar -> Finnhub earnings calendar (if key)

Writes data/snapshot.json (what the portal imports) and data/state.json
(yesterday's analyst targets, used to detect >=10% target changes).

Usage:  python fetch_snapshot.py            # live
        python fetch_snapshot.py --mock     # fake numbers, for testing the portal only
"""
import json, math, os, sys, time, random, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config/universe.json").read_text())
OUT = ROOT / "data/snapshot.json"
STATE = ROOT / "data/state.json"
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip()
NEWS_HOURS = 30            # look-back window for "last ~24h" (covers overnight + premarket)
MAX_HEADLINES = 4
DEAL_WORDS = ("contract", "agreement", "awarded", "award", "deal", "partnership", "acquire",
              "acquisition", "merger", "supply agreement", "order", "selected by", "wins")
MAJOR_WORDS = ("earnings", "guidance", "outlook", "downgrade", "upgrade", "price target",
               "sec", "probe", "lawsuit", "recall", "offering", "buyback", "ceo", "resign",
               "tariff", "export", "ban", "halt", "fda", "strike", "outage", "beats", "misses")


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def all_symbols():
    syms, phase_of = [], {}
    for phase, lst in CFG["tickers"].items():
        for s in lst:
            if s not in phase_of:
                syms.append(s); phase_of[s] = phase
    for s in CFG.get("user_added", []):
        s = s.upper()
        if s not in phase_of:
            syms.append(s); phase_of[s] = "Added by you"
    return syms, phase_of


# ---------- technicals ----------
def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = [max(h - l, abs(h - pc), abs(l - pc))
           for h, l, pc in zip(highs[-n:], lows[-n:], closes[-n - 1:-1])]
    return sum(trs) / n


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 2)


def sma(xs, n):
    return round(sum(xs[-n:]) / n, 2) if len(xs) >= n else None


def metrics_from_bars(df):
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None
    c = [float(x) for x in df["Close"]]
    h = [float(x) for x in df["High"]]
    l = [float(x) for x in df["Low"]]
    v = [float(x) for x in df["Volume"]] if "Volume" in df else []
    last = c[-1]
    a = atr(h, l, c)
    avgv = sum(v[-21:-1]) / 20 if len(v) >= 21 else None
    return {
        "price": round(last, 2),
        "prevClose": round(c[-2], 2) if len(c) > 1 else None,
        "chg1d": pct(last, c[-2]) if len(c) > 1 else None,
        "chg5d": pct(last, c[-6]) if len(c) > 5 else None,
        "chg1m": pct(last, c[-22]) if len(c) > 21 else None,
        "chg3m": pct(last, c[-64]) if len(c) > 63 else None,
        "high52": round(max(h[-252:]), 2),
        "low52": round(min(l[-252:]), 2),
        "sma20": sma(c, 20), "sma50": sma(c, 50), "sma200": sma(c, 200),
        "rsi14": rsi(c),
        "atr14": round(a, 2) if a else None,
        "atrPct": round(a / last * 100, 2) if a else None,
        "relVol": round(v[-1] / avgv, 2) if avgv else None,
        "lastBar": str(df.index[-1].date()),
        "spark": [round(x, 2) for x in c[-60:]],
    }


# ---------- news ----------
def finnhub(path, **params):
    import requests
    params["token"] = FINNHUB_KEY
    r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=15)
    r.raise_for_status()
    time.sleep(1.1)            # stay under 60 calls/min
    return r.json()


def news_for(sym, ytk):
    cutoff = now_utc() - dt.timedelta(hours=NEWS_HOURS)
    items = []
    if FINNHUB_KEY:
        try:
            d = finnhub("company-news", symbol=sym,
                        **{"from": (now_utc() - dt.timedelta(days=2)).date().isoformat(),
                           "to": now_utc().date().isoformat()})
            for n in d:
                t = dt.datetime.fromtimestamp(n.get("datetime", 0), dt.timezone.utc)
                if t >= cutoff and n.get("headline"):
                    items.append({"t": t.isoformat(), "title": n["headline"][:160],
                                  "src": n.get("source", ""), "url": n.get("url", "")})
        except Exception as e:
            print(f"  finnhub news {sym}: {e}", file=sys.stderr)
    if not items and ytk is not None:
        try:
            for n in (ytk.news or [])[:15]:
                c = n.get("content", n)
                title = c.get("title")
                ts = c.get("pubDate") or c.get("providerPublishTime")
                if isinstance(ts, (int, float)):
                    t = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
                elif isinstance(ts, str):
                    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                else:
                    continue
                url = (c.get("canonicalUrl") or {}).get("url") or c.get("link", "")
                src = (c.get("provider") or {}).get("displayName") or c.get("publisher", "")
                if title and t >= cutoff:
                    items.append({"t": t.isoformat(), "title": title[:160], "src": src, "url": url})
        except Exception as e:
            print(f"  yahoo news {sym}: {e}", file=sys.stderr)
    if not items:
        try:
            import feedparser
            f = feedparser.parse(f"https://news.google.com/rss/search?q={sym}+stock+when:1d&hl=en-US&gl=US&ceid=US:en")
            for e in f.entries[:10]:
                t = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                if t >= cutoff:
                    items.append({"t": t.isoformat(), "title": e.title[:160],
                                  "src": getattr(e, "source", {}).get("title", "Google News"), "url": e.link})
        except Exception as e:
            print(f"  google news {sym}: {e}", file=sys.stderr)
    items.sort(key=lambda x: x["t"], reverse=True)
    seen, out = set(), []
    for it in items:
        k = it["title"].lower()[:60]
        if k not in seen:
            seen.add(k); out.append(it)
    return out


# ---------- fundamentals-lite ----------
def info_for(sym, ytk):
    out = {}
    try:
        inf = ytk.info or {}
        out.update({
            "name": inf.get("shortName") or inf.get("longName"),
            "sector": inf.get("sector"), "industry": inf.get("industry"),
            "marketCap": inf.get("marketCap"),
            "targetMean": inf.get("targetMeanPrice"),
            "targetHigh": inf.get("targetHighPrice"), "targetLow": inf.get("targetLowPrice"),
            "analysts": inf.get("numberOfAnalystOpinions"),
            "recKey": inf.get("recommendationKey"),
            "shortPctFloat": round(inf["shortPercentOfFloat"] * 100, 1) if inf.get("shortPercentOfFloat") else None,
            "preMarket": inf.get("preMarketPrice"),
            "fwdPE": round(inf["forwardPE"], 1) if inf.get("forwardPE") else None,
        })
        ts = inf.get("earningsTimestamp") or inf.get("earningsTimestampStart")
        if ts:
            out["earningsDate"] = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
    except Exception as e:
        print(f"  yahoo info {sym}: {e}", file=sys.stderr)
    if FINNHUB_KEY:
        try:
            rec = finnhub("stock/recommendation", symbol=sym)
            if rec:
                r = rec[0]
                out["recTrend"] = {k: r.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        except Exception as e:
            print(f"  finnhub rec {sym}: {e}", file=sys.stderr)
        if not out.get("earningsDate"):
            try:
                cal = finnhub("calendar/earnings", symbol=sym,
                              **{"from": now_utc().date().isoformat(),
                                 "to": (now_utc() + dt.timedelta(days=100)).date().isoformat()})
                ev = (cal or {}).get("earningsCalendar") or []
                if ev:
                    out["earningsDate"] = min(e["date"] for e in ev)
            except Exception as e:
                print(f"  finnhub cal {sym}: {e}", file=sys.stderr)
    return out


# ---------- materiality (the "stay silent unless" rules) ----------
def materiality(row, prev_target):
    reasons = []
    if row.get("chg1d") is not None and abs(row["chg1d"]) >= 2:
        reasons.append(f"move {row['chg1d']:+.1f}%")
    tm = row.get("targetMean")
    if tm and prev_target and abs(tm / prev_target - 1) >= 0.10:
        reasons.append(f"analyst target {prev_target:.0f}->{tm:.0f}")
    titles = " | ".join(n["title"].lower() for n in row.get("news", []))
    if any(w in titles for w in DEAL_WORDS):
        reasons.append("contract/deal headline")
    if any(w in titles for w in MAJOR_WORDS):
        reasons.append("major headline")
    return reasons


def mock_bars(seed):
    import pandas as pd
    random.seed(seed)
    p = random.uniform(20, 400)
    rows, idx = [], pd.bdate_range(end=dt.date.today(), periods=260)
    for _ in idx:
        p *= math.exp(random.gauss(0.0006, 0.025))
        hi, lo = p * (1 + random.uniform(0, .02)), p * (1 - random.uniform(0, .02))
        rows.append((p, hi, lo, p, random.uniform(1e6, 2e7)))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


def main():
    mock = "--mock" in sys.argv
    syms, phase_of = all_symbols()
    macro = CFG["macro"]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    prev_targets = state.get("targets", {})

    bars = {}
    if mock:
        bars = {s: mock_bars(s) for s in syms + list(macro)}
    else:
        import yfinance as yf
        df = None
        for attempt in range(3):                      # Yahoo sometimes rate-limits; retry before giving up
            try:
                df = yf.download(syms + list(macro), period="1y", interval="1d", group_by="ticker",
                                 auto_adjust=True, threads=False, progress=False)
                if df is not None and not df.empty:
                    break
            except Exception as e:
                print(f"  yahoo download attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(20 * (attempt + 1))
        for s in syms + list(macro):
            try:
                bars[s] = df[s]
            except Exception:
                try:                                  # per-ticker fallback
                    bars[s] = yf.Ticker(s).history(period="1y", auto_adjust=True)
                    time.sleep(0.5)
                except Exception:
                    print(f"  no bars for {s}", file=sys.stderr)

    out = {"asOf": now_utc().isoformat(timespec="seconds"), "source": "mock" if mock else "yahoo+finnhub" if FINNHUB_KEY else "yahoo",
           "macro": {}, "tickers": {}, "errors": []}

    for s, label in macro.items():
        m = metrics_from_bars(bars[s]) if s in bars else None
        if m:
            out["macro"][s] = {"label": label, "price": m["price"], "chg1d": m["chg1d"], "chg5d": m["chg5d"],
                               "chg1m": m["chg1m"], "sma50": m["sma50"], "sma200": m["sma200"]}

    spy5 = (out["macro"].get("SPY") or {}).get("chg1m")
    new_targets = {}
    for s in syms:
      print(s, file=sys.stderr)
      try:
        m = metrics_from_bars(bars[s]) if s in bars else None
        if not m:
            out["errors"].append(f"{s}: no price data")
            if FINNHUB_KEY and not mock:
                try:
                    q = finnhub("quote", symbol=s)
                    if q.get("c"):
                        m = {"price": q["c"], "prevClose": q.get("pc"), "chg1d": q.get("dp"), "spark": []}
                except Exception:
                    pass
            if not m:
                continue
        row = {"sym": s, "phase": phase_of[s], **m}
        if mock:
            row.update({"name": s + " Inc", "targetMean": round(m["price"] * random.uniform(.9, 1.3), 2),
                        "analysts": random.randint(5, 40),
                        "earningsDate": (dt.date.today() + dt.timedelta(days=random.randint(2, 70))).isoformat(),
                        "news": [{"t": now_utc().isoformat(), "title": f"[mock] {s} headline", "src": "mock", "url": ""}]})
        else:
            import yfinance as yf
            tk = yf.Ticker(s)
            row.update(info_for(s, tk))
            row["news"] = news_for(s, tk)
            time.sleep(0.4)
        row["newsCount"] = len(row.get("news", []))
        row["news"] = row.get("news", [])[:MAX_HEADLINES]
        if spy5 is not None and row.get("chg1m") is not None:
            row["rs1m"] = round(row["chg1m"] - spy5, 2)      # relative strength vs S&P, 1 month
        if row.get("targetMean"):
            new_targets[s] = row["targetMean"]
            row["targetPrev"] = prev_targets.get(s)
        row["material"] = materiality(row, prev_targets.get(s))
        out["tickers"][s] = row
      except Exception as e:
        import traceback; traceback.print_exc()
        out["errors"].append(f"{s}: {type(e).__name__}: {e}"[:200])

    # phase medians -> "behaviour within its group"
    for phase in set(phase_of.values()):
        vals = sorted(r["chg5d"] for r in out["tickers"].values() if r["phase"] == phase and r.get("chg5d") is not None)
        if vals:
            med = vals[len(vals) // 2]
            for r in out["tickers"].values():
                if r["phase"] == phase and r.get("chg5d") is not None:
                    r["vsPhase5d"] = round(r["chg5d"] - med, 2)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    if not mock:
        STATE.write_text(json.dumps({"targets": {**prev_targets, **new_targets}, "asOf": out["asOf"]}))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(out['tickers'])} tickers, {len(out['errors'])} errors)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
    # never fail the job just because some tickers errored; errors are listed inside snapshot.json
