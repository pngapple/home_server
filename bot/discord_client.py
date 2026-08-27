"""
The shared discord.Client instance.

Split out from bot.py so tool modules (e.g. tools/reminders.py) can send
messages/DMs (fetch_user, .send(...)) without importing bot.py and creating
a circular import.
"""

import discord

intents = discord.Intents.default()
intents.message_content = True  # required to read message text; enable this
                                  # "Privileged Gateway Intent" in the Discord
                                  # Developer Portal too (see SETUP_GUIDE.md)

client = discord.Client(intents=intents)
