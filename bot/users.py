"""
Who the people using this server are.

Nine different stores key data by Discord user id, and until this module
none of them owned the *identity* behind that id — so "what is this person
called?" had four separate implementations: discord_client.display_name()
off a live object, tools/groceries.py snapshotting a name into every item,
metrics snapshotting one into every call, and cigboard/discord_users.py
fetching names back out of Discord's REST API from inside the very process
that already had them.

A profile holds identity and settings. It deliberately does NOT hold domain
data — todos, groceries, cigarettes and reminders stay in their own stores,
keyed by id. Folding those in here would mean rewriting one large file on
every list operation, which is exactly the write-amplification problem that
made the metrics file worth moving to SQLite.

touch() is the write path: app.py calls it once per incoming message, in the
same place it already resolves roles, where a live discord.Member is
guaranteed to be in hand. Everything else reads. That beats reading the
gateway cache directly, too — the bot runs on Intents.default() without the
Members intent, so client.get_user() misses anyone who hasn't spoken
recently, while a persisted snapshot survives restarts, cold caches, and the
user leaving the server entirely.

The file holds geofence webhook secrets, so it's written 0600 like
calendar_tokens.json.
"""

import hmac
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime

from . import config, store
from .store import key

log = logging.getLogger("discord-llm-bot.users")

# How stale last_seen may get before touch() bothers writing. Without this,
# a profile write would happen on literally every message — the same mistake
# the metrics file makes. A name change still writes immediately.
_LAST_SEEN_WRITE_INTERVAL_S = 300.0

_profiles = store.user_store(config.PROFILES_FILE, dict, mode=0o600, label="profiles.json")

# Whole-file cache. Single process, and this module is the only writer, so
# it's invalidated on write rather than polled. Guarded because tool
# handlers read it from worker threads while app.py writes from the loop.
_cache: dict | None = None
_cache_lock = threading.Lock()


@dataclass
class Profile:
    user_id: int
    display_name: str
    avatar_url: str | None = None
    # None means "use the server default" (config.TIMEZONE) rather than
    # "UTC" — a resident in another timezone can override just themselves.
    timezone: str | None = None
    # Per-phone webhook secret for location reminders. Per-person rather
    # than one shared household secret is what lets one resident's phone
    # arriving only ever fire their own reminders (see geofence_server.py).
    geofence_secret: str | None = None
    first_seen: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_seen: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_dict(cls, user_id: int, raw: dict) -> "Profile":
        known = {f.name for f in fields(cls)} - {"user_id"}
        return cls(user_id=user_id, **{k: v for k, v in raw.items() if k in known})

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("user_id")  # it's the key; storing it twice invites drift
        return data

    @property
    def tz(self):
        """This user's timezone, falling back to the server default."""
        if self.timezone:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(self.timezone)
            except Exception:
                log.warning("Bad timezone %r on profile %s, using server default", self.timezone, self.user_id)
        return config.LOCAL_TZ


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _load() -> dict:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _profiles.all()
        return _cache


def _invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _save(user_id: int, profile: Profile) -> None:
    _profiles.set(user_id, profile.to_dict())
    _invalidate()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def find(user_id: int) -> Profile | None:
    """This user's stored profile, or None if they've never been seen."""
    raw = _load().get(key(user_id))
    if raw is None:
        return None
    try:
        return Profile.from_dict(user_id, raw)
    except TypeError:
        log.warning("Unreadable profile for %s, ignoring", user_id)
        return None


def get(user_id: int) -> Profile:
    """This user's profile, synthesizing an unsaved placeholder if they have
    none. Never raises and never returns None, so callers rendering a name
    don't each need their own fallback."""
    return find(user_id) or Profile(user_id=user_id, display_name=f"User {str(user_id)[-4:]}")


def name(user_id: int) -> str:
    """The one display-name answer. Replaces the four that existed before."""
    return get(user_id).display_name


def all() -> list[Profile]:
    """Every known resident, most recently seen first."""
    profiles = []
    for raw_key, raw in _load().items():
        try:
            profiles.append(Profile.from_dict(int(raw_key), raw))
        except (TypeError, ValueError):
            log.warning("Skipping unreadable profile entry %r", raw_key)
    profiles.sort(key=lambda p: p.last_seen, reverse=True)
    return profiles


def known_ids() -> set[int]:
    """Everyone with a profile, plus anyone who has data in some other store
    but was never seen speaking (an id added by an admin, say)."""
    return {p.user_id for p in all()} | store.known_user_ids()


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


def _display_name_of(user) -> str:
    return getattr(user, "display_name", None) or str(user)


def _avatar_url_of(user) -> str | None:
    avatar = getattr(user, "display_avatar", None)
    return str(avatar.url) if avatar is not None else None


def touch(user) -> Profile:
    """Record that we just saw this Discord user, refreshing their name and
    avatar. Called once per incoming message from app.py's _route().

    Only writes when something actually changed or last_seen has gone stale,
    so an active conversation doesn't rewrite the file per message."""
    user_id = user.id
    display_name = _display_name_of(user)
    avatar_url = _avatar_url_of(user)
    now = datetime.now(UTC)

    existing = find(user_id)
    if existing is None:
        profile = Profile(
            user_id=user_id,
            display_name=display_name,
            avatar_url=avatar_url,
            first_seen=now.isoformat(),
            last_seen=now.isoformat(),
        )
        _save(user_id, profile)
        log.info("New profile for %s (%s)", display_name, user_id)
        return profile

    changed = existing.display_name != display_name or existing.avatar_url != avatar_url
    if not changed and not _last_seen_stale(existing, now):
        return existing

    if existing.display_name != display_name:
        log.info("User %s renamed: %r -> %r", user_id, existing.display_name, display_name)
    existing.display_name = display_name
    existing.avatar_url = avatar_url
    existing.last_seen = now.isoformat()
    _save(user_id, existing)
    return existing


def _last_seen_stale(profile: Profile, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(profile.last_seen)
    except (TypeError, ValueError):
        return True
    return (now - last).total_seconds() >= _LAST_SEEN_WRITE_INTERVAL_S


def update(user_id: int, **changes) -> Profile:
    """Set profile fields for a user, creating the profile if needed."""
    profile = get(user_id)
    for name_, value in changes.items():
        if not hasattr(profile, name_):
            raise AttributeError(f"Profile has no field {name_!r}")
        setattr(profile, name_, value)
    _save(user_id, profile)
    return profile


def forget(user_id: int) -> list[str]:
    """Erase this person from every registered store, profile included —
    a housemate moving out, or the test id nobody cleaned up. Returns the
    labels of the stores that actually held something."""
    erased = store.forget_everywhere(user_id)
    _invalidate()
    log.info("Erased user %s from: %s", user_id, ", ".join(erased) or "(nothing)")
    return erased


# ---------------------------------------------------------------------------
# Geofence secrets
# ---------------------------------------------------------------------------


def geofence_secret(user_id: int) -> str | None:
    return get(user_id).geofence_secret


def ensure_geofence_secret(user_id: int) -> tuple[str, bool]:
    """This user's webhook secret, generating and storing one if they don't
    have it yet. Returns (secret, created)."""
    existing = find(user_id)
    if existing is not None and existing.geofence_secret:
        return existing.geofence_secret, False
    secret = secrets.token_urlsafe(24)
    update(user_id, geofence_secret=secret)
    return secret, True


def by_geofence_secret(secret: str) -> Profile | None:
    """Which resident this webhook secret belongs to, or None.

    Checks every profile rather than stopping at the first match, so
    response timing can't be used to narrow down which secret (if any) is
    close to correct. Compares as bytes: hmac.compare_digest raises
    TypeError on str inputs containing non-ASCII, which an arbitrary query
    string can easily have.
    """
    candidate = secret.encode() if isinstance(secret, str) else bytes(secret)
    matched = None
    for profile in all():
        if not profile.geofence_secret:
            continue
        if hmac.compare_digest(candidate, profile.geofence_secret.encode()):
            matched = profile
    return matched


def any_geofence_users() -> bool:
    return any(p.geofence_secret for p in all())


async def backfill_from_discord() -> int:
    """Give a profile to anyone who has data in some store but has not
    messaged the bot since profiles existed.

    Without this there's a cold-start gap: the leaderboard and the shared
    grocery list would show "User 5224" for a housemate until the next time
    they happened to say something. Runs once at startup over a handful of
    ids, using the same REST lookup cigboard used to do on every poll —
    except now the answer is kept.
    """
    from .discord_client import client  # deferred: this module stays import-safe without a gateway

    missing = known_ids() - {p.user_id for p in all()}
    filled = 0
    for user_id in sorted(missing):
        try:
            user = await client.fetch_user(user_id)
        except Exception:
            log.warning("Could not look up user %s for a profile backfill", user_id, exc_info=True)
            continue
        _save(
            user_id,
            Profile(
                user_id=user_id,
                display_name=_display_name_of(user),
                avatar_url=_avatar_url_of(user),
            ),
        )
        filled += 1
    if filled:
        log.info("Backfilled %d profile(s) from Discord", filled)
    return filled


def seed_geofence_from_env() -> int:
    """One-time migration: copy any secrets still defined in .env's
    GEOFENCE_USERS into the profiles that lack one, so phones registered
    before this store existed keep working with no re-registration.

    Runs at startup and is idempotent — once every secret has a home in
    profiles.json, GEOFENCE_USERS can be deleted from .env.
    """
    seeded = 0
    for secret, user_id in config.GEOFENCE_USERS.items():
        profile = find(user_id)
        if profile is not None and profile.geofence_secret:
            continue
        update(user_id, geofence_secret=secret)
        seeded += 1
    if seeded:
        log.info(
            "Seeded %d geofence secret(s) from .env into %s — GEOFENCE_USERS can now be "
            "removed from .env",
            seeded,
            config.PROFILES_FILE,
        )
    return seeded
