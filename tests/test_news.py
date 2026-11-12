"""news.py: the same story from several outlets becomes one story; old, linkless and repeated items go."""
import json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

d = Path(tempfile.mkdtemp())
(d / "config").mkdir()
os.environ.update(FEEDS_DIR=str(d / "feeds"), FEEDS_CONFIG=str(d / "config"), AGENT_WEB_DIR=str(d / "web"))
here = Path(__file__).parent
sys.path[:0] = [str(here), str(here.parent / "agent-jobs")]
import news  # noqa

(d / "config" / "news.yaml").write_text("""
feeds:
  - {name: BBC Europe, outlet: BBC, region: Europe, topic: Politics, url: "https://a.example/1"}
  - {name: DW, region: Europe, topic: World, url: "https://b.example/1"}
  - {name: BBC World, outlet: BBC, region: World, topic: World, url: "https://a.example/2"}
  - {name: Fox Politics, outlet: Fox News, region: US, topic: Politics, url: "https://c.example/1"}
  - {name: Ars Technica, region: World, topic: Tech, url: "https://e.example/1"}
""")


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

# 3) sections: topic feeds by topic, world feeds as World, the rest by region; single stories newest first
assert st["Google Fi adds a second 5G network"]["section"] == "Tech"
assert st["DR Congo sentences soldiers"]["section"] == "World"
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
