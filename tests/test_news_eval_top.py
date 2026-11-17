"""news.py --eval-top: keep/drop labels against the Top list (candidates), ranked as of the fetch."""
import json, os, re, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

d = Path(tempfile.mkdtemp())
(d / "config").mkdir()
here = Path(__file__).parent
REPO = here.parent
JOB = REPO / "agent-jobs" / "news.py"
ENV = {**os.environ, "PYTHONPATH": str(REPO / "agent-jobs"), "FEEDS_DIR": str(d / "feeds"),
       "FEEDS_CONFIG": str(d / "config"), "AGENT_WEB_DIR": str(d / "web"), "AGENT_DB": str(d / "data" / "agent.db")}

# Runs news.py with the clock set to argv[1]: datetime.now() and time.time() both say so. Set before
# news.py is imported, so its `from datetime import datetime` gets the moved clock too.
CLOCK = """
import datetime as dt, runpy, sys, time
FIXED = dt.datetime.fromisoformat(sys.argv[1])
class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED.astimezone(tz) if tz else FIXED.astimezone(dt.timezone.utc).replace(tzinfo=None)
    @classmethod
    def utcnow(cls):
        return FIXED.astimezone(dt.timezone.utc).replace(tzinfo=None)
dt.datetime = Clock
time.time = lambda: FIXED.timestamp()
sys.argv = [sys.argv[2]] + sys.argv[3:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def job(*args, clock=None, env=None):
    cmd = [sys.executable, str(JOB), *args] if clock is None else \
        [sys.executable, "-c", CLOCK, clock.isoformat(), str(JOB), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env or ENV, cwd=REPO)


def untouched():
    assert not (d / "web").exists() and not (d / "data").exists(), "news.json or agent.db was written"


FETCHED = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
(d / "config" / "news.yaml").write_text("""
feeds:
  - {name: BBC Europe, outlet: BBC, region: Europe, topic: Politics, url: "https://a.example/1"}
  - {name: BBC World, outlet: BBC, region: World, topic: World, url: "https://a.example/2"}
  - {name: DW, region: Europe, topic: World, url: "https://b.example/1"}
topics:
  War: {include: [ceasefire, troops]}
candidates:
  max: 12
  per_outlet: 20
""")


def item(title, link, hours):
    return {"title": title, "link": f"https://{link}.example/", "published": (FETCHED - timedelta(hours=hours)).isoformat(),
            "summary": ""}


# one story from three articles: BBC Europe leads, DW is shown, BBC World's is grouped in but not shown
# (one line per outlet); 13 single DW stories, one hour apart; a story without a topic; one too old
NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliett", "Kilo",
         "Lima", "Mike"]
(d / "feeds" / "rss").mkdir(parents=True)
(d / "feeds" / "rss" / "2026-11-10-1800.json").write_text(json.dumps({"fetched": FETCHED.isoformat(), "data": {
    "BBC Europe": {"items": [item("Troops leave Kherson bridgehead after ceasefire deal", "bbc-g", 1),
                             item("Bakery wins national bread prize", "bakery", 2)]},
    "BBC World": {"items": [item("Kherson bridgehead: troops leave after ceasefire deal agreed", "bbc-g2", 3)]},
    "DW": {"items": [item("Kherson bridgehead troops leave after ceasefire deal", "dw-g", 2)] +
                    [item(f"Ceasefire in {n}", n.lower(), 4 + i) for i, n in enumerate(NAMES)] +
                    [item("Ceasefire in Zulu", "zulu", 40)]}}}))


def lab(link, keep, headline=None):
    return {"keep": keep, "headline": headline or link, "url": f"https://{link}.example/"}


LABELS = [lab("bbc-g2", True),                       # only the hidden link: the story still counts as a keep
          lab("alpha", True), lab("bravo", False), lab("charlie", True),   # #2 #3 #4; delta (#5) unlabelled
          *(lab(n.lower(), True) for n in NAMES[4:9]),                     # #6 to #10
          lab("juliett", "drop"),                     # #11, "drop" as a word; kilo (#12) unlabelled
          lab("lima", True),                          # #13: below the cut of 12; mike (#14) unlabelled
          lab("bakery", "yes"),                       # no topic, so never a candidate
          lab("zulu", False, "Ceasefire in Zulu"),    # 40 hours before the fetch: not in it
          {"url": "https://kilo.example/", "keep": "maybe"}, {"keep": True}, "just text"]   # skipped
labels = d / "labels.yaml"
labels.write_text(yaml.safe_dump(LABELS, sort_keys=False))


def table(out):
    """{"top 10": {"keep": n, ...}, "all 12": {...}} from the printed table."""
    lines = out.splitlines()
    head = next(i for i, l in enumerate(lines) if l.split()[:3] == ["keep", "drop", "unlabelled"])
    kinds = lines[head].split()
    rows = {}
    for l in lines[head + 1:head + 3]:
        m = re.match(r"\s+(top \d+|all \d+)\s+([\d\s]+)$", l)
        assert m, l
        rows[m[1]] = dict(zip(kinds, map(int, m[2].split())))
    return rows


# 1) the numbers, what's missed and why, what got in; at the fetch time; writes nothing
out = job("--eval-top", str(labels), clock=FETCHED)
assert out.returncode == 0, out
o = out.stdout
assert o.startswith(f"Top as of the fetch of 2026-11-10T18:00:00+00:00: 12 stories (max 12). 13 labels from {labels} "
                    "(3 skipped: no url or no keep: yes/no); labelled stories in this fetch: 10 keep, 2 drop.\n"), o
assert table(o) == {"top 10": {"keep": 8, "drop": 1, "unlabelled": 1},
                    "all 12": {"keep": 8, "drop": 2, "unlabelled": 2}}, o
assert "Keeps in the top 10: 8 of 10; in the whole list: 8 of 10.\n" in o
assert "Labels by link (however stories are grouped): keep 8 of 10 in the top 10, 8 in the whole list; " \
       "drop 1 of 3 in the top 10, 2 in the whole list.\n" in o, o
assert "Keeps not in the Top (2):\n" \
       "  #13, below the cut  Ceasefire in Lima  (DW)\n" \
       "  no topic            Bakery wins national bread prize  (BBC)\n" in o, o
assert "Drops in the Top (2):\n  #3   Ceasefire in Bravo  (DW)\n  #11  Ceasefire in Juliett  (DW)\n" in o, o
assert "Unlabelled in the Top (2):\n  #5   Ceasefire in Delta  (DW)\n  #12  Ceasefire in Kilo  (DW)\n" in o, o
assert "Labels that match no story in this fetch (1): from another fetch, or more than 36 hours old at it\n" \
       "  Ceasefire in Zulu\n" in o, o
assert "Mike" not in o.split("\nThe list")[0]       # unlabelled below the cut: not among the counts
# the whole list with ranks, label, score and section; then every story of several articles, each
# article with its outlet and label (the lead first, as news.yaml orders the feeds)
assert "\nThe list (12), best first:\n" \
       "  #1   keep       2.486  Europe  Troops leave Kherson bridgehead after ceasefire deal  (BBC, DW)\n" \
       "  #2   keep       1.444  Europe  Ceasefire in Alpha  (DW)\n" \
       "  #3   drop       1.431  Europe  Ceasefire in Bravo  (DW)\n" in o, o
assert "  #12  unlabelled 1.306  Europe  Ceasefire in Kilo  (DW)\n\n" in o, o
assert "\nStories grouped from several articles (1), each article with its label:\n" \
       "  #1: keep, 2 outlets, 3 articles\n" \
       "      -     BBC: Troops leave Kherson bridgehead after ceasefire deal\n" \
       "      keep  BBC: Kherson bridgehead: troops leave after ceasefire deal agreed\n" \
       "      -     DW: Kherson bridgehead troops leave after ceasefire deal\n" in o, o
untouched()
print("NUMBERS OK")

# 2) any link of a story labels it: the lead's, a shown one's, or one grouped in but not shown. A
#    keep on any of its links makes it a keep: a drop on another is a copy of a story already kept
for link in ("bbc-g", "dw-g", "bbc-g2"):
    labels.write_text(yaml.safe_dump([lab(link, False)]))
    o = job("--eval-top", str(labels), clock=FETCHED).stdout
    assert table(o)["top 10"] == {"keep": 0, "drop": 1, "unlabelled": 9}, (link, o)
    assert "Drops in the Top (1):\n  #1   Troops leave Kherson bridgehead after ceasefire deal  (BBC, DW)\n" in o, o
for keep, drop in (("bbc-g", "bbc-g2"), ("bbc-g2", "dw-g")):
    labels.write_text(yaml.safe_dump([lab(keep, True), lab(drop, False), lab("alpha", True)]))
    o = job("--eval-top", str(labels), clock=FETCHED).stdout
    assert table(o)["all 12"] == {"keep": 2, "drop": 0, "unlabelled": 10}, o
    assert "labelled stories in this fetch: 2 keep, 0 drop." in o and "Drops in the Top" not in o, o
    assert "  #1   keep       2.486" in o, o
    assert "Labels by link (however stories are grouped): keep 2 of 2 in the top 10, 2 in the whole list; " \
           "drop 1 of 1 in the top 10, 1 in the whole list." in o, o   # the copy's link is still counted here

print("LINKS OK")

# 3) a week later (and a year later, and the real now) the same output: ranked as of the fetch. The
#    moved clock works: a normal run at that moment finds nothing, as everything is too old
labels.write_text(yaml.safe_dump(LABELS, sort_keys=False))
at_fetch = job("--eval-top", str(labels), clock=FETCHED)
for clock in (FETCHED + timedelta(days=7), FETCHED + timedelta(days=365), None):
    later = job("--eval-top", str(labels), clock=clock)
    assert (later.returncode, later.stdout, later.stderr) == (0, at_fetch.stdout, ""), (clock, later)
assert job("--print", clock=FETCHED).stdout.startswith("15 stories from 2 outlets")   # the wrapper itself changes nothing
week = job("--print", clock=FETCHED + timedelta(days=7))
assert week.returncode == 1 and "no fresh stories" in week.stderr and "0 stories" in week.stdout, week
untouched()
print("CLOCK OK")

# 3b) a keep whose only topic comes from feeds: (a topic's whole feed) says so, not "no topic"
news_yaml = (d / "config" / "news.yaml").read_text()
(d / "config" / "news.yaml").write_text(news_yaml.replace("  War: {include: [ceasefire, troops]}",
                                                          "  War: {include: [ceasefire, troops]}\n"
                                                          "  Food: {include: [soup], feeds: [BBC Europe]}"))
o = job("--eval-top", str(labels), clock=FETCHED).stdout
assert "  topic only from feeds:  Bakery wins national bread prize  (BBC)\n" in o, o
assert table(o) == table(at_fetch.stdout)          # nothing else changes: the shortlist ignores such topics
(d / "config" / "news.yaml").write_text(news_yaml)
print("FEED TOPIC OK")

# 4) the same on the real fetch and labels in samples/, with the real news.yaml: the same numbers a
#    week later, and the list is exactly the candidates news.json got at the fetch
SAMPLES = {**ENV, "FEEDS_DIR": str(REPO / "samples" / "feeds"), "FEEDS_CONFIG": str(REPO / "state-feeds")}
sample_labels = REPO / "samples" / "labels.yaml"
if sample_labels.exists():
    fetched = datetime.fromisoformat(json.loads(max((REPO / "samples" / "feeds" / "rss").glob("*.json"))
                                                .read_text())["fetched"])
    at_fetch = job("--eval-top", str(sample_labels), clock=fetched, env=SAMPLES)
    assert at_fetch.returncode == 0 and at_fetch.stderr == "", at_fetch
    rows = table(at_fetch.stdout)
    assert sum(rows["top 10"].values()) == 10, at_fetch.stdout
    for clock in (fetched + timedelta(days=7), None):
        later = job("--eval-top", str(sample_labels), clock=clock, env=SAMPLES)
        assert (later.returncode, later.stdout) == (0, at_fetch.stdout), (clock, later)
    # what you asked for: the Canary Islands migrants story (3 outlets, no topic keyword) in the top 10
    first10 = at_fetch.stdout.split("\nThe list (")[1].split("\n")[1:11]
    assert any("Canary Islands" in l for l in first10), first10
    week = job("--print", clock=fetched + timedelta(days=7), env=SAMPLES)
    assert week.returncode == 1 and "no fresh stories" in week.stderr, week       # the clock really moved
    # the ranks it prints are news.json's: build() at the fetch time, in this process
    os.environ.update(FEEDS_DIR=SAMPLES["FEEDS_DIR"], FEEDS_CONFIG=SAMPLES["FEEDS_CONFIG"])
    sys.path.insert(0, str(REPO / "agent-jobs"))
    import news  # noqa: E402
    page = news.build(fetched)
    assert f"all {len(page['candidates'])}" in rows, rows
    # every line of the list: rank, score, title and outlets as news.json's candidates have them
    part = at_fetch.stdout.split("\nThe list (")[1].split("\n\n")[0]
    listed = re.findall(r"^  #(\d+) +(keep|drop|unlabelled) +([\d.]+)  (\w+) +(.+)  \(([^()]+)\)$", part, re.M)
    assert len(listed) == len(page["candidates"]), part
    for (n, _, score, section, title, outlets), s in zip(listed, page["candidates"]):
        assert (title, outlets, score, section) == (s["title"], ", ".join(s["outlets"]), f"{s['score']:.3f}", s["section"]), n
    for n, title, outlets in re.findall(r"^  #(\d+) +(.+)  \(([^()]+)\)$",
                                        at_fetch.stdout.split("\nDrops in the Top")[1].split("\nThe list")[0], re.M):
        s = page["candidates"][int(n) - 1]                 # the drops and unlabelled, by rank
        assert (s["title"], ", ".join(s["outlets"])) == (title, outlets), (n, title)
    untouched()
    print("SAMPLES OK")

# 5) a normal run never reads samples/ (nor a labels file): every file and folder it opens is recorded,
#    with the default FEEDS_DIR and FEEDS_CONFIG (an empty home) and with a real fetch
AUDIT = """
import json, runpy, sys
seen, report = [], sys.argv[1]
def hook(event, args):
    if event in ("open", "os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes)):
        seen.append(str(args[0]))
sys.addaudithook(hook)
sys.argv = sys.argv[2:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    with open(report, "w") as f:
        json.dump(seen, f)
"""
report = d / "opened.json"
home = d / "home"
home.mkdir()
normal = {**ENV, "AGENT_WEB_DIR": str(d / "normal-web")}       # a normal run writes news.json: not in d / "web"
defaults = {k: v for k, v in normal.items() if k not in ("FEEDS_DIR", "FEEDS_CONFIG")}
for env in ({**defaults, "HOME": str(home)}, normal):
    for args in ([], ["--print"]):
        subprocess.run([sys.executable, "-c", AUDIT, str(report), str(JOB), *args], capture_output=True, env=env,
                       cwd=REPO)
        opened = [p for p in json.loads(report.read_text()) if p != str(report)]
        assert any("news.yaml" in p for p in opened) or "FEEDS_DIR" not in env, opened   # the hook sees opens
        bad = [p for p in opened if "samples" in Path(os.path.abspath(p)).relative_to("/").parts
               or "labels" in Path(p).name]
        assert not bad, (args, bad)
print("NORMAL RUN OK")

# 6) input it can't use: a message, never a traceback; still nothing written
for content, message in [("- [", "labels.yaml: while parsing"), ("keep: yes", "expected a list of stories"),
                         (yaml.safe_dump([lab("alpha", True), lab("alpha", "no")]), "alpha.example/ is labelled both"),
                         (None, "No such file or directory")]:
    if content is None:
        labels.unlink()
    else:
        labels.write_text(content)
    out = job("--eval-top", str(labels))
    assert out.returncode == 1 and message in out.stderr and "Traceback" not in out.stderr, (content, out.stderr)
labels.write_text(yaml.safe_dump(LABELS))
for args in (["--eval-top", "a", "--eval", "b"], ["--print", "--eval-top", "a"]):
    out = job(*args)
    assert out.returncode == 2 and "not allowed with argument" in out.stderr, out.stderr
(d / "feeds" / "rss" / "2026-11-10-1800.json").rename(d / "moved.json")
out = job("--eval-top", str(labels))
assert out.returncode == 1 and out.stderr.strip() == f"no saved rss fetch in {d / 'feeds' / 'rss'}", out.stderr
untouched()
print("BAD INPUT OK")

# 7) output cut short (news.py --eval-top ... | head): quiet, exit 0, whatever the mode
(d / "moved.json").rename(d / "feeds" / "rss" / "2026-11-10-1800.json")
# (the samples/ case only where the folder is: it isn't in the public copy)
cases = [["--eval-top", str(labels)], ["--print"]] + ([["--eval-top", str(sample_labels)]] if sample_labels.exists() else [])
for args in cases:
    for unbuffered in ("1", None):                 # fails at the first line, or only at the last flush
        r, w = os.pipe()
        os.close(r)                                # the reader is gone before the first line
        env = {k: v for k, v in (SAMPLES if str(REPO / "samples") in args[-1] else ENV).items()
               if k != "PYTHONUNBUFFERED"}
        if unbuffered:
            env["PYTHONUNBUFFERED"] = unbuffered
        cut = subprocess.run([sys.executable, str(JOB), *args], stdout=w, stderr=subprocess.PIPE, text=True, env=env)
        os.close(w)
        assert cut.returncode == 0 and cut.stderr == "", (args, unbuffered, cut.returncode, cut.stderr)
untouched()
print("CUT OUTPUT OK")
