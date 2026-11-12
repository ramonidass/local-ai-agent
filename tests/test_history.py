"""Numbers kept for good: time per day, tasks per day, weekly goals judged once, email per week."""
import json, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

d = Path(tempfile.mkdtemp())
for sub in ("cal", "time", "extra"):
    (d / sub).mkdir()
os.environ.update(CAL_DIR=str(d / "cal"), EXTRA_ICS_DIR=str(d / "extra"), TIME_DIR=str(d / "time"),
                  TODO_FILES=str(d / "To-do.md"), EMAIL_DIGEST=str(d / "none.jsonl"), OLLAMA_URL="",
                  TIME_GOALS="Exercise=3h; Reading=2h",
                  AGENT_DB=str(d / "data" / "agent.db"), AGENT_WEB_DIR=str(d / "data" / "web"),
                  AGENT_STATS_DIR=str(d / "data" / "stats"))
sys.path.insert(0, str(Path(__file__).parent))
import agentdb  # noqa
import brief  # noqa

TZ = brief.TZ
HEADER = "activity name,time started,time ended,comment,categories,record tags,duration,duration minutes\r\n"
csv_path = d / "time" / "export.csv"


def export(rows, made):
    """Write the time-tracker export as made at `made` (its mtime decides which weeks can be judged)."""
    csv_path.write_text(HEADER + "".join(f"{a},{s},{e},,,,,\r\n" for a, s, e in rows), encoding="utf-8")
    os.utime(csv_path, (made.timestamp(), made.timestamp()))


def run(now):
    brief.build(now, consume_email=False)
    agentdb.record_history(**brief.build.history)
    agentdb.refresh()
    return brief.build.history


def db(sql, *args):
    return sqlite3.connect(os.environ["AGENT_DB"]).execute(sql, args).fetchall()


rows = [("Reading", "2026-10-01 20:00:00", "2026-10-01 20:45:00"),      # long ago: kept later on
        ("Exercise", "2026-10-27 18:00:00", "2026-10-27 19:00:00"),     # week of 26 Oct: 1h
        ("Exercise", "2026-11-03 18:00:00", "2026-11-03 19:00:00"),     # week of 2 Nov: 3h 10m
        ("Exercise", "2026-11-05 18:00:00", "2026-11-05 20:10:00"),
        ("Reading", "2026-11-04 21:00:00", "2026-11-04 21:30:00")]      # week of 2 Nov: 30m
(d / "To-do.md").write_text("- [x] A ✅ 2026-11-03\n- [x] B ✅ 2026-11-03\n- [x] C ✅ 2026-10-20\n"
                            "- [ ] Late 📅 2026-11-01\n- [ ] Someday\n", encoding="utf-8")
monday = datetime(2026, 11, 9, 7, 55, tzinfo=TZ)

# 1) an export made before the week ended can't judge it; the week before is fine
export(rows, datetime(2026, 11, 8, 23, 0, tzinfo=TZ))
brief.build(monday, consume_email=False)
assert {g[0] for g in brief.build.history["goal_weeks"]} == {"2026-10-12", "2026-10-19", "2026-10-26"}
print("late export OK")

# 2) first real run: daily minutes, tasks, and only the latest finished week judged
export(rows, datetime(2026, 11, 9, 6, 0, tzinfo=TZ))
h = run(monday)
assert h["time_days"][("2026-11-05", "Exercise")] == 130 and h["time_days"][("2026-10-01", "Reading")] == 45
assert h["done_days"] == {"2026-11-03": 2, "2026-10-20": 1}, h["done_days"]
assert (h["open_tasks"], h["overdue"]) == (2, 1)
assert db("SELECT * FROM week_goals ORDER BY activity") == [
    ("2026-11-02", "Exercise", 190, 180, "done"), ("2026-11-02", "Reading", 30, 120, "missed")]
assert db("SELECT * FROM tasks_daily ORDER BY day") == [("2026-10-20", 1, None, None),
                                                        ("2026-11-03", 2, None, None), ("2026-11-09", 0, 2, 1)]
print("first run OK")

# 3) a changed goal doesn't re-judge a stored week; a week missed while the server was off is filled
#    in with the goal of the day it's judged
brief.TIME_GOALS = "Exercise=4h; Reading=2h"
run(monday + timedelta(hours=1))
assert db("SELECT target, status FROM week_goals WHERE week = '2026-11-02' AND activity = 'Exercise'") == [(180, "done")]
rows += [("Exercise", "2026-11-10 07:00:00", "2026-11-10 11:00:00")]   # 4h in the week of 9 Nov
export(rows, datetime(2026, 11, 23, 6, 0, tzinfo=TZ))
run(datetime(2026, 11, 23, 7, 55, tzinfo=TZ))                           # no run on the 16th
assert db("SELECT week, activity, minutes, target, status FROM week_goals WHERE week > '2026-11-02' "
          "ORDER BY week, activity") == [
    ("2026-11-09", "Exercise", 240, 240, "done"), ("2026-11-09", "Reading", 0, 120, "missed"),
    ("2026-11-16", "Exercise", 0, 240, "missed"), ("2026-11-16", "Reading", 0, 120, "missed")]
print("weeks judged once OK")

# 4) the export is the truth for the days it covers (a renamed record moves), older days stay;
#    done tasks archived out of the file keep their count; no export = no verdicts
rows = [r for r in rows if r[1] >= "2026-10-27"]
rows[1] = ("Sport", rows[1][1], rows[1][2])
export(rows, datetime(2026, 11, 23, 7, 0, tzinfo=TZ))
(d / "To-do.md").write_text("- [ ] Someday\n", encoding="utf-8")
run(datetime(2026, 11, 23, 8, 0, tzinfo=TZ))
assert db("SELECT activity, minutes FROM time_daily WHERE day = '2026-11-03'") == [("Sport", 60)]
assert db("SELECT minutes FROM time_daily WHERE day = '2026-10-01'") == [(45,)]
assert db("SELECT done FROM tasks_daily WHERE day = '2026-11-03'") == [(2,)]
assert db("SELECT open, overdue FROM tasks_daily WHERE day = '2026-11-23'") == [(1, 0)]
csv_path.unlink()
brief.build(datetime(2026, 11, 30, 7, 55, tzinfo=TZ), consume_email=False)
assert brief.build.history["goal_weeks"] == [] and brief.build.history["time_days"] == {}
print("re-export, archived tasks, no export OK")

# 5) email counts per week outlive the emails, and a week half pruned keeps its full count
real_now = agentdb._now
week = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)                 # a Monday
for uid, (day, cat) in enumerate([(0, "work"), (0, "ads"), (4, "work")], 1):
    agentdb._now = lambda: week + timedelta(days=day)
    agentdb.record_email({"uid": uid, "category": cat, "subject": "Secret plans"}, "7", pushed=cat == "work")
agentdb.refresh()
assert db("SELECT * FROM email_weekly ORDER BY category") == [("2026-06-01", "ads", 1, 0), ("2026-06-01", "work", 2, 2)]
agentdb._now = lambda: week + timedelta(days=agentdb.KEEP_DAYS + 2)     # cut-off on Wednesday
agentdb.refresh()
assert db("SELECT COUNT(*) FROM emails") == [(1,)]                      # Monday's two are gone
assert db("SELECT * FROM email_weekly ORDER BY category") == [("2026-06-01", "ads", 1, 0), ("2026-06-01", "work", 2, 2)]
agentdb._now = lambda: week + timedelta(days=agentdb.KEEP_DAYS + 30)
agentdb.refresh()
assert db("SELECT COUNT(*) FROM emails") == [(0,)]
assert db("SELECT SUM(emails) FROM email_weekly") == [(3,)]
agentdb._now = real_now
print("email weekly OK")

# 6) the page's stats.json and Grafana's copy have the same rows, and no email text
stats = json.loads((d / "data" / "web" / "stats.json").read_text())
assert ["2026-11-02", "Exercise", 190, 180, "done"] in stats["goals"]
assert ["2026-06-01", "work", 2, 2] in stats["email"] and ["2026-10-01", "Reading", 45] in stats["time"]
st = sqlite3.connect(d / "data" / "stats" / "agent-stats.db")
assert st.execute("SELECT COUNT(*) FROM time_daily").fetchone()[0] == len(stats["time"])
assert st.execute("SELECT COUNT(*) FROM week_goals").fetchone()[0] == len(stats["goals"]) == 6
assert st.execute("SELECT time FROM week_goals WHERE week = '2026-11-02' LIMIT 1").fetchone()[0] == \
    int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())
assert b"Secret plans" not in (d / "data" / "stats" / "agent-stats.db").read_bytes()
print("stats.json + Grafana OK")

# 7) stats.json is rewritten only when its rows change (most triage runs change nothing there)
stats_path = d / "data" / "web" / "stats.json"
before = stats_path.read_text()
agentdb.refresh()
assert stats_path.read_text() == before                  # same rows: same file, same "generated"
agentdb.record_history("2026-11-30", {("2026-11-29", "Reading"): 20}, {}, 1, 0, [])
agentdb.refresh()
assert ["2026-11-29", "Reading", 20] in json.loads(stats_path.read_text())["time"]
print("stats.json only on change OK")

# 8) backup/agent.db: a whole copy once a day, never rewritten in between
snap = d / "data" / "backup" / "agent.db"
assert snap.exists() and not (d / "data" / "backup" / "agent.db.tmp").exists()
copy = sqlite3.connect(snap)
assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert copy.execute("SELECT COUNT(*) FROM week_goals").fetchone()[0] == 2   # made on the first run
copy.close()
made = snap.stat().st_mtime
agentdb.refresh()
assert snap.stat().st_mtime == made                      # less than a day old: left alone
os.utime(snap, (made - 24 * 3600, made - 24 * 3600))
agentdb.refresh()
assert snap.stat().st_mtime > made - 3600                # a day old: renewed, with today's rows
assert sqlite3.connect(snap).execute("SELECT COUNT(*) FROM week_goals").fetchone()[0] == 6
print("daily snapshot OK")
print("ALL HISTORY TESTS PASSED")
