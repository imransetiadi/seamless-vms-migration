# Seamless Migrate — Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Product | Seamless Migrate |
| Release | 0.1.0 (MVP) |
| Date | 2026-10-08 |
| Status | Approved; amended 2026-10-09 (NetApp ONTAP handover, Kubernetes and Podman, guest OS catalog) |
| Design | [SDD.md](SDD.md) |

## 1. Problem statement

Organizations running **Red Hat OpenStack Platform 17.1**, **community OpenStack** or **VMware
vSphere** must move their virtual machines to **Red Hat OpenStack Services on OpenShift (RHOSO)
18.0**. RHOSP 17.1 is the last TripleO-based release; RHOSO 18.0 runs the OpenStack control plane
on OpenShift. In-place *adoption* is not always possible (hardware refresh, new storage, network
redesign, community or VMware sources), so tenants' workloads must be migrated side by side.

Today's tooling forces painful trade-offs:

* **os-migrate** (the base of this product) exports/imports tenant resources reliably, but its
  workload copy is *cold*: the VM is down for the whole disk copy. A 1 TiB VM over a 1 Gbit/s link
  means hours of downtime.
* VMware warm migration exists (vmware-migration-kit with CBT) but there is no single place to plan
  waves, estimate downtime, approve cutovers, roll back, and report — across both OpenStack and
  VMware sources.
* Migration knowledge (what failed, how it was fixed, how long cutovers really took) lives in
  people's heads and chat logs.

## 2. Vision

> One control plane that moves any VM from OpenStack or VMware into RHOSO 18.0 with **minutes of
> downtime**, predictable before it starts, approved by the right people, reversible until it is
> finalized — and that gets smarter with every migration.

## 3. Goals and non-goals

### Goals (0.1.0)

| ID | Goal | Measure |
|---|---|---|
| G1 | Minimize downtime | In the reference lab (Performance.md): warm VMs whose scan term (SDD §9.1) is ≤ 330 s — e.g. largest disk ≤ 100 GiB and total ≤ 400 GiB at the default 500 MiB/s per stream × 4 streams — cut over in ≤ 10 min at p90, and a warm cutover beats the cold copy whenever more than ~25 % of the disk is used (scan at 500 MiB/s vs transfer at 125 MiB/s); handover-eligible VMs cut over in ≤ 6 min regardless of size. Warm downtime is bounded by fixed overhead (~4.5 min) + largest-disk scan (≈ 3.4 min per 100 GiB at 500 MiB/s); 0.2.0 removes the scan term (SDD §20 D1) |
| G2 | Predictability | Downtime estimate within ±30 % of actual for ≥ 80 % of migrations once one delta pass has calibrated change rate and scan throughput (SDD §9.1 calibration) |
| G3 | Safety | 100 % of cutovers reversible until finalize; no source data deleted without an explicit finalize by an approver |
| G4 | One workflow for all sources | RHOSP 17.1, community OpenStack and VMware handled by the same plan/wave/approval model |
| G5 | Operability | Every action audited; live progress in the dashboard; Prometheus metrics |
| G6 | Assisted decisions | Jev-based advisor for strategy ties, wave classification and post-cutover review; agentmemory recall of similar past incidents |

### Non-goals (0.1.0)

* In-place adoption of a 17.1 control plane (Red Hat adoption procedure).
* Zero-downtime live migration across clouds.
* Migrating Heat stacks, Octavia load balancers, Manila shares, Designate zones (reported as
  findings; manual runbooks).
* Network cut-over automation (DNS/FIP/BGP) — documented hooks only.
* Hyper-V, Nutanix, KVM-without-OpenStack sources.

## 4. Personas

| Persona | Needs | Primary surfaces |
|---|---|---|
| **Migration architect** (Rina) | Inventory, readiness, strategy per VM, wave plan, downtime estimates to negotiate windows | Plans, Inventory, estimates, CLI |
| **Platform operator** (Bayu) | Run waves, watch progress, react to failures, retry/rollback | Dashboard migration view, events, CLI |
| **Change approver** (Sari) | Approve cutovers inside the change window; see risk and evidence; finalize | Approve/Cutover/Finalize actions, audit log |
| **Application owner** (Dimas) | Know when their VM is down and for how long; confirm it works afterwards | Plan view (read-only), notifications via events |
| **Security officer** | Credentials never leak; every action attributable; AI use is bounded | Security.md, audit trail, RBAC |

## 5. Key user journeys

1. **Warm RHOSP 17.1 → RHOSO** — Rina registers both clouds, selects 40 VMs, maps networks/
   flavors/volume types, auto-plans waves (pilot first), validates (findings + estimates), starts.
   VMs pre-copy while running; when converged they wait in *awaiting cutover* with keep-warm
   syncs. In the Saturday window Sari approves; each VM stops, the final delta syncs, the VM boots
   on RHOSO, verification passes; downtime is shown per VM.
2. **Shared storage handover** — the new RHOSO cloud uses the existing external Ceph cluster or the
   same NetApp ONTAP SVM (NFS, iSCSI or FC). Volumes are handed over by Cinder unmanage/manage;
   downtime is a few minutes regardless of size.
3. **VMware exit** — Bayu adds vCenter; VMs with CBT run warm via vmware-migration-kit; those
   without CBT are flagged (enable CBT or accept cold).
4. **Failure and rollback** — a cutover fails verification (no login prompt, port 22 closed). The
   migration rolls back automatically: the RHOSO instance is removed (volumes kept), the source VM
   restarts; similar past incidents from agentmemory are shown with their fixes; Bayu fixes the
   mapping and retries.
5. **Assisted planning** — for VMs without tags, the advisor classifies workloads (stateless web,
   middleware, infrastructure, database, legacy OS) to build risk-ordered waves; when two
   strategies tie, it recommends one with evidence, and the operator sees why.

## 6. Functional requirements

Priority: **M** = must (0.1.0), **S** = should (0.1.0 if feasible), **C** = could (later).

| ID | Requirement | P | Acceptance criteria |
|---|---|---|---|
| FR-01 | Register source providers (OpenStack/RHOSP 17.1, VMware) and a RHOSO destination; check connectivity and capabilities | M | `POST /providers/{id}/check` returns status and capabilities (admin, compute microversion, OVN, CBT support) |
| FR-02 | Discover inventory (VMs, disks, NICs, flavors, power state, guest OS, CBT, snapshots) | M | Inventory table lists VMs with disk sizes, guest OS (SDD §9.5) and readiness flags |
| FR-03 | Create plans with VM selection, mappings (network, flavor, volume type, project), SLO, approval, window | M | Plan persisted; editable while draft/validated |
| FR-04 | Pre-flight validation with a documented finding catalog (blocker/warning/info) | M | All SDD §9.3 codes implemented and unit-tested |
| FR-05 | Downtime/duration estimate per VM and strategy | M | Estimator formulas SDD §9.1; shown in plan and migration views |
| FR-06 | Automatic strategy selection with eligibility rules; per-VM override | M | Ineligible strategies can never be selected; override rejected if ineligible |
| FR-07 | Warm OpenStack migration (snapshot pre-copy + delta sync) | M | Source VM keeps running until cutover; final pass transfers only changed chunks; checksums verified |
| FR-08 | Cold OpenStack migration via os-migrate | M | Existing playbooks orchestrated per VM |
| FR-09 | Storage handover for storage both clouds reach: Ceph RBD and NetApp ONTAP (NFS, iSCSI, FC) | S | Unmanage/manage flow with journaled rollback; storage references per driver family (SDD §7.3.1) |
| FR-10 | VMware cold and warm (CBT) via vmware-migration-kit | M | Executor drives `cbt_sync`/`cutover` flags |
| FR-11 | Waves with dependencies, parallelism limits and a pilot wave | M | Wave N+1 starts only when wave N is complete |
| FR-12 | Cutover gating: approval, change window, auto-cutover, concurrency cap | M | SDD §5.4 rules enforced and tested |
| FR-13 | Keep-warm delta syncs while awaiting cutover | S | Pass runs after `keep_warm_interval_s` |
| FR-14 | Post-cutover verification (server ACTIVE, ports, TCP probes, console patterns) | M | Failing verification triggers rollback when enabled |
| FR-15 | Rollback until finalize; finalize requires approver and typed confirmation | M | Source never deleted before finalize |
| FR-16 | Resume after control-plane restart | M | In-flight migrations continue from checkpoint |
| FR-17 | REST API + SSE live events | M | SDD §12 contract; SSE resumable |
| FR-18 | Dashboard (overview, plans, migration detail, providers, inventory, events, advisor) | M | All routes in SDD §16; accessibility AA |
| FR-19 | RBAC (viewer/operator/approver/admin) and audit trail | M | Each route enforces min role; mutating calls audited |
| FR-20 | Jev advisor (strategy tie-break, workload classification, verification review, console screening) | M | Advisor bounded per SDD §14.2; works with Jev off |
| FR-21 | agentmemory lessons and similar-incident recall | M | Failures/completions recorded; hits attached to failed migrations |
| FR-22 | CLI for plan apply/validate/start/status/estimate and token management | M | SDD §15 commands |
| FR-23 | Prometheus metrics | S | SDD §18 metric names |
| FR-24 | Demo mode with simulated providers and executor | M | `seamless serve --demo` exercises every phase including rollback |
| FR-25 | Deploy on OpenShift, Kubernetes and Docker Compose (Docker/Colima or Podman) with PostgreSQL | M | Manifests in `deploy/openshift` and its overlay `deploy/kubernetes`, compose stack in `deploy/compose` |
| FR-26 | Changed-extent tracking to remove the scan floor: Ceph-direct `rbd diff` and hypervisor-assisted libvirt checkpoints | C | 0.2.0 |
| FR-27 | HA control plane (leader election) | C | 0.2.0 |
| FR-28 | Network cut-over hooks (DNS/FIP/BGP) | C | 0.3.0 |

## 7. Non-functional requirements

| ID | Category | Requirement |
|---|---|---|
| NFR-01 | Downtime | See G1; downtime clock measured from source stop to verified boot |
| NFR-02 | Throughput | Per-disk delta sync ≥ 400 MiB/s local scan per side on a 4 vCPU conversion host; WAN-bound transfer saturates ≥ 90 % of a 10 Gbit/s link with ≥ 4 parallel disks |
| NFR-03 | Scale | 1,000 VMs per plan; 10 concurrent migrations and 3 concurrent cutovers by default (configurable) |
| NFR-04 | Reliability | Idempotent steps; at-least-once execution with safe retries; no data loss on control-plane crash |
| NFR-05 | Integrity | End-to-end manifest digest per disk per pass (BLAKE2b-128 over chunk digests) |
| NFR-06 | Security | No credentials in DB/API/logs; TLS to all endpoints; NBD only on loopback or SSH; RBAC; audit (Security.md) |
| NFR-07 | AI safety | AI outputs advisory, bounded, logged with source and confidence; untrusted text screened; secrets redacted |
| NFR-08 | Usability | Dashboard WCAG 2.2 AA; dense ops layout; live updates ≤ 2 s latency |
| NFR-09 | Observability | Structured logs, events, metrics; every migration has a complete timeline |
| NFR-10 | Portability | Runs on OpenShift 4.16+, Kubernetes 1.28+, Docker/Podman, macOS dev via Colima; Python 3.11+, Node 22 |
| NFR-11 | Maintainability | Unit test coverage ≥ 85 % for control-plane core modules; collection additions unit-tested |

## 8. Success metrics

* Median warm cutover downtime ≤ 10 min for VMs whose largest disk ≤ 100 GiB and total ≤ 400 GiB;
  p95 ≤ 26 min for largest disk ≤ 500 GiB at the NFR-02 floor of 400 MiB/s per stream; storage handover ≤ 6 min regardless of size (0.1.0 model, SDD §9.1).
  0.2.0 target with changed-extent tracking (SDD §20 D1): ≤ 5 min median independent of disk size.
* ≥ 95 % of migrations complete without manual intervention after pilot wave.
* 0 incidents of source data loss.
* Estimate accuracy (G2).
* Time to plan a 100-VM migration (inventory → validated plan) ≤ 1 hour.

## 9. Release plan

| Release | Scope |
|---|---|
| **0.1.0 (this delivery)** | FR-01…FR-25: collection warm path + hardening, control plane, dashboard, Jev + agentmemory integration, demo mode, OpenShift + Compose/Colima deployment, docs |
| 0.2.0 | Ceph-direct incremental diff, HA (PostgreSQL leader election), per-tenant credentials, notifications (email/Slack webhooks) |
| 0.3.0 | Network cut-over hooks (DNS, FIP re-mapping, OVN BGP), Octavia/Heat migration helpers |
| 1.0.0 | Certified on RHOSO 18.0.x FRs, scale test 5,000 VMs, signed releases |

## 10. Dependencies and assumptions

* Source/destination OpenStack APIs reachable from the control plane; conversion hosts reachable
  over SSH from the control plane and from each other (dst → src).
* Tenant credentials (or admin with role assignment) on both clouds; admin required for storage
  handover and some pre-staging resources.
* VMware: VDDK available on the RHOSO conversion host; CBT enabled for warm.
* Jev requires a provider API key (TypeSafe direct by default); everything works with Jev off.
* agentmemory is optional; when configured, it runs as the team's memory server.

## 11. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Scan floor of hash-based delta for very large disks | Downtime grows with disk size | Parallel read-ahead; recommend handover for shared Ceph or NetApp storage; Ceph-direct diff in 0.2.0 |
| LVM-backed Cinder makes snapshot→volume a full copy | Slow pre-copy passes | Detect backend; prefer RBD; warn in validation; tune `max_sync_passes` |
| Guest change rate higher than link | Warm never converges | `max_sync_passes` cap; estimate shows non-convergence; advisor suggests cold window or handover |
| Network semantics differ (OVS→OVN, MTU) | Post-boot connectivity issues | MTU finding, verification probes, rollback |
| AI advisor wrong | Suboptimal choice | Bounded to eligible tie set, confidence threshold, recorded with evidence, overridable |
| Credentials exposure | Security incident | Secrets only in mounted files/0600 temp files; redaction; Security.md controls |

## 12. Glossary

* **RHOSO** — Red Hat OpenStack Services on OpenShift.
* **Conversion host** — helper VM in a cloud that attaches volumes and moves data.
* **Pre-copy / delta pass** — copying data while the VM runs; later passes copy only changes.
* **Cutover** — the downtime window: stop source, final sync, boot destination.
* **CBT** — VMware Changed Block Tracking.
* **Handover** — moving volume ownership between Cinder services without copying data.
* **Finalize** — irreversible clean-up of the source after a successful migration.
