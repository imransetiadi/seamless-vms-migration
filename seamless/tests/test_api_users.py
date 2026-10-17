from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Role
from seamless_migrate.security.auth import DashboardUser, hash_token
from seamless_migrate.store import Store, events
from tests.api_support import Api, api_settings


@pytest.fixture
def api(tmp_path, any_store):
    settings, tokens = api_settings(tmp_path)
    with TestClient(create_app(settings, any_store)) as client:
        yield Api(client, any_store, settings, tokens)


@pytest.mark.parametrize("role", list(Role))
def test_created_user_can_sign_in_with_its_role_and_survives_restart(api, role):
    original = api.settings.tokens_file.read_bytes()
    response = api.post("/api/v1/users", json={"name": "New.User", "role": role})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["name"] == "New.User" and created["role"] == role
    assert created["token"].startswith("smg_") and len(created["token"]) == 47
    assert response.headers["cache-control"] == "no-store"
    headers = {"Authorization": f"Bearer {created['token']}"}
    identity = {"name": "New.User", "role": role}
    assert api.client.get("/api/v1/me", headers=headers).json() == identity
    users = api.get("/api/v1/users", Role.admin)
    assert identity in users.json()
    assert all(set(user) == {"name", "role"} for user in users.json())
    assert users.headers["cache-control"] == "no-store"
    assert api.client.get("/api/v1/users", headers=headers).status_code == (
        200 if role == Role.admin else 403
    )
    assert api.settings.tokens_file.read_bytes() == original
    stored = api.store.get("user", "new.user", DashboardUser)
    assert stored.sha256 == hash_token(created["token"])
    assert created["token"] not in stored.model_dump_json()
    audit = api.store.events(kinds=["user.created"])
    assert len(audit) == 1 and audit[0].actor == "admin-user"
    assert audit[0].data == {"name": "New.User", "role": role}
    assert created["token"] not in str(audit) and stored.sha256 not in str(audit)
    # A fresh connection and app read the durable identity, without an in-process token cache.
    other_store = Store(api.store.url)
    try:
        with TestClient(create_app(api.settings, other_store)) as client:
            assert client.get("/api/v1/me", headers=headers).json() == identity
        api.store.delete("user", "new.user")
        assert api.client.get("/api/v1/me", headers=headers).status_code == 401
    finally:
        other_store.dispose()


@pytest.mark.parametrize("role", [None, Role.viewer, Role.operator, Role.approver])
def test_only_admin_can_list_or_add_users(api, role):
    expected = 401 if role is None else 403
    assert api.get("/api/v1/users", role).status_code == expected
    assert api.post("/api/v1/users", role, json={"name": "alice"}).status_code == expected
    assert api.store.list("user", DashboardUser) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 129},
        {"name": "System"},
        {"name": " anonymous "},
        {"name": "cli"},
        {"name": "unauthenticated"},
        {"name": "user:alice"},
        {"name": "a\nb"},
        {"name": "has space"},
        {"name": "../alice"},
        {"name": "alice", "role": "owner"},
        {"name": "alice", "token": "chosen-by-client"},
        {"name": 123},
    ],
)
def test_invalid_user_is_not_created(api, body):
    assert api.post("/api/v1/users", json=body).status_code == 422
    assert api.store.list("user", DashboardUser) == []


def test_duplicate_names_do_not_overwrite_existing_access(api):
    assert api.post("/api/v1/users", json={"name": "ADMIN-user"}).status_code == 409
    created = api.post("/api/v1/users", json={"name": " alice "}).json()
    assert created["name"] == "alice" and created["role"] == "viewer"
    assert api.post("/api/v1/users", json={"name": "ALICE", "role": "admin"}).status_code == 409
    headers = {"Authorization": f"Bearer {created['token']}"}
    assert api.client.get("/api/v1/me", headers=headers).json() == {
        "name": "alice",
        "role": "viewer",
    }
    assert len(api.store.events(kinds=["user.created"])) == 1


def test_concurrent_creates_have_one_winner(api):
    def create(_):
        return api.post("/api/v1/users", json={"name": "alice"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(api.store.list("user", DashboardUser)) == 1
    assert len(api.store.events(kinds=["user.created"])) == 1


def test_failed_audit_rolls_back_user_creation(api):
    def refuse_audit(conn, clause, multiparams, params, execution_options):
        if getattr(clause, "is_insert", False) and clause.table is events:
            raise RuntimeError("audit unavailable")

    sa.event.listen(api.store.engine, "before_execute", refuse_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            api.post("/api/v1/users", json={"name": "alice"})
    finally:
        sa.event.remove(api.store.engine, "before_execute", refuse_audit)
    assert api.store.list("user", DashboardUser) == []
    assert api.post("/api/v1/users", json={"name": "alice"}).status_code == 201


def test_auth_disabled_cannot_issue_real_access(tmp_path, store):
    settings, _ = api_settings(tmp_path, auth_disabled=True)
    # an auth-off server is reached by a loopback name only (SDD §13.1, audit AP-03)
    with TestClient(create_app(settings, store), base_url="http://127.0.0.1:8080") as client:
        response = client.post("/api/v1/users", json={"name": "alice"})
        assert response.status_code == 409
        assert "enable authentication" in response.json()["error"]["message"]
    assert store.list("user", DashboardUser) == []
