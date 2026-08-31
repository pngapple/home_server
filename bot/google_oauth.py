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
import secrets
import threading
import time

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from . import config, jsonstore

log = logging.getLogger("discord-llm-bot.google_oauth")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# state -> (discord_user_id, code_verifier, created_at monotonic). Short-lived,
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
        _PENDING_STATES[state] = (discord_user_id, flow.code_verifier, time.monotonic())
    return auth_url


def _prune_expired_states() -> None:
    """Caller must hold _PENDING_LOCK."""
    now = time.monotonic()
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
    return jsonstore.read(config.GOOGLE_CALENDAR_TOKENS_FILE, {})


# These are live Google refresh tokens: create the file 0600 up front rather
# than writing at the default umask and chmod-ing after, which leaves a
# window where anyone on the box can read it.
def _tokens():
    """Read-modify-write context manager over the token file."""
    return jsonstore.update(config.GOOGLE_CALENDAR_TOKENS_FILE, {}, mode=0o600)


def _save_credentials(discord_user_id: int, creds: Credentials) -> None:
    with _tokens() as all_tokens:
        all_tokens[str(discord_user_id)] = json.loads(creds.to_json())


def _forget(discord_user_id: int) -> None:
    with _tokens() as all_tokens:
        all_tokens.pop(str(discord_user_id), None)


def get_credentials(discord_user_id: int) -> Credentials | None:
    """Usable credentials for this user, or None if they never linked an
    account or their grant no longer works (revoked in Google's account
    settings, refresh token expired). A dead grant is dropped so the caller's
    "not connected, send them a link" path takes over instead of the same
    doomed refresh being retried on every message."""
    raw = _load_all().get(str(discord_user_id))
    if raw is None:
        return None

    creds = Credentials.from_authorized_user_info(raw, SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except GoogleAuthError:
            log.warning("Dropping unusable Google credentials for user %s", discord_user_id, exc_info=True)
            _forget(discord_user_id)
            return None
        _save_credentials(discord_user_id, creds)
    return creds


def is_connected(discord_user_id: int) -> bool:
    return str(discord_user_id) in _load_all()
