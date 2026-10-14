"""Every loop of a role over workloads labels its items with an id (Seamless Migrate review
RV7-02): Ansible prints each loop item - or its label - on the task's result and include lines,
and the control plane classifies a failure as transient or permanent by the playbook output's
tail. A workload there - its name, metadata, tags, a boot image's properties such as Nova's
os_shutdown_timeout - is the tenant's data, and it must not decide whether a step is retried."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import glob
import os

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _tasks(node):
    """Every task of a tasks file, those inside block, rescue and always included."""
    for task in node or []:
        if isinstance(task, dict):
            yield task
            for key in ("block", "rescue", "always"):
                for inner in _tasks(task.get(key)):
                    yield inner


def _workload_loops():
    for path in sorted(glob.glob(os.path.join(ROOT, "roles", "*", "tasks", "*.yml"))):
        with open(path, encoding="utf-8") as f:
            for task in _tasks(yaml.safe_load(f)):
                if "workloads" in str(task.get("loop", "")):
                    yield os.path.relpath(path, ROOT), task


def test_the_roles_loop_over_workloads():
    assert len(list(_workload_loops())) >= 5


def test_every_loop_over_workloads_labels_its_items_with_an_id():
    unlabelled = []
    for path, task in _workload_loops():
        label = str((task.get("loop_control") or {}).get("label", ""))
        if "id" not in label or "name" in label:
            unlabelled.append("%s: %s (label %r)" % (path, task.get("name"), label))
    assert unlabelled == []
