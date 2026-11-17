#!/usr/bin/env python3
"""News: the fetched feeds -> web/news.json for the News tab.

Runs right after each successful news-fetch, with no network at all and no model. Code groups
the same story from different outlets (by the words in the headlines, then by the rarer words of
headline and summary, for the same event told in other words), so the tab can show one story with
its outlets instead of five near-identical headlines, each tagged with the topics: of news.yaml
whose keywords its headlines carry, plus the day's shortlist (candidates: in news.yaml).
Headlines are untrusted text: they are cut to length, only https:// links are kept, and the page
shows them as plain text.

Usage:
  news.py                          # write web/news.json
  news.py --print                  # print the stories several outlets cover, write nothing
  news.py --export-eval eval.yaml  # the latest fetch's stories as test cases; label their topics by hand
  news.py --eval eval.yaml         # how the current news.yaml topics tag them (exit 1 if any is wrong)
  news.py --eval-top labels.yaml   # keep/drop labels against the Top list (candidates) of the latest fetch

--eval-top ranks as of the fetch, not now, so a fetch from last week gives the same numbers today. To
check a saved fetch, point the job at it: FEEDS_DIR=samples/feeds FEEDS_CONFIG=state-feeds.
"""
import argparse
import json
import math
from collections import Counter
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from fractions import Fraction
from pathlib import Path

import yaml

import agentdb

FEEDS_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_DIR", "~/agent/data/feeds")))
CONFIG_DIR = Path(os.path.expanduser(os.environ.get("FEEDS_CONFIG", "~/agent/state/feeds")))
MAX_AGE_HOURS = int(os.environ.get("NEWS_MAX_AGE_HOURS", "36"))
PER_SECTION = 150           # stories kept per section: the fetch of 2026-11-16 07:35 had 111 in Europe
# The day's shortlist; news.yaml candidates: overrides these
CANDIDATES = {"max": 40, "per_outlet": 8, "no_topic_outlets": 2, "weights": {"outlets": 1.0, "fresh": 0.5},
              "sections": {}}
SECTIONS = ("Europe", "US", "World")   # by region only; the subject is the topics: of news.yaml
SAME_STORY = 0.34           # share of headline words two items must have in common
SAME_EVENT = 0.25           # or: likeness of two stories' words, rare words counting most (merge_events())
STOP = set("""a about after again against all also amid among an and are as at be been before being between
both but by can could did do does down during for from had has have he her his how if in into is it its
just may more most new no not of off on one or out over says said say she should since so some than that
the their them then there these they this to under up was were what when where which who why will with
would you your us uk""".split())
WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
TOKEN_RE = re.compile(WORD_RE.pattern, re.IGNORECASE)   # the same words, found before lower-casing


def latest(source):
    """The newest saved run of a source, or None."""
    for p in reversed(sorted((FEEDS_DIR / source).glob("*.json"))):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


class ConfigError(Exception):
    """news.yaml can't be used as it is; main() stops with the message instead of a traceback."""


def config():
    """news.yaml as a dict ({} when there is none)."""
    p = CONFIG_DIR / "news.yaml"
    try:
        cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    except yaml.YAMLError as e:
        raise ConfigError(f"news.yaml is not valid YAML: {e}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"news.yaml: expected feeds:, topics: and so on at the top, not a {type(cfg).__name__}")
    return cfg


def feeds_config():
    """{feed name: its entry in news.yaml}: the outlet a feed belongs to, its region and topic."""
    return {f["name"]: f for f in config().get("feeds") or [] if isinstance(f, dict) and f.get("name")}


def tokens(title: str) -> tuple:
    """Every word of a headline in order, for topics: (the word as words() writes it, the word as
    written). Unlike words() nothing is dropped, so "of" in "Bank of England" and "AI" count."""
    out = []
    for w in TOKEN_RE.findall(title.replace("’", "'")):
        w = w.split("'")[0]
        low = w.lower()
        out.append((low[:-1] if len(low) > 4 and low.endswith("s") and not low.endswith("ss") else low, w))
    return tuple(out)


def keyword(text, where) -> tuple:
    """A topic keyword as a phrase: its words in order, each (word, None) to match as words() writes
    it ("tariffs" = "Tariff"), or (None, word) for a short word in capitals, which matches only as
    written ("US" but not "us", "AI" but not "Ai"). Short words match whole, never inside a word."""
    if not isinstance(text, str) or not text.strip():
        raise ConfigError(f"{where}: {text!r} is not a keyword (a word or a few words, in quotes if in doubt)")
    phrase = tuple((None, w) if len(w) < 3 and w.isupper() else (low, None) for low, w in tokens(text))
    if not phrase:
        raise ConfigError(f"{where}: {text!r} can never match: it has no letters or digits")
    return phrase


def has_phrase(headline, phrase) -> bool:
    """The phrase's words stand next to each other, in order, somewhere in the headline (tokens())."""
    n = len(phrase)
    return any(all(w == exact if exact else low == word for (low, w), (word, exact) in zip(headline[i:i + n], phrase))
               for i in range(len(headline) - n + 1))


def topics_config():
    """{topic: (include, exclude, feeds)} from the topics: block of news.yaml, each keyword a phrase;
    feeds: (optional) names feeds whose every story is in the topic, keywords or not.
    No block means no topics; a block that can't be read stops the job rather than leaving a topic
    quietly empty."""
    raw = config().get("topics")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"news.yaml topics: expected topic names, each with include: and exclude: lists, "
                          f"not a {type(raw).__name__}")
    out, known = {}, set(feeds_config())
    for name, t in raw.items():
        where = f"news.yaml topics: {name}"
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"news.yaml topics: {name!r} is not a topic name (put it in quotes)")
        if not isinstance(t, dict):
            raise ConfigError(f"{where}: expected include: and exclude: lists")
        unknown = sorted(str(k) for k in t if k not in ("include", "exclude", "feeds"))
        if unknown:
            raise ConfigError(f"{where}: unknown {', '.join(unknown)} (only include:, exclude: and feeds:)")
        include, exclude = t.get("include"), [] if t.get("exclude") is None else t["exclude"]
        if not isinstance(include, list) or not include:
            raise ConfigError(f"{where}: include: expected a list with at least one keyword")
        if not isinstance(exclude, list):
            raise ConfigError(f"{where}: exclude: expected a list of keywords")
        feeds = [] if t.get("feeds") is None else t["feeds"]
        if not isinstance(feeds, list):
            raise ConfigError(f"{where}: feeds: expected a list of feed names")
        for f in feeds:
            if f not in known:
                raise ConfigError(f"{where}: feeds: {f!r} is not the name of a feed in feeds:")
        out[name] = (tuple(keyword(k, f"{where}: include") for k in include),
                     tuple(keyword(k, f"{where}: exclude") for k in exclude), frozenset(feeds))
    return out


def candidates_config():
    """The candidates: block of news.yaml over CANDIDATES: max and per_outlet whole numbers from 1,
    no_topic_outlets a whole number from 2 (the outlets a story without a topic needs to get in),
    weights numbers from 0, sections a number from 0 for any of SECTIONS (added to the score of its
    stories). A block that can't be read stops the job, like topics:."""
    raw = config().get("candidates")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"news.yaml candidates: expected max:, per_outlet: and weights:, not a {type(raw).__name__}")
    out = {**CANDIDATES, "weights": dict(CANDIDATES["weights"]), "sections": dict(CANDIDATES["sections"])}
    unknown = sorted(str(k) for k in raw if k not in out)
    if unknown:
        raise ConfigError(f"news.yaml candidates: unknown {', '.join(unknown)} "
                          "(only max:, per_outlet:, no_topic_outlets:, weights:, sections:)")
    for key in ("max", "per_outlet"):
        v = raw.get(key, out[key])
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"news.yaml candidates: {key}: expected a whole number from 1, not {v!r}")
        out[key] = v
    v = raw.get("no_topic_outlets", out["no_topic_outlets"])
    if not isinstance(v, int) or isinstance(v, bool) or v < 2:
        raise ConfigError(f"news.yaml candidates: no_topic_outlets: expected a whole number from 2, not {v!r}")
    out["no_topic_outlets"] = v
    weights = {} if raw.get("weights") is None else raw["weights"]
    if not isinstance(weights, dict):
        raise ConfigError("news.yaml candidates: weights: expected outlets: and fresh: numbers")
    unknown = sorted(str(k) for k in weights if k not in out["weights"])
    if unknown:
        raise ConfigError(f"news.yaml candidates: weights: unknown {', '.join(unknown)} (only outlets:, fresh:)")
    for key, v in weights.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v < float("inf"):
            raise ConfigError(f"news.yaml candidates: weights: {key}: expected a number from 0, not {v!r}")
        out["weights"][key] = float(v)
    sections = {} if raw.get("sections") is None else raw["sections"]
    if not isinstance(sections, dict):
        raise ConfigError(f"news.yaml candidates: sections: expected a number for any of {', '.join(SECTIONS)}")
    for key, v in sections.items():
        if key not in SECTIONS:
            raise ConfigError(f"news.yaml candidates: sections: {key!r} is not a section ({', '.join(SECTIONS)})")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v < float("inf"):
            raise ConfigError(f"news.yaml candidates: sections: {key}: expected a number from 0, not {v!r}")
        out["sections"][key] = float(v)
    return out


def topics_of(headlines, topics, feeds=()) -> list:
    """The topics a story is in, in news.yaml order: one of its headlines (as tokens()) has an
    include keyword and no exclude keyword, or one of its feeds is in the topic's feeds:."""
    def has(headline, keys):
        return any(has_phrase(headline, k) for k in keys)
    return [name for name, (include, exclude, by_feed) in topics.items()
            if any(f in by_feed for f in feeds) or any(has(h, include) and not has(h, exclude) for h in headlines)]


def hn_topic(topics):
    """The topic chip under which the page also shows Hacker News (hackernews: topic: in news.yaml),
    or None."""
    t = (config().get("hackernews") or {}).get("topic")
    if t is not None and t not in topics:
        raise ConfigError(f"news.yaml hackernews: topic: {t!r} is not one of the topics:")
    return t


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


def fold(word: str) -> str:
    """A word in lower case as written, folded for merge_events() a little further than words() does:
    plurals, also "ies" ("Canaries" =
    "Canary"), "es" after ch, sh, x and ss ("catches" = "catch"), and a doubled last letter, as
    names are spelled both ways ("Zelenskyy" = "Zelensky")."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word[:-1] if len(word) > 5 and word[-1] == word[-2] != "s" else word


def event_words(title, summary):
    """For merge_events(): the headline's words (folded), those of them written with a capital (names,
    mostly), and the words of headline and summary with how often each comes (the headline twice)."""
    head = [(fold(w.lower()), w[0].isupper()) for low, w in tokens(title) if len(low) >= 3 and low not in STOP]
    text = [fold(w) for w in words_in_order(summary)]
    return {w for w, _ in head}, {w for w, cap in head if cap}, Counter([w for w, _ in head] * 2 + text)


def words_in_order(text):
    """The words words() keeps, every time they come, as written (lower case, plurals not folded)."""
    out = []
    for w in WORD_RE.findall(text.lower().replace("’", "'")):
        w = w.split("'")[0]
        if len(w) >= 3 and w not in STOP:
            out.append(w)
    return out


def merge_events(groups, rarity):
    """Stories merged when they are the same event told in other words ("SpaceX catches Starship"
    and "SpaceX completes first-ever spaceship capture"), which same_story() misses. Two stories are
    one event when a headline of each shares at least two words with the other, one of them a name
    written with a capital in both (two different elections share "presidential election", not a
    name), and their headline and summary words are alike: a cosine of at least SAME_EVENT, each
    word weighted by how rare it is in this fetch, so "Starshield" counts and "says" hardly does.
    The likest pair first, then again with the merged story, until no pair is left: so the result
    doesn't depend on which item came first. Changes groups in place; they keep their order."""
    bags = {}
    for n, g in enumerate(groups):
        bags[n] = Counter()
        for it in g:
            bags[n].update(it["event"][2])

    def unit(bag):
        v = {w: x * rarity[w] for w, x in bag.items()}
        size = sum(x * x for x in v.values()) ** 0.5 or 1.0
        return {w: x / size for w, x in v.items()}

    def alike(a, b):
        """The cosine of stories a and b, or None when no headlines of theirs share a name and a word more."""
        if not any(len(x["event"][0] & y["event"][0]) >= 2 and x["event"][0] & y["event"][0] & x["event"][1]
                   & y["event"][1] for x in live[a] for y in live[b]):
            return None
        return sum(x * units[b].get(w, 0.0) for w, x in units[a].items())

    live = dict(enumerate(groups))           # story number -> its items; a merged story's number goes
    units = {n: unit(b) for n, b in bags.items()}
    by_name = {}                             # only stories whose headlines share a name can be one event
    for n, g in live.items():
        for w in {w for it in g for w in it["event"][1]}:
            by_name.setdefault(w, set()).add(n)
    pairs = {}
    for ns in by_name.values():
        for a in ns:
            for b in ns:
                if a < b and (a, b) not in pairs:
                    pairs[(a, b)] = alike(a, b)
    while True:
        ok = [(like, -a, -b) for (a, b), like in pairs.items() if like is not None and like >= SAME_EVENT]
        if not ok:
            break
        _, a, b = max(ok)                   # the likest pair; a tie goes to the earliest stories
        a, b = -a, -b
        live[a].extend(live.pop(b))
        bags[a].update(bags.pop(b))
        units.pop(b)
        units[a] = unit(bags[a])
        pairs = {k: v for k, v in pairs.items() if b not in k}
        for n in live:
            if n != a:
                k = (min(a, n), max(a, n))
                if k in pairs or any(w in {w for it in live[n] for w in it["event"][1]}
                                     for it in live[a] for w in it["event"][1]):
                    pairs[k] = alike(*k)
    groups[:] = [live[n] for n in sorted(live)]


def section_of(feed) -> str:
    """The feed's region in news.yaml (Europe, US or World); anything else is World."""
    region = str(feed.get("region") or "World")
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
            summary = str(it.get("summary") or "")[:280]
            out.append({"title": title, "url": url[:500], "summary": summary,
                        "outlet": str(meta.get("outlet") or name)[:40], "section": section_of(meta), "feed": name,
                        "at": at, "order": order, "words": words(title), "tokens": tokens(title),
                        "event": event_words(title, summary)})
    return sorted(out, key=lambda i: i["at"], reverse=True)


def stories(now, topics, links=False):
    """Items grouped into stories, most-covered first, then newest. A story's headline comes from
    the outlet listed first in news.yaml (public broadcasters lead); its section is where most of
    its items are; its topics are those of the headlines it shows, plus those whose feeds: list a
    feed any of its items came from. With links, each also has "links": the link of every item in
    it, also those the page doesn't show (a second article from one outlet), and "articles": each of
    them with its outlet and headline, lead first, for --eval-top.
    An item joins the first story whose headlines share enough words with its own (same_story()),
    else it starts one; then stories that are one event are merged (merge_events())."""
    found = items(now)
    groups = []
    for it in found:
        home = next((g for g in groups if any(same_story(it["words"], o["words"]) for o in g)), None) \
            if len(it["words"]) >= 3 else None
        if home is None:
            groups.append([it])
        else:
            home.append(it)
    seen_in = Counter(w for it in found for w in it["event"][2])
    merge_events(groups, {w: math.log(len(found) / n) for w, n in seen_in.items()})   # 0: a word in every item
    out = []
    for g in groups:
        lead = min(g, key=lambda i: (i["order"], -i["at"].timestamp()))
        per_outlet = {}
        for it in sorted(g, key=lambda i: (i["order"], -i["at"].timestamp())):
            per_outlet.setdefault(it["outlet"], it)   # one line per outlet: its newest in its first feed
        sections = [it["section"] for it in per_outlet.values()]
        shown = [it["tokens"] for it in per_outlet.values()]
        tagged = topics_of(shown, topics, sorted({it["feed"] for it in g}))
        by_keyword = topics_of(shown, topics)
        out.append({"title": lead["title"], "summary": lead["summary"], "url": lead["url"],
                    # where most of its outlets file it; a tie goes to the named place over World
                    "section": max(SECTIONS, key=lambda s: (sections.count(s), s != "World", s == lead["section"])),
                    "topics": tagged,
                    # those it has only as a story of a topic's feeds:, no keyword (not enough for candidates)
                    "topics_by_feed": [t for t in tagged if t not in by_keyword],
                    "feeds": sorted({it["feed"] for it in g}),   # every feed it came from
                    "at": max(it["at"] for it in g).isoformat(timespec="seconds"),
                    "outlets": list(per_outlet),
                    "items": [{"outlet": it["outlet"], "title": it["title"], "url": it["url"],
                               "at": it["at"].isoformat(timespec="seconds")} for it in per_outlet.values()]})
        if links:
            out[-1]["links"] = sorted({it["url"] for it in g})
            out[-1]["articles"] = [{"outlet": it["outlet"], "title": it["title"], "url": it["url"]}
                                   for it in sorted(g, key=lambda i: (i["order"], -i["at"].timestamp()))]
    out.sort(key=lambda s: (len(s["outlets"]), s["at"]), reverse=True)   # most-covered first, then newest
    return out


def per_section(found):
    """The first PER_SECTION stories of each section."""
    kept, count = [], {}
    for s in found:
        count[s["section"]] = count.get(s["section"], 0) + 1
        if count[s["section"]] <= PER_SECTION:
            kept.append(s)
    return kept


def candidates(found, cfg, fetched):
    """The day's shortlist from all stories (not only those the sections keep): those with a topic
    by keyword or covered by no_topic_outlets outlets or more (what many write about needs no keyword;
    cfg without it: 2), best first, at
    most max. A topic only from feeds: (every story of a feed) isn't enough: those feeds also carry
    consumer tips and features. The score is weights.outlets per outlet covering the story plus
    weights.fresh x freshness (1 at fetch time, 0 at MAX_AGE_HOURS old): with outlets 1 and fresh
    under 1, one more outlet beats any age, and the same outlets go newest first. sections adds its
    number to the stories of that section (a lean towards Europe: a head start of that many points,
    one outlet being 1 with the weights as above). A tie goes to
    the newer, then the link, so the same feeds always give the same list and scores. Exact
    fractions of the weights as written (0.1 is 1/10): a tie is a tie, not a rounding.
    per_outlet spreads the outlets: only that many stories led by one outlet (the first of its
    outlets) come in score order; the ones over it fill any room left at the end, in score order."""
    outlets, fresh = (Fraction(str(cfg["weights"][k])) for k in ("outlets", "fresh"))
    lean = {k: Fraction(str(v)) for k, v in (cfg.get("sections") or {}).items()}
    ref, window = int(fetched.timestamp()), MAX_AGE_HOURS * 3600

    def at(s):
        return int(datetime.fromisoformat(s["at"]).timestamp())

    def score(s):
        return outlets * len(s["outlets"]) + fresh * (1 - Fraction(ref - at(s), window)) + lean.get(s.get("section"), 0)
    ranked = sorted(((score(s), s) for s in found
                     if set(s["topics"]) - set(s.get("topics_by_feed") or ())
                     or len(s["outlets"]) >= cfg.get("no_topic_outlets", 2)), key=lambda p: (-p[0], -at(p[1]), p[1]["url"]))
    spread, over, led = [], [], {}
    for p in ranked:
        lead = p[1]["outlets"][0]
        led[lead] = led.get(lead, 0) + 1
        (spread if led[lead] <= cfg["per_outlet"] else over).append(p)
    return [{**s, "score": round(float(sc), 4)} for sc, s in (spread + over)[:cfg["max"]]]


def hacker_news():
    rows = ((latest("hackernews") or {}).get("data") or {}).get("stories") or []
    out = [{"title": str(r.get("title") or "")[:300], "url": u, "score": int(r.get("score") or 0),
            "comments": int(r.get("comments") or 0)}
           for r in rows if isinstance(r, dict) and r.get("title")
           and (u := str(r.get("url") or "")).startswith("https://")]
    return sorted(out, key=lambda r: -r["score"])[:15]


def build(now=None):
    """The page news.json gets; writes nothing."""
    now = now or datetime.now(timezone.utc)
    topics, shortlist = topics_config(), candidates_config()   # first: a broken block stops the run
    rss = latest("rss") or {}
    try:
        health = json.loads((FEEDS_DIR / "health.json").read_text())
    except (OSError, ValueError):
        health = {}
    every = stories(now, topics)
    found = per_section(every)
    fetched = parse_time(rss.get("fetched")) or now   # freshness as of the fetch: the same feeds, the same scores
    return {"generated": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "fetched": rss.get("fetched"), "sections": list(SECTIONS), "topics": list(topics),
            "hn_topic": hn_topic(topics),
            "outlets": sorted({o for s in found for o in s["outlets"]}),
            "failed_feeds": sorted(rss.get("errors") or {}), "stories": found,
            "candidates": candidates(every, shortlist, fetched), "hn": hacker_news(),
            "fetch": {"updated": health.get("updated"),
                      "failed": sorted(k for k, v in (health.get("sources") or {}).items()
                                       if not v.get("ok") and k in ("rss", "hackernews"))}}


def run(now=None, write=True):
    """Builds the page; with write, writes news.json."""
    page = build(now)
    if write:
        agentdb._atomic_write_text(agentdb.WEB_DIR / "news.json", json.dumps(page, ensure_ascii=False))
    return page


# ---------- evaluation: topics against hand-labelled headlines ----------

EVAL_HEADER = """\
# Stories from one fetch, for `news.py --eval`. Set "expected" to the topics each story SHOULD have
# (names as in news.yaml topics:, [] for none); "topics" is what it got when this file was written.
# A story's topics come from all its headlines: the one above and those under "also", plus any
# topic whose feeds: list one of its "feeds".
# Then change keywords in news.yaml and run --eval: it lists what changed and what is still wrong.
# Stories without an "expected" list are skipped. Written readable by you only, like triage's.
"""


def export_eval(path):
    """The latest fetch's stories as cases to label (writes only `path`: not news.json, not agent.db).
    "expected" starts as the topics they get now, so you only fix the wrong ones."""
    topics = topics_config()
    rss = latest("rss") or {}
    fetched = parse_time(rss.get("fetched"))
    if not fetched:
        raise ConfigError(f"no saved rss fetch in {FEEDS_DIR / 'rss'}")
    cases = []
    for s in stories(fetched, topics):       # as of the fetch, so an export a day later still has them all
        case = {"headline": s["title"], "outlet": s["outlets"][0], "section": s["section"],
                "topics": s["topics"], "expected": list(s["topics"]), "feeds": s["feeds"]}
        if len(s["items"]) > 1:
            case["also"] = [{"outlet": i["outlet"], "headline": i["title"]} for i in s["items"][1:]]
        cases.append(case)
    text = EVAL_HEADER + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=100,
                                        default_flow_style=None)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(text)                        # created 0600, never readable by others, even for a moment
    path.chmod(0o600)                        # and an older file with wider permissions is narrowed too
    print(f"{len(cases)} stories from the fetch of {rss.get('fetched')} written to {path}. "
          "Fix 'expected' where the topics are wrong, then run --eval.")


def run_eval(path):
    """Tags each labelled story again with the current news.yaml; prints what changed since the
    export and what doesn't match "expected". 1 if any is wrong. Writes nothing."""
    topics = topics_config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: expected a list of stories, as --export-eval writes")
    cases = [c for c in raw if isinstance(c, dict) and isinstance(c.get("headline"), str)
             and isinstance(c.get("expected"), list) and all(isinstance(t, str) for t in c["expected"])]
    changed, wrong, tally = [], [], {t: Counter() for t in topics}

    def names(ts):
        return "[" + ", ".join(ts) + "]"
    for c in cases:
        heads = [c["headline"]] + [str(a.get("headline") or "") for a in c.get("also") or [] if isinstance(a, dict)]
        feeds = [f for f in c.get("feeds") or [] if isinstance(f, str)]   # for topics: feeds:
        got = topics_of([tokens(h) for h in heads], topics, feeds)
        what = f"{c['headline']}  ({c.get('outlet', '?')})"
        if isinstance(c.get("topics"), list) and set(got) != set(c["topics"]):
            changed.append(f"  {names(c['topics'])} -> {names(got)}  {what}")
        for t in topics:
            tally[t]["right" if t in got and t in c["expected"] else "extra" if t in got else
                     "missed" if t in c["expected"] else "none"] += 1
        if set(got) != set(c["expected"]):
            extra, missed = [t for t in got if t not in c["expected"]], [t for t in c["expected"] if t not in got]
            wrong.append(f"  expected {names(c['expected'])}, got {names(got)}" +
                         (f"; extra {', '.join(extra)}" if extra else "") +
                         (f"; missed {', '.join(missed)}" if missed else "") + f"\n      {what}")
    print(f"{len(cases)} labelled stories from {path}" +
          (f" ({len(raw) - len(cases)} skipped: no headline or no 'expected' list)" if len(raw) > len(cases) else ""))
    unknown = sorted({t for c in cases for t in c["expected"]} - set(topics))
    if unknown:
        print(f"Not topics in news.yaml, so they can never match: {', '.join(unknown)}")
    if changed:
        print(f"\nChanged since the export ({len(changed)}):", *changed, sep="\n")
    if wrong:
        print(f"\nWrong ({len(wrong)}):", *wrong, sep="\n")
    print("\nPer topic: " + "; ".join(f"{t} {n['right']} right, {n['extra']} extra, {n['missed']} missed"
                                      for t, n in tally.items() if n["right"] or n["extra"] or n["missed"]))
    right = len(cases) - len(wrong)
    print(f"{right} of {len(cases)} right" + (f" ({round(right * 100 / len(cases))}%)." if cases else "."))
    return 1 if wrong else 0


# ---------- evaluation: the Top list against hand-labelled keep/drop ----------

TOP_N = 10                  # the first screenful of Top on the phone
KEEP_WORDS = {"yes": True, "keep": True, "true": True, "no": False, "drop": False, "false": False}


def read_labels(path):
    """{url: (keep, headline)} from a labels file (a list of stories, each with url: and keep: yes
    or no), and how many entries were skipped for lacking either. The same link labelled both keep
    and drop stops the run: which one is meant can't be guessed."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: expected a list of stories, each with url: and keep: yes or no")
    labels, skipped = {}, 0
    for c in raw:
        c = c if isinstance(c, dict) else {}
        keep, url = c.get("keep"), c.get("url")
        if isinstance(keep, str):
            keep = KEEP_WORDS.get(keep.strip().lower())
        if not isinstance(keep, bool) or not isinstance(url, str) or not url.strip():
            skipped += 1
            continue
        url = url.strip()
        if url in labels and labels[url][0] != keep:
            raise ConfigError(f"{path}: {url} is labelled both keep and drop")
        labels[url] = (keep, str(c.get("headline") or url))
    return labels, skipped


def top_as_of_fetch():
    """The latest fetch's Top list as news-build gave it right after the fetch (now = the fetch time),
    each story with all its links and articles: (the fetch, its time, every story, the whole ranking
    not cut at max, the list). For --eval-top here and in news_picks.py: the same on any later day."""
    topics, shortlist = topics_config(), candidates_config()
    rss = latest("rss") or {}
    fetched = parse_time(rss.get("fetched"))
    if not fetched:
        raise ConfigError(f"no saved rss fetch in {FEEDS_DIR / 'rss'}")
    every = stories(fetched, topics, links=True)
    # the whole order, not cut at max: its first max are exactly the candidates build() gives, and
    # the rest tell how far below the cut a missed keep came
    ranked = candidates(every, {**shortlist, "max": max(len(every), 1)}, fetched)
    return rss, fetched, every, ranked, ranked[:shortlist["max"]]


def label_of(story, labels) -> str:
    """keep, drop or unlabelled: keep when any of the story's links has keep (a drop on another of its
    articles is a copy of a story already kept), drop when only drops, unlabelled when none."""
    got = {labels[u][0] for u in story["links"] if u in labels}
    return "keep" if True in got else "drop" if got else "unlabelled"


def eval_top(path):
    """How the Top list (candidates) of the latest fetch fares against keep/drop labels: keeps,
    drops and unlabelled in the top TOP_N and in the whole list, the keeps it misses (and why),
    the drops it has; then the whole list with ranks, and every story grouped from several
    articles, each article with its outlet and label, to check by hand. Ranked as of the fetch, so
    it gives the same numbers on any later day. A story counts as labelled when any of its links
    (every article grouped into it) has a label, and as a keep when any has keep: a drop on one of
    its articles is then a copy of a story already kept, not the event unwanted.
    Writes nothing; 0 unless the input can't be used."""
    labels, skipped = read_labels(path)
    shortlist = candidates_config()
    rss, fetched, every, ranked, top = top_as_of_fetch()
    rank = {s["url"]: n for n, s in enumerate(ranked, 1)}

    def label(s):
        return label_of(s, labels)

    def line(s):
        return f"{s['title']}  ({', '.join(s['outlets'])})"

    kinds = ["keep", "drop", "unlabelled"]
    rows = [(f"top {TOP_N}", Counter(label(s) for s in top[:TOP_N])), (f"all {len(top)}", Counter(label(s) for s in top))]
    totals = Counter(label(s) for s in every)
    matched = {u for s in every for u in s["links"]}
    unmatched = [(u, head) for u, (_, head) in labels.items() if u not in matched]

    print(f"Top as of the fetch of {rss.get('fetched')}: {len(top)} stories (max {shortlist['max']}). "
          f"{len(labels)} labels from {path}" + (f" ({skipped} skipped: no url or no keep: yes/no)" if skipped else "")
          + f"; labelled stories in this fetch: {totals['keep']} keep, {totals['drop']} drop.")
    width = max(len(r[0]) for r in rows)
    print("\n  " + " " * width + "".join(f"  {k:>10}" for k in kinds))
    for name, n in rows:
        print(f"  {name:<{width}}" + "".join(f"  {n[k]:>10}" for k in kinds))
    print(f"\nKeeps in the top {TOP_N}: {rows[0][1]['keep']} of {totals['keep']}; "
          f"in the whole list: {rows[1][1]['keep']} of {totals['keep']}.")
    # by label, not story: the same count however the items are grouped, to compare groupings
    first, whole = ({u for s in part for u in s["links"]} for part in (top[:TOP_N], top))
    keeps, drops = ([u for u, (k, _) in labels.items() if k is want] for want in (True, False))
    print(f"Labels by link (however stories are grouped): keep {len(first & set(keeps))} of {len(keeps)} in the "
          f"top {TOP_N}, {len(whole & set(keeps))} in the whole list; drop {len(first & set(drops))} of {len(drops)} "
          f"in the top {TOP_N}, {len(whole & set(drops))} in the whole list.")

    missed = [s for s in every if label(s) == "keep" and s["url"] not in {t["url"] for t in top}]
    missed.sort(key=lambda s: rank.get(s["url"], len(ranked) + 1))   # nearest the cut first, then no topic
    why = {s["url"]: f"#{rank[s['url']]}, below the cut" if s["url"] in rank else
           "topic only from feeds:" if s["topics"] else "no topic" for s in missed}
    if missed:
        w = max(len(v) for v in why.values())
        print(f"\nKeeps not in the Top ({len(missed)}):", *(f"  {why[s['url']]:<{w}}  {line(s)}" for s in missed),
              sep="\n")
    dropped = [(n, s) for n, s in enumerate(top, 1) if label(s) == "drop"]
    if dropped:
        print(f"\nDrops in the Top ({len(dropped)}):", *(f"  #{n:<3} {line(s)}" for n, s in dropped), sep="\n")
    blank = [(n, s) for n, s in enumerate(top, 1) if label(s) == "unlabelled"]
    if blank:
        print(f"\nUnlabelled in the Top ({len(blank)}):", *(f"  #{n:<3} {line(s)}" for n, s in blank), sep="\n")
    print(f"\nThe list ({len(top)}), best first:",
          *(f"  #{n:<3} {label(s):<10} {s['score']:.3f}  {s['section']:<6}  {line(s)}" for n, s in enumerate(top, 1)),
          sep="\n")
    merged = sorted((s for s in every if len(s["links"]) > 1), key=lambda s: (rank.get(s["url"], len(ranked) + 1),
                                                                          s["url"]))

    def mark(u):
        return "keep" if labels.get(u, (None,))[0] is True else "drop" if u in labels else "-"
    if merged:
        print(f"\nStories grouped from several articles ({len(merged)}), each article with its label:")
        for s in merged:
            where = f"#{rank[s['url']]}" + ("" if rank[s["url"]] <= len(top) else ", below the cut") \
                if s["url"] in rank else "not a candidate"
            print(f"  {where}: {label(s)}, {len(s['outlets'])} outlet{'s' * (len(s['outlets']) > 1)}, "
                  f"{len(s['links'])} articles", *(f"      {mark(a['url']):<4}  {a['outlet']}: {a['title']}"
                                                  for a in s["articles"]), sep="\n")
    if unmatched:
        print(f"\nLabels that match no story in this fetch ({len(unmatched)}): from another fetch, or more than "
              f"{MAX_AGE_HOURS} hours old at it", *(f"  {head}" for _, head in unmatched), sep="\n")
    return 0


def quiet_on_cut_output(run):
    """run(), and when the reader of stdout goes away (news.py --eval-top ... | head), stop quietly:
    no traceback, exit 0 (what was read is all that was wanted). The rest of stdout goes to /dev/null,
    so the flush at exit can't fail again."""
    try:
        try:
            return run()
        finally:                  # also on sys.exit(): what's left in the buffer goes now, not at exit
            sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


def main():
    quiet_on_cut_output(_main)


def _main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    one = ap.add_mutually_exclusive_group()
    one.add_argument("--print", action="store_true", help="print only; write nothing")
    one.add_argument("--export-eval", metavar="FILE", type=Path,
                     help="write the latest fetch's stories as test cases to label by hand")
    one.add_argument("--eval", metavar="FILE", type=Path, help="check the current topics against labelled stories")
    one.add_argument("--eval-top", metavar="FILE", type=Path,
                     help="count keep/drop labels in the Top list of the latest fetch, ranked as of the fetch")
    args = ap.parse_args()
    try:
        if args.export_eval:
            return export_eval(args.export_eval)
        if args.eval:
            sys.exit(run_eval(args.eval))
        if args.eval_top:
            sys.exit(eval_top(args.eval_top))
    except ConfigError as e:
        sys.exit(str(e))
    try:
        page = run(write=not args.print)
    except ConfigError as e:
        sys.exit(str(e))
    top = [s for s in page["stories"] if len(s["outlets"]) > 1]
    print(f"{len(page['stories'])} stories from {len(page['outlets'])} outlets; {len(top)} covered by several; "
          f"{len(page['candidates'])} candidates")
    for s in top[:15]:
        print(f"  [{s['section']}] {s['title']}  ({', '.join(s['outlets'])})")
    if not page["stories"]:
        sys.exit("no fresh stories in feeds/rss")


if __name__ == "__main__":
    main()
