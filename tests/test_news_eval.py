"""news.py --export-eval / --eval: the latest fetch's stories to label by hand, then the topics checked again."""
import json, os, stat, subprocess, sys, tempfile
from pathlib import Path

import yaml

d = Path(tempfile.mkdtemp())
(d / "config").mkdir()
here = Path(__file__).parent
JOB = here.parent / "agent-jobs" / "news.py"
ENV = {**os.environ, "PYTHONPATH": str(here.parent / "agent-jobs"), "FEEDS_DIR": str(d / "feeds"),
       "FEEDS_CONFIG": str(d / "config"), "AGENT_WEB_DIR": str(d / "web"), "AGENT_DB": str(d / "data" / "agent.db")}
FEEDS = """
feeds:
  - {name: BBC Europe, outlet: BBC, region: Europe, topic: Politics, url: "https://a.example/1"}
  - {name: DW, region: Europe, topic: World, url: "https://b.example/1"}
  - {name: Ars Technica, region: World, topic: Tech, url: "https://e.example/1"}
"""
TOPICS = """
topics:
  Politics: {include: [election, parliament, Geneva]}
  War: {include: [ceasefire, war]}
  Tech: {include: [AI, chips]}
"""
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS)


def item(title, link, published):
    return {"title": title, "link": f"https://{link}.example/", "published": published, "summary": ""}


# a fetch from days ago: the export takes the stories as of that fetch, not as of now
(d / "feeds" / "rss").mkdir(parents=True)
(d / "feeds" / "rss" / "2026-11-10-1800.json").write_text(json.dumps({"fetched": "2026-11-10T18:00:00+00:00", "data": {
    "BBC Europe": {"items": [item("Ukraine and Russia open talks in Geneva", "bbc-talks", "2026-11-10T16:00:00Z"),
                             item("Dutch election: centrists take narrow lead", "bbc-dutch", "2026-11-10T12:00:00Z")]},
    "DW": {"items": [item("Geneva talks on ending the war: Ukraine and Russia meet", "dw-talks",
                          "2026-11-10T15:00:00Z"),
                     item("Paris museum reopens after jewel heist", "dw-museum", "2026-11-10T10:00:00Z")]},
    "Ars Technica": {"items": [item("New AI chips run hot", "ars-chips", "2026-11-10T09:00:00Z")]}}}))


def job(*args):
    return subprocess.run([sys.executable, str(JOB), *args], capture_output=True, text=True, env=ENV)


def untouched():
    assert not (d / "web").exists() and not (d / "data").exists(), "news.json or agent.db was written"


# 1) --export-eval: every story of the latest fetch, what it got now, expected a copy; 0600; nothing else
labels = d / "eval.yaml"
labels.write_text("old")
labels.chmod(0o644)                                  # an older, wider file is narrowed too
out = job("--export-eval", str(labels))
assert out.returncode == 0 and "4 stories from the fetch of 2026-11-10T18:00:00+00:00" in out.stdout, out
assert stat.S_IMODE(labels.stat().st_mode) == 0o600
text = labels.read_text()
assert text.startswith("# Stories from one fetch, for `news.py --eval`. Set \"expected\"")
cases = yaml.safe_load(text)
talks = cases[0]
assert talks == {"headline": "Ukraine and Russia open talks in Geneva", "outlet": "BBC", "section": "Europe",
                 "topics": ["Politics", "War"], "expected": ["Politics", "War"],   # "war": only in DW's headline
                 "feeds": ["BBC Europe", "DW"],
                 "also": [{"outlet": "DW",
                           "headline": "Geneva talks on ending the war: Ukraine and Russia meet"}]}, talks
assert [(c["headline"], c["topics"]) for c in cases[1:]] == [
    ("Dutch election: centrists take narrow lead", ["Politics"]), ("Paris museum reopens after jewel heist", []),
    ("New AI chips run hot", ["Tech"])], cases
assert all(c["expected"] == c["topics"] and "also" not in c for c in cases[1:])
assert "expected: [Politics, War]" in text and "expected: []" in text   # lists as [..], easy to edit by hand
fresh = d / "new.yaml"
assert job("--export-eval", str(fresh)).returncode == 0 and stat.S_IMODE(fresh.stat().st_mode) == 0o600
untouched()
print("EXPORT OK")

# 2) --eval: all right as exported (exit 0); one expected changed is caught (exit 1); the file,
#    news.json and agent.db stay as they were
before = labels.read_bytes()
out = job("--eval", str(labels))
assert out.returncode == 0 and "4 of 4 right (100%)." in out.stdout and "Wrong" not in out.stdout, out.stdout
labels.write_text(text.replace("  expected: [Politics]", "  expected: [Politics, War]"))
out = job("--eval", str(labels))
assert out.returncode == 1, out
assert "Wrong (1):\n  expected [Politics, War], got [Politics]; missed War\n" \
       "      Dutch election: centrists take narrow lead  (BBC)" in out.stdout, out.stdout
assert "3 of 4 right (75%)." in out.stdout and "Changed since the export" not in out.stdout
assert "Politics 2 right, 0 extra, 0 missed; War 1 right, 0 extra, 1 missed; Tech 1 right" in out.stdout
labels.write_text(text.replace("  expected: [Tech]", "  expected: []"))     # none expected: Tech is extra
out = job("--eval", str(labels))
assert out.returncode == 1 and "expected [], got [Tech]; extra Tech" in out.stdout, out.stdout
labels.write_text(text.replace("  expected: [Politics, War]", "  expected: [War, Politics]"))   # order is free
assert job("--eval", str(labels)).returncode == 0
labels.write_text(text.replace("  expected: [Tech]", "  expected: [Tech, War]"))
assert "expected [Tech, War], got [Tech]; missed War" in job("--eval", str(labels)).stdout
labels.write_bytes(before)
assert job("--eval", str(labels)).returncode == 0 and labels.read_bytes() == before
untouched()
print("EVAL OK")

# 3) after a news.yaml change: what changed since the export, and what is now wrong
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS.replace("[ceasefire, war]", "[ceasefire]")
                                        .replace("parliament, Geneva]", "parliament, Geneva, museum]"))
out = job("--eval", str(labels))
assert out.returncode == 1, out
assert "Changed since the export (2):\n  [Politics, War] -> [Politics]  Ukraine and Russia open talks in Geneva" \
       "  (BBC)\n  [] -> [Politics]  Paris museum reopens after jewel heist  (DW)\n" in out.stdout, out.stdout
assert "Wrong (2):" in out.stdout and "2 of 4 right (50%)." in out.stdout
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS)
# a topic's feeds: count in --eval too, through each case's "feeds"
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS.replace("[AI, chips]}", "[AI, chips], feeds: [DW]}"))
out = job("--eval", str(labels))
assert out.returncode == 1 and "[] -> [Tech]  Paris museum reopens after jewel heist  (DW)" in out.stdout, out.stdout
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS)
print("CHANGED OK")

# 4) hand edits that can't be used: skipped or named, never a traceback
bad = cases + [{"headline": "No expected here"}, {"expected": ["War"]}, "just text",
               {"headline": "A typo", "expected": ["Politcs"], "topics": []}]
labels.write_text(yaml.safe_dump(bad))
out = job("--eval", str(labels))
assert out.returncode == 1 and "5 labelled stories from" in out.stdout
assert "(3 skipped: no headline or no 'expected' list)" in out.stdout, out.stdout
assert "Not topics in news.yaml, so they can never match: Politcs" in out.stdout, out.stdout
for content, message in [("- [", "eval.yaml: while parsing"), ("headline: x", "expected a list of stories"),
                         (None, "No such file or directory")]:
    if content is None:
        labels.unlink()
    else:
        labels.write_text(content)
    out = job("--eval", str(labels))
    assert out.returncode == 1 and message in out.stderr and "Traceback" not in out.stderr, (content, out.stderr)
(d / "config" / "news.yaml").write_text(FEEDS + "topics: {War: {include: ['...']}}\n")     # a broken news.yaml
for args in (["--eval", str(fresh)], ["--export-eval", str(labels)]):
    out = job(*args)
    assert out.returncode == 1, out
    assert out.stderr.strip().startswith("news.yaml topics: War: include: '...' can never match"), out.stderr
(d / "config" / "news.yaml").write_text(FEEDS + TOPICS)
for args in (["--eval", "a", "--export-eval", "b"], ["--print", "--eval", "a"]):
    out = job(*args)
    assert out.returncode == 2 and "not allowed with argument" in out.stderr, out.stderr
(d / "feeds" / "rss" / "2026-11-10-1800.json").rename(d / "moved.json")
out = job("--export-eval", str(labels))
assert out.returncode == 1 and out.stderr.strip() == f"no saved rss fetch in {d / 'feeds' / 'rss'}", out.stderr
untouched()
print("BAD INPUT OK")
