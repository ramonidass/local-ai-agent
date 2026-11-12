// The agent page: reads data.json (rebuilt by the agent after every run) and draws it.
// Every string from email or the model goes in with textContent, never as HTML. The one
// exception is the brief, which the server already turned into escaped HTML.
"use strict";

const REFRESH_MS = 2 * 60 * 1000;
const SEEN_KEY = "agent.lastSeen";
const PUSHES_SEEN_KEY = "agent.pushesSeen"; // when the bell was last opened
const VIEWS = ["brief", "emails", "stats", "markets", "news", "pushes"]; // tab order; Pushes is the bell in the header

const FILTERS = [
  { id: "all", label: "All", test: () => true },
  { id: "important", label: "Important", test: (e) => e.category === "urgent" || e.category === "work" },
  { id: "personal", label: "Personal", test: (e) => e.category === "personal" },
  { id: "pushed", label: "Pushed", test: (e) => e.pushed },
  { id: "purchase", label: "Purchases", test: (e) => e.category === "purchase" },
  { id: "jobs", label: "Jobs", test: (e) => e.category === "jobs" },
  { id: "unsure", label: "Unsure", test: (e) => e.category === "unsure" },
  { id: "quiet", label: "Filed quietly", test: (e) => e.quiet },
  { id: "other", label: "Info & ads", test: (e) => e.category === "info" || e.category === "ads" },
];

// Email groups, most important first. Info and ads start folded.
const CATEGORIES = [
  ["urgent", "Urgent"], ["work", "Work"], ["personal", "Personal"], ["purchase", "Purchases"],
  ["jobs", "Jobs"], ["unsure", "Unsure"], ["info", "Info"], ["ads", "Ads"],
];
const GROUP_KEY = "agent.emailGroup";
const FOLDED_KEY = "agent.foldedGroups";
const TASK_VIEW_KEY = "agent.taskView";
const TASKS_OPEN_KEY = "agent.tasksOpen";
const TASKS_SHOWN = 3; // "Anytime" tasks shown before "Show all"

// Grafana on the NAS (time tracking in detail), shown in the Stats tab. The Caddyfile's CSP must
// allow this origin in frame-src. Empty: no Grafana view.
const GRAFANA_URL = "http://192.168.20.10:3001/d/default-home-dashboard/home";
// The dashboard's own controls still change these; this is only how it opens.
const GRAFANA_VIEW = "from=now-1y&to=now&timezone=browser&var-activity=%24__all&var-bucket=month";
const STATS_KEY = "agent.statsFilter";
const STATS_MODE_KEY = "agent.statsMode";
// [id, label, weeks]; with "Month" the same span is counted in whole months.
const RANGES = [["1m", "1 month", 4], ["3m", "3 months", 13], ["6m", "6 months", 26], ["1y", "1 year", 52], ["all", "All", Infinity]];
const SERIES = 7; // activities with a colour of their own (--series-1…7); the rest are "Other"

const state = { data: null, filter: "all", query: "", open: new Set(), seenBefore: null,
  group: "category", folded: new Set(["info", "ads"]), taskView: "priority", tasksOpen: false,
  stats: null, statsFilter: { range: "3m", group: "week", activity: "all" }, statsMode: "overview", statsDrawn: "" };

// ---------- small helpers ----------

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) {
    return null; // storage can be blocked; the page still works, only "new" marks are lost
  }
  return null;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  svg.setAttribute("aria-hidden", "true");
  return svg;
}

function when(iso) {
  return iso ? new Date(iso) : null;
}

function ago(iso) {
  const d = when(iso);
  if (!d) return "never";
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function dayLabel(d) {
  const today = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

function clock(d) {
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function isNew(iso) {
  return Boolean(state.seenBefore && iso && iso > state.seenBefore);
}

// Calendar days as "2026-11-09" strings; the arithmetic runs at noon UTC, so DST never moves a day.
function parseDay(iso) {
  return new Date(`${iso}T12:00:00Z`);
}

function addDays(iso, n) {
  const d = parseDay(iso);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function addMonths(iso, n) {
  const d = parseDay(iso);
  d.setUTCMonth(d.getUTCMonth() + n);
  return d.toISOString().slice(0, 10);
}

function mondayOf(iso) {
  return addDays(iso, -((parseDay(iso).getUTCDay() + 6) % 7));
}

function todayIso() {
  return new Date().toLocaleDateString("en-CA");
}

function shortDate(iso, opts = { day: "numeric", month: "short" }) {
  return parseDay(iso).toLocaleDateString("en-GB", { ...opts, timeZone: "UTC" });
}

// 2h 30m, 2h, 15m (as the brief writes them).
function hm(mins) {
  const m = Math.round(mins);
  const h = Math.floor(m / 60);
  const r = m % 60;
  return h && r ? `${h}h ${String(r).padStart(2, "0")}m` : h ? `${h}h` : `${r}m`;
}

// Pushes are stored as sent; ntfy Markdown is shown here as plain text.
function plain(md) {
  return (md || "")
    .split("\n")
    .map((l) => l.replace(/^#{1,6}\s+/, "").replace(/^(\s*)[-*]\s+/, "$1• ").replace(/\*\*(.+?)\*\*/g, "$1").replace(/`(.+?)`/g, "$1"))
    .filter((l) => l.trim())
    .join("\n");
}

// ---------- views ----------

function currentView() {
  const v = location.hash.slice(1);
  return VIEWS.includes(v) ? v : "brief";
}

function showView() {
  const view = currentView();
  document.body.dataset.view = view; // the Stats tab uses a wider page
  for (const s of document.querySelectorAll(".view")) s.hidden = s.dataset.view !== view;
  for (const t of document.querySelectorAll(".tab")) {
    const on = t.dataset.view === view;
    t.classList.toggle("active", on);
    if (on) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  }
  const bell = document.getElementById("pushes-btn");
  bell.classList.toggle("active", view === "pushes");
  if (view === "pushes") bell.setAttribute("aria-current", "page");
  else bell.removeAttribute("aria-current");
  drawPushDot(state.data ? state.data.pushes || [] : []);
  window.scrollTo(0, 0);
  if (view === "stats") showStatsMode(); // charts are measured, so they are drawn once visible
}

// ---------- sections ----------

function drawHealth(h) {
  const pill = document.getElementById("health");
  pill.title = "";
  if (!h || !h.last_run) {
    pill.className = "pill pill-muted";
    pill.textContent = "Not run yet";
  } else if (h.fails > 0) {
    pill.className = "pill pill-bad";
    pill.textContent = `Triage failing (${h.fails})`;
    pill.title = h.last_error || "";
  } else {
    const stale = Date.now() - when(h.last_ok).getTime() > 2 * 3600 * 1000;
    pill.className = stale ? "pill pill-bad" : "pill pill-ok";
    pill.textContent = `${stale ? "Last OK" : "OK"} · ${ago(h.last_ok)}`;
    pill.title = "Email triage";
  }
}

// Split one brief (escaped HTML from the server) into a card per "##" section.
function briefCards(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html; // escaped server-side (agentdb.md_to_html)
  decorateMail(tpl.content);
  const out = { title: "", headline: "", cards: [], note: null };
  let card = null;
  for (const node of [...tpl.content.children]) {
    if (node.tagName === "H2" && !out.title) {
      out.title = node.textContent;
    } else if (node.tagName === "H3") {
      card = el("article", "card brief");
      card.append(node);
      out.cards.push(card);
    } else if (node.tagName === "P" && /^Built /.test(node.textContent)) {
      out.note = el("p", "foot-note", node.textContent);
    } else {
      if (!card) {
        card = el("article", "card brief");
        out.cards.push(card);
      }
      card.append(node);
    }
  }
  // The first Summary bullet is the one-line headline: show it big, above the cards.
  const summary = out.cards.find((c) => /^summary$/i.test((c.querySelector("h3") || {}).textContent || ""));
  if (summary) {
    summary.classList.add("wide");
    const first = summary.querySelector("li");
    if (first) {
      out.headline = first.textContent;
      first.remove();
    }
    if (!summary.querySelector("li, p")) out.cards = out.cards.filter((c) => c !== summary);
  }
  // Long sections (Email, Markets, the month preview) always take the full width.
  for (const c of out.cards) {
    const name = (c.querySelector("h3") || {}).textContent || "";
    if (/^(email|markets|.* at a glance)$/i.test(name)) c.classList.add("wide");
    if (/^markets$/i.test(name)) marketsBriefCard(c); // world.js
  }
  // Wins and Slipping side by side; focus, goals and the Monday review across the page.
  for (const c of out.cards) {
    const name = ((c.querySelector("h3") || {}).textContent || "").toLowerCase();
    if (name === "wins") c.classList.add("insight-win");
    else if (name === "slipping") c.classList.add("insight-gap");
    else if (name === "focus today") c.classList.add("wide", "insight-focus");
    else if (name === "last week in review" || name === "calendar") c.classList.add("wide");
    else if (name === "weekly goals") goalCard(c);
    else if (name === "time tracked") sparkCard(c);
    if (/^(wins|slipping|last week in review|time tracked)$/.test(name)) markTrends(c);
  }
  const tasks = out.cards.find((c) => /^tasks$/i.test((c.querySelector("h3") || {}).textContent || ""));
  if (tasks) taskCard(tasks);
  // Two columns on a wide screen: a section left alone on its row takes the full width.
  const half = out.cards.filter((c) => !c.classList.contains("wide"));
  if (half.length % 2 === 1) half[half.length - 1].classList.add("wide-odd");
  return out;
}

const GROUP_LABELS = Object.fromEntries(CATEGORIES.map(([id, label]) => [label, id]));

// Email groups in the brief: "<strong>Purchases</strong> (3)" with the emails nested under it.
function decorateMail(root) {
  for (const li of root.querySelectorAll("li")) {
    const head = li.firstElementChild;
    if (head && head.tagName === "STRONG" && li.firstChild === head && li.querySelector(":scope > ul")
        && GROUP_LABELS[head.textContent]) {
      li.classList.add("mail-group", `cat-${GROUP_LABELS[head.textContent]}`);
    }
  }
}

// ---------- wins, goals, time ----------

// "▲ +200%" green, "▼ -42%" amber.
function markTrends(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) if (/[▲▼]/.test(walker.currentNode.data)) texts.push(walker.currentNode);
  for (const t of texts) {
    const frag = document.createDocumentFragment();
    for (const part of t.data.split(/([▲▼](?: [+-]\d+%)?)/)) {
      if (!part) continue;
      frag.append(/^[▲▼]/.test(part) ? el("span", part[0] === "▲" ? "trend-up" : "trend-down", part) : part);
    }
    t.replaceWith(frag);
  }
}

// "Kafka course 3h · last week 1h ▲ · ▁▃▅█": the bars become a small chart of the last 8 weeks.
const SPARK_RE = /\s·\s([▁▂▃▄▅▆▇█]{2,})\s*$/;

function sparkCard(card) {
  for (const li of card.querySelectorAll(":scope > ul > li")) {
    const t = [...li.childNodes].find((n) => n.nodeType === Node.TEXT_NODE && SPARK_RE.test(n.data));
    if (!t) continue;
    const m = t.data.match(SPARK_RE);
    t.data = t.data.slice(0, m.index);
    // drawn as bars: fonts give "▁…█" very different heights
    const spark = el("span", "spark");
    spark.title = "Last 8 weeks";
    spark.setAttribute("role", "img");
    spark.setAttribute("aria-label", "Last 8 weeks");
    for (const ch of m[1]) {
      const bar = el("i");
      bar.style.height = `${(("▁▂▃▄▅▆▇█".indexOf(ch) + 1) / 8) * 100}%`;
      spark.append(bar);
    }
    t.after(spark);
  }
}

// "**Exercise** 30m / 3h · behind · 2h 30m to go, 14m a day" -> name, status, and a bar with a
// mark where you'd be if on pace ("5 of 7 days done" in the card's note line).
const GOAL_RE = /^(\d+h(?: \d+m)?|\d+m) \/ (\d+h(?: \d+m)?|\d+m) · (done|on pace|behind|missed)(?: · (.*))?$/;

function minutes(text) {
  const m = text.match(/^(?:(\d+)h)? ?(?:(\d+)m)?$/);
  return m ? Number(m[1] || 0) * 60 + Number(m[2] || 0) : 0;
}

function goalCard(card) {
  card.classList.add("wide");
  const note = card.querySelector("p");
  const days = note && note.textContent.match(/^(\d) of 7 days/);
  const pace = days ? Number(days[1]) / 7 : 1;
  const ul = card.querySelector("ul");
  if (!ul) return;
  ul.classList.add("goal-list");
  for (const li of ul.children) {
    const name = li.querySelector("strong");
    const rest = name && name.nextSibling && name.nextSibling.nodeType === Node.TEXT_NODE ? name.nextSibling.data.trim() : "";
    const m = rest.match(GOAL_RE);
    if (!m) continue;
    const done = minutes(m[1]);
    const target = minutes(m[2]) || 1;
    const status = m[3].replace(" ", "-");
    const head = el("div", "goal-head");
    head.append(el("span", "goal-name", name.textContent), el("span", "goal-num", `${m[1]} / ${m[2]}`),
      el("span", "goal-status", m[3]));
    const bar = el("div", "goal-bar");
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", `${name.textContent}: ${m[1]} of ${m[2]}, ${m[3]}`);
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", String(target));
    bar.setAttribute("aria-valuenow", String(Math.min(done, target)));
    const fill = el("span", "goal-fill");
    fill.style.width = `${Math.min(100, (done / target) * 100)}%`;
    bar.append(fill);
    if (pace < 1) {
      const mark = el("span", "goal-pace");
      mark.style.left = `${pace * 100}%`;
      mark.title = "On pace by now";
      bar.append(mark);
    }
    li.replaceChildren(head, bar);
    if (m[4]) li.append(el("div", "goal-hint", m[4]));
    const weeks = weekBars(name.textContent, target);
    if (weeks) li.append(weeks);
    li.className = `goal goal-${status}`;
  }
}

// Under a goal: the last 8 complete weeks (from stats.json) as small bars, the goal as a line
// across them. Weeks that reached the goal are drawn solid.
function weekBars(name, target) {
  const s = state.stats;
  if (!s || !s.time) return null;
  const monday = mondayOf(todayIso());
  const weeks = Array.from({ length: 8 }, (_, i) => addDays(monday, -7 * (8 - i)));
  const mins = new Map(weeks.map((w) => [w, 0]));
  for (const [day, activity, m] of s.time) {
    const w = mondayOf(day);
    if (activity === name && mins.has(w)) mins.set(w, mins.get(w) + m);
  }
  const values = weeks.map((w) => mins.get(w));
  if (!values.some(Boolean)) return null;
  const top = Math.max(target * 1.25, ...values);
  const hit = values.filter((v) => v >= target).length;
  const bars = el("div", "goal-weeks-bars");
  bars.setAttribute("role", "img");
  bars.setAttribute("aria-label", `${name}: goal reached in ${hit} of the last 8 weeks`);
  weeks.forEach((w, i) => {
    const b = el("i", values[i] >= target ? "hit" : "");
    b.style.height = `${(values[i] / top) * 100}%`;
    b.title = `Week of ${shortDate(w)}: ${hm(values[i])}`;
    bars.append(b);
  });
  const line = el("span", "goal-weeks-line");
  line.style.bottom = `${(target / top) * 100}%`;
  bars.append(line);
  const wrap = el("div", "goal-weeks");
  wrap.append(bars, el("span", "goal-weeks-label", `goal reached ${hit} of the last 8 weeks`));
  return wrap;
}

// ---------- tasks ----------

// Tasks arrive as "⏫ Text · <em>Computer science › Server</em>" (dated ones as "Mon 9: ⏫ Text ...").
const PRIORITIES = { "🔺": ["Highest", 0], "⏫": ["High", 1], "🔼": ["Medium", 2], "🔽": ["Low", 4], "⏬": ["Lowest", 5] };
const PRIO_RE = /^((?:[^:]{1,24}: )?)(🔺|⏫|🔼|🔽|⏬)\uFE0F? /u;
const URL_RE = /https?:\/\/[^\s<>"]+[^\s<>".,;:!?)\]]/g;

// The task text is your own (from Obsidian), so its links can be real links.
function linkify(node) {
  for (const text of [...node.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE)) {
    const parts = text.data.split(URL_RE);
    const urls = text.data.match(URL_RE);
    if (!urls) continue;
    const frag = document.createDocumentFragment();
    parts.forEach((part, i) => {
      if (part) frag.append(part);
      if (i >= urls.length) return;
      let url;
      try { url = new URL(urls[i]); } catch (_) { frag.append(urls[i]); return; }
      const a = el("a", "task-link", url.hostname.replace(/^www\./, ""));
      a.href = url.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.title = url.href;
      frag.append(a);
    });
    text.replaceWith(frag);
  }
}

function taskItem(li, index) {
  let level = 3;
  const first = li.firstChild;
  if (first && first.nodeType === Node.TEXT_NODE) {
    const m = first.data.match(PRIO_RE);
    if (m) {
      level = PRIORITIES[m[2]][1];
      first.data = m[1] + first.data.slice(m[0].length);
    }
  }
  let section = "";
  const last = li.lastChild;
  const before = last && last.previousSibling;
  if (last && last.nodeName === "EM" && before && before.nodeType === Node.TEXT_NODE && before.data.endsWith(" · ")) {
    before.data = before.data.slice(0, -3);
    section = last.textContent;
    last.remove();
  }
  const text = el("span", "task-text");
  text.append(...li.childNodes);
  linkify(text);
  const body = el("span", "task-body");
  body.append(text);
  if (section) body.append(el("span", "section-tag", section));
  const mark = el("span", `prio prio-${level}`);
  const name = Object.values(PRIORITIES).find(([, l]) => l === level);
  if (name) {
    mark.title = `${name[0]} priority`;
    mark.setAttribute("role", "img");
    mark.setAttribute("aria-label", mark.title);
  }
  li.replaceChildren(mark, body);
  li.classList.add("task");
  return { li, level, section, index };
}

// Android browsers (and installed web apps) often ignore obsidian:// links but always pass an
// intent:// link to the app it names; md.obsidian is Obsidian's Android package.
function obsidianHref(uri) {
  if (!/Android/i.test(navigator.userAgent)) return uri;
  return `intent://${uri.slice("obsidian://".length)}#Intent;scheme=obsidian;package=md.obsidian;end`;
}

// The Tasks card: each "###" list gets the new look; "Anytime" can be shown by priority or by
// Obsidian list, and folds after a few tasks.
function taskCard(card) {
  card.classList.add("tasks-card");
  // "Open in Obsidian: obsidian://…" (TASKS_LINK): tapping a task opens the to-do file.
  const OPEN_RE = /^Open in Obsidian: (obsidian:\/\/\S+)$/;
  const linkLine = [...card.querySelectorAll("p")].find((p) => OPEN_RE.test(p.textContent));
  const open = linkLine ? obsidianHref(linkLine.textContent.match(OPEN_RE)[1]) : "";
  if (linkLine) linkLine.remove();
  let anytime = null;
  for (const h of card.querySelectorAll("h4")) {
    const ul = h.nextElementSibling;
    if (!ul || ul.tagName !== "UL") continue;
    const items = [...ul.children].filter((n) => n.tagName === "LI").map(taskItem);
    if (open) {
      for (const it of items) {
        it.li.classList.add("tappable");
        it.li.tabIndex = 0;
        it.li.setAttribute("role", "link");
        it.li.title = "Open in Obsidian";
        it.li.addEventListener("click", (ev) => {
          if (!ev.target.closest("a")) location.href = open; // links inside a task keep working
        });
        it.li.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" && ev.target === it.li) location.href = open;
        });
      }
    }
    ul.classList.add("task-list");
    if (/^(anytime|no date)\b/i.test(h.textContent)) { // "No date": briefs written before the rename
      ul.classList.add("anytime");
      h.textContent = h.textContent.replace(/^no date/i, "Anytime").replace(/, showing \d+/, "");
      anytime = { ul, items };
    }
  }
  if (!anytime) return;
  if (anytime.items.some((it) => it.section)) {
    const toggle = el("div", "segmented small");
    toggle.setAttribute("role", "group");
    toggle.setAttribute("aria-label", "Order tasks");
    for (const [id, label] of [["priority", "Priority"], ["list", "List"]]) {
      const b = el("button", null, label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(state.taskView === id));
      b.addEventListener("click", () => {
        state.taskView = id;
        store(TASK_VIEW_KEY, id);
        drawBrief(state.data ? state.data.briefs || [] : []);
      });
      toggle.append(b);
    }
    card.querySelector("h3").append(toggle);
  }
  drawAnytime(anytime.ul, anytime.items);
}

function drawAnytime(ul, items) {
  const byList = state.taskView === "list";
  ul.closest(".tasks-card").classList.toggle("by-list", byList);
  ul.replaceChildren();
  const rows = []; // [group header or null, task]
  if (byList) {
    const groups = new Map();
    for (const it of items) {
      const key = it.section || "Other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(it);
    }
    // The list with the most urgent task first; tasks inside keep their priority order.
    const best = (list) => Math.min(...list.map((it) => it.level));
    const order = [...groups].sort((a, b) => best(a[1]) - best(b[1]) || a[1][0].index - b[1][0].index);
    for (const [name, list] of order) {
      const head = el("li", "task-group");
      const parts = name.split(" › ");
      if (parts.length > 1) head.append(el("span", "task-group-parent", parts.slice(0, -1).join(" › ") + " › "));
      head.append(el("span", "task-group-name", parts[parts.length - 1]));
      head.append(el("span", "task-group-count", String(list.length)));
      list.forEach((it, i) => rows.push([i === 0 ? head : null, it]));
    }
  } else {
    for (const it of items) rows.push([null, it]);
  }
  const fold = !state.tasksOpen && items.length > TASKS_SHOWN;
  rows.forEach(([head, it], i) => {
    const hidden = fold && i >= TASKS_SHOWN;
    if (head && !hidden) ul.append(head);
    it.li.hidden = hidden;
    ul.append(it.li);
  });
  ul.parentNode.querySelectorAll(".task-more").forEach((b) => b.remove());
  if (items.length > TASKS_SHOWN) {
    const more = el("button", "task-more", fold ? `Show all ${items.length}` : "Show fewer");
    more.type = "button";
    more.setAttribute("aria-expanded", String(!fold));
    more.addEventListener("click", () => {
      state.tasksOpen = !state.tasksOpen;
      store(TASKS_OPEN_KEY, state.tasksOpen ? "1" : "0");
      drawAnytime(ul, items);
    });
    ul.after(more);
  }
}

// ---------- what the agent did ----------

function tile(label, value, sub, status) {
  const t = el("div", "tile");
  t.append(el("div", "tile-label", label), el("div", "tile-value", value));
  const s = el("div", "tile-sub");
  if (status) s.append(el("span", `dot dot-${status}`));
  s.append(sub);
  t.append(s);
  return t;
}

// "Agent this week": counts from data.json (last 7 days vs the 7 before), under the latest brief.
function valueCard(v) {
  const w = v && v.week;
  if (!w || (!w.emails && !w.runs)) return null;
  const b = v.before || {};
  const num = (n) => Number(n || 0).toLocaleString("en-GB");
  const pct = (a, n) => (n ? Math.round((a * 100) / n) : 0);
  const kept = w.emails - w.pushed;
  const failed = w.runs - w.runs_ok;

  const card = el("article", "card brief wide value-card");
  card.append(el("h3", null, "Agent this week"), el("p", "value-note", "Last 7 days, compared with the 7 before"));
  const tiles = el("div", "tiles");
  tiles.append(
    tile("Emails sorted", num(w.emails), `${num(b.emails)} the week before`),
    tile("Kept off your phone", num(kept), `${pct(kept, w.emails)}% of emails · ${num(w.pushed)} pushed`),
    tile("Sorted by your rules", `${pct(w.by_rule, w.emails)}%`,
      `${num(w.emails - w.by_rule)} by the AI · ${num(w.unsure)} unsure`),
    // rounded down to 0.1%, so a failed run never shows as 100%
    tile("Triage runs OK", `${w.runs ? Math.floor((w.runs_ok * 1000) / w.runs) / 10 : 0}%`,
      failed ? `${num(failed)} of ${num(w.runs)} failed` : `all ${num(w.runs)} runs`, failed ? "warn" : "ok"),
    tile("Briefs written", num(w.briefs), "in the last 7 days"),
  );
  card.append(tiles);
  return card;
}

// Sections of the brief shown on the Stats tab instead ("This week").
const STATS_CARDS = ["weekly goals", "time tracked", "last week in review"];

// The Email card in the brief is one line: the emails themselves are a tap away on their own tab.
function emailLine(card) {
  const parts = [];
  for (const h of card.querySelectorAll("h4")) {
    const m = h.textContent.match(/^(Pushed|Waiting for the digest).*\((\d+)\)$/);
    if (!m) return; // a wording this page doesn't know: leave the card as it is
    parts.push(m[1] === "Pushed" ? `${m[2]} pushed to your phone` : `${m[2]} waiting for the digest`);
  }
  if (!parts.length) return;
  const link = el("a", "line-link", parts.join(" · "));
  link.href = "#emails";
  card.replaceChildren(card.querySelector("h3"), link);
  card.classList.add("one-line");
}

function splitTitle(title) {
  const [name, ...rest] = title.split(", ");
  return { name: name || "Morning brief", date: rest.join(", ") };
}

function drawBrief(briefs) {
  const body = document.getElementById("brief-body");
  const day = document.getElementById("brief-day");
  const title = document.getElementById("brief-title");
  const headline = document.getElementById("brief-headline");
  const older = document.getElementById("brief-older");
  older.replaceChildren();
  document.getElementById("older-title").hidden = briefs.length < 2;
  if (!briefs.length) {
    body.replaceChildren(el("p", "card muted empty", "No brief yet. It is written at 07:55."));
    day.textContent = "";
    headline.textContent = "";
    return;
  }
  const [latest, ...rest] = briefs;
  const b = briefCards(latest.html);
  const t = splitTitle(b.title);
  const todayIso = new Date().toLocaleDateString("en-CA");
  day.textContent = (latest.day === todayIso ? "Today" : dayLabel(new Date(latest.day + "T12:00:00"))) +
    (t.date ? ` · ${t.date}` : "");
  title.textContent = t.name;
  headline.textContent = b.headline;
  // The brief is today: what to do, how the week is going, the few numbers worth a look. Looking
  // back (goals with their history, time tracked, last week, what the agent did) is on the Stats tab.
  const nameOf = (c) => ((c.querySelector("h3") || {}).textContent || "").toLowerCase();
  const back = b.cards.filter((c) => STATS_CARDS.includes(nameOf(c)));
  const today = b.cards.filter((c) => !back.includes(c));
  const check = today.find((c) => nameOf(c) === "check");
  if (check) { // warnings lead, as a thin banner
    check.classList.add("wide", "banner");
    today.splice(today.indexOf(check), 1);
    today.unshift(check);
  }
  const mail = today.find((c) => nameOf(c) === "email");
  if (mail) emailLine(mail);
  for (const c of today) c.classList.remove("wide-odd");
  const half = today.filter((c) => !c.classList.contains("wide"));
  if (half.length % 2 === 1) half[half.length - 1].classList.add("wide-odd");
  body.replaceChildren(...today);
  const stories = briefNewsCard(); // world.js: from news.json, never part of the brief's text
  if (stories) body.append(stories);
  if (b.note) body.append(b.note);

  if (!back.some((c) => nameOf(c) === "last week in review")) {
    const fold = mondayReview(latest.day, rest);
    if (fold) back.push(fold);
  }
  const value = valueCard(state.data && state.data.value);
  if (value) back.push(value);
  for (const c of back) c.classList.add("wide");
  document.getElementById("stats-week").replaceChildren(...back);
  document.getElementById("stats-week-title").hidden = !back.length;
  if (isNew(latest.built)) b.cards.forEach((c) => c.classList.add("is-new"));

  for (const ob of rest) {
    const det = el("details", "older");
    const parts = briefCards(ob.html);
    det.append(el("summary", null, dayLabel(new Date(ob.day + "T12:00:00")) +
      (parts.headline ? ` — ${parts.headline}` : "")));
    const grid = el("div", "brief-grid");
    grid.append(...parts.cards);
    det.append(grid);
    older.append(det);
  }
}

// Only Monday's brief has "Last week in review"; the rest of the week it is shown folded.
function mondayReview(day, older) {
  const monday = older.find((ob) => ob.day === mondayOf(day));
  if (!monday) return null;
  const card = briefCards(monday.html).cards.find((c) =>
    /^last week in review$/i.test((c.querySelector("h3") || {}).textContent || ""));
  if (!card) return null;
  const det = el("details", "review-fold");
  const summary = el("summary");
  summary.append(card.querySelector("h3"));
  det.append(summary, ...card.childNodes);
  const out = el("article", "card brief wide");
  out.append(det);
  return out;
}

function drawFilters(emails) {
  const box = document.getElementById("filters");
  box.replaceChildren();
  for (const f of FILTERS) {
    const n = emails.filter(f.test).length;
    if (f.id !== "all" && n === 0 && state.filter !== f.id) continue;
    const chip = el("button", "chip", f.label);
    chip.append(el("span", null, String(n)));
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(state.filter === f.id));
    chip.addEventListener("click", () => {
      state.filter = f.id;
      drawFilters(emails);
      drawEmails(emails);
    });
    box.append(chip);
  }
}

function matches(e, q) {
  if (!q) return true;
  return [e.from_name, e.from_addr, e.subject, e.summary, e.category]
    .some((s) => (s || "").toLowerCase().includes(q));
}

function toggleRow(row, id, onOpen) {
  const flip = () => {
    if (state.open.has(id)) state.open.delete(id);
    else state.open.add(id);
    row.classList.toggle("open");
    row.setAttribute("aria-expanded", String(row.classList.contains("open")));
    if (onOpen && row.classList.contains("open")) onOpen();
  };
  row.addEventListener("click", (ev) => {
    // Taps inside the email text select it; they don't close the row.
    if (ev.target.closest(".mail-body") || String(window.getSelection())) return;
    flip();
  });
  row.addEventListener("keydown", (ev) => {
    if (ev.target === row && (ev.key === "Enter" || ev.key === " ")) {
      ev.preventDefault();
      flip();
    }
  });
  row.setAttribute("aria-expanded", String(state.open.has(id)));
  if (state.open.has(id)) {
    row.classList.add("open");
    if (onOpen) onOpen();
  }
}

// The full text is a separate small file, fetched the first time you open the email.
const bodies = new Map();

async function loadBody(e, box) {
  if (box.dataset.loaded) return;
  box.dataset.loaded = "1";
  box.textContent = "Loading the email…";
  try {
    if (!bodies.has(e.id)) {
      const res = await fetch(`mail/${encodeURIComponent(e.id)}.txt`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      bodies.set(e.id, await res.text());
    }
    box.textContent = bodies.get(e.id); // plain text only: links and HTML stay inert
  } catch (_) {
    delete box.dataset.loaded;
    box.textContent = "Couldn't load the email text. Tap the email again to retry.";
  }
}

// Where the category came from: "rule: Security alerts" (yours) or "model" (the local AI).
function sourceBadge(source) {
  const rule = (source || "").startsWith("rule: ") ? source.slice(6) : "";
  const b = el("span", `badge plain source${rule ? "" : " by-model"}`, rule ? `rule · ${rule}` : "AI");
  b.title = rule ? `Sorted by your rule "${rule}"` : "Sorted by the local model";
  return b;
}

function mailRow(e, withDay) {
  const name = e.from_name || e.from_addr || "(unknown)";
  const row = el("div", `row cat-${e.category}`);
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.id = e.id;
  if (isNew(e.processed)) row.classList.add("is-new");

  row.append(el("span", "avatar", (name.match(/[\p{L}\p{N}]/u) || ["?"])[0].toUpperCase()));
  const main = el("div", "row-main");
  const top = el("div", "row-top");
  top.append(el("span", "row-from", name));
  const d = when(e.received || e.processed);
  top.append(el("span", "row-time", d ? (withDay ? `${dayLabel(d)} ${clock(d)}` : clock(d)) : ""));
  main.append(top);
  main.append(el("div", "row-subject", e.subject || "(no subject)"));
  if (e.summary) main.append(el("div", "row-preview", e.summary));

  const tags = el("div", "row-tags");
  if (withDay !== true || state.group !== "category") tags.append(el("span", "badge", e.category));
  if (e.pushed) tags.append(el("span", "badge plain", "pushed"));
  if (e.quiet) tags.append(el("span", "badge plain", "filed quietly"));
  tags.append(sourceBadge(e.source));
  main.append(tags);

  const meta = [e.from_addr, e.source === "model" ? "sorted by the model" : `sorted by ${e.source}`];
  const more = el("div", "row-more");
  more.append(el("div", null, meta.filter(Boolean).join(" · ")));
  const body = el("div", "mail-body");
  if (e.has_body) more.append(body);
  else more.append(el("div", "mail-none", "Full text isn't stored for this email (sorted before the page existed)."));
  main.append(more);
  row.append(main);
  toggleRow(row, e.id, e.has_body ? () => loadBody(e, body) : null);
  return row;
}

function drawEmails(emails) {
  const list = document.getElementById("email-list");
  const filter = FILTERS.find((f) => f.id === state.filter) || FILTERS[0];
  const q = state.query.trim().toLowerCase();
  const shown = emails.filter((e) => filter.test(e) && matches(e, q));
  const count = document.getElementById("email-count");
  count.textContent = shown.length === emails.length ? `${emails.length} · 30 days` : `${shown.length} of ${emails.length}`;
  count.title = "Emails sorted in the last 30 days";
  list.replaceChildren();
  if (!shown.length) {
    list.append(el("p", "card muted empty",
      emails.length ? "Nothing matches." : "No emails yet. They appear here as triage sorts new mail."));
    return;
  }
  if (state.group === "category") {
    drawByCategory(list, shown, q !== "" || state.filter !== "all");
    return;
  }
  let lastDay = "";
  for (const e of shown) {
    const d = when(e.received || e.processed);
    const label = d ? dayLabel(d) : "";
    if (label !== lastDay) {
      list.append(el("div", "day", label));
      lastDay = label;
    }
    list.append(mailRow(e, false));
  }
}

// One card per category (Urgent, Work, Personal, ...), newest first inside. Tap a header to fold it.
function drawByCategory(list, shown, searching) {
  const known = new Set(CATEGORIES.map(([id]) => id));
  const groups = [...CATEGORIES];
  for (const e of shown) {
    if (!known.has(e.category)) {
      known.add(e.category);
      groups.push([e.category, e.category]);
    }
  }
  for (const [id, label] of groups) {
    const items = shown.filter((e) => e.category === id);
    if (!items.length) continue;
    const box = el("section", `group-card cat-${id}`);
    const folded = state.folded.has(id) && !searching; // a search always shows what it found
    box.classList.toggle("folded", folded);
    const head = el("button", "group-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(!folded));
    head.append(el("span", "group-dot"));
    head.append(el("span", "group-name", label));
    const fresh = items.filter((e) => isNew(e.processed)).length;
    if (fresh) head.append(el("span", "group-new", `${fresh} new`));
    head.append(el("span", "group-count", String(items.length)));
    head.append(el("span", "group-chev", "›"));
    head.addEventListener("click", () => {
      if (state.folded.has(id)) state.folded.delete(id);
      else state.folded.add(id);
      store(FOLDED_KEY, JSON.stringify([...state.folded]));
      const now = state.folded.has(id);
      box.classList.toggle("folded", now);
      head.setAttribute("aria-expanded", String(!now));
    });
    box.append(head);
    const body = el("div", "group-body");
    for (const e of items) body.append(mailRow(e, true));
    box.append(body);
    list.append(box);
  }
}

// One icon button: it shows how the list is grouped now, and a tap switches.
function drawGroupToggle() {
  const b = document.getElementById("group-toggle");
  const byDate = state.group === "date";
  b.querySelector("use").setAttribute("href", byDate ? "#i-clock" : "#i-layers");
  b.title = byDate ? "Grouped by date: tap for by category" : "Grouped by category: tap for by date";
  b.setAttribute("aria-label", b.title);
}

// The search field takes the title's place while it's open, and stays open while it has text.
function openSearch(open) {
  const box = document.getElementById("search-box");
  const input = document.getElementById("search");
  box.hidden = !open;
  document.getElementById("emails-title").hidden = open;
  document.getElementById("search-open").hidden = open;
  if (open) {
    input.focus();
  } else if (input.value) {
    input.value = "";
    state.query = "";
    drawEmails(state.data ? state.data.emails || [] : []);
  }
}

// A dot on the bell for pushes sent since it was last opened. The morning brief's own push is
// left out: it arrives every day and the Brief tab already shows it. No dot on a first visit.
function drawPushDot(pushes) {
  if (currentView() === "pushes") store(PUSHES_SEEN_KEY, new Date().toISOString());
  const seen = when(store(PUSHES_SEEN_KEY));
  const fresh = seen && currentView() !== "pushes" ? pushes.filter((p) => p.topic !== "brief" && when(p.sent) > seen).length : 0;
  const bell = document.getElementById("pushes-btn");
  bell.querySelector(".dot").hidden = fresh === 0;
  bell.setAttribute("aria-label", fresh ? `Pushes sent, ${fresh} new` : "Pushes sent");
  bell.title = bell.getAttribute("aria-label");
}

function drawPushes(pushes) {
  drawPushDot(pushes);
  const list = document.getElementById("push-list");
  list.replaceChildren();
  if (!pushes.length) {
    list.append(el("p", "card muted empty", "No pushes yet."));
    return;
  }
  let lastDay = "";
  pushes.forEach((p, i) => {
    const d = when(p.sent);
    const label = d ? dayLabel(d) : "";
    if (label !== lastDay) {
      list.append(el("div", "day", label));
      lastDay = label;
    }
    const isBrief = p.topic === "brief";
    const row = el("button", `row ${isBrief ? "cat-brief" : `prio-${p.priority || "default"}`}`);
    row.type = "button";
    const av = el("span", "avatar");
    av.append(icon(isBrief ? "brief" : "mail"));
    row.append(av);
    const main = el("div", "row-main");
    const top = el("div", "row-top");
    top.append(el("span", "row-from", p.title || "(no title)"));
    top.append(el("span", "row-time", d ? clock(d) : ""));
    main.append(top);
    if (p.message) main.append(el("div", "row-preview push-msg", plain(p.message)));
    const tags = el("div", "row-tags");
    tags.append(el("span", "badge plain", p.topic || "push"));
    if (p.priority === "high") tags.append(el("span", "badge", "high priority"));
    main.append(tags);
    row.append(main);
    if (isBrief) {
      row.addEventListener("click", () => { location.hash = "#brief"; });
    } else {
      toggleRow(row, `push-${p.sent}-${i}`);
    }
    list.append(row);
  });
}

function drawNew(data) {
  const banner = document.getElementById("new-banner");
  const count = document.querySelector('.tab[data-view="emails"] .count');
  const newMail = state.seenBefore ? data.emails.filter((e) => isNew(e.processed)) : [];
  count.hidden = newMail.length === 0;
  count.textContent = newMail.length > 99 ? "99+" : String(newMail.length);
  if (!state.seenBefore) {
    banner.hidden = true;
    return;
  }
  const important = newMail.filter((e) => e.category === "urgent" || e.category === "work").length;
  const newBrief = data.briefs.length && isNew(data.briefs[0].built);
  const parts = [];
  if (newBrief) parts.push("a new brief");
  if (newMail.length) parts.push(`${newMail.length} new email${newMail.length === 1 ? "" : "s"}` +
    (important ? ` (${important} important)` : ""));
  banner.hidden = parts.length === 0;
  banner.textContent = parts.length ? `Since your last visit: ${parts.join(", ")}.` : "";
}

function draw() {
  const data = state.data;
  if (!data) return;
  const emails = data.emails || [];
  drawHealth(data.health);
  drawNew(data);
  drawBrief(data.briefs || []);
  drawFilters(emails);
  drawEmails(emails);
  drawPushes(data.pushes || []);
  drawStats();
  drawMarkets();
  drawNews();
  document.getElementById("generated").textContent = `Updated ${ago(data.generated)}`;
}

// ---------- stats ----------
// Everything here comes from stats.json: time per day and activity, tasks per day, each finished
// week against its goal. Charts are SVG built in code (no library); the CSP allows nothing else.

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

// A clean axis: the top tick and the step between ticks (1, 2 or 5 × a power of ten).
function niceScale(max, integer) {
  if (max <= 0) return { top: 1, step: 1 };
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  let step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  if (integer) step = Math.max(1, Math.round(step));
  return { top: Math.ceil(max / step) * step, step };
}

// A column with a 4px rounded top, square at the baseline.
function colPath(x, base, w, h, r) {
  r = Math.min(r, w / 2, h);
  const top = base - h;
  return `M${x},${base}V${top + r}Q${x},${top} ${x + r},${top}H${x + w - r}Q${x + w},${top} ${x + w},${top + r}V${base}Z`;
}

const TIME_UNIT = { per: 60, tick: (h) => `${h}h`, value: hm };
const COUNT_UNIT = { per: 1, integer: true, tick: String, value: String };

// Columns, stacked when there are several series (side by side with grouped): thin bars, a 2px
// gap between touching parts, hairline grid. Each column is one hover/focus target that lists
// every series. cols: [{short, long, partial}]; a partial column (the period so far) is pale.
// series: [{name, cls, values}] with one value per column.
function columnChart(card, cols, series, unit, label, grouped = false) {
  if (series.length > 1) card.append(legend(series));
  const box = el("div", "chart");
  card.append(box);
  const W = Math.max(box.clientWidth, 240);
  const H = 190, L = 42, R = 4, T = 8, B = 24;
  const pw = W - L - R;
  const ph = H - T - B;
  const totals = cols.map((_, i) => series.reduce((a, s) => a + s.values[i], 0));
  const tallest = grouped ? Math.max(0, ...series.flatMap((s) => s.values)) : Math.max(0, ...totals);
  const { top, step } = niceScale(tallest / unit.per, unit.integer);
  const root = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "chart-svg",
    role: "img", "aria-label": label });
  for (let t = 0; t <= top + 1e-9; t += step) {
    const y = Math.round(T + ph - (t / top) * ph) + 0.5;
    root.append(svgEl("line", { x1: L, x2: W - R, y1: y, y2: y, class: t === 0 ? "axis-line" : "grid-line" }));
    const text = svgEl("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "axis-text" });
    text.textContent = unit.tick(Math.round(t * 100) / 100);
    root.append(text);
  }
  const band = pw / cols.length;
  const bw = Math.max(2, Math.min(24, band * 0.62, band - 2));
  const every = Math.ceil(cols.length / Math.max(1, Math.floor(pw / 56)));
  const tip = el("div", "chart-tip");
  tip.hidden = true;
  const gw = Math.max(2, Math.min(16, (band * 0.7 - 2 * (series.length - 1)) / series.length));
  cols.forEach((col, i) => {
    const pale = col.partial ? " partial" : "";
    if (grouped) {
      const x0 = L + band * i + (band - series.length * gw - 2 * (series.length - 1)) / 2;
      series.forEach((s, j) => {
        const h = (s.values[i] / unit.per / top) * ph;
        if (h > 0) root.append(svgEl("path", { d: colPath(x0 + j * (gw + 2), T + ph, gw, h, 4), class: `bar ${s.cls}${pale}` }));
      });
    } else {
      const x = L + band * i + (band - bw) / 2;
      const parts = series.filter((s) => s.values[i] > 0);
      let base = T + ph;
      parts.forEach((s, j) => {
        const h = (s.values[i] / unit.per / top) * ph;
        const last = j === parts.length - 1;
        const drawn = last ? h : h - 2; // the gap above each lower part
        if (drawn > 0) root.append(svgEl("path", { d: colPath(x, base, bw, drawn, last ? 4 : 0), class: `bar ${s.cls}${pale}` }));
        base -= h;
      });
    }
    if ((cols.length - 1 - i) % every === 0) { // the newest column always has its label
      const text = svgEl("text", { x: L + band * i + band / 2, y: H - 6, "text-anchor": "middle", class: "axis-text" });
      text.textContent = col.short;
      root.append(text);
    }
    const hit = svgEl("rect", { x: L + band * i, y: T, width: band, height: ph, class: "hit", tabindex: "0",
      "aria-label": `${col.long}: ${series.map((s) => `${s.name} ${unit.value(s.values[i])}`).join(", ")}` });
    const show = () => {
      tip.replaceChildren(el("div", "tip-title", col.long));
      const rows = series.length > 1 && !grouped ? series.filter((s) => s.values[i] > 0) : series;
      for (const s of rows) {
        const row = el("div", `tip-row ${s.cls}`);
        row.append(el("i", "tip-key"), el("strong", null, unit.value(s.values[i])), el("span", null, s.name));
        tip.append(row);
      }
      if (rows.length > 1 && !grouped) {
        const row = el("div", "tip-row tip-total");
        row.append(el("strong", null, unit.value(totals[i])), el("span", null, "total"));
        tip.append(row);
      }
      tip.hidden = false;
      tip.style.left = `${Math.max(0, Math.min(W - tip.offsetWidth, L + band * (i + 0.5) - tip.offsetWidth / 2))}px`;
      hit.classList.add("on");
    };
    const hide = () => {
      tip.hidden = true;
      hit.classList.remove("on");
    };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("blur", hide);
    root.append(hit);
  });
  box.append(root, tip);
}

function legend(series) {
  const box = el("div", "legend");
  for (const s of series) {
    const item = el("span", `legend-item ${s.cls}`);
    item.append(el("i", "swatch"), s.name);
    box.append(item);
  }
  return box;
}

// The same numbers as a table (newest first): readable without colour or hovering.
function tableView(card, head, rows) {
  const det = el("details", "chart-table");
  det.append(el("summary", null, "Show as a table"));
  const scroll = el("div", "table-scroll");
  const table = el("table");
  const tr = el("tr");
  for (const h of head) tr.append(el("th", null, h));
  const thead = el("thead");
  thead.append(tr);
  const tbody = el("tbody");
  for (const r of [...rows].reverse()) {
    const row = el("tr");
    for (const c of r) row.append(el("td", null, c));
    tbody.append(row);
  }
  table.append(thead, tbody);
  scroll.append(table);
  det.append(scroll);
  card.append(det);
}

function statCard(body, title, note, wide) {
  const card = el("article", `card brief stat-card${wide ? " wide" : ""}`);
  card.append(el("h3", null, title));
  if (note) card.append(el("p", "value-note", note));
  body.append(card); // in the page before drawing: charts are measured
  return card;
}

// Complete weeks (Mon–Sun) or complete months in the chosen range, oldest first.
function statsPeriods(group, rangeId, firstDay) {
  const weeks = (RANGES.find(([id]) => id === rangeId) || RANGES[1])[2];
  const out = [];
  if (group === "month") {
    const months = Number.isFinite(weeks) ? Math.max(1, Math.round(weeks / 4.345)) : Infinity;
    const thisMonth = `${todayIso().slice(0, 7)}-01`;
    for (let start = addMonths(thisMonth, -1); out.length < months && start >= `${firstDay.slice(0, 7)}-01`;
      start = addMonths(start, -1)) {
      out.unshift({ start, end: addMonths(start, 1), short: shortDate(start, { month: "short" }),
        long: shortDate(start, { month: "long", year: "numeric" }) });
    }
  } else {
    for (let start = addDays(mondayOf(todayIso()), -7); out.length < weeks && start >= mondayOf(firstDay);
      start = addDays(start, -7)) {
      out.unshift({ start, end: addDays(start, 7), short: shortDate(start), long: `Week of ${shortDate(start)}` });
    }
  }
  return out;
}

// The week or month in progress, drawn pale after the complete ones.
function currentPeriod(group) {
  const start = group === "month" ? `${todayIso().slice(0, 7)}-01` : mondayOf(todayIso());
  const name = group === "month" ? "This month" : "This week";
  return { start, end: addDays(todayIso(), 1), short: "so far", long: `${name} so far`, partial: true };
}

function median(values) {
  const v = [...values].sort((a, b) => a - b);
  if (!v.length) return null;
  return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
}

function daysBetween(a, b) {
  return Math.round((parseDay(b) - parseDay(a)) / 86400000);
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function trendText(cur, prev) {
  if (!prev) return "";
  const pct = Math.round(((cur - prev) * 100) / prev);
  return pct ? ` ${pct > 0 ? "▲" : "▼"} ${pct > 0 ? "+" : ""}${pct}%` : "";
}

function drawStatsFilters(activities) {
  const box = document.getElementById("stats-filters");
  const f = state.statsFilter;
  const set = (key, value) => {
    f[key] = value;
    store(STATS_KEY, JSON.stringify(f));
    drawStats();
  };
  const ranges = el("div", "chips");
  ranges.setAttribute("role", "group");
  ranges.setAttribute("aria-label", "Range");
  for (const [id, label] of RANGES) {
    const chip = el("button", "chip", label);
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(f.range === id));
    chip.addEventListener("click", () => set("range", id));
    ranges.append(chip);
  }
  const group = el("div", "segmented small");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Group by");
  for (const [id, label] of [["week", "Week"], ["month", "Month"]]) {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(f.group === id));
    b.addEventListener("click", () => set("group", id));
    group.append(b);
  }
  const select = el("select", "stats-select");
  select.setAttribute("aria-label", "Activity");
  for (const [value, label] of [["all", "All activities"], ...activities.map((a) => [a, a])]) {
    const o = el("option", null, label);
    o.value = value;
    o.selected = f.activity === value;
    select.append(o);
  }
  select.addEventListener("change", () => set("activity", select.value));
  box.replaceChildren(ranges, group, select);
}

function drawStats() {
  const body = document.getElementById("stats-body");
  const note = document.getElementById("stats-note");
  const s = state.stats;
  const f = state.statsFilter;
  if (currentView() !== "stats" || state.statsMode !== "overview") return;
  const key = `${s ? s.generated : ""}|${JSON.stringify(f)}|${body.clientWidth}`;
  if (key === state.statsDrawn) return; // unchanged: keep open tables and the hover state
  state.statsDrawn = key;
  if (!s || !s.time || !s.time.length) {
    document.getElementById("stats-filters").replaceChildren();
    note.textContent = "No stats yet. The morning brief writes them.";
    body.replaceChildren();
    return;
  }
  // Colours follow all-time totals, so no filter ever repaints an activity.
  const allTime = new Map();
  for (const [, a, m] of s.time) allTime.set(a, (allTime.get(a) || 0) + m);
  const activities = [...allTime.keys()].sort((a, b) => allTime.get(b) - allTime.get(a));
  const named = activities.slice(0, SERIES);
  const clsOf = (a) => (named.includes(a) ? `s${named.indexOf(a) + 1}` : "s-other");
  if (f.activity !== "all" && !allTime.has(f.activity)) f.activity = "all";
  drawStatsFilters(activities);
  const pick = f.activity === "all" ? null : f.activity;

  const firstDay = [s.time[0][0], s.tasks.length ? s.tasks[0][0] : "9999"].sort()[0];
  const pers = statsPeriods(f.group, f.range, firstDay);
  const now = currentPeriod(f.group);
  body.replaceChildren();
  if (!pers.length) {
    note.textContent = "Nothing finished yet in this range: the current week counts once it's over.";
    return;
  }
  const from = pers[0].start;
  const to = pers[pers.length - 1].end; // complete periods end here; "so far" runs to today
  const word = f.group === "month" ? "month" : "week";
  note.textContent = `Totals use complete ${word}s, up to ${shortDate(addDays(to, -1), { weekday: "short", day: "numeric", month: "short" })}; `
    + `the pale bar is this ${word} so far.` + (pick ? ` Showing ${pick}; tasks are for everything.` : "");
  const all = [...pers, now];
  const indexOf = new Map(all.map((p, i) => [p.start, i]));
  const periodOf = f.group === "month" ? (d) => `${d.slice(0, 7)}-01` : mondayOf;
  const prevFrom = f.range === "all" ? null
    : f.group === "month" ? addMonths(from, -pers.length) : addDays(from, -7 * pers.length);

  // time per period, stacked by activity ("Other" on top)
  const byName = new Map();
  let total = 0;
  let prevTotal = 0;
  for (const [day, a, m] of s.time) {
    if (pick && a !== pick) continue;
    if (prevFrom && day >= prevFrom && day < from) prevTotal += m;
    const i = indexOf.get(periodOf(day));
    if (i === undefined) continue;
    const name = pick || named.includes(a) ? a : "Other";
    if (!byName.has(name)) byName.set(name, new Array(all.length).fill(0));
    byName.get(name)[i] += m;
    if (i < pers.length) total += m;
  }
  const series = [...named, "Other", pick].filter((n, i, list) => n && byName.has(n) && list.indexOf(n) === i)
    .map((n) => ({ name: n, cls: n === "Other" ? "s-other" : clsOf(n), values: byName.get(n) }));
  const perPeriod = all.map((_, i) => series.reduce((a, x) => a + x.values[i], 0));
  const complete = perPeriod.slice(0, pers.length);
  const best = complete.indexOf(Math.max(...complete));

  // weekly goals judged inside the range
  const goals = s.goals.filter(([week, a]) => week >= from && week < to && (!pick || a === pick));
  const hit = goals.filter((g) => g[4] === "done").length;

  const span = plural(pers.length, word);
  const tiles = el("div", "tiles");
  const days = daysBetween(from, to);
  tiles.append(
    tile("Time tracked", hm(total), prevFrom ? `${hm(prevTotal)} the ${span} before${trendText(total, prevTotal)}` : `in ${span}`),
    tile("Average week", hm((total * 7) / days), `over ${span}`),
    tile(`Best ${word}`, complete[best] ? hm(complete[best]) : "–", complete[best] ? pers[best].long.replace(/^Week of /, "") : "nothing tracked"),
    tile("Goals reached", goals.length ? `${hit} of ${goals.length}` : "–",
      goals.length ? `goal-weeks in this range` : "no week judged in this range yet"),
  );
  const head = statCard(body, "Overview", null, true);
  head.append(tiles);
  markTrends(head);

  const cols = all.map((p) => ({ short: p.short, long: p.long, partial: p.partial }));
  const time = statCard(body, "Time tracked", `Hours per ${word}${pick ? "" : ", stacked by activity"}`, true);
  columnChart(time, cols, series.length ? series : [{ name: pick || "Time", cls: "s-accent", values: perPeriod }],
    TIME_UNIT, `Time tracked per ${word}`);
  tableView(time, [word === "week" ? "Week of" : "Month", ...series.map((x) => x.name), ...(series.length > 1 ? ["Total"] : [])],
    all.map((p, i) => [p.short, ...series.map((x) => hm(x.values[i])), ...(series.length > 1 ? [hm(perPeriod[i])] : [])]));

  goalGrid(statCard(body, "Weekly goals", "Each finished week against the goal it had then"), goals, s.goals);
  habitCard(statCard(body, "Habit days", `Days with at least one session: average per week over ${span}, and this week`),
    s.time, pick ? [pick] : named.slice(0, 4), clsOf, from, to);

  drawTasks(body, s, cols, indexOf, periodOf, from, word);
}

// Consistency rather than hours: on how many days of the week each activity happened.
function habitCard(card, time, names, clsOf, from, to) {
  const days = new Map(names.map((n) => [n, new Set()]));
  for (const [day, a] of time) if (days.has(a)) days.get(a).add(day);
  const weeks = daysBetween(from, to) / 7;
  const monday = mondayOf(todayIso());
  const week = Array.from({ length: 7 }, (_, i) => addDays(monday, i));
  const list = el("div", "habits");
  for (const name of names) {
    const set = days.get(name);
    const avg = [...set].filter((d) => d >= from && d < to).length / weeks;
    const row = el("div", `habit ${clsOf(name)}`);
    const label = el("span", "habit-name");
    label.append(el("i", "swatch"), name);
    const strip = el("span", "habit-week");
    strip.setAttribute("role", "img");
    const doneNow = week.filter((d) => set.has(d)).length;
    strip.setAttribute("aria-label", `${name}: ${plural(doneNow, "day")} this week`);
    for (const d of week) {
      const cell = el("i", set.has(d) ? "on" : d > todayIso() ? "later" : "");
      cell.title = `${shortDate(d, { weekday: "short", day: "numeric", month: "short" })}${set.has(d) ? ": tracked" : ""}`;
      strip.append(cell);
    }
    row.append(label, el("span", "habit-avg", `${avg.toFixed(1)} a week`), strip);
    list.append(row);
  }
  card.append(list);
}

// ---------- tasks ----------
// One row per task from the history: [list, priority, due, first_seen, exact, done, gone].
// exact 0: the task was already there when the history began, so its age is "at least".

const AGE_BUCKETS = [["Under a week", 7], ["1–4 weeks", 28], ["1–3 months", 91], ["Older", Infinity]];

function drawTasks(body, s, cols, indexOf, periodOf, from, word) {
  const rows = s.task_rows || [];
  if (!rows.length) {
    statCard(body, "Tasks", null, true).append(el("p", "muted", "No task history yet. The morning brief starts it."));
    return;
  }
  const today = todayIso();
  // the history began on the day of the first run (when the tasks already open were found)
  const began = rows.filter((r) => !r[4] && !r[5]).map((r) => r[3]).sort()[0] || rows.map((r) => r[3]).sort()[0];
  // The top heading is the category ("Computer science › Server" counts as Computer science).
  // Tasks ticked off before the history began were already under "Done", so their list is unknown.
  // Without a list: done before the history began ("Before tracking"), or added and moved to Done
  // between two briefs, so no brief saw it open ("List unknown").
  const BEFORE = "Before tracking";
  const UNKNOWN = "List unknown";
  const listName = (l, doneOn) => (l ? l.split(" › ")[0] : doneOn && doneOn < began ? BEFORE : UNKNOWN);
  const noList = (name) => name === BEFORE || name === UNKNOWN;
  // colours follow all-time done counts, so a filter never repaints a category
  const doneCount = new Map();
  for (const r of rows) if (r[5]) doneCount.set(listName(r[0], r[5]), (doneCount.get(listName(r[0], r[5])) || 0) + 1);
  const ranked = [...new Set(rows.map((r) => listName(r[0], r[5])))].filter((l) => !noList(l))
    .sort((a, b) => (doneCount.get(b) || 0) - (doneCount.get(a) || 0)).slice(0, SERIES);
  const listCls = (l) => (ranked.includes(l) ? `s${ranked.indexOf(l) + 1}` : "s-other");

  const n = cols.length;
  const added = new Array(n).fill(0);
  const done = new Array(n).fill(0);
  const doneByList = new Map();
  const perList = new Map(); // name -> {done, due, onTime, days[], open}
  const lst = (name) => {
    if (!perList.has(name)) perList.set(name, { done: 0, due: 0, onTime: 0, days: [], open: 0 });
    return perList.get(name);
  };
  const ages = AGE_BUCKETS.map(() => 0);
  let open = 0;
  let unknownAge = 0;
  let addedSince = 0; // since the history began, to compare like with like
  let doneSince = 0;
  for (const [list, , due, first, exact, doneOn, gone] of rows) {
    const name = listName(list, doneOn);
    if (exact) addedSince += 1;
    if (doneOn && doneOn >= began) doneSince += 1;
    if (exact && indexOf.has(periodOf(first))) added[indexOf.get(periodOf(first))] += 1;
    if (doneOn && indexOf.has(periodOf(doneOn))) {
      const i = indexOf.get(periodOf(doneOn));
      done[i] += 1;
      if (!doneByList.has(name)) doneByList.set(name, new Array(n).fill(0));
      doneByList.get(name)[i] += 1;
      const p = lst(name);
      p.done += 1;
      if (due) {
        p.due += 1;
        p.onTime += doneOn <= due;
      }
      if (exact) p.days.push(daysBetween(first, doneOn));
    }
    if (!doneOn && !gone) {
      open += 1;
      lst(name).open += 1;
      const age = daysBetween(first, today);
      ages[AGE_BUCKETS.findIndex(([, max]) => age < max)] += 1;
      if (!exact) unknownAge += 1;
    }
  }
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const totalDone = sum(done);
  const all = [...perList.values()];
  const withDue = sum(all.map((p) => p.due));
  const onTime = sum(all.map((p) => p.onTime));
  const mid = median(all.flatMap((p) => p.days));
  const net = addedSince - doneSince;
  const young = daysBetween(began, today) < 7; // too early to say whether the list grows

  const flow = statCard(body, "Added vs done", `Tasks per ${word}. Added counts from ${shortDate(began)}, when the history began.`);
  const flowSeries = [{ name: "Added", cls: "s7", values: added }, { name: "Done", cls: "s3", values: done }];
  columnChart(flow, cols, flowSeries, COUNT_UNIT, `Tasks added and done per ${word}`, true);
  tableView(flow, [word === "week" ? "Week of" : "Month", "Added", "Done"], cols.map((c, i) => [c.short, String(added[i]), String(done[i])]));

  const lists = statCard(body, "Done by category", `Tasks ticked off per ${word}, by the list they were in`);
  const listSeries = [...ranked, ...[...doneByList.keys()].filter((l) => !ranked.includes(l))]
    .filter((l) => doneByList.has(l))
    .map((l) => ({ name: l, cls: listCls(l), values: doneByList.get(l) }));
  if (listSeries.length) {
    if (listSeries.length === 1) lists.append(legend(listSeries)); // the title doesn't name a lone category
    columnChart(lists, cols, listSeries, COUNT_UNIT, `Tasks done per ${word} by category`);
  } else {
    lists.append(el("p", "muted", "Nothing ticked off in this range."));
  }

  const table = statCard(body, "By category", "Done in this range, how fast and how often on time; open now", true);
  const rowsOut = [...perList.entries()].sort((a, b) => noList(a[0]) - noList(b[0])
      || b[1].done - a[1].done || b[1].open - a[1].open)
    .map(([name, p]) => {
      const m = median(p.days);
      return [name, String(p.done), m === null ? "–" : plural(Math.round(m), "day"),
        p.due ? `${Math.round((p.onTime * 100) / p.due)}% (${p.onTime}/${p.due})` : "–", String(p.open)];
    });
  listTable(table, ["Category", "Done", "Time to done", "On time", "Open"], rowsOut);
  if (perList.has(BEFORE)) {
    table.append(el("p", "muted", `"${BEFORE}": ticked off before ${shortDate(began)}, already under Done, so their list isn't known. `
      + "Tasks done from now on keep their category."));
  }
  if (perList.has(UNKNOWN)) {
    table.append(el("p", "muted", `"${UNKNOWN}": added and moved to Done before a morning brief saw them open. `
      + "Tick a task where it is and move it the next day to keep its category."));
  }


  const head = statCard(body, "Tasks", `Done, on time and time to done: ${shortDate(from)} to today. `
    + `Net change: since ${shortDate(began)}, when the history began.`, true);
  const tiles = el("div", "tiles");
  tiles.append(
    tile("Open now", String(open), unknownAge ? `${unknownAge} already open on ${shortDate(began)}` : "on your lists today"),
    tile("Done", String(totalDone), `ticked off in this range`),
    tile("Net change", young ? "–" : net > 0 ? `+${net}` : String(net),
      young ? `shown after a week of history (from ${shortDate(addDays(began, 7))})`
        : net > 0 ? `${addedSince} added, ${doneSince} done: the list is growing`
          : net < 0 ? `${addedSince} added, ${doneSince} done: the list is shrinking` : "as many added as done"),
    tile("On time", withDue ? `${Math.round((onTime * 100) / withDue)}%` : "–",
      withDue ? `${onTime} of the ${plural(withDue, "task")} that had a due date` : "no done task had a due date"),
    tile("Time to done", mid === null ? "–" : plural(Math.round(mid), "day"),
      mid === null ? "needs tasks added and done after the history began" : "typical (median) days from added to done"),
  );
  head.append(tiles);

  const age = statCard(body, "Open tasks by age", `Since the brief first saw them${unknownAge ? "; tasks from before the history are at least this old" : ""}`, true);
  const bars = el("div", "age-bars");
  const most = Math.max(1, ...ages);
  AGE_BUCKETS.forEach(([label], i) => {
    const row = el("div", "age-row");
    const track = el("span", "age-track");
    const fill = el("span", "age-fill");
    fill.style.width = `${(ages[i] / most) * 100}%`;
    track.append(fill);
    row.append(el("span", "age-label", label), track, el("span", "age-num", String(ages[i])));
    bars.append(row);
  });
  age.append(bars);
  const oldest = s.tasks_oldest || [];
  if (oldest.length) {
    age.append(el("h4", null, "Oldest open: finish, re-date or delete"));
    const ul = el("ul", "oldest");
    for (const [text, list, first, exact] of oldest) {
      const li = el("li");
      const d = daysBetween(first, today);
      li.append(el("span", "oldest-text", text),
        el("span", "oldest-meta", `${list || "no list"} · ${exact ? plural(d, "day") : `already open on ${shortDate(first)}`}`));
      ul.append(li);
    }
    age.append(ul);
  }
}

// A plain table that is always shown (not folded like tableView).
function listTable(card, head, rows) {
  const scroll = el("div", "table-scroll");
  const table = el("table", "list-table");
  const tr = el("tr");
  for (const h of head) tr.append(el("th", null, h));
  const thead = el("thead");
  thead.append(tr);
  const tbody = el("tbody");
  for (const r of rows) {
    const row = el("tr");
    for (const c of r) row.append(el("td", null, c));
    tbody.append(row);
  }
  table.append(thead, tbody);
  scroll.append(table);
  card.append(scroll);
}

// One row per goal, one cell per judged week: ✓ reached, ✕ missed (a symbol, not only a colour).
function goalGrid(card, goals, all) {
  const weeks = [...new Set(goals.map((g) => g[0]))].sort();
  if (!weeks.length) {
    card.append(el("p", "muted", all.length
      ? `No week judged in this range. The first judged week is ${shortDate(all[0][0])}.`
      : "Weeks are judged on Monday, once the time-tracker export covers the whole week."));
    return;
  }
  const names = [...new Set(goals.map((g) => g[1]))].sort();
  if (weeks.length < 4) { // a grid of one or two columns says less than a line per week
    const ul = el("ul", "goal-lines");
    for (const w of [...weeks].reverse()) {
      const li = el("li");
      li.append(el("span", "goal-line-week", `Week of ${shortDate(w)}`));
      for (const g of goals.filter((x) => x[0] === w)) {
        const ok = g[4] === "done";
        const chip = el("span", `goal-chip ${ok ? "done" : "missed"}`);
        chip.append(el("b", null, ok ? "✓" : "✕"), `${g[1]} ${hm(g[2])} / ${hm(g[3])}`);
        chip.setAttribute("aria-label", `${g[1]}: ${hm(g[2])} of ${hm(g[3])}, ${ok ? "reached" : "missed"}`);
        li.append(chip);
      }
      ul.append(li);
    }
    card.append(ul);
    return;
  }
  const grid = el("div", "goal-grid");
  grid.style.gridTemplateColumns = `max-content repeat(${weeks.length}, minmax(22px, 30px)) max-content`;
  grid.append(el("span"));
  for (const w of weeks) grid.append(el("span", "gg-week", shortDate(w)));
  grid.append(el("span"));
  for (const name of names) {
    grid.append(el("span", "gg-name", name));
    let n = 0;
    for (const w of weeks) {
      const g = goals.find((x) => x[0] === w && x[1] === name);
      if (!g) {
        grid.append(el("span"));
        continue;
      }
      const ok = g[4] === "done";
      n += ok;
      const cell = el("span", `goal-cell ${ok ? "done" : "missed"}`, ok ? "✓" : "✕");
      cell.title = `Week of ${shortDate(w)}: ${hm(g[2])} of ${hm(g[3])}, ${ok ? "reached" : "missed"}`;
      cell.setAttribute("aria-label", cell.title);
      cell.setAttribute("role", "img");
      grid.append(cell);
    }
    grid.append(el("span", "gg-sum", `${n} of ${goals.filter((x) => x[1] === name).length}`));
  }
  const wrap = el("div", "goal-grid-wrap");
  wrap.append(grid);
  card.append(wrap);
}

function loadGrafana() {
  const box = document.getElementById("grafana-box");
  document.getElementById("grafana-link").href = `${GRAFANA_URL}?${GRAFANA_VIEW}`;
  if (box.firstChild) return;
  const theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const frame = el("iframe", "grafana-frame");
  frame.title = "Time tracking in Grafana";
  frame.referrerPolicy = "no-referrer";
  // Grafana runs its own scripts against its own origin; nothing more.
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups");
  frame.src = `${GRAFANA_URL}?${GRAFANA_VIEW}&kiosk&theme=${theme}`;
  box.append(frame);
}

function drawStatsMode() {
  const box = document.getElementById("stats-mode");
  box.hidden = !GRAFANA_URL;
  box.replaceChildren();
  for (const [id, label] of [["overview", "Overview"], ["grafana", "Grafana"]]) {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(state.statsMode === id));
    b.addEventListener("click", () => {
      state.statsMode = id;
      store(STATS_MODE_KEY, id);
      showStatsMode();
    });
    box.append(b);
  }
}

function showStatsMode() {
  if (!GRAFANA_URL) state.statsMode = "overview";
  const grafana = state.statsMode === "grafana";
  document.body.dataset.statsMode = state.statsMode; // Grafana gets the whole screen below the header
  document.getElementById("grafana-link").hidden = !grafana;
  drawStatsMode();
  document.getElementById("stats-overview").hidden = grafana;
  document.getElementById("stats-grafana").hidden = !grafana;
  if (grafana) loadGrafana();
  else drawStats();
}

// ---------- loading ----------

// stats.json is optional: without it the brief has no week bars and the Stats tab says so.
async function loadStats() {
  try {
    const res = await fetch("stats.json", { cache: "no-store" });
    return res.ok ? await res.json() : null;
  } catch (_) {
    return null;
  }
}

async function load() {
  try {
    const [res, stats] = await Promise.all([fetch("data.json", { cache: "no-store" }), loadStats(), loadWorld()]);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    state.stats = stats || state.stats;
    draw();
  } catch (err) {
    const pill = document.getElementById("health");
    pill.className = "pill pill-bad";
    pill.textContent = "Can't load data";
    pill.title = String(err);
  }
}

function markSeen() {
  store(SEEN_KEY, new Date().toISOString());
}

document.addEventListener("DOMContentLoaded", () => {
  state.seenBefore = store(SEEN_KEY);
  if (!store(PUSHES_SEEN_KEY)) store(PUSHES_SEEN_KEY, new Date().toISOString()); // dots start from now
  state.group = store(GROUP_KEY) === "date" ? "date" : "category";
  try {
    const f = JSON.parse(store(FOLDED_KEY) || "null");
    if (Array.isArray(f)) state.folded = new Set(f);
  } catch (_) { /* keep the default */ }
  state.taskView = store(TASK_VIEW_KEY) === "list" ? "list" : "priority";
  state.tasksOpen = store(TASKS_OPEN_KEY) === "1";
  try {
    const f = JSON.parse(store(STATS_KEY) || "null");
    if (f && RANGES.some(([id]) => id === f.range) && ["week", "month"].includes(f.group) && typeof f.activity === "string") {
      state.statsFilter = f;
    }
  } catch (_) { /* keep the default */ }
  state.statsMode = store(STATS_MODE_KEY) === "grafana" ? "grafana" : "overview";
  initWorld();
  let resizing = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizing);
    resizing = setTimeout(drawStats, 150); // the charts are drawn to the card's width
  });
  document.getElementById("group-toggle").addEventListener("click", () => {
    state.group = state.group === "date" ? "category" : "date";
    store(GROUP_KEY, state.group);
    drawGroupToggle();
    drawEmails(state.data ? state.data.emails || [] : []);
  });
  drawGroupToggle();
  const search = document.getElementById("search");
  search.addEventListener("input", () => {
    state.query = search.value;
    drawEmails(state.data ? state.data.emails || [] : []);
  });
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") openSearch(false);
    if (ev.key === "Enter") search.blur(); // closes the phone keyboard, keeps the results
  });
  search.addEventListener("blur", () => {
    if (!search.value) openSearch(false);
  });
  document.getElementById("search-open").addEventListener("click", () => openSearch(true));
  // mousedown: act before the input's blur closes the box
  document.getElementById("search-close").addEventListener("mousedown", (ev) => ev.preventDefault());
  document.getElementById("search-close").addEventListener("click", () => openSearch(false));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && currentView() === "emails" && document.activeElement !== search) {
      ev.preventDefault();
      openSearch(true);
    }
  });
  window.addEventListener("hashchange", showView);
  // "New" marks compare against your previous visit; this visit counts once you leave.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      markSeen();
    } else {
      state.seenBefore = store(SEEN_KEY);
      load();
    }
  });
  window.addEventListener("pagehide", markSeen);
  setInterval(() => {
    if (document.visibilityState === "visible") load();
  }, REFRESH_MS);
  showView();
  load();
});
