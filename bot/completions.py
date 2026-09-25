"""
One place that talks to a chat-completions endpoint.

Either call path (ordinary chat, the moderation classifier) can be pointed at
an OpenAI-compatible server on the tailnet instead of OpenRouter — see
config.LLM_API_BASE / MODERATION_API_BASE. A box under a desk is not a
datacenter: it sleeps on idle, it reboots, it gets unplugged. So a call whose
configured endpoint doesn't answer falls back to OpenRouter rather than
failing, which is what makes "serve this from my own hardware" a safe default
instead of a reliability downgrade.

The fallback is deliberately narrow. A refused connection, a timeout, or a 5xx
means "this endpoint isn't there"; a 400 means the request itself is malformed
and would fail the same way upstream, so it raises instead of spending money to
rediscover that.

The model slug travels with the endpoint, never with the payload — a local
server's "qwen2.5:7b" is not a slug OpenRouter would accept, so falling back
has to swap the model as well as the host.
"""

import logging
import time
from dataclasses import dataclass

import requests

from . import config

log = logging.getLogger("discord-llm-bot.completions")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Statuses meaning "not serving right now" rather than "your request is wrong".
_UNAVAILABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Failures meaning nothing is listening, as opposed to a dropped read mid-reply.
_NOT_THERE = (requests.ConnectionError, requests.ConnectTimeout)

_session = requests.Session()

# A sleeping PC on the tailnet doesn't refuse a connection, it just never
# answers — so without this, the full read timeout elapses before falling
# back. An awake server accepts in milliseconds; only the read deserves to be
# long, since the first request after a wake may wait on a cold model load.
LOCAL_CONNECT_TIMEOUT_S = 3

# Once a local endpoint fails to answer, go straight to the fallback for this
# long. A turn can make several calls (tool loops, plus moderation), and each
# would otherwise pay the connect timeout again while the box sleeps.
LOCAL_COOLDOWN_S = 60

# endpoint base -> time.monotonic() before which it's presumed down.
_down_until: dict[str, float] = {}


@dataclass(frozen=True)
class Endpoint:
    base: str
    key: str
    model: str
    title: str

    @property
    def is_openrouter(self) -> bool:
        return self.base == OPENROUTER_BASE

    @property
    def metrics_source(self) -> str:
        """What metrics.record() should attribute the call to. A local call
        costs nothing, and filing it under "openrouter" would quietly inflate
        the spend figures on the /llm/ dashboard."""
        return "openrouter" if self.is_openrouter else "local"


class Unavailable(Exception):
    """The endpoint didn't answer. Carries the underlying requests error so a
    caller with nowhere to fall back to can re-raise the real thing."""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


def openrouter_endpoint(model: str, title: str) -> Endpoint:
    return Endpoint(OPENROUTER_BASE, config.OPENROUTER_API_KEY, model, title)


def post(
    payload: dict,
    endpoint: Endpoint,
    *,
    timeout: int,
    attempts: int = 1,
    backoff_s: float = 1.5,
    fallback: Endpoint | None = None,
) -> tuple[requests.Response, Endpoint]:
    """Returns the response and the endpoint that actually served it."""
    can_fall_back = fallback is not None and fallback.base != endpoint.base
    if can_fall_back and _down_until.get(endpoint.base, 0) > time.monotonic():
        return _attempt(payload, fallback, timeout, attempts, backoff_s), fallback
    try:
        return _attempt(payload, endpoint, timeout, attempts, backoff_s), endpoint
    except Unavailable as exc:
        if not endpoint.is_openrouter:
            _down_until[endpoint.base] = time.monotonic() + LOCAL_COOLDOWN_S
        if not can_fall_back:
            raise exc.cause
        log.warning(
            "%s did not answer (%s); falling back to %s with model %s",
            endpoint.base, exc, fallback.base, fallback.model,
        )
        try:
            return _attempt(payload, fallback, timeout, attempts, backoff_s), fallback
        except Unavailable as exc2:
            raise exc2.cause


def _attempt(
    payload: dict, endpoint: Endpoint, timeout: int, attempts: int, backoff_s: float
) -> requests.Response:
    body = dict(payload, model=endpoint.model)
    headers = {
        "Authorization": f"Bearer {endpoint.key}",
        "Content-Type": "application/json",
        # Optional but recommended by OpenRouter for attribution/rate-limit
        # purposes; local servers ignore it.
        "X-Title": endpoint.title,
    }
    if not endpoint.is_openrouter:
        timeout = (LOCAL_CONNECT_TIMEOUT_S, timeout)
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            resp = _session.post(
                f"{endpoint.base}/chat/completions", headers=headers, json=body, timeout=timeout
            )
        except requests.RequestException as exc:
            # Retrying a local box that didn't even accept the connection only
            # doubles the wait before falling back.
            if last or (not endpoint.is_openrouter and isinstance(exc, _NOT_THERE)):
                raise Unavailable(exc) from exc
            log.warning("%s request failed (attempt %d), retrying", endpoint.base, attempt, exc_info=True)
        else:
            if resp.status_code in _UNAVAILABLE_STATUSES and not last:
                log.warning(
                    "%s returned HTTP %s (attempt %d), retrying", endpoint.base, resp.status_code, attempt
                )
            else:
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    if resp.status_code in _UNAVAILABLE_STATUSES:
                        raise Unavailable(exc) from exc
                    raise
                return resp
        time.sleep(backoff_s * attempt)
    raise AssertionError("unreachable")  # the final attempt always returns or raises
