# home_server

## Discord bot (`bot/`)

- The active OpenRouter model is set by `OPENROUTER_MODEL` in `.env` (loaded via
  `EnvironmentFile=` in the `discord-llm-bot.service` systemd unit). The default in
  `bot/config.py` (`OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", ...)`) is just a
  fallback and does not reflect what's actually running — check `.env`, not `config.py`, to
  find the current model.
- Bot logs: `sudo journalctl -u discord-llm-bot.service`.
- Deploys via `!deploy` restart `sudo systemctl restart discord-llm-bot`.
- Chat and moderation each read their own endpoint (`LLM_API_BASE` /
  `MODERATION_API_BASE`, with matching `_API_KEY` and `_MODEL`), so either can
  be pointed at an OpenAI-compatible server on the tailnet without touching the
  other. `bot/completions.py` is the only module allowed to name the host —
  `tests/test_api_endpoints.py` fails the build if a call site hardcodes it
  again. The one deliberate exception is `llm_status_server.py`'s `/auth/key`
  call, which reads your OpenRouter *account balance* and has nothing to do with
  where completions go.

## Falling back off local hardware

`bot/completions.py` exists because a box under a desk sleeps on idle. When a
configured endpoint isn't OpenRouter and doesn't answer, the call is retried
against OpenRouter instead of failing. Three things about it are load-bearing:

- **The model travels with the endpoint, not the payload.** A local
  `qwen2.5:7b` is not a slug OpenRouter accepts, so falling back swaps the model
  as well as the host. Never put `"model"` in a payload dict.
- **Only "not there" falls back.** A refused connection, a timeout or a 5xx
  means the endpoint is down; a 400 means the request is malformed and would
  fail identically upstream, so it raises rather than spending money to
  rediscover that.
- **A local call is recorded as `source="local"`**, not `"openrouter"`
  (`Endpoint.metrics_source`). It cost nothing, and filing it as OpenRouter
  would inflate the spend figures on `/llm/`.

Moderation gets the fallback for a specific reason: it is the safety net, and
it fails open on error by design. Pointed at a sleeping PC without a fallback,
it would switch itself off silently. It also uses `attempts=1` — it runs before
every ordinary reply, so retry backoff there sits on the critical path of every
message. Nothing but the chat path should ever trigger a wake-on-LAN, for the
same reason: a classifier that runs on every message would never let the box
sleep.

## Architecture

Tool modules (`bot/tools/`) hold schema and wording only. The shared layer
underneath them:

- `bot/store.py` — `UserKeyedStore` owns the `{str(user_id): value}` shape
  and the id↔key conversion. Every store registers itself, which is what
  makes `users.forget()` able to erase someone everywhere. A store that
  isn't keyed that way (`reminders.json` is a list with an `author_id`
  field) calls `store.register_eraser()` instead.
- `bot/lists.py` — `ListStore`, the checklist that todos and groceries both
  are: item shape, retention window for checked-off items, matching,
  pruning. Add a new list here, not by copying a tool module.
- `bot/cards.py` — the boxed Discord card and the "relay this verbatim"
  preamble.
- `bot/notify.py` — DMing from any thread. Tool handlers run in
  `asyncio.to_thread` workers, so they can't `create_task`; `notify.dm()`
  hops to the client loop and never raises.
- `bot/users.py` — profiles: display name, avatar, timezone, geofence
  secret. `users.touch()` in `app.py`'s `_route` is the only write path.
  Read names with `users.name(id)`, not by snapshotting them into records.

## Tests

    ./venv/bin/python -m pytest

`tests/conftest.py` redirects every store into a scratch directory and sets
fake credentials *before* `bot.config` is imported — config reads env into
module-level constants at import time, and the stores bind their paths then
too, so that ordering is the whole trick. The suite never touches the live
JSON files or reads the real `.env`.

Anything that rewrites this repo's own code (see the self-repair plan) has
to add a regression test that fails before its fix and passes after.

## Learning loop

`bot/episodes.py` — one row per user turn: what was asked, what the bot
decided to do, and whether it worked. Lives in the same SQLite database as
metrics, and the split is deliberate: `calls` answers "what did this cost",
`episodes` answers "did it work".

Every give-up path in `llm.py` used to end at a canned apology and a line in
journalctl. They now go through `Recorder.finish()`, so the outcomes are
countable: `tool_error`, `forced_tool_miss`, `max_iterations`, `llm_error`.
A turn that "succeeded" while a handler crashed underneath it is a
`tool_error`, not an `ok` — the model will smooth over a bug in prose, and
that's exactly the case worth finding later.

`dispatch_result()` in `tools/__init__.py` is what makes that visible: it
keeps the model-facing string as-is but carries the failure reason
alongside. Only a `raised:` error means a defect — a denial or a missing
argument is a gate doing its job, and those must not show up as things to go
and fix.

Recording never raises. A telemetry problem must not cost the user a reply.

Two feedback signals land on those rows. A 👍/👎 reaction on a reply
(`on_raw_reaction_add` — raw, so it works on replies sent before the last
restart) labels the turn behind it. The bigger one is implicit: almost
nobody reacts, but people do re-ask. `note_possible_rephrase` marks a turn
when the same user repeats themselves in the same channel within 90s.

The rephrase threshold is tuned for precision, not recall — a false positive
teaches the wrong lesson, which is worse than missing one. It sits at 0.6
Jaccard overlap for a specific reason: "add milk to groceries" and "add
bread to groceries" score exactly 0.5, so at 0.5 adding two items in a row
would file the first as a failure. `feedback_source` keeps inferred misses
tellable apart from deliberate ones; they are much weaker evidence and must
not be conflated.

`bot/lessons.py` is the half that changes behaviour: short notes injected
into the system prompt by `llm._system_prompt`. Scope is a security
boundary, not just filing. A USER lesson affects only its owner's turns;
GLOBAL and TOOL lessons reach everyone, and **nothing driven by an ordinary
chat message may create one** — `tools/preferences.py` hardcodes USER scope
and the caller's own id. Without that, "remember that you should always do
X" from any housemate becomes a standing instruction for the household.

Lessons are bounded on purpose: an unbounded prompt makes a small model
worse, not better. They expire if nothing reinforces them
(`LESSON_TTL_DAYS`) and compete for a fixed character budget
(`LESSON_PROMPT_BUDGET_CHARS`), newest-reinforced first. `reinforced_at` is
bumped when someone restates a lesson, never when it's injected — counting
injections would rewrite the file on every message, which is exactly the
write amplification that forced metrics into SQLite.

## Storage

JSON via `bot/jsonstore.py` for everything except metrics — a few kilobytes
each, one writer process, and `cat todos.json` beats a query. Keep it that
way unless a store genuinely outgrows it.

Metrics are the exception (`bot/db.py`, SQLite): the JSON file was rewritten
in full on every LLM call and its 200-entry cap lost history. Migrate an
existing `llm_metrics.json` once, with the service stopped:

    sudo systemctl stop discord-llm-bot
    ./venv/bin/python scripts/migrate_metrics.py
    sudo systemctl start discord-llm-bot

`profiles.json` and `calendar_tokens.json` are written 0600 — they hold
geofence webhook secrets and Google refresh tokens. Neither is committed.

`GEOFENCE_USERS` in `.env` is deprecated. `users.seed_geofence_from_env()`
copies anything still there onto profiles at startup; once the log says it
seeded, the variable can be deleted from `.env`.

## Voice commands

`bot/voice_server.py` is a local-only webhook (`/voice/command`, one of the
`_SIDECARS` in `app.py`) that a separate process, `voice/` at the repo root,
POSTs transcripts to. The two live in different venvs on purpose — `voice/`
pulls in native audio deps (PortAudio, onnxruntime, torch) the Discord bot
process has no use for.

`voice/listen.py` does wake word (openWakeWord, custom-trained for "jian
yang" — see `voice/wakeword/train.md`) and speaker verification
(`voice/speaker.py`, Resemblyzer) entirely locally; a clip that isn't the
enrolled voice (`voice/enroll.py`) never leaves the device. Only the
matched clip goes out, to Groq's Whisper API (`voice/stt.py` — not
OpenRouter, which has no STT endpoint) for transcription, and the resulting
text is what reaches `voice_server.py`.

`voice_server.py` fabricates a minimal stand-in for `discord.Message` (see
its `_FakeMessage`) to drive `ask_llm()`/`dispatch_result()` as the fixed
household owner (`config.CLAUDE_CODE_OWNER_ID`) — nothing in the tool
registry needs more than `.author.id`/`.channel.id` except one line in
`tools/calendar.py`, which is why `.author` is still a real fetched Discord
user rather than a fake one. A regex fast path matches simple "turn on/off
X" commands straight to `set_plug_power`, skipping the LLM/OpenRouter round
trip that the fallback path (everything else, same as a DM) still pays for.
Every reply is DMed to the owner via `notify.send_dm()` regardless of
whether local TTS (`voice/tts.py`, piper) is configured or succeeds — that
DM is the durable record, TTS is best-effort.

## Host setup

`sudo scripts/install_services.sh` is the whole host-side install: systemd
units, the journald drop-in, and the tailnet resolver. It's idempotent —
re-run it after editing anything it installs. `.env.example` is the
authoritative inventory of settings; `bot/config.py` and `voice/config.py`
hold the defaults for everything absent from it.

The bare-name shortcuts (`http://llm`, `http://cigboard`, ...) come from
`scripts/dnsmasq/status.conf`. Edit it **there**, not in `/etc/dnsmasq.d/` —
it lived only on the host for months, hand-edited, and that is precisely how
it drifted into taking DNS down. Two traps, both now guarded by
`tests/test_dnsmasq_config.py`:

- It must say `bind-dynamic`, never `bind-interfaces`. tailscaled's unit goes
  active the moment the daemon starts, long before it has put an IPv4 address
  on `tailscale0`; `bind-interfaces` snapshots addresses once at startup, so
  dnsmasq binds nothing on the tailnet and — because that isn't an *error* —
  stays `active (running)` with `Restart=on-failure` never firing. Every
  device accepting Tailscale DNS then loses not just the shortcuts but all
  ordinary internet lookups, since this resolver forwards those too.
- Never leave a file in `/etc/dnsmasq.d/` that isn't `*.conf`. The unit's
  ExecStart passes `-7 /etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new`, so a
  `status.conf.bak-*` sitting next to the real file is *loaded as config*, not
  ignored. The installer now sweeps strays into `/etc/dnsmasq.d.backups/`.

The records are templated on `__TAILSCALE_IP__` and rendered at install time
from `tailscale ip -4`, because that address differs on every machine.

`scripts/check_startup.sh` probes the resolver rather than trusting
`systemctl is-active`, which reported healthy throughout the outage above.

`scripts/setup_tailscale_serve.sh` publishes the dashboards over Tailscale
Serve, replacing the hand-rolled nginx vhost and its cert-renew timer. Serve
does paths only (`/llm/`); it can't do bare names, since no cert covers them.

## Dashboards

`bot/static/llm.html` and `cigboard/static/cigboard.html`, loaded via
`webserver.page()`. Edit the HTML there, not inside the Python.
