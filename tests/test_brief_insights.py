"""Time tracking, task completion, clashes and the local-model summary (with a fake Ollama)."""
import json, os, sys, tempfile, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

d = Path(tempfile.mkdtemp())
for sub in ("cal", "time", "extra"):
    (d / sub).mkdir()

replies = []          # what the fake model will answer, one per call
seen_prompts = []


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen_prompts.append(req)
        self.send_response(200); self.end_headers()
        r = replies.pop(0)   # a list of bullets, or the whole reply as a dict
        self.wfile.write(json.dumps({"message": {"content": json.dumps(r if isinstance(r, dict) else {"bullets": r})}}).encode())


srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ.update(CAL_DIR=str(d / "cal"), EXTRA_ICS_DIR=str(d / "extra"), TIME_DIR=str(d / "time"),
                  TODO_FILES=str(d / "To-do.md"), EMAIL_DIGEST=str(d / "none.jsonl"),
                  OLLAMA_URL=f"http://127.0.0.1:{srv.server_address[1]}/api/chat",
                  TIME_ACTIVITY_MAP="Calisthenics=Exercise:Calisthenics; Sport/Gym=Exercise",
                  TIME_TAG_MAP="Sport/Gym=Gym")
sys.path.insert(0, str(Path(__file__).parent))
import brief  # noqa

TZ = brief.TZ
now = datetime(2026, 10, 28, 7, 55, tzinfo=TZ)  # Wednesday

(d / "cal" / "cal.ics").write_text("""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
DTSTART;TZID=Europe/London:20261028T100000
DTEND;TZID=Europe/London:20261028T110000
SUMMARY:Team call
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;TZID=Europe/London:20261028T103000
DTEND;TZID=Europe/London:20261028T113000
SUMMARY:Dentist
END:VEVENT
END:VCALENDAR
""")

# Exactly what SimpleTimeTracker writes: CRLF rows, quoted categories, mixed time formats.
csv_text = (
    "activity name,time started,time ended,comment,categories,record tags,duration,duration minutes\r\n"
    'Sport/Gym,2026-10-26 18:00:00,2026-10-26 19:00:00,,"Gym, Calisthenics",PullUps&PushUps (105),1:0:0,60\r\n'
    'Exercise,2026-10-27 20:00:00,2026-10-27 20:20:00,,,Sport/Gym,0:20:0,20\r\n'      # old tag name -> Gym
    'Sport/Gym,2026-10-19 18:00:00,2026-10-19 20:00:00,"legs, back",Gym,,2:0:0,120\r\n'
    'Sport/Gym,2026-10-20 18:00:00,2026-10-20 19:10:00,,Gym,,1:10:0,70\r\n'
    "Kafka course,2026-10-27T18:00:00Z,2026-10-27T21:00:00Z,,Study,Linux,3:0:0,180\r\n"
    "Kafka course,2026-10-20T19:00:00+01:00,2026-10-20T20:00:00+01:00,,Study,,1:0:0,60\r\n"
    "Kafka course,2026-10-21 09:00:00,2026-10-21 10:00:00,,Study,,1:0:0,60\r\n"   # later in last week: counted
    "Calisthenics,2026-10-23 18:00:00,2026-10-23 18:45:00,,Calisthenics,,0:45:0,45\r\n"  # last Friday: counted
    "Calisthenics,2026-10-27 07:00:00,2026-10-27 07:30:00,,Calisthenics,PullUps&PushUps (50),0:30:0,30\r\n"
)
(d / "time" / "export.csv").write_text(csv_text, encoding="utf-8")

(d / "To-do.md").write_text(
    "- [x] A ✅ 2026-10-27\n- [x] B ✅ 2026-10-20\n- [x] C ✅ 2026-10-26\n- [ ] Open thing\n", encoding="utf-8")

# 1) facts are right
replies.append([
    "Team call and Dentist overlap today at 10:00, so move one of them.",
    "Exercise so far: 1h 50m this week, 3h 10m on the same days last week.",
    "You exercised 5 hours less than usual.",          # invented number -> dropped
    "Kafka study is up strongly this week.",
])
text, c = brief.build(now, consume_email=False)
facts = brief.build.facts_text
print(facts, "\n----\n", text)
assert "Clash: Team call and Dentist overlap on Wed 28 Oct." in facts
# old activities folded into Exercise; last week only counts Mon until Wed 07:55 (not Friday's 45m)
assert "Exercise: 1h 50m this week, 3h 10m last week ▼ -42%." in facts, facts
assert "Calisthenics:" not in facts and "Sport/Gym" not in facts and "(category)" not in facts
assert "Kafka course: 3h this week, 1h last week ▲ +200%. Tags this period: Linux 3h." in facts  # UTC and +01:00 rows parsed
assert "Tasks completed: 2 in the last 7 days vs 1 in the 7 days before." in facts
assert "this week so far (Mon–Wed) vs the same days last week" in facts

# 2) summary sits under the title, invented figure dropped and reported
lines = text.splitlines()
assert lines[2] == "## Summary" and lines[3].startswith("- **2 events today, "), lines[:5]
assert "overlap today at 10:00" in text and "Exercise so far" in text and "Kafka study is up" in text
assert "5 hours" not in text and "were dropped" not in text
tt = text.split("## Time tracked")[1].split("\n## ")[0]
assert "_This week so far (Mon–Wed) vs the same days last week; the bars are the last 8 weeks_" in tt, tt
assert "- **Kafka course** 3h · last week 1h ▲ +200% · ▁▁▁▁▁▁▁█\n  - Linux 3h\n" in tt, tt
assert "- **Exercise** 1h 50m · last week 3h 10m ▼ -42% · ▁▁▁▁▁▁▁█\n  - PullUps&PushUps 1h 30m (155), Calisthenics 30m, Gym 20m" in tt, tt
assert tt.index("Kafka") < tt.index("Exercise")  # most time this week first
assert seen_prompts[-1]["think"] is False and "FACTS:" in seen_prompts[-1]["messages"][1]["content"]

# 3) Monday compares full weeks
replies.append(["Quiet week ahead."])
text_mon, _ = brief.build(datetime(2026, 11, 2, 7, 55, tzinfo=TZ), consume_email=False)
assert "last week vs the week before" in brief.build.facts_text
assert "Exercise: 1h 50m last week, 3h 55m the week before ▼ -53%" in brief.build.facts_text

# 4) model down -> brief still complete, with a warning
brief.OLLAMA_URL = "http://127.0.0.1:9/api/chat"
text_down, c_down = brief.build(now, consume_email=False)
assert text_down.splitlines()[4] == "" and "Summary not written" in text_down and "## Today" in text_down
assert brief.hm(0) == "0m" and brief.hm(15) == "15m" and brief.hm(120) == "2h" and brief.hm(150) == "2h 30m"
assert brief.parse_tags("Bible, PullUps (1.5), Push (20)") == [("Bible", None), ("PullUps", 1.5), ("Push", 20.0)]

# 5) weekly goals, wins and slipping (decided by code), focus from the model (numbers checked too)
assert brief.parse_goals("Exercise=3h; Reading=90m; CS=2h 30m; Go=1.5h; bad=xyz; =2h") == \
    {"Exercise": 180, "Reading": 90, "CS": 150, "Go": 90}
assert brief.sparkline([0, 4, 8]) == "▁▅█" and brief.sparkline([0, 0]) == ""
wed = now.date()
assert brief.streak({wed, wed.replace(day=27), wed.replace(day=26)}, wed) == 3
assert brief.streak({wed.replace(day=27), wed.replace(day=26)}, wed) == 2 and brief.streak(set(), wed) == 0
brief.OLLAMA_URL = f"http://127.0.0.1:{srv.server_address[1]}/api/chat"
brief.TIME_GOALS = "Exercise=3h; Kafka course=2h; Reading=1h"
replies.append({"bullets": ["Team call and Dentist clash."],
                "focus": ["Read for 12m this evening.", "Read for 99 minutes."]})   # 99 is made up -> dropped
text, _ = brief.build(now, consume_email=False)
facts = brief.build.facts_text
assert "Weekly goal Reading: 0m of 1h, behind (17m expected after 2 of 7 days); 1h to go, 12m a day." in facts, facts
assert "Weekly goal Kafka course: 3h of 2h, done." in facts
assert "SLIPPING: Reading 0m / 1h · behind." in facts and "WIN: Exercise 1h 50m / 3h · on pace." in facts
sections = [l for l in text.splitlines() if l.startswith("## ")]
assert sections[:6] == ["## Summary", "## Focus today", "## Wins", "## Slipping", "## Weekly goals", "## Today"], sections
assert "## Focus today\n- Read for 12m this evening.\n\n" in text and "99" not in text
assert "were dropped" not in text
assert ("## Wins\n- **Tasks done** 2 in 7 days · 1 the week before ▲\n- **Exercise** 1h 50m / 3h · on pace\n"
        "- **Kafka course** 3h / 2h · done\n") in text, text
assert "## Slipping\n- **Reading** 0m / 1h · behind\n" in text      # goal activity: not repeated as ▼ -42%
assert ("## Weekly goals\n_2 of 7 days done, 5 left_\n\n- **Exercise** 1h 50m / 3h · on pace · 1h 10m to go, 14m a day\n"
        "- **Kafka course** 3h / 2h · done\n- **Reading** 0m / 1h · behind · 1h to go, 12m a day\n") in text, text
assert "## Last week in review" not in text                       # Monday only

# 6) Monday: goals judge the whole of last week, plus a short review
replies.append({"bullets": ["Quiet week ahead."], "focus": ["Plan 3h of exercise this week."]})
text, _ = brief.build(datetime(2026, 11, 2, 7, 55, tzinfo=TZ), consume_email=False)
assert "## Weekly goals\n_Last week (complete)_\n\n- **Exercise** 1h 50m / 3h · missed\n" in text, text
review = text.split("## Last week in review\n")[1].split("\n\n")[0]
assert review == ("- **Total tracked** 4h 50m · the week before 5h 55m ▼ -18%\n"
                  "- **Best day** Tuesday 27 Oct, 3h 50m\n"
                  "- **Biggest rise** Kafka course +1h\n"
                  "- **Biggest drop** Exercise -2h 05m\n"
                  "- **Goals hit** 1 of 3\n"
                  "- **Tasks done** 1 in the last 7 days"), review
assert "Last week: Best day Tuesday 27 Oct, 3h 50m." in brief.build.facts_text

# 7) no goals set and no time data: no goals card, and Wins/Slipping only when there is something
brief.TIME_GOALS = ""
brief.OLLAMA_URL = ""
text, _ = brief.build(now, consume_email=False)
assert "## Weekly goals" not in text and "## Wins" in text and "## Focus today" not in text
assert "Open in Obsidian" not in text

# 8) shorter brief: an empty calendar is one line; the tasks card links to Obsidian
brief.TASKS_LINK = "obsidian://open?vault=MyVault&file=Tasks%2FTo-do"
text, _ = brief.build(datetime(2026, 11, 12, 7, 55, tzinfo=TZ), consume_email=False)
assert "## Calendar\n- Nothing today or in the next 7 days.\n" in text and "## Today" not in text, text
assert "\n_Open in Obsidian: obsidian://open?vault=MyVault&file=Tasks%2FTo-do_\n" in text.split("## Tasks")[1]
import agentdb
assert "<p><em>Open in Obsidian: obsidian://open?vault=MyVault&amp;file=Tasks%2FTo-do</em></p>" in agentdb.md_to_html(text)
print("INSIGHTS OK")
