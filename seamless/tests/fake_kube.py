"""A fake kubectl/oc for the operations script's cluster tests (SDD §17.2): it keeps a small cluster
state - the control plane's Deployment, PostgreSQL's StatefulSet, the ConfigMap and their pods - in
the JSON file FAKE_K8S_STATE, logs each call to STUB_LOG as the generic stubs do, and follows what
kubectl and the API server do (review 38b, read from their sources):

* a ReplicaSet scaled down deletes active pods only - a Failed (evicted) pod stays until the pod GC;
  ``kubectl wait --for=delete`` waits until every pod it selects is gone;
* ``kubectl rollout status`` of a Deployment at 0 replicas reports "successfully rolled out";
* a patch of the pod template (``rollout restart``'s annotation, new limits) makes a new template:
  ``Recreate`` ends the old pod, and the new one reads the ConfigMap as it starts; ``kubectl set
  resources`` with limits equal to the current ones patches nothing (``set_resources.go``), nor
  does a patch that leaves the template as it is;
* the API server refuses limits below the requests, a dry run (``--dry-run=server``) too, and
  applies nothing then.
"""

from __future__ import annotations

import json
from pathlib import Path

SCRIPT = r"""#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_K8S_STATE"])
st = json.loads(state_path.read_text())
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"cmd": Path(sys.argv[0]).name, "args": argv}) + "\n")
UNITS = {"m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "k": 1e3, "M": 1e6, "G": 1e9}
KINDS = {"deployment/seamless": "deployment", "statefulset/seamless-postgres": "statefulset"}
LABELS = {"seamless": "deployment", "seamless-postgres": "statefulset"}

words, flags = [], {}
args = list(argv)
while args:
    arg = args.pop(0)
    if arg in ("-n", "-l", "--selector", "-p", "--type", "-c", "-o"):
        flags[arg] = args.pop(0)
    elif arg.startswith("--") and "=" in arg:
        key, value = arg.split("=", 1)
        flags[key] = value
    else:
        words.append(arg)


def qty(value):
    number, unit = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(value)).groups()
    return float(number) * UNITS.get(unit, 1)


def new_pod(kind):
    st["serial"] += 1
    obj = st[kind]
    return {"name": f"{obj['name']}-{st['serial']}", "phase": "Running",
            "config": dict(st["configmap"]), "limits": dict(obj.get("limits", {}))}


def reconcile(kind):
    obj = st[kind]
    active = [p for p in obj["pods"] if p["phase"] == "Running"]
    for pod in active[obj["replicas"]:]:  # a scale-down deletes active pods only
        obj["pods"].remove(pod)
    while len([p for p in obj["pods"] if p["phase"] == "Running"]) < obj["replicas"]:
        obj["pods"].append(new_pod(kind))


def new_template():  # Recreate: the old pod ends, a new one reads the ConfigMap as it starts
    obj = st["deployment"]
    obj["pods"] = [p for p in obj["pods"] if p["phase"] != "Running"]
    reconcile("deployment")


def selected(kind):
    pods = st[kind]["pods"]
    field = flags.get("--field-selector", "")
    for clause in filter(None, field.split(",")):
        key, value = clause.split("!=", 1)
        assert key == "status.phase", clause
        pods = [p for p in pods if p["phase"] != value]
    return pods


def done(out="", code=0):
    state_path.write_text(json.dumps(st))
    sys.stdout.write(out)
    sys.exit(code)


def label_kind():
    selector = flags.get("-l", flags.get("--selector", ""))
    return LABELS[selector.split("=", 1)[1]]


verb = words[0]
if verb == "scale":
    kind = KINDS[words[1]]
    st[kind]["replicas"] = int(flags["--replicas"])
    reconcile(kind)
    done(f"{words[1]} scaled\n")
if verb == "get" and words[1] in KINDS:
    done(str(st[KINDS[words[1]]]["replicas"]))
if verb == "get" and words[1] == "pods":
    done("".join(f"pod/{p['name']}\n" for p in selected(label_kind())))
if verb == "wait":
    left = [p["name"] for p in selected(label_kind())]
    if left:  # nothing deletes what is left: the wait runs out
        done(f"error: timed out waiting for the condition on pods/{left[0]}\n", 1)
    done()
if verb == "rollout" and words[1] == "status":
    done(f"{words[2]} successfully rolled out\n")
if verb == "rollout" and words[1] == "restart":
    new_template()
    done("deployment.apps/seamless restarted\n")
if verb == "patch" and words[1] == "configmap/seamless-config":
    st["configmap"].update(json.loads(flags["-p"])["data"])
    done("configmap/seamless-config patched\n")
if verb == "patch" and words[1] == "deployment/seamless":
    obj = st["deployment"]
    template = json.loads(flags["-p"])["spec"]["template"]
    asked = template.get("metadata", {}).get("annotations", {})
    annotations = {**obj.get("annotations", {}), **asked}
    limits = dict(obj["limits"])
    for container in template.get("spec", {}).get("containers", []):
        assert container["name"] == "seamless", container
        limits.update(container.get("resources", {}).get("limits", {}))
    for key, request in obj["requests"].items():
        if qty(request) > qty(limits[key]):
            done(f'The Deployment "seamless" is invalid: spec.template.spec.containers[0].resources'
                 f'.requests: Invalid value: "{request}": must be less than or equal to {key} limit'
                 f' of {limits[key]}\n', 1)
    if flags.get("--dry-run") == "server":
        done("deployment.apps/seamless patched (server dry run)\n")
    if (limits, annotations) == (obj["limits"], obj.get("annotations", {})):
        done("deployment.apps/seamless patched (no change)\n")  # the same template: no rollout
    obj["limits"], obj["annotations"] = limits, annotations
    new_template()
    done("deployment.apps/seamless patched\n")
if verb == "set" and words[1] == "resources":
    obj = st["deployment"]
    limits = dict(obj["limits"])
    for pair in flags["--limits"].split(","):
        key, value = pair.split("=", 1)
        limits[key] = value
    for key, request in obj["requests"].items():
        if qty(request) > qty(limits[key]):
            done(f'The Deployment "seamless" is invalid: spec.template.spec.containers[0].resources'
                 f'.requests: Invalid value: "{request}": must be less than or equal to {key} limit'
                 f' of {limits[key]}\n', 1)
    if limits == obj["limits"]:  # set_resources.go: an empty patch is "no changes"
        done()
    obj["limits"] = limits
    new_template()
    done("deployment.apps/seamless resource requirements updated\n")
if verb == "apply":
    done("configured\n")
done()
"""


def cluster(
    replicas: int = 1, postgres: int = 1, limits: dict | None = None, evicted: bool = False
) -> dict:
    """A running stack as the manifests define it: requests 500m/1Gi, limits 4/4Gi
    (deploy/openshift/deployment.yaml), the ConfigMap's 10 and 3 (configmap.yaml); ``evicted``
    adds a Failed pod of the control plane, evicted under node pressure days before."""
    config = {"SEAMLESS_MAX_CONCURRENT_MIGRATIONS": "10", "SEAMLESS_MAX_CONCURRENT_CUTOVERS": "3"}
    limits = limits or {"cpu": "4", "memory": "4Gi"}
    pods = [
        {"name": "seamless-1", "phase": "Running", "config": dict(config), "limits": dict(limits)}
    ][:replicas]
    if evicted:
        pods.append({"name": "seamless-0", "phase": "Failed", "config": {}, "limits": {}})
    return {
        "serial": 1,
        "configmap": config,
        "deployment": {
            "name": "seamless",
            "replicas": replicas,
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": dict(limits),
            "pods": pods,
        },
        "statefulset": {
            "name": "seamless-postgres",
            "replicas": postgres,
            "pods": [
                {"name": f"seamless-postgres-{i}", "phase": "Running"} for i in range(postgres)
            ],
        },
    }


def install(bin_dir: Path, state_file: Path, state: dict) -> dict[str, str]:
    """Put the fake kubectl and oc into ``bin_dir`` and the state into ``state_file``; return the
    environment the fake needs."""
    for name in ("kubectl", "oc"):
        (bin_dir / name).write_text(SCRIPT)
        (bin_dir / name).chmod(0o755)
    state_file.write_text(json.dumps(state))
    return {"FAKE_K8S_STATE": str(state_file)}


def running(state: dict, kind: str = "deployment") -> list[dict]:
    """The running pods of ``kind`` in ``state``."""
    return [p for p in state[kind]["pods"] if p["phase"] == "Running"]
