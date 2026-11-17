"""news.py: the same story from several outlets becomes one story; old, linkless and repeated items go."""
import json, os, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

d = Path(tempfile.mkdtemp())
(d / "config").mkdir()
os.environ.update(FEEDS_DIR=str(d / "feeds"), FEEDS_CONFIG=str(d / "config"), AGENT_WEB_DIR=str(d / "web"),
                  AGENT_DB="")              # news.py never touches agent.db
here = Path(__file__).parent
sys.path[:0] = [str(here), str(here.parent / "agent-jobs")]
import news  # noqa

FEEDS = """
feeds:
  - {name: BBC Europe, outlet: BBC, region: Europe, topic: Politics, url: "https://a.example/1"}
  - {name: DW, region: Europe, topic: World, url: "https://b.example/1"}
  - {name: BBC World, outlet: BBC, region: World, topic: World, url: "https://a.example/2"}
  - {name: Fox Politics, outlet: Fox News, region: US, topic: Politics, url: "https://c.example/1"}
  - {name: Ars Technica, region: World, topic: Tech, url: "https://e.example/1"}
"""
TOPICS = """
topics:
  Politics:
    include: [GOP leader, war, soldiers, police chief]
    exclude: [Congo]
  Tech and AI:
    include: [5G, artificial intelligence]
  Space:
    include: [want]             # only in a headline the story doesn't show
    exclude: []
  Economy:
    include: [Google]
"""
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS)


def item(title, link, published="Tue, 10 Nov 2026 18:00:00 GMT", summary="s"):
    return {"title": title, "link": link, "published": published, "summary": summary}


rss = {
    "BBC Europe": {"region": "Europe", "topic": "Politics", "items": [
        item("US, Russia and Ukraine start talks in Abu Dhabi", "https://bbc.example/talks"),
        item("French police raid hotel", "https://bbc.example/raid", "Tue, 10 Nov 2026 20:00:00 GMT"),
        item("Too old to show", "https://bbc.example/old", "Sun, 08 Nov 2026 06:00:00 GMT"),
        item("No link kept", "http://bbc.example/plain"),
        item("No date", "https://bbc.example/nodate", "")]},
    "DW": {"region": "Europe", "topic": "World", "items": [
        item("US-mediated talks on ending Russia-Ukraine war begin in Abu Dhabi", "https://dw.example/talks",
             "2026-11-10T19:00:00Z"),
        item("DR Congo sentences soldiers", "https://dw.example/congo")]},
    "BBC World": {"region": "World", "topic": "World", "items": [
        item("US, Russia and Ukraine start talks in Abu Dhabi", "https://bbc.example/talks"),   # the same link again
        item("Ukraine talks in Abu Dhabi: what Russia and the US want", "https://bbc.example/explainer")]},
    "Fox Politics": {"region": "US", "topic": "Politics", "items": [
        item("Jim Jordan picked as next House GOP leader", "https://fox.example/jordan"),
        item("<b>Markup</b> stays text", "https://fox.example/markup")]},
    "Ars Technica": {"region": "World", "topic": "Tech", "items": [
        item("Google Fi adds a second 5G network", "https://ars.example/fi")]},
}
(d / "feeds" / "rss").mkdir(parents=True)
(d / "feeds" / "rss" / "2026-11-10-2100.json").write_text(json.dumps(
    {"source": "rss", "fetched": "2026-11-10T21:00:00+00:00", "data": rss, "errors": {"Sifted": "x"}}))
(d / "feeds" / "hackernews").mkdir()
(d / "feeds" / "hackernews" / "2026-11-10-2100.json").write_text(json.dumps({"data": {"stories": [
    {"title": "Low", "score": 5, "comments": 1, "url": "https://h.example/1"},
    {"title": "High", "score": 500, "comments": 80, "url": "https://h.example/2"},
    {"title": "Script link", "score": 900, "url": "javascript:alert(1)"}]}}))
(d / "feeds" / "health.json").write_text(json.dumps({"updated": "2026-11-10T21:00:40+00:00", "sources": {
    "rss": {"ok": True}, "tiingo": {"ok": False}}}))

now = datetime(2026, 11, 10, 21, 5, tzinfo=timezone.utc)
page = news.build(now)
st = {s["title"]: s for s in page["stories"]}

# 1) one story, two outlets: BBC leads (listed first), each outlet once, the newest time
talks = page["stories"][0]
assert talks["title"] == "US, Russia and Ukraine start talks in Abu Dhabi" and talks["outlets"] == ["BBC", "DW"], talks
assert [i["url"] for i in talks["items"]] == ["https://bbc.example/talks", "https://dw.example/talks"]
assert talks["at"] == "2026-11-10T19:00:00+00:00" and talks["section"] == "Europe"
print("GROUPING OK")

# 2) dropped: older than 36 hours, http links, no date, the same link twice
assert not {"Too old to show", "No link kept", "No date"} & set(st), list(st)
assert sum(len(s["items"]) for s in page["stories"]) == 7 and len(page["stories"]) == 6, list(st)
print("FILTER OK")

# 3) sections are the feeds' regions only (the subject is topics:); single stories newest first
assert page["sections"] == ["Europe", "US", "World"]
assert st["Google Fi adds a second 5G network"]["section"] == "World"         # Ars Technica: region World
assert st["DR Congo sentences soldiers"]["section"] == "Europe"              # DW: region Europe (topic World)
assert talks["feeds"] == ["BBC Europe", "BBC World", "DW"]   # every feed it came from (the explainer too)
assert st["Jim Jordan picked as next House GOP leader"]["section"] == "US"
assert [s["title"] for s in page["stories"]][1] == "French police raid hotel"
assert page["outlets"] == ["Ars Technica", "BBC", "DW", "Fox News"] and page["failed_feeds"] == ["Sifted"]
assert st["<b>Markup</b> stays text"]["title"] == "<b>Markup</b> stays text"   # the page shows it as text
print("SECTIONS OK")

# 4) Hacker News by score, https links only; fetch health for the news sources only
assert [h["title"] for h in page["hn"]] == ["High", "Low"], page["hn"]
assert page["fetch"] == {"updated": "2026-11-10T21:00:40+00:00", "failed": []}
print("HN OK")

# 5) headline words: small words out, plurals folded; unrelated headlines stay apart
assert news.words("The tariffs on Chinese chips") == {"tariff", "chinese", "chip"}
assert not news.same_story(news.words("Jim Jordan picked as next House GOP leader"),
                           news.words("House set to vote on spending bill as GOP leader warns"))
assert news.parse_time("2026-11-10T06:00:00Z") == datetime(2026, 11, 10, 6, tzinfo=timezone.utc)
assert news.parse_time("nonsense") is None
print("WORDS OK")

# 6) topics: a headline with an include keyword and no exclude keyword; a story can have several
st = {s["title"]: s for s in news.build(now)["stories"]}
assert page["topics"] == ["Politics", "Tech and AI", "Space", "Economy"]
assert st["Jim Jordan picked as next House GOP leader"]["topics"] == ["Politics"]        # "GOP leader"
assert st["US, Russia and Ukraine start talks in Abu Dhabi"]["topics"] == ["Politics"]   # "war": DW's headline
assert st["Google Fi adds a second 5G network"]["topics"] == ["Tech and AI", "Economy"]  # several, in order
assert st["DR Congo sentences soldiers"]["topics"] == []          # "soldiers", but excluded by "Congo"
assert st["French police raid hotel"]["topics"] == []             # "police" without "chief"
assert st["<b>Markup</b> stays text"]["topics"] == []
(d / "config" / "news.yaml").write_text(FEEDS)                    # no topics: block, no topics
page2 = news.build(now)
assert page2["topics"] == [] and all(s["topics"] == [] for s in page2["stories"])
(d / "config" / "news.yaml").write_text(FEEDS + "topics:\n")
assert news.build(now)["topics"] == []
# phrases in order, plurals folded, small words counted; short words whole, in capitals as written
for key, headline, found in [
        ("Bank of England", "Bank of England holds rates", True),
        ("Bank of England", "England's banks face test", False),
        ("Bank of England", "Banks of England", True), ("Bank of England", "Bank in England", False),
        ("interest rates", "Interest rate cut", True), ("interest rates", "Rates of interest", False),
        ("GOP leader", "House GOP leader", True), ("House leader", "House GOP leader", False),
        ("AI", "Nvidia's AI chips", True), ("AI", "Spain said no", False), ("AI", "Ai Weiwei show", False),
        ("ai", "Ai Weiwei show", True), ("US", "US-mediated talks", True), ("US", "Trump tells us", False),
        ("US", "USA win", False), ("us", "Trump tells us", True), ("U.S.", "U.S. Senate votes", True),
        ("EU", "EU's new rules", True), ("G7", "G7 leaders meet", True), ("5G", "a 5g network", False),
        ("war", "Film wins award", False), ("tariffs", "Tariff row", True), ("Musk's", "Musk quits", True)]:
    assert news.has_phrase(news.tokens(headline), news.keyword(key, "test")) == found, (key, headline)
# topics see the same words as words() (and so as grouping), with nothing dropped
for h in [i["title"] for f in rss.values() for i in f["items"]] + ["The tariffs on Chinese chips", "Musk’s xAI",
                                                                   "Bus, gas and glass prices rise", "A 5G mast"]:
    assert {w for w, _ in news.tokens(h) if len(w) >= 3 and w not in news.STOP} == news.words(h), h
print("TOPICS OK")

# 6b) feeds: every story from a listed feed is in the topic, keywords or not (and excludes don't apply)
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS.replace(
    "    include: [want]             # only in a headline the story doesn't show\n    exclude: []",
    "    include: [want]\n    exclude: [network]\n    feeds: [Ars Technica, DW]"))
st = {s["title"]: s for s in news.build(now)["stories"]}
assert st["Google Fi adds a second 5G network"]["topics"] == ["Tech and AI", "Space", "Economy"]   # despite "network"
assert st["DR Congo sentences soldiers"]["topics"] == ["Space"]
assert st["US, Russia and Ukraine start talks in Abu Dhabi"]["topics"] == ["Politics", "Space"]   # DW is one of its feeds
assert st["Jim Jordan picked as next House GOP leader"]["topics"] == ["Politics"]
assert news.topics_of([], {"T": ((), (), frozenset({"DW"}))}, ["BBC Europe", "DW"]) == ["T"]
# topics_by_feed: those a story has only through feeds:, no keyword (not enough for the candidates)
assert st["DR Congo sentences soldiers"]["topics_by_feed"] == ["Space"]
assert st["US, Russia and Ukraine start talks in Abu Dhabi"]["topics_by_feed"] == ["Space"]   # Politics by keyword
assert st["Jim Jordan picked as next House GOP leader"]["topics_by_feed"] == []
# hackernews: topic: names the chip that also shows Hacker News; none by default
assert news.build(now)["hn_topic"] is None
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS + "hackernews: {top: 30, topic: Space}\n")
assert news.build(now)["hn_topic"] == "Space"
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS)
print("TOPIC FEEDS OK")

# 7) a block that can't be used stops the run with a message saying where, not a traceback
bad = {
    "topics: [Politics, Space]": "topics: expected topic names",
    "topics: {Politics: [election]}": "topics: Politics: expected include: and exclude: lists",
    "topics: {Politics: {includes: [election]}}": "topics: Politics: unknown includes",
    "topics: {Politics: {exclude: [football]}}": "topics: Politics: include: expected a list with at least one",
    "topics: {Politics: {include: []}}": "topics: Politics: include: expected a list with at least one",
    "topics: {Politics: {include: election}}": "topics: Politics: include: expected a list",
    "topics: {Politics: {include: [election], exclude: football}}": "topics: Politics: exclude: expected a list",
    "topics: {Politics: {include: [election], feeds: DW}}": "topics: Politics: feeds: expected a list of feed names",
    "topics: {Politics: {include: [election], feeds: [Reuters]}}":
        "topics: Politics: feeds: 'Reuters' is not the name of a feed in feeds:",
    "hackernews: {top: 30, topic: Sport}": "hackernews: topic: 'Sport' is not one of the topics:",
    "topics: {Politics: {include: [election, '...']}}": "topics: Politics: include: '...' can never match",
    "topics: {Politics: {include: [election], exclude: ['  ']}}": "topics: Politics: exclude: '  ' is not a keyword",
    "topics: {Politics: {include: [2026]}}": "topics: Politics: include: 2026 is not a keyword",
    "topics: {Politics: {include: [election]}, 7: {include: [rocket]}}": "topics: 7 is not a topic name",
    "topics: {Politics: {include: [election": "news.yaml is not valid YAML",
    "candidates: [40]": "candidates: expected max:, per_outlet: and weights:",
    "candidates: {max: 40, per_outlets: 3}": "candidates: unknown per_outlets",
    "candidates: {max: 0}": "candidates: max: expected a whole number from 1, not 0",
    "candidates: {per_outlet: 2.5}": "candidates: per_outlet: expected a whole number from 1, not 2.5",
    "candidates: {max: yes}": "candidates: max: expected a whole number from 1, not True",
    "candidates: {weights: [1, 0.5]}": "candidates: weights: expected outlets: and fresh: numbers",
    "candidates: {weights: {outlet: 1}}": "candidates: weights: unknown outlet",
    "candidates: {weights: {fresh: -1}}": "candidates: weights: fresh: expected a number from 0, not -1",
    "candidates: {weights: {fresh: high}}": "candidates: weights: fresh: expected a number from 0, not 'high'",
    "candidates: {weights: {fresh: .inf}}": "candidates: weights: fresh: expected a number from 0, not inf",
    "candidates: {sections: [Europe]}": "candidates: sections: expected a number for any of Europe, US, World",
    "candidates: {sections: {Asia: 1}}": "candidates: sections: 'Asia' is not a section (Europe, US, World)",
    "candidates: {sections: {Europe: -0.5}}": "candidates: sections: Europe: expected a number from 0, not -0.5",
    "candidates: {sections: {Europe: yes}}": "candidates: sections: Europe: expected a number from 0, not True",
    "candidates: {colour: red}": "candidates: unknown colour (only max:, per_outlet:, no_topic_outlets:, "
                                 "weights:, sections:)",
    "candidates: {no_topic_outlets: 1}": "candidates: no_topic_outlets: expected a whole number from 2, not 1",
    "candidates: {no_topic_outlets: 2.5}": "candidates: no_topic_outlets: expected a whole number from 2, not 2.5",
}
for block, message in bad.items():
    (d / "config" / "news.yaml").write_text(FEEDS + block + "\n")
    try:
        news.build(now)
        raise AssertionError(f"no error for {block}")
    except news.ConfigError as e:
        assert message in str(e), (block, str(e))
# the job itself: exit 1, the message on stderr, no traceback, nothing written
(d / "config" / "news.yaml").write_text(FEEDS + "topics: {Politics: {include: [election, '—']}}\n")
run = subprocess.run([sys.executable, str(here.parent / "agent-jobs" / "news.py")], capture_output=True, text=True,
                     env={**os.environ, "PYTHONPATH": str(here.parent / "agent-jobs")})
assert run.returncode == 1 and "Traceback" not in run.stderr, run.stderr
assert run.stderr.strip() == ("news.yaml topics: Politics: include: '—' can never match: "
                              "it has no letters or digits"), run.stderr
assert not (d / "web" / "news.json").exists()
print("BAD CONFIG OK")

# 8) the real news.yaml loads, and its topics tag what they should (and not the figurative uses)
news.CONFIG_DIR = here.parent / "state-feeds"
real = news.topics_config()
assert news.candidates_config() == {"max": 40, "per_outlet": 8, "no_topic_outlets": 2,
                                   "weights": {"outlets": 1.0, "fresh": 0.5}, "sections": {"Europe": 0.25}}
for headline, tagged in [
        ("Rocket attack hits Kharkiv apartment block", ["War and security"]),
        ("SpaceX rocket reaches orbit", ["Space"]), ("Price war hits French supermarkets", []),
        ("Spain floods: thousands evacuated", ["Disasters and climate"]),
        ("Flood of complaints over new parking rules", []),
        ("Storm Amy brings 90mph winds", ["Disasters and climate"]),
        ("Minister caught in political storm over expenses", ["Politics"]),
        ("Labour wins by a landslide", ["Politics"]),                     # Labour, but no disaster
        ("Fed officials split on December rate cut", ["Economy"]), ("Fed chair warns on jobs", ["Economy"]),
        ("Fed up farmers block Paris roads", []), ("Bank of England holds interest rates", ["Economy"]),
        ("EU leaders agree AI rules", ["Politics", "Tech and AI"]), ("Trump tells us he will not run again", [])]:
    got = news.topics_of([news.tokens(headline)], real)
    assert got == tagged, (headline, got)
# the specialist feeds give their topic without a keyword; the page's Hacker News chip is a real topic
assert news.topics_of([news.tokens("Launch schedule slips again")], real, ["SpaceNews"]) == ["Space"]
assert news.topics_of([news.tokens("A new handheld console")], real, ["Ars Technica"]) == ["Tech and AI"]
assert news.topics_of([news.tokens("Shares in retailer jump")], real, ["BBC Business"]) == ["Economy"]
assert news.topics_of([news.tokens("Shares in retailer jump")], real, ["BBC Europe"]) == []
assert news.hn_topic(real) == "Tech and AI"
print("REAL CONFIG OK")
