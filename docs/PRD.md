# Seamless Migrate — Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Product | Seamless Migrate |
| Release | 0.1.0 (MVP) |
| Date | 2026-10-08 |
| Status | Approved; amended 2026-10-09 (NetApp ONTAP handover, Kubernetes and Podman, guest OS catalog) and 2026-10-16 (destinations: community OpenStack 2023.1 Antelope – 2026.2 Hibiscus beside RHOSO 18.0, goal G7) |
| Design | [SDD.md](SDD.md) |

## 1. Problem statement

Organizations running **Red Hat OpenStack Platform 17.1**, **community OpenStack** or **VMware
vSphere** must move their virtual machines to a newer cloud: **Red Hat OpenStack Services on OpenShift
(RHOSO) 18.0**, or a **community OpenStack** cloud of a current release series, from 2023.1 Antelope
through 2026.2 Hibiscus. RHOSP 17.1 is the last TripleO-based release; RHOSO 18.0 runs the OpenStack
control plane on OpenShift. In-place *adoption* is not always possible (hardware refresh, new storage,
network redesign, community or VMware sources), so tenants' workloads must be migrated side by side.
Community operators face the same move: a new Kolla-Ansible, OpenStack-Ansible or other upstream
deployment on new hardware or storage is often safer than upgrading in place across many releases.

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

> One control plane that moves any VM from OpenStack or VMware into RHOSO 18.0 or a current community
> OpenStack release with **minutes of downtime**, predictable before it starts, approved by the right people, reversible until it is
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
| G7 | Destination choice (0.2.0) | Besides RHOSO 18.0, every community OpenStack release series from 2023.1 Antelope to 2026.2 Hibiscus is a destination, by the support tiers of SDD §21: each strategy a destination's capabilities allow runs as on RHOSO 18.0; each Tier 1 series (the upstream-Maintained 2025.1 Epoxy, 2025.2 Flamingo, 2026.1 Gazpacho and 2026.2 Hibiscus) passes the destination lab matrix before a release; every destination's series, tier and missing capabilities are shown before a plan runs |

### Non-goals (0.1.0)

* In-place adoption of a 17.1 control plane (Red Hat adoption procedure).
* Zero-downtime live migration across clouds.
* Migrating Heat stacks, Octavia load balancers, Manila shares, Designate zones (reported as
  findings; manual runbooks).
* Network cut-over automation (DNS/FIP/BGP) — documented hooks only.
* Hyper-V, Nutanix, KVM-without-OpenStack sources.
* Destinations below 2023.1 Antelope, and clouds without the OpenStack APIs (SDD §21.1). Vendor builds of
  community releases other than RHOSO are not certified; they count as the upstream series they ship.

## 4. Personas

| Persona | Needs | Primary surfaces |
|---|---|---|
| **Migration architect** (Rina) | Inventory, readiness, strategy per VM, wave plan, downtime estimates to negotiate windows | Plans, Inventory, estimates, CLI |
| **Platform operator** (Bayu) | Run waves, watch progress, react to failures, retry/rollback | Dashboard migration view, events, CLI |
| **Change approver** (Sari) | Approve cutovers inside the change window; see risk and evidence; finalize | Approve/Cutover/Finalize actions, audit log |
| **Application owner** (Dimas) | Know when their VM is down and for how long; confirm it works afterwards | Plan view (read-only), notifications via events |
| **Security officer** | Credentials never leak; every action attributable; AI use is bounded | Security.md, audit trail, RBAC |
| **Community cloud operator** (Arif) | Move tenants from an old cloud to a new upstream release side by side, without a Red Hat stack; know before a plan runs whether the destination's release and policies fit | Providers (destination check: series, tier, roles), validation findings, Security.md roles |

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
6. **Community cloud refresh** — Arif moves tenants from an old cloud (community Zed, or RHOSP 17.1)
   to a new Kolla-Ansible cloud on 2026.2 Hibiscus. He registers the destination as community
   OpenStack; its check shows the series it detected (Tier 1), the credential's roles and the
   capabilities each strategy needs. Validation flags the one VM whose port goes to another project's
   network: under the secure-RBAC defaults that needs the `service` or `admin` role, no longer
   `advsvc`. He grants the role, revalidates, and runs the plan as in journey 1.

## 6. Functional requirements

Priority: **M** = must (0.1.0), **S** = should (0.1.0 if feasible), **C** = could (later), **M2** = must
for 0.2.0 (goal G7).

| ID | Requirement | P | Acceptance criteria |
|---|---|---|---|
| FR-01 | Register source providers (OpenStack/RHOSP 17.1, VMware) and a RHOSO destination; check connectivity and capabilities | M | `POST /providers/{id}/check` returns status and capabilities (admin, compute microversion, OVN, CBT support) |
| FR-02 | Discover inventory (VMs, disks, NICs, flavors, power state, guest OS, CBT, snapshots) | M | Inventory table lists VMs with disk sizes, guest OS (SDD §9.5) and readiness flags |
| FR-03 | Create plans with VM selection, mappings (network, flavor, volume type, project), SLO, approval, window | M | Plan persisted; editable while draft/validated, except while a migration is in flight, and its providers fixed while one is failed (SDD §12) |
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
| FR-29 | Community OpenStack destinations: a destination provider of kind `openstack` (distribution `openstack_community` or `kolla`) of a series in the support window, beside RHOSO 18.0 | M2 | Every strategy the destination's capabilities allow passes the lab matrix on each Tier 1 series (QASuite §6.7 AC-6, §7.1 M7–M12, §7.2 LAB-D); SDD §21.1 |
| FR-30 | Release detection and support tier per destination: the series range the API version documents fit, an optional declared series, and the tier | M2 | The provider check shows range, declared series and tier; a declared series the detected range contradicts warns, and where the APIs cannot tell the series the declared one decides; with none declared the tier is the least favourable one the range allows (SDD §21.3) |
| FR-31 | Destination capability pre-flight: what each chosen strategy needs (compute and block-storage microversions, Cinder manage, Neutron extensions, roles) | M2 | A missing capability blocks the plan and names it; a series outside the tiers or EOL upstream warns (SDD §21.4) |
| FR-32 | Permission checks follow the destination's policy profile: secure-RBAC defaults (`reader`/`member`/`manager`/`admin`, `service`) or the legacy defaults | M2 | A port on another project's network is judged with `admin`/`service` under the new defaults and `admin`/`advsvc` under the legacy ones (SDD §21.5) |
| FR-33 | Conversion hosts on community clouds: CentOS Stream 9 and RHEL 9, then Rocky Linux and AlmaLinux 9 and Ubuntu 24.04 LTS images | M2 | The conversion-host roles install and verify the copy tools on each listed image (SDD §21.6) |
| FR-34 | Destination-neutral wording: findings, events, the CLI and the dashboard name the destination provider, not "RHOSO" | M2 | No user-facing text assumes RHOSO when the destination is community OpenStack (SDD §21.8) |

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
| NFR-12 | Compatibility | The destination support window follows the upstream lifecycle (releases.openstack.org): Tier 1 is the series upstream maintains, validated in the lab before each Seamless release; a new series (next: 2027.1 Indri, estimated 2027-03-24) joins, and a series upstream ends moves down a tier, within one Seamless minor release (SDD §21.2) |

## 8. Success metrics

* Median warm cutover downtime ≤ 10 min for VMs whose largest disk ≤ 100 GiB and total ≤ 400 GiB;
  p95 ≤ 26 min for largest disk ≤ 500 GiB at the NFR-02 floor of 400 MiB/s per stream; storage handover ≤ 6 min regardless of size (0.1.0 model, SDD §9.1).
  0.2.0 target with changed-extent tracking (SDD §20 D1): ≤ 5 min median independent of disk size.
* ≥ 95 % of migrations complete without manual intervention after pilot wave.
* 0 incidents of source data loss.
* Estimate accuracy (G2).
* Time to plan a 100-VM migration (inventory → validated plan) ≤ 1 hour.
* 0.2.0: every Tier 1 community series passes the destination lab matrix (QASuite §6.7, §7.1) with the
  same downtime and success targets as RHOSO 18.0.

## 9. Release plan

| Release | Scope |
|---|---|
| **0.1.0 (this delivery)** | FR-01…FR-25: collection warm path + hardening, control plane, dashboard, Jev + agentmemory integration, demo mode, OpenShift, Kubernetes and Compose (Colima or Podman) deployment, docs |
| 0.2.0 | Ceph-direct incremental diff, HA (PostgreSQL leader election), per-tenant credentials, notifications (email/Slack webhooks); community OpenStack destinations 2023.1 Antelope – 2026.2 Hibiscus (G7, FR-29–FR-34) |
| 0.3.0 | Network cut-over hooks (DNS, FIP re-mapping, OVN BGP), Octavia/Heat migration helpers |
| 1.0.0 | Certified on RHOSO 18.0.x FRs, validated on every Tier 1 community series of the day, scale test 5,000 VMs, signed releases |

## 10. Dependencies and assumptions

* Source/destination OpenStack APIs reachable from the control plane; conversion hosts reachable
  over SSH from the control plane and from each other (dst → src).
* Tenant credentials (or admin with role assignment) on both clouds; admin required for storage
  handover and some pre-staging resources. At RHOSO a VM whose NIC goes to another project's shared or
  external network needs the `admin` or `advsvc` role (Neutron's port policy), and one whose NIC goes to an
  external network that is not shared, the project's own included, needs `admin` (Nova's policy on
  attaching an external network; `advsvc` does not count there) (SDD §6.6). On a community destination
  running the secure-RBAC defaults (the upstream default from 2023.2), Neutron counts `admin` or `service`
  for such a port and `advsvc` no longer (SDD §21.5).
* Community OpenStack destinations: Keystone v3 with project-scoped tokens (application credentials
  recommended), compute microversion ≥ 2.60 (a storage handover needs ≥ 2.85 on the source cloud), Cinder v3; a
  series of the support window (SDD §21.1).
* VMware: VDDK available on the destination's conversion host; CBT enabled for warm.
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
| Policy defaults differ by release (secure RBAC from 2023.2; `advsvc` deprecated for `service`) | A permission error late in a cutover, after the source stopped | Capability and role pre-flight per destination by its policy profile (SDD §21.4, §21.5); the lab matrix per Tier 1 series |
| Upstream API changes in new series (compute 2.101–2.104 change attach status codes, drop flavor fields and `os-volumes_boot`) | A newer SDK or cloud breaks a call | openstacksdk's per-resource microversion caps (SDD §21.7); recorded version documents in contract tests; the window moves only after the lab matrix passes |
| Community clouds without TLS, or with private CAs | Credentials or guest data in clear text | `verify_tls` on by default, CA bundle per provider; the check reports plain-HTTP endpoints (`tls`, `plain_http`, SDD §10, Security.md) |
| Credentials exposure | Security incident | Secrets in mounted files or the platform secret store (written once through the API, never read back), 0600 temp files for playbooks; redaction; Security.md controls |

## 12. Glossary

* **RHOSO** — Red Hat OpenStack Services on OpenShift.
* **Community OpenStack** — upstream OpenStack releases (releases.openstack.org) deployed with
  Kolla-Ansible, OpenStack-Ansible, DevStack or another upstream tool.
* **Release series** — a six-monthly upstream release, `YYYY.N Name` (2026.2 Hibiscus); **SLURP** marks
  the series between which upstream supports skip-level upgrades (2023.1, 2024.1, 2025.1, 2026.1).
* **Maintained / Unmaintained / End of Life** — upstream's maintenance phases of a series; they decide
  its support tier here (SDD §21.1).
* **Secure RBAC** — upstream's policy defaults with project personas (`reader`, `member`, `manager`,
  `admin`) and a `service` role; on by default from 2023.2.
* **Conversion host** — helper VM in a cloud that attaches volumes and moves data.
* **Pre-copy / delta pass** — copying data while the VM runs; later passes copy only changes.
* **Cutover** — the downtime window: stop source, final sync, boot destination.
* **CBT** — VMware Changed Block Tracking.
* **Handover** — moving volume ownership between Cinder services without copying data.
* **Finalize** — irreversible clean-up of the source after a successful migration.
