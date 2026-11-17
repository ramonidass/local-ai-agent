// The Markets and News tabs: drawn from markets.json and news.json (written after every fetch).
// Headlines and source names are untrusted text: they go in with textContent, and a link is only
// made from an https:// address. Uses the helpers in app.js (el, ago, store, svgEl, state).
"use strict";

// [key in markets.json, button label, words, days of the small charts (shares, Bitcoin)]
const MARKET_PERIODS = [
  ["d1", "1D", "1 day", 22, 30], ["w1", "1W", "1 week", 22, 30], ["m1", "1M", "1 month", 22, 30],
  ["m3", "3M", "3 months", 65, 91], ["y1", "1Y", "1 year", 260, 366],
];
const MARKETS_PERIOD_KEY = "agent.marketsPeriod";
const MARKETS_MODE_KEY = "agent.marketsMode";
const NEWS_SECTION_KEY = "agent.newsSection";
const NEWS_TOPIC_KEY = "agent.newsTopic";
const RULE_LABELS = {
  earnings: "Results", earnings_move: "After results", big_day: "Big day", big_week: "Big week",
  high52: "52-week high", low52: "52-week low", trend200: "Trend", vs_market: "vs S&P 500",
};
const KEY_GROUPS = ["crypto", "benchmarks", "gold"]; // the four tiles at the top
const LIST_GROUPS = ["themes", "stocks"];            // rows you can open
const ALERTS_SHOWN = 5;
const NEWS_SHOWN = 15;

const world = { markets: null, news: null, period: "d1", mode: "overview", open: new Set(), allAlerts: false,
  section: "top", topic: "all", newsShown: NEWS_SHOWN, marketsDrawn: "", newsDrawn: "" };

// ---------- numbers ----------

function fmtPrice(v) {
  if (v === null || v === undefined) return "–";
  return "$" + v.toLocaleString("en-GB", v >= 1000 ? { maximumFractionDigits: 0 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtBig(v) {
  if (v === null || v === undefined) return "–";
  const a = Math.abs(v);
  const s = a >= 1e9 ? `${(a / 1e9).toFixed(1)}B` : a >= 1e6 ? `${(a / 1e6).toFixed(1)}M` : a.toLocaleString("en-GB");
  return `${v < 0 ? "−" : ""}$${s}`;
}

function fmtPct(v) {
  if (v === null || v === undefined) return "–";
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(1)}%`;
}

function fmtEps(v) {
  return v === null || v === undefined ? "–" : v.toFixed(2).replace("-", "−");
}

// "▲ 1.9%": the arrow says the direction, the colour only repeats it.
function deltaEl(v, cls) {
  const none = v === null || v === undefined;
  const dir = none || v === 0 ? "flat" : v > 0 ? "up" : "down";
  const node = el("span", `delta ${dir}${cls ? ` ${cls}` : ""}`,
    none ? "–" : `${dir === "up" ? "▲" : dir === "down" ? "▼" : "■"} ${Math.abs(v * 100).toFixed(1)}%`);
  if (!none) node.setAttribute("aria-label", `${dir === "flat" ? "unchanged" : dir} ${Math.abs(v * 100).toFixed(1)}%`);
  return node;
}

function dayWords(iso) {
  const diff = Math.round((parseDay(iso) - parseDay(todayIso())) / 86400000);
  const date = shortDate(iso, { weekday: "short", day: "numeric", month: "short" });
  return diff === 0 ? `Today · ${date}` : diff === 1 ? `Tomorrow · ${date}` : date;
}

function httpsLink(url, text, cls) {
  if (typeof url !== "string" || !url.startsWith("https://")) return el("span", cls, text);
  const a = el("a", cls, text);
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

// A small line of closes; the last point carries a dot when there is room for one.
function sparkline(values, w, h, dot) {
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, class: dot ? "mk-spark" : "row-spark", "aria-hidden": "true" });
  if (!values || values.length < 2) return svg;
  const pad = dot ? 5 : 2;
  const lo = Math.min(...values);
  const span = Math.max(...values) - lo || 1;
  const x = (i) => pad + (i / (values.length - 1)) * (w - 2 * pad);
  const y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  svg.append(svgEl("polyline", { points: values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "), class: "spark-line" }));
  if (dot) svg.append(svgEl("circle", { cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 4, class: "spark-dot" }));
  return svg;
}

function segmented(box, options, current, onPick) {
  box.replaceChildren();
  for (const [id, label, title] of options) {
    const b = el("button", null, label);
    b.type = "button";
    if (title) b.title = title;
    b.setAttribute("aria-pressed", String(current === id));
    b.addEventListener("click", () => onPick(id));
    box.append(b);
  }
}

// ---------- markets ----------

function marketItems(keys) {
  const groups = (world.markets && world.markets.groups) || [];
  return keys.flatMap((k) => ((groups.find((g) => g.key === k) || {}).items || []).map((i) => ({ ...i, group: k })));
}

function sparkFor(item) {
  const p = MARKET_PERIODS.find(([id]) => id === world.period);
  return (item.spark || []).slice(-(item.group === "crypto" ? p[4] : p[3]));
}

function marketTile(item) {
  const t = el("div", "mk-tile");
  const name = el("div", "mk-tile-name", item.name);
  if (item.group !== "crypto") name.append(el("span", "mk-tile-sym", item.symbol)); // the price is the fund's
  t.append(name, el("div", "mk-tile-value", fmtPrice(item.price ?? item.close)),
    deltaEl(item[world.period]), sparkline(sparkFor(item), 140, 40, true));
  return t;
}

// Hits for one symbol within a few days are one alert: results lead (with the share-price move
// after them), then the newest; every rule that fired is a badge.
function groupAlerts(hits) {
  const groups = [];
  for (const h of [...hits].sort((x, y) => (x.day < y.day ? 1 : x.day > y.day ? -1 : y.score - x.score))) {
    const g = groups.find((x) => x.symbol === h.symbol && (parseDay(x.day) - parseDay(h.day)) / 86400000 <= 4);
    if (g) g.hits.push(h);
    else groups.push({ symbol: h.symbol, day: h.day, hits: [h] });
  }
  return groups.map((g) => {
    const result = g.hits.find((h) => h.rule === "earnings");
    const moved = g.hits.find((h) => h.rule === "earnings_move");
    const lead = result || moved || g.hits[0];
    const m = result && moved ? moved.text.match(/ ([+−]\d+(?:\.\d+)?%) on (\w+ \d+ \w+),/) : null;
    const rules = [lead.rule, ...g.hits.map((h) => h.rule)].filter((r, i, all) => all.indexOf(r) === i && !(m && r === "earnings_move"));
    return { day: g.day, rules, text: lead.text + (m ? ` · shares ${m[1]} on ${m[2]}` : "") };
  });
}

// Alerts: what the rules in markets.yaml flagged, newest first.
function alertsCard(hits) {
  const card = el("article", "card brief mk-card");
  card.append(el("h3", null, "Alerts"));
  const alerts = groupAlerts(hits);
  if (!alerts.length) {
    card.append(el("p", "muted", "Nothing unusual in the last 14 days."));
    return card;
  }
  const list = el("ul", "alert-list");
  for (const a of world.allAlerts ? alerts : alerts.slice(0, ALERTS_SHOWN)) {
    const li = el("li", "alert");
    const head = el("div", "alert-head");
    for (const r of a.rules) {
      const kind = r.startsWith("earnings") ? "results" : r === "low52" ? "down" : "move";
      head.append(el("span", `badge rule-${kind}`, RULE_LABELS[r] || r));
    }
    head.append(el("span", "alert-day", shortDate(a.day, { weekday: "short", day: "numeric", month: "short" })));
    li.append(head, el("div", "alert-text", a.text));
    list.append(li);
  }
  card.append(list);
  if (alerts.length > ALERTS_SHOWN) {
    const more = el("button", "task-more", world.allAlerts ? "Show fewer" : `Show all ${alerts.length} from the last 14 days`);
    more.type = "button";
    more.addEventListener("click", () => {
      world.allAlerts = !world.allAlerts;
      drawMarkets(true);
    });
    card.append(more);
  }
  return card;
}

// Sectors: one bar each from zero, best first; the line across them is the S&P 500, so a bar that
// ends right of it beat the market. Every value is written out, so nothing depends on colour.
function sectorCard(period, words) {
  const spy = marketItems(["benchmarks"]).find((i) => i.symbol === "SPY");
  const rows = marketItems(["sectors"]).filter((i) => typeof i[period] === "number").sort((a, b) => b[period] - a[period]);
  if (rows.length < 2) return null;
  const market = spy && typeof spy[period] === "number" ? spy[period] : null;
  const card = el("article", "card brief mk-card");
  card.append(el("h3", null, "Sectors"), el("p", "value-note", `Change over ${words}, best first`));
  const key = el("div", "legend");
  const ahead = el("span", "legend-item ahead");
  ahead.append(el("i", "swatch"), "Ahead of the S&P 500");
  const behind = el("span", "legend-item behind");
  behind.append(el("i", "swatch"), "Behind");
  key.append(ahead, behind);
  if (market !== null) {
    const line = el("span", "legend-item");
    line.append(el("i", "rule-key"), `S&P 500 ${fmtPct(market)}`);
    key.append(line);
  }
  card.append(key);
  const values = rows.map((r) => r[period]).concat(market === null ? [] : [market]);
  const lo = Math.min(0, ...values);
  const span = Math.max(0, ...values) - lo || 1;
  const pos = (v) => ((v - lo) / span) * 100;
  const box = el("div", "sec-rows");
  for (const r of rows) {
    const v = r[period];
    const row = el("div", "sec-row");
    row.title = `${r.name} (${r.symbol}): ${fmtPct(v)} over ${words}` +
      (market === null ? "" : `, ${Math.abs((v - market) * 100).toFixed(1)} points ${v >= market ? "ahead of" : "behind"} the S&P 500`);
    const track = el("div", "sec-track");
    const zero = el("i", "sec-zero");
    zero.style.left = `${pos(0)}%`;
    const bar = el("i", `sec-bar ${v >= 0 ? "pos" : "neg"} ${market === null || v >= market ? "ahead" : "behind"}`);
    bar.style.left = `${Math.min(pos(0), pos(v))}%`;
    bar.style.width = `${Math.abs(pos(v) - pos(0))}%`;
    track.append(zero, bar);
    if (market !== null) {
      const mark = el("i", "sec-mark");
      mark.style.left = `${pos(market)}%`;
      track.append(mark);
    }
    row.append(el("span", "sec-name", r.name), track, el("span", "sec-val", fmtPct(v)));
    box.append(row);
  }
  card.append(box);
  return card;
}

function earningsLine(e) {
  const hour = { amc: " after the close", bmo: " before the open" }[e.hour] || "";
  if (e.eps_act === null && e.rev_act === null) {
    const parts = [];
    if (e.eps_est !== null) parts.push(`EPS ${fmtEps(e.eps_est)}`);
    if (e.rev_est !== null) parts.push(`revenue ${fmtBig(e.rev_est)}`);
    return `Reports${hour}` + (parts.length ? ` · ${parts.join(" and ")} expected` : "");
  }
  const parts = [];
  if (e.rev_act !== null) parts.push(`revenue ${fmtBig(e.rev_act)} vs ${fmtBig(e.rev_est)} expected`);
  if (e.eps_act !== null) parts.push(`EPS ${fmtEps(e.eps_act)} vs ${fmtEps(e.eps_est)}`);
  return parts.join(" · ");
}

function verdictBadge(v) {
  const kind = { beat: "up", missed: "down" }[v] || "flat";
  return el("span", `badge verdict-${kind}`, { beat: "Beat", missed: "Missed", mixed: "Mixed", "in line": "In line" }[v] || "Reported");
}

// What opens under a row: every period, the 52-week range, earnings, alerts and headlines.
function marketDetail(item) {
  const m = world.markets;
  const box = el("div", "mk-more");
  const periods = el("div", "mk-periods");
  for (const [id, label] of MARKET_PERIODS) {
    const cell = el("div");
    cell.append(el("span", "mk-period-label", label), deltaEl(item[id]));
    periods.append(cell);
  }
  box.append(periods);
  if (item.high52 > item.low52) {
    const range = el("div", "range");
    const track = el("div", "range-track");
    const dot = el("i", "range-dot");
    dot.style.left = `${Math.max(0, Math.min(100, ((item.close - item.low52) / (item.high52 - item.low52)) * 100))}%`;
    track.append(dot);
    range.append(el("span", "range-end", fmtPrice(item.low52)), track, el("span", "range-end", fmtPrice(item.high52)));
    box.append(el("div", "mk-label", "52-week range"), range);
  }
  const today = todayIso();
  const mine = (m.earnings || []).filter((e) => e.symbol === item.symbol);
  const next = mine.find((e) => e.eps_act === null && e.rev_act === null && e.date >= today);
  const last = [...mine].reverse().find((e) => e.eps_act !== null || e.rev_act !== null);
  for (const e of [next, last]) {
    if (!e) continue;
    const p = el("p", "mk-fact");
    p.append(el("strong", null, `${shortDate(e.date, { weekday: "short", day: "numeric", month: "short" })} `));
    if (e === last) p.append(verdictBadge(e.verdict), " ");
    p.append(earningsLine(e));
    box.append(el("div", "mk-label", e === next ? "Next results" : "Last results"), p);
  }
  const hits = (m.hits || []).filter((h) => h.symbol === item.symbol);
  if (hits.length) {
    box.append(el("div", "mk-label", "Alerts, last 14 days"));
    const ul = el("ul", "mk-lines");
    for (const h of hits) ul.append(el("li", null, h.text));
    box.append(ul);
  }
  const news = (m.headlines || {})[item.symbol] || [];
  if (news.length) {
    box.append(el("div", "mk-label", "Latest headlines"));
    const ul = el("ul", "mk-lines");
    for (const n of news) {
      const li = el("li");
      li.append(httpsLink(n.url, n.headline, "story-link"), el("span", "story-src", ` ${n.source} · ${ago(n.at)}`));
      ul.append(li);
    }
    box.append(ul);
  }
  return box;
}

function marketRow(item) {
  const wrap = el("div", "mk-item");
  const row = el("button", "mk-row");
  row.type = "button";
  const open = world.open.has(item.symbol);
  row.setAttribute("aria-expanded", String(open));
  const id = el("span", "mk-id");
  id.append(el("span", "mk-sym", item.group === "stocks" ? item.symbol : item.name),
    el("span", "mk-name", item.group === "stocks" ? item.name : item.symbol));
  row.append(id, sparkline(sparkFor(item), 64, 24, false),
    el("span", "mk-price", fmtPrice(item.price ?? item.close)), deltaEl(item[world.period]));
  row.addEventListener("click", () => {
    if (world.open.has(item.symbol)) world.open.delete(item.symbol);
    else world.open.add(item.symbol);
    drawMarkets(true);
  });
  wrap.append(row);
  if (open) wrap.append(marketDetail(item));
  return wrap;
}

function marketsOverview(body) {
  const m = world.markets;
  const [period, , words] = MARKET_PERIODS.find(([id]) => id === world.period);
  body.append(alertsCard(m.hits || []));

  const bar = el("div", "mk-bar");
  bar.append(el("span", "mk-bar-label", "Change over"));
  const seg = el("div", "segmented");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Period");
  segmented(seg, MARKET_PERIODS.map(([id, label, w]) => [id, label, w]), world.period, (id) => {
    world.period = id;
    store(MARKETS_PERIOD_KEY, id);
    drawMarkets(true);
  });
  bar.append(seg);
  body.append(bar);

  const tiles = el("div", "mk-tiles");
  for (const item of marketItems(KEY_GROUPS)) tiles.append(marketTile(item));
  body.append(tiles);

  const sectors = sectorCard(period, words);
  if (sectors) body.append(sectors);

  for (const key of LIST_GROUPS) {
    const items = marketItems([key]);
    if (!items.length) continue;
    body.append(el("h3", "group", key === "themes" ? "Themes" : "Stocks"));
    const list = el("div", "mk-list");
    for (const item of items) list.append(marketRow(item));
    body.append(list);
  }
  body.append(el("p", "muted mk-foot",
    "End-of-day prices from Tiingo; Bitcoin as of the last fetch. The small lines show " +
    `${period === "y1" ? "the last year" : period === "m3" ? "the last 3 months" : "the last month"}. ` +
    "Alerts are rules in markets.yaml, not advice."));
}

// Calendar: results and data releases by day, then the results already out.
function marketsCalendar(body) {
  const m = world.markets;
  const today = todayIso();
  const days = new Map();
  const add = (date, node) => {
    if (!days.has(date)) days.set(date, []);
    days.get(date).push(node);
  };
  const line = (badge, cls, title, sub) => {
    const row = el("div", "cal-row");
    const main = el("div", "cal-main");
    main.append(el("div", "cal-title", title));
    if (sub) main.append(el("div", "cal-sub", sub));
    row.append(el("span", `badge ${cls}`, badge), main);
    return row;
  };
  for (const r of m.releases || []) {
    const rate = /rate decision$/.test(r.name);
    if (r.date >= today) add(r.date, line(rate ? "Rates" : "Data", rate ? "rule-results" : "plain", r.name));
  }
  const results = [];
  for (const e of m.earnings || []) {
    if (e.eps_act === null && e.rev_act === null) {
      if (e.date >= today) add(e.date, line("Results", "rule-move", e.name, earningsLine(e)));
    } else {
      results.push(e);
    }
  }
  const upcoming = [...days.keys()].sort();
  if (!upcoming.length) body.append(el("p", "card muted empty", "Nothing in the calendar for the next weeks."));
  for (const date of upcoming) {
    body.append(el("div", "day", dayWords(date)));
    const list = el("div", "mk-list");
    list.append(...days.get(date));
    body.append(list);
  }
  if (results.length) {
    body.append(el("h3", "group", "Results already out"));
    const list = el("div", "mk-list");
    for (const e of results.reverse()) {
      const row = el("div", "cal-row");
      const main = el("div", "cal-main");
      main.append(el("div", "cal-title", `${e.name} · ${shortDate(e.date, { weekday: "short", day: "numeric", month: "short" })}`),
        el("div", "cal-sub", earningsLine(e)));
      row.append(verdictBadge(e.verdict), main);
      list.append(row);
    }
    body.append(list);
  }
  body.append(el("p", "muted mk-foot",
    "US data dates from FRED; central-bank dates and the watchlist are in markets.yaml. Results for your stocks only."));
}

function drawMarkets(force) {
  const m = world.markets;
  const body = document.getElementById("markets-body");
  const note = document.getElementById("markets-note");
  segmented(document.getElementById("markets-mode"), [["overview", "Overview"], ["calendar", "Calendar"]], world.mode, (id) => {
    world.mode = id;
    store(MARKETS_MODE_KEY, id);
    drawMarkets(true);
  });
  if (!m || !(m.groups || []).length) {
    note.textContent = "";
    body.replaceChildren(el("p", "card muted empty", "No market data yet. It arrives with the next fetch (07:35 and 18:00)."));
    return;
  }
  const key = `${m.generated}|${world.mode}|${world.period}`;
  if (!force && key === world.marketsDrawn) return; // a refresh with nothing new keeps open rows as they are
  world.marketsDrawn = key;
  const failed = (m.fetch && m.fetch.failed) || [];
  note.textContent = `US close ${shortDate(m.us_close, { weekday: "short", day: "numeric", month: "short" })} · updated ${ago(m.generated)}` +
    (failed.length ? ` · not updated: ${failed.join(", ")}` : "");
  body.replaceChildren();
  if (world.mode === "calendar") marketsCalendar(body);
  else marketsOverview(body);
}

// "▲ 1.9%" in a text becomes a coloured span.
function colourDeltas(text) {
  const frag = document.createDocumentFragment();
  for (const part of text.split(/([▲▼■] \d+(?:\.\d+)?%)/)) {
    if (!part) continue;
    frag.append(/^[▲▼■] \d/.test(part) ? el("span", `delta ${part[0] === "▲" ? "up" : part[0] === "▼" ? "down" : "flat"}`, part) : part);
  }
  return frag;
}

// The Markets card inside the brief: a small table from markets.json (name, 1 day, 1 week) with the
// numbers in columns, so they can be compared by running an eye down them. The brief's own lines
// are only kept when markets.json is missing; its alerts and "Coming up" stay under the table.
const BRIEF_MOVERS = 3;

function briefMarketsTable() {
  const key = marketItems(KEY_GROUPS);
  if (!key.length) return null;
  const table = el("table", "mk-table");
  const row = (item, cls) => {
    const tr = el("tr", cls);
    const name = el("th", null, item.name);
    name.scope = "row";
    if (item.group === "crypto") name.append(el("span", "mk-table-price", fmtPrice(item.price ?? item.close)));
    const d1 = el("td"), w1 = el("td");
    d1.append(deltaEl(item.d1));
    w1.append(deltaEl(item.w1));
    tr.append(name, d1, w1);
    return tr;
  };
  const head = (label, cols) => {
    const tr = el("tr", "mk-table-head");
    const th = el("th", null, label);
    th.scope = cols ? "col" : "colgroup";
    if (!cols) th.colSpan = 3;
    tr.append(th);
    for (const c of cols || []) {
      const h = el("th", null, c);
      h.scope = "col";
      tr.append(h);
    }
    return tr;
  };
  const m = world.markets;
  const body = el("tbody");
  body.append(head(m.us_close ? `US close ${shortDate(m.us_close, { weekday: "short", day: "numeric", month: "short" })}` : "Prices", ["1 day", "1 week"]));
  for (const item of key) body.append(row(item));
  const movers = marketItems(LIST_GROUPS).filter((i) => typeof i.d1 === "number")
    .sort((a, b) => Math.abs(b.d1) - Math.abs(a.d1)).slice(0, BRIEF_MOVERS);
  if (movers.length) {
    body.append(head("Biggest moves"));
    for (const item of movers) body.append(row(item));
  }
  table.append(body);
  return table;
}

function marketsBriefCard(card) {
  const first = card.querySelector(":scope > ul");
  const table = briefMarketsTable();
  if (first && table) {
    first.replaceWith(table);
  } else if (first) {
    first.classList.add("mk-brief");
    for (const li of first.querySelectorAll(":scope > li")) {
      const label = li.querySelector(":scope > strong");
      if (!label) continue;
      const parts = li.textContent.slice(label.textContent.length).split(" · ").map((t) => t.trim()).filter(Boolean);
      const chips = el("div", "mk-chips");
      for (const part of parts) {
        const chip = el("span", "mk-chip");
        chip.append(colourDeltas(part));
        chips.append(chip);
      }
      li.replaceChildren(el("div", "mk-label", label.textContent), chips);
    }
  }
  const link = el("a", "card-link", "Open Markets");
  link.href = "#markets";
  card.append(link);
}

// ---------- news ----------

function newsSections() {
  const n = world.news;
  return [["top", "Top"]].concat(((n && n.sections) || []).map((s) => [s, s]));
}

// A list from news.json, or none: a news.json from before topics or candidates
// lacks them, and the tab then works as it did.
function newsList(key) {
  const v = world.news && world.news[key];
  return Array.isArray(v) ? v : [];
}

function newsTopics() {
  return newsList("topics").filter((t) => typeof t === "string");
}

function inTopic(s, topic) {
  return topic === "all" || (Array.isArray(s.topics) && s.topics.includes(topic));
}

// Top: the day's candidates in news.py's order (most-covered, then newest, outlets spread). Without
// them, what several outlets cover, most-covered first; on a quiet day the newest from Europe, the
// US and the world fill it up.
function topStories(limit) {
  const picked = newsList("candidates");
  if (picked.length) return picked.slice(0, limit);
  const stories = newsList("stories");
  const top = stories.filter((s) => s.outlets.length > 1).slice(0, limit);
  if (top.length < limit) {
    const rest = stories.filter((s) => s.outlets.length === 1 && ["Europe", "US", "World"].includes(s.section))
      .sort((a, b) => (a.at < b.at ? 1 : -1));
    top.push(...rest.slice(0, limit - top.length));
  }
  return top;
}

function storyNode(s, withSection) {
  const node = el("article", "story");
  node.append(httpsLink(s.url, s.title, "story-title"));
  if (s.summary) node.append(el("p", "story-sum", s.summary));
  const meta = el("div", "story-meta");
  if (withSection) meta.append(el("span", "badge plain", s.section));
  if (s.outlets.length === 1) {
    meta.append(el("span", null, `${s.outlets[0]} · ${ago(s.at)}`));
    node.append(meta);
    return node;
  }
  meta.append(el("span", null, ago(s.at)));
  node.append(meta);
  // the same story as each outlet tells it
  const det = el("details", "story-more");
  const sum = el("summary");
  sum.append(el("span", "outlet-count", `${s.outlets.length} outlets`), ` ${s.outlets.join(" · ")}`);
  det.append(sum);
  const ul = el("ul", "mk-lines");
  for (const it of s.items) {
    const li = el("li");
    li.append(el("strong", null, `${it.outlet} `), httpsLink(it.url, it.title, "story-link"), el("span", "story-src", ` ${ago(it.at)}`));
    ul.append(li);
  }
  det.append(ul);
  node.append(det);
  return node;
}

function hackerNewsCard(limit) {
  const hn = ((world.news && world.news.hn) || []).slice(0, limit);
  if (!hn.length) return null;
  const card = el("article", "card brief mk-card");
  card.append(el("h3", null, "On Hacker News"), el("p", "value-note", "What people in tech are reading, by points"));
  const ul = el("ul", "mk-lines");
  for (const h of hn) {
    const li = el("li");
    li.append(httpsLink(h.url, h.title, "story-link"),
      el("div", "story-src", `${h.score} points · ${h.comments} comment${h.comments === 1 ? "" : "s"}`));
    ul.append(li);
  }
  card.append(ul);
  return card;
}

// A row of chips; the pressed one is `current`. counts: {id: number} shown after the label.
function newsChips(box, options, current, counts, onPick) {
  box.replaceChildren();
  for (const [id, label] of options) {
    const b = el("button", "chip", label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(current === id));
    if (counts && id in counts) b.append(el("span", null, String(counts[id])));
    b.addEventListener("click", () => {
      onPick(id);
      world.newsShown = NEWS_SHOWN;
      drawNews(true);
      window.scrollTo(0, 0);
    });
    box.append(b);
  }
}

function drawNews(force) {
  const n = world.news;
  const body = document.getElementById("news-body");
  const chips = document.getElementById("news-filters");
  const topicChips = document.getElementById("news-topics");
  const count = document.getElementById("news-count");
  if (!n || !(n.stories || []).length) {
    chips.replaceChildren();
    topicChips.replaceChildren();
    topicChips.hidden = true;
    count.textContent = "";
    body.replaceChildren(el("p", "card muted empty", "No news yet. It arrives with the next fetch (07:35 and 18:00)."));
    return;
  }
  const sections = newsSections();
  if (!sections.some(([id]) => id === world.section)) world.section = "top";
  const topics = newsTopics();
  if (!topics.includes(world.topic)) world.topic = "all";
  const key = `${n.generated}|${world.section}|${world.topic}|${world.newsShown}`;
  if (!force && key === world.newsDrawn) return; // keeps an opened story open across refreshes
  world.newsDrawn = key;
  const sectionCounts = {};
  for (const [id] of sections.slice(1)) sectionCounts[id] = n.stories.filter((s) => s.section === id).length;
  newsChips(chips, sections, world.section, sectionCounts, (id) => {
    world.section = id;
    store(NEWS_SECTION_KEY, id);
  });
  count.textContent = `${n.outlets.length} outlets · ${ago(n.fetched)}`;
  const top = world.section === "top";
  const picked = top && newsList("candidates").length > 0;
  // the section picks the stories, the topic narrows them
  const pool = top ? topStories(picked ? Infinity : 10) : n.stories.filter((s) => s.section === world.section);
  const topicCounts = { all: pool.length };
  for (const t of topics) topicCounts[t] = pool.filter((s) => inTopic(s, t)).length;
  // topics with nothing here are left out; the chosen one stays, even at 0, so you can switch back
  const shownTopics = topics.filter((t) => topicCounts[t] > 0 || t === world.topic);
  topicChips.hidden = shownTopics.length === 0;
  newsChips(topicChips, [["all", "All topics"]].concat(shownTopics.map((t) => [t, t])), world.topic, topicCounts, (id) => {
    world.topic = id;
    store(NEWS_TOPIC_KEY, id);
  });
  const all = pool.filter((s) => inTopic(s, world.topic));
  const shown = all.slice(0, world.newsShown);
  const about = world.topic === "all" ? "" : ` about ${world.topic}`;
  body.replaceChildren();
  if (top) {
    body.append(el("p", "muted stats-note", picked
      ? "Today's shortlist: stories with a topic, most-covered first, then the newest, and only a few led by one outlet. Open one to compare how each tells it."
      : "Stories several outlets are covering, most-covered first. Open one to compare how each tells it."));
  }
  if (!all.length) {
    body.append(el("p", "card muted empty", `Nothing${about} in ${top ? "the top stories" : world.section} from the last fetch.`));
  } else {
    const list = el("div", "mk-list");
    for (const s of shown) list.append(storyNode(s, top));
    body.append(list);
  }
  if (all.length > shown.length) {
    const more = el("button", "task-more", `Show ${Math.min(NEWS_SHOWN, all.length - shown.length)} more`);
    more.type = "button";
    more.addEventListener("click", () => {
      world.newsShown += NEWS_SHOWN;
      drawNews(true);
    });
    body.append(more);
  } else if (all.length) {
    body.append(el("p", "muted list-end", top ? `Those are today's top stories${about}. The sections above have the rest.`
    : `That's everything${about} in ${world.section} from the last fetch.`));
  }
  // Hacker News: 15 under its topic (hackernews: topic: in news.yaml), 5 under Top with all topics
  const hnTopic = typeof n.hn_topic === "string" && world.topic === n.hn_topic;
  if (hnTopic || (top && world.topic === "all")) {
    const hn = hackerNewsCard(hnTopic ? 15 : 5);
    if (hn) body.append(hn);
  }
  const failed = (n.failed_feeds || []).concat((n.fetch && n.fetch.failed) || []);
  body.append(el("p", "muted mk-foot", `Headlines straight from ${n.outlets.join(", ")}; nothing is rewritten.` +
    (failed.length ? ` Not updated: ${failed.join(", ")}.` : "")));
}

// "Top stories" under the brief: drawn by the page, so headlines never enter the brief's own text.
function briefNewsCard() {
  const top = topStories(3);
  if (!top.length) return null;
  const card = el("article", "card brief wide news-card");
  card.append(el("h3", null, "Top stories"));
  const list = el("div", "brief-stories");
  for (const s of top) {
    const row = el("div", "brief-story");
    row.append(httpsLink(s.url, s.title, "story-title"),
      el("span", "story-src", s.outlets.length > 1 ? `${s.outlets.length} outlets · ${s.outlets.join(", ")}` : `${s.outlets[0]} · ${ago(s.at)}`));
    list.append(row);
  }
  const link = el("a", "card-link", "Open News");
  link.href = "#news";
  card.append(list, link);
  return card;
}

// ---------- loading ----------

async function loadWorld() {
  const get = async (name) => {
    try {
      const res = await fetch(name, { cache: "no-store" });
      return res.ok ? await res.json() : null;
    } catch (_) {
      return null;
    }
  };
  const [markets, news] = await Promise.all([get("markets.json"), get("news.json")]);
  world.markets = markets || world.markets;
  world.news = news || world.news;
}

function initWorld() {
  if (MARKET_PERIODS.some(([id]) => id === store(MARKETS_PERIOD_KEY))) world.period = store(MARKETS_PERIOD_KEY);
  world.mode = store(MARKETS_MODE_KEY) === "calendar" ? "calendar" : "overview";
  world.section = store(NEWS_SECTION_KEY) || "top";
  world.topic = store(NEWS_TOPIC_KEY) || "all";
}
