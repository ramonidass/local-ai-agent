"""The agent's own database, and the two files built from it after every run.

agent.db (SQLite, stays on the server):
  emails   every email triage sorted: when, category, rule or model, sender, subject, summary
  pushes   every ntfy notification the agent sent, with the link it carried
  briefs   one morning brief per day
  runs     every triage run: ok or the error
Kept for good (never pruned; numbers only, no email text):
  time_daily    minutes per day and activity, from the time-tracker export
  tasks_daily   tasks ticked off per day; open and overdue from the day the brief first saw them
  week_goals    each finished week against the goal it had then, judged once
  tasks         one row per to-do: its list, when it was first seen, done or deleted
  email_weekly  emails per week and category, still there after the emails are pruned
  market_daily  one close per symbol and trading day (Bitcoin: per completed UTC day)
  market_hits   every Markets rule hit, recorded once (markets.py)
  earnings      each report: EPS and revenue, estimate and actual

Built from it (all replaced atomically, so readers never see half a file):
  web/data.json        what the agent page shows (emails, briefs, pushes, health)
  web/stats.json       the kept-for-good tables, for the page's Stats tab (rewritten only when they change)
  web/markets.json     the Markets tab and the brief's Markets section (written by markets.py)
  stats/agent-stats.db counts only, no subjects or names: synced to the NAS for Grafana
  backup/agent.db      a consistent copy made once a day; Syncthing backs up this, never the live file

Standard library only. Everything is off unless AGENT_DB is set, and a problem here never
stops triage or the brief: it prints a warning and they carry on.
"""
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DB_PATH = os.environ.get("AGENT_DB", "")
WEB_DIR = Path(os.environ.get("AGENT_WEB_DIR", "/data/web"))
STATS_DIR = Path(os.environ.get("AGENT_STATS_DIR", "/data/stats"))
# Link put on every ntfy push (tapping it opens this). Always the same address, so a browser
# that reuses tabs can match it; the page itself shows what is new. Empty = pushes carry no link.
PUSH_LINK = os.environ.get("AGENT_PUSH_LINK", "")

KEEP_DAYS = 120          # rows older than this are deleted
PAGE_EMAIL_DAYS = 30     # the page shows this many days of email...
PAGE_EMAIL_MAX = 500     # ...up to this many
PAGE_BRIEFS = 14
PAGE_PUSH_DAYS = 14
BODY_KEEP_DAYS = 30      # full email text: kept only as long as the page shows the email
SNAPSHOT_HOURS = 23      # backup/agent.db is renewed by the first run after this long

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    uidvalidity TEXT NOT NULL,
    uid         INTEGER NOT NULL,
    received    TEXT,
    processed   TEXT NOT NULL,
    category    TEXT NOT NULL,
    source      TEXT NOT NULL,
    from_name   TEXT,
    from_addr   TEXT,
    subject     TEXT,
    summary     TEXT,
    pushed      INTEGER NOT NULL DEFAULT 0,
    quiet       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS bodies (
    uidvalidity TEXT NOT NULL,
    uid         INTEGER NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS pushes (
    id       INTEGER PRIMARY KEY,
    sent     TEXT NOT NULL,
    topic    TEXT,
    title    TEXT,
    message  TEXT,
    priority TEXT,
    link     TEXT
);
CREATE TABLE IF NOT EXISTS briefs (
    day      TEXT PRIMARY KEY,
    built    TEXT NOT NULL,
    markdown TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id    INTEGER PRIMARY KEY,
    ts    TEXT NOT NULL,
    job   TEXT NOT NULL,
    ok    INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS time_daily (
    day      TEXT NOT NULL,
    activity TEXT NOT NULL,
    minutes  INTEGER NOT NULL,
    PRIMARY KEY (day, activity)
);
CREATE TABLE IF NOT EXISTS tasks_daily (
    day     TEXT PRIMARY KEY,
    done    INTEGER NOT NULL DEFAULT 0,
    open    INTEGER,
    overdue INTEGER
);
CREATE TABLE IF NOT EXISTS week_goals (
    week     TEXT NOT NULL,
    activity TEXT NOT NULL,
    minutes  INTEGER NOT NULL,
    target   INTEGER NOT NULL,
    status   TEXT NOT NULL,
    PRIMARY KEY (week, activity)
);
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    key        TEXT NOT NULL,      -- the task's words, lower case, without dates or icons
    text       TEXT NOT NULL,
    list       TEXT,               -- the list it was in while open ("Computer science › Server", "Calendar")
    priority   INTEGER,
    due        TEXT,
    first_seen TEXT NOT NULL,      -- the first brief that saw it open; its ✅ day if never seen open
    exact      INTEGER NOT NULL,   -- 0: already there on the first run, so its real age is unknown
    done       TEXT,               -- ✅ day
    gone       TEXT                -- the day it left the list without a ✅ (deleted or cancelled)
);
CREATE TABLE IF NOT EXISTS email_weekly (
    week     TEXT NOT NULL,
    category TEXT NOT NULL,
    emails   INTEGER NOT NULL,
    pushed   INTEGER NOT NULL,
    PRIMARY KEY (week, category)
);
CREATE TABLE IF NOT EXISTS market_daily (
    day       TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    close     REAL NOT NULL,
    adj_close REAL NOT NULL,       -- for returns: splits and dividends taken out
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS market_hits (
    day        TEXT NOT NULL,      -- the trading day the rule looked at (earnings: the report day)
    rule       TEXT NOT NULL,      -- big_day, big_week, high52, low52, trend200, sector_shift, earnings, earnings_move
    symbol     TEXT NOT NULL,
    text       TEXT NOT NULL,
    score      REAL NOT NULL,      -- for ordering: how unusual (in multiples of the usual move)
    first_seen TEXT NOT NULL,      -- when markets.py recorded it; the brief shows hits newer than the last brief
    PRIMARY KEY (day, rule, symbol)
);
CREATE TABLE IF NOT EXISTS earnings (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,
    hour    TEXT,                  -- bmo before the open, amc after the close
    quarter INTEGER,
    year    INTEGER,               -- the company's fiscal year
    eps_est REAL, eps_act REAL, rev_est REAL, rev_act REAL,
    updated TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS emails_processed ON emails (processed);
CREATE INDEX IF NOT EXISTS pushes_sent ON pushes (sent);
CREATE INDEX IF NOT EXISTS runs_job_ts ON runs (job, ts);
"""


def enabled():
    return bool(DB_PATH)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _warn(what, exc):
    print(f"agentdb: {what} failed: {exc}", file=sys.stderr)


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript(SCHEMA)
    return conn


def _write(fn, *args):
    """Run one write in its own short transaction; never raise."""
    if not enabled():
        return
    try:
        conn = connect()
        try:
            with conn:
                fn(conn, *args)
        finally:
            conn.close()
    except Exception as exc:
        _warn(fn.__name__, exc)


def link(_section=""):
    """The link for a push. One fixed address for every push (see PUSH_LINK)."""
    return PUSH_LINK


def email_anchor(uidvalidity, uid):
    return f"email-{uidvalidity}-{uid}"


def received_at(date_header):
    try:
        return _iso(parsedate_to_datetime(date_header))
    except Exception:
        return None


# ---------- writes ----------

def _email(conn, item, uidvalidity, received, pushed, quiet, body):
    if body:
        conn.execute("INSERT OR REPLACE INTO bodies VALUES (?,?,?)", (str(uidvalidity), int(item["uid"]), body))
    conn.execute(
        "INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(uidvalidity), int(item["uid"]), received, _iso(_now()), item["category"],
         item.get("source", ""), item.get("from_name", ""), item.get("from", ""),
         item.get("subject", ""), item.get("summary", ""), int(bool(pushed)), int(bool(quiet))))


def record_email(item, uidvalidity, received=None, pushed=False, quiet=False, body=""):
    _write(_email, item, uidvalidity, received, pushed, quiet, body)


def _import_queued(conn, uidvalidity, digest_file, pushed_file):
    for path, pushed in ((digest_file, False), (pushed_file, True)):
        if not path or not Path(path).exists():
            continue
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                it = json.loads(line)
            except ValueError:
                continue
            if "uid" not in it or "category" not in it:
                continue
            at = _iso(datetime.fromtimestamp(it["at"], timezone.utc)) if it.get("at") else _iso(_now())
            conn.execute(
                "INSERT OR IGNORE INTO emails VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uidvalidity), int(it["uid"]), None, at, it["category"], it.get("source", ""),
                 it.get("from_name", ""), it.get("from", ""), it.get("subject", ""), it.get("summary", ""),
                 int(pushed), int(bool(it.get("quiet")))))


def import_queued(uidvalidity, digest_file, pushed_file):
    """Mail sorted before the database existed still sits in the digest queue and the pushed log:
    copy it in so the page isn't empty at the start. Rows already present are left alone."""
    _write(_import_queued, uidvalidity, digest_file, pushed_file)


def _mark_pushed(conn, uidvalidity, uids):
    conn.executemany("UPDATE emails SET pushed = 1 WHERE uidvalidity = ? AND uid = ?",
                     [(str(uidvalidity), int(u)) for u in uids])


def mark_pushed(uidvalidity, uids):
    _write(_mark_pushed, uidvalidity, uids)


def _push(conn, topic, title, message, priority, link_):
    conn.execute("INSERT INTO pushes (sent, topic, title, message, priority, link) VALUES (?,?,?,?,?,?)",
                 (_iso(_now()), topic, title, message[:2000], priority, link_))


def record_push(url, title, message, priority, link_):
    topic = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    _write(_push, topic, title, message, priority, link_)


def last_brief_built(before_day):
    """When the newest brief before this day was built (ISO, UTC), or None."""
    if not enabled() or not Path(DB_PATH).exists():
        return None
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        row = conn.execute("SELECT built FROM briefs WHERE day < ? ORDER BY day DESC LIMIT 1", (before_day,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _brief(conn, day, markdown):
    conn.execute("INSERT OR REPLACE INTO briefs VALUES (?,?,?)", (day, _iso(_now()), markdown))


def record_brief(day, markdown):
    _write(_brief, day, markdown)


def _run(conn, job, ok, error):
    conn.execute("INSERT INTO runs (ts, job, ok, error) VALUES (?,?,?,?)",
                 (_iso(_now()), job, int(bool(ok)), (error or "")[:300]))


def record_run(job, ok, error=""):
    _write(_run, job, ok, error)


def _task_rows(conn, day, tasks):
    first_run = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    exact = 0 if first_run else 1
    active = {}   # key -> ids of rows still open, oldest first
    for rid, key in conn.execute("SELECT id, key FROM tasks WHERE done IS NULL AND gone IS NULL ORDER BY id"):
        active.setdefault(key, []).append(rid)
    matched = set()

    def take(key):
        rid = next((i for i in active.get(key, []) if i not in matched), None)
        if rid is not None:
            matched.add(rid)
        return rid

    for t in tasks["open"]:
        rid = take(t["key"])
        if rid is None:
            conn.execute("INSERT INTO tasks (key, text, list, priority, due, first_seen, exact) VALUES (?,?,?,?,?,?,?)",
                         (t["key"], t["text"], t["list"] or None, t["priority"], t["due"], day, exact))
        else:  # moved, re-prioritised or re-dated: keep the latest
            conn.execute("UPDATE tasks SET text = ?, list = ?, priority = ?, due = ? WHERE id = ?",
                         (t["text"], t["list"] or None, t["priority"], t["due"], rid))
    # Ticked off: close the open row it came from; one never seen open was added and done
    # between two briefs (or before the history began). Rows already recorded are skipped.
    wanted = {}
    for t in tasks["done"]:
        wanted.setdefault((t["key"], t["done"]), []).append(t)
    for (key, done), rows in wanted.items():
        have = conn.execute("SELECT COUNT(*) FROM tasks WHERE key = ? AND done = ?", (key, done)).fetchone()[0]
        for t in rows[have:]:
            rid = take(key)
            if rid is None:
                conn.execute("INSERT INTO tasks (key, text, list, priority, due, first_seen, exact, done) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (key, t["text"], t["list"] or None, t["priority"], t["due"], done, exact, done))
            else:
                conn.execute("UPDATE tasks SET done = ?, due = COALESCE(due, ?) WHERE id = ?", (done, t["due"], rid))
    # Open in the history but no longer in the files: deleted or cancelled. Calendar rows only
    # when the calendar was read, or a missing backup would delete them all.
    gone = [(day, rid) for key, ids in active.items() for rid in ids
            if rid not in matched and (tasks["calendar"] or not key.startswith("calendar: "))]
    conn.executemany("UPDATE tasks SET gone = ? WHERE id = ?", gone)


def _history(conn, day, time_days, done_days, open_tasks, overdue, goal_weeks, tasks=None):
    if time_days:
        # The export is the truth for the days it covers (edits and renames included); days
        # before its first record stay as they were.
        days = [d for d, _ in time_days]
        conn.execute("DELETE FROM time_daily WHERE day BETWEEN ? AND ?", (min(days), max(days)))
        conn.executemany("INSERT INTO time_daily VALUES (?,?,?)",
                         [(d, a, m) for (d, a), m in sorted(time_days.items())])
    # Never lower a count: done tasks may be archived out of the to-do file later.
    conn.executemany("INSERT INTO tasks_daily (day, done) VALUES (?,?) "
                     "ON CONFLICT(day) DO UPDATE SET done = MAX(done, excluded.done)", sorted(done_days.items()))
    conn.execute("INSERT INTO tasks_daily (day, open, overdue) VALUES (?,?,?) "
                 "ON CONFLICT(day) DO UPDATE SET open = excluded.open, overdue = excluded.overdue",
                 (day, open_tasks, overdue))
    # A week is judged once and never rewritten, so a goal changed later can't re-judge it. Weeks
    # missed while the server was off are filled in; the first run only judges the latest week.
    newest = conn.execute("SELECT MAX(week) FROM week_goals").fetchone()[0]
    weeks = sorted({g[0] for g in goal_weeks})
    wanted = {w for w in weeks if w > newest} if newest else set(weeks[-1:])
    conn.executemany("INSERT OR IGNORE INTO week_goals VALUES (?,?,?,?,?)",
                     [g for g in goal_weeks if g[0] in wanted])
    if tasks is not None:
        _task_rows(conn, day, tasks)


def record_history(day, time_days, done_days, open_tasks, overdue, goal_weeks, tasks=None):
    """The brief's numbers, kept for good: time_days {(day, activity): minutes}, done_days
    {day: tasks ticked off}, today's open and overdue counts, goal_weeks as rows of
    (week, activity, minutes, target, status) for finished weeks, and tasks as
    {"open": [...], "done": [...], "calendar": read?} (None: the to-do file wasn't read)."""
    _write(_history, day, time_days, done_days, open_tasks, overdue, goal_weeks, tasks)


def _email_weekly(conn):
    """Recount emails per week and category for the weeks whose rows are all still here; older
    weeks keep the count they had before pruning began on them. Runs just before _prune."""
    cutoff = (_now() - timedelta(days=KEEP_DAYS)).date()
    first = cutoff + timedelta(days=7 - cutoff.weekday())   # the Monday after the cut-off day
    conn.execute("DELETE FROM email_weekly WHERE week >= ?", (first.isoformat(),))
    conn.execute("""
        INSERT INTO email_weekly
        SELECT date(substr(processed, 1, 10), 'weekday 0', '-6 days'), category, COUNT(*), SUM(pushed)
        FROM emails WHERE processed >= ? GROUP BY 1, 2""", (first.isoformat(),))


def _prune(conn):
    cutoff = _iso(_now() - timedelta(days=KEEP_DAYS))
    conn.execute("DELETE FROM emails WHERE processed < ?", (cutoff,))
    conn.execute("DELETE FROM bodies WHERE NOT EXISTS (SELECT 1 FROM emails e WHERE e.uidvalidity = bodies.uidvalidity "
                 "AND e.uid = bodies.uid AND e.processed >= ?)", (_iso(_now() - timedelta(days=BODY_KEEP_DAYS)),))
    conn.execute("DELETE FROM pushes WHERE sent < ?", (cutoff,))
    conn.execute("DELETE FROM runs WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM briefs WHERE day < ?", (cutoff[:10],))


# ---------- the brief's Markdown, as safe HTML ----------

def _inline(text):
    text = html.escape(text, quote=True)  # escape first: only the tags added below can exist
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"(?<![\w])_(.+?)_(?![\w])", r"<em>\1</em>", text)
    return text


def md_to_html(md):
    out, depth = [], 0      # depth: open <ul> levels; a sub-list (2) opens inside its parent's <li>

    def close_to(level):
        nonlocal depth
        while depth > level:
            out[-1] += "</li></ul>"
            depth -= 1

    for raw in md.splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^(\s*)[-*] (.*)", line)
        if bullet:
            want = 2 if len(bullet.group(1)) >= 2 and depth >= 1 else 1
            if want > depth:
                out.append("<ul>")
                depth += 1
            else:
                close_to(want)
                out[-1] += "</li>"
            out.append(f"<li>{_inline(bullet.group(2))}")
            continue
        close_to(0)
        heading = re.match(r"^(#{1,4}) (.*)", line)
        if heading:
            level = min(len(heading.group(1)) + 1, 5)   # the page has its own h1
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    close_to(0)
    return "\n".join(out)


# ---------- reads ----------

def _health(conn):
    rows = conn.execute("SELECT ts, ok, error FROM runs WHERE job = 'triage' ORDER BY id DESC LIMIT 200").fetchall()
    last_ok = next((ts for ts, ok, _ in rows if ok), None)
    fails = 0
    for _, ok, _ in rows:
        if ok:
            break
        fails += 1
    last_error = rows[0][2] if rows and not rows[0][1] else ""
    return {"last_run": rows[0][0] if rows else None, "last_ok": last_ok,
            "fails": fails, "last_error": last_error}


def decisions(uidvalidity):
    """{uid: category} the agent gave each email of this mailbox; used by triage --export-eval."""
    if not enabled() or not Path(DB_PATH).exists():
        return {}
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return dict(conn.execute("SELECT uid, category FROM emails WHERE uidvalidity = ?", (str(uidvalidity),)))
    finally:
        conn.close()


def _value(conn):
    """What the agent did in the last 7 days and in the 7 before: the page's "Agent this week" card."""
    def period(start, end):
        s, e = _iso(start), _iso(end)
        emails, pushed, by_rule, unsure = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pushed), 0), COALESCE(SUM(source != 'model'), 0), "
            "COALESCE(SUM(category = 'unsure'), 0) FROM emails WHERE processed >= ? AND processed < ?", (s, e)).fetchone()
        runs, runs_ok = conn.execute("SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM runs "
                                     "WHERE job = 'triage' AND ts >= ? AND ts < ?", (s, e)).fetchone()
        briefs = conn.execute("SELECT COUNT(*) FROM briefs WHERE day > ? AND day <= ?", (s[:10], e[:10])).fetchone()[0]
        return {"emails": emails, "pushed": pushed, "by_rule": by_rule, "unsure": unsure,
                "runs": runs, "runs_ok": runs_ok, "briefs": briefs}
    now = _now()
    return {"week": period(now - timedelta(days=7), now + timedelta(seconds=1)),  # rows written this second count
            "before": period(now - timedelta(days=14), now - timedelta(days=7))}


def _atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_bodies(wanted):
    """One text file per email in web/mail/, fetched only when you open that email. A file is
    written once (the text never changes) and deleted when the email leaves the page."""
    mail_dir = WEB_DIR / "mail"
    mail_dir.mkdir(parents=True, exist_ok=True)
    for f in mail_dir.glob("*.txt"):
        if f.stem not in wanted:
            f.unlink()
    for name, body in wanted.items():
        path = mail_dir / f"{name}.txt"
        if not path.exists():
            _atomic_write_text(path, body)


def build_web(conn):
    since = _iso(_now() - timedelta(days=PAGE_EMAIL_DAYS))
    emails = [dict(zip(
        ("uidvalidity", "uid", "received", "processed", "category", "source", "from_name",
         "from_addr", "subject", "summary", "pushed", "quiet"), r))
        for r in conn.execute(
            "SELECT * FROM emails WHERE processed >= ? "
            "ORDER BY COALESCE(received, processed) DESC LIMIT ?", (since, PAGE_EMAIL_MAX))]
    bodies = {email_anchor(uv, uid): body for uv, uid, body in conn.execute("SELECT uidvalidity, uid, body FROM bodies")}
    for e in emails:
        e["id"] = email_anchor(e.pop("uidvalidity"), e.pop("uid"))
        e["pushed"], e["quiet"] = bool(e["pushed"]), bool(e["quiet"])
        e["has_body"] = e["id"] in bodies
    _write_bodies({e["id"]: bodies[e["id"]] for e in emails if e["has_body"]})
    briefs = [{"day": d, "built": b, "html": md_to_html(m)}
              for d, b, m in conn.execute("SELECT day, built, markdown FROM briefs ORDER BY day DESC LIMIT ?",
                                          (PAGE_BRIEFS,))]
    psince = _iso(_now() - timedelta(days=PAGE_PUSH_DAYS))
    pushes = [dict(zip(("sent", "topic", "title", "message", "priority", "link"), r))
              for r in conn.execute("SELECT sent, topic, title, message, priority, link FROM pushes "
                                    "WHERE sent >= ? ORDER BY sent DESC, id DESC LIMIT 300", (psince,))]
    for p in pushes:
        p["message"] = (p["message"] or "")[:400]
    data = {"generated": _iso(_now()), "health": _health(conn), "value": _value(conn),
            "briefs": briefs, "emails": emails, "pushes": pushes}
    _atomic_write_text(WEB_DIR / "data.json", json.dumps(data, ensure_ascii=False))


def build_history(conn):
    """web/stats.json for the Stats tab: the kept-for-good tables as compact rows, oldest first.
    Written only when the rows changed, so "generated" says when they last did and the page
    redraws (closing any open table) only then; most triage runs change nothing here."""
    rows = lambda sql: [list(r) for r in conn.execute(sql)]
    data = {"time": rows("SELECT day, activity, minutes FROM time_daily ORDER BY day, activity"),
            "tasks": rows("SELECT day, done, open, overdue FROM tasks_daily ORDER BY day"),
            "goals": rows("SELECT week, activity, minutes, target, status FROM week_goals ORDER BY week, activity"),
            "email": rows("SELECT week, category, emails, pushed FROM email_weekly ORDER BY week, category"),
            # one row per task without its text; the text only for the five oldest still open
            "task_rows": rows("SELECT list, priority, due, first_seen, exact, done, gone FROM tasks ORDER BY id"),
            "tasks_oldest": rows("SELECT text, list, first_seen, exact FROM tasks WHERE done IS NULL AND gone IS NULL "
                                 "ORDER BY first_seen, id LIMIT 5")}
    path = WEB_DIR / "stats.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old.pop("generated", None)
    except (OSError, ValueError):
        old = None
    if old != data:
        _atomic_write_text(path, json.dumps({"generated": _iso(_now()), **data}, ensure_ascii=False))


def snapshot(conn):
    """backup/agent.db: a consistent copy of the database, at most once a day. Syncthing copying
    the live file could catch it mid-write, and its versioning would keep a copy of every
    10-minute change; this file changes once a day and is always whole."""
    final = Path(DB_PATH).parent / "backup" / "agent.db"
    if final.exists() and time.time() - final.stat().st_mtime < SNAPSHOT_HOURS * 3600:
        return
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name("agent.db.tmp")
    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(tmp)
    try:
        conn.backup(out)   # SQLite's own online backup: a whole, consistent copy
    finally:
        out.close()
    os.replace(tmp, final)


def export_stats(conn):
    """Counts only (no subjects, names or addresses), for Grafana on the NAS."""
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    final = STATS_DIR / "agent-stats.db"
    tmp = STATS_DIR / "agent-stats.db.tmp"
    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(tmp)
    try:
        out.executescript("""
            CREATE TABLE email_daily (time INTEGER, day TEXT, category TEXT, sorted_by TEXT,
                                      emails INTEGER, pushed INTEGER, quiet INTEGER);
            CREATE TABLE push_daily (time INTEGER, day TEXT, topic TEXT, pushes INTEGER);
            CREATE TABLE triage_daily (time INTEGER, day TEXT, ok_runs INTEGER, failed_runs INTEGER);
            CREATE TABLE briefs (time INTEGER, day TEXT, built TEXT);
            CREATE TABLE health (updated TEXT, last_ok TEXT, minutes_since_ok INTEGER, fails INTEGER);
            CREATE TABLE time_daily (time INTEGER, day TEXT, activity TEXT, minutes INTEGER);
            CREATE TABLE tasks_daily (time INTEGER, day TEXT, done INTEGER, open INTEGER, overdue INTEGER);
            CREATE TABLE week_goals (time INTEGER, week TEXT, activity TEXT, minutes INTEGER,
                                     target INTEGER, status TEXT);
            CREATE TABLE email_weekly (time INTEGER, week TEXT, category TEXT, emails INTEGER, pushed INTEGER);
            CREATE TABLE task_weekly (time INTEGER, week TEXT, list TEXT, added INTEGER, done INTEGER,
                                      done_with_due INTEGER, done_on_time INTEGER);
        """)
        day_epoch = "CAST(strftime('%s', {0}) AS INTEGER)"
        rows = conn.execute(f"""
            SELECT {day_epoch.format("substr(COALESCE(received, processed), 1, 10)")},
                   substr(COALESCE(received, processed), 1, 10),
                   category,
                   CASE WHEN source LIKE 'rule%' THEN 'rule' ELSE 'model' END,
                   COUNT(*), SUM(pushed), SUM(quiet)
            FROM emails GROUP BY 2, 3, 4 ORDER BY 1""").fetchall()
        out.executemany("INSERT INTO email_daily VALUES (?,?,?,?,?,?,?)", rows)
        rows = conn.execute(f"""
            SELECT {day_epoch.format("substr(sent, 1, 10)")}, substr(sent, 1, 10), topic, COUNT(*)
            FROM pushes GROUP BY 2, 3 ORDER BY 1""").fetchall()
        out.executemany("INSERT INTO push_daily VALUES (?,?,?,?)", rows)
        rows = conn.execute(f"""
            SELECT {day_epoch.format("substr(ts, 1, 10)")}, substr(ts, 1, 10), SUM(ok), SUM(1 - ok)
            FROM runs WHERE job = 'triage' GROUP BY 2 ORDER BY 1""").fetchall()
        out.executemany("INSERT INTO triage_daily VALUES (?,?,?,?)", rows)
        rows = conn.execute(f"SELECT {day_epoch.format('day')}, day, built FROM briefs ORDER BY day").fetchall()
        out.executemany("INSERT INTO briefs VALUES (?,?,?)", rows)
        for table, cols, key in (("time_daily", "day, activity, minutes", "day"),
                                 ("tasks_daily", "day, done, open, overdue", "day"),
                                 ("week_goals", "week, activity, minutes, target, status", "week"),
                                 ("email_weekly", "week, category, emails, pushed", "week")):
            rows = conn.execute(f"SELECT {day_epoch.format(key)}, {cols} FROM {table} ORDER BY {key}").fetchall()
            out.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * (cols.count(',') + 2))})", rows)
        # tasks as counts per week and list (no task text leaves the server); "added" only counts
        # tasks whose first day is known
        week = "date({0}, 'weekday 0', '-6 days')"
        rows = conn.execute(f"""
            SELECT {day_epoch.format('w')}, w, l, SUM(added), SUM(done), SUM(with_due), SUM(on_time) FROM (
                SELECT {week.format('first_seen')} AS w, COALESCE(list, '') AS l, 1 AS added, 0 AS done,
                       0 AS with_due, 0 AS on_time FROM tasks WHERE exact = 1
                UNION ALL
                SELECT {week.format('done')}, COALESCE(list, ''), 0, 1, due IS NOT NULL, done <= due
                FROM tasks WHERE done IS NOT NULL)
            GROUP BY w, l ORDER BY w""").fetchall()
        out.executemany("INSERT INTO task_weekly VALUES (?,?,?,?,?,?,?)", rows)
        h = _health(conn)
        minutes = None
        if h["last_ok"]:
            minutes = int((_now() - datetime.fromisoformat(h["last_ok"])).total_seconds() // 60)
        out.execute("INSERT INTO health VALUES (?,?,?,?)", (_iso(_now()), h["last_ok"], minutes, h["fails"]))
        out.commit()
    finally:
        out.close()
    os.replace(tmp, final)


def refresh():
    """After a run: drop old rows, rebuild the page data and the Grafana stats."""
    if not enabled():
        return
    try:
        conn = connect()
        try:
            with conn:
                _email_weekly(conn)   # before _prune, so a week is counted while it is complete
                _prune(conn)
            build_web(conn)
            build_history(conn)
            export_stats(conn)
            snapshot(conn)
        finally:
            conn.close()
    except Exception as exc:
        _warn("refresh", exc)
