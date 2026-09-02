# home_server

## Discord bot (`bot/`)

- The active OpenRouter model is set by `OPENROUTER_MODEL` in `.env` (loaded via
  `EnvironmentFile=` in the `discord-llm-bot.service` systemd unit). The default in
  `bot/config.py` (`OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", ...)`) is just a
  fallback and does not reflect what's actually running — check `.env`, not `config.py`, to
  find the current model.
- Bot logs: `sudo journalctl -u discord-llm-bot.service`.
- Deploys via `!deploy` restart `sudo systemctl restart discord-llm-bot`.
