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
