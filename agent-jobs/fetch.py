#!/usr/bin/env python3
"""Markets and News fetch: public APIs and feeds -> raw JSON in feeds/ (no model, no pushes).

The only agent job with internet, and only through Squid to the hosts in squid.conf. It reads
nothing of yours (mail, tasks, agent.db) and decides nothing: it saves what each source said, and
the brief and the page pick what matters. Everything it saves is untrusted text, like email.

Usage:
  fetch.py                    # fetch every source, write feeds/<source>/<date>-<time>.json
  fetch.py --only rss gdelt   # just these sources (daily ones are fetched again even if done today)
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/London"))
FEEDS_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_DIR", "~/agent/data/feeds")))
# markets.yaml (watchlist) and news.yaml (feeds, GDELT topics), mounted read-only.
CONFIG_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_CONFIG", "~/agent/state/feeds")))
# Squid. A host missing from squid.conf fails with "403 Forbidden" in health.json.
PROXY = os.environ.get("HTTPS_PROXY", "")
TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "20"))
MAX_BYTES = int(os.environ.get("FETCH_MAX_MB", "5")) * 1_000_000
KEEP_DAYS = int(os.environ.get("FEEDS_KEEP_DAYS", "14"))
# Free API keys (Podman secrets). A source without its key is skipped and says so in health.json.
KEYS = {k: os.environ.get(k, "") for k in ("TIINGO_KEY", "FINNHUB_KEY", "FRED_KEY")}
UA = "agent-fetch/1 (personal dashboard)"
# Seconds between calls: GDELT asks for one request every 5 s; Finnhub allows 60 a minute.
GDELT_PAUSE = float(os.environ.get("GDELT_PAUSE", "6"))
FINNHUB_PAUSE = float(os.environ.get("FINNHUB_PAUSE", "1.1"))
MAX_ITEMS = 50  # per feed or symbol; the page shows far fewer
EQUITY_GROUPS = ("benchmarks", "themes", "sectors", "gold", "stocks")
PRICE_FIELDS = ("date", "close", "adjClose", "volume", "divCash", "splitFactor")
# Hosts the code itself calls; the feed hosts come from news.yaml. Both must be in squid.conf.
API_HOSTS = {"api.tiingo.com", "finnhub.io", "api.stlouisfed.org", "api.gdeltproject.org",
             "hacker-news.firebaseio.com"}
SECRET_HEADERS = {"authorization", "x-finnhub-token"}


class FetchError(Exception):
    pass


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to https://, and drops the key headers if the host changes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise FetchError(f"redirect to non-HTTPS refused ({code})")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        # req.host is the proxy's once a proxy is set, so compare the URLs themselves.
        host = urllib.parse.urlsplit(req.full_url).hostname
        if new is not None and urllib.parse.urlsplit(newurl).hostname != host:
            for k in [k for k in new.headers if k.lower() in SECRET_HEADERS]:
                del new.headers[k]
        return new


def opener(proxy=PROXY):
    # An empty ProxyHandler also ignores any *_proxy variables, so the proxy is only ever this one.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": proxy} if proxy else {}), HttpsOnly())


OPENER = opener()


def read_capped(r, limit=None):
    limit = limit or MAX_BYTES
    body = r.read(limit + 1)
    if len(body) > limit:
        raise FetchError(f"response over {limit // 1_000_000} MB")
    return body


def get(url, headers=None):
    if not url.startswith("https://"):
        raise FetchError("only https:// URLs")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with OPENER.open(req, timeout=TIMEOUT) as r:
        return read_capped(r)


def get_json(url, headers=None):
    body = get(url, headers)
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError("not JSON: " + body[:120].decode("utf-8", "replace").strip()) from None


def need(key):
    if not KEYS[key]:
        raise FetchError(f"no {key} (Podman secret {key.split('_')[0].lower()}-key)")
    return KEYS[key]


def scrub(s: str) -> str:
    for v in KEYS.values():
        if v:
            s = s.replace(v, "***")
    return s


def err(e) -> str:
    s = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {e}"
    return scrub(s)[:200]


def clean(s, n=500) -> str:
    """Feed text as plain text: tags removed, entities decoded, spaces squeezed."""
    s = re.sub(r"<[^>]+>", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or "")))
    return re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", s)).strip()[:n]


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_feed(body: bytes):
    """Items of an RSS 2.0, RSS 1.0 (RDF) or Atom feed: title, link, published, summary."""
    if b"<!ENTITY" in body:
        raise FetchError("feed declares XML entities; refused")
    items = []
    for el in ET.fromstring(body).iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f, link = {}, ""
        for c in el:
            name = local(c.tag)
            if name == "link" and not link:
                if c.get("rel") in (None, "alternate"):
                    link = (c.get("href") or c.text or "").strip()
            else:
                f.setdefault(name, "".join(c.itertext()))
        items.append({
            "title": clean(f.get("title"), 300),
            "link": link,
            "published": (f.get("pubDate") or f.get("published") or f.get("updated")
                          or f.get("date") or "").strip(),
            "summary": clean(f.get("description") or f.get("summary")),
        })
    return items[:MAX_ITEMS]


def collect(names, fetch_one):
    """Runs fetch_one(name) for each name; one failure doesn't stop the rest."""
    out, errors = {}, {}
    for n in names:
        try:
            out[n] = fetch_one(n)
        except Exception as e:
            errors[n] = err(e)
    return out, errors


def equities(markets):
    return [s for g in EQUITY_GROUPS for s in (markets.get(g) or {})]


SOURCES = {}  # name -> (function, once a day)


def source(name, daily=False):
    def wrap(f):
        SOURCES[name] = (f, daily)
        return f
    return wrap


@source("tiingo", daily=True)
def tiingo(markets, news, now):
    """Daily closes for every ETF and stock, about a year back, so rules can compare weeks."""
    key = need("TIINGO_KEY")
    symbols = equities(markets)
    if not symbols:
        raise FetchError("no symbols in markets.yaml")
    start = (now - timedelta(days=int(markets.get("history_days", 400)))).date().isoformat()

    def one(sym):
        rows = get_json(f"https://api.tiingo.com/tiingo/daily/{urllib.parse.quote(sym.lower())}"
                        f"/prices?startDate={start}", {"Authorization": f"Token {key}"})
        return [{k: r.get(k) for k in PRICE_FIELDS} for r in rows]
    return collect(symbols, one)


@source("crypto")
def crypto(markets, news, now):
    """Bitcoin and any other coins: daily candles; today's is still open, so its close is the latest price."""
    key = need("TIINGO_KEY")
    start = (now - timedelta(days=int(markets.get("history_days", 400)))).date().isoformat()

    def one(t):
        rows = get_json("https://api.tiingo.com/tiingo/crypto/prices?" + urllib.parse.urlencode(
            {"tickers": t, "startDate": start, "resampleFreq": "1day"}), {"Authorization": f"Token {key}"})
        return [{k: p.get(k) for k in ("date", "close", "volume")}
                for r in rows for p in r.get("priceData", [])]
    return collect(list(markets.get("crypto") or {}), one)


@source("earnings")
def earnings(markets, news, now):
    """Earnings per stock, a week back and four ahead: EPS and revenue, estimate and actual."""
    key = need("FINNHUB_KEY")
    a, b = (now - timedelta(days=7)).date(), (now + timedelta(days=28)).date()

    def one(sym):
        time.sleep(FINNHUB_PAUSE)
        return get_json(f"https://finnhub.io/api/v1/calendar/earnings?from={a}&to={b}"
                        f"&symbol={urllib.parse.quote(sym)}", {"X-Finnhub-Token": key}
                        ).get("earningsCalendar") or []
    return collect(list(markets.get("stocks") or {}), one)


@source("company_news")
def company_news(markets, news, now):
    """Headlines per stock since yesterday (Finnhub gathers them from many outlets)."""
    key = need("FINNHUB_KEY")
    a, b = (now - timedelta(days=1)).date(), now.date()

    def one(sym):
        time.sleep(FINNHUB_PAUSE)
        rows = get_json(f"https://finnhub.io/api/v1/company-news?symbol={urllib.parse.quote(sym)}"
                        f"&from={a}&to={b}", {"X-Finnhub-Token": key})
        return [{"headline": clean(r.get("headline"), 300), "source": r.get("source"),
                 "url": r.get("url"), "datetime": r.get("datetime"),
                 "summary": clean(r.get("summary"))} for r in rows[:MAX_ITEMS]]
    return collect(list(markets.get("stocks") or {}), one)


@source("releases", daily=True)
def releases(markets, news, now):
    """US macro release dates for the next five weeks (CPI, jobs, GDP, the Fed...), from FRED."""
    key = need("FRED_KEY")
    q = urllib.parse.urlencode({
        "api_key": key, "file_type": "json", "realtime_start": now.date().isoformat(),
        "realtime_end": (now + timedelta(days=35)).date().isoformat(),
        "include_release_dates_with_no_data": "true", "sort_order": "asc", "limit": 1000})
    rows = get_json("https://api.stlouisfed.org/fred/releases/dates?" + q).get("release_dates") or []
    return {"release_dates": rows}, {}


@source("gdelt")
def gdelt(markets, news, now):
    """Articles per topic from GDELT, in English, from the countries in news.yaml."""
    g = news.get("gdelt") or {}
    countries = " OR ".join(f"sourcecountry:{c}" for c in g.get("countries") or [])

    def one(topic):
        time.sleep(GDELT_PAUSE)
        query = f"{g['topics'][topic]} sourcelang:english" + (f" ({countries})" if countries else "")
        data = get_json("https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({
            "query": query, "mode": "artlist", "format": "json", "sort": "hybridrel",
            "maxrecords": g.get("max_records", 75), "timespan": g.get("timespan", "12h")}))
        return [{"title": clean(a.get("title"), 300), "url": a.get("url"), "domain": a.get("domain"),
                 "country": a.get("sourcecountry"), "seen": a.get("seendate")}
                for a in data.get("articles") or []]
    return collect(list(g.get("topics") or {}), one)


@source("rss")
def rss(markets, news, now):
    """Every feed in news.yaml, with its region and topic."""
    feeds = {f["name"]: f for f in news.get("feeds") or []}

    def one(name):
        f = feeds[name]
        return {"region": f.get("region"), "topic": f.get("topic"), "items": parse_feed(get(f["url"]))}
    return collect(list(feeds), one)


@source("hackernews")
def hackernews(markets, news, now):
    """Hacker News front page: what people in tech are reading."""
    n = int((news.get("hackernews") or {}).get("top", 30))
    ids = get_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:n]

    def one(i):
        it = get_json(f"https://hacker-news.firebaseio.com/v0/item/{int(i)}.json") or {}
        return {"title": clean(it.get("title"), 300), "score": it.get("score"), "time": it.get("time"),
                "comments": it.get("descendants"),
                "url": it.get("url") or f"https://news.ycombinator.com/item?id={int(i)}"}
    stories, errors = collect(ids, one)
    return {"stories": list(stories.values())}, errors


def count(data) -> int:
    """Rows in a source's data: lists are counted, dicts of lists summed."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        if "items" in data:
            return count(data["items"])
        return sum(count(v) for v in data.values())
    return 0


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(scrub(json.dumps(obj, ensure_ascii=False)), encoding="utf-8")
    tmp.replace(path)


def load_yaml(name):
    p = CONFIG_DIR / name
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def prune(now):
    """Removes dated files older than KEEP_DAYS (name starts with YYYY-MM-DD)."""
    cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    for p in FEEDS_DIR.glob("*/*.json"):
        if re.match(r"\d{4}-\d\d-\d\d", p.name) and p.name[:10] < cutoff:
            p.unlink()


def run(now, only=()):
    markets, news = load_yaml("markets.yaml"), load_yaml("news.yaml")
    hp = FEEDS_DIR / "health.json"
    try:
        health = json.loads(hp.read_text())["sources"]
    except (OSError, ValueError, KeyError):
        health = {}
    stamp, at, today = now.strftime("%Y-%m-%d-%H%M"), now.isoformat(timespec="seconds"), now.date().isoformat()
    for name, (fn, daily) in SOURCES.items():
        if only and name not in only:
            continue
        h = health.setdefault(name, {})
        if daily and not only and h.get("last_full", "")[:10] == today:
            print(f"{name}: already fetched today")
            continue
        started = time.monotonic()
        try:
            data, errors = fn(markets, news, now)
        except Exception as e:
            data, errors = None, {"": err(e)}
        n, secs = count(data), round(time.monotonic() - started, 1)
        h.update(last_run=at, ok=bool(n) or not errors, items=n, failed=len(errors),
                 error=next((f"{k}: {v}" if k else v for k, v in errors.items()), ""), seconds=secs)
        if data is not None:
            write_json(FEEDS_DIR / name / f"{stamp}.json",
                       {"source": name, "fetched": at, "data": data, "errors": errors})
            h["file"] = f"{name}/{stamp}.json"
        if h["ok"]:
            h["last_ok"] = at
            if not errors:
                h["last_full"] = at
        print(f"{name}: {'ok' if h['ok'] else 'FAILED'}, {n} items, {len(errors)} failed, {secs}s"
              + (f" ({h['error']})" if errors else ""))
    prune(now)
    write_json(hp, {"updated": at, "sources": health})
    return health


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="+", choices=list(SOURCES), default=())
    args = ap.parse_args()
    health = run(datetime.now(TZ), set(args.only))
    if not any(h.get("ok") for h in health.values()):
        sys.exit(1)  # nothing worked: the unit shows as failed


if __name__ == "__main__":
    main()
