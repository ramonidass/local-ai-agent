#!/usr/bin/env python3
"""Markets: the fetched feeds -> closes, rule hits and earnings in agent.db, and web/markets.json.

Runs right after each successful news-fetch, with no network at all. Code decides every hit from
the numbers; no model writes anything here, and nothing suggests buying or selling. Fetched
text (headlines) is untrusted: it is cut to length and only https:// links are kept.

Usage:
  markets.py           # update agent.db and web/markets.json
  markets.py --print   # print the quotes and the hits it would record, write nothing
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import agentdb

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/London"))
FEEDS_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_DIR", "~/agent/data/feeds")))
CONFIG_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_CONFIG", "~/agent/state/feeds")))
GROUPS = ("crypto", "benchmarks", "gold", "themes", "sectors", "stocks")  # Bitcoin first
TITLES = {"crypto": "Bitcoin", "benchmarks": "Benchmarks", "gold": "Gold", "themes": "Themes",
          "sectors": "Sectors", "stocks": "Stocks"}
EVAL_DAYS = 3        # at most this many new trading days are checked, so a missed run still counts
PAGE_HIT_DAYS = 14
DAY_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
# Rule defaults; markets.yaml "rules:" overrides them.
RULES = {"big_day": 2.5, "big_week": 2.0, "min_day_move": 1.5, "min_week_move": 3.0,
         "vs_market": 2.0, "min_vs_market": 2.0, "earnings_margin": 2.0,
         "trend_groups": ["benchmarks", "gold", "crypto"]}
COOLDOWN = {"big_week": 1, "high52": 2, "low52": 2, "trend200": 2, "vs_market": 1}  # weeks
VS_MARKET_GROUPS = ("sectors", "themes")   # compared with the S&P 500 (SPY)
OLD_RULES = ("sector_shift",)              # recorded by earlier versions; no longer shown


def num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def pct(v, digits=1):
    return "–" if v is None else f"{v * 100:+.{digits}f}%".replace("-", "−")


def money(v):
    if v is None:
        return "–"
    a = abs(v)
    s = (f"{a / 1e9:.1f}B" if a >= 1e9 else f"{a / 1e6:.1f}M" if a >= 1e6
         else f"{a:,.0f}" if a >= 1000 else f"{a:,.2f}")
    return ("−$" if v < 0 else "$") + s


def signed(v):
    return "–" if v is None else f"{v:.2f}".replace("-", "−")


def short_day(d) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%a} {d.day} {d:%b}"


def latest(source):
    """The newest saved run of a source, or None."""
    files = sorted((FEEDS_DIR / source).glob("*.json"))
    for p in reversed(files):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def load_config():
    p = CONFIG_DIR / "markets.yaml"
    m = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    m["rules"] = {**RULES, **(m.get("rules") or {})}
    return m


def names(markets):
    """{symbol: (group, label)}; stocks and sectors carry their ticker, e.g. "Energy (XLE)"."""
    out = {}
    for g in GROUPS:
        for sym, name in (markets.get(g) or {}).items():
            out[str(sym)] = (g, f"{name} ({sym})" if g in ("stocks", "sectors", "themes") else str(name))
    return out


def fetched_utc_day(feed):
    try:
        return datetime.fromisoformat(feed["fetched"]).astimezone(timezone.utc).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return "9999-12-31"


def price_rows(markets):
    """{symbol: [(day, close, adj)]} from the newest Tiingo files. Bitcoin's open day is left out
    (it is still moving) and returned separately as the live price."""
    rows, live = {}, {}
    eod = latest("tiingo") or {}
    for sym, rs in (eod.get("data") or {}).items():
        rows[sym] = [(r["date"][:10], c, num(r.get("adjClose")) or c) for r in rs
                     if isinstance(r, dict) and DAY_RE.match(str(r.get("date", ""))[:10])
                     and (c := num(r.get("close"))) is not None]
    cr = latest("crypto") or {}
    today = fetched_utc_day(cr)
    for sym, rs in (cr.get("data") or {}).items():
        good = [(r["date"][:10], c, c) for r in rs if isinstance(r, dict)
                and DAY_RE.match(str(r.get("date", ""))[:10]) and (c := num(r.get("close"))) is not None]
        rows[sym] = [r for r in good if r[0] < today]
        if good and good[-1][0] >= today:
            live[sym] = {"price": good[-1][1], "at": cr.get("fetched")}
    return rows, live


def series(conn, sym):
    return conn.execute("SELECT day, close, adj_close FROM market_daily WHERE symbol = ? ORDER BY day",
                        (sym,)).fetchall()


def ratio(s, i, back):
    return s[i][2] / s[i - back][2] - 1 if i - back >= 0 and s[i - back][2] else None


def first_new(s, last_seen):
    """Index of the first day to check: after last_seen, at most EVAL_DAYS back, never 0."""
    if last_seen is None:
        return max(1, len(s) - 1)
    k = next((n for n, r in enumerate(s) if r[0] > last_seen), len(s))
    return max(1, k, len(s) - EVAL_DAYS)


def periods(group):
    """(rows per week, rows per year): crypto trades every day."""
    return (7, 365) if group == "crypto" else (5, 252)


def month(group):
    return 30 if group == "crypto" else 21


def quote(s, group, live=None):
    """The numbers a tile shows: last close, changes over 1 day, 1 week, 1 and 3 months, 1 year."""
    if len(s) < 2:
        return None
    w, y = periods(group)
    i = len(s) - 1
    q = {"day": s[i][0], "close": s[i][1], "d1": ratio(s, i, 1), "w1": ratio(s, i, w),
         "m1": ratio(s, i, month(group)), "m3": ratio(s, i, 13 * w), "y1": ratio(s, i, y),
         "high52": max(r[1] for r in s[-y:]), "low52": min(r[1] for r in s[-y:]),
         # a year of closes for the page's small charts (adjusted, so they match the changes)
         "spark": [round(r[2], 2) for r in s[-y:]]}
    if live:  # Bitcoin: the live price against the last completed days
        p = live["price"]
        q.update(price=p, at=live["at"], d1=p / s[i][2] - 1, w1=p / s[i - w + 1][2] - 1 if i - w + 1 >= 0 else None)
        q["spark"].append(round(p, 2))
    return q


def day_hits(sym, group, label, s, i, cfg, bench=None):
    """Rule hits for one symbol on trading day s[i]: (rule, text, score). bench is the S&P 500 as
    {day: adjusted close}, given for the groups that are compared with it."""
    w, y = periods(group)
    out, close, day = [], s[i][1], short_day(s[i][0])
    if i >= 61:
        rets = [s[j][2] / s[j - 1][2] - 1 for j in range(i - 60, i)]
        sd, r = statistics.pstdev(rets), s[i][2] / s[i - 1][2] - 1
        if sd and abs(r) > cfg["big_day"] * sd and abs(r) >= cfg["min_day_move"] / 100:
            out.append(("big_day", f"{label} {pct(r)} on {day}, {abs(r) / sd:.1f}× its usual daily move", abs(r) / sd))
    weekly = [s[j][2] / s[j - w][2] - 1 for j in range(i - w, i - w - 52 * w, -w) if j - w >= 0]
    if len(weekly) >= 26:
        sd, r = statistics.pstdev(weekly), s[i][2] / s[i - w][2] - 1
        if sd and abs(r) > cfg["big_week"] * sd and abs(r) >= cfg["min_week_move"] / 100:
            out.append(("big_week", f"{label} {pct(r)} in a week, {abs(r) / sd:.1f}× its usual weekly move",
                        abs(r) / sd))
    if bench:
        def gap(j):  # this symbol's week ending s[j] against the S&P 500's same week
            a, b = bench.get(s[j][0]), bench.get(s[j - w][0])
            return (s[j][2] / s[j - w][2]) / (a / b) - 1 if a and b else None
        usual = [g for j in range(i - w, i - w - 52 * w, -w) if j - w >= 0 and (g := gap(j)) is not None]
        g = gap(i) if i - w >= 0 else None
        if g is not None and len(usual) >= 26:
            sd = statistics.pstdev(usual)
            if sd and abs(g) > cfg["vs_market"] * sd and abs(g) >= cfg["min_vs_market"] / 100:
                out.append(("vs_market", f"{label} {'beat' if g > 0 else 'trailed'} the S&P 500 by "
                                         f"{abs(g) * 100:.1f}% in a week, {abs(g) / sd:.1f}× its usual gap",
                            abs(g) / sd))
    if i >= y:
        before = [r[2] for r in s[i - y:i]]
        if s[i][2] > max(before):
            out.append(("high52", f"{label} at a 52-week high ({money(close)})", 2.0))
        elif s[i][2] < min(before):
            out.append(("low52", f"{label} at a 52-week low ({money(close)})", 2.0))
    if group in cfg["trend_groups"] and i >= 201:
        now = statistics.fmean(r[2] for r in s[i - 199:i + 1])
        then = statistics.fmean(r[2] for r in s[i - 200:i])
        above, was = s[i][2] > now, s[i - 1][2] > then
        if above != was:
            avg = now * s[i][1] / s[i][2]  # in plain prices, like the close shown
            out.append(("trend200", f"{label} crossed {'above' if above else 'below'} its 200-day average "
                                    f"({money(avg)})", 1.5))
    return out


def surprise(act, est):
    return (act - est) / abs(est) if act is not None and est else None


def verdict(eps_s, rev_s, margin):
    words = ["beat" if v >= margin else "missed" if v <= -margin else "in line"
             for v in (eps_s, rev_s) if v is not None]
    if not words:
        return None
    if "beat" in words and "missed" in words:
        return "mixed"
    return "beat" if "beat" in words else "missed" if "missed" in words else "in line"


def earnings_rows(markets):
    out = []
    for sym, rs in ((latest("earnings") or {}).get("data") or {}).items():
        for r in rs if isinstance(rs, list) else []:
            if isinstance(r, dict) and DAY_RE.match(str(r.get("date", ""))):
                out.append({"symbol": sym, "date": r["date"], "hour": str(r.get("hour") or "")[:3],
                            "quarter": r.get("quarter"), "year": r.get("year"),
                            "eps_est": num(r.get("epsEstimate")), "eps_act": num(r.get("epsActual")),
                            "rev_est": num(r.get("revenueEstimate")), "rev_act": num(r.get("revenueActual"))})
    return out


def earnings_text(e, label, margin):
    es, rs = surprise(e["eps_act"], e["eps_est"]), surprise(e["rev_act"], e["rev_est"])
    v = verdict(es, rs, margin / 100)
    parts = []
    if e["rev_act"] is not None:
        parts.append(f"revenue {money(e['rev_act'])} vs {money(e['rev_est'])} expected ({pct(rs)})")
    if e["eps_act"] is not None:
        parts.append(f"EPS {signed(e['eps_act'])} vs {signed(e['eps_est'])} ({pct(es)})"
                     if es is not None else f"EPS {signed(e['eps_act'])}")
    return v, f"{label} {v or 'reported'}: " + ", ".join(parts)


def releases(markets, today: date, days=45):
    """US releases from FRED (names in markets.yaml) and the central-bank dates in markets.yaml."""
    wanted = markets.get("releases") or {}
    if isinstance(wanted, list):
        wanted = {n: n for n in wanted}
    labels = {str(k).lower(): str(v) for k, v in wanted.items()}
    end = (today + timedelta(days=days)).isoformat()
    fred = defaultdict(set)
    for r in ((latest("releases") or {}).get("data") or {}).get("release_dates") or []:
        name = str(r.get("release_name", "")).lower()
        if name in labels and DAY_RE.match(str(r.get("date", ""))) and today.isoformat() <= r["date"] <= end:
            fred[labels[name]].add(r["date"])
    # a name FRED lists on many days is a data update, not an event (its "FOMC Press Release" is daily)
    rows = {(d, name) for name, ds in fred.items() if len(ds) <= days // 14 for d in ds}
    for bank, dates in (markets.get("central_banks") or {}).items():
        rows |= {(str(d), f"{bank} rate decision") for d in dates or []}
    return [{"date": d, "name": n} for d, n in sorted(rows) if today.isoformat() <= d <= end]


def headlines(markets, per=5):
    """Company news per stock: plain text, https links only, newest first."""
    out = {}
    for sym, rs in ((latest("company_news") or {}).get("data") or {}).items():
        rows = [r for r in rs if isinstance(r, dict) and isinstance(r.get("datetime"), (int, float))]
        rows.sort(key=lambda r: r["datetime"], reverse=True)
        out[sym] = [{"headline": str(r.get("headline") or "")[:300], "source": str(r.get("source") or "")[:60],
                     "url": u if (u := str(r.get("url") or "")).startswith("https://") else "",
                     "at": datetime.fromtimestamp(r["datetime"], timezone.utc).isoformat(timespec="seconds")}
                    for r in rows[:per] if r.get("headline")]
    return out


def run(now=None, write=True):
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    markets = load_config()
    cfg, labels = markets["rules"], names(markets)
    rows, live = price_rows(markets)
    conn = agentdb.connect() if write else _memory_db()
    try:
        with conn:
            # rules look only at trading days that arrived since the last run (the very first run:
            # the last day only, so a year of history doesn't turn into a flood of alerts)
            before = dict(conn.execute("SELECT symbol, MAX(day) FROM market_daily GROUP BY symbol"))
            for sym, rs in rows.items():
                conn.executemany("INSERT OR REPLACE INTO market_daily VALUES (?, ?, ?, ?)",
                                 [(d, sym, c, a) for d, c, a in rs])
            earn = earnings_rows(markets)
            conn.executemany("INSERT OR REPLACE INTO earnings VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [(e["symbol"], e["date"], e["hour"], e["quarter"], e["year"], e["eps_est"],
                               e["eps_act"], e["rev_est"], e["rev_act"], stamp) for e in earn])
            seen = defaultdict(set)
            for d, rule, sym in conn.execute("SELECT day, rule, symbol FROM market_hits"):
                seen[(rule, sym)].add(d)
            new = []

            def hit(day, rule, sym, text, score):
                if day in seen[(rule, sym)]:
                    return
                seen[(rule, sym)].add(day)
                conn.execute("INSERT OR IGNORE INTO market_hits VALUES (?,?,?,?,?,?)",
                             (day, rule, sym, text, round(score, 2), stamp))
                new.append((day, rule, sym, text))

            quotes, all_series = {}, {}
            bench = {d: a for d, _, a in series(conn, "SPY")}
            for sym, (group, label) in labels.items():
                s = series(conn, sym)
                all_series[sym] = s
                quotes[sym] = quote(s, group, live.get(sym))
                w = periods(group)[0]
                for i in range(first_new(s, before.get(sym)), len(s)):
                    for rule, text, score in day_hits(sym, group, label, s, i, cfg,
                                                      bench if group in VS_MARKET_GROUPS else None):
                        back = [r[0] for r in s[max(0, i - COOLDOWN.get(rule, 0) * w):i]]
                        if not any(d in seen[(rule, sym)] for d in back):
                            hit(s[i][0], rule, sym, text, score)
            recent = (now.date() - timedelta(days=10)).isoformat()
            for e in earn:
                if e["date"] < recent or (e["eps_act"] is None and e["rev_act"] is None) or e["symbol"] not in labels:
                    continue
                label = labels[e["symbol"]][1]
                v, text = earnings_text(e, label, cfg["earnings_margin"])
                hit(e["date"], "earnings", e["symbol"], text, 10)
                s = all_series.get(e["symbol"]) or []
                k = next((n for n, r in enumerate(s) if (r[0] > e["date"] if e["hour"] == "amc" else r[0] >= e["date"])), None)
                if k:  # the first trading day after the news, against the close before it
                    move = s[k][2] / s[k - 1][2] - 1
                    hit(s[k][0], "earnings_move", e["symbol"],
                        f"{label} {pct(move)} on {short_day(s[k][0])}, after results that {v or 'came out'}", 9)
        page = build_page(conn, markets, labels, quotes, now)
        if write:
            agentdb._atomic_write_text(agentdb.WEB_DIR / "markets.json", json.dumps(page, ensure_ascii=False))
    finally:
        conn.close()
    return page, new


def build_page(conn, markets, labels, quotes, now):
    since = (now.date() - timedelta(days=PAGE_HIT_DAYS)).isoformat()
    hits = [{"day": d, "rule": r, "symbol": s, "name": labels.get(s, ("", s))[1], "text": t, "score": sc,
             "first_seen": f}
            for d, r, s, t, sc, f in conn.execute(
                "SELECT day, rule, symbol, text, score, first_seen FROM market_hits WHERE day >= ? "
                "ORDER BY day DESC, score DESC", (since,)) if r not in OLD_RULES]
    lo, hi = (now.date() - timedelta(days=7)).isoformat(), (now.date() + timedelta(days=28)).isoformat()
    margin = markets["rules"]["earnings_margin"]
    earnings = []
    for sym, d, hour, est, act, rest, ract in conn.execute(
            "SELECT symbol, date, hour, eps_est, eps_act, rev_est, rev_act FROM earnings "
            "WHERE date BETWEEN ? AND ? ORDER BY date, symbol", (lo, hi)):
        if sym not in labels:
            continue
        e = {"symbol": sym, "name": labels[sym][1], "date": d, "hour": hour, "eps_est": est, "eps_act": act,
             "rev_est": rest, "rev_act": ract}
        e["verdict"] = verdict(surprise(act, est), surprise(ract, rest), margin / 100)
        earnings.append(e)
    groups = [{"key": g, "title": TITLES[g],
               "items": [{"symbol": s, "name": str((markets.get(g) or {})[s]), **quotes[s]}
                         for s in (markets.get(g) or {}) if quotes.get(s)]}
              for g in GROUPS]
    us = max((q["day"] for s, q in quotes.items() if q and labels[s][0] != "crypto"), default=None)
    try:
        health = json.loads((FEEDS_DIR / "health.json").read_text())
    except (OSError, ValueError):
        health = {}
    return {"generated": agentdb._iso(now), "us_close": us, "groups": [g for g in groups if g["items"]],
            "hits": hits, "earnings": earnings,
            "releases": releases(markets, now.astimezone(TZ).date()), "headlines": headlines(markets),
            "fetch": {"updated": health.get("updated"),
                      "failed": sorted(k for k, v in (health.get("sources") or {}).items() if not v.get("ok"))}}


def _memory_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.executescript(agentdb.SCHEMA)
    if agentdb.enabled() and Path(agentdb.DB_PATH).exists():
        src = sqlite3.connect(agentdb.DB_PATH)
        src.backup(conn)
        src.close()
    return conn


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print", action="store_true", help="print only; write nothing")
    args = ap.parse_args()
    if not args.print and not agentdb.enabled():
        sys.exit("AGENT_DB is not set")
    page, new = run(write=not args.print)
    for g in page["groups"]:
        print(f"{g['title']}: " + ", ".join(f"{i['symbol']} {pct(i['d1'])}" for i in g["items"]))
    print(f"US close: {page['us_close']}; {len(new)} new hit(s)")
    for day, rule, sym, text in new:
        print(f"  {day} {rule}: {text}")


if __name__ == "__main__":
    main()
