# Seamless Migrate — Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Product | Seamless Migrate |
| Release | 0.1.0 (MVP) |
| Date | 2026-10-08 |
| Status | Approved; amended 2026-10-09 (NetApp ONTAP handover, Kubernetes and Podman, guest OS catalog), 2026-10-16 (destinations: community OpenStack 2023.1 Antelope – 2026.2 Hibiscus beside RHOSO 18.0, goal G7) 2026-10-20 (GPU workloads as a Technology Preview in 0.3.0, goal G8; the network cut-over hooks and the Octavia/Heat helpers move to 0.4.0) and 2026-10-21 (network cut-over as a Technology Preview in 0.4.0, goal G9; SDD §23, D18) |
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
| G1 | Minimize downtime | In the reference lab (Performance.md): warm VMs whose scan term (SDD §9.1) is ≤ 330 s — e.g. largest disk ≤ 100 GiB and total ≤ 400 GiB at the default 500 MiB/s per stream × 4 streams — cut over in ≤ 10 min at p90, and a warm cutover beats the cold copy whenever more than ~25 % of the disk is used (scan at 500 MiB/s vs transfer at 125 MiB/s); handover-eligible VMs cut over in ≤ 6 min regardless of size. Warm downtime is bounded by fixed overhead (~4.5 min) + largest-disk scan (≈ 3.4 min per 100 GiB at 500 MiB/s); 0.5.0 removes it from the final pass for Ceph RBD volumes (SDD §28, D1) |
| G2 | Predictability | Downtime estimate within ±30 % of actual for ≥ 80 % of migrations once one delta pass has calibrated change rate and scan throughput (SDD §9.1 calibration) |
| G3 | Safety | 100 % of cutovers reversible until finalize; no source data deleted without an explicit finalize by an approver |
| G4 | One workflow for all sources | RHOSP 17.1, community OpenStack and VMware handled by the same plan/wave/approval model |
| G5 | Operability | Every action audited; live progress in the dashboard; Prometheus metrics |
| G6 | Assisted decisions | Jev-based advisor for strategy ties, wave classification and post-cutover review; agentmemory recall of similar past incidents |
| G7 | Destination choice (0.2.0) | Besides RHOSO 18.0, every community OpenStack release series from 2023.1 Antelope to 2026.2 Hibiscus is a destination, by the support tiers of SDD §21: each strategy a destination's capabilities allow runs as on RHOSO 18.0; each Tier 1 series (the upstream-Maintained 2025.1 Epoxy, 2025.2 Flamingo, 2026.1 Gazpacho and 2026.2 Hibiscus) passes the destination lab matrix before a release; every destination's series, tier and missing capabilities are shown before a plan runs |
| G8 | GPU workloads (0.3.0, Technology Preview) | VMs with an NVIDIA vGPU (MIG-backed profiles included) or a passed-through GPU migrate with an explicit GPU mapping instead of being blocked (SDD §22): the GPU rules of SDD §22 hold in simulation for 0.3.0 (the demo's GPU VMs through mapping, validation, the cutover gate's wait, cutover and verification), and the lab cases LAB-GPU01–LAB-GPU08 pass on GPU hosts before the Technology Preview label goes |
| G9 | Network cut-over (0.4.0, Technology Preview) | A migrated VM keeps its network identity where Seamless can move it: on an opt-in plan a floating IP keeps its address, a DNS record follows a changed address, and BGP announcements follow the floating IP. An admin's hooks run at fixed points of each migration for the rest, and the pre-flight reports the Heat stacks, autoscaling groups and Octavia memberships it does not move (SDD §23). The rules hold in simulation for 0.4.0, and the LAB-NET cases pass before the label goes |
| G10 | Notifications (0.5.0) | People who do not watch the dashboard are told at once what concerns them: a change approver that a cutover waits for an approval, an application owner that the VM is down and that it runs again, an operator that a migration failed (SDD §24). Every notified event reaches each subscribed channel at least once, deduplicated by its key and in order per channel, and no channel ever delays or fails a migration; the live smoke (SDD §24.10) passes before 0.5.0 ships |

### Non-goals (0.1.0)

* In-place adoption of a 17.1 control plane (Red Hat adoption procedure).
* Zero-downtime live migration across clouds.
* Migrating Heat stacks, Octavia load balancers, Manila shares, Designate zones: they move by runbook. From 0.4.0
  the pre-flight reports a VM's Heat stack, its autoscaling group and its Octavia memberships, and a helper moves an
  Octavia membership (SDD §23). Up to 0.3.0 no finding reported them, though this list said so.
* Network cut-over beyond SDD §23: Seamless as a BGP speaker, recreating load balancers, copying their TLS secrets,
  and Heat's stack abandon and adopt.
* Hyper-V, Nutanix, KVM-without-OpenStack sources.
* Destinations below 2023.1 Antelope, and clouds without the OpenStack APIs (SDD §21.1). Vendor builds of
  community releases other than RHOSO are not certified; they count as the upstream series they ship.
* GPU state: a GPU VM's memory and its GPU's memory do not move, and the VM boots on the destination as after a
  shutdown; AMD's and Intel's own GPU virtualization (their passthrough devices map like any passthrough device);
  a per-VM GPU flavor in 0.3.0 (SDD §22.1, D17).

## 4. Personas

| Persona | Needs | Primary surfaces |
|---|---|---|
| **Migration architect** (Rina) | Inventory, readiness, strategy per VM, wave plan, downtime estimates to negotiate windows | Plans, Inventory, estimates, CLI |
| **Platform operator** (Bayu) | Run waves, watch progress, react to failures, retry/rollback | Dashboard migration view, events, CLI |
| **Change approver** (Sari) | Approve cutovers inside the change window; see risk and evidence; finalize | Approve/Cutover/Finalize actions, audit log |
| **Application owner** (Dimas) | Know when their VM is down and for how long; confirm it works afterwards | Plan view (read-only); messages about their plan's downtime by email, Slack or a webhook (SDD §24) |
| **Security officer** | Credentials never leak; every action attributable; AI use is bounded | Security.md, audit trail, RBAC |
| **Community cloud operator** (Arif) | Move tenants from an old cloud to a new upstream release side by side, without a Red Hat stack; know before a plan runs whether the destination's release and policies fit | Providers (destination check: series, tier, roles), validation findings, Security.md roles |
| **ML platform engineer** (Nadia) | Move inference servers, notebook hosts and render nodes with their GPUs; know before a plan runs whether the destination has the GPUs, and that no source stops before its GPU is free at the destination | Inventory (GPU column), the plan's GPU mappings, the provider card's GPU capacity, validation findings |
| **Network engineer** (Wulan) | Keep a VM's public address and DNS name through its cutover; know which load balancers and stacks a plan touches; run the site's own network steps at the right moment | Plan network cut-over settings, the hook catalog, findings, events |

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
7. **GPU exit from VMware** (0.3.0, Technology Preview) — Nadia selects the VMs with vGPUs and maps each vGPU
   profile to a destination GPU flavor. Validation shows each VM's GPU flavor, the free capacity per vGPU type and
   the guest driver and license warnings. The pre-copy runs while the VMs serve. One cutover waits because the last
   vGPU of its type was taken meanwhile; its source keeps running and syncing, and the cutover starts once a vGPU is
   free. After boot the verification confirms each server holds its GPU.
8. **Keep the address** (0.4.0, Technology Preview) — Wulan plans a web tier whose floating IPs both clouds' external
   network reaches. The plan keeps their addresses, swaps the API's DNS record in Designate, and runs the site's
   firewall playbook after each server is created. Validation blocks a VM of an autoscaling group until Wulan
   acknowledges it, and names the Octavia pools the VMs belong to; the helper drains each member before its source
   stops and joins the migrated VM to the destination's pool after its verification. A hook that fails after a stop
   rolls that VM back, its floating IP associated at the source again.
9. **Told, not watching** (0.5.0) — Dimas's team list is an email channel that Bayu subscribed to the finance plan.
   Dimas gets one message when his VM stops for its cutover and one when it is verified on the destination, with the
   downtime it took. Sari gets one when a cutover waits for her approval, and a failed migration reaches the
   operators' Slack channel. No message carries a credential, an address or an error's text.

## 6. Functional requirements

Priority: **M** = must (0.1.0), **S** = should (0.1.0 if feasible), **C** = could (later), **M2** = must
for 0.2.0 (goal G7), **M3** = must for 0.3.0 (goal G8, a Technology Preview until its lab cases pass), **M4** = must for
0.4.0 (goal G9, a Technology Preview until its lab cases pass), **M5** = must for 0.5.0 (goal G10 and §9's 0.5.0 row; an
item marked a Technology Preview stays one until its lab cases pass).

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
| FR-25 | Deploy on OpenShift, Kubernetes and Docker Compose (Docker Engine on a Linux host, Colima on macOS, or Podman) with PostgreSQL | M | Manifests in `deploy/openshift` and its overlay `deploy/kubernetes`, compose stack in `deploy/compose` |
| FR-26 | Changed-extent tracking: the final warm pass reads only the chunks that Ceph's `rbd diff` lists between Cinder's pass snapshots, instead of scanning whole disks (SDD §28); libvirt dirty bitmaps follow a lab spike | M5 | A disk that cannot use extents, or any doubt, scans the disk in full and never fails a pass; every earlier pass checks the extents against its scan; a Technology Preview until LAB-CE01–LAB-CE07 and PERF-E2E-SM1b pass (SDD §28.10) |
| FR-27 | HA control plane: a lease on PostgreSQL elects one leader, whose epoch fences every write; two replicas on a cluster (SDD §25) | M5 | A leader lost on a cluster with a ReadWriteMany data volume is replaced, and its migrations resume, within about 35 s (60 s the lab's target); no write of a deposed leader lands after a takeover; a Technology Preview until LAB-HA01–LAB-HA05 pass (SDD §25.10) |
| FR-28 | Network cut-over hooks: an admin's playbook and webhook hooks that a plan runs at `pre_stop`, `post_create`, `post_verify`, `on_rollback` and `on_finalize`, each with a timeout, an idempotency key and a revert | M4 | A hook's failure before the stop keeps the source running, one after it follows `auto_rollback`, and no credential reaches a hook's context (SDD §23.3) |
| FR-29 | Community OpenStack destinations: a destination provider of kind `openstack` (distribution `openstack_community` or `kolla`) of a series in the support window, beside RHOSO 18.0 | M2 | Every strategy the destination's capabilities allow passes the lab matrix on each Tier 1 series (QASuite §6.7 AC-6, §7.1 M7–M12, §7.2 LAB-D); SDD §21.1 |
| FR-30 | Release detection and support tier per destination: the series range the API version documents fit, an optional declared series, and the tier | M2 | The provider check shows range, declared series and tier; a declared series the detected range contradicts warns, and where the APIs cannot tell the series the declared one decides; with none declared the tier is the least favourable one the range allows (SDD §21.3) |
| FR-31 | Destination capability pre-flight: what each chosen strategy needs (compute and block-storage microversions, Cinder manage, Neutron extensions, roles) | M2 | A missing capability blocks the plan and names it; a series outside the tiers or EOL upstream warns (SDD §21.4) |
| FR-32 | Permission checks follow the destination's policy profile: secure-RBAC defaults (`reader`/`member`/`manager`/`admin`, `service`) or the legacy defaults | M2 | A port on another project's network is judged with `admin`/`service` under the new defaults and `admin`/`advsvc` under the legacy ones (SDD §21.5) |
| FR-33 | Conversion hosts on community clouds: CentOS Stream 9 and RHEL 9, then Rocky Linux and AlmaLinux 9 and Ubuntu 24.04 LTS images | M2 | The conversion-host roles install and verify the copy tools on each listed image (SDD §21.6) |
| FR-34 | Destination-neutral wording: a text built for a plan or a migration (a finding, an event, a refusal, in the CLI and the dashboard) names the destination provider; a fixed text says "the destination"; only a text about the Red Hat product says "RHOSO 18.0"; nothing the advisor sends to Jev names a provider | M2 | No user-facing text assumes RHOSO when the destination is community OpenStack (SDD §1 naming, §21.8) |
| FR-35 | GPU inventory: each source VM's GPUs — a VMware vGPU by its profile (MIG-backed included), a VMware passthrough device by its PCI vendor and device ids, an OpenStack VM's by its flavor's requests | M3 | The Inventory shows each VM's GPUs; the VMware provider keeps the vGPU profile (SDD §22.2) |
| FR-36 | Destination GPU flavors and capacity: the destination's GPU flavors, their declared vGPU profiles, and the free GPU capacity per pool read from Placement | M3 | A capacity Placement does not let the credential read is "unknown", never guessed; no Placement provider's name or uuid is kept (SDD §22.3, §22.12) |
| FR-37 | An explicit GPU mapping in the plan: a VMware GPU key to a destination GPU flavor, an OpenStack GPU VM's flavor mapping | M3 | A GPU VM is never matched to a flavor by size or by name and its flavor is never pre-staged; without a mapping that applies it stays blocked (SDD §22.4) |
| FR-38 | GPU pre-flight: the GPU findings, with the plan's GPU demand counted against the free capacity per pool | M3 | The SDD §22.5 codes implemented and unit-tested; a plan that does not fit is blocked before its first copy |
| FR-39 | The cutover gate reads the GPU capacity again before the source stops | M3 | With no room the migration waits and keeps syncing, and its source never stops (SDD §22.7) |
| FR-40 | GPU verification: the server holds its GPU flavor and its allocations, and the DCGM exporter's port is suggested as a probe | M3 | A server created without its GPU fails the verification, and the plan's automatic rollback rolls it back (SDD §22.8) |
| FR-41 | Technology Preview: every GPU surface says so, and no text claims hardware validation | M3 | The label stays until LAB-GPU01–LAB-GPU08 pass on GPU hosts (SDD §22.14) |
| FR-42 | Network findings: a VM's Heat stack and autoscaling group, its Octavia memberships, a floating IP whose address would change, `allowed_address_pairs`, an announced prefix a plan's waves split, and DNS names the destination cannot publish | M4 | The SDD §23.8 codes implemented and unit-tested; an autoscaled VM is blocked until acknowledged |
| FR-43 | A floating IP that keeps its address, opt-in per plan, on every OpenStack strategy | M4 | The address is reserved at the destination before the window and moves after the source stopped; a rollback gives it back to the source before the source starts (SDD §23.4) |
| FR-44 | A DNS swap through Designate or an RFC 2136 server when an address changes | M4 | A compare-and-swap that changes nothing when the record holds another address, and swaps back on rollback; a TTL longer than the downtime SLO warns (SDD §23.5) |
| FR-45 | A moved floating IP is reachable before the verification passes | M4 | The wait counts in the downtime; Seamless speaks no BGP (SDD §23.6) |
| FR-46 | An Octavia member helper | M4 | The source member is drained before the stop and the migrated VM joins the mapped destination pool after its verification; TLS keys are never read (SDD §23.7) |
| FR-47 | Technology Preview: every network cut-over surface says so, and no text claims a lab validation | M4 | The label stays until the LAB-NET cases pass on two clouds (SDD §23.10) |
| FR-48 | Notification channels: an admin keeps a catalog of email channels (through the installation's SMTP relay), Slack incoming webhooks and HTTPS webhooks signed as Standard Webhooks specify | M5 | A channel's secret is written write-only and never returned; email never leaves without TLS; a channel's test reports its outcome (SDD §24.3) |
| FR-49 | Subscriptions: an operator subscribes a channel to a plan and an admin to every plan, each for topics - approval, downtime and problem by default, outcome and progress on request | M5 | A subscription changes no plan's status, and a channel sends an event once whatever subscriptions match it (SDD §24.4) |
| FR-50 | Messages of fixed content: the VM, the plan and the wave, their ids, the phase, times, the downtime, the estimate and the SLO | M5 | No message holds a credential, an address, an error's text or who acted; names are escaped for each format, and a channel can send ids instead (SDD §24.5, §24.8) |
| FR-51 | Delivery that never holds a migration back: at least once with a dedupe key, in order per channel, with bounded retries, an hour's expiry and at most one message a second per channel | M5 | Each message is audited once as sent or failed, and a channel that never answers leaves every migration as it would be without it (SDD §24.6, §24.7) |
| FR-52 | Per-tenant credentials: a provider holds a Keystone application credential per project, and a plan acts in its source and destination projects with them (SDD §27) | M5 | A plan names one source and one destination project; each step acts with its project's credential, or for admin-only work with the provider's own, and never falls back to it silently; a credential that expires within 24 hours holds the cutover; a Technology Preview until LAB-TC01–LAB-TC06 pass (SDD §27.10) |

## 7. Non-functional requirements

| ID | Category | Requirement |
|---|---|---|
| NFR-01 | Downtime | See G1; downtime clock measured from source stop to verified boot |
| NFR-02 | Throughput | Per-disk delta sync ≥ 400 MiB/s local scan per side on a 4 vCPU conversion host; WAN-bound transfer saturates ≥ 90 % of a 10 Gbit/s link with ≥ 4 parallel disks |
| NFR-03 | Scale | 1,000 VMs per plan; 10 concurrent migrations and 3 concurrent cutovers by default (configurable) |
| NFR-04 | Reliability | Idempotent steps; at-least-once execution with safe retries; no data loss on control-plane crash |
| NFR-05 | Integrity | End-to-end manifest digest per disk per pass (BLAKE2b-128 over chunk digests); a final pass that reads Ceph's extents (0.5.0) proves the chunks it read, and the earlier passes' checks prove the extents (SDD §28.5) |
| NFR-06 | Security | No credentials in DB/API/logs; TLS to all endpoints; NBD only on loopback or SSH; RBAC; audit (Security.md); from 0.5.0 the data path's SSH checks every conversion host's pinned key, and the dst→src link key is held to a forced command (SDD §26) |
| NFR-07 | AI safety | AI outputs advisory, bounded, logged with source and confidence; untrusted text screened; secrets redacted |
| NFR-08 | Usability | Dashboard WCAG 2.2 AA; dense ops layout; live updates ≤ 2 s latency |
| NFR-09 | Observability | Structured logs, events, metrics; every migration has a complete timeline |
| NFR-10 | Portability | Runs on OpenShift 4.16+, Kubernetes 1.28+, Docker Engine 20.10+ with Compose v2.20+ on Linux (Ubuntu, Debian, RHEL, Rocky Linux, AlmaLinux, Fedora; no Colima), Podman 4.7+, macOS via Colima; Python 3.11+, Node 22 |
| NFR-11 | Maintainability | Unit test coverage ≥ 85 % for control-plane core modules; collection additions unit-tested |
| NFR-12 | Compatibility | The destination support window follows the upstream lifecycle (releases.openstack.org): Tier 1 is the series upstream maintains, validated in the lab before each Seamless release; a new series (next: 2027.1 Indri, estimated 2027-03-24) joins, and a series upstream ends moves down a tier, within one Seamless minor release (SDD §21.2) |

## 8. Success metrics

* Median warm cutover downtime ≤ 10 min for VMs whose largest disk ≤ 100 GiB and total ≤ 400 GiB;
  p95 ≤ 26 min for largest disk ≤ 500 GiB at the NFR-02 floor of 400 MiB/s per stream; storage handover ≤ 6 min regardless of size (0.1.0 model, SDD §9.1).
  0.5.0 target with changed-extent tracking (SDD §28, D1): ≤ 5 min median independent of disk size (SM-1b; PERF-E2E-SM1b).
* ≥ 95 % of migrations complete without manual intervention after pilot wave.
* 0 incidents of source data loss.
* Estimate accuracy (G2).
* Time to plan a 100-VM migration (inventory → validated plan) ≤ 1 hour.
* 0.2.0: every Tier 1 community series passes the destination lab matrix (QASuite §6.7, §7.1) with the
  same downtime and success targets as RHOSO 18.0.
* 0.3.0: no GPU VM's source stops without a capacity read that found room for its GPU (SDD §22.7); LAB-GPU01–LAB-GPU08
  pass on GPU hosts before the Technology Preview label goes.
* 0.4.0: no floating IP changes its address without a finding that says so, and a keep-address move's gap stays
  inside the downtime (SDD §23.4); the LAB-NET cases pass on two clouds before the Technology Preview label goes.

## 9. Release plan

| Release | Scope |
|---|---|
| **0.1.0 (this delivery)** | FR-01…FR-25: collection warm path + hardening, control plane, dashboard, Jev + agentmemory integration, demo mode, OpenShift, Kubernetes and Compose (Docker on Linux, Colima on macOS, or Podman) deployment, docs |
| 0.2.0 | Community OpenStack destinations 2023.1 Antelope – 2026.2 Hibiscus (G7, FR-29–FR-34), as delivered. Its other planned items - notifications, HA, per-tenant credentials and the Ceph-direct incremental diff - moved to 0.5.0 |
| 0.3.0 | GPU workloads as a Technology Preview (G8, FR-35–FR-41; SDD §22) |
| 0.4.0 | Network cut-over as a Technology Preview (G9, FR-28, FR-42–FR-47; SDD §23): hooks, the keep-address floating IP move, the DNS swap, the BGP reachability wait, the Heat and Octavia findings and the Octavia member helper |
| 0.5.0 | Notifications by email, Slack and signed webhooks (G10, FR-48–FR-51; SDD §24); the HA control plane as a Technology Preview (FR-27; SDD §25); SSH trust on the data path, its lab cases a gate of the release (NFR-06; SDD §26); per-tenant credentials as a Technology Preview (FR-52; SDD §27); changed-extent tracking for warm passes as a Technology Preview (FR-26; SDD §28) |
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
* GPU workloads (0.3.0): the NVIDIA vGPU manager on the source and destination hosts, Nova's vGPU or a PCI alias
  configured at the destination, and GPU flavors the operator defines there. Placement's default policy gives the
  capacity reads to `admin`: without it the capacity reads "unknown" and the pre-flight warns. The guests' NVIDIA
  drivers and the NVIDIA License System stay the operator's (SDD §22).
* Network cut-over (0.4.0): for the keep-address move, an external segment or a BGP fabric both clouds reach, and a
  destination credential that may choose a floating IP's address; for the DNS swap, Designate or an RFC 2136 server
  with a TSIG key; Octavia at the destination for the member helper (SDD §23).
* Jev requires a provider API key (TypeSafe direct by default); everything works with Jev off.
* agentmemory is optional; when configured, it runs as the team's memory server.

## 11. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Scan floor of hash-based delta for very large disks | Downtime grows with disk size | Parallel read-ahead; recommend handover for shared Ceph or NetApp storage; Ceph-direct extents for the final pass in 0.5.0 (SDD §28) |
| LVM-backed Cinder makes snapshot→volume a full copy | Slow pre-copy passes | Detect backend; prefer RBD; warn in validation; tune `max_sync_passes` |
| Guest change rate higher than link | Warm never converges | `max_sync_passes` cap; estimate shows non-convergence; advisor suggests cold window or handover |
| Network semantics differ (OVS→OVN, MTU) | Post-boot connectivity issues | MTU finding, verification probes, rollback |
| AI advisor wrong | Suboptimal choice | Bounded to eligible tie set, confidence threshold, recorded with evidence, overridable |
| Policy defaults differ by release (secure RBAC from 2023.2; `advsvc` deprecated for `service`) | A permission error late in a cutover, after the source stopped | Capability and role pre-flight per destination by its policy profile (SDD §21.4, §21.5); the lab matrix per Tier 1 series |
| Upstream API changes in new series (compute 2.101–2.104 change attach status codes, drop flavor fields and `os-volumes_boot`) | A newer SDK or cloud breaks a call | openstacksdk's per-resource microversion caps (SDD §21.7); recorded version documents in contract tests; the window moves only after the lab matrix passes |
| Community clouds without TLS, or with private CAs | Credentials or guest data in clear text | `verify_tls` on by default, CA bundle per provider; the check reports plain-HTTP endpoints (`tls`, `plain_http`, SDD §10, Security.md) |
| Credentials exposure | Security incident | Secrets in mounted files or the platform secret store (written once through the API, never read back), 0600 temp files for playbooks; redaction; Security.md controls |
| A GPU taken between the cutover gate's read and the server's creation | The cutover fails after the source stopped | The capacity read right before the stop (SDD §22.7), the automatic rollback, LAB-GPU06 |
| A guest's NVIDIA driver does not suit the destination host's vGPU manager | The GPU does not work after the cutover | `GUEST_GPU_DRIVER` before the plan runs, a test migration first, the verification's GPU checks, LAB-GPU08 (SDD §22.5) |
| vSphere takes no snapshot of a VM with a DirectPath I/O GPU | No warm migration for such VMs | `vmware_warm` ineligible, `vmware_cold` while the VM is powered off (SDD §22.6) |
| A floating IP's address answered from both clouds during a move | Traffic split between a stopped source and its copy | The source's floating IP is disassociated after the stop and before the destination's is associated, journaled; LAB-NET (SDD §23.4) |
| A hook does harm or hangs | A failed or slow cutover, a leaked secret | Hooks registered by an admin alone, a timeout per hook, a revert, output through redaction, no credential in the context (SDD §23.3) |
| An autoscaling group replaces a server Seamless stopped | Two live copies of a VM | `SRC_VM_AUTOSCALED` blocks the VM until acknowledged (SDD §23.8) |

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
* **vGPU** — an NVIDIA virtual GPU, a share of a physical GPU the hypervisor's vGPU manager hands to a VM by its
  profile (`A100-4C`: 4 GB of framebuffer, compute); **MIG-backed** profiles take a fixed slice of a MIG-capable GPU.
* **Placement** — the OpenStack service that counts each compute host's resources, a GPU's vGPUs among them.
* **Technology Preview** — shipped and tested in simulation, not yet validated on real hardware.
