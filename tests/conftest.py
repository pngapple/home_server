"""
Shared test setup.

bot.config reads every setting into a module-level constant at import time,
and the stores bind their file paths at import too (tools/todos.py's
`TODOS = lists.ListStore(config.TODOS_FILE)` runs on import). So the
environment has to be redirected into a scratch directory *before* anything
under bot/ is imported — hence the module-level block below rather than a
fixture. conftest.py is imported before any test module, which is what makes
that ordering work.

Setting os.environ directly (rather than via a fixture) also shadows the
real .env: config.py calls load_dotenv(), which by design does not override
variables that are already set, so these win and the suite never reads the
live credentials or touches the live JSON files.
"""

import os
import shutil
import sys
import tempfile
import types

DATA_DIR = tempfile.mkdtemp(prefix="home-server-tests-")

os.environ.update(
    {
        # Required by config._required(); never used, nothing connects.
        "DISCORD_BOT_TOKEN": "test-token",
        "OPENROUTER_API_KEY": "test-key",
        # Every store, pointed at the scratch directory.
        "PROFILES_FILE": f"{DATA_DIR}/profiles.json",
        "REMINDERS_FILE": f"{DATA_DIR}/reminders.json",
        "CIGARETTES_FILE": f"{DATA_DIR}/cigarettes.json",
        "TODOS_FILE": f"{DATA_DIR}/todos.json",
        "GROCERIES_FILE": f"{DATA_DIR}/groceries.json",
        "GOOGLE_CALENDAR_TOKENS_FILE": f"{DATA_DIR}/calendar_tokens.json",
        "MODERATION_STRIKES_FILE": f"{DATA_DIR}/moderation.json",
        "GEOFENCE_STATE_FILE": f"{DATA_DIR}/geofence_state.json",
        "LESSONS_FILE": f"{DATA_DIR}/lessons.json",
        "METRICS_DB": f"{DATA_DIR}/metrics.db",
        # Pin the values the permission tests assert against, so they don't
        # depend on whatever the live .env happens to say.
        "CLAUDE_CODE_OWNER_ID": "1111",
        "ADMIN_ROLE_NAME": "Administrator",
        "HOUSEHOLD_ROLE_NAME": "Home Resident",
        "TIMEZONE": "America/Indiana/Indianapolis",
        # Blank by default so tests never inherit the live secret from
        # .env — bot/config.py's load_dotenv() only fills in vars that
        # aren't already set, so an explicit blank here wins either way.
        # Individual tests monkeypatch config.VOICE_SERVER_SECRET as needed.
        "VOICE_SERVER_SECRET": "",
    }
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_data_dir():
    """Each test starts with no store files at all. The stores treat a
    missing file as empty (see jsonstore.read), so deleting is enough —
    there's nothing to re-create."""
    yield
    for name in os.listdir(DATA_DIR):
        path = os.path.join(DATA_DIR, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)


@pytest.fixture(autouse=True)
def reset_metrics_db():
    """Drop the SQLite connection between tests so each one gets a fresh
    database rather than inheriting rows through the process-wide handle in
    db.connect()."""
    yield
    from bot import db

    db.close()


def make_message(user_id: int = 42, channel_id: int = 99, content: str = "", name: str = "Tester"):
    """A stand-in for discord.Message.

    ToolContext is a plain dataclass with no runtime type checking, and tool
    handlers only ever reach for author.id, channel.id and display_name — so
    a namespace with those is a faithful enough double, and avoids
    constructing a real Message (which needs a connection state object)."""
    author = types.SimpleNamespace(id=user_id, display_name=name, bot=False)
    channel = types.SimpleNamespace(id=channel_id)
    return types.SimpleNamespace(
        id=channel_id * 1000 + user_id, author=author, channel=channel, content=content, guild=None
    )


@pytest.fixture
def message():
    return make_message()


@pytest.fixture
def ctx(message):
    from bot.tools import ToolContext

    return ToolContext(message=message, roles=frozenset({"Home Resident"}))


@pytest.fixture
def registry():
    """Register throwaway tools without leaking them into other tests."""
    from bot import tools

    before = dict(tools._REGISTRY)
    yield tools
    tools._REGISTRY.clear()
    tools._REGISTRY.update(before)


@pytest.fixture(autouse=True)
def clear_llm_history():
    """llm._history is a process-wide LRU keyed by (channel, user); without
    this, one test's conversation leaks into the next one's prompt."""
    yield
    from bot import llm

    llm._history.clear()
