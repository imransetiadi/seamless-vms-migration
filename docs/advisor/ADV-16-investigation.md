# ADV-16 — investigation: the cutover numbers of dc2-db-01

Requested by the review of 10 October 2026 (spec *PHASE-2*, ADV-16, open questions #11 and #12): find where each
number comes from before changing anything. **No engine change is made until the owner approves the proposals
below.** Data read from the demo stack (`GET /migrations`, `/plans/plan-0fe5e08c`, `/events?migration_id=…`,
read-only) and recomputed with the engine's own functions (`planning/estimator.py`).

## Where the numbers come from (#11)

dc2-db-01 (`mig-8d1ea67fd6`, plan "DC2 VMware exit", `vmware_warm`) ran on the demo stack: the **real
orchestrator** (convergence rule, cutover gate, downtime clock, byte totals) driving the **simulated executor**
(`executors/simulated.py`, SDD §7.4). The simulator draws byte counts from the estimator model and records pass
durations in *model seconds*, but sleeps only `model_s / SEAMLESS_DEMO_SPEED` (60) of wall-clock time; every
timestamp the orchestrator writes is wall-clock. Nothing comes from a static seed.

| Reported | Stored | Source | Explanation |
|---|---|---|---|
| Downtime 0:05 | `actual_downtime_s` 5.98 | orchestrator: `downtime_ended_at − downtime_started_at`, wall clock | compressed 60×: 5.98 s × 60 ≈ **359 model seconds** — close to the 361 s estimate. A real run has no compression. |
| Transferred 391 GiB ≈ 374 + 16.8 | `bytes_transferred` 390.81 GiB | orchestrator: sum of the passes | pass 1 `full` 373.98 GiB + pass 2 **`final`** 16.82 GiB. The final sync *is* there: the simulated cutover copies it after `mark_downtime_start` (the source stop, 11:28:56.649; event `pass 2 (final) changed 16.8 GiB` at 11:29:01.996), although the pass's recorded `started_at` (11:28:55.596) is taken before the shutdown (P2). |
| Cutover at pass 2 with a delta above 1.0 GiB, before 5 passes | phase history: `precopy → awaiting_cutover` right after pass 1, reason "estimated final downtime 512 s is within the SLO" | orchestrator `_converged` (SDD §5.3) | The migration converged after the **full copy**, by the SLO rule: VMware CBT passes carry no byte counts, so the 1.0 GiB rule does not apply to `vmware_warm` (SDD §5.3, last paragraph); the estimated final downtime (60 shutdown + 16.7 GiB / 125 MiB/s ≈ 137 + 120 conversion in place + 60 create + 120 boot ≈ 500 s; 512 s with the write rate before the final pass recalibrated it) was under the plan's 900 s SLO. Pass 2 is the final sync, not a delta pass. |
| Estimate 6m01s vs actual 0:05 | `estimate.downtime_s` 361 (3 passes, 92 MiB final delta) | estimator at validation | The panel shows the validation estimate; the estimate the gate used at convergence (512 s, with the real 16.8 GiB delta) appears only in the phase-history reason. The actual is wall clock (above). |
| "demo46", requested and approved by the same admin | two `approvals` by `admin` at 11:27:23 (one comment "demo46"), `cutover_requested` | **a test script, not the product**: `demo46.sh`, run against the demo stack on 8 October by the coding agent, approved (`POST …/approve {"comment": "demo46"}`) and requested the cutover with the same admin token | ADV-17 makes the API refuse this; the seed itself contains no "demo46". |

Recomputation (captured output of `reproduce.py`, the engine's functions on the stored documents):

```
guest write rate used: 5.59 MiB/s (vm.change_rate_bps=5859767.227950663); link 125 MiB/s
pass 1 full: 373.98 GiB, duration_s 3063.7 model s; wall 51.4 s
estimate_final_downtime after pass 1: 497 s (SLO 900 s) -> converged by the SLO rule
pass 2 final: 16.82 GiB, duration_s 257.8 model s = 137.8 s transfer + 120 s v2v in place; wall 5.4 s
transferred: 390.81 GiB = full 373.98 + final 16.82 (+ dropped 0)
actual_downtime_s 5.98 (wall clock, demo_speed 60) -> 359 model s; validation estimate 361 s with 3 passes and a 92 MiB final delta
final pass started_at 11:28:55.596491 vs downtime_started_at 11:28:56.648114: before the source stop
final sync alone at the link rate: 138 s
```

## Does the engine sync after the source stops, and where is it recorded? (#12)

| Path | Final sync after the stop | Recorded |
|---|---|---|
| OpenStack warm (`roles/import_workloads_warm/tasks/cutover.yml`) | yes: stop (wait for SHUTOFF) → snapshot → `pass_kind: final` → boot | a `final` pass with its bytes, from the warm state file (`executors/ansible.py`) |
| VMware warm (`os_migrate.vmware_migration_kit`, `cbt_sync` + `cutover`) | yes, inside the kit's cutover | **not recorded**: the cutover step returns no pass; the kit reports no byte counts (SDD §5.3) — a real gap for invariants 1, 2 and 4 |
| Simulator (demo) | yes, a `final` pass in the cutover step | recorded, but its `started_at` is taken before the 60 s shutdown, so it reads as starting before the source stopped |

## What is a bug, and what is working as specified

* **Demo time base (demo only).** Durations in model seconds next to wall-clock timestamps: "Downtime 0:05" and
  "verified after 6 s of downtime" are wall clock. Invariant 4 fails in wall time (5.98 s < 138 s) and holds in model
  time (359 s ≥ 138 s).
* **The chart misreads the final pass (dashboard).** `SyncPassChart` treats the final sync as a delta: "Last delta
  (pass 2) changed 16.8 GiB — above the convergence threshold", and its copy names only the pass limit.
* **The SLO rule is specified (SDD §5.3), but the owner's invariant 3 leaves it out.** Today a warm migration may
  converge when its estimated final downtime fits the SLO; for VMware it is, with the pass limit, the only rule.
* **The panel's estimate is the validation one**, not the one the gate used.
* **VMware warm final sync unrecorded (real engine)** — see #12.

## Proposals — waiting for the owner's approval

| # | Change | Where | Engine? |
|---|---|---|---|
| P1 | Decide the SLO rule: **keep** it (recommended: it is what lets a large VM cut over within its SLO without pointless delta passes) and add it to invariant 3 and to the chart copy — or drop it, so only the threshold, the pass limit or an approved Force cutover lets a migration cut over | `orchestrator._converged`, SDD §5.3 | yes, if dropped |
| P2 | Demo time: in demo mode record model time — `actual_downtime_s` and the downtime events in model seconds (wall × `SEAMLESS_DEMO_SPEED`), the final pass's `started_at` after the stop | `orchestrator` downtime clock (demo branch), `executors/simulated.py` | yes (demo branch) |
| P3 | `migration_invariants` (the five invariants) checked for every migration, real and demo: an event on a violation and, in Demo mode, a "Data inconsistent" badge; a property test over the demo seed | new module, orchestrator, dashboard | yes |
| P4 | Force cutover: a cutover before convergence only through an explicit action — current delta and estimated downtime shown, reason required, an approver other than the requester (ADV-17), audited. Today `POST …/cutover` before convergence waits for convergence; it never forces | API, orchestrator, dashboard | yes |
| P5 | Keep the estimate the gate used (`estimate` refreshed at convergence) and show the Downtime panel's components — final sync, conversion, create, boot/verification — with the difference between estimate and actual | orchestrator, dashboard | yes |
| P6 | VMware warm: record the kit's final CBT sync as a `final` pass (its duration from the playbook log; bytes "not reported") | `executors/ansible.py` | yes — needs the lab (not ready) |
| P7 | Dashboard only: the final sync as its own bar ("Final sync, after the source stopped"), left out of the convergence verdict; chart copy "Cutover is allowed when a pass changes less than 1.0 GiB, when the estimated downtime fits the SLO, or after 5 passes" (VMware: "CBT passes report no bytes: the SLO estimate or the pass limit decides") | `components/SyncPassChart.tsx` | no |

Until the owner answers, the work continues with ADV-17, which changes no engine step.

Checked with `jev_verify` against captured evidence (the stored documents, the code excerpts, `reproduce.py`, a
byte-sum check and a probe of `AnsibleExecutor._result` for a VMware cutover): 10 claims verified automatically
(0.80–1.00); one — the simulator copies the final pass after the stop although it records the pass's start before
it — verified at 0.74, short of the automatic bar.
