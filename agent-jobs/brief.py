#!/usr/bin/env python3
"""Morning brief: calendar (.ics) + Obsidian to-do file + email digest -> brief.md (+ optional ntfy).

No AI model involved: everything here is plain parsing, so the brief is the same every time
for the same files. It warns you when the calendar or to-do file looks out of date.

Usage:
  brief.py               # write brief.md (and push to ntfy if NTFY_URL is set)
  brief.py --print       # print the brief, write nothing, send nothing
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import agentdb  # the agent page; does nothing unless AGENT_DB is set
import icalendar
import recurring_ical_events

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/London"))
CAL_DIR = Path(os.path.expanduser(os.environ.get("CAL_DIR", "~/Sync/calendar")))
TODO_FILES = [Path(os.path.expanduser(p)) for p in
              os.environ.get("TODO_FILES", "~/Obsidian/Tasks/To-do.md").split(":") if p]
EMAIL_DIGEST = Path(os.path.expanduser(os.environ.get("EMAIL_DIGEST", "~/email-agent/digest.jsonl")))
EMAIL_PUSHED = EMAIL_DIGEST.with_name("pushed.jsonl")  # written by triage: urgent/work alerts already pushed
PUSHED_HOURS = int(os.environ.get("PUSHED_HOURS", "24"))
OUT_FILE = Path(os.path.expanduser(os.environ.get("BRIEF_FILE", "~/agent-data/brief.md")))
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "7"))
# The page opens this when you tap a task, e.g. "obsidian://open?vault=MyVault&file=Tasks%2FTo-do".
TASKS_LINK = os.environ.get("TASKS_LINK", "")
UNDATED_SHOWN = int(os.environ.get("UNDATED_SHOWN", "40"))  # the page shows 5 and a "Show all" button
STALE_HOURS = int(os.environ.get("STALE_HOURS", "36"))
# Extra .ics files read every time (e.g. UK bank holidays); all *.ics in this folder are used.
EXTRA_ICS_DIR = Path(os.path.expanduser(os.environ.get("EXTRA_ICS_DIR", "~/agent/extra-calendars")))
# "Later" section: events after the next LOOKAHEAD_DAYS and up to LATER_DAYS ahead,
# shown only if the title contains one of these words.
LATER_DAYS = int(os.environ.get("LATER_DAYS", "45"))
LATER_KEYWORDS = [k.strip().lower() for k in os.environ.get(
    "LATER_KEYWORDS",
    "flight,appointment,doctor,dentist,barber,interview,birthday,holiday,deadline,exam,wedding",
).split(",") if k.strip()]
# From this day of the month on, preview next month's events and dated tasks.
MONTH_PREVIEW_FROM_DAY = int(os.environ.get("MONTH_PREVIEW_FROM_DAY", "20"))
# Time tracking: the newest SimpleTimeTracker CSV export in this folder (synced by Syncthing).
TIME_DIR = Path(os.path.expanduser(os.environ.get("TIME_DIR", "~/Sync/timetracker")))
# Old activity names folded into new ones, optionally adding a tag, e.g. after merging activities:
# "Calisthenics=Exercise:Calisthenics; Sport/Gym=Exercise:Gym". Separate entries with ";".
TIME_ACTIVITY_MAP = {
    old.strip(): (new.partition(":")[0].strip(), new.partition(":")[2].strip())
    for old, _, new in (e.partition("=") for e in os.environ.get("TIME_ACTIVITY_MAP", "").split(";"))
    if old.strip() and new.strip()
}
# Old tag names shown under their new name, e.g. "Sport/Gym=Gym". Separate entries with ";".
TIME_TAG_MAP = {
    old.strip(): new.strip()
    for old, _, new in (e.partition("=") for e in os.environ.get("TIME_TAG_MAP", "").split(";"))
    if old.strip() and new.strip()
}
# Weekly goals per activity, e.g. "Computer Science=10h; Exercise=3h; Reading=2h". The brief shows
# progress bars, whether you're on pace, and counts them in "Wins" / "Slipping". Empty: no goals.
TIME_GOALS = os.environ.get("TIME_GOALS", "")
# Local model that writes the "Summary" bullets at the top. Leave OLLAMA_URL empty to skip it.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
MODEL = os.environ.get("MODEL", "qwen3.5:9b")
# Off by default: email summaries are untrusted text, and keeping them out of the brief lets
# a chat model that reads the brief safely have write tools (notes, tasks).
EMAIL_IN_BRIEF = os.environ.get("EMAIL_IN_BRIEF", "no").lower() in ("1", "yes", "true")
# "no": only show the queue and leave it for the 08:00 digest (the brief can then mount it read-only).
EMAIL_CONSUME = os.environ.get("EMAIL_CONSUME", "yes").lower() in ("1", "yes", "true")
# Markets section: web/markets.json from markets.py (run after each news-fetch). Empty: no section.
MARKETS_JSON = os.environ.get("MARKETS_JSON", "")
# news-fetch's health file; the brief warns when a source failed or the fetch stopped running.
FEEDS_HEALTH = os.environ.get("FEEDS_HEALTH", "")
FEEDS_STALE_HOURS = float(os.environ.get("FEEDS_STALE_HOURS", "30"))
MARKET_STALE_DAYS = int(os.environ.get("MARKET_STALE_DAYS", "4"))   # US closes older than this: a warning
# Email triage health file (written by triage.py); the brief warns if triage is broken or stale.
EMAIL_HEALTH = os.environ.get("EMAIL_HEALTH", "")
EMAIL_STALE_HOURS = float(os.environ.get("EMAIL_STALE_HOURS", "2"))
NTFY_URL = os.environ.get("NTFY_URL", "")
# With AGENT_PUSH_LINK set, the push is one line plus a link to the page; "full" sends the whole brief.
BRIEF_PUSH = os.environ.get("BRIEF_PUSH", "link").lower()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()  # a pasted secret often ends in a newline

# Due: 📅 (Obsidian Tasks), also 📆 / 🗓 as phone keyboards insert them, each with or without
# the invisible emoji selector (U+FE0F).
DUE_RE = re.compile(r"(?:[📅📆🗓]️?|\[due::)\s*(\d{4}-\d{2}-\d{2})\]?")
SCHED_RE = re.compile(r"(?:⏳️?|\[scheduled::)\s*(\d{4}-\d{2}-\d{2})\]?")
TASK_RE = re.compile(r"^\s*[-*]\s+\[(.)\]\s+(.*)$")
META_RE = re.compile(r"\s*(?:[📅📆🗓⏳🛫✅➕❌]️?\s*\d{4}-\d{2}-\d{2}|\[\w+::[^\]]*\]|[⏫🔼🔽🔺⏬]|🔁[^📅⏳🛫✅]*)")
HIGH = ("🔺", "⏫")
# Obsidian Tasks priorities, highest first; 3 = no priority set.
PRIORITY = {"🔺": 0, "⏫": 1, "🔼": 2, "🔽": 4, "⏬": 5}
PRIO_ICON = {v: k for k, v in PRIORITY.items()}
STRAY_DATE_RE = re.compile(r"\s*[📅📆🗓⏳🛫]\ufe0f?(?!\s*\d)")   # a date emoji left with no date after it
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*#*$")   # "## Inbox"; the "# To-do" title is not a section
CATEGORY_RE = re.compile(r"\[category::\s*([^\]]*?)\s*\]", re.I)   # written by the QuickAdd capture


# ---------- calendar ----------

def newest_ics(folder: Path):
    files = sorted(folder.glob("*.ics"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _as_date(v):
    return v.date() if isinstance(v, datetime) else v


def fix_broken_repeat(ev):
    """A repeat rule that ends before the event starts (a known Fossify export quirk)
    would hide the event entirely; treat it as a one-off event instead."""
    rule = ev.get("RRULE")
    if not rule or not rule.get("UNTIL") or not ev.get("DTSTART"):
        return
    try:
        if _as_date(rule["UNTIL"][0]) < _as_date(ev.decoded("DTSTART")):
            del ev["RRULE"]
    except Exception:
        pass


def load_events(ics_path: Path, start: datetime, end: datetime):
    """Expand events (including repeats) between start and end.

    Fossify exports sometimes contain repeat rules that end before they start, and
    duplicate UIDs. Each event is expanded on its own, so one bad event is skipped
    instead of breaking the whole calendar.
    """
    cal = icalendar.Calendar.from_ical(ics_path.read_bytes())
    groups = defaultdict(lambda: {"masters": [], "overrides": []})
    for ev in cal.walk("VEVENT"):
        fix_broken_repeat(ev)
        key = str(ev.get("UID", id(ev)))
        groups[key]["overrides" if ev.get("RECURRENCE-ID") else "masters"].append(ev)

    out, skipped = [], 0
    for g in groups.values():
        batches = [[m] for m in g["masters"]] or [[]]
        batches[0] += g["overrides"]  # attach modified occurrences to the first master
        for batch in batches:
            mini = icalendar.Calendar()
            for ev in batch:
                mini.add_component(ev)
            try:
                out.extend(recurring_ical_events.of(mini).between(start, end))
            except Exception:
                skipped += len(batch)
    return out, skipped


# Calendar tasks this many days in the past and not ticked off still show as overdue.
CAL_TASK_OVERDUE_DAYS = int(os.environ.get("CAL_TASK_OVERDUE_DAYS", "14"))


def load_calendar_tasks(ics_path: Path, today: date, end: datetime):
    """Open Fossify tasks (VTODO, date in DTSTART, done = STATUS:COMPLETED) as to-do items.

    A repeating task shows only its next occurrence: Fossify does not export which past
    occurrences were ticked off, so they would all look overdue.
    """
    cal = icalendar.Calendar.from_ical(ics_path.read_bytes())
    start = datetime.combine(today - timedelta(days=CAL_TASK_OVERDUE_DAYS), datetime.min.time(), TZ)
    out, skipped = [], 0
    for todo in cal.walk("VTODO"):
        if str(todo.get("STATUS", "")).upper() == "COMPLETED" or not todo.get("DTSTART"):
            continue
        fix_broken_repeat(todo)
        mini = icalendar.Calendar()
        mini.add_component(todo)
        try:
            occurrences = recurring_ical_events.of(mini, components=["VTODO"]).between(start, end)
        except Exception:
            skipped += 1
            continue
        if todo.get("RRULE"):
            day_of = lambda o: _as_date(to_local(o.decoded("DTSTART")))
            occurrences = sorted((o for o in occurrences if day_of(o) >= today), key=day_of)[:1]
        for occ in occurrences:
            s = to_local(occ.decoded("DTSTART"))
            day = s.date() if isinstance(s, datetime) else s
            title = str(occ.get("SUMMARY", "(no title)")).strip()
            out.append({"text": (f"{s:%H:%M} " if isinstance(s, datetime) else "") + title,
                        "due": day, "high": False, "prio": 3, "section": "", "file": ics_path.name, "line": 0,
                        "title": title, "repeat": bool(todo.get("RRULE"))})
    return out, skipped


def calendar_done(ics_path: Path):
    """One-off Fossify tasks ticked off, as (title, day done). Repeating ones never say which
    occurrence was done, so they are left out."""
    out = []
    for todo in icalendar.Calendar.from_ical(ics_path.read_bytes()).walk("VTODO"):
        when = todo.get("COMPLETED")
        if str(todo.get("STATUS", "")).upper() != "COMPLETED" or not when or todo.get("RRULE"):
            continue
        out.append((str(todo.get("SUMMARY", "(no title)")).strip(), _as_date(to_local(when.dt))))
    return out


def to_local(value):
    if isinstance(value, datetime):
        return value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    return value  # all-day date


def event_rows(events):
    rows = []
    for ev in events:
        s = to_local(ev.decoded("DTSTART"))
        e = to_local(ev.decoded("DTEND")) if ev.get("DTEND") else None
        all_day = not isinstance(s, datetime)
        day = s if all_day else s.date()
        rows.append({
            "day": day,
            "sort": datetime.combine(day, datetime.min.time(), TZ) if all_day else s,
            "time": "All day" if all_day else s.strftime("%H:%M") + (f"–{e:%H:%M}" if e and e != s else ""),
            "title": str(ev.get("SUMMARY", "(no title)")).strip(),
            "where": str(ev.get("LOCATION", "")).strip(),
            "end": e if (not all_day and isinstance(e, datetime)) else None,
        })
    rows.sort(key=lambda r: (r["sort"], r["title"]))
    # drop exact duplicates (same title at same time), common with duplicate UIDs
    seen, unique = set(), []
    for r in rows:
        k = (r["sort"], r["title"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    return unique


# ---------- tasks ----------

def file_lines(p: Path):
    """(section, line, line number) for every line of a to-do file. section is the headings above
    the line; a heading line comes with its own path."""
    section, heads = "", {}
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        h = HEADING_RE.match(line)
        if h:
            # "## Computer science" then "### Server" -> "Computer science › Server"
            level = len(h.group(1))
            heads = {k: v for k, v in heads.items() if k < level}
            heads[level] = h.group(2)
            section = " › ".join(heads[k] for k in sorted(heads))
        yield section, line, n


def task_lines(p: Path):
    """(box, text, list, line number) for every task in a to-do file. box is what's in the
    brackets: " " open, "x" done, "-" cancelled. list is the headings above it."""
    for section, line, n in file_lines(p):
        m = TASK_RE.match(line)
        if m:
            yield m.group(1), m.group(2), section, n


def category_path(name: str, sections) -> str:
    """[category:: ### Server] -> "Computer science › Server", the path the file's headings give,
    so it matches the list of tasks seen open. A name no heading has (renamed) is kept as written."""
    name = name.lstrip("#").strip()
    for path in sections:
        if name.lower() in (path.lower(), path.split(" › ")[-1].lower()):
            return path
    return name


def task_text(text: str) -> str:
    """The task without its dates, priority and repeat markers."""
    return STRAY_DATE_RE.sub("", META_RE.sub("", text)).strip()


def task_prio(text: str) -> int:
    return min((v for k, v in PRIORITY.items() if k in text), default=3)


def parse_tasks(paths):
    tasks, missing = [], []
    for p in paths:
        if not p.exists():
            missing.append(str(p))
            continue
        for box, text, section, n in task_lines(p):
            if box != " ":
                continue  # done [x] / cancelled [-]
            due = DUE_RE.search(text) or SCHED_RE.search(text)
            prio = task_prio(text)
            tasks.append({
                "text": task_text(text),
                "due": date.fromisoformat(due.group(1)) if due else None,
                "high": prio <= 1, "prio": prio, "section": section,
                "file": p.name, "line": n,
            })
    return tasks, missing


DONE_RE = re.compile(r"✅️?\s*(\d{4}-\d{2}-\d{2})")


def done_by_day(paths):
    """Tasks ticked off per day (Obsidian Tasks adds '✅ date'), over the whole file."""
    days = Counter()
    for p in paths:
        if not p.exists():
            continue
        for box, text, _, _ in task_lines(p):
            d = DONE_RE.search(text)
            if box.lower() == "x" and d:
                days[date.fromisoformat(d.group(1))] += 1
    return days


# ---------- task history (agent.db keeps one row per task: added, done, deleted) ----------

def task_key(text: str) -> str:
    """What identifies a task from one day to the next: its words, without dates or icons."""
    return re.sub(r"\s+", " ", task_text(text)).lower()


def task_history(paths, cal_open, cal_done):
    """Open and ticked-off tasks as rows for agentdb.record_history. A task moved under "## Done"
    has lost its list; the history remembers the list from the days it was open, or, for one added
    and done between two syncs, takes its [category:: X] field. Calendar tasks count under
    "Calendar"; repeating ones are routines and are left out."""
    open_rows, done_rows = [], []
    for p in paths:
        sections = [sec for sec, line, _ in file_lines(p) if HEADING_RE.match(line)]
        for box, text, section, _ in task_lines(p):
            due = DUE_RE.search(text) or SCHED_RE.search(text)
            # The heading wins: the field says where the task was captured, not where it was moved to.
            field = CATEGORY_RE.search(text)
            lst = (section if section and section.lower() != "done"
                   else category_path(field.group(1), sections) if field else "")
            row = {"key": task_key(text), "text": task_text(text), "priority": task_prio(text),
                   "list": lst, "due": due.group(1) if due else None}
            done = DONE_RE.search(text)
            if box == " ":
                open_rows.append(row)
            elif box.lower() == "x" and done:
                done_rows.append({**row, "done": done.group(1)})
    for t in cal_open:
        if not t["repeat"]:
            open_rows.append({"key": "calendar: " + task_key(t["title"]), "text": t["title"], "priority": 3,
                              "list": "Calendar", "due": t["due"].isoformat()})
    for title, day in cal_done:
        done_rows.append({"key": "calendar: " + task_key(title), "text": title, "priority": 3,
                          "list": "Calendar", "due": None, "done": day.isoformat()})
    return {"open": open_rows, "done": done_rows}


def done_counts(done_days, today: date):
    """Tasks ticked off in the last 7 days vs the 7 before."""
    recent = before = 0
    for day, n in done_days.items():
        age = (today - day).days
        if 0 <= age < 7:
            recent += n
        elif 7 <= age < 14:
            before += n
    return recent, before


# ---------- time tracking (SimpleTimeTracker CSV export) ----------
# Header written by the app: activity name,time started,time ended,comment,categories,
# record tags,duration,duration minutes. Times are local "yyyy-MM-dd HH:mm:ss", UTC
# "yyyy-MM-ddTHH:mm:ssZ" or with an offset, depending on the export setting.

def parse_stt_time(value: str):
    value = value.strip()
    if value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


TAG_VALUE_RE = re.compile(r"^(.*?)\s*\((-?\d+(?:[.,]\d+)?)\)$")  # "PullUps (130)"


def parse_tags(value: str):
    """Record tags as (name, value or None); the app writes a tag value as "Name (130)"."""
    out = []
    for item in (t.strip() for t in (value or "").split(",")):
        if not item:
            continue
        m = TAG_VALUE_RE.match(item)
        out.append((m.group(1), float(m.group(2).replace(",", "."))) if m else (item, None))
    return out


def load_time_records(folder: Path):
    files = sorted(folder.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    if not files:
        return None, []
    text = files[0].read_text(encoding="utf-8-sig")
    records = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            start = parse_stt_time(row["time started"])
            end = parse_stt_time(row["time ended"])
        except (KeyError, ValueError):
            continue
        activity = row.get("activity name", "").strip()
        tags = [(TIME_TAG_MAP.get(n, n), v) for n, v in parse_tags(row.get("record tags", ""))]
        if activity in TIME_ACTIVITY_MAP:
            activity, extra = TIME_ACTIVITY_MAP[activity]
            if extra and extra not in {n for n, _ in tags}:
                tags.insert(0, (extra, None))
        records.append({"activity": activity, "tags": tags, "start": start,
                        "minutes": max(0, int((end - start).total_seconds() // 60))})
    return files[0], records


def time_comparison(records, now: datetime):
    """Minutes per activity: this week so far vs the same days of last week (Mon until this
    time a week ago), plus this week's tags per activity. Monday: last week vs the week before.

    Returns the label, the names of the two weeks and whether ▼ may be shown (always: both
    periods are the same length), and rows of (activity, [current, previous], {tag: [minutes,
    value total or None]}).
    """
    monday = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time(), TZ)
    if now.weekday() == 0:
        cur = (monday - timedelta(days=7), monday)
        label, words = "last week vs the week before", ("last week", "the week before", True)
    else:
        cur = (monday, now)
        label, words = f"this week so far (Mon–{now:%a}) vs the same days last week", ("this week", "last week", True)
    prev = (cur[0] - timedelta(days=7), cur[1] - timedelta(days=7))
    totals = defaultdict(lambda: [0, 0])          # activity -> [current, previous]
    tags = defaultdict(dict)                      # activity -> {tag: [minutes, value]}
    for r in records:
        slot = 0 if cur[0] <= r["start"] < cur[1] else 1 if prev[0] <= r["start"] < prev[1] else None
        if slot is None:
            continue
        totals[r["activity"]][slot] += r["minutes"]
        if slot == 0:
            for name, value in r["tags"]:
                t = tags[r["activity"]].setdefault(name, [0, None])
                t[0] += r["minutes"]
                if value is not None:
                    t[1] = (t[1] or 0) + value
    rows = sorted(((a, mins, tags.get(a, {})) for a, mins in totals.items()),
                  key=lambda row: (-row[1][0], -row[1][1]))
    return label, words, rows


def hm(minutes: int) -> str:
    """2h 30m, 2h, 15m."""
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h and m else f"{h}h" if h else f"{m}m"


def trend(cur: int, prev: int, complete: bool) -> str:
    """▲/▼ against the other week. Mid-week only "▲" (part of a week always looks like less)."""
    if not complete:
        return " ▲" if cur > prev else ""
    if cur and prev and cur != prev:
        pct = round((cur - prev) * 100 / prev)
        if not pct:
            return " · about the same"   # 5h 30m against 5h 31m is not a fall
        return f" {'▲' if cur > prev else '▼'} {pct:+d}%"
    return " ▲" if cur > prev else " ▼" if cur < prev else ""


def tag_text(tags) -> str:
    """'Calisthenics 2h, PullUps&PushUps 1h (105)', biggest first."""
    items = sorted(tags.items(), key=lambda kv: -kv[1][0])[:5]
    return ", ".join(f"{name} {hm(m)}" + (f" ({value:g})" if value is not None else "") for name, (m, value) in items)


def time_line(name: str, cur: int, prev: int, words) -> str:
    """Plain sentence for the model, e.g. "Exercise: 2h 30m this week, 1h 30m last week ▲"."""
    now_word, before_word, complete = words
    return f"{name}: {hm(cur)} {now_word}, {hm(prev)} {before_word}{trend(cur, prev, complete)}"


SPARK = "▁▂▃▄▅▆▇█"


def week_history(records, now: datetime, weeks: int = 8):
    """Minutes per activity in each of the last complete weeks, oldest first."""
    first = now.date() - timedelta(days=now.weekday() + 7 * weeks)
    out = defaultdict(lambda: [0] * weeks)
    for r in records:
        i = (r["start"].date() - first).days // 7
        if 0 <= i < weeks:
            out[r["activity"]][i] += r["minutes"]
    return out


def sparkline(values) -> str:
    """'▁▃▅█' scaled to the biggest week; empty if nothing was tracked."""
    top = max(values, default=0)
    return "".join(SPARK[min(7, v * 8 // top)] for v in values) if top else ""


def daily_minutes(records):
    """{(day, activity): minutes}, by the day a record started (as the weekly totals count it)."""
    out = defaultdict(int)
    for r in records:
        out[(r["start"].date().isoformat(), r["activity"])] += r["minutes"]
    return dict(out)


def activity_days(records):
    days = defaultdict(set)
    for r in records:
        days[r["activity"]].add(r["start"].date())
    return days


def streak(days: set, today: date) -> int:
    """Days in a row with the activity, up to yesterday (today counts once it is done)."""
    d, n = (today if today in days else today - timedelta(days=1)), 0
    while d in days:
        n, d = n + 1, d - timedelta(days=1)
    return n


def since_text(days: set, today: date) -> str:
    """' · last tracked 9 days ago' when it has been a while, else ''."""
    gap = (today - max(days)).days if days else None
    return f" · last tracked {gap} days ago" if gap and gap >= 3 else ""


# ---------- weekly goals ----------

def goal_minutes(value: str):
    """'3h', '90m', '2h 30m', '1.5h' -> minutes, or None."""
    m = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+)\s*m)?\s*", value or "")
    if not m or not (m.group(1) or m.group(2)):
        return None
    return round(float(m.group(1) or 0) * 60) + int(m.group(2) or 0)


def parse_goals(text: str):
    """'Exercise=3h; Reading=2h' -> {'Exercise': 180, 'Reading': 120}; bad entries are skipped."""
    out = {}
    for name, _, value in (e.partition("=") for e in (text or "").split(";")):
        mins = goal_minutes(value)
        if name.strip() and mins:
            out[name.strip()] = mins
    return out


def goal_rows(goals, time_rows, now: datetime):
    """Progress per weekly goal. The brief runs in the morning, so "by now" means the days
    already over: on Saturday 5 of 7, so a 3h goal expects 2h 09m. Monday judges last week."""
    monday = now.weekday() == 0
    days_done = 7 if monday else now.weekday()
    current = {name: c for name, (c, _), _ in time_rows}
    rows = []
    for name, target in goals.items():
        done = current.get(name, 0)
        expected = round(target * days_done / 7)
        status = ("done" if done >= target else "missed" if monday
                  else "on pace" if done >= expected * 0.9 else "behind")
        rows.append({"name": name, "done": done, "target": target, "status": status,
                     "expected": expected, "days_done": days_done})
    return rows


def finished_goal_weeks(goals, records, now: datetime, exported: datetime, weeks: int = 4):
    """(week, activity, minutes, target, status) for each goal in the last complete weeks. A week
    counts only if the export was made after it ended, so a late export can't judge it early."""
    history = week_history(records, now, weeks)
    first = now.date() - timedelta(days=now.weekday() + 7 * weeks)
    out = []
    for i in range(weeks):
        start = first + timedelta(weeks=i)
        if exported < datetime.combine(start + timedelta(days=7), datetime.min.time(), TZ):
            continue
        for name, target in goals.items():
            done = history.get(name, [0] * weeks)[i]
            out.append((start.isoformat(), name, done, target, "done" if done >= target else "missed"))
    return out


def to_go(g) -> str:
    """'2h 30m to go, 1h 15m a day' for a goal still open this week, else ''."""
    left, days_left = g["target"] - g["done"], 7 - g["days_done"]
    if g["status"] not in ("behind", "on pace") or not days_left:
        return ""
    return f"{hm(left)} to go, {hm(-(-left // days_left))} a day"


def goal_line(g) -> str:
    """'**Exercise** 30m / 3h · behind · 2h 30m to go, 1h 15m a day'; the page draws it as a bar."""
    rest = to_go(g)
    return f"**{g['name']}** {hm(g['done'])} / {hm(g['target'])} · {g['status']}" + (f" · {rest}" if rest else "")


def goal_fact(g) -> str:
    text = f"Weekly goal {g['name']}: {hm(g['done'])} of {hm(g['target'])}, {g['status']}"
    if g["status"] in ("behind", "on pace"):
        text += f" ({hm(g['expected'])} expected after {g['days_done']} of 7 days)"
    rest = to_go(g)
    return text + (f"; {rest}" if rest else "") + "."


# ---------- wins and gaps (decided here, so the model can phrase but never flip them) ----------

def wins_and_gaps(time_rows, goals, days, today: date, done, overdue: int, before_word: str):
    recent, before = done
    wins, gaps = [], []
    if recent > before:
        wins.append(f"**Tasks done** {recent} in 7 days · {before} the week before ▲")
    elif recent < before:
        gaps.append(f"**Tasks done** {recent} in 7 days · {before} the week before ▼")
    if overdue:
        gaps.append(f"**Overdue** {plural(overdue, 'task')}")
    for g in goals:
        line = f"**{g['name']}** {hm(g['done'])} / {hm(g['target'])} · {g['status']}"
        if g["status"] in ("done", "on pace"):
            wins.append(line)
        else:
            gaps.append(line + (since_text(days.get(g["name"]), today) if not g["done"] else ""))
    with_goal = {g["name"] for g in goals}
    for name, (c, p), _ in time_rows:
        if name in with_goal:
            continue
        line = f"**{name}** {hm(c)} · {before_word} {hm(p)}{trend(c, p, True)}"
        if c > p * 1.15 and c - p >= 30:
            wins.append(line)
        elif p >= 30 and c < p * 0.85 and p - c >= 30:
            gaps.append(line + (since_text(days.get(name), today) if not c else ""))
    for name, ds in sorted(days.items()):
        if (n := streak(ds, today)) >= 3:
            wins.append(f"**{name}** {n} days in a row")
    return wins[:5], gaps[:5]


def week_review(records, now: datetime, time_rows, goals, done_recent: int):
    """Monday only: a few lines about the week that just ended."""
    end = datetime.combine(now.date(), datetime.min.time(), TZ)
    per_day = defaultdict(int)
    for r in records:
        if end - timedelta(days=7) <= r["start"] < end:
            per_day[r["start"].date()] += r["minutes"]
    total_c = sum(c for _, (c, _), _ in time_rows)
    total_p = sum(p for _, (_, p), _ in time_rows)
    out = [f"**Total tracked** {hm(total_c)} · the week before {hm(total_p)}{trend(total_c, total_p, True)}"]
    if per_day:
        best = max(per_day.items(), key=lambda kv: kv[1])
        out.append(f"**Best day** {best[0]:%A %d %b}, {hm(best[1])}")
    changes = sorted((c - p, name) for name, (c, p), _ in time_rows)
    if changes and changes[-1][0] > 0:
        out.append(f"**Biggest rise** {changes[-1][1]} +{hm(changes[-1][0])}")
    if changes and changes[0][0] < 0:
        out.append(f"**Biggest drop** {changes[0][1]} -{hm(-changes[0][0])}")
    if goals:
        out.append(f"**Goals hit** {sum(g['status'] == 'done' for g in goals)} of {len(goals)}")
    out.append(f"**Tasks done** {done_recent} in the last 7 days")
    return out


# ---------- local model summary ----------

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                   "focus": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
    "required": ["bullets", "focus"],
}

SUMMARY_PROMPT = (
    "You write the top of my personal morning brief. You get FACTS computed by code from my "
    "calendar, to-do list and time tracking. "
    "bullets: 2 to 4 short bullets, most important first: what matters today, what to prepare for "
    "in the coming days, and anything notable. The WIN and SLIPPING lines are already shown to me "
    "as their own lists: do not repeat them. "
    "focus: 1 to 3 concrete suggestions for today, using free time in the calendar, overdue or due "
    "tasks and SLIPPING lines (e.g. a workout this evening, a reading session tonight). On Monday, make "
    "one of them a suggestion for the new week. "
    "Time this week is compared with the same days of last week, so the comparison is fair. "
    "Be direct and specific, like a good assistant. "
    "Use only the FACTS. Every number you mention must appear in FACTS exactly as written. "
    "Copy durations exactly as FACTS write them (e.g. 48m a day); never work out new ones. "
    "MARKET lines and market prices are already shown in their own section: mention at most one, "
    "only if it is unusual, as a plain fact. Never suggest buying, selling or holding anything. "
    "Never invent events, tasks or reasons. Never mention the words FACTS, WIN, SLIPPING or MARKET; "
    "write as if you simply know my week. If nothing stands out, say so in one bullet."
)

NUMBER_RE = re.compile(r"\d+(?:[.:]\d+)?")


def model_summary(facts_text: str):
    """Ask the local model for summary bullets and today's focus. Lines containing a number that
    is not in the facts are dropped, so the model can phrase and prioritise but cannot make figures up."""
    payload = {
        "model": MODEL, "stream": False, "think": False, "format": SUMMARY_SCHEMA,
        "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": SUMMARY_PROMPT},
                     {"role": "user", "content": "FACTS:\n" + facts_text}],
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        reply = json.loads(json.loads(resp.read())["message"]["content"])
    allowed = set(NUMBER_RE.findall(facts_text))

    def check(items, limit):
        items = items if isinstance(items, list) else []
        kept = []
        for b in items:
            if not isinstance(b, str) or not b.strip():
                continue
            if set(NUMBER_RE.findall(b)) <= allowed:
                kept.append(b.strip())
            else:   # to the journal only: nothing to act on, so not in the brief's Check
                print(f"dropped model line: {b.strip()[:300]}", file=sys.stderr)
        return kept[:limit], len(items) - len(kept)

    bullets, dropped = check(reply.get("bullets", []), 4)
    focus, dropped_focus = check(reply.get("focus", []), 3)
    return bullets, focus, dropped + dropped_focus


# ---------- email ----------

def email_items(consume: bool):
    if not EMAIL_DIGEST.exists():
        return []
    items = [json.loads(l) for l in EMAIL_DIGEST.read_text().splitlines() if l.strip()]
    if consume and EMAIL_CONSUME:
        EMAIL_DIGEST.write_text("")
    order = ["urgent", "work", "personal", "purchase", "jobs", "unsure", "info", "ads"]
    items = [it for it in items if not it.get("quiet")]  # model-filed info/ads stay in the digest only
    return sorted(items, key=lambda it: order.index(it["category"]) if it["category"] in order else len(order))


def pushed_items(now: datetime):
    if not EMAIL_PUSHED.exists():
        return []
    cutoff = now.timestamp() - PUSHED_HOURS * 3600
    items = [json.loads(l) for l in EMAIL_PUSHED.read_text().splitlines() if l.strip()]
    return [it for it in items if it.get("at", 0) >= cutoff]


# ---------- markets ----------

def usd(v) -> str:
    return "–" if v is None else (f"${v:,.0f}" if v >= 1000 else f"${v:,.2f}")


def short_day(d) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%a} {d.day} {d:%b}"


def money_big(v) -> str:
    if v is None:
        return "–"
    return f"${v / 1e9:.1f}B" if abs(v) >= 1e9 else f"${v / 1e6:.1f}M" if abs(v) >= 1e6 else usd(v)


def arrow(v) -> str:
    """▲ 1.9% / ▼ 2.0%: the direction as a shape, so it never depends on colour."""
    if v is None:
        return "–"
    return f"{'▲' if v > 0 else '▼' if v < 0 else '■'} {abs(v) * 100:.1f}%"


MARKET_ALERTS_SHOWN = 3
MARKET_COMING_SHOWN = 5


def markets_section(m: dict, now: datetime, since: datetime):
    """The brief's Markets card from markets.json, short enough to read at a glance: Bitcoin, the
    US close, the day's biggest moves, sectors over the week, new alerts and what is coming up
    (Monday: the whole week, and last week). The detail is on the Markets tab. Headlines are left
    out on purpose: they are untrusted text, and the brief is read by a model with tools.
    Returns (lines, facts, new alerts)."""
    today, monday = now.date(), now.weekday() == 0
    groups = {g["key"]: g["items"] for g in m.get("groups") or []}
    move = lambda i, k="d1": f"{i['name']} {arrow(i.get(k))}"
    word = lambda v: "–" if v is None else f"{'up' if v > 0 else 'down'} {abs(v) * 100:.1f}%"
    lines, facts = ["## Markets"], []
    for b in groups.get("crypto", []):
        lines.append(f"- **{b['name']}** {usd(b.get('price', b['close']))} {arrow(b['d1'])} · 7 days {arrow(b['w1'])}")
        facts.append(f"Markets: {b['name']} {usd(b.get('price', b['close']))}, {word(b['d1'])} since yesterday.")
    us = m.get("us_close")
    core = groups.get("benchmarks", []) + groups.get("gold", [])
    spy = next((i for i in core if i["symbol"] == "SPY"), None)
    if us and core:
        lines.append(f"- **US close {short_day(us)}** · " + " · ".join(move(i) for i in core))
        facts.append(f"US close {short_day(us)}: " + ", ".join(f"{i['name']} {word(i.get('d1'))}" for i in core) + ".")
    movers = sorted((i for k in ("stocks", "themes") for i in groups.get(k, []) if i.get("d1") is not None),
                    key=lambda i: -abs(i["d1"]))[:3]
    if movers:
        lines.append("- **Biggest moves** · " + " · ".join(move(i) for i in movers))
    sectors = sorted((i for i in groups.get("sectors", []) if i.get("w1") is not None), key=lambda i: i["w1"])
    if len(sectors) >= 4:
        market = f" · S&P 500 {arrow(spy['w1'])}" if spy and spy.get("w1") is not None else ""
        lines.append(f"- **Sectors this week** · best {move(sectors[-1], 'w1')} · worst {move(sectors[0], 'w1')}{market}")

    def is_new(h):
        try:
            return datetime.fromisoformat(h["first_seen"]) > since
        except (KeyError, TypeError, ValueError):
            return False
    alerts = market_alerts([h for h in m.get("hits") or [] if is_new(h)])
    if alerts:
        lines += ["", f"### New alerts ({len(alerts)})"] + [f"- {a}" for a in alerts[:MARKET_ALERTS_SHOWN]]
        if len(alerts) > MARKET_ALERTS_SHOWN:
            lines.append(f"- +{len(alerts) - MARKET_ALERTS_SHOWN} more on the Markets tab")
        facts += [f"MARKET: {a}." for a in alerts[:MARKET_ALERTS_SHOWN]]

    if monday and core:
        last = groups.get("crypto", []) + core + groups.get("themes", [])
        lines += ["", "### Last week"] + ["- " + " · ".join(move(i, "w1") for i in last)]
        if spy and spy.get("w1") is not None and len(sectors) >= 6:
            rel = sorted(((i["w1"] - spy["w1"], i) for i in sectors), key=lambda t: t[0])
            show = lambda t: f"{t[1]['name']} {arrow(t[0])}"
            lines.append(f"- Sectors against the S&P 500: ahead {', '.join(show(t) for t in rel[::-1][:3])}"
                         f"; behind {', '.join(show(t) for t in rel[:3])}")

    ahead = today + timedelta(days=7 if monday else 3)
    when = {"amc": " after the close", "bmo": " before the open"}

    def expected(e):
        parts = ([f"EPS {e['eps_est']:.2f}".replace("-", "−")] if e.get("eps_est") is not None else []) + \
                ([f"revenue {money_big(e['rev_est'])}"] if e.get("rev_est") is not None else [])
        return f" · {' and '.join(parts)} expected" if parts else ""
    coming = [(e["date"], f"{short_day(e['date'])}: {e['name']} reports{when.get(e.get('hour'), '')}{expected(e)}")
              for e in m.get("earnings") or []
              if e.get("eps_act") is None and today.isoformat() <= e["date"] <= ahead.isoformat()]
    coming += [(r["date"], f"{short_day(r['date'])}: {r['name']}") for r in m.get("releases") or []
               if today.isoformat() <= r["date"] <= ahead.isoformat()]
    coming.sort()
    if coming:
        lines += ["", "### Coming up" + (" this week" if monday else "")]
        lines += [f"- {t}" for _, t in coming[:MARKET_COMING_SHOWN]]
        if len(coming) > MARKET_COMING_SHOWN:
            lines.append(f"- +{len(coming) - MARKET_COMING_SHOWN} more in the Markets calendar")
        facts += [f"Coming up: {t}." for _, t in coming[:4]]
    lines.append("")
    return lines, facts, len(alerts)


RULE_NAMES = {"big_day": "big day", "big_week": "big week", "high52": "52-week high", "low52": "52-week low",
              "trend200": "200-day average", "vs_market": "against the S&P 500"}
MOVE_RE = re.compile(r" ([+−]\d+(?:\.\d+)?%) on (\w+ \d+ \w+),")


def market_alerts(hits):
    """One line per symbol, most unusual first. Results come with the share-price reaction on the
    same line; other hits lead with the newest and name the rest, e.g. "(also: big week)"."""
    by_sym = defaultdict(list)
    for h in hits:
        by_sym[h["symbol"]].append(h)
    lines = []
    for group in by_sym.values():
        result = next((h for h in group if h["rule"] == "earnings"), None)
        moved = next((h for h in group if h["rule"] == "earnings_move"), None)
        rest = sorted((h for h in group if h["rule"] not in ("earnings", "earnings_move")),
                      key=lambda h: (h["day"], h.get("score", 0)), reverse=True)
        if result or moved:
            text = (result or moved)["text"]
            m = MOVE_RE.search(moved["text"]) if result and moved else None
            if m:
                text += f" · shares {m.group(1)} on {m.group(2)}"
            lines.append((100.0, text))
            continue
        lead = rest[0]
        others = sorted({RULE_NAMES.get(h["rule"], h["rule"]) for h in rest[1:] if h["rule"] != lead["rule"]})
        lines.append((max(h.get("score", 0) for h in rest),
                      lead["text"] + (f" (also: {', '.join(others)})" if others else "")))
    return [t for _, t in sorted(lines, key=lambda x: -x[0])]


def feeds_warnings(health_path: Path, m: dict, now: datetime):
    out = []
    try:
        h = json.loads(health_path.read_text())
    except FileNotFoundError:
        return ["Markets and News fetch has not run yet (news-fetch)."]
    except Exception:
        return [f"Markets and News health file {health_path.name} could not be read."]
    failed = {k: v for k, v in (h.get("sources") or {}).items() if not v.get("ok")}
    if failed:
        out.append("Markets and News fetch: " + "; ".join(f"{k} failed ({v.get('error', '')[:80]})"
                                                          for k, v in failed.items()) + ".")
    try:
        age = (now - datetime.fromisoformat(h["updated"])).total_seconds() / 3600
        if age > FEEDS_STALE_HOURS:
            out.append(f"Markets and News last fetched {age:.0f} hours ago: check news-fetch.timer.")
    except (KeyError, TypeError, ValueError):
        pass
    us = m.get("us_close")
    if us and (now.date() - date.fromisoformat(us)).days > MARKET_STALE_DAYS:
        out.append(f"Market prices are from {short_day(us)}: check the tiingo source in news-fetch.")
    return out


# ---------- build ----------

def email_summary(pushed, mails):
    """One Summary line built from counts, so no email text ever reaches the model."""
    if not pushed and not mails:
        return "Email: nothing new that needs your attention."
    def by_cat(items):
        n = Counter(m["category"] for m in items)
        return ", ".join(f"{v} {k}" for k, v in n.most_common())
    parts = []
    if pushed:
        parts.append(f"{len(pushed)} pushed to your phone in the last {PUSHED_HOURS} hours ({by_cat(pushed)})")
    if mails:
        parts.append(f"{len(mails)} waiting for the digest ({by_cat(mails)})")
    return "Email: " + "; ".join(parts) + "."


# Email in the brief: grouped under a heading per category, most important first.
EMAIL_GROUPS = [("urgent", "Urgent"), ("work", "Work"), ("personal", "Personal"), ("purchase", "Purchases"),
                ("jobs", "Jobs"), ("unsure", "Unsure"), ("info", "Info"), ("ads", "Ads")]


def email_groups(items):
    """- **Purchases** (3)
      - **Amazon.co.uk**: summary or subject"""
    order = [c for c, _ in EMAIL_GROUPS] + sorted({m["category"] for m in items} - {c for c, _ in EMAIL_GROUPS})
    names = dict(EMAIL_GROUPS)
    out = []
    for cat in order:
        group = [m for m in items if m["category"] == cat]
        if group:
            out.append(f"- **{names.get(cat, cat.capitalize())}** ({len(group)})")
            out += [f"  - **{m['from_name']}**: {m.get('summary') or m['subject']}" for m in group]
    return out


def fmt_day(d: date, today: date):
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %d %b")


def email_health_warnings(path: Path, now: datetime):
    if not path.exists():
        return ["Email triage has not run yet, so urgent emails are not being watched."]
    try:
        h = json.loads(path.read_text())
    except Exception:
        return [f"Email triage health file {path.name} could not be read."]
    if h.get("fails"):
        return [f"Email triage has failed {h['fails']} runs in a row (last error: "
                f"{h.get('last_error', 'unknown')[:120]}). Urgent emails may be missed."]
    last_ok = h.get("last_ok")
    if last_ok and (now.timestamp() - last_ok) / 3600 > EMAIL_STALE_HOURS:
        return [f"Email triage last ran {(now.timestamp() - last_ok) / 3600:.0f} hours ago: "
                "check its timer (systemctl --user list-timers)."]
    return []


def age_hours(p: Path, now: datetime):
    return (now - datetime.fromtimestamp(p.stat().st_mtime, TZ)).total_seconds() / 3600


def has_word(title: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", title.lower()) is not None


def next_month(today: date):
    first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first, last


def build(now: datetime, consume_email: bool):
    today = now.date()
    lines, warnings = [f"# Morning brief, {now:%A %d %B}", ""], []

    # calendar: newest Fossify backup + every file in the extra folder
    ics = newest_ics(CAL_DIR) if CAL_DIR.exists() else None
    extra = sorted(EXTRA_ICS_DIR.glob("*.ics")) if EXTRA_ICS_DIR.exists() else []
    nm_first, nm_last = next_month(today)
    start = datetime.combine(today, datetime.min.time(), TZ)
    horizon = max(today + timedelta(days=LATER_DAYS), nm_last) + timedelta(days=1)
    end = datetime.combine(horizon, datetime.min.time(), TZ)
    events, cal_tasks, cal_done = [], [], []
    if ics is None:
        warnings.append(f"No calendar file found in {CAL_DIR}.")
    else:
        h = age_hours(ics, now)
        if h > STALE_HOURS:
            warnings.append(f"Calendar backup is {h / 24:.1f} days old: check Fossify backups and Syncthing.")
    for f in ([ics] if ics else []) + extra:
        try:
            evs, skipped = load_events(f, start, end)
            events += evs
            tks, skipped_tasks = load_calendar_tasks(f, today, end)
            cal_tasks += tks
            cal_done += calendar_done(f)
            skipped += skipped_tasks
            if skipped:
                warnings.append(f"{skipped} event(s) in {f.name} could not be read and were skipped.")
        except Exception as exc:
            warnings.append(f"Calendar file {f.name} could not be read: {exc}")
    rows = event_rows(events)
    week_end = today + timedelta(days=LOOKAHEAD_DAYS)
    today_rows = [r for r in rows if r["day"] == today]
    soon_rows = [r for r in rows if today < r["day"] <= week_end]
    later_rows, seen_titles = [], set()
    for r in rows:  # important events only; repeats shown once
        if (week_end < r["day"] <= today + timedelta(days=LATER_DAYS)
                and any(has_word(r["title"], k) for k in LATER_KEYWORDS)
                and r["title"].lower() not in seen_titles):
            seen_titles.add(r["title"].lower())
            later_rows.append(r)

    if not today_rows and not soon_rows:  # one line instead of two empty cards
        lines += ["## Calendar", f"- Nothing today or in the next {LOOKAHEAD_DAYS} days.", ""]
    else:
        lines.append("## Today")
        if today_rows:
            lines += [f"- {r['time']}  {r['title']}" + (f" ({r['where']})" if r["where"] else "") for r in today_rows]
        else:
            lines.append("- Nothing in the calendar.")
        lines.append("")

        lines.append(f"## Next {LOOKAHEAD_DAYS} days")
        if soon_rows:
            lines += [f"- {fmt_day(r['day'], today)} {r['time']}  {r['title']}" for r in soon_rows]
        else:
            lines.append("- Nothing coming up.")
        lines.append("")

    if later_rows:
        lines.append(f"## Later (up to {LATER_DAYS} days)")
        lines += [f"- {r['day']:%a %d %b} {r['time']}  {r['title']}" for r in later_rows[:15]]
        lines.append("")

    # tasks (read here so the month preview can list next month's dated tasks)
    tasks, missing = parse_tasks(TODO_FILES)
    tasks += cal_tasks  # Obsidian and Fossify tasks are listed together
    for m in missing:
        warnings.append(f"To-do file not found: {m}")
    overdue = sorted([t for t in tasks if t["due"] and t["due"] < today], key=lambda t: (t["due"], t["prio"]))
    # by priority; then Obsidian first (file order), then calendar by time
    due_today = sorted([t for t in tasks if t["due"] == today], key=lambda t: (t["prio"], t["text"] if t["line"] == 0 else ""))
    this_week = sorted([t for t in tasks if t["due"] and today < t["due"] <= today + timedelta(days=LOOKAHEAD_DAYS)],
                       key=lambda t: t["due"])
    undated = [t for t in tasks if not t["due"]]
    undated.sort(key=lambda t: t["prio"])  # highest priority first, otherwise file order

    if today.day >= MONTH_PREVIEW_FROM_DAY:
        # events already listed above are skipped; repeats are shown once
        shown = {r["title"].lower() for r in later_rows} | {(r["day"], r["title"].lower()) for r in soon_rows}
        month_rows, times = [], Counter()
        for r in rows:
            t = r["title"].lower()
            if nm_first <= r["day"] <= nm_last and t not in shown and (r["day"], t) not in shown:
                times[t] += 1
                if times[t] == 1:
                    month_rows.append(r)
        month_tasks = sorted([t for t in tasks if t["due"] and nm_first <= t["due"] <= nm_last
                              and t["due"] > today + timedelta(days=LOOKAHEAD_DAYS)], key=lambda t: t["due"])
        lines.append(f"## {nm_first:%B} at a glance")
        lines += [f"- {r['day']:%a %d}  {r['title']}" + (f" (x{n})" if (n := times[r['title'].lower()]) > 1 else "")
                  for r in month_rows[:12]]
        if len(month_rows) > 12:
            lines.append(f"- +{len(month_rows) - 12} more events")
        lines += [f"- Task due {t['due']:%a %d}: {t['text']}" for t in month_tasks[:8]]
        if not month_rows and not month_tasks:
            lines.append("- Nothing in the calendar or to-do list yet.")
        lines.append("")

    lines.append("## Tasks")
    # "⏫ Task · _Computer science_": the Obsidian priority icon first, the list section last
    star = lambda t: f"{PRIO_ICON[t['prio']]} " if t["prio"] != 3 else ""
    where = lambda t: f" · _{t['section']}_" if t["section"] else ""
    if overdue:
        lines += ["", f"### Overdue ({len(overdue)})"]
        lines += [f"- {star(t)}{t['text']} (was due {t['due']:%a %d %b}){where(t)}" for t in overdue]
    if due_today:
        lines += ["", f"### Due today ({len(due_today)})"]
        lines += [f"- {star(t)}{t['text']}{where(t)}" for t in due_today]
    if this_week:
        lines += ["", f"### Due in the next {LOOKAHEAD_DAYS} days ({len(this_week)})"]
        lines += [f"- {fmt_day(t['due'], today)}: {star(t)}{t['text']}{where(t)}" for t in this_week]
    if undated:
        more = f", showing {UNDATED_SHOWN}" if len(undated) > UNDATED_SHOWN else ""
        lines += ["", f"### Anytime ({len(undated)}{more})"]
        lines += [f"- {star(t)}{t['text']}{where(t)}" for t in undated[:UNDATED_SHOWN]]
    if not tasks:
        lines.append("- No open tasks.")
    if TASKS_LINK:
        lines += ["", f"_Open in Obsidian: {TASKS_LINK}_"]
    lines.append("")

    # time tracking
    time_file, records = load_time_records(TIME_DIR)
    time_label, words, time_rows = time_comparison(records, now) if records else ("", None, [])
    if time_file is None and TIME_DIR.exists():  # no folder = feature not set up, so stay quiet
        warnings.append(f"No time-tracking export (.csv) found in {TIME_DIR}.")
    elif time_file is not None and age_hours(time_file, now) > STALE_HOURS:
        warnings.append(f"Time-tracking export is {age_hours(time_file, now) / 24:.1f} days old.")
    history = week_history(records, now)
    if time_rows:
        now_word, before_word, complete = words
        lines += ["## Time tracked", f"_{time_label[0].upper() + time_label[1:]}; the bars are the last 8 weeks_", ""]
        for name, (c, p), tags in time_rows[:8]:
            spark = sparkline(history.get(name, []))
            lines.append(f"- **{name}** {hm(c)} · {before_word} {hm(p)}{trend(c, p, complete)}"
                         + (f" · {spark}" if spark else ""))
            if tags:
                lines.append(f"  - {tag_text(tags)}")
        lines.append("")

    # markets (markets.json from markets.py; nothing here is fetched or computed)
    market_facts, market_new, markets = [], 0, None
    if MARKETS_JSON:
        try:
            markets = json.loads(Path(MARKETS_JSON).read_text(encoding="utf-8"))
        except FileNotFoundError:
            warnings.append("No Markets data yet: markets-build runs after each news-fetch.")
        except Exception as exc:
            warnings.append(f"Markets data could not be read: {exc}")
        if markets:
            prev = agentdb.last_brief_built(today.isoformat())
            since = datetime.fromisoformat(prev) if prev else now - timedelta(hours=24)
            market_lines, market_facts, market_new = markets_section(markets, now, since)
            lines += market_lines
    if FEEDS_HEALTH:
        warnings += feeds_warnings(Path(FEEDS_HEALTH), markets or {}, now)

    # email
    if EMAIL_IN_BRIEF:
        pushed, mails = pushed_items(now), email_items(consume_email)
        lines.append("## Email")
        if pushed:
            lines += ["", f"### Pushed in the last {PUSHED_HOURS} hours ({len(pushed)})"]
            lines += email_groups(pushed[-10:][::-1])  # newest first
        if mails:
            lines += ["", f"### Waiting for the digest ({len(mails)})"]
            lines += email_groups(mails[:10])
            if len(mails) > 10:
                lines.append(f"- +{len(mails) - 10} more in the digest")
        if not pushed and not mails:
            lines.append("- Nothing new that needs your attention.")
        lines.append("")
        email_bullet, email_count = email_summary(pushed, mails), len(pushed) + len(mails)
    else:
        email_bullet, email_count = "", None

    # facts for the local model: plain sentences built by code, so every figure is checked
    done_days = done_by_day(TODO_FILES)
    done_recent, done_before = done_counts(done_days, today)
    per_day = defaultdict(list)
    for r in today_rows + soon_rows:
        per_day[r["day"]].append(r)
    busiest = max(per_day.items(), key=lambda kv: len(kv[1]), default=(None, []))
    clashes = []
    for day, rs in per_day.items():
        timed = sorted((r for r in rs if r["end"]), key=lambda r: r["sort"])
        clashes += [f"{a['title']} and {b['title']} overlap on {day:%a %d %b}"
                    for a, b in zip(timed, timed[1:]) if b["sort"] < a["end"]]
    facts = [f"Today is {now:%A %d %B}."]
    facts.append("Today's events: " + ("; ".join(f"{r['time']} {r['title']}" for r in today_rows) or "none") + ".")
    facts.append(f"Events in the next {LOOKAHEAD_DAYS} days: {len(soon_rows)}"
                 + (f"; busiest day {busiest[0]:%a %d %b} with {len(busiest[1])} events" if busiest[0] else "") + ".")
    facts += [f"{r['day']:%a %d %b}: {r['title']}." for r in soon_rows]
    facts += [f"Clash: {c}." for c in clashes]
    facts += [f"Coming later: {r['day']:%a %d %b} {r['title']}." for r in later_rows]
    facts.append(f"Tasks: {len(overdue)} overdue, {len(due_today)} due today, "
                 f"{len(this_week)} due in the next {LOOKAHEAD_DAYS} days, {len(undated)} open without a date.")
    facts += [f"Overdue: {t['text']} (due {t['due']:%a %d %b})." for t in overdue]
    facts += [f"Due today: {t['text']}." for t in due_today]
    facts.append(f"Tasks completed: {done_recent} in the last 7 days vs {done_before} in the 7 days before.")
    if time_rows:
        facts.append(f"Time tracked, {time_label}:")
        facts += [f"{time_line(name, c, p, words)}." + (f" Tags this period: {tag_text(tags)}." if tags else "")
                  for name, (c, p), tags in time_rows[:8]]
    goals = goal_rows(parse_goals(TIME_GOALS), time_rows, now) if records else []
    facts += [goal_fact(g) for g in goals]
    wins, gaps = wins_and_gaps(time_rows, goals, activity_days(records), today, (done_recent, done_before),
                               len(overdue), words[1] if words else "last week")
    facts += [f"WIN: {w.replace('**', '')}." for w in wins] + [f"SLIPPING: {g.replace('**', '')}." for g in gaps]
    review = week_review(records, now, time_rows, goals, done_recent) if now.weekday() == 0 and time_rows else []
    facts += [f"Last week: {r.replace('**', '')}." for r in review]
    facts += market_facts
    facts_text = "\n".join(facts)

    bullets, focus = [], []
    if OLLAMA_URL:
        try:
            bullets, focus, _ = model_summary(facts_text)
        except Exception as exc:
            warnings.append(f"Summary not written (local model unavailable: {exc}).")
    if email_bullet:
        bullets.append(email_bullet)

    if EMAIL_HEALTH:
        warnings += email_health_warnings(Path(EMAIL_HEALTH), now)

    if warnings:
        lines.append("## Check")
        lines += [f"- {w}" for w in warnings]
        lines.append("")

    lines.append(f"_Built {now:%H:%M}. Calendar file: {ics.name if ics else 'none'}._")
    counts = {"today": len(today_rows), "soon": len(soon_rows), "later": len(later_rows),
              "overdue": len(overdue),
              "due_today": len(due_today), "warnings": len(warnings), "email": email_count,
              "markets": market_new}
    # right under the title: the counts line first, then the model's bullets and the email line,
    # today's focus, wins and gaps, goals and (on Monday) last week in review
    top = ["## Summary", f"- **{headline(counts)}**"] + [f"- {b}" for b in bullets] + [""]
    if focus:
        top += ["## Focus today"] + [f"- {f}" for f in focus] + [""]
    if wins or gaps:  # always both, so they sit side by side
        top += ["## Wins"] + [f"- {w}" for w in wins or ["Nothing stands out yet."]] + [""]
        top += ["## Slipping"] + [f"- {g}" for g in gaps or ["Nothing slipping."]] + [""]
    if goals:
        days_done = goals[0]["days_done"]
        top += ["## Weekly goals",
                "_Last week (complete)_" if days_done == 7 else f"_{days_done} of 7 days done, {7 - days_done} left_",
                ""] + [f"- {goal_line(g)}" for g in goals] + [""]
    if review:
        top += ["## Last week in review"] + [f"- {r}" for r in review] + [""]
    lines[2:2] = top
    build.facts_text = facts_text  # kept for --facts and tests
    # the numbers kept for good in agent.db (Stats tab, Grafana); saved by main, not by --print
    exported = datetime.fromtimestamp(time_file.stat().st_mtime, TZ) if time_file else None
    build.history = {"day": today.isoformat(), "time_days": daily_minutes(records),
                     "done_days": {d.isoformat(): n for d, n in done_days.items()},
                     "open_tasks": len(tasks), "overdue": len(overdue),
                     "goal_weeks": finished_goal_weeks(parse_goals(TIME_GOALS), records, now, exported)
                     if records else [],   # no export: no verdicts, rather than every goal "missed"
                     # a missing to-do file would look like every task deleted: skip the task rows then
                     "tasks": None if missing else {**task_history(TODO_FILES, cal_tasks, cal_done),
                                                    "calendar": ics is not None}}
    return "\n".join(lines), counts


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def headline(counts) -> str:
    """e.g. "0 events today, 1 task due, 3 emails, check warnings"."""
    return (f"{plural(counts['today'], 'event')} today, {plural(counts['due_today'] + counts['overdue'], 'task')} due"
            + (f", {plural(counts['email'], 'email')}" if counts.get("email") else "")
            + (f", {plural(counts['markets'], 'market alert')}" if counts.get("markets") else "")
            + (", check warnings" if counts["warnings"] else ""))


def notify(text, counts):
    # the push title is the "# Morning brief, <date>" line, so the body starts at the Summary
    first, _, rest = text.partition("\n")
    headers = {"Title": first.lstrip("# ").strip()}
    page = agentdb.link()
    if page and BRIEF_PUSH != "full":
        # Short push: the one-line headline, and a tap opens the brief on the agent page.
        page += "#brief"
        headers["Click"] = page
        headers["Actions"] = f"view, Open brief, {page}"
        text = f"{headline(counts)}\nTap to read the brief."
    else:
        headers["Markdown"] = "yes"
        if page:
            headers["Click"] = page
        text = rest.lstrip("\n") if first.startswith("# ") else text
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    body = text.encode("utf-8")[:4000].decode("utf-8", "ignore").encode("utf-8")  # never cut a character in half
    req = urllib.request.Request(NTFY_URL, data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        print(f"ntfy failed: {exc}", file=sys.stderr)
        return
    agentdb.record_push(NTFY_URL, headers["Title"], body.decode("utf-8"), "default", page)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="print only; write and send nothing")
    ap.add_argument("--facts", action="store_true", help="also print the facts given to the model")
    args = ap.parse_args()
    now = datetime.now(TZ)
    text, counts = build(now, consume_email=not args.print)
    if args.facts:
        print("FACTS GIVEN TO THE MODEL:\n" + build.facts_text + "\n")
    if args.print:
        print(text)
        sys.exit(0)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(OUT_FILE)
    agentdb.record_brief(now.date().isoformat(), text)
    agentdb.record_history(**build.history)
    if NTFY_URL:
        notify(text, counts)
    agentdb.refresh()
    print(text)
