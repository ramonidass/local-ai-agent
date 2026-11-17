"""news.py candidates: stories with a topic, most-covered then newest, outlets spread, a score, always the same."""
import itertools, json, os, random, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

d = Path(tempfile.mkdtemp())
(d / "config").mkdir()
os.environ.update(FEEDS_DIR=str(d / "feeds"), FEEDS_CONFIG=str(d / "config"), AGENT_WEB_DIR=str(d / "web"),
                  AGENT_DB="")              # news.py never touches agent.db
here = Path(__file__).parent
sys.path[:0] = [str(here), str(here.parent / "agent-jobs")]
import news  # noqa


def story(url, outlets, at, topics=("War",)):
    return {"title": url, "url": f"https://{url}.example/", "outlets": list(outlets), "topics": list(topics),
            "at": f"2026-11-10T{at}:00+00:00"}


def cfg(**k):
    return {**news.CANDIDATES, **k, "weights": {**news.CANDIDATES["weights"], **k.get("weights", {})}}


FETCHED = datetime(2026, 11, 11, tzinfo=timezone.utc)   # the stories are from the day before


def titles(found, c=None):
    return [s["title"] for s in news.candidates(found, c or cfg(), FETCHED)]


# 1) more outlets first, however old; the same outlets newest first; no topic, no candidate
found = [story("one-new", ["BBC"], "20:00"), story("two-old", ["BBC", "DW"], "10:00"),
         story("three-oldest", ["DW", "BBC", "Fox News"], "00:01"), story("two-new", ["DW", "BBC"], "12:00"),
         story("untagged", ["BBC", "DW", "Fox News", "NPR"], "21:00", topics=())]
assert titles(found) == ["three-oldest", "two-new", "two-old", "one-new"], titles(found)
# the weights: fresh 6 is a point per 6 hours, so 20 hours newer beats two more outlets; outlets 0: newest first
assert titles(found, cfg(weights={"fresh": 6})) == ["one-new", "two-new", "two-old", "three-oldest"]
assert titles(found, cfg(weights={"outlets": 0})) == ["one-new", "two-new", "two-old", "three-oldest"]
print("RANKING OK")

# 2) per_outlet led by one outlet (its first) come in score order, those over it fill the room left
#    at the end, also in score order; at most max in all
found = [story(f"bbc-{i}", ["BBC", "DW"], f"1{i}:00") for i in range(5)] + \
        [story("dw-led", ["DW", "BBC"], "09:00"), story("fox", ["Fox News"], "23:00")]
assert titles(found, cfg(per_outlet=2)) == ["bbc-4", "bbc-3", "dw-led", "fox", "bbc-2", "bbc-1", "bbc-0"]
assert titles(found, cfg(per_outlet=2, max=5)) == ["bbc-4", "bbc-3", "dw-led", "fox", "bbc-2"]
assert titles(found, cfg(per_outlet=2, max=3)) == ["bbc-4", "bbc-3", "dw-led"]     # no room: the spread wins
assert titles(found, cfg(per_outlet=1, max=1)) == ["bbc-4"]
assert titles(found) == ["bbc-4", "bbc-3", "bbc-2", "bbc-1", "bbc-0", "dw-led", "fox"]   # 8 per outlet
many = [story(f"s{i:02}", [f"Outlet {i % 9}"] + ["X"] * (i % 3), f"{i % 24:02}:{i % 60:02}") for i in range(90)]
assert len(news.candidates(many, cfg(), FETCHED)) == 40
got = news.candidates(many, cfg(per_outlet=3), FETCHED)
spread, over = got[:27], got[27:]                  # 9 outlets x 3, then 13 from over the cap
leads = [s["outlets"][0] for s in spread]
assert len(got) == 40 and max(leads.count(o) for o in leads) == 3
assert all(sum(t["outlets"][0] == s["outlets"][0] for t in spread) == 3 for s in over)   # only over-the-cap ones
assert [s["score"] for s in spread] == sorted((s["score"] for s in spread), reverse=True)
assert [s["score"] for s in over] == sorted((s["score"] for s in over), reverse=True)
assert over[0]["score"] > spread[-1]["score"]      # at the end although it scores higher
assert len(news.candidates(many[:4], cfg(), FETCHED)) == 4
print("CAP OK")

# 3) the score: weights.outlets per outlet + weights.fresh x freshness (1 at fetch time, 0 at 36 hours)
found = [story("three", ["BBC", "DW", "NPR"], "18:00"), story("one", ["BBC"], "00:00")]
got = news.candidates(found, cfg(), FETCHED)
assert [(s["title"], s["score"]) for s in got] == [("three", 3.4167), ("one", 1.1667)]   # 3 + .5x30/36, 1 + .5x12/36
got = news.candidates(found, cfg(weights={"outlets": 0.1, "fresh": 0.3}), FETCHED)
assert [s["score"] for s in got] == [0.55, 0.2]
assert "score" not in found[0]                     # the stories themselves stay as they are
print("SCORE OK")

# 4) ties: the same score and time go by link; input order never matters
found = [story("b", ["BBC"], "12:00"), story("a", ["BBC"], "12:00"), story("c", ["BBC"], "12:00"),
         story("older", ["BBC", "DW"], "00:00"), story("newer", ["BBC"], "18:00")]
for order in itertools.permutations(found):
    assert titles(list(order)) == ["older", "newer", "a", "b", "c"]
    assert titles(list(order), cfg(weights={"fresh": 2})) == ["newer", "older", "a", "b", "c"]   # see below
# an exact tie between one more outlet and being newer goes to the newer (by link it would be "tie-earlier"),
# also with weights floats can't hold exactly: 0.3 + 0.6 x 18/36 hours ties with 2 x 0.3 only in fractions
for weights, later in (({"fresh": 2}, "19:00"), ({"outlets": 0.3, "fresh": 0.6}, "19:00"),
                       ({"outlets": 0.1, "fresh": 0.3}, "13:00")):
    tie = [story("tie-earlier", ["BBC", "DW"], "01:00"), story("tie-later", ["BBC"], later)]
    for order in (tie, tie[::-1]):
        assert titles(order, cfg(weights=weights)) == ["tie-later", "tie-earlier"], weights
rnd = random.Random(7)
for _ in range(50):
    rnd.shuffle(many)
    assert titles(many, cfg(per_outlet=3)) == titles(sorted(many, key=lambda s: s["url"]), cfg(per_outlet=3))
print("TIES OK")

# 5) the job: news.yaml sets the cap; candidates are an extra list beside the sections, which
#    keep their own limit; two runs (different hash seeds, different moments) write the same list
(d / "config" / "news.yaml").write_text("""
feeds:
  - {name: BBC Europe, outlet: BBC, region: Europe, topic: Politics, url: "https://a.example/1"}
  - {name: DW, region: Europe, topic: World, url: "https://b.example/1"}
  - {name: Guardian Europe, outlet: The Guardian, region: Europe, topic: Politics, url: "https://c.example/1"}
  - {name: Ars Technica, region: World, topic: Tech, url: "https://e.example/1"}
topics:
  War: {include: [ceasefire, drone strike, troops]}
  Tech and AI: {include: [AI]}
candidates:
  max: 5
  per_outlet: 2
  weights: {outlets: 1, fresh: 0.5}
""")
now = datetime.now(timezone.utc).replace(microsecond=0)


def item(title, link, hours):
    return {"title": title, "link": link, "published": (now - timedelta(hours=hours)).isoformat(), "summary": ""}


rss = {
    "BBC Europe": {"items": [item("Ukraine ceasefire talks open in Geneva", "https://bbc.example/1", 3),
                             item("Drone strike hits Odesa port overnight", "https://bbc.example/2", 5),
                             item("Troops leave border region after deal", "https://bbc.example/3", 2),
                             item("Bakery wins national bread prize", "https://bbc.example/4", 1)]},
    "DW": {"items": [item("Geneva hosts Ukraine ceasefire talks", "https://dw.example/1", 4),
                     item("Odesa port drone strike overnight", "https://dw.example/2", 6),
                     item("Berlin troops deployment debated in parliament", "https://dw.example/3", 8)]},
    "Guardian Europe": {"items": [
        item("Ukraine ceasefire talks open in Geneva amid doubt", "https://gu.example/1", 2)]},
    "Ars Technica": {"items": [item("New AI model tops coding benchmark", "https://ars.example/1", 1),
                               item("Why AI chips run hot", "https://ars.example/2", 7),
                               item("AI startups face a cash squeeze", "https://ars.example/3", 9)]},
}
(d / "feeds" / "rss").mkdir(parents=True)
(d / "feeds" / "rss" / "2026-11-10-2100.json").write_text(json.dumps({"fetched": now.isoformat(), "data": rss}))
page = news.build(now)
got = [(s["title"], s["outlets"]) for s in page["candidates"]]
assert got == [("Ukraine ceasefire talks open in Geneva", ["BBC", "DW", "The Guardian"]),
               ("Drone strike hits Odesa port overnight", ["BBC", "DW"]),
               ("New AI model tops coding benchmark", ["Ars Technica"]),     # BBC has its two: "Troops leave" is out
               ("Why AI chips run hot", ["Ars Technica"]),
               ("Berlin troops deployment debated in parliament", ["DW"])], got
assert page["candidates"][0]["score"] == 3.4722 and "score" not in page["stories"][0]    # 3 + 0.5 x 34/36
assert all(s["topics"] for s in page["candidates"]) and "Bakery wins national bread prize" in \
    [s["title"] for s in page["stories"]]
assert page["candidates"] == news.build(now + timedelta(hours=1))["candidates"]
news.PER_SECTION = 1                               # a section cut never takes a story off the shortlist
cut = news.build(now)
assert len(cut["stories"]) < len(page["stories"]) and cut["candidates"] == page["candidates"]
news.PER_SECTION = 40
runs = []
for seed in ("1", "2"):
    subprocess.run([sys.executable, str(here.parent / "agent-jobs" / "news.py")], check=True, capture_output=True,
                   env={**os.environ, "PYTHONPATH": str(here.parent / "agent-jobs"), "PYTHONHASHSEED": seed})
    runs.append(json.loads((d / "web" / "news.json").read_text()))
    del runs[-1]["generated"]
assert runs[0] == runs[1] and runs[0]["candidates"] == json.loads(json.dumps(page["candidates"]))
print("SAME LIST OK")
