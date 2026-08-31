"""
Local-only HTTP server exposing the cigarette leaderboard.

Bound to 127.0.0.1 like bot/llm_status_server.py — reverse-proxy /cigboard/
to it from nginx the same way (see sites-available/status) if you want it
reachable outside the box.
"""

import logging

from aiohttp import web

from bot import config, webserver

from . import discord_users, leaderboard

log = logging.getLogger("discord-llm-bot.cigboard.server")

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cigboard 🚬</title>
<style>
  :root {
    color-scheme: dark;
    --page:      #0b0806;
    --surface-1: #17110c;
    --surface-2: #1e1610;
    --text-1:    #f3ece2;
    --text-2:    #b8a893;
    --text-3:    #7a6c5a;
    --border:    rgba(255,200,140,0.10);
    --ember:     #ff7a1a;
    --ember-2:   #ffb84d;
    --gold:      #ffd166;
    --silver:    #cfd4dc;
    --bronze:    #d3894f;
    --danger:    #ff5c5c;
    --shadow:    0 1px 2px rgba(0,0,0,0.5), 0 12px 32px -16px rgba(0,0,0,0.7);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background:
      radial-gradient(60em 34em at 15% -10%, rgba(255,122,26,0.14), transparent 60%),
      radial-gradient(50em 30em at 100% 0%, rgba(255,92,92,0.10), transparent 55%),
      var(--page);
    color: var(--text-1);
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
  }

  /* drifting embers in the background */
  .embers { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }
  .ember-particle {
    position: absolute;
    bottom: -5%;
    width: var(--s, 5px); height: var(--s, 5px);
    border-radius: 50%;
    background: radial-gradient(circle, var(--ember-2), var(--ember) 70%, transparent 100%);
    opacity: 0.7;
    animation: rise var(--dur, 14s) linear infinite;
    animation-delay: var(--delay, 0s);
    filter: blur(0.3px);
  }
  @keyframes rise {
    0%   { transform: translateY(0) translateX(0); opacity: 0; }
    10%  { opacity: 0.8; }
    90%  { opacity: 0.4; }
    100% { transform: translateY(-115vh) translateX(var(--drift, 20px)); opacity: 0; }
  }

  .wrap { position: relative; z-index: 1; max-width: 56em; margin: 0 auto; padding: 3.2em 1.5em 5em; }

  .header { display: flex; align-items: baseline; gap: 0.6em; margin-bottom: 0.15em; }
  h1 {
    font-size: 1.9em; font-weight: 800; letter-spacing: -0.02em; margin: 0;
    background: linear-gradient(100deg, var(--text-1) 30%, var(--ember-2) 65%, var(--ember) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .live { display: inline-flex; align-items: center; gap: 0.45em; font-size: 0.72em; color: var(--text-3); }
  .live .pulse {
    width: 7px; height: 7px; border-radius: 50%; background: var(--ember);
    box-shadow: 0 0 0 0 rgba(255,122,26,0.5);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(255,122,26,0.5); }
    70%  { box-shadow: 0 0 0 7px rgba(255,122,26,0); }
    100% { box-shadow: 0 0 0 0 rgba(255,122,26,0); }
  }
  .subtitle { color: var(--text-2); font-size: 0.92em; margin: 0 0 2em; }

  .stats-strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(10em, 1fr));
    gap: 0.8em;
    margin-bottom: 2.2em;
  }
  .stat {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1em 1.2em;
    box-shadow: var(--shadow);
  }
  .stat .label { color: var(--text-3); font-size: 0.7em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.5em; }
  .stat .value { font-size: 1.55em; font-weight: 800; font-variant-numeric: tabular-nums; }

  #board { display: flex; flex-direction: column; gap: 0.75em; }

  .card {
    position: relative;
    display: grid;
    grid-template-columns: 2.6em 3.2em 1fr auto;
    align-items: center;
    gap: 1em;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 0.95em 1.3em;
    box-shadow: var(--shadow);
    overflow: hidden;
    transition: transform 0.15s ease, border-color 0.15s ease;
  }
  .card:hover { transform: translateY(-2px); border-color: rgba(255,184,77,0.35); }
  .card.rank-1 {
    border-color: rgba(255,209,102,0.5);
    background: linear-gradient(120deg, rgba(255,209,102,0.10), var(--surface-1) 55%);
  }
  .card.rank-1::after {
    content: ""; position: absolute; inset: 0; border-radius: 14px; pointer-events: none;
    box-shadow: 0 0 30px -4px rgba(255,184,77,0.35) inset;
  }
  .card.rank-2 { border-color: rgba(207,212,220,0.35); }
  .card.rank-3 { border-color: rgba(211,137,79,0.4); }

  .rank {
    font-size: 1.15em; font-weight: 800; text-align: center; color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .rank.medal { font-size: 1.5em; }

  .avatar-wrap { position: relative; width: 3.2em; height: 3.2em; }
  .avatar {
    width: 100%; height: 100%; border-radius: 50%; object-fit: cover;
    border: 2px solid var(--border); display: block; background: var(--surface-2);
  }
  .rank-1 .avatar { border-color: var(--gold); box-shadow: 0 0 18px -2px rgba(255,209,102,0.6); }
  .rank-2 .avatar { border-color: var(--silver); }
  .rank-3 .avatar { border-color: var(--bronze); }

  .who .name { font-weight: 700; font-size: 1.02em; }
  .who .meta { color: var(--text-3); font-size: 0.78em; margin-top: 0.2em; }
  .who .meta b { color: var(--text-2); font-weight: 700; }

  .bar-track { height: 6px; border-radius: 4px; background: var(--surface-2); margin-top: 0.55em; overflow: hidden; max-width: 22em; }
  .bar-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, var(--ember), var(--ember-2)); transition: width 0.6s ease; }

  .totals { text-align: right; }
  .totals .count { font-size: 1.7em; font-weight: 800; font-variant-numeric: tabular-nums; line-height: 1; }
  .totals .count small { font-size: 0.42em; color: var(--text-3); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; margin-left: 0.2em; }
  .totals .today { color: var(--ember-2); font-size: 0.8em; margin-top: 0.35em; font-weight: 600; }

  .spark { display: flex; align-items: flex-end; gap: 2px; height: 22px; margin-top: 0.5em; }
  .spark i { display: block; width: 4px; border-radius: 1px; background: var(--border); }
  .spark i.hit { background: var(--ember); }

  .empty {
    text-align: center; color: var(--text-3); padding: 4em 1em;
    border: 1px dashed var(--border); border-radius: 14px;
  }
  .empty .big { font-size: 2.4em; margin-bottom: 0.3em; }

  footer { text-align: center; color: var(--text-3); font-size: 0.75em; margin-top: 3em; }
</style>
</head>
<body>
<div class="embers" id="embers"></div>
<div class="wrap">
  <div class="header">
    <h1>🚬 cigboard</h1>
    <span class="live"><span class="pulse"></span>live</span>
  </div>
  <p class="subtitle">who's smoking the most, ranked by all-time total</p>

  <div class="stats-strip" id="stats-strip"></div>
  <div id="board"><div class="empty">Loading…</div></div>
  <footer>updates every 5s · timestamps in America/Indiana/Indianapolis</footer>
</div>

<script>
function spawnEmbers() {
  const host = document.getElementById('embers');
  const n = 22;
  for (let i = 0; i < n; i++) {
    const el = document.createElement('div');
    el.className = 'ember-particle';
    const size = (2 + Math.random() * 4).toFixed(1);
    const dur = (9 + Math.random() * 10).toFixed(1);
    const delay = (-Math.random() * 20).toFixed(1);
    const drift = (Math.random() * 80 - 40).toFixed(0);
    el.style.left = (Math.random() * 100) + 'vw';
    el.style.setProperty('--s', size + 'px');
    el.style.setProperty('--dur', dur + 's');
    el.style.setProperty('--delay', delay + 's');
    el.style.setProperty('--drift', drift + 'px');
    host.appendChild(el);
  }
}
spawnEmbers();

const MEDALS = ['🥇', '🥈', '🥉'];

// Display names come from whatever a Discord user set as their global name,
// so they're untrusted input and must never reach innerHTML unescaped.
const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ESCAPES[c]);
}

function fmtAgo(seconds) {
  if (seconds == null) return 'never';
  if (seconds < 60) return 'just now';
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  const d = Math.floor(h / 24);
  return d + 'd ago';
}

function renderSpark(days) {
  const max = Math.max(1, ...days.map(d => d.count));
  return days.map(d => {
    const h = Math.max(2, Math.round((d.count / max) * 20));
    const cls = d.count > 0 ? 'hit' : '';
    return `<i class="${cls}" style="height:${h}px" title="${d.date}: ${d.count}"></i>`;
  }).join('');
}

function renderStats(users) {
  const totalAll = users.reduce((s, u) => s + u.total, 0);
  const todayAll = users.reduce((s, u) => s + u.today, 0);
  const weekAll = users.reduce((s, u) => s + u.week, 0);
  const leader = users[0];
  const el = document.getElementById('stats-strip');
  el.innerHTML = `
    <div class="stat"><div class="label">Smokers tracked</div><div class="value">${users.length}</div></div>
    <div class="stat"><div class="label">Logged today</div><div class="value">${todayAll}</div></div>
    <div class="stat"><div class="label">Logged this week</div><div class="value">${weekAll}</div></div>
    <div class="stat"><div class="label">All-time total</div><div class="value">${totalAll}</div></div>
    <div class="stat"><div class="label">Current leader</div><div class="value" style="font-size:1.05em">${leader ? esc(leader.display_name) : '–'}</div></div>
  `;
}

function render(users) {
  const boardEl = document.getElementById('board');
  if (!users.length) {
    boardEl.innerHTML = '<div class="empty"><div class="big">🫙</div>no cigarettes logged yet — clean streak!</div>';
    document.getElementById('stats-strip').innerHTML = '';
    return;
  }
  renderStats(users);
  const maxTotal = Math.max(1, ...users.map(u => u.total));
  boardEl.innerHTML = users.map((u, i) => {
    const rankLabel = i < 3 ? `<span class="rank medal">${MEDALS[i]}</span>` : `<span class="rank">#${i + 1}</span>`;
    const pct = Math.max(3, Math.round((u.total / maxTotal) * 100));
    return `
      <div class="card rank-${i + 1}">
        ${rankLabel}
        <div class="avatar-wrap"><img class="avatar" src="${esc(u.avatar_url)}" alt="" loading="lazy"></div>
        <div class="who">
          <div class="name">${esc(u.display_name)}</div>
          <div class="meta"><b>${u.avg_per_day}</b>/day avg · last one ${fmtAgo(u.last_smoked_ago_s)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
          <div class="spark">${renderSpark(u.sparkline)}</div>
        </div>
        <div class="totals">
          <div class="count">${u.total}<small>total</small></div>
          <div class="today">${u.today} today · ${u.week} this wk</div>
        </div>
      </div>`;
  }).join('');
}

async function tick() {
  try {
    const resp = await fetch('api/leaderboard');
    const data = await resp.json();
    render(data.users);
  } catch (e) { /* transient — next tick retries */ }
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=_PAGE, content_type="text/html")


async def handle_leaderboard(request: web.Request) -> web.Response:
    rows = leaderboard.compute()
    profiles = await discord_users.resolve_many([row["id"] for row in rows])
    users = [{**row, **profiles[row["id"]]} for row in rows]
    return web.json_response({"users": users})


async def start() -> None:
    await webserver.serve(
        "Cigboard server",
        config.CIGBOARD_SERVER_PORT,
        [web.get("/", handle_index), web.get("/api/leaderboard", handle_leaderboard)],
    )
