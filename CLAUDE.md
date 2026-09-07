# home_server

## Discord bot (`bot/`)

- The active OpenRouter model is set by `OPENROUTER_MODEL` in `.env` (loaded via
  `EnvironmentFile=` in the `discord-llm-bot.service` systemd unit). The default in
  `bot/config.py` (`OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", ...)`) is just a
  fallback and does not reflect what's actually running — check `.env`, not `config.py`, to
  find the current model.
- Bot logs: `sudo journalctl -u discord-llm-bot.service`.
- Deploys via `!deploy` restart `sudo systemctl restart discord-llm-bot`.

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

## Dashboards

`bot/static/llm.html` and `cigboard/static/cigboard.html`, loaded via
`webserver.page()`. Edit the HTML there, not inside the Python.
