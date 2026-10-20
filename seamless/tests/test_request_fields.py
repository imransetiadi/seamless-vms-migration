"""SDD §11, §12 (audit AP-01, AP-02): a request body's unknown field is refused at every depth,
while a stored document keeps loading with the fields a later version added."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Role
from seamless_migrate.domain.models import REQUEST_BODY, Plan, PlanCreate, Provider
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings
from tests.factories import make_plan

PLAN = {
    "name": "Unknown fields",
    "source_provider_id": "src-osp",
    "destination_provider_id": "dst-rhoso",
    "vm_ids": [],
}


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        a = Api(client, store, settings, tokens)
        for provider in (SOURCE, DESTINATION):
            assert a.post("/api/v1/providers", json=provider).status_code == 201
        yield a
    store.dispose()


def send(api, method, path, body, role):
    return api.client.request(method, path, headers=api.h(role), json=body)


HOST = {"name": "conv-src", "flavor": "m1.large"}


@pytest.mark.parametrize(
    ("method", "path", "body", "role", "misspelt"),
    [
        # Security.md SEC-03's guard: the conversion host's SSH would stay open to the world
        (
            "POST",
            "/api/v1/providers",
            {
                **SOURCE,
                "id": "src-two",
                "conversion_host": {**HOST, "ssh_allowed_cdir": "10.0.0.0/16"},
            },
            Role.admin,
            "ssh_allowed_cdir",
        ),
        (
            "PATCH",
            "/api/v1/providers/src-osp",
            {"conversion_host": {**HOST, "ssh_allowed_cdir": "10.0.0.0/16"}},
            Role.admin,
            "ssh_allowed_cdir",
        ),
        # the verification would never probe the application port
        (
            "POST",
            "/api/v1/plans",
            {**PLAN, "verification": {"tcp_port": [443]}},
            Role.operator,
            "tcp_port",
        ),
        # a list of probes inside a map, three levels down
        (
            "POST",
            "/api/v1/plans",
            {
                **PLAN,
                "probe_overrides": {"vm-1": [{"kind": "http", "port": 80, "expect_stauts": 200}]},
            },
            Role.operator,
            "expect_stauts",
        ),
        (
            "PATCH",
            "{plan}",
            {"mappings": {"network": {"app-net": "prod"}}},
            Role.operator,
            "network",
        ),
        # a plan-scoped policy would become a global one (audit AP-02)
        (
            "POST",
            "/api/v1/advisor/policies",
            {
                "name": "p",
                "scopes": {"plan_ids": ["{plan_id}"]},
                "mode": "always_require_approval",
                "threshold": 0.9,
            },
            Role.admin,
            "scopes",
        ),
        (
            "PUT",
            "{policy}",
            {
                "name": "p",
                "scope": {"plan_id": ["{plan_id}"]},
                "mode": "always_require_approval",
                "threshold": 0.9,
            },
            Role.admin,
            "plan_id",
        ),
    ],
)
def test_a_misspelt_field_at_any_depth_of_a_request_body_is_refused(
    api, method, path, body, role, misspelt
):
    """SDD §12 (audit AP-01, AP-02): a field the route does not take is refused with 422 naming it -
    nested too - so a misspelt guard never silently yields a provider, plan or policy without it;
    nothing is written."""
    plan_id = api.post("/api/v1/plans", Role.operator, json=PLAN).json()["id"]
    spec = {"name": "p", "mode": "always_require_approval", "threshold": 0.9}
    policy = api.post(
        "/api/v1/advisor/policies", Role.admin, json={**spec, "scope": {"plan_ids": [plan_id]}}
    )
    assert policy.status_code == 201, policy.text
    path = path.format(
        plan=f"/api/v1/plans/{plan_id}", policy=f"/api/v1/advisor/policies/{policy.json()['id']}"
    )
    body = json.loads(json.dumps(body).replace("{plan_id}", plan_id))
    before = api.store.events(since_seq=0, limit=100000)
    provider = api.store.get("provider", "src-osp", Provider)
    res = send(api, method, path, body, role)
    assert res.status_code == 422, res.text
    error = res.json()["error"]
    assert error["code"] == "validation_error" and misspelt in error["message"], error
    assert len(api.store.events(since_seq=0, limit=100000)) == len(before), "something was written"
    assert api.store.get("provider", "src-osp", Provider) == provider


def test_a_misspelt_gpu_mapping_or_profile_field_is_refused(api):
    """SDD §4.2, §12 (plan task G1): Mappings.gpus and Provider.gpu_profiles are taken as spelt,
    and a misspelling of either - "gpu" for the mapping kind, "gpu_profile" for the profiles - is a
    422 naming it, never a plan or provider without its GPU settings; nothing is written."""
    gpus = {"grid_a100-4c": "g1.a100-4c"}
    for method, path, body, role, misspelt in (
        ("POST", "/api/v1/plans", {**PLAN, "mappings": {"gpu": gpus}}, Role.operator, "gpu"),
        (
            "POST",
            "/api/v1/providers",
            {**DESTINATION, "id": "dst-two", "gpu_profile": {"g1": "A100-4C"}},
            Role.admin,
            "gpu_profile",
        ),
        (
            "PATCH",
            "/api/v1/providers/dst-rhoso",
            {"gpu_profile": {"g1": "A100-4C"}},
            Role.admin,
            "gpu_profile",
        ),
    ):
        before = len(api.store.events(since_seq=0, limit=100000))
        res = send(api, method, path, body, role)
        assert res.status_code == 422, (path, res.text)
        error = res.json()["error"]
        assert error["code"] == "validation_error" and misspelt in error["message"], error
        assert len(api.store.events(since_seq=0, limit=100000)) == before, "something was written"
    plan = api.post("/api/v1/plans", Role.operator, json={**PLAN, "mappings": {"gpus": gpus}})
    assert plan.status_code == 201, plan.text
    assert plan.json()["mappings"]["gpus"] == gpus
    provider = send(
        api, "PATCH", "/api/v1/providers/dst-rhoso", {"gpu_profiles": {"g1": "A100-4C"}}, Role.admin
    )
    assert provider.status_code == 200 and provider.json()["gpu_profiles"] == {"g1": "A100-4C"}


def test_unknown_fields_are_refused_in_a_request_body_only():
    """SDD §11, §12 (audit AP-01): a stored document keeps loading with a field a later version
    added, at any depth; the same document as a request body is refused, naming the field."""
    stored = make_plan().model_dump(mode="json")
    stored["verification"] = {**stored["verification"], "later_field": 1}
    assert Plan.model_validate(stored).verification == make_plan().verification
    body = {k: v for k, v in stored.items() if k in PlanCreate.model_fields}
    with pytest.raises(ValidationError, match="later_field"):
        PlanCreate.model_validate(body, context=REQUEST_BODY)


def test_a_top_level_unknown_field_keeps_the_message_the_dashboard_mock_gives(api):
    """SDD §12, §16 (mock parity): a plan's or provider's top-level unknown field still reads
    "<field>: Extra inputs are not permitted", as the dashboard's offline mock answers it - the
    nested check leaves the top level of a body that forbids extras to pydantic."""
    plan = api.post("/api/v1/plans", Role.operator, json={**PLAN, "requrie_approval": False})
    assert plan.status_code == 422, plan.text
    assert plan.json()["error"]["message"] == "requrie_approval: Extra inputs are not permitted"
    provider = api.post("/api/v1/providers", json={**SOURCE, "id": "src-two", "verify_tsl": False})
    assert provider.status_code == 422, provider.text
    assert provider.json()["error"]["message"] == "verify_tsl: Extra inputs are not permitted"
