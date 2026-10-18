"""A fake kubectl/oc for the operations script's cluster tests (SDD §17.2): it keeps a small cluster
state - the control plane's Deployment, PostgreSQL's StatefulSet, the ConfigMap and their pods - in
the JSON file FAKE_K8S_STATE, logs each call to STUB_LOG as the generic stubs do, and follows what
kubectl and the API server do (review 38b, read from their sources):

* a ReplicaSet or StatefulSet scaled down deletes active pods only - a Failed (evicted) pod stays
  until the pod GC; a deleted pod is terminating, still listed and still reached through its
  Service, until its grace period ends, which here is while ``kubectl wait --for=delete`` waits -
  that wait ends once every pod it selects is gone;
* a new control plane pod records the PostgreSQL pods there as it starts (``databases``): the
  headless Service it connects to resolves to each, and each is a database of its own;
* ``kubectl rollout status`` of a Deployment at 0 replicas reports "successfully rolled out";
* a patch of the pod template (``rollout restart``'s annotation, new limits) makes a new template:
  ``Recreate`` ends the old pod, and the new one reads the ConfigMap as it starts; ``kubectl set
  resources`` with limits equal to the current ones patches nothing (``set_resources.go``), nor
  does a patch that leaves the template as it is;
* the API server refuses limits below the requests, a dry run (``--dry-run=server``) too, and
  applies nothing then;
* a LimitRange (``limitrange``: a container's maximum) judges the pod, not the Deployment: a
  template above it patches and rolls - Recreate ends the old pod -, the new pod is never created,
  and ``rollout status`` fails once the progress deadline passes; ``rollout undo`` brings the
  previous template back, as a new one;
* ``unavailable`` set, every real (not dry-run) patch of the Deployment fails as the API server
  restarting does; ``fail_configmap_patch`` N fails the ConfigMap's Nth patch the same way;
* a rollout that passes its progress deadline (``rollout status`` with fewer running pods than
  replicas) leaves the Deployment's Progressing condition at ProgressDeadlineExceeded: a later
  ``rollout status`` fails at once (kubectl's rollout_status.go), a new template resets it, an
  undo keeps it (the controller's sync.go) - ``wait --for=condition=Available`` judges the pods.
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
    obj = st[kind]
    limits = dict(obj.get("limits", {}))
    for key, most in st.get("limitrange", {}).items():  # LimitRanger admits the pod or none
        if key in limits and qty(limits[key]) > qty(most):
            obj["failed_create"] = (f"maximum {key} usage per Container is {most}, but limit is "
                                    f"{limits[key]}")
            return None
    st["serial"] += 1
    pod = {"name": f"{obj['name']}-{st['serial']}", "phase": "Running",
           "config": dict(st["configmap"]), "limits": limits}
    if kind == "deployment":
        pod["databases"] = [p["name"] for p in st["statefulset"]["pods"] if p["phase"] == "Running"]
    return pod


def active(kind):
    return [p for p in st[kind]["pods"] if p["phase"] == "Running" and not p.get("terminating")]


def reconcile(kind):
    obj = st[kind]
    for pod in active(kind)[obj["replicas"]:]:  # a scale-down deletes active pods only
        pod["terminating"] = True
    while len(active(kind)) < obj["replicas"]:
        pod = new_pod(kind)
        if pod is None:
            break
        obj["pods"].append(pod)


def new_template(keep_progress=False):  # Recreate: the old pod ends, a new one reads the ConfigMap
    obj = st["deployment"]
    obj.pop("failed_create", None)
    if not keep_progress:  # a new template progresses again; an undo keeps the deadline
        obj.pop("deadline_exceeded", None)
    obj["pods"] = [p for p in obj["pods"] if p["phase"] != "Running"]
    reconcile("deployment")


def selected(kind):
    pods = st[kind]["pods"]
    field = flags.get("--field-selector", "")
    for clause in filter(None, field.split(",")):
        key, value = clause.split("!=", 1)
        assert key in ("status.phase", "metadata.name"), clause
        pods = [p for p in pods if p["phase" if key == "status.phase" else "name"] != value]
    return pods


def done(out="", code=0):  # kubectl writes an error to stderr
    state_path.write_text(json.dumps(st))
    (sys.stderr if code else sys.stdout).write(out)
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
# -o go-template={{index .data "KEY"}}: the value, or "<no value>" for a key the ConfigMap lacks
if verb == "get" and words[1] == "configmap/seamless-config":
    key = re.search(r'index \.data "([^"]+)"', flags["-o"]).group(1)
    done(st["configmap"].get(key, "<no value>"))
if verb == "get" and words[1] == "pods":
    done("".join(f"pod/{p['name']}\n" for p in selected(label_kind())))
if verb == "wait" and flags.get("--for") == "condition=Available":
    if len(active("deployment")) < st["deployment"]["replicas"]:
        done("error: timed out waiting for the condition on deployments/seamless\n", 1)
    st["deployment"].pop("deadline_exceeded", None)  # the controller marks it available
    done("deployment.apps/seamless condition met\n")
if verb == "wait":
    for pod in selected(label_kind()):
        if pod.get("terminating"):  # its grace period ends
            st[label_kind()]["pods"].remove(pod)
    left = [p["name"] for p in selected(label_kind())]
    if left:  # nothing deletes what is left: the wait runs out
        done(f"error: timed out waiting for the condition on pods/{left[0]}\n", 1)
    done()
if verb == "rollout" and words[1] == "status":
    kind = KINDS[words[2]]
    if st[kind].get("deadline_exceeded") or len(active(kind)) < st[kind]["replicas"]:
        st[kind]["deadline_exceeded"] = True
        done(f'error: deployment "seamless" exceeded its progress deadline\n', 1)
    done(f"{words[2]} successfully rolled out\n")
if verb == "rollout" and words[1] == "restart":
    obj = st["deployment"]
    obj.setdefault("history", []).append(
        {"limits": dict(obj["limits"]), "annotations": dict(obj.get("annotations", {}))})
    new_template()
    done("deployment.apps/seamless restarted\n")
if verb == "rollout" and words[1] == "undo":
    obj = st["deployment"]
    previous = obj.get("history", []).pop()
    obj["limits"], obj["annotations"] = previous["limits"], previous["annotations"]
    new_template(keep_progress=True)
    done("deployment.apps/seamless rolled back\n")
if verb == "patch" and words[1] == "configmap/seamless-config":  # a merge patch: null removes
    st["configmap_patches"] = st.get("configmap_patches", 0) + 1
    if st.get("fail_configmap_patch") == st["configmap_patches"]:
        done("Error from server (InternalError): an admission webhook timed out\n", 1)
    for key, value in json.loads(flags["-p"])["data"].items():
        if value is None:
            st["configmap"].pop(key, None)
        else:
            st["configmap"][key] = value
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
    if st.get("unavailable"):
        done("Error from server (ServiceUnavailable): the server is currently unable to handle the "
             "request\n", 1)
    if (limits, annotations) == (obj["limits"], obj.get("annotations", {})):
        done("deployment.apps/seamless patched (no change)\n")  # the same template: no rollout
    obj.setdefault("history", []).append(
        {"limits": dict(obj["limits"]), "annotations": dict(obj.get("annotations", {}))})
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
