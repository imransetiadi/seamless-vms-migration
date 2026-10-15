"""The Kubernetes overlay replaces what is OpenShift-specific (SDD §17, Security.md C9).

``deploy/kubernetes`` is a kustomize overlay of ``deploy/openshift``. CI renders it and asserts the
result (``.github/workflows/ci.yml``, job manifests); this test reads the overlay's patches so the
control-plane suite catches a regression without kustomize.
"""

from pathlib import Path

import yaml

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
OPENSHIFT_NETWORKS = {
    "10.128.0.0/14",
    "172.30.0.0/16",
}  # OpenShift's default pod and service networks
VANILLA_NETWORKS = {
    "10.244.0.0/16",
    "10.96.0.0/12",
}  # kubeadm/kind default pod and service networks


def _overlay() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "kubernetes" / "kustomization.yaml").read_text())


def _ops(policy: str) -> list[dict]:
    """The JSON patch operations the overlay applies to the NetworkPolicy ``policy``."""
    ops: list[dict] = []
    for patch in _overlay()["patches"]:
        target = patch.get("target", {})
        if target.get("kind") == "NetworkPolicy" and target.get("name") == policy:
            ops += yaml.safe_load(patch["patch"])
    return ops


def _value(ops: list[dict], path: str):
    found = [op["value"] for op in ops if op["path"] == path and op["op"] in ("add", "replace")]
    assert found, f"no patch sets {path}"
    return found[-1]


def test_external_egress_excepts_the_cluster_networks_not_openshifts():
    """In-cluster pods stay unreachable on SSH/HTTPS/OpenStack ports (vanilla pod and service
    networks), and OpenShift's ranges — ordinary private addresses elsewhere — are not blocked."""
    excepted = set(
        _value(_ops("seamless-allow-egress-external"), "/spec/egress/0/to/0/ipBlock/except")
    )
    assert VANILLA_NETWORKS <= excepted
    assert "169.254.0.0/16" in excepted  # link-local, cloud metadata endpoints
    assert not excepted & OPENSHIFT_NETWORKS


def test_kube_api_egress_keeps_the_pod_and_service_networks_out():
    """6443 reaches the API server on node addresses, never a pod or a Service of the cluster."""
    excepted = set(
        _value(_ops("seamless-allow-egress-kube-api"), "/spec/egress/0/to/0/ipBlock/except")
    )
    assert VANILLA_NETWORKS <= excepted


def test_postgres_image_is_pinned_by_digest():
    images = {image["name"]: image for image in _overlay()["images"]}
    postgres = images["registry.redhat.io/rhel9/postgresql-16"]
    assert postgres["newName"] == "quay.io/sclorg/postgresql-16-c9s"
    assert postgres.get("digest", "").startswith("sha256:") and len(postgres["digest"]) == 71
    assert "newTag" not in postgres


def _deployment() -> dict:
    manifests = yaml.safe_load_all((ROOT / "deploy" / "openshift" / "deployment.yaml").read_text())
    return next(m for m in manifests if m and m["kind"] == "Deployment")


def test_postgres_gets_a_writable_home_under_its_read_only_root():
    """The SCL image's start script writes ``$HOME/passwd`` and its generated config under
    ``HOME=/var/lib/pgsql`` for every UID (OpenShift's arbitrary one and the overlay's 26): with
    ``readOnlyRootFilesystem`` that path must be an emptyDir, or the database exits 1 on start
    ("/var/lib/pgsql/passwd: Read-only file system"). The data PVC stays nested at
    ``/var/lib/pgsql/data`` (QASuite §14.5 reproduces both with the pinned image)."""
    docs = yaml.safe_load_all(
        (ROOT / "deploy" / "openshift" / "postgres-statefulset.yaml").read_text()
    )
    (sts,) = [d for d in docs if d and d.get("kind") == "StatefulSet"]
    pod = sts["spec"]["template"]["spec"]
    (container,) = [c for c in pod["containers"] if c["name"] == "postgres"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
    empty = {v["name"] for v in pod["volumes"] if "emptyDir" in v}
    claims = {t["metadata"]["name"] for t in sts["spec"]["volumeClaimTemplates"]}
    assert mounts.get("/var/lib/pgsql") in empty
    assert mounts.get("/var/lib/pgsql/data") in claims
    for scratch in ("/var/run/postgresql", "/tmp"):
        assert mounts.get(scratch) in empty


def test_security_md_describes_the_api_token_the_deployment_mounts():
    """Security.md's secrets rules and hardening table say what deployment.yaml does with the
    control plane's Kubernetes API token: mounted for the dashboard's Secret store (R-15)."""
    mounted = _deployment()["spec"]["template"]["spec"]["automountServiceAccountToken"]
    text = (ROOT / "docs" / "Security.md").read_text(encoding="utf-8")
    [row] = [line for line in text.splitlines() if line.startswith("| Kubernetes API token |")]
    assert f"`automountServiceAccountToken: {str(mounted).lower()}`" in row, row
    if mounted:
        assert "service account has no Kubernetes API token" not in " ".join(text.split())


def test_external_egress_reaches_the_verification_ports_the_plan_form_proposes():
    """SDD §17, §7.5: a `tcp:<port>` check the policy blocks times out and fails the verification,
    so auto_rollback would roll back a migration that worked: the dashboard's defaults pass."""
    import re

    policies = yaml.safe_load_all(
        (ROOT / "deploy" / "openshift" / "networkpolicy.yaml").read_text(encoding="utf-8")
    )
    external = next(
        p for p in policies if p and p["metadata"]["name"] == "seamless-allow-egress-external"
    )
    allowed = {
        port["port"]
        for rule in external["spec"]["egress"]
        for port in rule.get("ports", [])
        if port.get("protocol", "TCP") == "TCP"
    }
    form = (ROOT / "dashboard" / "src" / "components" / "PlanCreateDialog.tsx").read_text(
        encoding="utf-8"
    )
    proposed = {
        int(port)
        for field in ("tcpPorts", "windowsTcpPorts")
        for port in re.findall(r"\d+", re.search(rf"{field}: '([^']*)'", form).group(1))
    }
    assert proposed == {22, 3389}
    assert proposed <= allowed, sorted(proposed - allowed)


def test_the_secret_recipe_keeps_the_database_password_off_command_lines():
    """Security.md §7.2: never a secret on a command line (`ps`, /proc and shell history keep
    them). The recipe feeds the database Secret through stdin from the printf builtin."""
    recipe = (ROOT / "deploy" / "openshift" / "secret-example.yaml").read_text(encoding="utf-8")
    lines = [line.lstrip("#").strip() for line in recipe.splitlines() if line.startswith("#")]
    assert not [line for line in lines if "from-literal" in line and "PGPW" in line]
    assert any("--from-env-file=/dev/stdin" in line for line in lines)
    assert any(line.startswith("printf") and "$PGPW" in line for line in lines)


def test_the_control_plane_pod_reaps_the_processes_ansible_leaves():
    """SDD §17: Ansible's ssh ControlPersist masters outlive their playbook and are re-parented
    to PID 1; with a shared process namespace that is the pod's pause process, which reaps them,
    as `init: true` does in Compose - `seamless serve` as PID 1 would leave them as zombies."""
    docs = yaml.safe_load_all((ROOT / "deploy" / "openshift" / "deployment.yaml").read_text())
    (deployment,) = [d for d in docs if d and d.get("kind") == "Deployment"]
    assert deployment["spec"]["template"]["spec"].get("shareProcessNamespace") is True
    compose = yaml.safe_load((ROOT / "deploy" / "compose" / "compose.yaml").read_text())
    assert compose["services"]["seamless"].get("init") is True


def test_the_route_tells_browsers_to_keep_to_https():
    """SDD §17, Security.md R-05: the OpenShift router adds no HSTS by default and the pod serves
    plain HTTP behind the edge Route, so the Route asks for it (at least a year)."""
    import re

    route = yaml.safe_load((ROOT / "deploy" / "openshift" / "route.yaml").read_text())
    hsts = route["metadata"]["annotations"].get("haproxy.router.openshift.io/hsts_header", "")
    age = re.search(r"max-age=(\d+)", hsts)
    assert age and int(age.group(1)) >= 31536000, hsts
    assert route["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"


def _compose_files(**make_vars: str) -> list[str]:
    """The Compose files `make seamless-up` passes for these Make variables (a dry run)."""
    import re
    import subprocess

    args = ["make", "-n", "--no-print-directory", "seamless-up"]
    args += [f"{name}={value}" for name, value in make_vars.items()]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if " up -d " in ln)
    return [Path(f).name for f in re.findall(r" -f (\S+)", line)]


def test_a_linux_docker_engine_maps_host_docker_internal_to_the_host():
    """SDD §17.1: only a Linux machine's own Docker Engine gets compose.linux-host.yaml; on Colima
    and Docker Desktop the name already reaches the developer's machine (host-gateway would be
    their VM)."""
    override = "compose.linux-host.yaml"
    linux = dict(SEAMLESS_HOST_OS="Linux", SEAMLESS_DOCKER_CONTEXT="default")
    assert override in _compose_files(**linux)
    assert override not in _compose_files(**{**linux, "SEAMLESS_DOCKER_CONTEXT": "colima-x"})
    assert override not in _compose_files(**{**linux, "SEAMLESS_HOST_OS": "Darwin"})
    assert override not in _compose_files(**{**linux, "SEAMLESS_ENGINE": "podman"})
    mapped = yaml.safe_load((ROOT / "deploy" / "compose" / override).read_text())
    hosts = mapped["services"]["seamless"]["extra_hosts"]
    assert hosts == ["host.docker.internal:host-gateway"]


# --- the optional backup overlay (SDD §17, Security.md §12 "restore-tested", audit D-06) ---

BACKUP = ROOT / "deploy" / "openshift" / "backup"


def _backup_docs(name: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all((BACKUP / name).read_text()) if doc]


def _backup_pod() -> dict:
    (cron,) = _backup_docs("cronjob.yaml")
    assert cron["kind"] == "CronJob"
    return cron["spec"]["jobTemplate"]["spec"]["template"]


def test_backup_cronjob_dumps_the_database_under_its_secret_and_the_restricted_context():
    """One nightly pg_dump of the system of record into its own PVC, with the database's credentials
    from the seamless-db Secret and the security context every workload of the namespace runs under
    (restricted PSA): never as root, read-only root, no capabilities, no SA token."""
    (cron,) = _backup_docs("cronjob.yaml")
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert cron["spec"]["schedule"].count(" ") == 4
    pod = _backup_pod()["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (container,) = pod["containers"]
    context = container["securityContext"]
    assert context["readOnlyRootFilesystem"] is True
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert container["resources"]["limits"]
    refs = {
        env["name"]: env["valueFrom"]["secretKeyRef"]
        for env in container["env"]
        if "valueFrom" in env
    }
    assert {ref["name"] for ref in refs.values()} == {"seamless-db"}
    keys = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    assert {ref["key"] for ref in refs.values()} == keys
    script = container["args"][0]
    assert "pg_dump" in script and "-Fc" in script and "BACKUP_KEEP_DAYS" in script
    claims = {
        v["persistentVolumeClaim"]["claimName"]
        for v in pod["volumes"]
        if "persistentVolumeClaim" in v
    }
    assert claims == {"seamless-backups"}
    (pvc,) = _backup_docs("pvc.yaml")
    assert pvc["kind"] == "PersistentVolumeClaim" and pvc["metadata"]["name"] == "seamless-backups"


def test_backup_pods_are_admitted_by_their_own_network_policies():
    """default-deny-all covers every pod of the namespace, and the database admits the control plane
    only (seamless-postgres-allow-from-seamless): the overlay carries the egress rule of the backup
    pods and the database's ingress rule for them, matched by the CronJob template's pod label."""
    label = _backup_pod()["metadata"]["labels"]["app.kubernetes.io/name"]
    policies = _backup_docs("networkpolicy.yaml")
    egress = [p for p in policies if p["spec"]["policyTypes"] == ["Egress"]]
    ingress = [p for p in policies if p["spec"]["policyTypes"] == ["Ingress"]]
    assert len(egress) == 1 and len(ingress) == 1
    assert egress[0]["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == label
    (rule,) = egress[0]["spec"]["egress"]
    postgres = {"app.kubernetes.io/name": "seamless-postgres"}
    assert rule["to"][0]["podSelector"]["matchLabels"] == postgres
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]
    assert ingress[0]["spec"]["podSelector"]["matchLabels"] == postgres
    (rule,) = ingress[0]["spec"]["ingress"]
    assert rule["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == label
    kustomization = yaml.safe_load((BACKUP / "kustomization.yaml").read_text())
    assert sorted(kustomization["resources"]) == ["cronjob.yaml", "networkpolicy.yaml", "pvc.yaml"]
    assert kustomization["namespace"] == "seamless-migrate"


def test_backup_runbook_names_the_restore_order_and_the_drill():
    """Security.md §12 asks for restore-tested backups: the README beside the overlay is the
    runbook, and restore-job.yaml the one-off restore it describes."""
    text = (BACKUP / "README.md").read_text()
    for needle in ("pg_restore", "restore-job.yaml", "seamless-data", "drill", "Secrets"):
        assert needle in text, needle
    (job,) = _backup_docs("restore-job.yaml")
    assert job["kind"] == "Job"
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert "pg_restore" in container["args"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
