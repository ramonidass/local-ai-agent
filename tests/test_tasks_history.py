"""Task history: one row per to-do (list, first seen, done or deleted), through brief.py and agentdb."""
import json, os, sqlite3, sys, tempfile
from datetime import datetime
from pathlib import Path

d = Path(tempfile.mkdtemp())
for sub in ("cal", "time", "extra"):
    (d / sub).mkdir()
os.environ.update(CAL_DIR=str(d / "cal"), EXTRA_ICS_DIR=str(d / "extra"), TIME_DIR=str(d / "time"),
                  TODO_FILES=str(d / "To-do.md"), EMAIL_DIGEST=str(d / "none.jsonl"), OLLAMA_URL="",
                  AGENT_DB=str(d / "data" / "agent.db"), AGENT_WEB_DIR=str(d / "data" / "web"),
                  AGENT_STATS_DIR=str(d / "data" / "stats"))
sys.path.insert(0, str(Path(__file__).parent))
import agentdb  # noqa
import brief  # noqa

TZ = brief.TZ
todo = d / "To-do.md"
cal = d / "cal" / "calendar.ics"
CAL = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
UID:t1
SUMMARY:Renew passport
DTSTART;VALUE=DATE:20261115
STATUS:NEEDS-ACTION
END:VTODO
BEGIN:VTODO
UID:t2
SUMMARY:Water plants
DTSTART;VALUE=DATE:20261101
RRULE:FREQ=WEEKLY
END:VTODO
BEGIN:VTODO
UID:t3
SUMMARY:Call bank
DTSTART;VALUE=DATE:20261105
STATUS:COMPLETED
COMPLETED:20261106T100000Z
END:VTODO
END:VCALENDAR
"""


def run(day):
    brief.build(datetime.fromisoformat(f"{day}T07:55").replace(tzinfo=TZ), consume_email=False)
    agentdb.record_history(**brief.build.history)
    agentdb.refresh()


def rows(where="1"):
    """{text: (list, first_seen, exact, done, gone)}"""
    return {r[0]: r[1:] for r in sqlite3.connect(os.environ["AGENT_DB"]).execute(
        f"SELECT text, list, first_seen, exact, done, gone FROM tasks WHERE {where} ORDER BY id")}


# 1) first run: everything already there has an unknown age; old done tasks keep no list;
#    one-off calendar tasks count under "Calendar", repeating ones are left out
cal.write_text(CAL)
todo.write_text("""## Inbox
- [ ] Sell keyboard 🔼
## Computer science
- [ ] Encrypt documents ⏫ 📅 2026-11-20
### Server
- [ ] Backup NAS 📅 2026-11-12
- [ ] Old idea
## Done
- [x] Fix tailscale ⏫ 📅 2026-11-07 ✅ 2026-11-07
- [x] No date on this one
""", encoding="utf-8")
run("2026-11-10")
r = rows()
assert r["Sell keyboard"] == ("Inbox", "2026-11-10", 0, None, None), r["Sell keyboard"]
assert r["Backup NAS"][0] == "Computer science › Server"
assert r["Fix tailscale"] == (None, "2026-11-07", 0, "2026-11-07", None), r["Fix tailscale"]
assert "No date on this one" not in r                    # done without a ✅ date: nothing to count
assert r["Renew passport"][:3] == ("Calendar", "2026-11-10", 0) and r["Call bank"][3] == "2026-11-06"
assert "Water plants" not in r
print("first run OK")

# 2) next day: a task moved to Done keeps the list it had while open; a new one is added with a
#    known day; a removed one is "gone"; two identical tasks done the same day count twice;
#    running twice changes nothing
todo.write_text("""## Inbox
- [ ] Sell keyboard 🔼
- [ ] Buy switches
## Computer science
- [ ] Encrypt documents ⏫ 📅 2026-11-20
## Done
- [x] Backup NAS 📅 2026-11-12 ✅ 2026-11-11
- [x] Test ✅ 2026-11-11
- [x] Test ✅ 2026-11-11
- [x] Fix tailscale ⏫ 📅 2026-11-07 ✅ 2026-11-07
""", encoding="utf-8")
run("2026-11-11")
run("2026-11-11")
r = rows()
assert r["Backup NAS"] == ("Computer science › Server", "2026-11-10", 0, "2026-11-11", None), r["Backup NAS"]
assert r["Buy switches"] == ("Inbox", "2026-11-11", 1, None, None), r["Buy switches"]
assert r["Old idea"][4] == "2026-11-11"
db = sqlite3.connect(os.environ["AGENT_DB"])
assert db.execute("SELECT COUNT(*), MIN(exact), MIN(first_seen) FROM tasks WHERE text = 'Test'").fetchone() == (2, 1, "2026-11-11")
assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 10, db.execute("SELECT text FROM tasks").fetchall()
print("moves, adds, deletes, duplicates, re-runs OK")

# 3) a missing to-do file or calendar deletes nothing; changed wording is a new task
todo.rename(d / "away.md")
cal.unlink()
run("2026-11-12")
assert rows("gone IS NOT NULL").keys() == {"Old idea"}
(d / "away.md").rename(todo)
run("2026-11-12")                                        # calendar still missing: its rows stay open
assert rows("gone IS NOT NULL").keys() == {"Old idea"}
todo.write_text(todo.read_text(encoding="utf-8").replace("Buy switches", "Buy silent switches"), encoding="utf-8")
cal.write_text(CAL.replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED\nCOMPLETED:20261113T090000Z"))
run("2026-11-13")
r = rows()
assert r["Buy switches"][4] == "2026-11-13" and r["Buy silent switches"][1:3] == ("2026-11-13", 1)
assert r["Renew passport"][3] == "2026-11-13", r["Renew passport"]   # ticked off in Fossify
print("missing files, renames, calendar done OK")

# 4) the page gets the rows without text (the text only for the 5 oldest open); Grafana gets
#    counts per week and list
stats = json.loads((d / "data" / "web" / "stats.json").read_text())
assert len(stats["task_rows"]) == 11 and all(len(t) == 7 for t in stats["task_rows"])
assert not any("Sell keyboard" in json.dumps(t) for t in stats["task_rows"])
assert [t[0] for t in stats["tasks_oldest"]] == ["Sell keyboard", "Encrypt documents", "Buy silent switches"]
st = sqlite3.connect(d / "data" / "stats" / "agent-stats.db")
assert st.execute("SELECT SUM(added), SUM(done) FROM task_weekly").fetchone() == (4, 6)
assert st.execute("SELECT done_with_due, done_on_time FROM task_weekly "
                  "WHERE week = '2026-11-09' AND list = 'Computer science › Server'").fetchone() == (1, 1)
assert b"Sell keyboard" not in (d / "data" / "stats" / "agent-stats.db").read_bytes()
print("stats.json + Grafana OK")

# 5) QuickAdd writes [category:: X] before the 📅 (Tasks needs its dates last). A task added and
#    done between two syncs takes the field, a subsection as its full path; one seen open keeps its
#    heading; open, the heading wins; a heading that no longer exists is kept as written
todo.write_text("""## Inbox
- [ ] Sell keyboard 🔼
## Research
- [ ] Read paper [category:: Inbox] 📅 2026-11-20
## Computer science
### Server
### Learning
## Done
- [x] Order cables [category:: ## Research] 📅 2026-11-15 ✅ 2026-11-14
- [x] Set up RAID [category:: ### Server] 📅 2026-11-15 ✅ 2026-11-14
- [x] Watch lecture [category:: Learning] ✅ 2026-11-14
- [x] Sort photos [category:: ## Hobbies] ✅ 2026-11-14
- [x] Encrypt documents [category:: Inbox] ⏫ 📅 2026-11-20 ✅ 2026-11-14
""", encoding="utf-8")
run("2026-11-14")
db = sqlite3.connect(os.environ["AGENT_DB"])
assert db.execute("SELECT list, due, first_seen, done FROM tasks WHERE text = 'Order cables'").fetchone() == \
    ("Research", "2026-11-15", "2026-11-14", "2026-11-14")
assert db.execute("SELECT text, list FROM tasks WHERE text IN ('Set up RAID', 'Watch lecture', 'Sort photos') "
                  "ORDER BY text").fetchall() == [("Set up RAID", "Computer science › Server"),
                                                  ("Sort photos", "Hobbies"),
                                                  ("Watch lecture", "Computer science › Learning")]
assert db.execute("SELECT list, done FROM tasks WHERE text = 'Encrypt documents'").fetchone() == \
    ("Computer science", "2026-11-14")
assert db.execute("SELECT list, due FROM tasks WHERE text = 'Read paper'").fetchone() == ("Research", "2026-11-20")
print("category field OK")
print("ALL TASK HISTORY TESTS PASSED")
