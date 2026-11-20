"""The hook catalog and a plan's references to it (SDD §4.2, §4.3, §12, §23.3, plan task N3)."""

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import PlanStatus, Role
from seamless_migrate.domain.models import Plan
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings

#: a webhook hook's bearer token: write-only (SDD §12, §13.3)
TOKEN = "canary-" * 5 + "hook"

PLAYBOOK = {
    "name": "drain-lb",
    "kind": "playbook",
    "playbook": "/opt/seamless/hooks/drain-lb.yml",
    "timeout_s": 120,
}
WEBHOOK = {
    "name": "dns-swap",
    "kind": "webhook",
    "url": "https://hooks.example.org/dns",
    "secret": TOKEN,
}
PLAN = {
    "name": "Network",
    "source_provider_id": "src-osp",
    "destination_provider_id": "dst-rhoso",
    "vm_ids": ["vm-1"],
}


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path, secrets_dir=tmp_path / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
        assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
        yield api
    store.dispose()


def patch(api, path, body, role=Role.admin):
    return api.client.patch(f"/api/v1{path}", headers=api.h(role), json=body)


def delete(api, path, role=Role.admin):
    return api.client.delete(f"/api/v1{path}", headers=api.h(role))


def register(api, body):
    created = api.post("/api/v1/hooks", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def message(response):
    return response.json()["error"]["message"]


def hook_events(api):
    return [e for e in api.store.events(since_seq=0, limit=100000) if e.kind.startswith("hook.")]


def test_only_an_admin_registers_a_hook(api):
    """SDD §12, §23.3 (owner decision 2): a hook runs code with the control plane's reach, so only
    an admin registers, edits or deletes one; anyone may read the catalog. Each change is a hook.*
    event naming the hook and its kind - never its URL or its secret's name."""
    for role in (Role.viewer, Role.operator, Role.approver):
        assert api.post("/api/v1/hooks", role=role, json=WEBHOOK).status_code == 403
    assert api.get("/api/v1/hooks").json() == []
    created = register(api, WEBHOOK)
    assert {
        k: created[k] for k in ("name", "kind", "url", "secret_set", "timeout_s", "revert")
    } == {
        "name": "dns-swap",
        "kind": "webhook",
        "url": "https://hooks.example.org/dns",
        "secret_set": True,
        "timeout_s": 300,
        "revert": None,
    }
    assert "secret" not in created
    assert created["playbook"] is None and created["description"] is None
    assert created["created_at"] and created["updated_at"]
    register(api, PLAYBOOK)
    # anyone reads the catalog, by name
    assert [h["name"] for h in api.get("/api/v1/hooks").json()] == ["dns-swap", "drain-lb"]
    assert api.get("/api/v1/hooks/drain-lb").json()["timeout_s"] == 120
    assert api.get("/api/v1/hooks/nope").status_code == 404
    # the same name twice is a conflict
    assert api.post("/api/v1/hooks", json=WEBHOOK).status_code == 409
    for role in (Role.viewer, Role.operator, Role.approver):
        assert patch(api, "/hooks/drain-lb", {"timeout_s": 60}, role).status_code == 403
        assert delete(api, "/hooks/drain-lb", role).status_code == 403
    edited = patch(api, "/hooks/drain-lb", {"timeout_s": 60, "description": "Drain the pool"})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["timeout_s"], edited.json()["description"]) == (60, "Drain the pool")
    # name, kind and the server-owned fields are immutable
    for body in ({"name": "other"}, {"kind": "webhook"}, {"created_at": "2026-01-01T00:00:00Z"}):
        refused = patch(api, "/hooks/drain-lb", body)
        assert refused.status_code == 422 and "cannot be changed" in message(refused)
    assert delete(api, "/hooks/drain-lb").status_code == 204
    assert api.get("/api/v1/hooks/drain-lb").status_code == 404
    events = hook_events(api)
    assert [(e.kind, e.data, e.actor) for e in events] == [
        ("hook.created", {"name": "dns-swap", "kind": "webhook"}, "admin-user"),
        ("hook.created", {"name": "drain-lb", "kind": "playbook"}, "admin-user"),
        (
            "hook.updated",
            {"name": "drain-lb", "kind": "playbook", "fields": ["description", "timeout_s"]},
            "admin-user",
        ),
        ("hook.deleted", {"name": "drain-lb", "kind": "playbook"}, "admin-user"),
    ]
    assert not any("hooks.example.org" in e.message or TOKEN in e.message for e in events)


def test_a_webhook_hook_needs_https(api):
    """SDD §4.2, §12, §23.9: a webhook's URL is https:// without user information, and its bearer
    secret a token an Authorization header carries, which no refusal repeats; a playbook hook names
    an absolute .yml or .yaml path; each kind takes its own fields alone,
    a timeout of 1-1800 s, and a revert naming another hook of the catalog. A refused edit stores
    nothing."""
    cases = [
        ({**WEBHOOK, "url": "http://hooks.example.org/dns"}, "https"),
        ({**WEBHOOK, "url": "https://user:pw@hooks.example.org/dns"}, "user information"),
        ({**WEBHOOK, "url": "https://"}, "https"),
        ({k: v for k, v in WEBHOOK.items() if k != "url"}, "url"),
        ({**WEBHOOK, "playbook": "/opt/x.yml"}, "playbook"),
        ({**WEBHOOK, "secret": "a token"}, "secret"),
        ({**WEBHOOK, "secret": "tok\nen"}, "secret"),
        ({**WEBHOOK, "secret": ""}, "secret"),
        ({**WEBHOOK, "secret": 7}, "secret"),
        ({**WEBHOOK, "secret": "x" * 4097}, "secret"),
        ({**PLAYBOOK, "playbook": "hooks/drain.yml"}, "absolute"),
        ({**PLAYBOOK, "playbook": "/opt/hooks/drain.sh"}, ".yml"),
        ({k: v for k, v in PLAYBOOK.items() if k != "playbook"}, "playbook"),
        ({**PLAYBOOK, "url": "https://hooks.example.org/x"}, "url"),
        ({**PLAYBOOK, "secret": "dns-hook-token"}, "secret"),
        ({**PLAYBOOK, "timeout_s": 0}, "timeout_s"),
        ({**PLAYBOOK, "timeout_s": 1801}, "timeout_s"),
        ({**PLAYBOOK, "name": "Drain LB"}, "name"),
        ({**PLAYBOOK, "revert": "drain-lb"}, "revert"),
        ({**PLAYBOOK, "revert": "nope"}, "revert"),
        ({**PLAYBOOK, "colour": "blue"}, "colour"),
    ]
    for body, why in cases:
        refused = api.post("/api/v1/hooks", json=body)
        assert refused.status_code == 422, (body, refused.text)
        assert why in message(refused), (body, message(refused))
    assert api.get("/api/v1/hooks").json() == []
    # a refusal never repeats the token, and writes none
    refused = api.post("/api/v1/hooks", json={**WEBHOOK, "secret": "bad token"})
    assert "bad token" not in refused.text
    assert not (api.settings.secrets_dir / "hook-dns-swap").exists()
    register(api, WEBHOOK)
    register(api, {**PLAYBOOK, "revert": "dns-swap", "playbook": "/opt/hooks/drain.YAML"})
    refused = patch(api, "/hooks/dns-swap", {"url": "http://hooks.example.org/dns"})
    assert refused.status_code == 422 and "https" in message(refused)
    refused = patch(api, "/hooks/drain-lb", {"secret": TOKEN})
    assert refused.status_code == 422 and "takes none" in message(refused)
    kept = api.get("/api/v1/hooks/dns-swap", role=Role.admin).json()
    assert kept["url"] == "https://hooks.example.org/dns"
    assert patch(api, "/hooks/drain-lb", {"revert": "nope"}).status_code == 422
    assert patch(api, "/hooks/drain-lb", {"revert": None}).json()["revert"] is None


def test_a_plan_references_hooks_by_name_per_point(api):
    """SDD §4.2, §12, §23.3: a plan names catalog hooks per point in their run order, each once per
    point; a name the catalog lacks, a hook twice in a point, or a point §23.3 does not name is
    refused (422) and stores nothing - on a new plan as on an edit."""
    register(api, PLAYBOOK)
    register(api, WEBHOOK)
    settings = {
        "hooks": {"pre_stop": ["drain-lb"], "post_create": ["dns-swap", "drain-lb"]},
        "floating_ips": "keep_address",
        "dns_targets": {"vm-1": {"kind": "designate", "zone": "example.com.", "record": "web-01"}},
        "lb_pools": {"pool-src-1": "pool-dst-9"},
    }
    created = api.post(
        "/api/v1/plans", role=Role.operator, json={**PLAN, "network_cutover": settings}
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["network_cutover"]["hooks"] == settings["hooks"]
    assert plan["network_cutover"]["floating_ips"] == "keep_address"
    assert plan["network_cutover"]["dns_targets"]["vm-1"]["record"] == "web-01"
    assert (
        api.get(f"/api/v1/plans/{plan['id']}").json()["network_cutover"] == plan["network_cutover"]
    )
    refusals = [
        (
            {"hooks": {"pre_stop": ["nope"]}},
            "network_cutover.hooks.pre_stop: no hook 'nope' in the catalog",
        ),
        ({"hooks": {"pre_stop": ["drain-lb", "drain-lb"]}}, "drain-lb"),
        ({"hooks": {"pre_start": ["drain-lb"]}}, "pre_start"),
        (
            {"dns_targets": {"vm-1": {"kind": "rfc2136", "zone": "example.com.", "record": "web"}}},
            "server",
        ),
    ]
    for body, why in refusals:
        refused = api.post(
            "/api/v1/plans", role=Role.operator, json={**PLAN, "network_cutover": body}
        )
        assert refused.status_code == 422, (body, refused.text)
        assert why in message(refused), (body, message(refused))
        edited = patch(api, f"/plans/{plan['id']}", {"network_cutover": body}, Role.operator)
        assert edited.status_code == 422 and why in message(edited)
    assert len(api.get("/api/v1/plans").json()) == 1
    assert (
        api.get(f"/api/v1/plans/{plan['id']}").json()["network_cutover"] == plan["network_cutover"]
    )
    # an edit names other hooks of the catalog, or none
    moved = patch(
        api,
        f"/plans/{plan['id']}",
        {"network_cutover": {"hooks": {"on_finalize": ["dns-swap"]}}},
        Role.operator,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["network_cutover"]["hooks"] == {"on_finalize": ["dns-swap"]}
    assert moved.json()["network_cutover"]["floating_ips"] == "auto"


def test_a_hook_in_use_cannot_be_deleted(api):
    """SDD §12: a hook a plan names at a point - any plan but a cancelled one: a completed plan may
    still roll back or finalize -, or another hook names as its revert, cannot be deleted (409,
    each named); once nothing names it, it can."""
    register(api, PLAYBOOK)
    register(api, {**WEBHOOK, "revert": "drain-lb"})
    created = api.post(
        "/api/v1/plans",
        role=Role.operator,
        json={**PLAN, "network_cutover": {"hooks": {"on_rollback": ["dns-swap"]}}},
    )
    plan_id = created.json()["id"]
    used = delete(api, "/hooks/dns-swap")
    assert used.status_code == 409
    assert f"plan(s) Network ({plan_id})" in message(used)
    reverted = delete(api, "/hooks/drain-lb")
    assert reverted.status_code == 409 and "dns-swap" in message(reverted)
    # a completed plan still needs it; a cancelled one does not
    stored = api.store.get("plan", plan_id, Plan)
    api.store.put("plan", stored.model_copy(update={"status": PlanStatus.completed}))
    assert delete(api, "/hooks/dns-swap").status_code == 409
    api.store.put("plan", stored.model_copy(update={"status": PlanStatus.cancelled}))
    assert delete(api, "/hooks/dns-swap").status_code == 204
    assert delete(api, "/hooks/drain-lb").status_code == 204
    assert delete(api, "/hooks/drain-lb").status_code == 404
    assert api.get("/api/v1/hooks").json() == []


def stored_token(api, name):
    return (api.settings.secrets_dir / f"hook-{name}" / "token").read_text()


def test_a_hooks_secret_is_write_only_and_named_after_it(api):
    """SDD §12, §13.3 (pentest 2026-11-19, P1): a webhook hook's bearer token is write-only. POST
    and PATCH take it as `secret`, PUT /hooks/{name}/secret as {"token"} - an admin alone -; it
    reaches the secret store under hook-{name} and no route, event or refusal returns it: the
    answers say `secret_set` and `secret_updated_at` instead. Deleting the hook deletes it."""
    created = register(api, WEBHOOK)
    assert created["secret_set"] is True and created["secret_updated_at"]
    assert stored_token(api, "dns-swap") == TOKEN
    bare = register(api, {k: v for k, v in WEBHOOK.items() if k != "secret"} | {"name": "bare"})
    assert bare["secret_set"] is False and bare["secret_updated_at"] is None
    rotated = "rotated-" + TOKEN
    for role in (Role.viewer, Role.operator, Role.approver):
        refused = api.client.put(
            "/api/v1/hooks/bare/secret", headers=api.h(role), json={"token": rotated}
        )
        assert refused.status_code == 403
    put = lambda name, body: api.client.put(  # noqa: E731
        f"/api/v1/hooks/{name}/secret", headers=api.h(Role.admin), json=body
    )
    assert put("nope", {"token": rotated}).status_code == 404
    for body in ({}, {"secret": rotated}, {"token": rotated, "x": 1}, {"token": "a b"}):
        refused = put("bare", body)
        assert refused.status_code == 422 and rotated not in refused.text, body
    answer = put("bare", {"token": rotated})
    assert answer.status_code == 200, answer.text
    assert answer.json()["secret_set"] is True and answer.json()["secret_updated_at"]
    assert stored_token(api, "bare") == rotated
    # PATCH rotates it too, and the other fields stay
    newer = "newer-" + TOKEN
    patched = patch(api, "/hooks/dns-swap", {"secret": newer, "timeout_s": 30})
    assert patched.status_code == 200 and patched.json()["timeout_s"] == 30
    assert stored_token(api, "dns-swap") == newer
    assert patched.json()["secret_updated_at"] >= created["secret_updated_at"]
    # no answer of any role holds a token, nor does an event
    answers = "".join(
        api.get(path, role=role).text
        for role in (Role.viewer, Role.operator, Role.approver, Role.admin)
        for path in ("/api/v1/hooks", "/api/v1/hooks/dns-swap", "/api/v1/hooks/bare")
    )
    answers += answer.text + patched.text + created.__repr__()
    events = api.client.get("/api/v1/events?limit=1000", headers=api.h(Role.admin)).text
    for token in (TOKEN, rotated, newer):
        assert token not in answers and token not in events
    assert '"secret":' not in answers
    kinds = [(e.kind, e.data.get("fields")) for e in hook_events(api)]
    assert ("hook.updated", ["secret"]) in kinds
    assert ("hook.updated", ["secret", "timeout_s"]) in kinds
    # the secret goes with its hook
    assert delete(api, "/hooks/bare").status_code == 204
    assert not (api.settings.secrets_dir / "hook-bare").exists()
    # a registration refused as a duplicate replaces no hook's secret
    assert api.post("/api/v1/hooks", json={**WEBHOOK, "secret": "other"}).status_code == 409
    assert stored_token(api, "dns-swap") == newer


def test_a_hooks_url_is_shown_in_full_to_an_admin_alone(api):
    """SDD §12 (pentest 2026-11-19, P1): a webhook's URL may hold a token in its path, so a role
    below admin reads it as null beside its host, as a notification channel's (§24.3)."""
    register(api, {**WEBHOOK, "url": "https://hooks.example.org/in/T0P-S3CRET-PATH"})
    register(api, PLAYBOOK)
    for role in (Role.viewer, Role.operator, Role.approver):
        for answer in (
            api.get("/api/v1/hooks/dns-swap", role=role),
            api.get("/api/v1/hooks", role=role),
        ):
            assert "T0P-S3CRET-PATH" not in answer.text
        hook = api.get("/api/v1/hooks/dns-swap", role=role).json()
        assert (hook["url"], hook["url_host"]) == (None, "hooks.example.org")
    hook = api.get("/api/v1/hooks/dns-swap", role=Role.admin).json()
    assert hook["url"] == "https://hooks.example.org/in/T0P-S3CRET-PATH"
    assert hook["url_host"] == "hooks.example.org"
    playbook = api.get("/api/v1/hooks/drain-lb").json()
    assert (playbook["url"], playbook["url_host"], playbook["secret_set"]) == (None, None, False)


def test_a_hook_secret_name_is_refused_for_a_provider_and_a_dns_target(api):
    """SDD §13.3 (pentest 2026-11-19, P1): hook-* names are the hooks' secrets - a provider's
    credential or a DNS target's TSIG key never is one, compared as the readers fold a name: a
    check would send a hook's bearer token to the provider's endpoint."""
    register(api, WEBHOOK)
    for name in ("hook-dns-swap", "Hook_dns.swap"):
        refused = patch(api, "/providers/dst-rhoso", {"credentials_secret": name})
        assert refused.status_code == 422 and "hook-*" in message(refused), name
        refused = patch(
            api,
            "/providers/src-osp",
            {"conversion_host": {**SOURCE["conversion_host"], "ssh_key_secret": name}},
        )
        assert refused.status_code == 422 and "hook-*" in message(refused), name
    plan = {
        **PLAN,
        "network_cutover": {
            "dns_targets": {
                "vm-1": {
                    "kind": "rfc2136",
                    "zone": "example.org.",
                    "record": "app.example.org.",
                    "server": "192.0.2.53",
                    "tsig_secret": "hook-dns-swap",
                }
            }
        },
    }
    refused = api.post("/api/v1/plans", role=Role.operator, json=plan)
    assert refused.status_code == 422 and "hook-*" in message(refused), refused.text
