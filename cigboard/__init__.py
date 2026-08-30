"""
Cigarette leaderboard — a small local-only web dashboard over the same
cigarettes.json that bot/tools/cigarettes.py writes to.

See cigboard/server.py for the entrypoint (started from bot/app.py the same
way bot/llm_status_server.py is), cigboard/leaderboard.py for the stats
computation, and cigboard/discord_users.py for resolving Discord ids to
names/avatars for display.
"""
