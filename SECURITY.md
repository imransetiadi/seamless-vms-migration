# Security policy

The full policy — threat model, controls, incident response and disclosure terms — is
[docs/Security.md](docs/Security.md); this file is the short version GitHub shows.

## Supported versions

The latest 0.1.x release and the `main` branch receive security fixes.

## Reporting a vulnerability

**Please do not open a public issue for a vulnerability.** Report it privately through GitHub's
private vulnerability reporting for this repository
([Security → Report a vulnerability](https://github.com/imransetiadi/seamless-vms-migration/security/advisories/new)).

Include: the affected version or commit, the component (control plane, dashboard, collection,
deployment), the impact, reproduction steps or a proof of concept against synthetic data, and any
suggested fix. Never include real credentials, customer data, real hostnames or console output.

## What to expect

Acknowledgement within 3 business days and triage within 7. Fix or mitigation by severity: critical
within 7 days, high within 30, medium within 90, low in the next release. Disclosure is coordinated,
by default 90 days after the report or sooner once a fix ships; reporters are credited if they wish.

## Scope and safe harbor

In scope: this repository's code, container images, manifests and the defaults they ship.
Vulnerabilities in OpenStack, vCenter, upstream os-migrate, Jev, agentmemory or their dependencies
should also be reported to those projects. Good-faith research on your own deployments with synthetic
data, without degrading others' service or accessing others' data, will not be pursued.
