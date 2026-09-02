"""
Local-only HTTP server exposing an at-a-glance LLM metrics dashboard.

Bound to 127.0.0.1 — nginx reverse-proxies /llm/ to it from the
Tailscale-only interface, same pattern as /calendar/oauth/callback and the
Netdata dashboard at /status/ (see sites-available/status).
"""

import html
import logging
import time

from aiohttp import web

from . import config, httpclient, metrics, webserver

log = logging.getLogger("discord-llm-bot.llm_status_server")

# OpenRouter's key-info endpoint is the authoritative source for credit
# usage/limit (rather than us estimating from per-call responses) — cached
# briefly so the 4s dashboard poll doesn't hit it every tick.
_OR_CREDITS_TTL_S = 60.0
_or_credits_cache: dict = {"data": None, "ts": 0.0}


async def _fetch_openrouter_credits() -> dict | None:
    now = time.monotonic()
    if _or_credits_cache["data"] is not None and now - _or_credits_cache["ts"] < _OR_CREDITS_TTL_S:
        return _or_credits_cache["data"]
    try:
        async with httpclient.session().get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    except Exception:
        log.exception("Failed to fetch OpenRouter credit info")
        return _or_credits_cache["data"]  # serve last-known value (or None) rather than fail the whole tick
    data = payload["data"]
    _or_credits_cache["data"] = data
    _or_credits_cache["ts"] = now
    return data


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM status</title>
<style>
  :root {
    color-scheme: dark;
    --page:           #0a0c10;
    --surface-1:      #14171d;
    --surface-2:      #191d24;
    --text-primary:   #eef0f3;
    --text-secondary: #9aa1ac;
    --text-muted:     #5d636e;
    --gridline:       #262b33;
    --border:         rgba(255,255,255,0.08);
    --series-1:       #5b9df9;
    --series-2:       #f5a35b;
    --good:           #35c987;
    --shadow:         0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -12px rgba(0,0,0,0.6);
  }
  @media (prefers-color-scheme: light) {
    :root {
      color-scheme: light;
      --page:           #f7f7f5;
      --surface-1:      #ffffff;
      --surface-2:      #f0f0ee;
      --text-primary:   #14161a;
      --text-secondary: #55595f;
      --text-muted:     #8b8f95;
      --gridline:       #e3e3e0;
      --border:         rgba(11,11,11,0.08);
      --series-1:       #2a6fd6;
      --series-2:       #d9702a;
      --good:           #1f9e5e;
      --shadow:         0 1px 2px rgba(20,20,20,0.05), 0 8px 24px -14px rgba(20,20,20,0.15);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 64em; margin: 0 auto; padding: 3em 1.5em 4em; }
  .header { display: flex; align-items: baseline; gap: 0.7em; margin-bottom: 0.2em; }
  h1 { font-size: 1.5em; font-weight: 700; letter-spacing: -0.01em; margin: 0; }
  .live { display: inline-flex; align-items: center; gap: 0.45em; font-size: 0.76em; color: var(--text-muted); }
  .live .pulse {
    width: 7px; height: 7px; border-radius: 50%; background: var(--good);
    box-shadow: 0 0 0 0 rgba(53,201,135,0.5);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(53,201,135,0.45); }
    70%  { box-shadow: 0 0 0 6px rgba(53,201,135,0); }
    100% { box-shadow: 0 0 0 0 rgba(53,201,135,0); }
  }
  .subtitle { color: var(--text-secondary); font-size: 0.92em; margin: 0 0 2.2em; }

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(11.5em, 1fr));
    gap: 0.9em;
    margin-bottom: 2.2em;
  }
  .tile {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.15em 1.3em;
    box-shadow: var(--shadow);
  }
  .tile .label {
    color: var(--text-secondary); font-size: 0.72em; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.55em;
  }
  .tile .value {
    font-size: 1.7em; font-weight: 700; letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums;
  }
  .tile .sub { color: var(--text-muted); font-size: 0.78em; margin-top: 0.35em; }

  .panel {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.4em 1.5em 1.6em;
    box-shadow: var(--shadow);
    margin-bottom: 1.4em;
  }
  h2 { font-size: 0.92em; color: var(--text-primary); font-weight: 700; margin: 0 0 1em; }
  .legend { display: flex; gap: 1.4em; font-size: 0.8em; color: var(--text-secondary); margin-bottom: 1.1em; }
  .legend span { display: inline-flex; align-items: center; gap: 0.45em; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
  .dot.openrouter { background: var(--series-1); }
  .dot.claude-code { background: var(--series-2); }

  .chart-wrap { width: 100%; overflow: hidden; }
  #chart { width: 100%; height: 110px; display: block; overflow: visible; }
  .chart-empty { color: var(--text-muted); font-size: 0.85em; padding: 2em 0; text-align: center; }

  table { width: 100%; border-collapse: collapse; font-size: 0.86em; }
  th, td {
    text-align: left;
    padding: 0.65em 0.7em;
    border-bottom: 1px solid var(--gridline);
    font-variant-numeric: tabular-nums;
  }
  tbody tr:hover { background: var(--surface-2); }
  tbody tr:last-child td { border-bottom: none; }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.76em; text-transform: uppercase; letter-spacing: 0.04em; }
  td.num, th.num { text-align: right; }
  td.time { color: var(--text-secondary); }
  .empty { color: var(--text-muted); padding: 1em 0; }
  .badge {
    display: inline-flex; align-items: center; gap: 0.5em;
    padding: 0.25em 0.65em 0.25em 0.55em;
    border-radius: 999px;
    font-size: 0.92em;
  }
  .badge.openrouter { background: color-mix(in srgb, var(--series-1) 16%, transparent); color: var(--series-1); }
  .badge.claude-code { background: color-mix(in srgb, var(--series-2) 16%, transparent); color: var(--series-2); }

  .bar { height: 5px; border-radius: 3px; background: var(--gridline); margin-top: 0.65em; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; background: var(--series-1); transition: width 0.4s ease; }
</style>
</head>
<body>
<div class="viz-root wrap">
  <div class="header">
    <h1>LLM status</h1>
    <span class="live"><span class="pulse"></span>live</span>
  </div>
  <p class="subtitle">home_server discord bot — metrics in-memory since last restart</p>

  <div class="tiles" id="tiles">
    <div class="tile"><div class="label">Chat model</div><div class="value" style="font-size:1.05em">__CHAT_MODEL__</div></div>
    <div class="tile"><div class="label">Total tokens</div><div class="value" id="t-total">–</div><div class="sub" id="t-total-sub"></div></div>
    <div class="tile"><div class="label">Avg tokens/sec</div><div class="value" id="t-tps">–</div><div class="sub">recent calls, output tokens</div></div>
    <div class="tile"><div class="label">Requests</div><div class="value" id="t-calls">–</div></div>
    <div class="tile"><div class="label">Uptime</div><div class="value" id="t-uptime">–</div></div>
    <div class="tile"><div class="label">Last context window</div><div class="value" id="t-ctx">–</div><div class="sub" id="t-ctx-sub"></div></div>
    <div class="tile">
      <div class="label">OpenRouter credits</div>
      <div class="value" id="t-or-credits">–</div>
      <div class="sub" id="t-or-credits-sub"></div>
      <div class="bar" id="t-or-bar-wrap"><div class="bar-fill" id="t-or-bar" style="width:0%"></div></div>
    </div>
    <div class="tile"><div class="label">Claude Code spend</div><div class="value" id="t-cc-cost">–</div><div class="sub">since last restart</div></div>
  </div>

  <div class="panel">
    <h2>Throughput, recent calls</h2>
    <div class="legend">
      <span><span class="dot openrouter"></span>OpenRouter chat</span>
      <span><span class="dot claude-code"></span>Claude Code bridge</span>
    </div>
    <div class="chart-wrap" id="chart-wrap"></div>
  </div>

  <div class="panel">
    <h2>Per-user usage</h2>
    <div id="user-table-wrap"></div>
  </div>

  <div class="panel">
    <h2>Recent calls</h2>
    <div id="table-wrap"></div>
  </div>
</div>

<script>
function fmtCompact(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\\.0$/, '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\\.0$/, '') + 'K';
  return String(n);
}
function fmtUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm ' + Math.floor(s % 60) + 's';
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}
function renderChart(recent) {
  const wrap = document.getElementById('chart-wrap');
  const data = [...recent].reverse(); // oldest -> newest, left to right
  if (!data.length) {
    wrap.innerHTML = '<p class="chart-empty">No requests yet since the bot started.</p>';
    return;
  }
  const W = 1000, H = 110, PAD_B = 18;
  const maxTps = Math.max(1, ...data.map(c => c.tokens_per_sec));
  const n = data.length;
  const gap = 4;
  const barW = Math.max(3, (W / n) - gap);
  const style = getComputedStyle(document.querySelector('.viz-root'));
  const colors = {
    openrouter: style.getPropertyValue('--series-1').trim(),
    'claude-code': style.getPropertyValue('--series-2').trim(),
  };
  let bars = data.map((c, i) => {
    const h = Math.max(2, (c.tokens_per_sec / maxTps) * (H - PAD_B));
    const x = i * (W / n) + gap / 2;
    const y = (H - PAD_B) - h;
    const title = `${c.model} — ${c.tokens_per_sec.toFixed(1)} tok/s`;
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="${colors[c.source] || '#888'}" opacity="0.92"><title>${title}</title></rect>`;
  }).join('');
  wrap.innerHTML = `<svg id="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}</svg>`;
}
function render(data) {
  document.getElementById('t-total').textContent = fmtCompact(data.total_tokens);
  document.getElementById('t-total-sub').textContent =
    fmtCompact(data.total_input_tokens) + ' in / ' + fmtCompact(data.total_output_tokens) + ' out';
  document.getElementById('t-tps').textContent = data.avg_tokens_per_sec.toFixed(1) + ' tok/s';
  document.getElementById('t-calls').textContent = data.total_calls;
  document.getElementById('t-uptime').textContent = fmtUptime(data.uptime_s);

  const last = data.last;
  document.getElementById('t-ctx').textContent = last && last.context_window
    ? fmtCompact(last.context_window) : '–';
  document.getElementById('t-ctx-sub').textContent = last ? last.model : '';

  const cr = data.openrouter_credits;
  if (cr && cr.remaining != null) {
    document.getElementById('t-or-credits').textContent = '$' + cr.remaining.toFixed(2);
    document.getElementById('t-or-bar-wrap').style.display = '';
    if (cr.limit) {
      document.getElementById('t-or-credits-sub').textContent =
        '$' + cr.used.toFixed(2) + ' used of $' + cr.limit.toFixed(2);
      const pct = Math.max(0, Math.min(100, (cr.remaining / cr.limit) * 100));
      document.getElementById('t-or-bar').style.width = pct + '%';
    } else {
      document.getElementById('t-or-credits-sub').textContent = '$' + cr.used.toFixed(2) + ' used, no cap set';
      document.getElementById('t-or-bar-wrap').style.display = 'none';
    }
  } else {
    document.getElementById('t-or-credits').textContent = 'n/a';
    document.getElementById('t-or-credits-sub').textContent = 'could not reach OpenRouter';
    document.getElementById('t-or-bar-wrap').style.display = 'none';
  }
  document.getElementById('t-cc-cost').textContent = '$' + (data.claude_code_cost_usd || 0).toFixed(3);

  renderChart(data.recent);

  const uwrap = document.getElementById('user-table-wrap');
  if (!data.by_user || !data.by_user.length) {
    uwrap.innerHTML = '<p class="empty">No requests yet since the bot started.</p>';
  } else {
    let urows = data.by_user.map(u => `
      <tr>
        <td>${escapeHtml(u.user_name)}</td>
        <td class="num">${u.calls.toLocaleString()}</td>
        <td class="num">${fmtCompact(u.total_tokens)}</td>
        <td class="num">${u.openrouter_cost_usd ? '$' + u.openrouter_cost_usd.toFixed(3) : '–'}</td>
        <td class="num">${u.claude_code_cost_usd ? '$' + u.claude_code_cost_usd.toFixed(3) : '–'}</td>
      </tr>`).join('');
    uwrap.innerHTML = `<table>
      <thead><tr>
        <th>User</th><th class="num">Requests</th><th class="num">Tokens</th>
        <th class="num">OpenRouter cost</th><th class="num">Claude Code spend</th>
      </tr></thead>
      <tbody>${urows}</tbody>
    </table>`;
  }

  const wrap = document.getElementById('table-wrap');
  if (!data.recent.length) {
    wrap.innerHTML = '<p class="empty">No requests yet since the bot started.</p>';
    return;
  }
  let rows = data.recent.map(c => `
    <tr>
      <td class="time">${fmtTime(c.timestamp)}</td>
      <td><span class="badge ${c.source}"><span class="dot ${c.source}"></span>${c.source === 'openrouter' ? 'OpenRouter' : 'Claude Code'}</span></td>
      <td>${c.model}</td>
      <td class="num">${c.input_tokens.toLocaleString()}</td>
      <td class="num">${c.output_tokens.toLocaleString()}</td>
      <td class="num">${c.tokens_per_sec.toFixed(1)}</td>
      <td class="num">${c.context_window ? c.context_window.toLocaleString() : '–'}</td>
    </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Time</th><th>Source</th><th>Model</th>
      <th class="num">In</th><th class="num">Out</th><th class="num">Tok/s</th><th class="num">Context</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}
async function tick() {
  try {
    const resp = await fetch('api/metrics');
    render(await resp.json());
  } catch (e) { /* transient — next tick retries */ }
}
tick();
setInterval(tick, 4000);
</script>
</body>
</html>
"""


async def handle_index(request: web.Request) -> web.Response:
    page = _PAGE.replace("__CHAT_MODEL__", html.escape(config.OPENROUTER_MODEL))
    return web.Response(text=page, content_type="text/html")


async def handle_metrics(request: web.Request) -> web.Response:
    snap = metrics.snapshot()
    credits = await _fetch_openrouter_credits()
    snap["openrouter_credits"] = (
        {
            "limit": credits.get("limit"),
            "used": credits.get("usage"),
            "remaining": credits.get("limit_remaining"),
        }
        if credits
        else None
    )
    return web.json_response(snap)


async def start() -> None:
    await webserver.serve(
        "LLM status server",
        config.LLM_STATUS_SERVER_PORT,
        [web.get("/", handle_index), web.get("/api/metrics", handle_metrics)],
    )
