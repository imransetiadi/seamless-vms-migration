"""The Kubernetes overlay replaces what is OpenShift-specific (SDD §17, Security.md C9).

``deploy/kubernetes`` is a kustomize overlay of ``deploy/openshift``. CI renders it and asserts the
result (``.github/workflows/ci.yml``, job manifests); this test reads the overlay's patches so the
control-plane suite catches a regression without kustomize.
"""

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
