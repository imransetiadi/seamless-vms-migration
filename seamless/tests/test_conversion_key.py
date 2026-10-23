"""One conversion key per installation (SDD §7.2 "Conversion key", issue #112).

Before a pre-staging deploys conversion hosts, the orchestrator reads the os_migrate_conv
keypair of each managed side and resolves the key against it: it adopts the plan's key the
clouds hold, lets the deploy generate one, re-keys a side that holds an older plan's key, or
refuses. Every key is synthetic (tests/key_support.py).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from seamless_migrate import conversion_key
from seamless_migrate.config import Settings, find_repo_root
from seamless_migrate.domain.enums import ProviderRole
from seamless_migrate.domain.models import ConversionHostConfig
from seamless_migrate.executors.ansible import AnsibleExecutor
from seamless_migrate.executors.base import PermanentStepError
from tests.factories import make_plan, make_provider
from tests.fake_ansible import install, read_log
from tests.key_support import fingerprint, installation_key, plan_key, public_key

SRC = make_provider(name="RHOSP 17.1 east")
DST = make_provider(
    id="dst-rhoso",
    name="RHOSO 18.0",
    role=ProviderRole.destination,
    cloud="dst",
    conversion_host=ConversionHostConfig(name="conv-dst"),
)
RUNBOOK = "docs/Operations.md §7.1"


class Cloud:
    """A provider's double: the public keys of its credential's user's keypairs (SDD §10)."""

    def __init__(self, held: str | None = None) -> None:
        self.keypairs = {} if held is None else {"os_migrate_conv": held}
        self.reads: list[str] = []

    async def keypair_public_key(self, name: str) -> str | None:
        self.reads.append(name)
        return self.keypairs.get(name)


def clouds(src: str | None = None, dst: str | None = None) -> dict[str, Cloud]:
    """The source's and the destination's doubles, by provider id: each holds the os_migrate_conv
    keypair with the public key given."""
    return {SRC.id: Cloud(src), DST.id: Cloud(dst)}


async def resolve(settings, held, source=SRC, destination=DST):
    return await conversion_key.resolve(settings, source, destination, lambda p: held[p.id])


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def ansible(tmp_path, monkeypatch):
    """Settings whose ansible-playbook is the recording fake (tests/fake_ansible.py) - and its
    log."""
    log = tmp_path / "ansible.log"
    monkeypatch.setenv("ANSIBLE_FAKE_LOG", str(log))
    clouds_yaml = tmp_path / "clouds.yaml"
    clouds_yaml.write_text(
        "clouds:\n"
        "  src:\n    auth: {auth_url: 'https://src.example.invalid/v3', username: su}\n"
        "  dst:\n    auth: {auth_url: 'https://dst.example.invalid/v3', username: du}\n",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        ansible_playbook=str(install(tmp_path / "bin")),
        clouds_yaml=clouds_yaml,
        collection_root=find_repo_root(),
    )
    return settings, log


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def pub(private: Path) -> Path:
    """The public key beside a private key, as ssh-keygen writes it."""
    return Path(f"{private}.pub")


def blob(private: Path) -> str:
    """The base64 field of the public key beside a private key: the key, its comment left out."""
    return pub(private).read_text(encoding="utf-8").split()[1]


@pytest.mark.parametrize("dst_holds", [True, False], ids=["both sides", "the source alone"])
async def test_the_first_prestage_adopts_the_plan_key_the_keypair_holds(settings, dst_holds):
    """SDD §7.2 (issue #112): no installation key yet, and one plan's key matches every side that
    holds the os_migrate_conv keypair - the source's copy with another comment, the destination's
    with a line break after it, as Nova may keep them: keys compare by type and base64 alone.
    That plan's pair is copied, never moved, to {data_dir}/conversion/ - the private key 0600 in
    a 0700 directory, the public key keeping its mode -, and no side is re-keyed."""
    plan_key(settings, "plan-0000000a", 1)
    own = plan_key(settings, "plan-0000000b", 2)
    held = clouds(
        src=public_key(2, "as Nova keeps it"), dst=public_key(2) + "\n" if dst_holds else None
    )

    assert await resolve(settings, held) == {}

    key = installation_key(settings)
    assert key.read_bytes() == own.read_bytes()
    assert pub(key).read_bytes() == pub(own).read_bytes()
    assert (mode(key), mode(pub(key)), mode(key.parent)) == (0o600, 0o644, 0o700)
    assert own.is_file() and pub(own).is_file(), "copied, never moved"
    assert held[SRC.id].reads == held[DST.id].reads == ["os_migrate_conv"]


async def test_no_keypair_means_a_new_installation_key(ansible):
    """SDD §7.2: neither side holds the os_migrate_conv keypair - fresh clouds, or a keypair an
    operator deleted. Nothing is adopted, though a plan of an earlier version left its key: the
    deploy runs with the installation's path, where its generate_keypair.yml creates the key,
    and re-keys nothing."""
    settings, log = ansible
    plan_key(settings, "plan-0000000a", 1)
    held = clouds()

    assert await resolve(settings, held) == {}
    assert not (settings.data_dir / "conversion").exists(), "the deploy generates the key"

    plan = make_plan(id="plan-0000000a", prestage_resources=[])
    await AnsibleExecutor(settings).prestage(
        plan, SRC, DST, deploy_conversion_hosts=True, rekey_conversion_hosts={}
    )
    [deploy] = read_log(log)
    assert deploy["playbook"] == "deploy_conversion_hosts.yml"
    assert deploy["vars"]["os_migrate_conversion_keypair_private_path"] == str(
        installation_key(settings)
    )
    assert not [name for name in deploy["vars"] if "rekey" in name or "keypair_state" in name]


async def test_keypairs_that_disagree_fail_the_prestage_naming_both(settings):
    """SDD §7.2: no installation key yet, and the two sides' keypairs hold two plans' keys: no one
    key reaches both conversion hosts, so the pre-staging is refused before any playbook - naming
    both providers and both keys' fingerprints and pointing to the runbook - and nothing is
    written."""
    plan_key(settings, "plan-0000000a", 1)
    plan_key(settings, "plan-0000000b", 2)
    held = clouds(src=public_key(1), dst=public_key(2))

    with pytest.raises(PermanentStepError) as refused:
        await resolve(settings, held)

    message = str(refused.value)
    for part in (SRC.label, DST.label, fingerprint(1), fingerprint(2), RUNBOOK):
        assert part in message, message
    assert not (settings.data_dir / "conversion").exists()


@pytest.mark.parametrize("installed", [False, True], ids=["no installation key", "one"])
async def test_an_unknown_key_in_a_keypair_fails_the_prestage(settings, installed):
    """SDD §7.2: a keypair holding a key that neither the installation's key nor any plan's
    matches - one a person or another tool made - names hosts this installation has no private
    key for: the deploy would replace the keypair and still not reach them. The pre-staging is
    refused, naming that provider and its key's fingerprint; the other side, whose key is known,
    is not named, and nothing is adopted or re-keyed."""
    plan_key(settings, "plan-0000000a", 1)
    if installed:
        installation_key(settings, 3)
    held = clouds(src=public_key(9), dst=public_key(3 if installed else 1))

    with pytest.raises(PermanentStepError) as refused:
        await resolve(settings, held)

    message = str(refused.value)
    assert SRC.label in message and fingerprint(9) in message and RUNBOOK in message, message
    assert DST.label not in message, message
    key = installation_key(settings)
    if installed:
        assert blob(key) == public_key(3).split()[1]
    else:
        assert not key.exists()


async def test_an_old_plan_key_on_a_cloud_is_rekeyed_before_the_deploy(ansible):
    """SDD §7.2: the installation key exists. The source's keypair holds it, with another
    comment; the destination's holds an older plan's key - a cloud an earlier version's plan
    deployed on other credentials. The destination alone is re-keyed: rekey_conversion_hosts.yml
    runs right before the deploy and logs in to its conversion host with that plan's key, and the
    deploy then replaces its keypair; the source needs nothing."""
    settings, log = ansible
    key = installation_key(settings, 3)
    plan_key(settings, "plan-0000000a", 1)
    old = plan_key(settings, "plan-0000000b", 2)
    held = clouds(src=public_key(3, "another comment"), dst=public_key(2))

    rekey = await resolve(settings, held)
    assert rekey == {"dst": old}

    plan = make_plan(id="plan-000000c0", prestage_resources=["networks"])
    await AnsibleExecutor(settings).prestage(
        plan, SRC, DST, deploy_conversion_hosts=True, rekey_conversion_hosts=rekey
    )
    runs = read_log(log)
    assert [run["playbook"] for run in runs] == [
        "export_networks.yml",
        "import_networks.yml",
        "rekey_conversion_hosts.yml",
        "deploy_conversion_hosts.yml",
    ]
    variables = runs[2]["vars"]
    assert variables["os_migrate_dst_conversion_rekey_private_path"] == str(old)
    assert variables["os_migrate_dst_conversion_keypair_state"] == "replace"
    assert "os_migrate_src_conversion_rekey_private_path" not in variables
    assert "os_migrate_src_conversion_keypair_state" not in variables
    assert variables["os_migrate_conversion_keypair_private_path"] == str(key)
    assert runs[3]["vars"] == variables, "the re-key and the deploy read one vars file"


async def test_a_side_whose_conversion_host_is_not_managed_is_never_read(settings):
    """SDD §7.2: the deploy leaves an operator's conversion host (conversion_host.manage false) and
    its side's keypair alone, so the resolution neither reads nor judges that side: an unrelated
    os_migrate_conv there refuses nothing, and the managed side's plan key is adopted."""
    own = plan_key(settings, "plan-0000000a", 1)
    source = SRC.model_copy(
        update={"conversion_host": ConversionHostConfig(name="ops-conv", manage=False)}
    )
    held = clouds(src=public_key(9), dst=public_key(1))

    assert await resolve(settings, held, source=source) == {}

    assert held[SRC.id].reads == []
    assert installation_key(settings).read_bytes() == own.read_bytes()


async def test_a_key_generated_meanwhile_is_never_written_over(settings, monkeypatch):
    """SDD §7.2: another pre-staging's deploy may generate the installation key between this
    resolution's look and its copy. The copy never writes over it: that key stays, and the sides
    are judged against it - here re-keyed from the plan's key their keypairs hold."""
    old = plan_key(settings, "plan-0000000a", 1)
    adopt = conversion_key._adopt

    def generated_meanwhile(private, target):
        installation_key(settings, 3)  # what generate_keypair.yml left
        return adopt(private, target)

    monkeypatch.setattr(conversion_key, "_adopt", generated_meanwhile)
    held = clouds(src=public_key(1), dst=public_key(1))

    assert await resolve(settings, held) == {"src": old, "dst": old}

    assert blob(installation_key(settings)) == public_key(3).split()[1]
