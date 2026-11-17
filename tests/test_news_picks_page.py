"""The page's picks (site/world.js), run in Node with a tiny fake DOM: headlines and links from
news.json's list only, the model's why lines as plain text (never HTML), stale or mismatched picks
ignored, the top of the list when the model gave none. Skipped when Node isn't installed."""
import json, shutil, subprocess, sys
from pathlib import Path

site = Path(__file__).parent.parent / "site"
node = shutil.which("node") or shutil.which("nodejs")
if not node:
    print("SKIPPED: Node is not installed (the page test needs it)")
    sys.exit(0)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [appPath, worldPath, casesJson] = process.argv.slice(1);   // node -e: the arguments start at 1
class FakeNode {
  constructor(tag) { this.tagName = tag; this.children = []; this.textContent = ""; this.className = ""; this.attrs = {}; }
  append(...xs) { for (const x of xs) this.children.push(typeof x === "string" ? { text: x } : x); }
  replaceChildren(...xs) { this.children = []; this.append(...xs); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  set innerHTML(v) { throw new Error("innerHTML used: " + v); }
  set outerHTML(v) { throw new Error("outerHTML used"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML used"); }
  get classList() { const n = this; return { add(c) { n.className += " " + c; }, remove() {}, contains() { return false; } }; }
}
const ids = {};
const document = { createElement: (t) => new FakeNode(t), createElementNS: (_, t) => new FakeNode(t),
                   getElementById: (id) => (ids[id] = ids[id] || new FakeNode("div")) };
// the page's own el() from app.js, and small stand-ins for the other helpers world.js uses
const app = fs.readFileSync(appPath, "utf8");
const elSrc = app.match(/function el\(tag, cls, text\) \{[\s\S]*?\n\}/)[0];
const ctx = { document, console, window: { scrollTo() {} }, Set, Map, Number, Array, JSON, Math, Date, String, Object };
vm.createContext(ctx);
vm.runInContext(elSrc + `
function ago() { return "2h ago"; }
function store() { return null; }
function svgEl(t) { return document.createElement(t); }
const state = {};`, ctx);
vm.runInContext(fs.readFileSync(worldPath, "utf8") + "\n;this.world = world;", ctx);

// everything a node shows: [kind, text, href], depth first
function flat(n, out = []) {
  if (!n) return out;
  if (n.text !== undefined) { out.push(["text", n.text, null]); return out; }
  out.push([n.tagName + (n.className ? "." + n.className.trim() : ""), n.textContent, n.href || null]);
  for (const c of n.children) flat(c, out);
  return out;
}
const results = {};
for (const c of JSON.parse(casesJson)) {
  ctx.world.news = c.news; ctx.world.picks = c.picks; ctx.world.section = "top"; ctx.world.topic = c.topic || "all";
  const r = { picks: vm.runInContext("newsPicks()", ctx) };
  r.brief = flat(vm.runInContext("briefNewsCard()", ctx));
  vm.runInContext("drawNews(true)", ctx);
  r.news = flat(ids["news-body"]);
  results[c.name] = r;
}
process.stdout.write(JSON.stringify(results));
"""

HOSTILE = '<img src=x onerror="alert(1)"> Visit now'


def story(n, title=None):
    return {"title": title or f"Headline {n}", "url": f"https://news{n}.example/a", "outlets": ["BBC", "DW"][: 1 + n % 2],
            "section": "Europe", "topics": ["Politics"], "at": "2026-11-16T07:00:00+00:00", "summary": "",
            "items": [{"outlet": "BBC", "title": title or f"Headline {n}", "url": f"https://news{n}.example/a",
                       "at": "2026-11-16T07:00:00+00:00"}]}


LIST = [story(n) for n in range(1, 13)]
NEWS = {"generated": "2026-11-16T07:36:10+00:00", "fetched": "2026-11-16T07:35:43+00:00", "sections": ["Europe"],
        "topics": ["Politics"], "outlets": ["BBC", "DW"], "stories": LIST, "candidates": LIST, "hn": [], "failed_feeds": []}


def picks(ranks, source="model", why=lambda n: f"Why {n} matters.", **extra):
    return {"generated": "2026-11-16T07:37:00+00:00", "list_generated": NEWS["generated"], "source": source,
            "reason": None if source == "model" else "no answer within 180 s (Ollama busy, e.g. with email triage, or slow)",
            "picks": [{"rank": n, "title": LIST[n - 1]["title"], "url": LIST[n - 1]["url"], "outlets": LIST[n - 1]["outlets"],
                       "why": why(n) if source == "model" else None} for n in ranks], **extra}


hostile = picks([1, 4, 5, 7, 8])
hostile["picks"][1]["why"] = HOSTILE
hostile["picks"][2]["why"] = "w" * 300
hostile["picks"][3]["title"] = "EVIL HEADLINE"                    # a picks file can't change a headline
cases = [
    {"name": "model", "news": NEWS, "picks": picks([1, 4, 5, 7, 8, 12])},
    {"name": "hostile", "news": NEWS, "picks": hostile},
    {"name": "fallback", "news": NEWS, "picks": picks(range(1, 8), source="fallback")},
    {"name": "none", "news": NEWS, "picks": None},
    {"name": "stale", "news": NEWS, "picks": {**picks([1, 2, 3, 4, 5]), "list_generated": "2026-11-15T18:01:00+00:00"}},
    {"name": "wrong link", "news": NEWS,
     "picks": {**picks([1, 2, 3, 4, 5]), "picks": picks([1, 2, 3, 4, 5])["picks"][:4] +
               [{**picks([5])["picks"][0], "url": "https://evil.example/"}]}},
    {"name": "out of range", "news": NEWS,
     "picks": {**picks([1, 2, 3, 4, 5]), "picks": picks([1, 2, 3, 4, 5])["picks"][:4] +
               [{"rank": 13, "title": "Headline 13", "url": "https://news13.example/a", "why": "x"}]}},
    {"name": "too many", "news": NEWS, "picks": picks(range(1, 13))},
    {"name": "other topic", "news": NEWS, "picks": picks([1, 4, 5, 7, 8]), "topic": "Politics"},
]
run = subprocess.run([node, "-e", HARNESS, str(site / "app.js"), str(site / "world.js"), json.dumps(cases)],
                     capture_output=True, text=True)
assert run.returncode == 0, run.stderr
res = json.loads(run.stdout)


def texts(nodes, kind=None):
    return [t for k, t, _ in nodes if kind is None or k == kind]


def links(nodes):
    return [(t, h) for k, t, h in nodes if h]


# 1) the model's picks: in the News tab's Top (a card above the list) and the brief's card, each with
#    the list's headline and link and the model's line, as text
r = res["model"]
assert [p["rank"] for p in r["picks"]["picks"]] == [1, 4, 5, 7, 8, 12] and r["picks"]["model"] is True
for where in ("brief", "news"):
    assert texts(r[where], "p.pick-why")[:6] == [f"Why {n} matters." for n in (1, 4, 5, 7, 8, 12)], r[where]
assert links(r["brief"])[:6] == [(f"Headline {n}", f"https://news{n}.example/a") for n in (1, 4, 5, 7, 8, 12)]
assert "Picked for you" in texts(r["news"]) and "Picked by the local model; its notes can be wrong" in texts(r["brief"])
assert "#4 · 1 outlets · BBC" not in texts(r["news"]) and "#4 · BBC · 2h ago" in texts(r["news"])
print("PICKS SHOWN OK")

# 2) hostile lines stay text; too long is cut; a changed headline in the file is not used
r = res["hostile"]
whys = texts(r["brief"], "p.pick-why")
assert whys[1] == HOSTILE and whys[2] == "w" * 140, whys          # as text: the fake DOM throws on innerHTML
assert all(not k.startswith("img") for k, _, _ in r["brief"] + r["news"])
assert "EVIL HEADLINE" not in texts(r["brief"]) + texts(r["news"]) and ("Headline 7", "https://news7.example/a") in links(r["brief"])
print("PLAIN TEXT OK")

# 3) the model gave none: the brief shows the top of the list (no lines), the News tab says why
r = res["fallback"]
assert r["picks"]["model"] is False and not texts(r["brief"], "p.pick-why")
assert links(r["brief"])[:7] == [(f"Headline {n}", f"https://news{n}.example/a") for n in range(1, 8)]
assert "The top of today's list" in texts(r["brief"]) and "Picked for you" not in texts(r["news"])
assert "No picks from the local model this time (no answer within 180 s (Ollama busy, e.g. with email triage, " \
       "or slow))." in texts(r["news"])
print("FALLBACK OK")

# 4) no picks file, picks for an older list, or one that doesn't match the list: ignored, and the
#    brief's card is what it was before picks (the first three of the list); too many: the first 7
for name in ("none", "stale", "wrong link", "out of range"):
    r = res[name]
    assert r["picks"] is None, name
    assert links(r["brief"]) == [(f"Headline {n}", f"https://news{n}.example/a") for n in (1, 2, 3)] + \
           [("Open News", "#news")], (name, links(r["brief"]))
    assert "Picked for you" not in texts(r["news"]) and not texts(r["news"], "p.pick-why"), name
assert [p["rank"] for p in res["too many"]["picks"]["picks"]] == list(range(1, 8))
# a topic chosen in Top: the list narrowed to it, no picks card
assert "Picked for you" not in texts(res["other topic"]["news"])
print("IGNORED OK")
