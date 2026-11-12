# Obsidian tasks: priority icons, sort order, the "## Section" each task is under, stray date emoji.
import os, sys, tempfile
from pathlib import Path
d = Path(tempfile.mkdtemp())
(d / "To-do.md").write_text("""# To-do
## Inbox
- [ ] Sell the old keyboard  🔼  📆
## Computer science
- [ ] Kubernetes course
- [ ] Low thing 🔽
- [ ] Compare two backup tools ⏫
### Learning
- [ ] Write an interpreter
## Research
- [ ] Read a paper
- [ ] Fix tailscale access 📆 2026-11-07
- [ ] Spiral calendar 🗓️ 2026-11-09 ⏫
- [ ] Classic due 📅️2026-11-10
- [ ] Inline field [due:: 2026-11-11]
- [ ] Scheduled ⏳ 2026-11-12

## Done
- [x] Fg    📆 ✅ 2026-11-02
""", encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import brief
tasks, missing = brief.parse_tasks([d / "To-do.md"])
by = {t["text"]: t for t in tasks}
assert set(by) == {"Sell the old keyboard", "Kubernetes course", "Low thing", "Compare two backup tools",
                   "Write an interpreter", "Read a paper", "Fix tailscale access", "Spiral calendar",
                   "Classic due", "Inline field", "Scheduled"}, by
# every calendar emoji works as a due date, with or without U+FE0F, and leaves the text clean
from datetime import date
assert by["Fix tailscale access"]["due"] == date(2026, 11, 7), by["Fix tailscale access"]
assert by["Spiral calendar"]["due"] == date(2026, 11, 9) and by["Spiral calendar"]["prio"] == 1
assert by["Classic due"]["due"] == date(2026, 11, 10) and by["Inline field"]["due"] == date(2026, 11, 11)
assert by["Scheduled"]["due"] == date(2026, 11, 12) and by["Read a paper"]["due"] is None
assert by["Sell the old keyboard"]["due"] is None    # 📆 with no date after it
assert by["Sell the old keyboard"]["section"] == "Inbox" and by["Sell the old keyboard"]["prio"] == 2
assert by["Compare two backup tools"]["prio"] == 1 and by["Kubernetes course"]["prio"] == 3
assert by["Low thing"]["prio"] == 4 and by["Kubernetes course"]["section"] == "Computer science"
# a "###" heading keeps its "##" parent; the next "##" starts again
assert by["Write an interpreter"]["section"] == "Computer science › Learning", by["Write an interpreter"]
assert by["Read a paper"]["section"] == "Research"
print("TASKS OK")
