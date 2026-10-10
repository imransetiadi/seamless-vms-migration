import os
import re

import pytest
import yaml

from seamless_migrate import __version__, cli
from seamless_migrate.domain.enums import Phase, PlanStatus, ProviderKind, ProviderRole
from seamless_migrate.domain.models import Plan
from seamless_migrate.security.auth import hash_token
from seamless_migrate.store import Store
from tests.factories import make_migration, make_provider, make_vm

GIB = 2**30


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("SEAMLESS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SEAMLESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SEAMLESS_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    return tmp_path


def store_for(tmp_path) -> Store:
    store = Store(f"sqlite:///{tmp_path / 'cli.db'}")
    store.create_schema()
    return store


def test_version(env, capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_token_create_prints_token_and_yaml_hash(env, capsys):
    assert cli.main(["token", "create", "--name", "sari", "--role", "approver"]) == 0
    out = capsys.readouterr().out
    token = out.splitlines()[0].strip()
    assert re.fullmatch(r"smg_[A-Za-z0-9_-]{43}", token)
    entry = yaml.safe_load(out.split("\n", 1)[1])
    assert entry == {"tokens": [{"name": "sari", "role": "approver", "sha256": hash_token(token)}]}
    assert out.count(token) == 1, "the token is printed once"
    with pytest.raises(SystemExit):
        cli.main(["token", "create", "--name", "x", "--role", "root"])


def test_plan_apply_from_yaml(env, capsys):
    store = store_for(env)
    store.put("provider", make_provider(id="src-osp"))
    store.put(
        "provider",
        make_provider(id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination),
    )
    path = env / "plan.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Finance web tier",
                "source_provider_id": "src-osp",
                "destination_provider_id": "dst-rhoso",
                "vm_ids": ["vm-1", "vm-2"],
                "downtime_slo_s": 600,
                "mappings": {"networks": {"app-net": "rhoso-app"}},
            }
        )
    )
    assert cli.main(["plan", "apply", "-f", str(path)]) == 0
    plan_id = capsys.readouterr().out.strip().split()[-1]
    plan = store.get("plan", plan_id, Plan)
    assert plan.name == "Finance web tier" and plan.downtime_slo_s == 600
    assert plan.mappings.networks == {"app-net": "rhoso-app"}

    # applying again updates the same plan (matched by name)
    doc = yaml.safe_load(path.read_text())
    doc["downtime_slo_s"] = 900
    path.write_text(yaml.safe_dump(doc))
    assert cli.main(["plan", "apply", "-f", str(path)]) == 0
    assert capsys.readouterr().out.strip().split()[-1] == plan_id
    assert store.get("plan", plan_id, Plan).downtime_slo_s == 900
    assert len(store.list("plan", Plan)) == 1
    kinds = [e.kind for e in store.events(since_seq=0)]
    assert kinds == ["plan.created", "plan.updated"]

    bad = env / "bad.yaml"
    bad.write_text(yaml.safe_dump({**doc, "source_provider_id": "nope"}))
    assert cli.main(["plan", "apply", "-f", str(bad)]) == 1
    assert "nope" in capsys.readouterr().err
    # an explicit id must have the SDD §4.2 shape (it is part of every route)
    bad_id = env / "bad-id.yaml"
    bad_id.write_text(yaml.safe_dump({**doc, "id": "finance plan"}))
    assert cli.main(["plan", "apply", "-f", str(bad_id)]) == 1
    assert "invalid plan id" in capsys.readouterr().err


def _applied_plan(env, capsys):
    store = store_for(env)
    store.put("provider", make_provider(id="src-osp"))
    store.put(
        "provider",
        make_provider(id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination),
    )
    path = env / "plan.yaml"
    doc = {
        "name": "Finance web tier",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": ["vm-1"],
        "downtime_slo_s": 600,
    }
    path.write_text(yaml.safe_dump(doc))
    assert cli.main(["plan", "apply", "-f", str(path)]) == 0
    plan_id = capsys.readouterr().out.strip().split()[-1]
    path.write_text(yaml.safe_dump({**doc, "downtime_slo_s": 900}))
    return store, plan_id, path


def test_plan_apply_refuses_an_update_while_a_migration_is_in_flight(env, capsys):
    """SDD §15/§12: an update follows PATCH /plans/{id}, with the same refusal."""
    store, plan_id, path = _applied_plan(env, capsys)
    done = make_migration(
        plan_id=plan_id, vm=make_vm(source_id="vm-1", name="web-01"), phase=Phase.completed
    )
    store.put("migration", done)
    assert cli.main(["plan", "apply", "-f", str(path)]) == 1
    assert capsys.readouterr().err.strip() == (
        "error: finalize or roll back the completed migration (web-01) before editing the plan"
    )
    assert store.get("plan", plan_id, Plan).downtime_slo_s == 600
    done.phase = Phase.finalized
    store.put("migration", done)
    assert cli.main(["plan", "apply", "-f", str(path)]) == 0
    assert store.get("plan", plan_id, Plan).downtime_slo_s == 900


def test_plan_apply_never_overwrites_a_change_it_did_not_see(env, capsys, monkeypatch):
    """SDD §15: a server that starts the plan between the CLI's read and its write wins; the CLI
    fails and asks to run it again instead of putting the plan back to draft."""
    store, plan_id, path = _applied_plan(env, capsys)
    opened = cli._store

    def racing_store(settings):
        cli_store = opened(settings)
        read = cli_store.get_versioned

        def get_versioned(kind, id_, model_cls):
            got = read(kind, id_, model_cls)
            if kind == "plan":  # meanwhile the server starts the plan
                started = store.get("plan", id_, Plan)
                started.status = PlanStatus.running
                store.put("plan", started)
            return got

        cli_store.get_versioned = get_versioned
        return cli_store

    monkeypatch.setattr(cli, "_store", racing_store)
    assert cli.main(["plan", "apply", "-f", str(path)]) == 1
    assert capsys.readouterr().err.strip() == (
        f"error: plan {plan_id} changed while it was being applied; run plan apply again"
    )
    plan = store.get("plan", plan_id, Plan)
    assert (plan.status, plan.downtime_slo_s) == (PlanStatus.running, 600)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "x" * 201}, "name: at most 200 characters (got 201)"),
        ({"vm_ids": ["vm-1", "vm-1"]}, "vm_ids lists a VM more than once: vm-1"),
        (
            {"cutover_window": {"start": "2026-10-11T02:00:00Z", "end": "2026-10-11T01:00:00Z"}},
            "cutover_window: end must be after start",
        ),
        (
            {"estimator_overrides": {"nope": 1.0}},
            "invalid estimator_overrides: nope: unknown field",
        ),
    ],
    ids=["name", "vm_ids", "settings", "estimator_overrides"],
)
def test_plan_apply_refuses_what_the_api_refuses(env, capsys, change, message):
    """SDD §15: the checks of POST /plans, in its order and with its messages (§12) - a plan the
    API and the dashboard's form refuse is never stored, neither created nor updated."""
    store, plan_id, path = _applied_plan(env, capsys)
    doc = yaml.safe_load(path.read_text())
    for name, extra in (("new", {"name": "Another plan"}), ("update", {})):
        bad = env / f"{name}.yaml"
        bad.write_text(yaml.safe_dump({**doc, **extra, **change}))
        assert cli.main(["plan", "apply", "-f", str(bad)]) == 1, name
        assert capsys.readouterr().err.strip() == f"error: {message}", name
    assert [p.id for p in store.list("plan", Plan)] == [plan_id]
    assert store.get("plan", plan_id, Plan).downtime_slo_s == 600


def test_plan_validate_start_and_status_in_demo(env, capsys, monkeypatch):
    monkeypatch.setenv("SEAMLESS_DEMO", "true")
    store = store_for(env)
    from seamless_migrate.providers.fake import FakeSourceProvider

    store.put("provider", make_provider(id="src-osp"))
    store.put(
        "provider",
        make_provider(id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination),
    )
    import asyncio

    vms = asyncio.run(FakeSourceProvider(ProviderKind.openstack, 42).list_vms())
    picked = [v.source_id for v in vms if v.name in ("web-01", "dns-01")]
    plan = Plan(
        name="Demo",
        source_provider_id="src-osp",
        destination_provider_id="dst-rhoso",
        vm_ids=picked,
        mappings={"networks": {"finance-app": "finance-app"}},
    )
    store.put("plan", plan)
    assert cli.main(["plan", "validate", plan.id]) == 0
    out = capsys.readouterr().out
    assert "web-01" in out and "dns-01" in out and "ready" in out and "OK" in out
    assert cli.main(["plan", "start", plan.id]) == 0
    assert "running" in capsys.readouterr().out
    assert cli.main(["status", "--plan", plan.id]) == 0
    status = capsys.readouterr().out
    assert "web-01" in status and "ready" in status


def test_estimate_table(env, capsys):
    path = env / "vms.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "vms": [
                    {
                        "source_id": "vm-1",
                        "name": "db-01",
                        "vcpus": 4,
                        "ram_mb": 8192,
                        "disks": [{"id": "d1", "size_gb": 200, "used_gb": 100.0}],
                    }
                ]
            }
        )
    )
    assert cli.main(["estimate", "-f", str(path), "--strategy", "cold", "--slo", "300"]) == 0
    out = capsys.readouterr().out
    assert "db-01" in out and "cold" in out and "1089" in out  # 60+30+819.2+60+120
    lines = [ln for ln in out.splitlines() if "db-01" in ln]
    assert len(lines) == 1 and "no" in lines[0].split()

    assert cli.main(["estimate", "-f", str(path), "--strategy", "cold", "--link-mbps", "1000"]) == 0
    assert "1129" in capsys.readouterr().out  # 100 GiB over 125,000,000 B/s

    assert cli.main(["estimate", "-f", str(path)]) == 0
    out = capsys.readouterr().out
    assert all(s in out for s in ("cold", "warm", "storage_handover"))

    # calibration knobs mirror Plan.estimator_overrides (SDD §9.1): a faster scan shrinks the
    # warm downtime floor, which is 60+30+scan+60+120 with scan = 200 GiB / S
    assert cli.main(["estimate", "-f", str(path), "--strategy", "warm", "--slo", "600"]) == 0
    default = capsys.readouterr().out
    assert "680" in default  # scan = 200 GiB / 500 MiB/s = 409.6 s
    assert (
        cli.main(["estimate", "-f", str(path), "--strategy", "warm", "--scan-mibps", "1000"]) == 0
    )
    faster = capsys.readouterr().out
    assert "475" in faster  # scan = 204.8 s
    lines = [ln for ln in faster.splitlines() if "db-01" in ln]
    assert len(lines) == 1 and "yes" in lines[0].split(), "meets the default 600 s SLO"
    assert cli.main(["estimate", "-f", str(path), "--scan-mibps", "0"]) == 2
    assert "scan_bps" in capsys.readouterr().err
    assert (
        cli.main(["estimate", "-f", str(path), "--parallel-disks", "1", "--max-passes", "2"]) == 0
    )


def test_serve_refuses_auth_disabled_on_public_bind(env, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_server", lambda app, settings, reload: calls.append(settings))
    monkeypatch.setenv("SEAMLESS_AUTH_DISABLED", "true")
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9999"]) == 2
    assert "loopback" in capsys.readouterr().err and calls == []

    assert cli.main(["serve", "--host", "127.0.0.1"]) == 0
    assert calls[-1].auth_disabled is True and calls[-1].host == "127.0.0.1"

    # demo mode on loopback disables auth by default, but not on a public bind
    monkeypatch.delenv("SEAMLESS_AUTH_DISABLED")
    assert cli.main(["serve", "--demo"]) == 0
    assert calls[-1].demo is True and calls[-1].auth_disabled is True
    assert cli.main(["serve", "--demo", "--host", "0.0.0.0"]) == 0
    assert calls[-1].auth_disabled is False and calls[-1].port == 8080


def test_json_logging_covers_uvicorn_loggers(env, capsys, monkeypatch):
    import json as _json
    import logging
    import logging.config

    from seamless_migrate.config import Settings

    settings = Settings(data_dir=env / "data", log_json=True, log_level="INFO")
    cli.configure_logging(settings)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # left over by a server
    logging.config.dictConfig(cli.uvicorn_log_config(settings.log_level))
    try:
        logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /api/v1/health" 200')
        logging.getLogger("seamless_migrate.orchestrator").warning("tick slow")
    finally:
        logging.getLogger().handlers[:] = []
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    records = [_json.loads(ln) for ln in lines]
    assert [r["logger"] for r in records] == ["uvicorn.access", "seamless_migrate.orchestrator"]
    assert records[0]["level"] == "INFO" and "/api/v1/health" in records[0]["message"]
    assert not logging.getLogger("uvicorn.access").handlers  # routed through the root handler


@pytest.mark.parametrize("log_json", [False, True])
def test_the_service_log_is_redacted(env, capsys, log_json):
    """SDD §13.4 (Security.md C2-02): every service-log record passes through redact — a message,
    and the chained exception a traceback prints under the ProviderError raised from it, which the
    provider redacted only in its own text; a JSON line stays valid JSON."""
    import json as _json
    import logging

    from seamless_migrate.config import Settings
    from seamless_migrate.providers.base import ProviderError

    cli.configure_logging(Settings(data_dir=env / "data", log_json=log_json, log_level="INFO"))
    log = logging.getLogger("seamless_migrate.orchestrator")
    sdk_error = "Unauthorized: POST https://svc:" + "Sup3rS3cretPw" + "@keystone.example:5000/v3"
    try:
        log.warning('vcenter refused password="Hunter2xyz"')
        try:
            try:
                raise RuntimeError(sdk_error)
            except RuntimeError as exc:
                raise ProviderError("src-osp: Unauthorized") from exc
        except ProviderError:
            log.warning("source watch failed for %s", "mig-1", exc_info=True)
    finally:
        logging.getLogger().handlers[:] = []
    err = capsys.readouterr().err
    assert "Hunter2xyz" not in err and "Sup3r" not in err
    assert "source watch failed for mig-1" in err and "https://[REDACTED]@keystone.example" in err
    if log_json:
        records = [_json.loads(ln) for ln in err.splitlines() if ln.strip()]
        assert [r["message"] for r in records] == [
            "vcenter refused password=[REDACTED]",
            "source watch failed for mig-1",
        ]
        assert "RuntimeError: Unauthorized: POST https://[REDACTED]@" in records[1]["exc"]


def test_events_export_and_prune(env, capsys, tmp_path):
    import json
    from datetime import UTC, datetime, timedelta

    from seamless_migrate.domain.models import Event

    store = store_for(env)
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(3):
        store.append_event(
            Event(ts=t0 + timedelta(days=i), kind="plan.updated", actor="t", message=f"e{i}",
                  plan_id="plan-00000001" if i else None)
        )  # fmt: skip
    out = tmp_path / "events.jsonl"
    assert cli.main(["events", "export", "-o", str(out)]) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"  # the audit export is owner-only
    assert cli.main(["events", "export", "-o", str(out)]) == 1  # never silently overwritten
    assert "exists" in capsys.readouterr().err
    lines = out.read_text().splitlines()
    assert len(lines) == 3 and all(json.loads(ln)["kind"] == "plan.updated" for ln in lines)
    assert cli.main(["events", "export", "--plan", "plan-00000001"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    # prune refuses without --confirm and without a bound
    assert cli.main(["events", "prune", "--before", "2026-10-02T00:00:00Z"]) == 2
    assert cli.main(["events", "prune", "--confirm"]) == 2
    assert cli.main(["events", "prune", "--before", "2026-10-02T00:00:00Z", "--confirm"]) == 0
    assert "deleted 1 event(s)" in capsys.readouterr().out
    assert cli.main(["events", "prune", "--older-than-days", "0", "--confirm"]) == 0
    assert "deleted 2 event(s)" in capsys.readouterr().out
    assert store.events(since_seq=0, limit=10) == []
