"""fetch.py: every source saved as raw JSON, one failure isolated, keys never written, limits held."""
import io, json, os, re, sys, tempfile, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

d = Path(tempfile.mkdtemp())
KEYS = dict(TIINGO_KEY="tiingo-SECRET-1", FINNHUB_KEY="finnhub-SECRET-2", FRED_KEY="fred-SECRET-3")
os.environ.update(FEEDS_DIR=str(d / "feeds"), FEEDS_CONFIG=str(d / "config"), GDELT_PAUSE="0",
                  FINNHUB_PAUSE="0", HTTPS_PROXY="http://egress-proxy:3128", **KEYS)
here = Path(__file__).parent
repo = here.parent  # works from tests/ and when copied next to the scripts in agent-jobs/
sys.path[:0] = [str(here), str(repo / "agent-jobs")]
import fetch  # noqa

TZ = fetch.TZ
(d / "config").mkdir()
(d / "config" / "markets.yaml").write_text("""
benchmarks: {SPY: S&P 500}
stocks: {NVDA: Nvidia, MSFT: Microsoft}
crypto: {btcusd: Bitcoin}
""")
(d / "config" / "news.yaml").write_text("""
feeds:
  - {name: A, region: Europe, topic: Politics, url: "https://a.example/rss"}
  - {name: B, region: World, topic: Tech, url: "https://b.example/atom"}
  - {name: C, region: Europe, topic: World, url: "https://c.example/rdf"}
  - {name: D, region: US, topic: Politics, url: "https://d.example/down"}
gdelt:
  countries: [unitedkingdom, unitedstates]
  topics: {Space: '(NASA OR SpaceX)'}
hackernews: {top: 2}
""")

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>EU summit &amp; tariffs</title><link>https://a.example/1</link>
<pubDate>Tue, 10 Nov 2026 07:00:00 GMT</pubDate>
<description>&lt;p&gt;Leaders &lt;b&gt;met&lt;/b&gt;.&lt;/p&gt;</description></item></channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Chip news</title>
<link rel="self" href="https://b.example/self"/><link rel="alternate" href="https://b.example/1"/>
<updated>2026-11-10T06:00:00Z</updated><summary type="html">&lt;i&gt;New&lt;/i&gt; fab</summary></entry></feed>"""
RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>DW</title><items><rdf:Seq/></items></channel>
<item><title>Berlin vote</title><link>https://c.example/2</link><dc:date>2026-11-10T05:00:00Z</dc:date>
<description>Coalition talks</description></item></rdf:RDF>"""
calls = []


def fake(url, headers=None):
    calls.append((url, dict(headers or {})))
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(u.query)
    j = lambda o: json.dumps(o).encode()  # noqa
    if u.hostname == "api.tiingo.com" and "/daily/" in u.path:
        if "/msft/" in u.path:
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        return j([{"date": "2026-11-09T00:00:00.000Z", "close": 100.5, "adjClose": 100.5, "volume": 7,
                   "divCash": 0, "splitFactor": 1, "open": 99}])
    if u.hostname == "api.tiingo.com":
        return j([{"ticker": "btcusd", "priceData": [{"date": "2026-11-10T00:00:00Z", "close": 76000,
                                                      "volume": 5, "open": 1}]}])
    if u.path.endswith("/calendar/earnings"):
        return j({"earningsCalendar": [{"symbol": q["symbol"][0], "date": "2026-11-19", "hour": "amc",
                                        "epsEstimate": 1.2, "epsActual": None,
                                        "revenueEstimate": 5e10, "revenueActual": None}]})
    if u.path.endswith("/company-news"):
        return j([{"headline": "<b>Big</b> deal", "source": "Wire", "url": "https://w.example/1",
                   "datetime": 1, "summary": "x", "image": "drop me"}])
    if u.hostname == "api.stlouisfed.org":
        return j({"release_dates": [{"release_id": 10, "release_name": "Consumer Price Index",
                                     "date": "2026-11-13"}]})
    if u.hostname == "api.gdeltproject.org":
        return j({"articles": [{"title": "Launch", "url": "https://n.example/1", "domain": "bbc.co.uk",
                                "sourcecountry": "United Kingdom", "seendate": "20261110T060000Z"}]})
    if u.path.endswith("topstories.json"):
        return j([11, 12])
    if "/item/" in u.path:
        return j({"title": "Show HN", "score": 50, "url": "https://s.example"} if "11" in u.path
                 else {"title": "Ask HN", "score": 5})
    if u.hostname == "d.example":  # an error message that carries a key must not reach the files
        raise urllib.error.URLError(f"tunnel failed ?token={KEYS['TIINGO_KEY']}")
    return {"a.example": RSS, "b.example": ATOM, "c.example": RDF}[u.hostname]


real_get = fetch.get
fetch.get = fake


def written():
    return b"".join(p.read_bytes() for p in (d / "feeds").rglob("*") if p.is_file())


# 1) morning run: every source saved; one bad symbol and one dead feed don't stop the rest
h = fetch.run(datetime(2026, 11, 10, 7, 35, tzinfo=TZ))
assert set(h) == set(fetch.SOURCES), h
assert all(v["ok"] for v in h.values()), h
assert h["tiingo"]["items"] == 2 and h["tiingo"]["failed"] == 1, h["tiingo"]
assert h["tiingo"]["error"] == "MSFT: HTTPError: HTTP Error 500: Server Error", h["tiingo"]["error"]
assert h["rss"]["items"] == 3 and h["rss"]["failed"] == 1 and "***" in h["rss"]["error"], h["rss"]
assert h["releases"]["last_full"].startswith("2026-11-10") and "last_full" not in h["tiingo"]
for name in fetch.SOURCES:
    assert (d / "feeds" / name / "2026-11-10-0735.json").exists(), name
rss = json.loads((d / "feeds/rss/2026-11-10-0735.json").read_text())["data"]
assert rss["A"]["items"][0] == {"title": "EU summit & tariffs", "link": "https://a.example/1",
                                "published": "Tue, 10 Nov 2026 07:00:00 GMT",
                                "summary": "Leaders met."}, rss["A"]
assert rss["B"]["items"][0]["link"] == "https://b.example/1" and rss["B"]["items"][0]["summary"] == "New fab"
assert rss["C"]["items"][0]["title"] == "Berlin vote" and rss["C"]["region"] == "Europe"
news = json.loads((d / "feeds/company_news/2026-11-10-0735.json").read_text())["data"]
assert news["NVDA"][0]["headline"] == "Big deal" and "image" not in news["NVDA"][0]
hn = json.loads((d / "feeds/hackernews/2026-11-10-0735.json").read_text())["data"]["stories"]
assert hn[1]["url"] == "https://news.ycombinator.com/item?id=12", hn
health = json.loads((d / "feeds/health.json").read_text())
assert health["updated"] == "2026-11-10T07:35:00+00:00" and health["sources"]["gdelt"]["items"] == 1
print("SOURCES OK")

# 2) keys travel in headers (FRED only takes a query key) and are never written to a file
for url, hd in calls:
    host = urllib.parse.urlsplit(url).hostname
    if host == "api.tiingo.com":
        assert hd == {"Authorization": "Token tiingo-SECRET-1"} and "SECRET" not in url
    if host == "finnhub.io":
        assert hd == {"X-Finnhub-Token": "finnhub-SECRET-2"} and "SECRET" not in url
    if host == "api.stlouisfed.org":
        assert "api_key=fred-SECRET-3" in url and not hd
    if host not in ("api.tiingo.com", "finnhub.io"):
        assert not hd, (url, hd)
g = next(u for u, _ in calls if "gdeltproject" in u)
gq = urllib.parse.parse_qs(urllib.parse.urlsplit(g).query)["query"][0]
assert gq == "(NASA OR SpaceX) sourcelang:english (sourcecountry:unitedkingdom OR sourcecountry:unitedstates)", gq
blob = written()
assert all(k.encode() not in blob for k in KEYS.values())
print("KEYS OK")

# 3) evening run: daily sources wait for tomorrow, unless the morning had a failure (tiingo did)
calls.clear()
fetch.run(datetime(2026, 11, 10, 18, 0, tzinfo=TZ))
hosts = [urllib.parse.urlsplit(u).hostname for u, _ in calls]
assert "api.stlouisfed.org" not in hosts and any("/daily/" in u for u, _ in calls), hosts
assert not (d / "feeds/releases/2026-11-10-1800.json").exists()
# --only fetches again even if done today
calls.clear()
fetch.run(datetime(2026, 11, 10, 18, 5, tzinfo=TZ), {"releases"})
assert [urllib.parse.urlsplit(u).hostname for u, _ in calls] == ["api.stlouisfed.org"]
print("DAILY OK")

# 4) a missing key: that source fails with a clear message, the others are untouched
fetch.KEYS["FRED_KEY"] = ""
h = fetch.run(datetime(2026, 11, 10, 18, 10, tzinfo=TZ), {"releases"})
assert not h["releases"]["ok"] and h["releases"]["error"] == "no FRED_KEY (Podman secret fred-key)"
assert h["releases"]["last_ok"] == "2026-11-10T18:05:00+00:00" and h["gdelt"]["ok"]
fetch.KEYS["FRED_KEY"] = KEYS["FRED_KEY"]
print("MISSING KEY OK")

# 5) old dated files go after 14 days; others stay
old = d / "feeds/rss/2026-10-20-0735.json"
old.write_text("{}")
keep = d / "feeds/rss/2026-10-28-0735.json"
keep.write_text("{}")
fetch.run(datetime(2026, 11, 10, 18, 15, tzinfo=TZ), {"gdelt"})
assert not old.exists() and keep.exists() and (d / "feeds/health.json").exists()
print("PRUNE OK")

# 6) limits: https only, size cap, entities refused
fetch.get = real_get
for url in ("http://feeds.bbci.co.uk/news/rss.xml", "ftp://x.example/"):
    try:
        fetch.get(url)
        raise AssertionError(url)
    except fetch.FetchError as e:
        assert "https" in str(e)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeOpener:
    def __init__(self, body):
        self.body, self.seen = body, []

    def open(self, req, timeout=None):
        self.seen.append((req.full_url, timeout))
        return Resp(self.body)


fetch.OPENER = FakeOpener(b"x" * (fetch.MAX_BYTES + 1))
try:
    fetch.get("https://feeds.bbci.co.uk/big")
    raise AssertionError("no cap")
except fetch.FetchError as e:
    assert str(e) == "response over 5 MB", e
fetch.OPENER = FakeOpener(b"y" * fetch.MAX_BYTES)
assert len(fetch.get("https://feeds.bbci.co.uk/ok")) == fetch.MAX_BYTES
assert fetch.OPENER.seen == [("https://feeds.bbci.co.uk/ok", 20.0)]
try:
    fetch.parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><item><title>&a;</title></item></rss>')
    raise AssertionError("entity")
except fetch.FetchError:
    pass
print("LIMITS OK")

# 7) the opener only knows Squid, and redirects stay on HTTPS without carrying keys elsewhere
# An empty ProxyHandler isn't even kept, and it stops build_opener adding the one that reads *_proxy.
proxies = lambda o: next((h.proxies for h in o.handlers if isinstance(h, urllib.request.ProxyHandler)), {})  # noqa
os.environ["https_proxy"] = os.environ["http_proxy"] = "http://elsewhere:9"
assert proxies(fetch.opener("http://egress-proxy:3128")) == {"https": "http://egress-proxy:3128"}
assert proxies(fetch.opener("")) == {}
red = fetch.HttpsOnly()
req = urllib.request.Request("https://api.tiingo.com/a", headers={"Authorization": "Token k", "User-Agent": "u"})
req.set_proxy("egress-proxy:3128", "https")
assert red.redirect_request(req, None, 302, "Found", {}, "https://api.tiingo.com/b").headers["Authorization"] == "Token k"
moved = red.redirect_request(req, None, 302, "Found", {}, "https://cdn.example/b")
assert "Authorization" not in moved.headers and moved.headers["User-agent"] == "u", moved.headers
try:
    red.redirect_request(req, None, 301, "Moved", {}, "http://api.tiingo.com/b")
    raise AssertionError("http redirect")
except fetch.FetchError:
    pass
print("PROXY OK")

# 8) Squid lets the fetch network reach exactly the hosts fetch.py and news.yaml use
import yaml  # noqa

squid = (repo / "egress-proxy/squid.conf").read_text()
allowed = {h for line in squid.splitlines() if line.startswith("acl fetch_hosts dstdomain")
           for h in line.split()[3:]}
feeds = yaml.safe_load((repo / "state-feeds/news.yaml").read_text())["feeds"]
needed = fetch.API_HOSTS | {urllib.parse.urlsplit(f["url"]).hostname for f in feeds}
assert allowed == needed, (allowed - needed, needed - allowed)
assert all(f["url"].startswith("https://") for f in feeds)
subnet = re.search(r"^Subnet=(\S+)", (repo / "quadlets/fetch.network").read_text(), re.M).group(1)
assert f"acl fetch_net src {subnet}" in squid
assert "http_access allow CONNECT proton !fetch_net" in squid
assert "http_access allow CONNECT fetch_net fetch_hosts" in squid
unit = (repo / "quadlets/news-fetch.container").read_text()
assert re.findall(r"^Network=(\S+)", unit, re.M) == ["fetch.network"]
markets = yaml.safe_load((repo / "state-feeds/markets.yaml").read_text())
assert len(fetch.equities(markets)) == 26 and list(markets["crypto"]) == ["btcusd"], fetch.equities(markets)
print("ALLOWLIST OK")
