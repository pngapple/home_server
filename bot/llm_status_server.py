"""
Local-only HTTP server exposing an at-a-glance LLM metrics dashboard.

Bound to 127.0.0.1 — nginx reverse-proxies /llm/ to it from the
Tailscale-only interface, same pattern as /calendar/oauth/callback and the
Netdata dashboard at /status/ (see sites-available/status).
"""

import logging

from aiohttp import web

from . import config, metrics

log = logging.getLogger("discord-llm-bot.llm_status_server")

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM status</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page:           #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --gridline:       #e1e0d9;
    --border:         rgba(11,11,11,0.10);
    --series-1:       #2a78d6;
    --series-2:       #eb6834;
  }}
  @media (prefers-color-scheme: dark) {{
    .viz-root {{
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page:           #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --gridline:       #2c2c2a;
      --border:         rgba(255,255,255,0.10);
      --series-1:       #3987e5;
      --series-2:       #d95926;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 60em; margin: 0 auto; padding: 2.5em 1.5em 4em; }}
  h1 {{ font-size: 1.4em; margin: 0 0 0.15em; }}
  .subtitle {{ color: var(--text-secondary); font-size: 0.9em; margin: 0 0 2em; }}
  .tiles {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(11em, 1fr));
    gap: 1px;
    background: var(--gridline);
    border: 1px solid var(--gridline);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 2.5em;
  }}
  .tile {{ background: var(--surface-1); padding: 1.1em 1.3em; }}
  .tile .label {{ color: var(--text-secondary); font-size: 0.78em; margin-bottom: 0.4em; }}
  .tile .value {{ font-size: 1.6em; font-weight: 600; font-variant-numeric: proportional-nums; }}
  .tile .sub {{ color: var(--text-muted); font-size: 0.78em; margin-top: 0.3em; }}
  h2 {{ font-size: 1em; color: var(--text-secondary); font-weight: 600; margin: 0 0 0.8em; }}
  .legend {{ display: flex; gap: 1.4em; font-size: 0.82em; color: var(--text-secondary); margin-bottom: 0.9em; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 0.4em; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dot.openrouter {{ background: var(--series-1); }}
  .dot.claude-code {{ background: var(--series-2); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.86em; }}
  th, td {{
    text-align: left;
    padding: 0.55em 0.8em;
    border-bottom: 1px solid var(--gridline);
    font-variant-numeric: tabular-nums;
  }}
  th {{ color: var(--text-muted); font-weight: 500; font-size: 0.82em; }}
  td.num {{ text-align: right; }}
  th.num {{ text-align: right; }}
  .empty {{ color: var(--text-muted); padding: 1em 0; }}
  .badge {{
    display: inline-flex; align-items: center; gap: 0.4em;
    color: var(--text-secondary);
  }}
</style>
</head>
<body>
<div class="viz-root wrap">
  <h1>LLM status</h1>
  <p class="subtitle">home_server discord bot — live metrics, in-memory since last restart</p>

  <div class="tiles" id="tiles">
    <div class="tile"><div class="label">Chat model</div><div class="value" style="font-size:1.1em">{chat_model}</div></div>
    <div class="tile"><div class="label">Total tokens</div><div class="value" id="t-total">–</div><div class="sub" id="t-total-sub"></div></div>
    <div class="tile"><div class="label">Avg tokens/sec</div><div class="value" id="t-tps">–</div><div class="sub">recent calls, output tokens</div></div>
    <div class="tile"><div class="label">Requests</div><div class="value" id="t-calls">–</div></div>
    <div class="tile"><div class="label">Uptime</div><div class="value" id="t-uptime">–</div></div>
    <div class="tile"><div class="label">Last context window</div><div class="value" id="t-ctx">–</div><div class="sub" id="t-ctx-sub"></div></div>
  </div>

  <h2>Recent calls</h2>
  <div class="legend">
    <span><span class="dot openrouter"></span>OpenRouter chat</span>
    <span><span class="dot claude-code"></span>Claude Code bridge</span>
  </div>
  <div id="table-wrap"></div>
</div>

<script>
function fmtCompact(n) {{
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\\.0$/, '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\\.0$/, '') + 'K';
  return String(n);
}}
function fmtUptime(s) {{
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm ' + Math.floor(s % 60) + 's';
}}
function fmtTime(ts) {{
  return new Date(ts * 1000).toLocaleTimeString();
}}
function render(data) {{
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

  const wrap = document.getElementById('table-wrap');
  if (!data.recent.length) {{
    wrap.innerHTML = '<p class="empty">No requests yet since the bot started.</p>';
    return;
  }}
  let rows = data.recent.map(c => `
    <tr>
      <td>${{fmtTime(c.timestamp)}}</td>
      <td><span class="badge"><span class="dot ${{c.source}}"></span>${{c.source === 'openrouter' ? 'OpenRouter' : 'Claude Code'}}</span></td>
      <td>${{c.model}}</td>
      <td class="num">${{c.input_tokens.toLocaleString()}}</td>
      <td class="num">${{c.output_tokens.toLocaleString()}}</td>
      <td class="num">${{c.tokens_per_sec.toFixed(1)}}</td>
      <td class="num">${{c.context_window ? c.context_window.toLocaleString() : '–'}}</td>
    </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Time</th><th>Source</th><th>Model</th>
      <th class="num">In</th><th class="num">Out</th><th class="num">Tok/s</th><th class="num">Context</th>
    </tr></thead>
    <tbody>${{rows}}</tbody>
  </table>`;
}}
async function tick() {{
  try {{
    const resp = await fetch('api/metrics');
    render(await resp.json());
  }} catch (e) {{ /* transient — next tick retries */ }}
}}
tick();
setInterval(tick, 4000);
</script>
</body>
</html>
"""


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=_PAGE.format(chat_model=config.OPENROUTER_MODEL), content_type="text/html")


async def handle_metrics(request: web.Request) -> web.Response:
    return web.json_response(metrics.snapshot())


async def start() -> None:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/metrics", handle_metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.LLM_STATUS_SERVER_PORT)
    await site.start()
    log.info("LLM status server listening on 127.0.0.1:%d", config.LLM_STATUS_SERVER_PORT)
