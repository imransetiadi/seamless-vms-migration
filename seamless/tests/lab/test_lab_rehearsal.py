"""Rehearsal of the lab module in CI: the three lab tests run end to end through the real
provider and executor code (clouds.yaml parsing, openstack.connect, OpenStackProvider, the
readiness report, cutover and rollback), with the strict Cinder/Nova doubles of
test_executor_handover behind openstack.connect. It proves the lab module's wiring; the lab
itself proves the clouds (QASuite LAB-H07…H10). The changed-extents lab module (plan task CE13)
and the project credentials lab module (plan task TC11) are rehearsed below the same way."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from tests.lab import test_lab_storage_handover as lab_module
from tests.test_executor_handover import (
    NFS_DST,
    NFS_SRC,
    ONTAP_NFS,
    FakeCloud,
    source_cloud,
)


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    openstack = pytest.importorskip("openstack")
    clouds_yaml = tmp_path / "clouds.yaml"
    clouds_yaml.write_text(
        "clouds:\n"
        "  rhosp17:\n    auth:\n      auth_url: https://keystone.rhosp17.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
        "  rhoso18:\n    auth:\n      auth_url: https://keystone.rhoso18.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
    )
    calls: list = []
    src = source_cloud(calls)
    for volume in src.volumes.values():
        volume.host = NFS_SRC
    src.pools = [(NFS_SRC, ONTAP_NFS)]
    dst = FakeCloud("dst", calls, pools=[(NFS_DST, ONTAP_NFS)])
    for cloud in (src, dst):
        cloud.compute.hypervisors = lambda: [NS(name="compute-0")]
        cloud.network.agents = lambda **_: [NS(agent_type="OVN Controller agent", binary="ovn")]
        cloud.compute.find_server = lambda name, ignore_missing=True: None

    def connect(**kwargs):
        return src if "rhosp17" in kwargs["auth"]["auth_url"] else dst

    monkeypatch.setattr(openstack, "connect", connect)
    env = {
        "SEAMLESS_LAB_CLOUDS_YAML": str(clouds_yaml),
        "SEAMLESS_LAB_SOURCE_CLOUD": "rhosp17",
        "SEAMLESS_LAB_DEST_CLOUD": "rhoso18",
        "SEAMLESS_LAB_HANDOVER_SERVER": "srv-1",
        "SEAMLESS_LAB_BACKEND_MAP": json.dumps(
            {"ceph-ssd": "hostgroup@ontap_nfs", "ceph-hdd": "hostgroup@ontap_nfs"}
        ),
        "SEAMLESS_LAB_DESTRUCTIVE": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return src, dst, calls


async def test_lab_module_runs_end_to_end_against_the_doubles(rehearsal, tmp_path):
    src, dst, calls = rehearsal
    lab = lab_module.lab.__wrapped__(tmp_path)  # the fixture body, with the rehearsal environment
    await lab_module.test_lab_cinder_pools_are_classified_by_driver_family(lab)
    await lab_module.test_lab_handover_readiness_of_the_test_server(lab)
    assert calls == [], "pool listing and readiness change nothing"
    await lab_module.test_lab_handover_cutover_then_rollback(lab)
    ops = [(cloud, op) for cloud, op, _ in calls]
    assert ("dst", "manage") in ops and ("dst", "create_server") in ops
    # rolled back: the source server was recreated, its boot volume's delete_on_termination given
    # back once it ran (SDD §7.3, review RV35B-04)
    assert ops[-2:] == [("src", "create_server"), ("src", "dot")], ops
    assert src.deleted_by_nova == []


# ------------------------------------------------- changed-extent tracking (plan task CE13)


def _use(mode: str, reason: str | None = None, missed: int = 0) -> dict:
    query = None if mode == "scanned" else 9.6
    return {
        "/dev/vda": {
            "mode": mode,
            "reason": reason,
            "query_s": query,
            "missed": missed,
            "assumed_zero": False,
        }
    }


@pytest.fixture
def extents_rehearsal(tmp_path, monkeypatch):
    """The changed-extents lab module's environment against doubles: the fake ansible-playbook
    (its passes' extent use scripted per pass through a file the fallback commands rewrite), the
    executor tests' provider registry double, and the provider's check and VM read as the lab's
    source answers them (the handover rehearsal proves the provider code itself)."""
    from seamless_migrate.providers.openstack import OpenStackProvider
    from tests.factories import make_disk, make_vm
    from tests.fake_ansible import install
    from tests.key_support import public_key
    from tests.test_executor_ansible import CEPH_FSID, CEPH_KEY

    pytest.importorskip("openstack")
    fake = install(tmp_path / "bin")
    monkeypatch.setenv("ANSIBLE_FAKE_LOG", str(tmp_path / "ansible.log"))
    monkeypatch.setenv("ANSIBLE_FAKE_CREATED", str(tmp_path / "created.txt"))
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.01")
    extents = tmp_path / "extents.json"
    monkeypatch.setenv("ANSIBLE_FAKE_EXTENTS", f"@{extents}")
    clouds_yaml = tmp_path / "clouds.yaml"
    clouds_yaml.write_text(
        "clouds:\n"
        "  rhosp17:\n    auth:\n      auth_url: https://keystone.rhosp17.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
        "  rhoso18:\n    auth:\n      auth_url: https://keystone.rhoso18.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
    )
    key_file = tmp_path / "seamless.key"
    key_file.write_text(CEPH_KEY + "\n")  # synthetic, no key material
    # the fallback commands of LAB-CE03 rewrite what the next passes use (the lab's: a deleted base,
    # blocked monitors); the final pass reads again
    scripts = {}
    for reason in ("base_gone", "query_failed"):
        script = tmp_path / f"{reason}.json"
        script.write_text(
            json.dumps({"by_pass": {"default": _use("scanned", reason), "final": _use("read")}})
        )
        scripts[reason] = f"cp {script} {extents}"
    env = {
        "SEAMLESS_LAB_CLOUDS_YAML": str(clouds_yaml),
        "SEAMLESS_LAB_SOURCE_CLOUD": "rhosp17",
        "SEAMLESS_LAB_DEST_CLOUD": "rhoso18",
        "SEAMLESS_LAB_EXTENTS_SERVER": "srv-1",
        "SEAMLESS_LAB_CEPH_FSID": CEPH_FSID,
        "SEAMLESS_LAB_CEPH_MONITORS": "mon1.ceph.example, mon2.ceph.example:6789",
        "SEAMLESS_LAB_CEPH_KEY_FILE": str(key_file),
        "SEAMLESS_LAB_ANSIBLE_PLAYBOOK": str(fake),
        "SEAMLESS_LAB_MAPPINGS": json.dumps({"networks": {"app-net": "rhoso-app"}}),
        "SEAMLESS_LAB_PRESTAGE": "1",
        # host keys are checked by default since plan task ST7 (SDD §26.5): the lab declares its
        # conversion hosts' keys as the operator reads them out of band; here synthetic ones
        "SEAMLESS_LAB_CONVERSION_HOSTS": json.dumps(
            {
                "source": {"name": "seamless-conv-src", "ssh_host_keys": [public_key(1)]},
                "destination": {"name": "seamless-conv-dst", "ssh_host_keys": [public_key(2)]},
            }
        ),
        "SEAMLESS_LAB_DESTRUCTIVE": "1",
        "SEAMLESS_LAB_CHECKSUM_CMD": "printf 'deadbeef\\ndeadbeef\\n'",
        "SEAMLESS_LAB_CE02_PASSES": "3",
        "SEAMLESS_LAB_FALLBACK_CMDS": json.dumps(scripts),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    capabilities = {
        "admin": True,
        "storage_backends": [
            {
                "pool": "hostgroup@ceph#ceph",
                "vendor": "Open Source",
                "protocol": "ceph",
                "family": "rbd",
                "ceph_fsid": CEPH_FSID,
                "ceph_pool": "volumes",
            },
        ],
    }

    async def check(self):
        return capabilities

    async def get_vm(self, source_id):
        disk = make_disk(id="vol-1", device="/dev/vda", pool="hostgroup@ceph#ceph")
        return make_vm(source_id=source_id, name="web-01", disks=[disk])

    monkeypatch.setattr(OpenStackProvider, "check", check)
    monkeypatch.setattr(OpenStackProvider, "get_vm", get_vm)
    return extents


async def test_the_changed_extents_lab_runs_through_the_real_executor_code(
    extents_rehearsal, tmp_path, monkeypatch
):
    """QASuite LAB-CE01…CE03 rehearsed (plan task CE13): each lab test drives the real
    AnsibleExecutor - the Ceph run files, the extent variables, the passes' extent use read from
    the warm state - against the fake ansible-playbook, and a check that missed a write fails the
    lab, which is what it is for."""
    from tests.fake_ansible import read_log
    from tests.lab import test_lab_changed_extents as module
    from tests.test_executor_ansible import FakeRegistry

    extents = extents_rehearsal

    def fresh(name: str) -> module.Lab:
        # the fixture body, with the rehearsal environment; each lab its own destination, so a
        # server one cutover created is not another's "already exists" (SDD §7.2)
        monkeypatch.setenv("ANSIBLE_FAKE_CREATED", str(tmp_path / name / "created.txt"))
        lab = module.lab.__wrapped__(tmp_path / name)
        lab.providers = FakeRegistry()
        return lab

    def scripted(by_pass: dict) -> None:
        extents.write_text(json.dumps({"by_pass": by_pass}))

    scripted(
        {"1": _use("scanned", "first_pass"), "default": _use("checked"), "final": _use("read")}
    )
    await module.test_lab_ce01_a_tracked_final_pass_leaves_equal_checksums(fresh("ce01"))
    scripted({"1": _use("scanned", "first_pass"), "default": _use("checked")})
    await module.test_lab_ce02_no_check_misses_a_write_under_io(fresh("ce02"))
    scripted(
        {"1": _use("scanned", "first_pass"), "default": _use("checked"), "final": _use("read")}
    )
    await module.test_lab_ce03_every_fallback_scans_in_full_and_completes(fresh("ce03"))
    # the real executor code ran each pass: the Ceph files beside secrets.yml, 0600, and the
    # modes per run
    runs = [
        r
        for r in read_log(Path(os.environ["ANSIBLE_FAKE_LOG"]))
        if "os_migrate_warm_extents_precopy" in r["vars"]
        or "os_migrate_warm_extents_final" in r["vars"]
    ]
    assert runs, "no warm run carried an extent variable"
    assert {r["vars"].get("os_migrate_warm_extents_precopy") for r in runs} == {"check", None}
    assert {r["vars"].get("os_migrate_warm_extents_final") for r in runs} == {"read", None}
    assert all(r["ceph_files"] == {"ceph.conf": "0o600", "ceph.keyring": "0o600"} for r in runs), (
        runs[0]["ceph_files"]
    )
    assert not any("key =" in json.dumps(r["vars"]) for r in runs)
    # a check that missed a write fails LAB-CE02: the lab asserts, never only prints
    scripted({"1": _use("scanned", "first_pass"), "default": _use("checked", None, 2)})
    with pytest.raises(AssertionError, match="missed"):
        await module.test_lab_ce02_no_check_misses_a_write_under_io(fresh("ce02-missed"))


# ------------------------------------------------- per-tenant credentials (plan task TC11)

#: the in-process clouds' Keystone: each its own auth URL, its catalog's identity endpoint too
SRC_KEYSTONE = "https://keystone.rhosp17.example/v3"
DST_KEYSTONE = "https://keystone.rhoso18.example/v3"
#: the servers a pre-staging's deploy reports it created, by its side's alias (SDD §6.5)
CREATED_HOSTS = {"os_migrate_conv_src": "conv-src-server", "os_migrate_conv_dst": "conv-dst-server"}


def _keystone_tenant(cloud: Any, name: str) -> NS:
    """A made-up tenant project ``name`` of ``cloud`` - its id 32 hex digits - with a Keystone user
    of its own and an application credential that user made with ``member`` and ``reader``, as the
    in-process Keystone holds it (SDD §27.11 decision 3; plan task TC11): ids and secret generated
    here."""
    tenant = NS(
        name=name,
        project_id=secrets.token_hex(16),
        user_id=secrets.token_hex(16),
        credential_id=secrets.token_hex(16),
        secret=secrets.token_urlsafe(32),
    )
    cloud.app_credentials[tenant.credential_id] = {
        "secret": tenant.secret,
        "user_id": tenant.user_id,
        "project": {
            "id": tenant.project_id,
            "name": name,
            "domain": {"id": "default", "name": "Default"},
        },
        "roles": ["member", "reader"],
    }
    return tenant


def _credential_file(path: Path, cloud: Any, credential_id: str) -> Path:
    """What `openstack application credential create -f json` prints of a credential of
    ``cloud``'s Keystone, its keys as a newer client names them (Operations.md §7.5 item 2)."""
    held = cloud.app_credentials[credential_id]
    printed = {
        "ID": credential_id,
        "Name": f"seamless-{held['project']['name']}",
        "Project ID": held["project"]["id"],
        "Roles": " ".join(held["roles"]),
        "Unrestricted": False,
        "Expires At": held.get("expires_at"),
        "Secret": held["secret"],
    }
    path.write_text(json.dumps(printed))
    return path


class _World:
    """Two in-process clouds - the source and the destination, each with its own Keystone, the
    providers' own project and two tenant projects - and what the fake playbooks behind the lab's
    recorder do to them, played as the real ones do it (SDD §27.6, plan task TC11): os-migrate
    creates a run's server, volumes, port and floating IP in the project of the run's destination
    credential - its secrets.yml's -, stops the source at a cutover and starts it again at a
    rollback, and a deploy creates each conversion host in its side's project. ``sync``, the
    clouds' ``on_request``, plays what the fake logged since the last request; ``created`` holds
    each server a cutover created, by its name and its project."""

    def __init__(self, base: Path) -> None:
        from seamless_migrate.domain.models import utcnow
        from tests.cloud_support import InProcessCloud

        def cloud(name: str, keystone: str) -> Any:
            own = {"id": secrets.token_hex(16), "name": "admin", "domain": {"id": "default"}}
            return InProcessCloud(
                base / name,
                auth_url=keystone,
                catalog={"identity": keystone},
                roles=("admin", "member", "reader"),
                project=own,
            )

        self.log = base / "ansible.log"
        self.src, self.dst = cloud("src", SRC_KEYSTONE), cloud("dst", DST_KEYSTONE)
        self.pairs: list[NS] = []
        for name in ("tenant-a", "tenant-b"):
            source, destination = _keystone_tenant(self.src, name), _keystone_tenant(self.dst, name)
            vm = {
                "id": str(uuid.uuid4()),
                "name": f"web-{name[-1]}",
                "project_id": source.project_id,
                "status": "ACTIVE",
            }
            network = {
                "id": f"net-{name}",
                "name": f"{name}-net",
                "project_id": destination.project_id,
                "mtu": 1450,
                "shared": False,
                "router:external": False,
                "status": "ACTIVE",
            }
            self.src.servers.append(vm)
            self.dst.networks.append(network)
            self.pairs.append(NS(name=name, source=source, destination=destination, vm=vm))
        # TC03's: a second credential of the first pair's destination project, made by its user,
        # that expires within minutes
        first = self.dst.app_credentials[self.pairs[0].destination.credential_id]
        self.expiring = secrets.token_hex(16)
        self.dst.app_credentials[self.expiring] = {
            **first,
            "secret": secrets.token_urlsafe(32),
            "expires_at": (utcnow() + timedelta(minutes=10)).isoformat(),
        }
        self.created: list[tuple[str, str | None]] = []
        #: the destination's sign-ins and the fake's runs when the expiring credential expired
        self.at_expiry: tuple[int, int] | None = None
        #: a cloud gone wrong: a cutover's server lands in the providers' own project
        self.misplaced = False
        self._seen = 0
        self._lock = threading.Lock()

    async def expire(self, expiry: Any) -> None:
        """TC03's wait, rehearsed: the credential expires at once - Keystone refuses it from now on
        (401), as it refuses one whose expiry passed."""
        from tests.fake_ansible import read_log

        self.dst.app_credentials[self.expiring]["sign_in"] = 401
        self.at_expiry = (len(self.dst.sign_ins), len(read_log(self.log)))

    def sync(self) -> None:
        from tests.fake_ansible import read_log

        with self._lock:
            records = read_log(self.log)
            for record in records[self._seen :]:
                self._play(record)
            self._seen = len(records)

    def _play(self, record: dict[str, Any]) -> None:
        from tests.test_executor_ansible import load_ansible_yaml

        playbook, variables = record["playbook"], record.get("vars") or {}
        held = load_ansible_yaml(record["raw"].get("secrets.yml") or "{}") or {}
        src = self._project(self.src, held.get("os_migrate_src_auth"))
        dst = self._project(self.dst, held.get("os_migrate_dst_auth"))
        if playbook == "deploy_conversion_hosts.yml":
            for cloud, side, project in ((self.src, "src", src), (self.dst, "dst", dst)):
                host = {
                    "id": CREATED_HOSTS[f"os_migrate_conv_{side}"],
                    "name": variables[f"os_migrate_{side}_conversion_host_name"],
                    "project_id": project,
                    "status": "ACTIVE",
                }
                cloud.servers.append(host)
            return
        wanted = [f["regex"] for f in variables.get("os_migrate_workloads_filter") or []]
        vm = next(
            (p.vm for p in self.pairs if any(re.search(w, p.vm["name"]) for w in wanted)), None
        )
        if vm is None:
            return
        # the warm passes' destination volume (fake_ansible's dest_volumes), a cold import's own
        disk = f"dst-{vm['id']}-vda" if playbook != "import_workloads.yml" else f"dst-{vm['id']}"
        if playbook in ("import_workloads_precopy.yml", "import_workloads.yml") and not any(
            v["id"] == disk for v in self.dst.volumes
        ):
            self.dst.volumes.append({"id": disk, "project_id": dst, "attachments": []})
        if playbook in ("import_workloads.yml", "import_workloads_cutover.yml"):
            server = f"dst-{vm['id']}"
            for volume in self.dst.volumes:
                if volume["id"] == disk:
                    volume["attachments"] = [server]
            self.dst.servers.append(
                {
                    "id": server,
                    "name": vm["name"],
                    "project_id": self.dst.project["id"] if self.misplaced else dst,
                    "status": "ACTIVE",
                    "os-extended-volumes:volumes_attached": [{"id": disk}],
                }
            )
            network = next(n["id"] for n in self.dst.networks if n["project_id"] == dst)
            port = {
                "id": f"port-{vm['id']}",
                "project_id": dst,
                "device_id": server,
                "device_owner": "compute:nova",
                "network_id": network,
                "mac_address": "fa:16:3e:00:00:01",
                "fixed_ips": [{"ip_address": "10.0.0.5", "subnet_id": f"sub-{network}"}],
                "status": "ACTIVE",
            }
            self.dst.ports.append(port)
            self.dst.floatingips.append(
                {
                    "id": f"fip-{vm['id']}",
                    "floating_ip_address": f"203.0.113.{10 + len(self.created)}",
                    "port_id": port["id"],
                    "project_id": dst,
                    "fixed_ip_address": "10.0.0.5",
                    "status": "ACTIVE",
                }
            )
            vm["status"] = "SHUTOFF"
            self.created.append((vm["name"], dst))
        elif playbook == "rollback_workloads.yml":
            gone = {
                s["id"]
                for s in self.dst.servers
                if s["name"] == vm["name"] and s.get("project_id") == dst
            }
            ports = {p["id"] for p in self.dst.ports if p["device_id"] in gone}
            self.dst.servers = [s for s in self.dst.servers if s["id"] not in gone]
            self.dst.ports = [p for p in self.dst.ports if p["id"] not in ports]
            self.dst.floatingips = [f for f in self.dst.floatingips if f["port_id"] not in ports]
            for volume in self.dst.volumes:
                volume["attachments"] = [s for s in volume["attachments"] if s not in gone]
            if variables.get("os_migrate_rollback_delete_dest_volumes"):
                ours = (f"dst-{vm['id']}", f"dst-{vm['id']}-vda")
                self.dst.volumes = [v for v in self.dst.volumes if v["id"] not in ours]
            if variables.get("os_migrate_rollback_start_source", True):
                vm["status"] = "ACTIVE"

    @staticmethod
    def _project(cloud: Any, auth: Any) -> str | None:
        """The project a run's credential acts in: an application credential's own; the
        clouds.yaml user's - the provider's own credential - for a password."""
        if not isinstance(auth, dict):
            return None
        credential = cloud.app_credentials.get(auth.get("application_credential_id") or "")
        if credential is not None:
            return credential["project"]["id"]
        return cloud.project["id"] if auth.get("password") else None


@pytest.fixture
def credentials_rehearsal(tmp_path, tmp_path_factory, monkeypatch):
    """The project credentials lab module's environment against in-process clouds (plan task
    TC11): each call builds a new _World and the lab of it - both clouds reached through
    Seamless's own connect() over the in-process keystoneauth sessions of tests/cloud_support.py,
    by the provider registry's OpenStackProviders (each step's calls and sign-ins, each check's)
    and by the lab's own reads alike -, with the fake ansible-playbook behind the lab's
    recorder."""
    import functools

    from seamless_migrate.domain.enums import ProviderRole
    from seamless_migrate.providers import registry as registry_module
    from seamless_migrate.providers.openstack import OpenStackProvider
    from tests.fake_ansible import install

    pytest.importorskip("openstack")
    fake = install(tmp_path / "bin")
    current: dict[str, _World] = {}
    made: list[tuple[Any, Any]] = []

    def connecting(provider, settings, project_id=None):
        world = current["world"]
        cloud = world.src if provider.role == ProviderRole.source else world.dst
        return cloud.connect(provider, settings, project_id)

    monkeypatch.setattr(
        registry_module,
        "OpenStackProvider",
        functools.partial(OpenStackProvider, connect_fn=connecting),
    )
    for name in (
        "SEAMLESS_LAB_DATA_DIR",
        "SEAMLESS_LAB_DEST_KIND",
        "SEAMLESS_LAB_EXPIRY_WAIT_S",
        "ANSIBLE_FAKE_CREATED",
        "ANSIBLE_FAKE_VM_NAME",
        "ANSIBLE_FAKE_SERVER_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    def make(module: Any, name: str = "lab") -> tuple[Any, _World]:
        base = tmp_path / name
        world = current["world"] = _World(base)
        clouds_yaml = base / "clouds.yaml"
        clouds_yaml.write_text(
            "clouds:\n"
            + "".join(
                f"  {cloud}:\n    auth:\n      auth_url: {keystone}\n"
                f"      username: admin\n      password: {secrets.token_urlsafe(16)}\n"
                "      project_name: admin\n      user_domain_name: Default\n"
                "      project_domain_name: Default\n    region_name: RegionOne\n"
                for cloud, keystone in (("rhosp17", SRC_KEYSTONE), ("rhoso18", DST_KEYSTONE))
            )
        )
        pairs = [
            {
                "name": pair.name,
                "source": {
                    "project": pair.source.project_id,
                    "credential": str(
                        _credential_file(
                            base / f"src-{pair.name}.json", world.src, pair.source.credential_id
                        )
                    ),
                    "server": pair.vm["id"],
                },
                "destination": {
                    "project": pair.destination.project_id,
                    "credential": str(
                        _credential_file(
                            base / f"dst-{pair.name}.json",
                            world.dst,
                            pair.destination.credential_id,
                        )
                    ),
                },
                # the fake exports each VM's port on app-net
                "mappings": {"networks": {"app-net": f"{pair.name}-net"}},
            }
            for pair in world.pairs
        ]
        tenants = base / "tenants.json"
        tenants.write_text(json.dumps(pairs))
        report = {"created": [{"alias": a, "id": i} for a, i in CREATED_HOSTS.items()]}
        env = {
            "SEAMLESS_LAB_CLOUDS_YAML": str(clouds_yaml),
            "SEAMLESS_LAB_SOURCE_CLOUD": "rhosp17",
            "SEAMLESS_LAB_DEST_CLOUD": "rhoso18",
            "SEAMLESS_LAB_TENANTS": str(tenants),
            "SEAMLESS_LAB_TC03_CREDENTIAL": str(
                _credential_file(base / "tc03.json", world.dst, world.expiring)
            ),
            "SEAMLESS_LAB_CONVERSION_HOSTS": json.dumps(
                {"source": {"name": "seamless-conv-src"}, "destination": {"name": "conv-dst"}}
            ),
            "SEAMLESS_LAB_ANSIBLE_PLAYBOOK": str(fake),
            "SEAMLESS_LAB_DESTRUCTIVE": "1",
            "ANSIBLE_FAKE_LOG": str(world.log),
            "ANSIBLE_FAKE_SLEEP": "0.01",
            "ANSIBLE_FAKE_SERVERS": json.dumps({p.vm["name"]: p.vm["id"] for p in world.pairs}),
            "ANSIBLE_FAKE_HOSTS_REPORT": json.dumps(report),
            "ANSIBLE_FAKE_PRELUDE": "1",
            "ANSIBLE_FAKE_DEST_VOLUMES": "1",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        fixture = module.lab.__wrapped__(tmp_path_factory)  # the fixture body, this environment's
        lab = next(fixture)
        made.append((lab, fixture))
        for cloud in (world.src, world.dst):
            # the lab's clouds.yaml entries and its secret store, as Seamless's connect() reads them
            cloud.settings = lab.settings
            cloud.on_request = world.sync
        lab.connect = connecting
        lab.wait_for_expiry = world.expire
        return lab, world

    yield make
    for lab, fixture in made:
        fixture.close()  # the lab fixture's teardown
        assert not lab.settings.secrets_dir.exists(), "the lab's secret store outlived its module"


async def test_the_project_credentials_lab_runs_through_the_real_provider_code(
    credentials_rehearsal, monkeypatch
):
    """QASuite LAB-TC01 and LAB-TC03 rehearsed (plan task TC11): each lab test drives the real
    provider check, the real ProviderRegistry's OpenStackProviders - Keystone sign-ins with each
    pair's application credentials, Nova's, Cinder's and Neutron's reads through keystoneauth -
    and the real AnsibleExecutor - its sign-in before a step, its conversion host keys, its run
    files - against two in-process clouds, the fake ansible-playbook behind the lab's recorder.
    Each cutover's server lands in its pair's destination project; every run signs in with its
    pair's two project credentials; an expired credential is refused before a step acts. A lab
    whose steps act with the providers' own credentials, whose cloud puts a server in another
    project, or whose steps do not sign in first fails: the lab asserts, never only prints."""
    from seamless_migrate.executors.ansible import AnsibleExecutor
    from tests.fake_ansible import read_log
    from tests.lab import test_lab_project_credentials as module
    from tests.test_executor_ansible import load_ansible_yaml

    lab, world = credentials_rehearsal(module)
    await module.test_lab_tc01_a_member_plan_lands_in_its_destination_project(lab)
    a, b = world.pairs
    # each cutover - a warm and a cold one per pair - created its server in its pair's
    # destination project, and each rollback removed it again
    assert world.created == [
        (pair.vm["name"], pair.destination.project_id) for pair in world.pairs for _ in "wc"
    ]
    hosts = set(CREATED_HOSTS.values())
    assert [s["name"] for s in world.dst.servers if s["id"] not in hosts] == []
    assert all(pair.vm["status"] == "ACTIVE" for pair in world.pairs)
    # each import ran on the VM its workload filter names, the export holding that VM alone - each
    # pair's in turn -, and the recorder read the clouds.yaml os-migrate's prelude writes after
    # every run: its check never passed for want of the file
    imports = [run for run in read_log(world.log) if run["playbook"].startswith("import_")]
    for run in imports:
        wanted = run["vars"]["os_migrate_workloads_filter"][0]["regex"]
        named = [p.vm["name"] for p in world.pairs if re.search(wanted, p.vm["name"])]
        assert run["servers"] == named and len(named) == 1, run["playbook"]
    assert {tuple(run["servers"]) for run in imports} == {(p.vm["name"],) for p in world.pairs}
    assert lab.runs() and all(run["clouds_yaml"] for run in lab.runs())

    await module.test_lab_tc03_an_expired_credential_stops_nothing(lab)
    # once it had expired, the expiring credential tried twice - the sync pass and the cutover -,
    # refused by Keystone each time before any playbook
    assert world.at_expiry is not None
    signed, ran = world.at_expiry
    assert world.dst.sign_ins[signed:].count(world.expiring) == 2
    after = read_log(world.log)[ran:]
    assert [run["playbook"] for run in after] == [
        "import_workloads_precopy.yml",
        "rollback_workloads.yml",
    ]
    # every run of both tests - each pair's pre-staging, passes, cutovers and rollbacks - held its
    # pair's two application credentials in the real executor's run files, the expiring one never
    names = {
        **{p.source.credential_id: f"{p.name} source" for p in world.pairs},
        **{p.destination.credential_id: f"{p.name} destination" for p in world.pairs},
    }
    runs = read_log(world.log)
    assert len(runs) > 10
    for run in runs:
        held = load_ansible_yaml(run["raw"]["secrets.yml"])
        who = [
            names.get(held[f"os_migrate_{side}_auth"].get("application_credential_id"), "another")
            for side in ("src", "dst")
        ]
        assert "another" not in who and who[0].split()[0] == who[1].split()[0], run["playbook"]
    # so did every sign-in at the destination but the provider's own check and the lab's reads
    pairs = {a.destination.credential_id, b.destination.credential_id}
    assert set(world.dst.sign_ins) == {"admin", world.expiring, *pairs}

    # a lab whose steps act with the providers' own credentials fails at once: the pre-staging's
    # run files sign in as the providers (TC01) ...
    with monkeypatch.context() as patched:
        own = staticmethod(lambda step, plan: (None, None))
        patched.setattr(AnsibleExecutor, "_projects", own)
        broken, _ = credentials_rehearsal(module, "own-credentials")
        with pytest.raises(AssertionError, match="signs in as"):
            await module.test_lab_tc01_a_member_plan_lands_in_its_destination_project(broken)

    # ... one where a cutover's server lands in the providers' own project fails at that cutover
    # (TC01) ...
    broken, elsewhere = credentials_rehearsal(module, "misplaced")
    elsewhere.misplaced = True
    with pytest.raises(AssertionError, match="outside its destination project"):
        await module.test_lab_tc01_a_member_plan_lands_in_its_destination_project(broken)
    assert len(elsewhere.created) == 1

    # ... and one whose steps do not sign in first runs a pass with the expired credential, its
    # connection's token cached from the check (TC03)
    async def unsigned(self, step, plan, source, destination):
        return None

    with monkeypatch.context() as patched:
        patched.setattr(AnsibleExecutor, "_sign_in", unsigned)
        broken, _ = credentials_rehearsal(module, "no-sign-in")
        with pytest.raises(AssertionError, match="ran with the expired credential"):
            await module.test_lab_tc03_an_expired_credential_stops_nothing(broken)
