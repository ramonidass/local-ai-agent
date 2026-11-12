"""markets.py and the brief's Markets section: rules fire once, Bitcoin first, coming up, warnings."""
import json, os, random, sqlite3, sys, tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

d = Path(tempfile.mkdtemp())
for sub in ("cal", "time", "extra", "config"):
    (d / sub).mkdir()
os.environ.update(CAL_DIR=str(d / "cal"), EXTRA_ICS_DIR=str(d / "extra"), TIME_DIR=str(d / "time"),
                  TODO_FILES=str(d / "To-do.md"), EMAIL_DIGEST=str(d / "none.jsonl"), OLLAMA_URL="",
                  AGENT_DB=str(d / "data" / "agent.db"), AGENT_WEB_DIR=str(d / "data" / "web"),
                  AGENT_STATS_DIR=str(d / "data" / "stats"), FEEDS_DIR=str(d / "feeds"),
                  FEEDS_CONFIG=str(d / "config"), MARKETS_JSON=str(d / "data" / "web" / "markets.json"),
                  FEEDS_HEALTH=str(d / "feeds" / "health.json"))
here = Path(__file__).parent
sys.path[:0] = [str(here), str(here.parent / "agent-jobs")]
import agentdb  # noqa
import brief  # noqa
import markets  # noqa

(d / "config" / "markets.yaml").write_text("""
benchmarks: {SPY: S&P 500}
themes: {SMH: Semiconductors}
sectors: {XLE: Energy, XLK: Technology, XLU: Utilities, XLF: Financials, XLV: Health care,
          XLI: Industrials, XLB: Materials}
gold: {GLD: Gold}
stocks: {NVDA: Nvidia, RKLB: Rocket Lab}
crypto: {btcusd: Bitcoin}
releases: {Consumer Price Index: US CPI, FOMC Press Release: Fed rate decision}
central_banks: {Bank of England: [2026-11-12, 2027-02-04]}
""")

# ---- 400 trading days up to Tue 10 Nov 2026, each symbol a quiet random walk, plus set-ups:
days, x = [], date(2026, 11, 10)
while len(days) < 400:
    if x.weekday() < 5:
        days.append(x.isoformat())
    x -= timedelta(days=1)
days.reverse()
rnd = random.Random(7)


def walk(start=100.0, sd=0.01, drift=0.0):
    p, out = start, []
    for _ in days:
        p *= 1 + rnd.gauss(drift, sd)
        out.append(p)
    return out


px = {s: walk() for s in ("SMH", "XLK", "XLF", "XLV", "XLI", "XLB", "GLD", "RKLB")}
px["SPY"] = [100.0] * 340 + [100 - 5 * k / 59 for k in range(59)] + [101.5]  # back above its 200-day average
px["XLE"] = walk()
for k in range(-5, 0):
    px["XLE"][k] = px["XLE"][-6] * (1 - 0.03 * (k + 6))                       # a hard week ending −15%
px["NVDA"] = walk(drift=0.002)
px["NVDA"][-1] = max(px["NVDA"]) * 1.03                                        # a new 52-week high
px["XLU"] = walk(drift=-0.003)
for k in range(-5, 0):
    px["XLU"][k] = px["XLU"][-6] * (1 + 0.06 * (k + 6))                       # last to first in a week
px["RKLB"][-1] = px["RKLB"][-2] * 1.12                                         # reaction to results


def tiingo(sym):
    return [{"date": f"{dy}T00:00:00.000Z", "close": round(p, 4), "adjClose": round(p, 4), "volume": 1}
            for dy, p in zip(days, px[sym])]


btc = []
for k in range(401):
    btc.append({"date": f"{(date(2026, 11, 11) - timedelta(days=400 - k)).isoformat()}T00:00:00+00:00",
                "close": 70000 + 10 * k, "volume": 1})
btc[-1]["close"] = 75000                                                       # today's open candle: live price


def save(source, data, stamp="2026-11-11-0735"):
    p = d / "feeds" / source / f"{stamp}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"source": source, "fetched": "2026-11-11T07:35:10+00:00", "data": data, "errors": {}}))


save("tiingo", {s: tiingo(s) for s in px})
save("crypto", {"btcusd": btc})
save("earnings", {"RKLB": [{"symbol": "RKLB", "date": "2026-11-09", "hour": "amc", "epsEstimate": -0.0714,
                            "epsActual": -0.07, "revenueEstimate": 260822761, "revenueActual": 267829999}],
                  "NVDA": [{"symbol": "NVDA", "date": "2026-11-12", "hour": "amc", "epsEstimate": 2.5263,
                            "epsActual": None, "revenueEstimate": 111356225808, "revenueActual": None}]})
save("releases", {"release_dates": [{"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-11-13"},
                                    {"release_id": 1, "release_name": "Some Other Release", "date": "2026-11-12"}]
                  # FRED lists this one every day: a data update, not an event
                  + [{"release_id": 101, "release_name": "FOMC Press Release", "date": f"2026-11-{n}"}
                     for n in (11, 12, 13, 16, 17)]})
save("company_news", {"NVDA": [{"headline": "Old", "source": "A", "url": "https://a.example/1", "datetime": 1},
                               {"headline": "New", "source": "B", "url": "http://b.example/2", "datetime": 2}]})
(d / "feeds" / "health.json").write_text(json.dumps({"updated": "2026-11-11T07:35:40+00:00", "sources": {
    "tiingo": {"ok": True}, "rss": {"ok": False, "error": "OSError: Tunnel connection failed: 403 Forbidden"}}}))

# 1) first run: each set-up fires once; Bitcoin's open day isn't stored but is its live price
run1 = datetime(2026, 11, 11, 7, 36, tzinfo=timezone.utc)
page, new = markets.run(run1)
got = {(rule, sym) for _, rule, sym, _ in new}
for want in [("big_day", "SPY"), ("trend200", "SPY"), ("big_week", "XLE"), ("high52", "NVDA"),
             ("vs_market", "XLU"), ("vs_market", "XLE"), ("earnings", "RKLB"), ("earnings_move", "RKLB")]:
    assert want in got, (want, sorted(got))
text = {(r, s): t for _, r, s, t in new}
assert text[("earnings", "RKLB")] == ("Rocket Lab (RKLB) beat: revenue $267.8M vs $260.8M expected (+2.7%), "
                                      "EPS −0.07 vs −0.07 (+2.0%)"), text[("earnings", "RKLB")]
assert text[("earnings_move", "RKLB")] == "Rocket Lab (RKLB) +12.0% on Tue 10 Nov, after results that beat"
assert text[("trend200", "SPY")].startswith("S&P 500 crossed above its 200-day average ($")
assert text[("vs_market", "XLU")].startswith("Utilities (XLU) beat the S&P 500 by "), text[("vs_market", "XLU")]
assert text[("vs_market", "XLE")].startswith("Energy (XLE) trailed the S&P 500 by "), text[("vs_market", "XLE")]
assert text[("vs_market", "XLU")].endswith("× its usual gap") and ("vs_market", "SPY") not in got
# tiny moves are never "big", however calm the series (Bitcoin here climbs $10 a day)
assert not any(r in ("big_day", "big_week") and s == "btcusd" for r, s in got), got
# the first run looks at the last day only
assert {dy for dy, *_ in new} <= {"2026-11-09", "2026-11-10"}, {dy for dy, *_ in new}
db = sqlite3.connect(os.environ["AGENT_DB"])
assert db.execute("SELECT MAX(day), COUNT(*) FROM market_daily WHERE symbol = 'btcusd'").fetchone() == ("2026-11-10", 400)
assert db.execute("SELECT COUNT(*) FROM market_daily WHERE symbol = 'SPY'").fetchone()[0] == 400
assert db.execute("SELECT eps_act, rev_est FROM earnings WHERE symbol = 'RKLB'").fetchone() == (-0.07, 260822761)
m = json.loads((d / "data/web/markets.json").read_text())
assert [g["key"] for g in m["groups"]] == ["crypto", "benchmarks", "gold", "themes", "sectors", "stocks"]
b = m["groups"][0]["items"][0]
assert b["symbol"] == "btcusd" and b["price"] == 75000 and b["close"] == 73990, b
assert round(b["d1"], 4) == round(75000 / 73990 - 1, 4)
assert m["us_close"] == "2026-11-10" and "sectors" not in m
assert {"d1", "w1", "m1", "m3", "y1", "spark", "high52", "low52"} <= set(b)
assert m["releases"] == [{"date": "2026-11-12", "name": "Bank of England rate decision"},
                         {"date": "2026-11-13", "name": "US CPI"}], m["releases"]
assert m["headlines"]["NVDA"][0]["headline"] == "New" and m["headlines"]["NVDA"][0]["url"] == ""  # http dropped
assert m["headlines"]["NVDA"][1]["url"] == "https://a.example/1"
assert m["fetch"]["failed"] == ["rss"]
nv = next(e for e in m["earnings"] if e["symbol"] == "NVDA")
assert nv["verdict"] is None and next(e for e in m["earnings"] if e["symbol"] == "RKLB")["verdict"] == "beat"
print("RULES OK")

# 2) the same data again: nothing new; --print style run writes nothing
_, again = markets.run(run1 + timedelta(hours=10))
assert again == [], again
before = (d / "data/web/markets.json").read_text()
markets.run(run1 + timedelta(hours=11), write=False)
assert (d / "data/web/markets.json").read_text() == before
print("ONCE OK")

# 3) next day: another hard XLE day is a new big_day, but its big week is still cooling down
days.append("2026-11-11")
for s in px:
    px[s].append(px[s][-1])
px["XLE"][-1] = px["XLE"][-2] * 0.90
save("tiingo", {s: tiingo(s) for s in px}, "2026-11-12-0735")
_, new = markets.run(datetime(2026, 11, 12, 7, 36, tzinfo=timezone.utc))
got = {(r, s) for _, r, s, _ in new}
assert ("big_day", "XLE") in got and ("big_week", "XLE") not in got, got
print("COOLDOWN OK")

# 4) the brief: short. Bitcoin first, the US close, the biggest moves, sectors over the week
m = json.loads((d / "data/web/markets.json").read_text())
now = datetime(2026, 11, 12, 7, 55, tzinfo=brief.TZ)
lines, facts, n = brief.markets_section(m, now, datetime(2026, 11, 11, 7, 55, tzinfo=timezone.utc))
assert lines[0] == "## Markets" and lines[1].startswith("- **Bitcoin** $75,000 ▲ 1.4% · 7 days ▲ "), lines[1]
assert lines[2] == "- **US close Wed 11 Nov** · S&P 500 ■ 0.0% · Gold ■ 0.0%", lines[2]
assert lines[3].startswith("- **Biggest moves** · "), lines[3]
assert lines[4].startswith("- **Sectors this week** · best Utilities ▲ ") and " · worst Energy ▼ " in lines[4], lines[4]
assert lines[4].endswith(" · S&P 500 ▲ 6.5%"), lines[4]
assert n == 1 and "### New alerts (1)" in lines and "- Energy (XLE) −10.0% on Wed 11 Nov, 8.0× its usual daily move" in lines
assert "- Thu 12 Nov: Nvidia (NVDA) reports after the close · EPS 2.53 and revenue $111.4B expected" in lines
assert "- Thu 12 Nov: Bank of England rate decision" in lines and "- Fri 13 Nov: US CPI" in lines
assert not any("Fed rate decision" in l for l in lines)            # listed daily by FRED: dropped
assert "### Last week" not in lines and not any("Old" in l or "New" == l for l in lines)  # no headlines
assert facts[0] == "Markets: Bitcoin $75,000, up 1.4% since yesterday." and any(f.startswith("MARKET: ") for f in facts)
assert len([l for l in lines if l.startswith("- ")]) <= 4 + brief.MARKET_ALERTS_SHOWN + 1 + brief.MARKET_COMING_SHOWN + 1
# nothing new since this morning's run: no alerts heading, the rest stays
lines2, _, n2 = brief.markets_section(m, now, datetime(2026, 11, 12, 7, 50, tzinfo=timezone.utc))
assert n2 == 0 and not any(l.startswith("### New alerts") for l in lines2)
# Monday: last week, and coming up covers the whole week
monday = datetime(2026, 11, 9, 7, 55, tzinfo=brief.TZ)
lines3, _, _ = brief.markets_section(m, monday, monday)
assert "### Last week" in lines3 and "### Coming up this week" in lines3
assert any(l.startswith("- Sectors against the S&P 500: ahead Utilities ▲ ") for l in lines3), lines3
print("SECTION OK")

# 5) warnings: a failed source, a stale fetch, old prices
w = brief.feeds_warnings(Path(os.environ["FEEDS_HEALTH"]), m, now)
assert w == ["Markets and News fetch: rss failed (OSError: Tunnel connection failed: 403 Forbidden)."], w
w = brief.feeds_warnings(Path(os.environ["FEEDS_HEALTH"]), {"us_close": "2026-11-03"}, now + timedelta(days=2))
assert any("last fetched" in x for x in w) and any("Market prices are from Tue 3 Nov" in x for x in w), w
assert brief.feeds_warnings(d / "nope.json", {}, now) == ["Markets and News fetch has not run yet (news-fetch)."]
print("WARNINGS OK")

# 6) the whole brief: Markets card after the other sections, "market alerts" in the headline
(d / "To-do.md").write_text("## Inbox\n- [ ] Something\n")
agentdb.record_brief("2026-11-11", "# yesterday")
with sqlite3.connect(os.environ["AGENT_DB"]) as c:
    c.execute("UPDATE briefs SET built = '2026-11-11T07:55:00+00:00' WHERE day = '2026-11-11'")
text, counts = brief.build(now, consume_email=False)
assert "\n## Markets\n- **Bitcoin** " in text and counts["markets"] == n, counts
assert brief.plural(n, "market alert") in brief.headline(counts)
assert "Markets and News fetch: rss failed" in text
assert "MARKET: " in brief.build.facts_text
html = agentdb.md_to_html(text)
assert "<h3>Markets</h3>" in html and "<h4>New alerts" in html
print("BRIEF OK")

# 7) alerts: one line per symbol; results carry the share-price reaction, other hits name the rest
rk = brief.market_alerts([h for h in m["hits"] if h["symbol"] == "RKLB"])
assert rk == ["Rocket Lab (RKLB) beat: revenue $267.8M vs $260.8M expected (+2.7%), EPS −0.07 vs −0.07 (+2.0%)"
              " · shares +12.0% on Tue 10 Nov"], rk
nv = brief.market_alerts([h for h in m["hits"] if h["symbol"] == "NVDA"])
assert len(nv) == 1 and nv[0].endswith("(also: 52-week high, big week)"), nv
xu = brief.market_alerts([h for h in m["hits"] if h["symbol"] == "XLU"])
assert len(xu) == 1 and "against the S&P 500" in xu[0], xu
print("ALERTS OK")

# 8) hits of rules that no longer exist are kept in agent.db but not shown
with sqlite3.connect(os.environ["AGENT_DB"]) as c:
    c.execute("INSERT INTO market_hits VALUES ('2026-11-10', 'sector_shift', 'XLK', 'old ranking text', 2.5, '2026-11-10T22:43:57+00:00')")
page, _ = markets.run(datetime(2026, 11, 12, 18, 1, tzinfo=timezone.utc))
assert not any(h["rule"] == "sector_shift" for h in page["hits"]) and page["hits"]
print("OLD RULES OK")
