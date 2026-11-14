"""Build a self-contained, browsable HTML page from the index: `recs site -o referrals.html`.

The page embeds the data as JSON (no server, works offline). It shows, for every pro, who vouched,
their full quotes with group and date, and the question each neighbour was answering, so a name
never stands alone without the evidence behind it.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date

from .db import DB
from .extract import fmt_phone
from .trades import TRADES


def slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def build_data(db: DB) -> dict:
    pros = []
    for p in db.con.execute("SELECT * FROM pros WHERE hidden = 0"):
        ms = db.con.execute(
            """SELECT m.*, n.first_name, o.first_name AS owner_first FROM mentions m
               JOIN neighbors n ON n.id = m.neighbor_id LEFT JOIN neighbors o ON o.id = m.card_owner_id
               WHERE m.pro_id = ? ORDER BY m.posted_at DESC""", (p["id"],)).fetchall()
        if not ms:
            continue
        contacts = []
        for c in db.con.execute("SELECT kind, value FROM pro_contacts WHERE pro_id = ? ORDER BY kind DESC, value", (p["id"],)):
            if c["kind"] == "phone":
                contacts.append({"kind": "phone", "value": fmt_phone(c["value"])})
            elif c["kind"] == "email":
                contacts.append({"kind": "email", "value": c["value"]})
            else:
                label = re.sub(r"^https?://(www\.)?", "", c["value"]).split("?")[0].rstrip("/")
                contacts.append({"kind": "web", "value": c["value"], "label": label[:48]})
        mentions = []
        for m in ms:
            extra = json.loads(m["extra"] or "{}")
            req = extra.get("request") or None
            mentions.append({
                "n": m["neighbor_id"], "who": m["first_name"] or "a neighbor", "group": m["group_name"],
                "date": m["posted_at"][:10], "s": m["sentiment"], "quote": m["quote"],
                "trades": json.loads(m["trades"]) or ([m["trade"]] if m["trade"] else []),
                "card": bool(m["is_card"]), "owner": m["owner_first"],
                "req": {"by": req.get("by") or "a neighbor", "text": req["text"]} if req and req.get("text") else None,
            })
        trades = [r[0] for r in db.con.execute("SELECT trade FROM pro_trades WHERE pro_id = ?", (p["id"],))]
        owners = sorted({m["owner"] or m["who"] for m in mentions if m["card"]})
        pros.append({"id": p["id"], "name": p["display_name"], "kind": p["kind"], "trades": trades,
                     "contacts": contacts, "mentions": mentions, "cardOwners": owners})
    groups = [r[0] for r in db.con.execute("SELECT DISTINCT group_name FROM sources ORDER BY group_name")]
    last = db.con.execute("SELECT MAX(posted_at) FROM mentions").fetchone()[0] or ""
    order = [t for t in TRADES if any(t in p["trades"] for p in pros)]
    return {"pros": pros, "groups": groups, "trades": [{"key": t, "slug": slug(t)} for t in order],
            "through": last[:10], "built": date.today().isoformat(),
            "neighbors": db.con.execute("SELECT COUNT(DISTINCT neighbor_id) FROM mentions m JOIN pros p ON p.id = m.pro_id AND p.hidden = 0").fetchone()[0],
            "mentions": sum(len(p["mentions"]) for p in pros)}


def default_title(groups: list[str]) -> str:
    common = re.match(r"(\w+)", groups[0]).group(1) if groups else "Neighborhood"
    return f"{common} Referrals"


def render(db: DB, title: str | None = None, standalone: bool = True, nonce: str | None = None,
           user: str | None = None, csrf: str | None = None) -> str:
    """standalone: a full HTML document (for a file or the web server). False gives the body-only
    fragment the claude.ai artifact viewer expects. nonce/user/csrf are set by the web server."""
    data = build_data(db)
    title = title or default_title(data["groups"])
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    auth = ""
    if user is not None:
        auth = (f'<form class="authbar" method="post" action="/logout"><span>Signed in as <b>{html.escape(user)}</b></span>'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf or "")}"><button type="submit">Sign out</button></form>')
    page = TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__AUTHBAR__", auth).replace("__DATA__", payload)
    if nonce:
        page = page.replace("<style>", f'<style nonce="{nonce}">').replace("<script>", f'<script nonce="{nonce}">')
    if standalone:
        page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                '<meta name="robots" content="noindex, nofollow">\n' + page.replace("\n<div class=\"wrap\">", "\n</head>\n<body>\n<div class=\"wrap\">", 1)
                + "\n</body>\n</html>\n")
    return page


TEMPLATE = r"""<title>__TITLE__</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* Layout: a referral board. Trade rail on the left (chips on phones), evidence-first pro cards on the right:
   every name sits on top of the neighbours' own words, who said them, where, and what they were answering. */
:root {
  --bg: #f3f5f4; --surface: #ffffff; --sunk: #e8edeb; --line: #d5ddda;
  --fg: #17211e; --muted: #5b6b66; --accent: #1f5f8b; --accent-soft: #dceaf3;
  --good: #23784a; --good-soft: #dcefe3; --warn: #9a6a07; --warn-soft: #f6ecd2; --bad: #b23a2e; --bad-soft: #f6dedb;
  --display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --body: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --r: 10px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #111816; --surface: #18211e; --sunk: #0d1312; --line: #2a3632;
  --fg: #e4ece9; --muted: #93a49e; --accent: #7db8e0; --accent-soft: #1c3343;
  --good: #6cc792; --good-soft: #17301f; --warn: #e0b453; --warn-soft: #33290f; --bad: #ef8b7f; --bad-soft: #3a1d1a;
  color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #111816; --surface: #18211e; --sunk: #0d1312; --line: #2a3632;
  --fg: #e4ece9; --muted: #93a49e; --accent: #7db8e0; --accent-soft: #1c3343;
  --good: #6cc792; --good-soft: #17301f; --warn: #e0b453; --warn-soft: #33290f; --bad: #ef8b7f; --bad-soft: #3a1d1a;
  color-scheme: dark }

* { box-sizing: border-box }
body { background: var(--bg); color: var(--fg); font: 15px/1.55 var(--body); }
.wrap { max-width: 1120px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 28px 64px; }
header.top { display: grid; gap: 6px; margin-bottom: 22px }
h1 { font: 700 clamp(28px, 4vw, 40px)/1.05 var(--display); letter-spacing: -0.02em; margin: 0; text-wrap: balance }
.sub { color: var(--muted); max-width: 68ch; margin: 0 }
.facts { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 8px; font-size: 13px; color: var(--muted) }
.facts b { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums }

.layout { display: grid; grid-template-columns: 230px minmax(0, 1fr); gap: 28px; align-items: start }
nav.rail { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 12px); display: grid; gap: 2px }
.rail h2 { font: 600 11px/1 var(--body); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 8px 10px }
.rail a { display: flex; justify-content: space-between; gap: 8px; padding: 7px 10px; border-radius: 8px; color: var(--fg); text-decoration: none; font-size: 14px }
.rail a span:last-child { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12.5px }
.rail a:hover { background: var(--sunk) }
.rail a[aria-current="true"] { background: var(--accent-soft); color: var(--accent); font-weight: 600 }
.rail a[aria-current="true"] span:last-child { color: var(--accent) }
.rail a:first-letter { text-transform: uppercase }

.tools { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 16px }
.search { flex: 1 1 260px; min-width: 0; position: relative }
.search input { width: 100%; font: inherit; padding: 10px 12px 10px 36px; border: 1px solid var(--line); border-radius: var(--r); background: var(--surface); color: var(--fg) }
.search svg { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); width: 16px; height: 16px; color: var(--muted) }
.tools label { display: inline-flex; gap: 6px; align-items: center; font-size: 13.5px; color: var(--muted); cursor: pointer }
select { font: inherit; font-size: 13.5px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--fg) }
input:focus-visible, select:focus-visible, button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }

.heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin: 4px 0 14px }
.heading h2 { font: 700 22px/1.2 var(--display); margin: 0; letter-spacing: -0.01em }
.heading h2::first-letter { text-transform: uppercase }
.heading p { margin: 0; color: var(--muted); font-size: 13px }

.list { display: grid; gap: 14px }
article.pro { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; display: grid; gap: 12px; min-width: 0 }
.pro-head { display: flex; justify-content: space-between; gap: 14px; flex-wrap: wrap; align-items: flex-start }
.pro-name { font: 700 20px/1.2 var(--display); margin: 0; letter-spacing: -0.01em; overflow-wrap: anywhere }
.pro-name small { display: block; font: 500 13px/1.4 var(--body); color: var(--muted); margin-top: 3px; letter-spacing: 0 }
.trust { display: grid; justify-items: end; gap: 4px; text-align: right }
.trust .count { font: 600 14px/1.2 var(--body) }
.trust .count.none { color: var(--muted); font-weight: 500 }
.faces { display: flex; gap: 0 }
.faces span { width: 26px; height: 26px; border-radius: 50%; display: grid; place-items: center; font: 600 11.5px/1 var(--body);
  background: var(--good-soft); color: var(--good); border: 2px solid var(--surface); margin-left: -6px }
.faces span:first-child { margin-left: 0 }
.faces span.neg { background: var(--bad-soft); color: var(--bad) }
.faces span.neu { background: var(--warn-soft); color: var(--warn) }

.meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12.5px; color: var(--muted) }
.chip { display: inline-block; padding: 2px 9px; border-radius: 999px; background: var(--sunk); color: var(--fg); font-size: 12px; white-space: nowrap }
.chip.on { background: var(--accent-soft); color: var(--accent) }
.dot::before { content: "·"; margin: 0 2px }

.contacts { display: flex; flex-wrap: wrap; gap: 8px }
.contact { display: inline-flex; align-items: center; gap: 8px; padding: 6px 6px 6px 11px; border: 1px solid var(--line); border-radius: 8px; font: 500 13.5px/1.2 var(--mono); max-width: 100%; min-width: 0 }
.contact a { color: var(--accent); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.contact a:hover { text-decoration: underline }
.contact .val { user-select: all; white-space: nowrap }
.contact button { font: 500 11.5px/1 var(--body); border: 0; background: var(--sunk); color: var(--muted); padding: 5px 8px; border-radius: 6px; cursor: pointer }
.contact button:hover { color: var(--fg) }
.ask { font-size: 13.5px; color: var(--warn); background: var(--warn-soft); border-radius: 8px; padding: 8px 11px; margin: 0 }

ol.quotes { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px }
ol.quotes li { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 10px; padding-top: 10px; border-top: 1px solid var(--line) }
.mark { width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font: 700 12px/1 var(--body); margin-top: 2px }
.mark.positive { background: var(--good-soft); color: var(--good) }
.mark.negative { background: var(--bad-soft); color: var(--bad) }
.mark.neutral { background: var(--warn-soft); color: var(--warn) }
blockquote { margin: 0; overflow-wrap: anywhere; max-width: 72ch }
.by { font-size: 12.5px; color: var(--muted); margin-top: 3px }
.by b { color: var(--fg); font-weight: 600 }
.req { font-size: 12.5px; color: var(--muted); margin-top: 5px; padding-left: 10px; border-left: 2px solid var(--line); max-width: 72ch; overflow-wrap: anywhere }
.req b { font-weight: 600; color: var(--fg) }
.more { justify-self: start; font: 500 13px/1 var(--body); color: var(--accent); background: none; border: 0; padding: 4px 0; cursor: pointer }
.empty { padding: 40px 20px; text-align: center; color: var(--muted); border: 1px dashed var(--line); border-radius: 14px }
.authbar { display: flex; justify-content: flex-end; align-items: center; gap: 10px; font-size: 13px; color: var(--muted) }
.authbar button { font: 500 13px/1 var(--body); padding: 7px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--fg); cursor: pointer }
footer { margin-top: 40px; font-size: 12.5px; color: var(--muted); max-width: 75ch }

@media (max-width: 760px) {
  .layout { grid-template-columns: minmax(0, 1fr); gap: 14px }
  nav.rail { position: static; display: flex; overflow-x: auto; gap: 6px; padding-bottom: 6px; scrollbar-width: thin }
  .rail h2 { display: none }
  .rail a { flex: 0 0 auto; border: 1px solid var(--line); background: var(--surface); padding: 6px 11px; border-radius: 999px; gap: 6px }
  .trust { justify-items: start; text-align: left }
  article.pro { padding: 16px }
}
@media (prefers-reduced-motion: no-preference) { .rail a, .contact button { transition: background .15s, color .15s } }
</style>

<div class="wrap">
  <header class="top">
    __AUTHBAR__
    <h1 id="title">__TITLE__</h1>
    <p class="sub">Service pros your neighbors have actually used, with their own words, who said it, in which group, and what they were answering.</p>
    <div class="facts" id="facts"></div>
  </header>
  <div class="layout">
    <nav class="rail" id="rail" aria-label="Trades"></nav>
    <main>
      <div class="tools">
        <div class="search">
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg>
          <input id="q" type="search" placeholder="Search names, phone numbers, quotes…" aria-label="Search">
        </div>
        <select id="sort" aria-label="Sort">
          <option value="vouch">Most neighbors vouch</option>
          <option value="recent">Most recent</option>
        </select>
        <label><input type="checkbox" id="named"> Named pros only</label>
      </div>
      <div class="heading"><h2 id="h"></h2><p id="hint"></p></div>
      <div class="list" id="list"></div>
    </main>
  </div>
  <footer id="foot"></footer>
</div>

<script>
const DATA = __DATA__;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtDate = (d) => { const [y, m, dd] = d.split("-").map(Number); return new Date(y, m - 1, dd).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}); };
const cap = (s) => s === "hvac" ? "HVAC" : s.charAt(0).toUpperCase() + s.slice(1);
const SHOW = 3;
const opened = new Set();

function stats(p, trade) {
  const ms = trade ? p.mentions.filter((m) => m.trades.includes(trade)) : p.mentions;
  const ids = (s) => new Set(ms.filter((m) => m.s === s).map((m) => m.n));
  const pos = ids("positive"), neg = ids("negative"), neu = ids("neutral");
  const score = [...pos].filter((n) => !neg.has(n)).length;
  const lastPos = ms.filter((m) => m.s === "positive").map((m) => m.date).sort().pop() || "";
  const last = ms.map((m) => m.date).sort().pop() || "";
  const groups = [...new Set(ms.map((m) => m.group))];
  return {ms, pos, neg, neu, score, lastPos, last, groups};
}

function counts() {
  const c = {};
  for (const t of DATA.trades) c[t.key] = DATA.pros.filter((p) => p.mentions.some((m) => m.trades.includes(t.key))).length;
  return c;
}

function currentTrade() {
  const h = decodeURIComponent(location.hash.slice(1));
  const t = DATA.trades.find((t) => t.slug === h);
  return t ? t.key : null;
}

function renderRail(trade) {
  const c = counts();
  const all = `<a href="#all" aria-current="${!trade}"><span>All trades</span><span>${DATA.pros.length}</span></a>`;
  $("#rail").innerHTML = `<h2>Trades</h2>` + all + DATA.trades
    .map((t) => `<a href="#${t.slug}" aria-current="${trade === t.key}"><span>${esc(cap(t.key))}</span><span>${c[t.key]}</span></a>`).join("");
}

function contactHTML(c) {
  if (c.kind === "web") return `<span class="contact"><a href="${esc(c.value)}" target="_blank" rel="noopener noreferrer">${esc(c.label)}</a></span>`;
  return `<span class="contact"><span class="val">${esc(c.value)}</span><button type="button" data-copy="${esc(c.value)}">Copy</button></span>`;
}

function faces(st, p) {
  const firsts = new Map();
  for (const m of st.ms) if (!firsts.has(m.n)) firsts.set(m.n, m);
  const out = [];
  for (const [n, m] of firsts) {
    const cls = st.neg.has(n) ? "neg" : st.pos.has(n) ? "" : "neu";
    out.push(`<span class="${cls}" title="${esc(m.who)}">${esc((m.who === "a neighbor" ? "?" : m.who[0]).toUpperCase())}</span>`);
  }
  return out.slice(0, 8).join("");
}

function trustLine(st) {
  const bits = [];
  if (st.score) bits.push(st.score === 1 ? "1 neighbor vouches" : `${st.score} neighbors vouch`);
  if (st.neg.size) bits.push(`${st.neg.size} warned`);
  if (st.neu.size) bits.push(`${st.neu.size} unverified`);
  return bits.join(" · ") || "Mentioned, not vouched for";
}

function quoteHTML(m) {
  const sym = {positive: "✓", negative: "!", neutral: "?"}[m.s];
  const label = {positive: "Recommends", negative: "Warns", neutral: "Unverified mention"}[m.s];
  const req = m.req ? `<div class="req">Answering <b>${esc(m.req.by)}</b>: “${esc(m.req.text)}”</div>` : "";
  const q = m.quote === "(shared a contact card)" ? `<i>Shared a contact card, no comment.</i>` : `“${esc(m.quote)}”`;
  return `<li><span class="mark ${m.s}" role="img" aria-label="${label}">${sym}</span><div>
      <blockquote>${q}</blockquote>
      <div class="by"><b>${esc(m.who)}</b> · ${esc(m.group)} · ${fmtDate(m.date)}${m.card && m.quote !== "(shared a contact card)" ? " · shared a contact card" : ""}</div>
      ${req}</div></li>`;
}

function proHTML(p, trade) {
  const st = stats(p, trade);
  const other = p.trades.filter((t) => t !== trade);
  const chips = (trade ? [trade] : []).concat(trade ? [] : p.trades).map((t) => `<span class="chip on">${esc(t === "hvac" ? "HVAC" : t)}</span>`).join(" ");
  const also = trade && other.length ? `<span class="dot"></span><span>also for ${other.map(esc).join(", ")}</span>` : "";
  const ms = st.ms.slice().sort((a, b) => b.date.localeCompare(a.date));
  const open = opened.has(p.id);
  const shown = open ? ms : ms.slice(0, SHOW);
  const ask = p.kind === "unnamed" && !p.contacts.length
    ? `<p class="ask">WhatsApp leaves contact cards out of exports, so the name and number aren't here. Ask ${esc(p.cardOwners.join(" or "))} for it.</p>` : "";
  const sinceTxt = st.groups.length > 1 ? `${st.groups.length} groups` : esc(st.groups[0]);
  return `<article class="pro">
    <div class="pro-head">
      <h3 class="pro-name">${esc(p.name)}</h3>
      <div class="trust"><div class="faces" aria-hidden="true">${faces(st, p)}</div><div class="count ${st.score ? "" : "none"}">${trustLine(st)}</div></div>
    </div>
    <div class="meta">${chips}${also}<span class="dot"></span><span>${sinceTxt}</span><span class="dot"></span><span>latest ${fmtDate(st.last)}</span></div>
    ${p.contacts.length ? `<div class="contacts">${p.contacts.map(contactHTML).join("")}</div>` : ""}
    ${ask}
    <ol class="quotes">${shown.map(quoteHTML).join("")}</ol>
    ${ms.length > SHOW ? `<button type="button" class="more" data-pro="${p.id}">${open ? "Show fewer" : `Show all ${ms.length} mentions`}</button>` : ""}
  </article>`;
}

function render() {
  const trade = currentTrade();
  renderRail(trade);
  const q = $("#q").value.trim().toLowerCase();
  const qd = q.replace(/\D/g, "");
  const namedOnly = $("#named").checked;
  let pros = DATA.pros.filter((p) => !trade || p.mentions.some((m) => m.trades.includes(trade)));
  if (namedOnly) pros = pros.filter((p) => p.kind !== "unnamed");
  if (q) pros = pros.filter((p) => {
    const hay = [p.name, ...p.trades, ...p.mentions.map((m) => m.quote + " " + m.who), ...p.contacts.map((c) => c.value)].join(" ").toLowerCase();
    return hay.includes(q) || (qd.length >= 4 && p.contacts.some((c) => c.value.replace(/\D/g, "").includes(qd)));
  });
  const byRecent = $("#sort").value === "recent";
  pros = pros.map((p) => ({p, st: stats(p, trade)})).sort((a, b) =>
    byRecent ? b.st.last.localeCompare(a.st.last)
             : (b.st.score - a.st.score) || b.st.lastPos.localeCompare(a.st.lastPos) || b.st.last.localeCompare(a.st.last)
  ).map((x) => x.p);
  $("#h").textContent = trade ? cap(trade) : "All trades";
  $("#hint").textContent = `${pros.length} pro${pros.length === 1 ? "" : "s"} · ranked by ${byRecent ? "most recent mention" : "neighbors who vouch, then most recent"}`;
  $("#list").innerHTML = pros.length ? pros.map((p) => proHTML(p, trade)).join("")
    : `<div class="empty">No pros match${q ? ` “${esc(q)}”` : ""}. Try another trade or clear the search.</div>`;
  try { localStorage.setItem("recs-prefs", JSON.stringify({sort: $("#sort").value, named: namedOnly})); } catch (e) {}
}

$("#facts").innerHTML = [
  `<span><b>${DATA.pros.length}</b> pros</span>`,
  `<span><b>${DATA.mentions}</b> recommendations</span>`,
  `<span><b>${DATA.neighbors}</b> neighbors</span>`,
  `<span>From ${DATA.groups.map((g) => `<b>${esc(g)}</b>`).join(" and ")}</span>`,
  `<span>Latest recommendation <b>${fmtDate(DATA.through)}</b></span>`,
].join("");
$("#foot").innerHTML = `✓ recommends, ! warns, ? mentioned without first-hand experience. Neighbors appear by first name only; their phone numbers are not stored.
  Built ${fmtDate(DATA.built)} with <code>recs site</code>; rebuild after importing new exports.`;

try { const pr = JSON.parse(localStorage.getItem("recs-prefs") || "{}"); if (pr.sort) $("#sort").value = pr.sort; $("#named").checked = !!pr.named; } catch (e) {}
$("#q").addEventListener("input", render);
$("#sort").addEventListener("change", render);
$("#named").addEventListener("change", render);
window.addEventListener("hashchange", () => { render(); window.scrollTo({top: 0}); });
document.addEventListener("click", async (e) => {
  const c = e.target.closest("[data-copy]");
  if (c) {
    try { await navigator.clipboard.writeText(c.dataset.copy); c.textContent = "Copied"; }
    catch (err) { const v = c.previousElementSibling; const r = document.createRange(); r.selectNodeContents(v); const s = getSelection(); s.removeAllRanges(); s.addRange(r); c.textContent = "Selected"; }
    setTimeout(() => (c.textContent = "Copy"), 1500);
  }
  const m = e.target.closest(".more");
  if (m) { const id = Number(m.dataset.pro); opened.has(id) ? opened.delete(id) : opened.add(id); render(); }
});
render();
</script>
"""
