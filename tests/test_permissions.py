"""is_admin — the gate in front of !code, !deploy and every owner_only tool."""

from bot import permissions

OWNER_ID = 1111  # set in conftest.py


def test_the_legacy_owner_id_is_always_an_admin():
    assert permissions.is_admin(OWNER_ID, frozenset())


def test_the_admin_role_grants_admin():
    assert permissions.is_admin(2222, frozenset({"Administrator"}))


def test_other_roles_do_not_grant_admin():
    assert not permissions.is_admin(2222, frozenset({"Home Resident"}))


def test_no_roles_and_not_the_owner_is_not_admin():
    assert not permissions.is_admin(2222, frozenset())
