"""news.py grouping, second step: the same event told in other words is one story (merge_events())."""
import itertools, json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

d = Path(tempfile.mkdtemp())
(d / "config").mkdir()
(d / "feeds" / "rss").mkdir(parents=True)
os.environ.update(FEEDS_DIR=str(d / "feeds"), FEEDS_CONFIG=str(d / "config"), AGENT_WEB_DIR=str(d / "web"),
                  AGENT_DB="")              # news.py never touches agent.db
sys.path.insert(0, str(Path(__file__).parent.parent / "agent-jobs"))
import news  # noqa: E402

FETCHED = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
(d / "config" / "news.yaml").write_text("""
feeds:
  - {name: BBC Europe, outlet: BBC, region: Europe, url: "https://a.example/1"}
  - {name: DW, region: Europe, url: "https://b.example/1"}
  - {name: Euronews, region: Europe, url: "https://c.example/1"}
  - {name: Ars Technica, region: World, url: "https://d.example/1"}
""")
def item(title, link, hours, summary=""):
    return {"title": title, "link": f"https://{link}.example/", "published": (FETCHED - timedelta(hours=hours)).isoformat(), "summary": summary}
FILLER = ["Bakery wins national bread prize", "Museum reopens after long renovation", "Farmers protest over milk prices",
          "Orchestra cancels winter tour", "Heatwave grips southern coast", "Train strike halts commuter services",
          "Chess champion retains world title", "City council approves new bike lanes", "Zoo welcomes rare panda cub",
          "Airline cuts routes amid fuel costs", "Storm knocks out power to thousands", "Court jails fraudster for eight years",
          "Teachers vote on pay offer", "Hospital waiting lists grow again", "Film festival opens with comedy",
          "Vineyard harvest falls after frost", "Police seize record cocaine haul", "Startup builds electric ferry",
          "Football club sacks manager", "Water company fined for sewage spills"]
def fetch(order=("BBC Europe", "DW", "Euronews", "Ars Technica")):
    data = {
      "BBC Europe": [item("SpaceX catches Starship rocket in historic first", "bbc-ship", 3,
                          "Mechanical arms at the Texas launch tower caught the Starship spacecraft on its return."),
                     item("Zelensky asks Musk to lift Starlink limits", "bbc-link", 5,
                          "The Ukrainian president says throttled satellite internet makes Russian drones hard to stop."),
                     item("Haiti's presidential election: candidates register", "bbc-haiti", 6,
                          "The vote to choose a new president is set for next year.")] +
                    [item(t, f"bbc-f{i}", 10 + i, "") for i, t in enumerate(FILLER[:10])],
      "DW": [item("SpaceX completes first-ever spaceship capture", "dw-ship", 4,
                  "The recovery of the Starship by the tower's mechanical arms marks a breakthrough for reusable rockets."),
             item("Cape Verde presidential election: incumbent set to win", "dw-verde", 7,
                  "Voters in the West African nation backed the president for a second term.")] +
            [item(t, f"dw-f{i}", 10 + i, "") for i, t in enumerate(FILLER[10:])],
      "Euronews": [item("Zelenskyy urges Musk to restore Starlink access", "eu-link", 2,
                        "Ukraine says slowed satellite internet stops it intercepting Russian drones.")],
      "Ars Technica": [item("Vulcan rocket fails during launch, a blow to SpaceX rivals", "ars-vulcan", 8,
                            "The first total mission failure in United Launch Alliance's history destroyed a payload of satellites.")],
    }
    (d / "feeds" / "rss" / "2026-11-10-1800.json").write_text(json.dumps(
        {"fetched": FETCHED.isoformat(), "data": {k: {"items": data[k]} for k in order}}))


def groups(order=None):
    """The stories as sets of short link names, and by lead."""
    fetch(*([order] if order else []))
    found = news.stories(FETCHED, {}, links=True)
    return {frozenset(u.split("//")[1].split(".")[0] for u in s["links"]) for s in found}, \
        {s["links"] and s["title"]: s for s in found}


# 1) folding: what it matters for is which words fold together: plurals ("ies", "es" after ch, sh,
#    x, ss, plain "s"), and a doubled last letter, as names are spelled both ways
for a, b in [("canaries", "canary"), ("catches", "catch"), ("crashes", "crash"), ("taxes", "tax"),
             ("addresses", "address"), ("zelenskyy", "zelensky"), ("tariffs", "tariff"), ("starships", "starship")]:
    assert news.fold(a) == news.fold(b), (a, b, news.fold(a), news.fold(b))
for a, b in [("boss", "bos"), ("news", "new"), ("estonia", "estonian")]:
    assert news.fold(a) != news.fold(b), (a, b)
assert news.fold("canaries") == "canary" and news.fold("address") == "address"
heads, names, counts = news.event_words("Zelenskyy urges Musk to restore Starlink access", "Ukraine says it.")
assert heads == {"zelensky", "urge", "musk", "restore", "starlink", "access"}, heads
assert names == {"zelensky", "musk", "starlink"}, names                     # written with a capital
assert counts["zelensky"] == 2 and counts["ukraine"] == 1                   # the headline counts twice
print("FOLD OK")

# 2) one event in other words is one story; its outlets all count. Different events stay apart:
#    two elections share "presidential election" but no name; SpaceX's rival's failure shares the
#    name SpaceX but its words are unlike the catch's
got, by_title = groups()
assert {"bbc-ship", "dw-ship"} in got, got                 # "catches Starship" = "spaceship capture"
assert {"bbc-link", "eu-link"} in got, got                 # Zelensky = Zelenskyy, and the same words
for alone in ("bbc-haiti", "dw-verde", "ars-vulcan"):
    assert {alone} in got, (alone, got)
assert len(got) == 20 + 5, len(got)                        # the filler stays one story each
ship = by_title["SpaceX catches Starship rocket in historic first"]
assert ship["outlets"] == ["BBC", "DW"] and len(ship["items"]) == 2      # the lead: the first feed in news.yaml
print("SAME EVENT OK")

# 3) the result doesn't depend on the order of the feeds (which only picks the headline)
for order in itertools.permutations(["BBC Europe", "DW", "Euronews", "Ars Technica"]):
    assert groups(order)[0] == got, order
print("ORDER OK")
