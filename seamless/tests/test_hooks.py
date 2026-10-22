"""The hook runner (SDD §7.2 Hooks, §13.3, §23.3, plan task N4)."""

import json
import os
import stat
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole
from seamless_migrate.domain.models import Hook, Nic, NicOverride
from seamless_migrate.hooks import (
    HookRunner,
    context_document,
    hook_action_event,
    idempotency_key,
)
from tests.factories import make_migration, make_plan, make_provider, make_vm

SOURCE = make_provider()
DESTINATION = make_provider(
    id="dst-rhoso", name="RHOSO 18.0", kind=ProviderKind.rhoso, role=ProviderRole.destination
)
PLAYBOOK = Hook(name="drain-lb", kind="playbook", playbook="/opt/hooks/drain-lb.yml", timeout_s=30)
WEBHOOK = Hook(
    name="dns-swap", kind="webhook", url="https://hooks.example.org/dns", secret="dns-hook-token"
)

#: a fake ansible-playbook: records what it was given, prints its lines, sleeps, exits - told how
#: by ANSIBLE_HOOK_FAKE_* variables, the ANSIBLE_* the hook's environment lets through
FAKE = """#!{python}
import json, os, sys, time
context = os.path.join(os.getcwd(), "context.yml")
mode = os.stat(context).st_mode & 0o777 if os.path.exists(context) else None
with open(os.environ["ANSIBLE_HOOK_FAKE_RECORD"], "w") as fh:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
               "context": open(context).read() if mode is not None else None,
               "mode": mode, "pid": os.getpid()}}, fh)
for line in os.environ.get("ANSIBLE_HOOK_FAKE_LINES", "").split("|"):
    if line:
        print(line, flush=True)
time.sleep(float(os.environ.get("ANSIBLE_HOOK_FAKE_SLEEP", "0")))
sys.exit(int(os.environ.get("ANSIBLE_HOOK_FAKE_EXIT", "0")))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "ansible-playbook"
    fake.parent.mkdir()
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    secrets = tmp_path / "secrets"
    (secrets / "dns-hook-token").mkdir(parents=True)
    (secrets / "dns-hook-token" / "token").write_text("t0ken-for-the-hook")
    record = tmp_path / "record.json"
    monkeypatch.setenv("ANSIBLE_HOOK_FAKE_RECORD", str(record))
    # what the control plane holds and no hook may get (SDD §7.2)
    monkeypatch.setenv("SEAMLESS_SECRET_RHOSO_PASSWORD", "must-not-leak")
    monkeypatch.setenv("TYPESAFE_API_KEY", "must-not-leak")
    monkeypatch.setenv("OS_PASSWORD", "must-not-leak")
    settings = Settings(
        data_dir=tmp_path / "data",
        ansible_playbook=str(fake),
        secrets_dir=secrets,
        clouds_yaml=tmp_path / "clouds.yaml",
    )
    return settings, record


def migration():
    nics = [
        Nic(
            network="app-net",
            mac="fa:16:3e:00:00:01",
            fixed_ips=["10.0.0.5"],
            floating_ips=["203.0.113.10"],
        ),
        Nic(network="db-net", mac="fa:16:3e:00:00:02", fixed_ips=["10.1.0.5"]),
    ]
    return make_migration(
        id="mig-0123456789",
        vm=make_vm(nics=nics),
        attempts=2,
        destination_server_id="srv-dst-1",
    )


def plan():
    return make_plan(
        id="plan-0000abcd",
        nic_overrides={"vm-1": {"fa:16:3e:00:00:02": NicOverride(fixed_ips=["10.9.0.5"])}},
    )


def document(point="post_create"):
    return context_document(migration(), plan(), point, SOURCE, DESTINATION)


async def run(settings, hook, context, tmp_path, transport=None, lines=None):
    seen = []

    async def log(line):
        seen.append(line)

    runner = HookRunner(settings, transport=transport)
    result = await runner.run(hook, context, tmp_path / "run", log)
    return result, seen


def test_the_context_document_carries_the_migration_and_no_credential():
    """SDD §7.2: what a hook gets - the migration, its attempt and point, the plan, the VM, both
    providers by id and label, the destination server, the addresses before and after (a NIC's
    override's, else its own), the floating IPs and the idempotency key."""
    assert document() == {
        "migration_id": "mig-0123456789",
        "attempt": 2,
        "point": "post_create",
        "plan_id": "plan-0000abcd",
        "vm_name": "web-01",
        "source": {"id": "src-osp", "label": "RHOSP 17.1"},
        "destination": {"id": "dst-rhoso", "label": "RHOSO 18.0"},
        "server_id": "srv-dst-1",
        "addresses": {"before": ["10.0.0.5", "10.1.0.5"], "after": ["10.0.0.5", "10.9.0.5"]},
        "floating_ips": ["203.0.113.10"],
        "idempotency_key": "mig-0123456789.2.post_create",
    }


async def test_a_playbook_hook_gets_the_context_and_no_credential(env, tmp_path):
    """SDD §7.2: a playbook hook runs as ansible-playbook -i localhost, -c local <playbook>
    -e @context.yml in its own run directory, the document under seamless_hook in a 0600 file with
    every string !unsafe; its environment holds none of the control plane's secrets, tokens or
    cloud credentials, and its run directory is gone after it."""
    settings, record = env
    context = document()
    result, _ = await run(settings, PLAYBOOK, context, tmp_path)
    assert (result.outcome, result.detail, result.hook, result.point) == (
        "succeeded",
        None,
        "drain-lb",
        "post_create",
    )
    seen = json.loads(record.read_text())
    assert seen["argv"] == [
        "-i",
        "localhost,",
        "-c",
        "local",
        "/opt/hooks/drain-lb.yml",
        "-e",
        "@context.yml",
    ]
    assert Path(seen["cwd"]) == tmp_path / "run" / "hooks" / "post_create-drain-lb"
    assert seen["mode"] == 0o600
    assert "!unsafe" in seen["context"]

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))
    assert yaml.load(seen["context"], Loader=Loader) == {"seamless_hook": context}
    leaked = {k for k, v in seen["env"].items() if v == "must-not-leak"}
    assert leaked == set()
    assert not any(k.startswith(("SEAMLESS_", "OS_")) for k in seen["env"])
    assert not (tmp_path / "run" / "hooks" / "post_create-drain-lb").exists()
    # a playbook that fails names its exit status
    os.environ["ANSIBLE_HOOK_FAKE_EXIT"] = "2"
    try:
        failed, _ = await run(settings, PLAYBOOK, context, tmp_path)
    finally:
        del os.environ["ANSIBLE_HOOK_FAKE_EXIT"]
    assert failed.outcome == "failed" and failed.detail.startswith("exit 2")


async def test_a_webhook_hook_posts_the_context_with_its_bearer_secret(env, tmp_path):
    """SDD §7.2, §13.3: a webhook hook POSTs the document as JSON with the bearer token its secret
    holds as `token` and the idempotency key; any 2xx succeeds; a redirect is never followed - the
    token never reaches another URL -; another status fails by its number, and a secret without a
    token fails without a request."""
    settings, _ = env
    context = document()
    requests = []

    def answering(status, headers=None):
        def handler(request):
            requests.append(request)
            return httpx.Response(status, headers=headers or {}, text="body never logged")

        return httpx.MockTransport(handler)

    result, logged = await run(settings, WEBHOOK, context, tmp_path, answering(204))
    assert (result.outcome, result.detail) == ("succeeded", None)
    [request] = requests
    assert (request.method, str(request.url)) == ("POST", "https://hooks.example.org/dns")
    assert request.headers["authorization"] == "Bearer t0ken-for-the-hook"
    assert request.headers["idempotency-key"] == "mig-0123456789.2.post_create"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == context
    requests.clear()
    moved = answering(307, {"location": "https://elsewhere.example/dns"})
    result, _ = await run(settings, WEBHOOK, context, tmp_path, moved)
    assert (result.outcome, result.detail) == ("failed", "HTTP 307")
    assert len(requests) == 1
    requests.clear()
    result, logged = await run(settings, WEBHOOK, context, tmp_path, answering(500))
    assert (result.outcome, result.detail) == ("failed", "HTTP 500")
    assert not any("body never logged" in line for line in logged)
    requests.clear()
    (settings.secrets_dir / "dns-hook-token" / "token").unlink()
    result, _ = await run(settings, WEBHOOK, context, tmp_path, answering(204))
    assert (result.outcome, result.detail) == ("failed", "its secret holds no token")
    assert requests == []


async def test_a_hook_that_exceeds_its_timeout_fails(env, tmp_path, monkeypatch):
    """SDD §7.2, §23.3: a run past the hook's timeout_s fails - the playbook's process is stopped,
    the webhook's request abandoned."""
    settings, record = env
    context = document()
    monkeypatch.setenv("ANSIBLE_HOOK_FAKE_SLEEP", "30")
    slow = PLAYBOOK.model_copy(update={"timeout_s": 1})
    started = time.monotonic()
    result, _ = await run(settings, slow, context, tmp_path)
    assert time.monotonic() - started < 15
    assert (result.outcome, result.detail) == ("failed", "timed out after 1 s")
    pid = json.loads(record.read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    async def stalling(request):
        import asyncio

        await asyncio.sleep(30)
        return httpx.Response(204)

    started = time.monotonic()
    result, _ = await run(
        settings,
        WEBHOOK.model_copy(update={"timeout_s": 1}),
        context,
        tmp_path,
        httpx.MockTransport(stalling),
    )
    assert time.monotonic() - started < 15
    assert (result.outcome, result.detail) == ("failed", "timed out after 1 s")


async def test_a_hooks_output_is_redacted(env, tmp_path, monkeypatch):
    """SDD §7.2, §13.4: each line a playbook hook prints reaches the migration's log through
    redact(), and a failure's detail - its last error line - too; the migration.action event
    carries no output."""
    settings, _ = env
    monkeypatch.setenv(
        "ANSIBLE_HOOK_FAKE_LINES",
        "TASK [drain]|password: hunter2|Authorization: Bearer abc.def.ghi|"
        "fatal: [localhost]: FAILED! token=s3cr3t-value",
    )
    monkeypatch.setenv("ANSIBLE_HOOK_FAKE_EXIT", "2")
    result, logged = await run(settings, PLAYBOOK, document(), tmp_path)
    text = "\n".join(logged)
    assert "TASK [drain]" in text
    for secret in ("hunter2", "abc.def.ghi", "s3cr3t-value"):
        assert secret not in text and secret not in (result.detail or "")
    assert result.outcome == "failed"
    assert result.detail.startswith("exit 2: fatal: [localhost]: FAILED! token=[REDACTED]")
    event = hook_action_event(migration(), result)
    assert event.kind == "migration.action"
    assert event.data == {
        "action": "hook",
        "hook": "drain-lb",
        "kind": "playbook",
        "point": "post_create",
        "idempotency_key": "mig-0123456789.2.post_create",
        "outcome": "failed",
        "duration_s": result.duration_s,
        "detail": result.detail,
    }
    assert (event.plan_id, event.migration_id) == ("plan-0000abcd", "mig-0123456789")
    assert "hunter2" not in json.dumps(event.model_dump(mode="json"))


async def test_a_rerun_hook_gets_the_same_idempotency_key(env, tmp_path):
    """SDD §7.2, §8: every run of a point within an attempt - a step a restart resumes runs its
    hooks again - gets the same key; another point, or a retry's new attempt, another."""
    settings, _ = env
    assert idempotency_key("mig-0123456789", 2, "post_create") == "mig-0123456789.2.post_create"
    keys = []

    def handler(request):
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(200)

    for _ in range(2):
        await run(settings, WEBHOOK, document(), tmp_path, httpx.MockTransport(handler))
    assert keys == ["mig-0123456789.2.post_create"] * 2
    assert document("on_rollback")["idempotency_key"] == "mig-0123456789.2.on_rollback"
    retried = migration().model_copy(update={"attempts": 3})
    again = context_document(retried, plan(), "post_create", SOURCE, DESTINATION)
    assert again["idempotency_key"] == "mig-0123456789.3.post_create"
