#!/usr/bin/env python3
"""News: the fetched feeds -> web/news.json for the News tab.

Runs right after each successful news-fetch, with no network at all and no model. Code groups
the same story from different outlets (by the words in the headlines), so the tab can show one
story with its outlets instead of five near-identical headlines, each tagged with the topics: of
news.yaml whose keywords its headlines carry, plus the day's shortlist (candidates: in news.yaml).
Headlines are untrusted text: they are cut to length, only https:// links are kept, and the page
shows them as plain text.

Usage:
  news.py                          # write web/news.json
  news.py --print                  # print the stories several outlets cover, write nothing
  news.py --export-eval eval.yaml  # the latest fetch's stories as test cases; label their topics by hand
  news.py --eval eval.yaml         # how the current news.yaml topics tag them (exit 1 if any is wrong)
"""
import argparse
import json
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
CANDIDATES = {"max": 40, "per_outlet": 8, "weights": {"outlets": 1.0, "fresh": 0.5}}
SECTIONS = ("Europe", "US", "World")   # by region only; the subject is the topics: of news.yaml
SAME_STORY = 0.34           # share of headline words two items must have in common
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
    weights numbers from 0. A block that can't be read stops the job, like topics:."""
    raw = config().get("candidates")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"news.yaml candidates: expected max:, per_outlet: and weights:, not a {type(raw).__name__}")
    out = {**CANDIDATES, "weights": dict(CANDIDATES["weights"])}
    unknown = sorted(str(k) for k in raw if k not in out)
    if unknown:
        raise ConfigError(f"news.yaml candidates: unknown {', '.join(unknown)} (only max:, per_outlet:, weights:)")
    for key in ("max", "per_outlet"):
        v = raw.get(key, out[key])
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"news.yaml candidates: {key}: expected a whole number from 1, not {v!r}")
        out[key] = v
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
            out.append({"title": title, "url": url[:500], "summary": str(it.get("summary") or "")[:280],
                        "outlet": str(meta.get("outlet") or name)[:40], "section": section_of(meta), "feed": name,
                        "at": at, "order": order, "words": words(title), "tokens": tokens(title)})
    return sorted(out, key=lambda i: i["at"], reverse=True)


def stories(now, topics):
    """Items grouped into stories, most-covered first, then newest. A story's headline comes from
    the outlet listed first in news.yaml (public broadcasters lead); its section is where most of
    its items are; its topics are those of the headlines it shows, plus those whose feeds: list a
    feed any of its items came from."""
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
                    "topics": topics_of([it["tokens"] for it in per_outlet.values()], topics,
                                        sorted({it["feed"] for it in g})),
                    "feeds": sorted({it["feed"] for it in g}),   # every feed it came from
                    "at": max(it["at"] for it in g).isoformat(timespec="seconds"),
                    "outlets": list(per_outlet),
                    "items": [{"outlet": it["outlet"], "title": it["title"], "url": it["url"],
                               "at": it["at"].isoformat(timespec="seconds")} for it in per_outlet.values()]})
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
    """The day's shortlist from all stories (not only those the sections keep): those with a topic,
    best first, at most max. The score is weights.outlets per outlet covering the story plus
    weights.fresh x freshness (1 at fetch time, 0 at MAX_AGE_HOURS old): with outlets 1 and fresh
    under 1, one more outlet beats any age, and the same outlets go newest first. A tie goes to
    the newer, then the link, so the same feeds always give the same list and scores. Exact
    fractions of the weights as written (0.1 is 1/10): a tie is a tie, not a rounding.
    per_outlet spreads the outlets: only that many stories led by one outlet (the first of its
    outlets) come in score order; the ones over it fill any room left at the end, in score order."""
    outlets, fresh = (Fraction(str(cfg["weights"][k])) for k in ("outlets", "fresh"))
    ref, window = int(fetched.timestamp()), MAX_AGE_HOURS * 3600

    def at(s):
        return int(datetime.fromisoformat(s["at"]).timestamp())

    def score(s):
        return outlets * len(s["outlets"]) + fresh * (1 - Fraction(ref - at(s), window))
    ranked = sorted(((score(s), s) for s in found if s["topics"]), key=lambda p: (-p[0], -at(p[1]), p[1]["url"]))
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    one = ap.add_mutually_exclusive_group()
    one.add_argument("--print", action="store_true", help="print only; write nothing")
    one.add_argument("--export-eval", metavar="FILE", type=Path,
                     help="write the latest fetch's stories as test cases to label by hand")
    one.add_argument("--eval", metavar="FILE", type=Path, help="check the current topics against labelled stories")
    args = ap.parse_args()
    try:
        if args.export_eval:
            return export_eval(args.export_eval)
        if args.eval:
            sys.exit(run_eval(args.eval))
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
