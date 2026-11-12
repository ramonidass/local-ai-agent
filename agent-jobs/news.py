#!/usr/bin/env python3
"""News: the fetched feeds -> web/news.json for the News tab.

Runs right after each successful news-fetch, with no network at all and no model. Code groups
the same story from different outlets (by the words in the headlines), so the tab can show one
story with its outlets instead of five near-identical headlines. Headlines are untrusted text:
they are cut to length, only https:// links are kept, and the page shows them as plain text.

Usage:
  news.py           # write web/news.json
  news.py --print   # print the stories several outlets cover, write nothing
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

import agentdb

FEEDS_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_DIR", "~/agent/data/feeds")))
CONFIG_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_CONFIG", "~/agent/state/feeds")))
MAX_AGE_HOURS = int(os.environ.get("NEWS_MAX_AGE_HOURS", "36"))
PER_SECTION = 40            # stories kept per section
TOPIC_SECTIONS = ("Tech", "Space", "Business")   # these topics are sections; the rest go by region
SECTIONS = ("Europe", "US", "World", "Business", "Tech", "Space")
SAME_STORY = 0.34           # share of headline words two items must have in common
STOP = set("""a about after again against all also amid among an and are as at be been before being between
both but by can could did do does down during for from had has have he her his how if in into is it its
just may more most new no not of off on one or out over says said say she should since so some than that
the their them then there these they this to under up was were what when where which who why will with
would you your us uk""".split())
WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def latest(source):
    """The newest saved run of a source, or None."""
    for p in reversed(sorted((FEEDS_DIR / source).glob("*.json"))):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def feeds_config():
    """{feed name: its entry in news.yaml}: the outlet a feed belongs to, its region and topic."""
    p = CONFIG_DIR / "news.yaml"
    cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    return {f["name"]: f for f in cfg.get("feeds") or [] if isinstance(f, dict) and f.get("name")}


def parse_time(text):
    """RSS dates ("Tue, 10 Nov 2026 07:00:00 GMT") and Atom dates ("2026-11-10T06:00:00Z") as UTC."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def words(title: str) -> frozenset:
    """The words that carry a headline: lower case, no small words, plurals folded ("tariffs" = "tariff")."""
    out = set()
    for w in WORD_RE.findall(title.lower().replace("’", "'")):
        w = w.split("'")[0]
        if len(w) < 3 or w in STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return frozenset(out)


def same_story(a: frozenset, b: frozenset) -> bool:
    """Two headlines are one story when they share at least three words and a third of all their
    words (or most of the shorter one)."""
    both = len(a & b)
    return both >= 3 and (both / len(a | b) >= SAME_STORY or both / min(len(a), len(b)) >= 0.6)


def section_of(feed) -> str:
    """Tech, Space and Business feeds are sections of their own; a general world feed is World
    whatever its outlet's home; the rest go by region."""
    topic, region = str(feed.get("topic") or ""), str(feed.get("region") or "World")
    if topic in TOPIC_SECTIONS or topic == "World":
        return topic
    return region if region in SECTIONS else "World"


def items(now):
    """Every fresh feed item once (the same link in two feeds counts once), newest first."""
    cfg, seen, out = feeds_config(), set(), []
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    data = (latest("rss") or {}).get("data") or {}
    for order, (name, feed) in enumerate(data.items()):
        meta = {**feed, **cfg.get(name, {})} if isinstance(feed, dict) else {}
        for it in (feed.get("items") or []) if isinstance(feed, dict) else []:
            title, url = str(it.get("title") or "").strip()[:300], str(it.get("link") or "")
            at = parse_time(it.get("published"))
            if not title or not url.startswith("https://") or url in seen or not at or not cutoff <= at <= now:
                continue
            seen.add(url)
            out.append({"title": title, "url": url[:500], "summary": str(it.get("summary") or "")[:280],
                        "outlet": str(meta.get("outlet") or name)[:40], "section": section_of(meta),
                        "at": at, "order": order, "words": words(title)})
    return sorted(out, key=lambda i: i["at"], reverse=True)


def stories(now):
    """Items grouped into stories. A story's headline comes from the outlet listed first in
    news.yaml (public broadcasters lead); its section is where most of its items are."""
    groups = []
    for it in items(now):
        home = next((g for g in groups if any(same_story(it["words"], o["words"]) for o in g)), None) \
            if len(it["words"]) >= 3 else None
        if home is None:
            groups.append([it])
        else:
            home.append(it)
    out = []
    for g in groups:
        lead = min(g, key=lambda i: (i["order"], -i["at"].timestamp()))
        per_outlet = {}
        for it in sorted(g, key=lambda i: (i["order"], -i["at"].timestamp())):
            per_outlet.setdefault(it["outlet"], it)   # one line per outlet: its newest in its first feed
        sections = [it["section"] for it in per_outlet.values()]
        out.append({"title": lead["title"], "summary": lead["summary"], "url": lead["url"],
                    # where most of its outlets file it; a tie goes to the named place over World
                    "section": max(SECTIONS, key=lambda s: (sections.count(s), s != "World", s == lead["section"])),
                    "at": max(it["at"] for it in g).isoformat(timespec="seconds"),
                    "outlets": list(per_outlet),
                    "items": [{"outlet": it["outlet"], "title": it["title"], "url": it["url"],
                               "at": it["at"].isoformat(timespec="seconds")} for it in per_outlet.values()]})
    out.sort(key=lambda s: (len(s["outlets"]), s["at"]), reverse=True)   # most-covered first, then newest
    kept, count = [], {}
    for s in out:
        count[s["section"]] = count.get(s["section"], 0) + 1
        if count[s["section"]] <= PER_SECTION:
            kept.append(s)
    return kept


def hacker_news():
    rows = ((latest("hackernews") or {}).get("data") or {}).get("stories") or []
    out = [{"title": str(r.get("title") or "")[:300], "url": u, "score": int(r.get("score") or 0),
            "comments": int(r.get("comments") or 0)}
           for r in rows if isinstance(r, dict) and r.get("title")
           and (u := str(r.get("url") or "")).startswith("https://")]
    return sorted(out, key=lambda r: -r["score"])[:15]


def build(now=None):
    now = now or datetime.now(timezone.utc)
    rss = latest("rss") or {}
    try:
        health = json.loads((FEEDS_DIR / "health.json").read_text())
    except (OSError, ValueError):
        health = {}
    found = stories(now)
    return {"generated": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "fetched": rss.get("fetched"), "sections": list(SECTIONS),
            "outlets": sorted({o for s in found for o in s["outlets"]}),
            "failed_feeds": sorted(rss.get("errors") or {}), "stories": found, "hn": hacker_news(),
            "fetch": {"updated": health.get("updated"),
                      "failed": sorted(k for k, v in (health.get("sources") or {}).items()
                                       if not v.get("ok") and k in ("rss", "hackernews"))}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print", action="store_true", help="print only; write nothing")
    args = ap.parse_args()
    page = build()
    top = [s for s in page["stories"] if len(s["outlets"]) > 1]
    print(f"{len(page['stories'])} stories from {len(page['outlets'])} outlets; {len(top)} covered by several")
    for s in top[:15]:
        print(f"  [{s['section']}] {s['title']}  ({', '.join(s['outlets'])})")
    if not args.print:
        agentdb._atomic_write_text(agentdb.WEB_DIR / "news.json", json.dumps(page, ensure_ascii=False))
    if not page["stories"]:
        sys.exit("no fresh stories in feeds/rss")


if __name__ == "__main__":
    main()
