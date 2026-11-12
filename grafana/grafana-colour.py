#!/usr/bin/env python3
"""Time Tracker dashboard: gradient bars for "Hours by activity", bring back "Hours by tag",
tidy the layout. Run next to the dashboard file; it keeps a .bak copy first.

  python3 grafana-colour.py grafana/dashboards/time-tracker.json
"""
import json
import shutil
import sys

path = sys.argv[1]
shutil.copy(path, path + ".bak")
with open(path) as f:
    d = json.load(f)
panels = {p["id"]: p for p in d["panels"]}
src = {"type": "frser-sqlite-datasource", "uid": "stt-sqlite"}

# 1) Hours by activity: gradient bars in each activity's own colour (its overrides stay).
panels[10]["options"].update(displayMode="gradient", valueMode="color")

# 2) Hours by tag: one colour scheme (blue to purple), so a longer bar only means more hours.
q = ("SELECT t.tag AS tag, SUM(r.duration_min) / 60.0 AS hours FROM record_tags t JOIN records r ON r.id = t.record_id "
     "WHERE r.started_at >= $__from / 1000 AND r.started_at < $__to / 1000 AND r.activity IN (${activity:singlequote}) "
     "GROUP BY t.tag ORDER BY hours DESC LIMIT 12")
tag = {
    "id": 20, "type": "bargauge", "title": "Hours by tag", "datasource": src,
    "description": "The 12 tags with the most hours in the range. A session with two tags counts for both.",
    "targets": [{"refId": "A", "datasource": src, "rawQueryText": q, "queryText": q,
                 "queryType": "table", "timeColumns": ["time"]}],
    "fieldConfig": {"defaults": {"unit": "none", "decimals": 1, "min": 0,
                                 "color": {"mode": "continuous-BlPu"}}, "overrides": []},
    "options": {"reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
                "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                "valueMode": "color", "namePlacement": "top", "minVizHeight": 16, "maxVizHeight": 24},
    "transformations": [{"id": "rowsToFields", "options": {"mappings": [
        {"fieldName": "tag", "handlerKey": "field.name"},
        {"fieldName": "hours", "handlerKey": "field.value"}]}}],
}
d["panels"] = [p for p in d["panels"] if p["id"] != 20] + [tag]
panels[20] = tag

# 3) Layout (x, y, w, h): the tag bars beside the category donut, tables under them.
layout = {20: (0, 17, 12, 9), 13: (12, 17, 12, 9),
          11: (0, 26, 12, 10), 12: (12, 26, 12, 10),
          14: (0, 36, 24, 9),
          15: (0, 45, 24, 7),
          16: (0, 52, 16, 10), 17: (16, 52, 8, 10),
          18: (0, 62, 24, 5),
          19: (0, 67, 24, 9)}
for pid, (x, y, w, h) in layout.items():
    panels[pid]["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
d["panels"].sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
d["version"] = d.get("version", 1) + 1

with open(path, "w") as f:
    json.dump(d, f, indent=2)
print(f"updated {path} (version {d['version']}); the old file is {path}.bak")
