# Seamless Migrate — Performance

| Field | Value |
|---|---|
| Product | Seamless Migrate (`seamless`) |
| Version | 0.1.0 |
| Status | Performance model, targets and tuning guide, aligned with SDD §9.1 as amended (per-disk parallel scan term, calibration) and PRD G1/G2/§8. Numbers marked *model* come from the SDD §9.1 formulas; numbers marked *to be filled* are measured at integration |
| Date | 2026-10-08 |
| Related | [SDD.md](SDD.md) §5.3, §6.2, §9.1, §11, §18, §20 D1 · [PRD.md](PRD.md) G1, G2, FR-26, NFR-02, NFR-03, §8 · [QASuite.md](QASuite.md) §7, §9, §13 · [Security.md](Security.md) |

Downtime is the product's headline number, so this document starts from the model that predicts it
(SDD §9.1), works the numbers for realistic VMs, states which PRD targets the model supports, under which
assumptions and **where it cannot support them** (a single 500 GiB disk at the NFR-02a scan floor, multi-disk
VMs behind one storage path), explains how the block-delta engine is built for throughput, and then covers
measurement, tuning, capacity planning, control-plane limits and monitoring.

---

## 1. Terms and how to read this document

| Symbol | Meaning | Default (`EstimatorParams`, SDD §9.1) |
|---|---|---|
| `D` | total disk bytes of the VM (`vm.disk_bytes`) | — |
| `Dmax` | size of the largest disk of the VM | — |
| `U` | used bytes (`vm.used_bytes`; Σ `used_gb`, else 60 % of `D`) | — |
| `c` | guest change rate in bytes/s (`vm.change_rate_bps`; measured by a delta pass, else the default) | 2 MiB/s |
| `L` | link throughput per migration (`plan.link_bps`) | 125 MiB/s (the model's "1 Gbit/s") |
| `S` | scan rate of **one disk stream**: read + hash of the device, per side (`scan_bps`) | 500 MiB/s |
| `P` | disks of one VM that are synced concurrently (`parallel_disks`; the collection's `os_migrate_warm_parallel_disks`) | 4 |
| `scan` | **scan time** of the VM: `max(Dmax / S, D / (S·P))` — the largest disk, or all disks spread over `P` streams | — |
| `F` | fixed costs inside the downtime window: `shutdown + snapshot + create + boot` | 60 + 30 + 60 + 120 = **270 s** |
| `Δk`, `Tk` | bytes moved and duration of pre-copy pass *k* | — |
| `Δf` | final delta moved during the downtime window | — |

Conventions: the model treats 1 Gbit/s as 125 MiB/s (so 10 Gbit/s is 1,250 MiB/s; the raw line rate of a 10 GbE
port is ≈ 1,190 MiB/s), and pre-copy passes follow SDD §9.1 and the runtime rule of §5.3: pass *k* + 1 runs while
the bytes pass *k* moved exceed `convergence_threshold_bytes` (`Δ1 = U`, so a converging migration always runs at
least one delta pass — the first delta pass is what measures the change) and *k* < `max_sync_passes` — the total,
including pass 1, never exceeds `max_sync_passes` (the runtime adds an SLO-based exit). The `Estimate.passes`
field of the tool is authoritative for pass counting; the tables below show the pre-copy passes. Percentiles use the nearest-rank rule: with 10 runs p90 is the 9th smallest value and
p95 the largest.

**Configuration and calibration (SDD §9.1, PRD G2).** Every `EstimatorParams` field can be overridden per plan
with `Plan.estimator_overrides` (unknown keys are rejected with 400; `Plan.link_bps` keeps precedence for
`link_bps`). After every completed warm pass the orchestrator calibrates the migration and re-estimates it:
`vm.change_rate_bps = bytes_changed / (pass.started_at − previous_pass.started_at)` for delta passes, and the
observed per-stream scan rate `bytes_scanned / duration_s / min(P, V)` replaces `scan_bps` for that migration
(`Migration.observed_scan_bps`). The estimate an approver sees therefore converges on reality after the first
delta pass.

> **Implementation check (Track B).** A *full* first pass is usually bound by moving the used data (`U/L`), not by
> scanning, so `bytes_scanned / duration_s` of that pass underestimates `S`: for the 200 GiB worked example of
> §4.2 it gives 202 MiB/s instead of 500, and the estimate shown before the first delta pass would be 21.4 min
> instead of 11.3. A migration that converges after a single full pass never gets a delta pass and would keep the
> inflated number. SDD §9.1 states "delta passes only" for the change rate, not explicitly for the scan rate —
> the scan calibration should skip link-bound full passes (or take the maximum of the observed and the
> configured rate). PERF-E2E-G2 reports calibrated and uncalibrated migrations separately for this reason.

---

## 2. Reference lab

PRD goal G1 is measured "in the reference lab (Performance.md)". The lab below is the **specification** of that
environment. Until it exists, every figure in §6 is provisional; G1/G2 are only *verified* on a lab that matches
it (or on a documented deviation recorded with the results).

| Element | Specification |
|---|---|
| Source cloud A | RHOSP 17.1 (also run once on community OpenStack 2023.1/2024.1); Ceph RBD-backed Cinder (3+ OSD nodes, SSD/NVMe OSDs, 10 GbE); OVN networking |
| Destination cloud B | RHOSO 18.0 on OpenShift 4.16+; Ceph RBD-backed Cinder of the same hardware class; a second variant where cloud B shares cloud A's Ceph cluster (handover tests) |
| Inter-cloud network | 10 GbE, RTT < 1 ms, MTU 1500; shaped variants with `tc`: 1 Gbit/s, and 10 Gbit/s with 20 ms RTT (`netem`) for WAN behavior; jumbo-frame variant (MTU 9000 underlay) |
| Storage path | volumes are read through the compute node's 10 GbE storage network, so the aggregate read ceiling of one conversion host is **≈ 1,190 MiB/s** per side (`S_agg`) whatever `P × S` says — measure it with `fio` on four attached volumes at once (§6.1) |
| Conversion hosts | one pair per cloud, 4 vCPU / 8 GiB (plus an 8 vCPU variant for multi-disk runs), RHEL 9 image, virtio devices, `hw_vif_multiqueue_enabled=true`; OpenSSH with `aes128-gcm@openssh.com`, compression off (§7) |
| Control plane | OpenShift deployment (`deploy/openshift/`) at the manifests' default sizing; a Colima profile (4 CPU / 6 GiB) is sufficient for functional runs, **not** for load tests |
| Workload VMs | P-S 20 GiB boot disk; P-M **100 GiB single disk**; P-L 200 GiB single disk; **P-XL 500 GiB single disk**; P-DB 200 GiB with an `fio` writer at 2 MiB/s and at 10 MiB/s (`--rate`); P-MULTI 4 × 50 GiB; P-MULTI-L 4 × 100 GiB; P-WIN Windows Server 60 GiB; P-IMG image-booted VM; VMware set for the vmware strategies (CBT on) |
| Load tools | `fio` (guest writers; storage), `iperf3` (link), `ssh` + `dd` (SSH ceiling), `k6` or similar for the API |
| Observability | Prometheus scraping `/api/v1/metrics`, `node_exporter` on the conversion hosts, the `/stats` endpoint for p95 downtime |
| Protocol | ≥ 10 runs per scenario for p50/p90/max (≥ 20 when a result is within 10 % of its gate); record the environment descriptor (CPU model, kernel, OpenSSH, Python, storage backend, MTU, RTT, Seamless commit) next to every result. `actual_downtime_s` runs from the executor's detection of the source-stop task in the playbook output to `downtime_ended_at` (SDD §7.2); cross-check at least three runs per scenario against the cloud's own server-action timestamps (`openstack server event list`) so that log-driven timing cannot hide a gate miss |

---

## 3. Targets and feasibility

### 3.1 Targets

| ID | Target | Source |
|---|---|---|
| G1a | Warm VMs whose **largest disk ≤ 100 GiB**: cutover downtime **≤ 10 min (600 s) at p90** | PRD §3 G1 |
| G1b | A warm cutover **beats the cold copy whenever more than ~25 % of the disk is used** (scan at 500 MiB/s versus transfer at 125 MiB/s) | PRD §3 G1 |
| G1c | Handover-eligible VMs: **≤ 6 min (360 s) regardless of size** | PRD §3 G1 |
| G2 | Estimated downtime within **±30 %** of actual for **≥ 80 %** of migrations once one delta pass has calibrated change rate and scan throughput | PRD §3 G2 |
| NFR-02a | Per-disk delta sync **≥ 400 MiB/s** local scan per side on a 4 vCPU conversion host | PRD §7 |
| NFR-02b | WAN-bound transfer saturates **≥ 90 %** of a 10 Gbit/s link with ≥ 4 parallel disks | PRD §7 |
| NFR-03 | 1,000 VMs per plan; 10 concurrent migrations and 3 concurrent cutovers by default | PRD §7 |
| SM-1 | Median warm cutover downtime **≤ 10 min** (largest disk ≤ 100 GiB); **p95 ≤ 25 min (1,500 s)** (largest disk ≤ 500 GiB); storage handover **≤ 6 min** regardless of size | PRD §8 |
| SM-1b | 0.2.0 target with changed-extent tracking: **≤ 5 min median independent of disk size** | PRD §8, FR-26, SDD §20 D1 |
| SM-2 | Inventory → validated plan for 100 VMs ≤ 1 hour | PRD §8 |

SM-1 and SM-2 are this document's shorthand for the bullets of PRD §8.

### 3.2 What the model says about them (default parameters, *model*)

Warm downtime is `F + max(scan, Δf/L)`; with `Δf/L` almost always smaller than the scan term (§4) it is
essentially **`270 s + scan`** — the PRD's "fixed overhead (~4.5 min) + largest-disk scan (≈ 3.4 min per 100 GiB
at 500 MiB/s)".

| Target | Model result | Conclusion |
|---|---|---|
| G1a, 600 s | single 100 GiB disk: `scan` = 205 s ⇒ **475 s (7.9 min)**. 4 × 100 GiB: still 475 s with four concurrent streams, but **614 s (10.2 min)** behind a 1,190 MiB/s storage path | Met with 125 s (21 %) margin for single disks; multi-disk VMs depend on the aggregate ceiling (note 1) |
| G1b, warm beats cold | `warm < cold ⟺ U/L > scan`. Single disk: `U/D > L/S` = **25 %** at 125 MiB/s, 50 % at 250 MiB/s, never at ≥ 500 MiB/s; four equal disks: 6.25 % at 125 MiB/s | Matches the PRD at the 1 Gbit/s planning link; on faster links a single-disk VM is cheaper cold (note 2) |
| G1c, 360 s | `240 s + 20 s × V` ⇒ **1 volume 4.3 min, 3 → 5.0, 6 → 6.0 (the limit), 7 → 6.3** | Met up to six volumes, for any disk size; recalibrate `handover_per_volume_s` in the lab |
| G2 | after the first delta pass `c` and the per-stream scan rate are measured and the estimate is recomputed (§1); what remains is `F` and write amplification (§5.4) | Put lab-measured fixed costs into `Plan.estimator_overrides` (§6.4); PERF-E2E-G2 evaluates the final calibrated estimate |
| NFR-02a | Feasible: BLAKE2b-128 alone runs at ≈ 1.5 GiB/s per thread on an Apple-silicon laptop and scales with threads (§5.2); the bound is storage read and SSH | Verify with `bench_blocksync.py` and `fio` on the lab hosts |
| NFR-02b | `10 Gbit/s × 90 % = 1,125 MiB/s` ⇒ 3 streams at 400–500 MiB/s, 4 streams give margin | Consistent with "≥ 4 parallel disks" (the default `P`); a single SSH stream is window-limited on high-RTT links (§5.3) |
| SM-1 median | largest disk 50–100 GiB ⇒ **6.2–7.9 min** | Met |
| SM-1 p95, ≤ 500 GiB | single 500 GiB disk: **21.6 min at `S` = 500 MiB/s**; **25.8 min at the NFR-02a floor of 400 MiB/s** | Met only if the measured per-stream `S` ≥ **416 MiB/s** (note 3) — LAB-W12 decides |
| SM-1b, 0.2.0 ≤ 5 min | `F` alone is 4.5 min, leaving ≈ 30 s for the final delta | Reachable only when the scan term is removed (`rbd diff` between pass snapshots, libvirt checkpoints; SDD §20 D1) **and** the real fixed costs do not exceed the planning defaults; calibrate `F` |
| SM-2 | Planning cost is O(VMs): one inventory sweep, pre-flight per VM, quota aggregation per project | Verified by PERF-CP-04 (100-VM plan, demo providers and a lab inventory) |

1. **Population and aggregate ceiling (G1a).** The PRD defines the G1a population by the *largest* disk, but the
   amended model also depends on the total: the 600 s budget leaves `scan ≤ 330 s`, i.e. `D ≤ 644 GiB` at
   `P × S` = 2,000 MiB/s (model) and only `D ≤ 383 GiB` behind a 1,190 MiB/s path. A VM of 4 × 100 GiB passes in
   the model (475 s) and misses behind the ceiling (614 s, 2 % over the budget; the budget would need
   1,241 MiB/s); 8 × 100 GiB misses even in the model (680 s). LAB-W13 measures the aggregate, and the
   population query of §6.4 lists every miss with `largest_gib` and `total_gib`, so a miss can be reported by
   cause. This document does not grant a waiver; whether such VMs belong to G1a is a product decision.
2. **Warm versus cold (G1b).** On a 10 Gbit/s link even a fully used 100 GiB disk transfers in `D/L` = 82 s,
   below its 205 s scan, so cold has the lower downtime at every fill level and `min_downtime` will select it.
   `plan.link_bps` must carry the real per-migration share of the link (§6.4), otherwise the selector compares
   wrong numbers. The model assumes the cold path moves `U` bytes; LAB-W14 records whether it moves `U` or `D`.
3. **p95 (SM-1).** The 1,500 s budget leaves `scan ≤ 1,230 s`, i.e. `S ≥ 416 MiB/s` for 500 GiB. At the planning
   rate of 500 MiB/s the margin is 206 s (3.4 min); at the NFR-02a floor of 400 MiB/s the target is missed by
   50 s. Parallel scanning does not help a single disk (`Dmax/S` dominates), so the remedies are a faster
   stream (more `workers`, more vCPUs, faster storage) or splitting data over several disks; beyond that the
   0.2.0 scan removal is the answer.

---

## 4. Performance model (SDD §9.1)

### 4.1 Formulas

| Strategy | Pre-copy | Downtime |
|---|---|---|
| `cold` | none | `shutdown + snapshot + U/L + create + boot` |
| `warm` | `T1 = snapshot + max(U/L, scan)`, `Δ1 = U`; then `Δk = min(D, c·T(k−1))`, `Tk = snapshot + max(scan, Δk/L)` for k = 2, 3, … while `Δ(k−1) > threshold` and `k ≤ max_passes` | `shutdown + snapshot + max(scan, Δf/L) + create + boot`, `Δf = min(D, c·T_last)` |
| `storage_handover` | none | `shutdown + V·handover_per_volume + create + boot` |
| `vmware_cold` | none | `shutdown + U/L + v2v + create + boot` |
| `vmware_warm` | `T1 = U/L`; `Tk = 10 + Δk/L` (same loop) | `shutdown + Δf/L + v2v_inplace + create + boot` |

The model's `T1` assumes fresh destination volumes are not read (`--assume-zero`). In the collection that option is
opt-in (`os_migrate_warm_assume_zero`, default `false`, Security.md R-01); without it the receiver also reads the
fresh destination, which leaves `T1` unchanged as long as the destination reads at least as fast as the source
scans (cheap on RBD).

Derived properties worth knowing:

1. **The scan floor.** After shutdown the final pass must still read and hash the devices on both sides, so warm
   downtime is `F + scan` unless `Δf/L > scan`. That needs a change rate above ≈ 116 MiB/s on a 1 Gbit/s link for a
   200 GiB disk — in practice never. **A faster network does not reduce warm downtime; a faster per-stream scan, or
   more disks scanned in parallel, does.** The scan term is exactly what 0.2.0 removes (§11).
2. **Warm beats cold only when `U/L > scan`.** For a single disk that is `U/D > L/S`: 25 % at 125 MiB/s (1 Gbit/s),
   50 % at 250 MiB/s, never at ≥ 500 MiB/s. For `V ≥ P` equal disks it is `U/D > L/(S·P)`: 6.25 % at 1 Gbit/s,
   12.5 % at 2 Gbit/s, 25 % at 4 Gbit/s, 62.5 % at 10 Gbit/s.
3. **Convergence fixed point.** Every pass after the first takes at least `snapshot + scan`, so the delta cannot fall
   below `Δ* = c·(snapshot + scan)`. The loop converges only if `Δ* ≤ convergence_threshold_bytes`, i.e. `scan ≤
   threshold/c − snapshot` (default threshold 1 GiB, `snapshot` 30 s):

   | change rate `c` | `scan` limit | largest **single** disk that converges (`S` = 500 MiB/s) |
   |---|---|---|
   | 0.5 MiB/s | 2,018 s | 985 GiB |
   | 1 MiB/s | 994 s | 485 GiB |
   | **2 MiB/s (default)** | **482 s** | **≈ 235 GiB** |
   | 5 MiB/s | 175 s | 85 GiB |
   | 10 MiB/s | 72 s | 35 GiB |
   | > 34 MiB/s | none | none (the 30 s snapshot alone already exceeds 1 GiB of change) |

   Beyond the limit every pass after the first is a full scan with no benefit and all `max_sync_passes` run
   (§7.3). For multi-disk VMs the limit applies to `scan`, not to a single disk: in the model 4 × 235 GiB still
   converges, provided the storage path really sustains four full-rate streams.
4. **Non-convergence.** When `c·T ≥ D` the "delta" is the whole disk: downtime becomes `F + D/min(S·P, L)`.
5. **CBT removes the floor.** `vmware_warm` moves an exact delta, so its downtime is
   `shutdown + Δf/L + v2v_inplace + create + boot` regardless of disk size.
6. **Calibration changes the number the approver sees.** The defaults describe an idealized host; after the first
   delta pass the observed per-stream scan rate and change rate replace them (§1). Example: four 125 GiB disks
   behind a 1,190 MiB/s storage path — the defaults predict 8.8 min; the first delta pass observes ≈ 280–300 MiB/s
   per stream (the pass duration includes the 30 s snapshot) and re-estimates 11.7–12.2 min, which is what such a
   host delivers (§4.3).

### 4.2 Worked example — 200 GiB single-disk VM, 120 GiB used, `c` = 2 MiB/s (*model*)

| | 1 Gbit/s (`L` = 125 MiB/s) | 10 Gbit/s (`L` = 1,250 MiB/s) |
|---|---|---|
| `cold` downtime | `270 + 983 s` = **1,253 s (20.9 min)** | `270 + 98 s` = **368 s (6.1 min)** |
| `warm` pre-copy | `scan` = 410 s. `T1 = 30 + max(983, 410) = 1,013 s` moves `U` ⇒ pass 2 carries `Δ2 = 2 MiB/s × 1,013 s = 2,026 MiB`: `T2 = 30 + max(410, 16) = 440 s`; `2,026 MiB > 1 GiB` ⇒ pass 3 carries `Δ3 = 2 MiB/s × 440 s = 879 MiB`: `T3 = 440 s`; `879 MiB ≤ 1 GiB` ⇒ stop. **3 passes, 1,892 s (31.5 min) while the VM runs** | `T1 = 30 + max(98, 410) = 440 s` moves `U` ⇒ pass 2 carries `879 MiB` in `440 s`; `≤ 1 GiB` ⇒ stop. **2 passes, 879 s (14.7 min)** |
| `warm` final delta | 879 MiB (moved in 7 s, hidden inside the 410 s scan) | 879 MiB (0.7 s) |
| `warm` downtime | `60 + 30 + 410 + 60 + 120` = **680 s (11.3 min)** | **680 s (11.3 min)** — identical |
| `storage_handover` downtime | `60 + 20 + 60 + 120` = **260 s (4.3 min)** | 260 s |

This is the worked example of SDD §9.1. Reading: on 1 Gbit/s, warm cuts the downtime of cold by almost half (11.3 vs
20.9 min); on 10 Gbit/s a single-disk warm migration is *worse* than cold (11.3 vs 6.1 min) because it must scan the
free space too; handover wins in both cases but needs shared Ceph. The VM is outside G1a (largest disk > 100 GiB)
and inside the SM-1 p95 population.

### 4.3 Downtime by disk layout (60 % used, `c` = 2 MiB/s, `S` = 500 MiB/s, minutes, *model*)

Single-disk VMs (`scan = D/S`, as before the parallel-disk term):

| Disk | cold 1 G | warm 1 G | cold 10 G | warm 10 G | handover | Against the targets (warm) |
|---|---|---|---|---|---|---|
| 50 GiB | 8.6 | 6.2 | 4.9 | 6.2 | 4.3 | G1a met |
| 100 GiB | 12.7 | 7.9 | 5.3 | 7.9 | 4.3 | G1a met (limit: 161 GiB) |
| 200 GiB | 20.9 | 11.3 | 6.1 | 11.3 | 4.3 | outside G1a; SM-1 p95 met |
| 300 GiB | 29.1 | 14.7 | 7.0 | 14.7 | 4.3 | SM-1 p95 met |
| 500 GiB | 45.5 | 21.6 | 8.6 | 21.6 | 4.3 | SM-1 p95 met at `S` = 500, **missed at 400 (25.8)** |
| 1 TiB | 88.4 | 39.5 | 12.9 | 39.5 | 4.3 | outside the 0.1.0 targets (0.2.0) |

A 500 GiB VM in different layouts, 1 Gbit/s, `P` = 4 (the last two columns cap the aggregate read rate at
1,190 MiB/s):

| Layout | `scan` (model) | warm (model) | `scan` at the ceiling | warm at the ceiling |
|---|---|---|---|---|
| 1 × 500 GiB | 1,024 s | 21.6 min | 1,024 s | 21.6 min |
| 2 × 250 GiB | 512 s | 13.0 min | 512 s | 13.0 min |
| 4 × 125 GiB | 256 s | 8.8 min | 430 s | 11.7 min |
| 8 × 62.5 GiB | 256 s | 8.8 min | 430 s | 11.7 min |

The G1a population in the same terms: 1 × 100 GiB = 7.9 min; 4 × 50 GiB = 6.2 min (7.4 at the ceiling);
4 × 100 GiB = 7.9 min in the model but **10.2 min** at the ceiling. Spreading data over disks helps only while
the aggregate ceiling is not reached; beyond it `scan = D / S_agg`. Set `parallel_disks` (§6.4) to the number of
full-rate streams the host really sustains and the estimator stays honest: with `P` = 2, 4 × 100 GiB is estimated
at 680 s (11.3 min).

Warm pre-copy cost on 1 Gbit/s: 50 GiB → 2 passes (6.8 min); 100 GiB → 3 passes (16.5 min); 200 GiB → 3 passes
(31.5 min); **300 GiB → 5 passes (68 min, final delta 1.26 GiB); 500 GiB → 5 passes (112 min, 2.1 GiB); 1 TiB →
5 passes (226 min, 4.2 GiB)**. Every single disk above ≈ 235 GiB fails to converge below the 1 GiB threshold at
the default change rate (property 3 of §4.1) — see §7.3.

### 4.4 Sensitivity to the change rate (200 GiB single disk, 120 GiB used, 1 Gbit/s, *model*)

| `c` (MiB/s) | Pre-copy passes (s) | Final delta | Downtime | `Δf/L` |
|---|---|---|---|---|
| 0.5 | 1 013, 440 | 220 MiB | 11.3 min | 2 s |
| 2 | 1 013, 440, 440 | 879 MiB | 11.3 min | 7 s |
| 10 | 1 013, 440 × 4 (never converges) | 4.3 GiB | 11.3 min | 35 s |
| 50 | 1 013, 440 × 4 | 21.5 GiB | 11.3 min | 176 s |
| 100 | 1 013, 840, 702, 592, 504 | 49.2 GiB | 11.3 min | 403 s |
| 200 | 1 013, 1 651, 1 668 × 3 | the whole disk | **31.8 min** | 1 638 s |

Downtime stays at the scan floor until `Δf/L` approaches `scan` (≈ 410 s); only beyond ≈ 100 MiB/s does the final
delta start to matter, and at 200 MiB/s the migration degenerates into a full copy. For a 10 Gbit/s link the final
delta never matters in this range.

### 4.5 VMware (200 GiB, 120 GiB used, 1 Gbit/s, `c` = 2 MiB/s, *model*)

`vmware_cold`: `60 + 983 + 300 + 60 + 120` = 1,523 s (**25.4 min**). `vmware_warm`: `T1 = 983 s`,
`Δ1 = 1,966 MiB` ⇒ pass 2 `10 + 15.7 = 26 s`, `Δ = 51 MiB` ⇒ stop; final delta 51 MiB; downtime
`60 + 0.4 + 120 + 60 + 120` = **360 s (6.0 min)**, independent of the disk size.

---

## 5. blocksync — design for throughput

`plugins/module_utils/blocksync.py` (SDD §6.2) is Python 3.6+ standard library only because it runs on the
conversion hosts.

### 5.1 Data flow and what bounds it

```text
 dst conversion host (receiver)                             src conversion host (sender, via ssh + sudo)
 ─────────────────────────────                              ─────────────────────────────────────────────
  read dst chunk i ─► BLAKE2b-128 ─► digest i ──ssh stdin──►  read digest i
  (skipped with --assume-zero)        16 B / chunk             read src chunk i ─► BLAKE2b-128 ─► compare
                                                                   equal ─► nothing
  thread B: apply frame ◄──ssh stdout── frame D (offset,len,data) | Z (offset,len)  ◄─ differs
  os.pwrite ─► fsync at end            E (chunks, changed, bytes, manifest digest)
  recompute manifest digest, compare → exit 0 / 3
```

Per stream the sustained rate is `min(R_src, H_src, R_dst, H_dst, SSH, link)`, where `R` is device read, `H`
hash throughput, and *SSH* the cipher/channel ceiling. The digest stream costs 4 bytes per MiB scanned
(≈ 2 KB/s at 500 MiB/s) and the frames add 13 bytes of header per changed chunk, so for a delta pass the wire
carries almost nothing and the pass time is the **scan time**. On a first pass the destination volume is fresh,
so only non-zero source chunks cross the link, which is why `T1 = snapshot + max(U/L, scan)`; with the opt-in
`--assume-zero` (`os_migrate_warm_assume_zero`, default `false`, applied only to volumes created in that pass) the
fresh destination is not even read.

### 5.2 Design choices that matter

| Choice | Value | Why / effect |
|---|---|---|
| Chunk size | 4 MiB (`DEFAULT_CHUNK_SIZE = 4194304`) | Matches the default RBD object size, so reads are object-aligned. Manifest overhead 4 MiB of digests per TiB (1 MiB chunks: 16 MiB/TiB; 16 MiB chunks: 1 MiB/TiB). Larger chunks mean fewer syscalls and dispatches but coarser deltas (§5.4) |
| Hash | BLAKE2b, `digest_size=16` | Fast in pure `hashlib`, 128 bit is ample for change detection; the manifest digest (BLAKE2b-128 over all chunk digests) gives end-to-end verification. **Not FIPS-approved** (Security.md R-06) |
| Workers | `--workers W` (default 4) bounded in-order read-ahead threads on each side | `hashlib` releases the GIL for large buffers, so hashing scales with cores. Memory ≈ `2 × W × chunk` (≈ 32 MiB at the defaults) |
| Pipelining | receiver sends digests and applies frames in separate threads | Required to avoid pipe deadlock; keeps RTT off the critical path — the digest stream is continuous, like a TCP stream |
| Zero frames | `Z` frame for an all-zero source chunk | Sparse/thin volumes cost 13 bytes per zero chunk instead of 4 MiB |
| I/O | `os.pread` / `os.pwrite`, one `fsync` at the end | No per-chunk sync stalls; correctness check is the manifest digest, durability is the final `fsync` |
| Progress | JSON lines on stderr, summary JSON on the last stdout line | Tailed by the executor into `Migration.progress_pct` |

Indicative primitive speeds measured on the author's laptop (Apple silicon, Python 3.13.5, 4 MiB buffers) —
**not** the reference lab and **not** representative of x86 virtual machines, but they show that hashing is not
the bottleneck:

| Primitive | Throughput |
|---|---|
| `hashlib.blake2b(digest_size=16)`, 1 thread | ≈ 1.5 GiB/s |
| same, 2 / 4 / 8 worker threads | ≈ 2.9 / 5.1 / 7.2 GiB/s aggregate |
| `hashlib.sha256`, 1 thread (hardware SHA) | ≈ 3.0 GiB/s |
| zero-chunk test by `bytes.count(0)` | ≈ 3.6 GiB/s |

SHA-256 can be *faster* than BLAKE2b on CPUs with SHA extensions; on cores without them BLAKE2b wins. Measure
on the conversion-host flavor before drawing conclusions (§6).

### 5.3 SSH and the network

* **Cipher.** AES-GCM with hardware AES (`aes128-gcm@openssh.com`) typically exceeds 1 GB/s per core; software
  ciphers or chacha20 on CPUs without AES instructions can cap a stream at a few hundred MiB/s. Turn
  compression off — disk data is mostly incompressible and compression burns the core that hashes.
* **Window.** OpenSSH uses a fixed ~2 MiB channel window, so a single SSH stream is bounded by
  `window / RTT`: ≈ 2 GiB/s at 1 ms but only ≈ 100 MiB/s at 20 ms. WAN-grade throughput needs several
  streams — which is what NFR-02b's "≥ 4 parallel disks" provides — or an HPN-patched SSH.
* **Parallel disks.** The warm sync module runs the disks of one VM at the same time
  (`os_migrate_warm_parallel_disks`, default 4), each with its own SSH session and its own `--workers` hashing
  threads. This is the `P` of the scan term `max(Dmax/S, D/(S·P))` (SDD §9.1): a multi-disk VM sees an aggregate
  scan rate of up to `P × S`, a single-disk VM does not benefit. The model charges `S` per stream, so it is
  optimistic whenever `P × S` exceeds what one host can read — about 1,190 MiB/s through a 10 GbE storage path
  and 2–3 full-rate streams on 4 vCPU (§7.2). Calibration (§1) measures the real value per migration.
* **Attach serialization.** os-migrate serializes volume attach and detach per conversion host with a `flock`
  (`ATTACH_LOCK_FILE_SOURCE/DESTINATION`, "to prevent device path mismatches"); transfers themselves run in
  parallel. For many small VMs the serialized attach/detach time, not the link, sets the pace (§8).

### 5.4 Write amplification: the model's `c` is a *chunk* rate

The delta unit is the chunk. A single 4 KiB guest write dirties a whole 4 MiB chunk, so the effective rate
`c_eff` can far exceed the guest's byte rate. For uniformly random 4 KiB writes at 512 IOPS (2 MiB/s) over a
200 GiB disk during one 440 s pass, about 225,000 writes land on 51,200 chunks and dirty **98.8 %** of them —
a ≈ 198 GiB delta where the linear model predicts 0.86 GiB. With 1 MiB chunks the same workload dirties
66.7 % (133 GiB); if the writes are concentrated on a 2 % hot set, only 1,024 chunks (≈ 4 GiB) change. Real
databases sit between the extremes. Consequences:

* The linear `Δ = c·T` is exact only for clustered or sequential writers and is otherwise a lower bound for
  short passes (and an overestimate for very long ones, because distinct dirty chunks saturate).
* **Always use the measured `bytes_changed`** of a delta pass (it already counts whole chunks): the calibration of
  SDD §9.1 derives `vm.change_rate_bps` from exactly this number, which is why PRD G2 conditions accuracy on one
  delta pass.
* For random-write-heavy VMs prefer a smaller `--chunk-size` (`os_migrate_warm_chunk_size`, 1 MiB) or a cold
  window / handover; the benchmark matrix in §6.2 shows the trade-off per environment.

---

## 6. Benchmark methodology

### 6.1 Layers of measurement

| Layer | Tool | Question answered |
|---|---|---|
| Primitive and engine | `python3 tests/perf/bench_blocksync.py --size-gib 1` (Track A5) | MiB/s scanned, bytes transferred and wall time for full (`--assume-zero`) and delta (1 %, 5 %, 20 % changed) passes at chunk 1/4/16 MiB × workers 1/4; writes `tests/perf/results-<host>.md` |
| Storage | `fio --name=scan --filename=<dev> --rw=read --bs=4M --iodepth=16 --direct=1 --readonly` on the conversion host, once on one attached volume and once as four concurrent jobs, one per volume (`--name=s1 --filename=<dev1> --name=s2 --filename=<dev2> …` with `--group_reporting`; QASuite §14.8) | Per-stream read rate `R` and the **aggregate ceiling** `S_agg` of the host (RBD vs LVM) |
| Network | `iperf3 -c <peer> -P 4`; `dd if=/dev/zero bs=4M count=2048 \| ssh <peer> 'cat >/dev/null'` with and without `-c aes128-gcm@openssh.com` | Link ceiling and SSH ceiling, single and parallel streams |
| End to end | lab scenarios of [QASuite.md](QASuite.md) §7 (LAB-W*, LAB-H*) | Downtime per VM profile, estimate accuracy |
| Control plane | demo mode with a 1,000-VM plan; API load (QASuite §9, PERF-CP-*) | Tick time, API/SSE latency, DB growth |

### 6.2 Results — engine benchmark (developer host, 2026-10-08, after the zero-chunk fast path)

MiB/s scanned, from [`tests/perf/results-darwin-arm64-apple-m5.md`](../tests/perf/results-darwin-arm64-apple-m5.md):
Darwin 27.0.0 arm64, 10 CPUs, Python 3.13.5; a 1 GiB file in the page cache with a 64 MiB hole every
256 MiB (25 % zero chunks), random 1 MiB change extents, sender and receiver on the same host through
pipes (the CPU ceiling of read + BLAKE2b + apply, not disk or network throughput); load average ≈ 3
during the run. Single runs on a laptop vary by ± 30 %.

| Chunk / workers | full (`--assume-zero`) | delta 1 % | delta 5 % | delta 20 % |
|---|---|---|---|---|
| 1 MiB / 1 | 1047 | 1388 | 1422 | 1275 |
| 1 MiB / 4 | 1030 | 2749 | 2676 | 2064 |
| 4 MiB / 1 | 956 | 1381 | 1239 | 636 |
| 4 MiB / 4 | 1142 | 2878 | 2127 | 1244 |
| 16 MiB / 1 | 1103 | 1282 | 1065 | 593 |
| 16 MiB / 4 | 1178 | 2625 | 1238 | 935 |

Reading: the default 4 MiB / 4 workers scans at ≈ 1.1 GiB/s on a full pass and 1.2–2.9 GiB/s on delta
passes here, two to three times the first measurement of the day (515 / 557–1118 MiB/s, taken on a
host with load average 5–10 and before all-zero chunks skipped hashing — `blocksync` now recognises
them with a byte count, 2.5× cheaper than BLAKE2b). The 20 % rows are transfer-bound (the delta is
sent over a pipe). These numbers are an upper bound for a 4 vCPU conversion host reading from Cinder:
the NFR-02a acceptance below still has to be measured on that flavor (§6.3).

Acceptance for NFR-02a: ≥ 400 MiB/s scanned per side on a 4 vCPU conversion-host flavor with the 4 MiB / 4
worker configuration. The same measurement decides SM-1 p95 for a single 500 GiB disk: `S` ≥ 416 MiB/s (§3.2).

### 6.3 Results — end to end in the reference lab (*to be filled at integration*)

The model column is the expected value at the default parameters (§4); the gate column is the pass rule of
QASuite §9 (PERF-E2E). Downtime is `actual_downtime_s` over ≥ 10 runs.

| Scenario | Case | Strategy / link | Model | Gate | Passes (model → measured) | Downtime p50 / p90 / max (measured) |
|---|---|---|---|---|---|---|
| P-M 100 GiB single disk, 2 MiB/s writer | LAB-W01 | `warm`, 10 G | 475 s (7.9 min) | **G1a**: p90 ≤ 600 s | 1 → *to be filled* | *to be filled* |
| P-M 100 GiB at 20 % and 40 % used, warm vs cold | LAB-W14 | `warm` and `cold`, 1 G | 20 %: cold 434 s < warm 475 s; 40 %: warm 475 s < cold 598 s | **G1b**: warm < cold at 40 % | 1 → *to be filled* | *to be filled* |
| P-MULTI-L 4 × 100 GiB | LAB-W13 | `warm`, 10 G, `P` = 4 and 2 | 475 s (`P` = 4), 614 s at a 1,190 MiB/s ceiling, 680 s (`P` = 2) | **G1a**: p90 ≤ 600 s; report the measured aggregate | 1 → *to be filled* | *to be filled* |
| P-MULTI 4 × 50 GiB | LAB-W04 | `warm`, 10 G | 372 s (6.2 min); 442 s at the ceiling | SM-1 median ≤ 600 s | 1 → *to be filled* | *to be filled* |
| P-L 200 GiB, 2 MiB/s writer | LAB-W02 | `warm`, 1 G and 10 G | 680 s (11.3 min) | SM-1 p95 ≤ 1,500 s; G2 | 2 (1 G), 1 (10 G) → *to be filled* | *to be filled* |
| P-DB 200 GiB at 10 MiB/s | LAB-W03 | `warm`, 10 G | 680 s; never converges | G2; record `change_rate_bps` | 5 → *to be filled* | *to be filled* |
| **P-XL 500 GiB single disk, 2 MiB/s writer** | **LAB-W12** | `warm`, 10 G and 1 G | **1,294 s (21.6 min)** at `S` = 500; 1,550 s (25.8 min) at `S` = 400 | **SM-1 p95**: max of 10 runs ≤ 1,500 s ⇔ measured `S` ≥ 416 MiB/s | 5 with the defaults; tuned (§7.3): 1 at 10 G, 2 at 1 G → *to be filled* | *to be filled* |
| P-L 200 GiB, cold | LAB-C01 (on P-L) | `cold`, 1 G | 1,253 s (20.9 min) | none (reference) | — | *to be filled* |
| P-M, 1 / 3 / 6 volumes | LAB-H01, H02, H03 | `storage_handover`, shared Ceph | 260 / 300 / 360 s | **G1c**: ≤ 360 s up to six volumes | — | *to be filled* |
| VMware 200 GiB, CBT | LAB-V02 | `vmware_warm`, 1 G | 360 s (6.0 min) | ±30 % of the model | 2 → *to be filled* | *to be filled* |

### 6.4 Calibrating the estimator

The orchestrator already calibrates change rate and scan rate per migration after every completed pass (§1); what
is left to the operator is the plan-level starting point, so that the very first estimate is already close.

1. Measure `L` with `iperf3` between the conversion hosts (parallel streams) and set `plan.link_bps` to the
   **per-migration share** (link throughput ÷ expected concurrent pre-copies) — the estimator does not know about
   concurrency.
2. Measure `S` as the single-stream result of `bench_blocksync.py` on the conversion-host flavor, and the
   aggregate ceiling `S_agg` with four concurrent `fio` readers (§6.1). Set `scan_bps` to the single-stream rate
   and `parallel_disks` to `⌊S_agg / S⌋` (at most the `os_migrate_warm_parallel_disks` the playbooks run with).
3. Record real `shutdown`, `snapshot`, `create` and `boot` durations from the first migrations (events
   `migration.phase`, step-duration metrics) and replace the planning defaults.
4. Put the results into the plan; the keys are `EstimatorParams` fields (SDD §9.1), unknown keys are rejected with
   400, and `link_bps` keeps its own plan field:

   ```yaml
   estimator_overrides:          # example values — use your measurements
     scan_bps: 450000000         # bytes/s per disk stream
     parallel_disks: 2           # full-rate streams the conversion host sustains
     shutdown_s: 30
     boot_s: 90
   ```

5. Evaluate the results after the fact from `GET /migrations` (`vm.disks[].size_gb`, `actual_downtime_s`,
   `estimate`, `sync_passes[].kind`); `$TOKEN` is a viewer token taken from the environment:

   ```bash
   # downtime populations of the targets: G1a (largest disk <= 100 GiB, p90 <= 600 s),
   # SM-1 (largest disk <= 500 GiB, p95 <= 1500 s), G1c (handover, <= 360 s); nearest-rank percentiles
   curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/migrations | jq '
     def largest_gib: (.vm.disks | map(.size_gb) | max);
     def total_gib: (.vm.disks | map(.size_gb) | add);
     def pct($pp): sort | .[((((length * $pp) + 99) / 100) | floor) - 1];
     def summary($limit): { n: length, p50_s: pct(50), p90_s: pct(90), p95_s: pct(95), max_s: max, limit_s: $limit };
     [ .[] | select(.strategy == "warm" and .phase == "completed" and .actual_downtime_s != null) ] as $warm
     | { g1a_largest_le_100gib: ([ $warm[] | select(largest_gib <= 100) | .actual_downtime_s ] | summary(600)),
         sm1_largest_le_500gib: ([ $warm[] | select(largest_gib <= 500) | .actual_downtime_s ] | summary(1500)),
         g1a_over_600s: [ $warm[] | select(largest_gib <= 100 and .actual_downtime_s > 600)
                          | { name: .vm.name, actual_downtime_s, largest_gib: largest_gib, total_gib: total_gib } ],
         handover: ([ .[] | select(.strategy == "storage_handover" and .actual_downtime_s != null)
                        | .actual_downtime_s ] | summary(360)) }'
   ```

   G1a holds when `g1a_largest_le_100gib.p90_s ≤ 600` with `n ≥ 10`; the SM-1 p95 gate uses
   `sm1_largest_le_500gib.p95_s ≤ 1500`; `g1a_over_600s` names the misses together with their total size
   (§3.2 note 1). `/stats` gives p95 only over **all** migrations, which mixes the populations, so use this
   query for the gates.

6. Estimate accuracy (G2) is judged on the final, calibrated estimate of migrations that had at least one delta
   pass; the others are reported separately:

   ```bash
   curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/migrations | jq '
     def delta_calibrated: any(.sync_passes[]?; .kind == "delta");
     def within30: ((.actual_downtime_s - .estimate.downtime_s) | fabs) / .estimate.downtime_s <= 0.3;
     [ .[] | select(.actual_downtime_s != null and .estimate != null and .estimate.downtime_s > 0) ]
     | { calibrated:   ([ .[] | select(delta_calibrated) ]       | { n: length, within_30_pct: (map(select(within30)) | length) }),
         uncalibrated: ([ .[] | select(delta_calibrated | not) ] | { n: length, within_30_pct: (map(select(within30)) | length) }) }
     | .calibrated.share = (if .calibrated.n > 0 then (.calibrated.within_30_pct / .calibrated.n) else null end)'
   ```

   G2 is met when `calibrated.share ≥ 0.8`. A large gap between the calibrated and the uncalibrated share points
   at the first-pass scan calibration (§1) or at wrong planning defaults.

---

## 7. Tuning guide

### 7.1 Symptom → action

| Symptom | Likely cause | Action |
|---|---|---|
| Downtime ≈ `270 s + scan` and does not improve with a faster link | Scan floor (§4.1) | Raise `S` (more `workers`, faster storage) or scan more disks in parallel (`parallel_disks`, split large volumes); use handover; `rbd diff` (0.2.0) |
| Multi-disk VM slower than `270 s + D/(S·P)` | Aggregate read ceiling of the host (1,190 MiB/s on 10 GbE, 2–3 streams on 4 vCPU) | Measure `S_agg` (§6.1); lower `parallel_disks` to what the host sustains (§6.4); add vCPUs or a second conversion-host pair |
| Warm never leaves `syncing`; five passes, each at the scan floor | `Δ* > convergence_threshold_bytes` (every single disk above ≈ 235 GiB at 2 MiB/s) | Raise the threshold to ≥ `1.2·Δ*` or set `max_sync_passes: 2` (§7.3) |
| Pre-copy slower than `U/L` | LVM-backed Cinder: snapshot→volume is a full copy; SSH cipher; CPU-bound hashing | §7.4; check `volume_backends` from the provider check; benchmark the cipher; add vCPUs |
| Pass time grows with every pass | Deep RBD clone chains from `volume from snapshot` | Delete tmp volumes promptly (the warm modules do); consider `rbd_flatten_volume_from_snapshot` on a dedicated pool |
| Throughput stuck near 100 MiB/s on a WAN | Single-stream SSH window (§5.3) | Parallelize disks/migrations; HPN-SSH; reduce RTT |
| Stalls after the SSH handshake, small packets fine | MTU black hole (Geneve 1442 vs 1500/9000 paths) | Align `os_migrate_*_conversion_net_mtu`, enable `net.ipv4.tcp_mtu_probing=1` |
| Many small VMs take far longer than data size suggests | Attach/detach serialization per conversion host | Fewer, larger waves per host pair; (future) multiple conversion-host pairs (§11) |
| Estimate error > 30 % | Planning defaults differ from the lab; random-write amplification; no delta pass yet | Set `estimator_overrides` (§6.4); wait for the first delta pass (calibration); smaller chunk size |
| `meets_slo` is false for the warm estimate of every disk above ≈ 15 GiB | The default `downtime_slo_s` of 300 s is below `F + scan` (270 s + 205 s for 100 GiB) | Set the SLO to the business budget: 600 for the G1a population, 1,500 for ≤ 500 GiB |
| API slow with many migrations | Python-side filtering of all documents (§9) | Narrow queries, SSE-driven refresh, upgrade path in §9.3 |

### 7.2 Conversion-host sizing and configuration

* **vCPU:** one core per sustained 500 MiB/s stream for hashing, plus ≈ 0.3 core for SSH AES-GCM per stream:
  4 vCPU sustain 2–3 concurrent full-rate streams, which already saturates a 10 Gbit/s link (§3.2). More
  concurrency queues behind CPU, not behind the link. Note that `os_migrate_warm_parallel_disks` (4) times
  `os_migrate_warm_workers` (4) allows 16 hashing threads for one VM: lower `workers` to 2 on 4 vCPU hosts that
  serve several VMs, or raise vCPUs. Keep `os_migrate_warm_parallel_disks` and `estimator_overrides.parallel_disks`
  consistent: the first is the concurrency that actually runs, the second what the estimator assumes.
* **Memory:** 8 GiB is ample (`2 × W × chunk` per stream plus the page cache). Do not size for the disk.
* **Devices:** a virtio-blk host exposes ≈ 25 data devices (`vdb…vdz`); each in-flight migration holds `V`
  devices per side, so 10 concurrent migrations of ≥ 3 disks do not fit one host — limit `max_parallel` per wave.
* **NIC:** `hw_vif_multiqueue_enabled=true` on the conversion image; jumbo frames where the underlay supports
  them (conversion subnet MTU `os_migrate_{src,dst}_conversion_net_mtu`; Geneve tenant MTU is typically 1442).
* **SSH** (`/etc/ssh/ssh_config.d/50-seamless.conf` on the destination host):
  `Ciphers aes128-gcm@openssh.com,aes256-gcm@openssh.com`, `Compression no`, `IPQoS throughput`,
  `ServerAliveInterval 15`, `ServerAliveCountMax 4`.
* **TCP:** for high bandwidth-delay paths raise `net.core.rmem_max`/`wmem_max` and `net.ipv4.tcp_rmem`/`tcp_wmem`
  maxima to 64 MiB; consider `net.ipv4.tcp_congestion_control=bbr` on WAN paths (validate in the lab).
* **Storage:** attach the temporary volumes from the same availability zone and backend as the source disks; a
  volume type with `read` IOPS and throughput limits removed for the migration project avoids QoS throttling the scan.

### 7.3 Pass-count and convergence settings

| Setting (plan field) | Default | Guidance |
|---|---|---|
| `convergence_threshold_bytes` | 1 GiB | ≥ `1.2·c·(snapshot + scan)`; e.g. 10 MiB/s on 200 GiB: ≥ 5.2 GiB; **a single 500 GiB disk at 2 MiB/s: ≥ 2.5 GiB (use 3 GiB)**, which turns the five passes of the defaults (88 min at 10 Gbit/s, 112 min at 1 Gbit/s, each a full scan) into one or two with the same downtime |
| `max_sync_passes` | 5 | `2` when pass 2 already runs at the scan floor (every later pass costs a full scan and gains nothing); `3` for VMs with a bursty writer |
| `downtime_slo_s` | 300 | Below `F + scan` warm can never meet it (270 s + 205 s for 100 GiB) — pick the strategy (handover, cold on fast links) instead of adding passes, or state the real budget: 600 for the G1a population, 1,500 for ≤ 500 GiB |
| `link_bps` | 125 MiB/s | Per-migration share of the link (§6.4) |
| `estimator_overrides` | `{}` | Measured `scan_bps`, `parallel_disks` and fixed costs (§6.4); the estimator's own name for the pass limit is `max_passes`, but use the plan fields above for threshold and pass count |
| `keep_warm_interval_s` | 900 | Each keep-warm pass is a full device scan plus a snapshot and a temporary volume. The final delta grows only by `c × interval`, which at 2 MiB/s adds ≈ 14 GiB over two hours — and overlaps the scan — so the interval can be relaxed (≥ 4 × the pass time) for large disks; keep-warm mainly proves the path still works |
| `selection_policy` | `min_downtime` | `simplest_meeting_slo` for predictable operations when several strategies meet the SLO |

### 7.4 Backend-specific costs

| Backend | Snapshot | Volume from snapshot | Effect on warm passes |
|---|---|---|---|
| Ceph RBD | O(1) | O(1) copy-on-write clone (flatten is off by default) | `snapshot_s` ≈ seconds; pass time is the scan. Preferred |
| LVM thin | O(1) | O(1) thin snapshot | similar to RBD |
| LVM thick | O(size) | **full copy** of the volume | every pass pays a local copy before the scan — pre-copy and `snapshot` terms are much larger than the model assumes; reduce `max_sync_passes`, prefer cold/handover, or move volumes to RBD. The 0.1.0 finding catalog has no LVM finding: read `volume_backends` from the provider check |
| Image-booted VMs | — | `boot_disk_copy: true` snapshots the server to Glance and builds a volume from the image **each pass** (SDD §6.1) | minutes per pass proportional to the disk; with `boot_disk_copy: false` only data volumes sync and the destination boots from the same image name — use it whenever the image exists on the destination |

---

## 8. Capacity planning

### 8.1 Rules of thumb (planning estimates — confirm in the lab)

| Resource | Estimate |
|---|---|
| Control-plane memory | ≈ 0.5 GiB base + ≈ 250 MiB per concurrently running `ansible-playbook` (planning figure; measure `ps -o rss`). 10 concurrent migrations ⇒ ≈ 3 GiB. The OpenShift manifests request 1 GiB and limit 4 GiB; the Compose stack limits the container to 3 GiB — on a 6 GiB Colima profile lower `SEAMLESS_MAX_CONCURRENT_MIGRATIONS` to 5 for real (non-demo) runs |
| Control-plane CPU | I/O bound: 0.5–1 core average; bursts during `export_workloads` and JSON parsing. Limit 4 cores |
| Database | < 100 MiB for 1,000 VMs (≈ 40 persisted events per migration ≈ 40 k rows; ≈ 20 KB per migration document). Size by backups and retention, not by capacity |
| Pre-copy time | `Σ U / (L_share × streams)` if link-bound; `Σ D / S_agg` if scan-bound — the larger of the two |
| Warm downtime per VM | `270 s + scan`, `scan = max(Dmax/S, D/(S·P))`, with `S_agg` replacing `S·P` where the host's ceiling is lower (§4.3) |
| Cutover throughput | rounds of `max_concurrent_cutovers` VMs, each taking the VM's downtime: 3 concurrent × 100 GiB warm VMs (475 s) ≈ 22.7 VMs/hour and a 10-VM wave ≈ ⌈10/3⌉ × 475 s = 32 min; with 150 GiB VMs (577 s) ≈ 18.7 VMs/hour and 38 min |
| Conversion hosts | one pair per provider in 0.1.0 (SDD §6); attach/detach serialized per host; ≤ ≈ 25 devices per host; aggregate read ≈ 1,190 MiB/s per side on 10 GbE |

### 8.2 Worked example — PRD journey 1 (40 VMs, RHOSP 17.1 → RHOSO)

Two fleets of 40 single-disk VMs, `c` = 2 MiB/s, waves: pilot of 3 + four waves of ≈ 9–10 VMs.

| | 40 × 100 GiB (60 GiB used) — the G1a population | 40 × 150 GiB (90 GiB used) |
|---|---|---|
| Used data | 2,400 GiB (2.3 TiB) | 3,600 GiB (3.5 TiB) |
| Pre-copy, link-bound at 1,125 MiB/s (10 Gbit/s at 90 %) | 2,400 GiB / 1,125 MiB/s ≈ 36 min | ≈ 55 min |
| Pre-copy, link-bound at 125 MiB/s (1 Gbit/s) | ≈ 5.5 hours | ≈ 8.2 hours |
| Downtime per VM (model) | `270 + 205` = **475 s (7.9 min)**, on both links | `270 + 307` = **577 s (9.6 min)** |
| Cutover plan, 3 concurrent | ≈ 22.7 VMs/hour; a wave of 10 needs 4 rounds ≈ 32 min | ≈ 18.7 VMs/hour; 4 rounds ≈ 38 min |
| Against the targets | G1a (≤ 600 s): met, 125 s margin | outside G1a (largest disk > 100 GiB); inside SM-1 p95; below 10 min anyway |

* **Streams to saturate 10 Gbit/s:** `⌈1,125 / 500⌉ = 3` at 500 MiB/s, 3 at 400 MiB/s (2.8) — hence NFR-02b's
  4 parallel disks. A scan-bound stream cannot speed up a link-bound transfer.
* **Cutover plan:** four waves fit a half-day change window, with the pilot wave in an earlier window.
* **Control plane:** 10 concurrent migrations ⇒ ≈ 3 GiB RAM. Per-VM attach/detach (≈ 1–2 min of serialized
  time per VM on each side) is small next to the pre-copy.
* **Multi-disk VMs in the fleet:** every VM above ≈ 380 GiB in total (≈ 640 GiB in the model) leaves the 600 s
  budget regardless of how small its disks are (§3.2 note 1); plan them as their own wave.

### 8.3 Scale-out limits in 0.1.0

One orchestrator (SDD §20 D2), one conversion-host pair per provider, serialized attach/detach, Python-side
list filtering (§9). Beyond ≈ 100–200 concurrent in-flight VMs, split plans across control-plane instances
with separate databases until 0.2.0.

---

## 9. Control-plane performance

### 9.1 API and streaming (SDD §12)

* **SSE:** one asyncio stream per client; ephemeral `migration.progress` events are limited to 1 per second per
  migration, so 10 active migrations produce ≤ 10 messages/s fanned out to *N* clients (500 msg/s for 50
  dashboards) — negligible. Heartbeat every 15 s keeps proxies open; resume with `?since=<seq>` replays from
  the `events` table (`limit` ≤ 1,000).
* **Reads:** `GET /plans`, `/migrations`, `/stats` call `Store.list`, which loads **every document of the
  kind** and filters in Python (SDD §11). At ≈ 20 KB per migration document, 1,000 migrations ≈ 20 MB per
  request and Pydantic validation on the order of a second (planning estimate; PERF-CP-01 measures it). Prefer
  SSE-triggered cache invalidation in the dashboard over short-interval polling.
* **Writes:** the orchestrator persists after every state change; progress updates are throttled. Each update
  rewrites one JSONB value (new tuple version): ≤ 10–20 writes/s at 10 active migrations — easy for PostgreSQL.
  Re-estimation after a completed pass (calibration, §1) is one more write per pass and a pure function.
* **Orchestrator tick** (`SEAMLESS_TICK_S` = 1 s): must not re-read all migrations every tick. PERF-CP-02
  measures tick duration with a 1,000-VM plan; as a stopgap at ≥ 500 migrations use `SEAMLESS_TICK_S=2–5`.
* **Concurrency model:** store calls run in worker threads (`asyncio.to_thread`); the engine pool is 5 (+5
  overflow) connections per process, `pool_pre_ping=True` — a PostgreSQL restart is absorbed on the next call.

### 9.2 PostgreSQL

| Item | Guidance |
|---|---|
| Size | 1 vCPU / 1–2 GiB, 20 GiB volume is generous (manifests: request 250 m / 512 Mi, limit 2 CPU / 1 GiB, 20 Gi PVC) |
| Memory | `shared_buffers` 256 MB, `effective_cache_size` 512 MB–1 GB; `max_connections` 50 (pool ≤ 10 per process plus CLI/psql) |
| Indexes in place | `documents(kind, id)` primary key; `events(seq)` primary key, `events(plan_id)`, `events(migration_id)` |
| Bloat | frequently updated documents: `ALTER TABLE documents SET (fillfactor = 70)` and an aggressive autovacuum (`autovacuum_vacuum_scale_factor = 0.05`) |
| Retention | archive `events` older than 13 months ([MEMORY.md](MEMORY.md) §3.4); `VACUUM (ANALYZE)` after bulk deletes |
| Backups | `pg_dump` nightly (encrypted) plus a restore drill each release; the dump of 1,000 migrations is tens of MiB |

### 9.3 Known scaling limits and the upgrade path

| Limit | Cause | Upgrade |
|---|---|---|
| List endpoints scale with the **total** number of documents of a kind | filters evaluated in Python (SDD §11) | 0.2.0: filter in SQL on JSONB (`data->>'plan_id'`) with an expression index `ON documents ((data->>'plan_id')) WHERE kind = 'migration'`, keyset pagination |
| Single orchestrator | singleton design (D2) | 0.2.0: leader election with PostgreSQL advisory locks |
| Event table growth | append-only, no purge | partition by month, archive |

---

## 10. Monitoring and SLIs

### 10.1 Metrics (SDD §18, `GET /api/v1/metrics`, Prometheus text)

| Metric | Type | Use |
|---|---|---|
| `seamless_migrations{phase}` | gauge | in-flight work, failures, stuck phases |
| `seamless_bytes_transferred_total` | counter | throughput |
| `seamless_downtime_seconds_sum`, `_count`, `_max` | counters / gauge | mean and worst downtime (percentiles come from `/stats`, populations from §6.4) |
| `seamless_step_duration_seconds_sum`, `_count{step}` | counters | mean duration per step (`prestage`, `precopy`, `sync`, `cutover`, `rollback`, `finalize`) |
| `seamless_advisor_calls_total{tool,outcome}` | counter | Jev usage and failures (label values are defined by the implementation) |

The endpoint needs a viewer token unless `SEAMLESS_METRICS_PUBLIC=true`; configure the scrape with a bearer
token Secret. Queries:

```text
# mean downtime over 24 h
increase(seamless_downtime_seconds_sum[24h]) / increase(seamless_downtime_seconds_count[24h])

# in-flight migrations
sum(seamless_migrations{phase=~"precopy|syncing|cutover|verifying|rolling_back"})

# transfer throughput (bytes/s)
rate(seamless_bytes_transferred_total[5m])

# mean cutover step duration over 1 h
rate(seamless_step_duration_seconds_sum{step="cutover"}[1h]) / rate(seamless_step_duration_seconds_count{step="cutover"}[1h])
```

Percentiles, SLO compliance and the downtime-by-strategy breakdown come from `GET /api/v1/stats`
(`p95_downtime_s`, `max_downtime_s`, `slo_compliance_pct`, `downtime_by_strategy`, a 60-bucket
`throughput_series`); they cover all migrations of the plan, so the per-population gates use §6.4.

### 10.2 Alerts (starting points)

```yaml
groups:
  - name: seamless
    rules:
      - alert: SeamlessMigrationsFailed
        expr: seamless_migrations{phase="failed"} > 0
        for: 15m
        annotations: {summary: "Migrations stuck in failed; retry, roll back or cancel"}
      - alert: SeamlessNoThroughputDuringPrecopy
        expr: sum(seamless_migrations{phase=~"precopy|syncing"}) > 0 and sum(rate(seamless_bytes_transferred_total[10m])) == 0
        for: 15m
        annotations: {summary: "Pre-copy running but no bytes transferred"}
      - alert: SeamlessCutoverTooLong
        expr: rate(seamless_step_duration_seconds_sum{step="cutover"}[6h]) / rate(seamless_step_duration_seconds_count{step="cutover"}[6h]) > 1200
        for: 10m
        annotations: {summary: "Average cutover step above 20 minutes"}
      - alert: SeamlessAdvisorDegraded
        expr: sum(rate(seamless_advisor_calls_total{outcome!="ok"}[15m])) > 0.2
        for: 15m
        annotations: {summary: "Jev calls failing; rules are deciding"}
      - alert: SeamlessDown
        expr: up{job="seamless"} == 0
        for: 2m
```

The aggregation on both sides of `and` in `SeamlessNoThroughputDuringPrecopy` matters: `and` matches series by their
full label sets, and a bare `rate(seamless_bytes_transferred_total[10m])` carries the `instance` and `job` labels
that the label-less `sum(...)` on the left lacks, so the expression would never fire.

Also probe `/api/v1/health` from a blackbox exporter (`"db":"error"` means PostgreSQL is unreachable) and watch the
conversion hosts with `node_exporter` (CPU saturation, network throughput, disk read).

### 10.3 Service-level indicators for the PRD goals

| Goal | SLI | Source |
|---|---|---|
| G1a | p90 of `actual_downtime_s` of completed `warm` migrations whose largest disk ≤ 100 GiB (gate 600 s, `n ≥ 10`) | §6.4 population query |
| G1b | warm versus cold downtime of the same VM profile at 20 % and 40 % used (lab only: production runs one strategy per VM) | LAB-W14 |
| G1c | p90 of `actual_downtime_s` of `storage_handover` migrations (gate 360 s) | §6.4 population query |
| SM-1 | median (≤ 600 s, largest disk ≤ 100 GiB) and p95 (≤ 1,500 s, largest disk ≤ 500 GiB) of warm downtime | §6.4 population query |
| G2 | share of calibrated migrations (≥ 1 delta pass) with `abs(actual − estimate) / estimate ≤ 0.3` | jq query in §6.4 |
| NFR-02 | MiB/s scanned per side and link utilization during pre-copy | `bench_blocksync` results, `node_exporter` |
| NFR-03 | tick duration and API p95 with 1,000 VMs | PERF-CP-01, PERF-CP-02 |

---

## 11. Known limits and roadmap

| Limit | Impact | Plan |
|---|---|---|
| Hash-scan floor (SDD §20 D1): every warm pass reads and hashes the devices on both sides | downtime ≥ `F + scan`; a single disk above ≈ 160 GiB exceeds 10 min, a single 500 GiB disk needs `S` ≥ 416 MiB/s for the 25 min p95 | **0.2.0** changed-extent tracking (FR-26): Ceph-direct `rbd diff` between pass snapshots (opt-in, needs Ceph credentials) and hypervisor-assisted libvirt checkpoints; target ≤ 5 min median independent of disk size (PRD §8) |
| Aggregate read ceiling of a conversion host (≈ 1,190 MiB/s on 10 GbE; 2–3 full-rate streams on 4 vCPU) | `P × S` overstates the scan rate of multi-disk VMs; G1a can be missed by VMs above ≈ 380 GiB in total | calibration measures the real rate per migration; `parallel_disks`/`scan_bps` via `Plan.estimator_overrides`; conversion-host pools (below) |
| Calibration needs a completed pass, and a delta pass for a trustworthy scan rate | the first estimate uses defaults and plan overrides; single-pass migrations are never calibrated (§1) | scan calibration from delta passes only; persist observed rates per provider pair as the next default (proposed; not in SDD §9.1) |
| Single stream per disk and per SSH channel | WAN throughput bounded by `window/RTT` | multi-stream sync (several SSH channels per disk, chunk-range sharding); HPN-SSH |
| Delta unit is the chunk | random-write workloads amplify the delta (§5.4) | adaptive chunk size per VM from the first pass |
| One conversion-host pair per provider; attach/detach serialized | many small VMs are attach-bound | multiple host pairs / host pool |
| Single replica orchestrator (D2) | no HA | **0.2.0** leader election |
| Python-side list filtering (§9) | API cost grows with total documents | **0.2.0** SQL filters and pagination |
| Image-booted VMs with `boot_disk_copy: true` re-image each pass | slow passes | use `false` when the image exists on the destination |
| No LVM-thick detection finding | surprise slow passes | add a finding from `volume_backends` |
| Page-cache pollution on conversion hosts from full-device reads | cache churn, no correctness issue | `posix_fadvise` sequential/don't-need hints |
| BLAKE2b is not FIPS-approved | policy exceptions | protocol v2 with negotiated SHA-256 |

---

## Appendix A — reproducing the model

The tables above come from this reference implementation of SDD §9.1 (`MiB`-based, loop convention of §1; the
optional `ceiling` argument models the aggregate read ceiling of §3.2 note 1 and is **not** part of the SDD):

```python
from dataclasses import dataclass
MiB, GiB = 1024**2, 1024**3

@dataclass(frozen=True)
class P:
    link: float = 125 * MiB; scan: float = 500 * MiB; parallel_disks: int = 4; change: float = 2 * MiB
    shutdown: float = 60; boot: float = 120; create: float = 60; snapshot: float = 30
    handover_per_volume: float = 20; v2v: float = 300; v2v_inplace: float = 120
    threshold: int = 1 * GiB; max_passes: int = 5

def scan_time(disks, p, ceiling=None):
    """SDD 9.1: max(Dmax/S, D/(S*P)); `ceiling` optionally caps the aggregate rate (storage path, host CPU)."""
    aggregate = p.scan * p.parallel_disks
    if ceiling:
        aggregate = min(aggregate, ceiling)
    return max(max(disks) / p.scan, sum(disks) / aggregate)

def warm(disks, U, p, ceiling=None):
    D, scan = sum(disks), scan_time(disks, p, ceiling)
    t = [p.snapshot + max(U / p.link, scan)]; moved = U          # pass 1 moves the used data
    while moved > p.threshold and len(t) < p.max_passes:        # SDD 5.3: another pass while the last one moved > threshold
        moved = min(D, p.change * t[-1]); t.append(p.snapshot + max(scan, moved / p.link))
    final = min(D, p.change * t[-1])
    return dict(scan=scan, passes=len(t), pass_times=t, final=final,
                down=p.shutdown + p.snapshot + max(scan, final / p.link) + p.create + p.boot)

cold = lambda U, p: p.shutdown + p.snapshot + U / p.link + p.create + p.boot
handover = lambda V, p: p.shutdown + V * p.handover_per_volume + p.create + p.boot

p = P(); D = 200 * GiB; U = int(D * 0.6)
w = warm([D], U, p)
print(w["down"], cold(U, p), handover(1, p), w["passes"], round(sum(w["pass_times"])))   # 679.6 1253.04 260 3 1892
print(warm([100 * GiB], 60 * GiB, p)["down"])                                # G1a: 474.8
print(round(warm([100 * GiB] * 4, 240 * GiB, p)["down"], 1),
      round(warm([100 * GiB] * 4, 240 * GiB, p, ceiling=1190 * MiB)["down"], 1))   # 4 x 100 GiB: 474.8 614.2
print(warm([500 * GiB], 300 * GiB, p)["down"],
      warm([500 * GiB], 300 * GiB, P(scan=400 * MiB))["down"])               # SM-1 p95: 1294.0 1550.0
```

Cross-check the tool against the model with `seamless estimate -f vms.yaml --link-mbps 1000 --slo 600`
(SDD §15) and the unit tests `test_cold_downtime_formula`, `test_warm_converges_and_counts_passes`,
`test_warm_scan_floor_applies`, `test_handover_downtime_independent_of_size`, `test_vmware_warm_uses_exact_delta`.
The parallel-disk scan term, `Plan.estimator_overrides` and the per-pass calibration are covered by
`test_warm_scan_uses_largest_disk_and_parallel_streams`, `test_sdd_worked_example`,
`test_estimate_final_downtime_uses_scan_term`, `test_estimator_overrides_with_plan_precedence`,
`test_invalid_estimator_overrides`, `test_calibration_helpers` (`seamless/tests/test_estimator.py`) and
`test_warm_passes_calibrate_change_rate_scan_rate_and_estimate`, `test_first_pass_alone_does_not_calibrate`
(`seamless/tests/test_orchestrator.py`); the 400 on unknown or non-positive overrides is asserted in
`test_plan_create_validate_start_flow` (`seamless/tests/test_api_resources.py`).
