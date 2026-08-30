"""
Per-Discord-user Google Calendar OAuth.

Each Discord user links their own Google account. Tokens are stored as a
flat JSON dict keyed by Discord user id:

  {"123456789012345678": {"token": "...", "refresh_token": "...", ...}, ...}

Not a database, on purpose — same reasoning as tools/reminders.py: this is a
handful of personal links, not a workload.
"""

import json
import logging
import os
import secrets
import threading
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from . import config, jsonstore

log = logging.getLogger("discord-llm-bot.google_oauth")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# state -> (discord_user_id, code_verifier, created_at). Short-lived,
# in-memory only: if the bot restarts mid-flow the user just asks to connect
# again. code_verifier must be carried from the authorize step to the
# exchange step (PKCE) since each uses its own Flow instance.
# Guarded by a lock: the authorize step runs in a tool worker thread while
# the callback runs off the aiohttp server (see oauth_server.py).
_PENDING_STATES: dict[str, tuple[int, str, float]] = {}
_PENDING_LOCK = threading.Lock()
_STATE_TTL_SECONDS = 600


def _new_flow() -> Flow:
    return Flow.from_client_secrets_file(
        config.GOOGLE_CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=config.GOOGLE_REDIRECT_URI,
    )


def build_authorize_url(discord_user_id: int) -> str:
    state = secrets.token_urlsafe(24)
    flow = _new_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # forces a refresh_token even on repeat authorizations
        state=state,
    )
    # authorization_url() generates flow.code_verifier (PKCE) as a side
    # effect; it must be replayed on the Flow instance used to exchange the
    # code below, since that's a separate object.
    with _PENDING_LOCK:
        _prune_expired_states()
        _PENDING_STATES[state] = (discord_user_id, flow.code_verifier, time.time())
    return auth_url


def _prune_expired_states() -> None:
    """Caller must hold _PENDING_LOCK."""
    now = time.time()
    expired = [s for s, (_, _, created) in _PENDING_STATES.items() if now - created > _STATE_TTL_SECONDS]
    for s in expired:
        del _PENDING_STATES[s]


def exchange_code(state: str, code: str) -> int | None:
    """Completes the flow for the given state/code, persists the resulting
    credentials, and returns the Discord user id they belong to (or None if
    the state is unknown/expired)."""
    with _PENDING_LOCK:
        _prune_expired_states()
        pending = _PENDING_STATES.pop(state, None)
    if pending is None:
        return None
    discord_user_id, code_verifier, _ = pending

    flow = _new_flow()
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    _save_credentials(discord_user_id, flow.credentials)
    return discord_user_id


def _load_all() -> dict:
    if not os.path.exists(config.GOOGLE_CALENDAR_TOKENS_FILE):
        return {}
    try:
        with open(config.GOOGLE_CALENDAR_TOKENS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Failed to read %s, treating as empty", config.GOOGLE_CALENDAR_TOKENS_FILE)
        return {}


def _save_all(data: dict) -> None:
    # These are live Google refresh tokens: create the file 0600 up front
    # rather than writing at the default umask and chmod-ing after, which
    # leaves a window where anyone on the box can read it.
    jsonstore.write(config.GOOGLE_CALENDAR_TOKENS_FILE, data, mode=0o600)


def _save_credentials(discord_user_id: int, creds: Credentials) -> None:
    with jsonstore.lock(config.GOOGLE_CALENDAR_TOKENS_FILE):
        all_tokens = _load_all()
        all_tokens[str(discord_user_id)] = json.loads(creds.to_json())
        _save_all(all_tokens)


def get_credentials(discord_user_id: int) -> Credentials | None:
    all_tokens = _load_all()
    raw = all_tokens.get(str(discord_user_id))
    if raw is None:
        return None

    creds = Credentials.from_authorized_user_info(raw, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(discord_user_id, creds)
    return creds


def is_connected(discord_user_id: int) -> bool:
    return str(discord_user_id) in _load_all()
