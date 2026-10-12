# Advisor improvement — plan

Implementation plan for the *Seamless Migrate — Advisor Improvement Spec*. Terms map to code in
[CODEBASE_MAP.md](CODEBASE_MAP.md). Repository rules still apply (`CLAUDE.md`): `docs/SDD.md` is binding,
so every new kind, field, route, event kind, setting and permission is added to the SDD **in its own
commit before the code**; TDD; Jev decides between options and gates each change; local commits only
(each spec "PR" is a series of local commits — no push or pull request without the owner's go-ahead).

## Design decisions

| # | Decision | Why | Jev |
|---|---|---|---|
| D1 | `AdvisorRecommendation` is a new document kind `recommendation` (one document per recommendation: subject, model, recommendation, evidence, decision, outcome, feedback) behind `/api/v1/advisor/recommendations` (list with filters and pagination, get, approve, override, feedback). | Approve/override need stable ids; plan-level subjects (workload tier) have no migration to embed into; the feed must filter and page 5,000 entries server-side. Nothing is stored twice: new code writes recommendations instead of strategy notes. | `jev_decide` 0.72 over extending `AdvisorNote` (0.09) and a hybrid (0.12) |
| D2 | `AuditEvent` = new persisted event kinds in the existing append-only `events` table (actor `policy:<id>` / `user:<id>` / `system:fallback`; `data.{target_id, model_version, before, after, snapshot_sha256}`); audit page, filters and CSV/JSON export read `GET /events`. | The table is already append-only (no API updates or deletes), streamed and exported; a second audit table would duplicate it. | `jev_decide` 0.99 over a new table |
| D3 | Input snapshots: write-once document kind `advisor_snapshot`, id = SHA-256 of the redacted payload. | Content-addressed = immutable and verifiable. | — |
| D4 | The engine keeps reading `migration.strategy`; `_validate_plan` writes it from `decision.final_option` (the deterministic choice while `pending_approval`, and the migration does not start until the decision is approved or overridden). | Smallest change to the migration engine; the spec's "engine reads only the final decision". | — |
| D5 | Old strategy notes are not copied: the recommendations listing adapts each strategy note left in `Migration.advisor_notes` into a `legacy: true` entry when it reads it (fields parsed where possible); validation keeps existing notes. | No data migration at all, so nothing to roll back and the counts match by construction (refined while writing the SDD for PR-1; it replaces a one-time copy). | — |
| D6 | Permissions are named in the API and mapped to a minimum role: `advisor.policy.manage` → `admin`, `advisor.approve` → `admin`. | Existing role model (SDD §13); spec default "Admin". `TODO(ADV-04): confirm` (`approver` is the alternative). | — |

## Open questions — defaults in force

See CODEBASE_MAP.md for the answers found in the code. Defaults until the owner answers: #1 `none` →
"Other", Details only; #3 deployment remote, console screening not sent when remote; #4 memory shared →
tenant isolation after PR-2; #7 Admin only; #8 keep forever; #10 enum values only. Each default is marked
`TODO(ADV-xx): confirm` in the code.

## Order (from the spec) and status

Phase 1 — trust. **Stop after PR-7 for the owner's review (demo of Phase 1 in Demo mode).**

| PR | Item | Scope here | Status |
|---|---|---|---|
| PR-1 | ADV-01 | `recommendation` kind and model, pure `decide()` with every `ReasonCode`, strategy recommendations written at validation, `migration.strategy` from `decision.final_option`, legacy adapter in the listing, feed from the new route, notes show "Recommended … / Decision …" (no "Selected"). Labels keep the SDD's strategy names ("Warm", not "Warm migration (CBT)": OpenStack warm uses snapshot deltas, only VMware warm uses CBT) — `TODO(ADV-03): confirm` | done — 22a318d, 9719b27, 7331687, 7b5cbdc, ab29332, d8f2fb0, eec70db |
| PR-2 | ADV-06 core | one redaction gateway every Jev call and memory write passes; fixture test (password, bearer, PEM, clouds.yaml `auth`) | done — 4cf3f31, ed99de7 (`ai/redaction.py`, jev_decide 0.99 over keeping it in the agentmemory client) |
| PR-3 | ADV-05 | audit event kinds, snapshots with SHA-256, Audit tab, audit page with filters, CSV/JSON export | done — e652375, 013b627, b313ce2 (`ai/snapshots.py`, `/advisor/audit`, `/advisor/recommendations/:id`; jev_decide 0.91 replace `advisor.strategy`, 0.92 dedicated audit route) |
| PR-4 | ADV-03 | one confidence ("57% (threshold 60%)"), threshold from settings in `decision.threshold_used`, Details panel with option labels, tooltip | done — 791c259, c18699a (confidence once, rounded down; reason without numbers; `Term` tooltip; Details tab; old-notes replay test). After the review of 10 Oct the Decision line carries the comparison ("below threshold (57% < 60%), default kept"; c6f62c9 SDD and the dashboard commit after it; jev_decide 0.64) |
| PR-5 | ADV-04 | `advisor_policy` kind, resolution (plan > source provider > global; ties → strictest), CRUD (admin), approve/override (admin, reason ≥ 10 chars when required), pending approval holds the migration, Demo policy "Finance — approval required" | done — 33f7d09 (SDD), 7054651 (API: `ai/policies.py`, `advisor_hold`, `/advisor/policies`, approve/override), the dashboard commit after it (`/advisor/policies`, Approve/Override in the feed, the plan's "Waiting for advisor approval" panel, the migration's hold banner and next step, the detail page; `lib/policies.ts` resolves like the API) |
| PR-6 | ADV-02 | deterministic evidence builder with sources, evidence card (3–6 + Show all, Supports/Opposes with icons), similar incidents (≤ 3), "Limited evidence" → `pending_approval` | done — da3ca1b, 0cb9dd3 (SDD), 05bfe22, d406720, acec272 (API: `ai/evidence.py`, eleven keys measured against the recommended option else the deterministic one, `decide()` counts what supports or opposes, incidents stored at creation, Jev's keyed reasons filtered — none today; a new policy asks for 3, the built-in default for none), d99d1e5 (dashboard: Evidence panel, list line, `?vm=` and `#findings`/`#estimates` links, mock port on the shared six-case table). jev_decide 1.0 ×5, 0.96 (placement) |
| PR-7 | ADV-06 UI | deployment badge (`SEAMLESS_JEV_DEPLOYMENT`, endpoint host), "Data sent to advisor" panel from the serializers, console screening `send_to_remote` setting | done — 72c0bee (SDD), 67df4c3 (API: `ai/payloads.py` field specs with golden tests, `/advisor/data-sent`, the console stays local with a remote model), the dashboard commit after it (badge on the Jev card and on each recommendation, the panel; jev_decide 0.99 declared specs). `TODO(ADV-06): confirm` — default remote, endpoint host unset until configured |

Phases 2–4 (PR-8 … PR-16) follow the spec's order after the review.

**Order after the owner's review of 10 October 2026** (`docs/Seamless Migrate — Advisor Improvement Spec
(PHASE-2).md`, the owner's file, untracked like the first one; it adds Phase 1b, ADV-16 … ADV-21):

1. ADV-16 — investigate the cutover numbers of dc2-db-01 and report; **no engine change before the owner approves**.
   Reported in [ADV-16-investigation.md](ADV-16-investigation.md): the numbers come from the demo simulator's time
   compression (model seconds next to wall-clock timestamps), a chart that reads the final sync as a delta, and the
   SDD §5.3 SLO rule the owner's invariant 3 leaves out; the real VMware warm cutover records no final pass.
   The owner kept the SLO rule (P1) and approved P2–P6. Done — f1b5bc3, 957caa1, 1a24c1d (SDD), 0b930df (API: the
   five invariants at completion in `Migration.data_issues` with `migration.data_inconsistent`, stats that count and
   leave them out, `converged_by`, the estimate the rule used with its `downtime_parts`, a simulated cutover's
   downtime in model seconds, `POST …/force-cutover`, `/health` `demo_speed`), fd10b78 (dashboard: the final sync as
   its own bar, the rules in force and the one that converged, the downtime parts and estimate vs actual, demo clocks
   in model time, the "Data inconsistent" badge and KPI notes, Force cutover; the mock checked against the
   invariants). `TODO(ADV-16): P6` — the real VMware warm cutover's final CBT sync waits for the lab. Follow-up, done:
   a demo rollback recorded its downtime in wall seconds, so its clock froze at a few seconds after running in model
   time; it now counts the model seconds the simulated steps kept the source stopped, like a verified cutover
   (SDD b56d9af, API 2fe1fc3; jev_decide 0.64 for the item, 1.0 for the design).
2. ADV-17 — maker-checker for approvals (a requester never approves their own request; no break-glass). Done —
   02d0dad, 77a6078 (SDD), 2b1a3b4 (API: `Plan.require_distinct_approver`, `cutover_requested_by/at`, the requester's
   approval refused with 409 and not counted by the gate; `AdvisorPolicy.require_distinct_approver`,
   `AdvisorRecommendation.requested_by` = the validating user), 2a749d6 (dashboard), the e2e scripts' second approver
   (`APPROVER_TOKEN`). jev_decide 0.82: a request is not an approval; 0.96: the validating user requests a decision.
   `TODO(ADV-17): confirm` — approver = anyone with the role other than the requester; break-glass off.
3. Rest of Phase 1 — the threshold value in the Decision line (ADV-03), approve/override and the Finance policy
   visible on the demo stack (ADV-04, PR-5), the deployment badge and "Data sent to advisor" (ADV-06, PR-7); then
   a new demo for the owner.
4. ADV-18 application-level verification. Done — 16c87df (SDD), 55945ad (API: `AppProbe` in
   `verification.app_probes` and `Plan.probe_overrides`; refused or a wrong status fails the verification and the plan's
   auto-rollback runs, unreachable passes with review; results in `Migration.verification`; `APP_PROBE_MISSING` and
   review for a database VM without a probe; "Boot verified; no review needed"; rules-based suggestions in
   `Migration.suggested_probes`; demo: billing-db-01 carries `demo-fault: app-down` and rolls back), 0bc821e (dashboard:
   probe editor in the plan form and per VM, the Verification panel, the plan's "Application probes" warning with Use
   suggested probe). jev_decide 0.98 for the model. `TODO(ADV-18): confirm` — the advisor's part of the suggestions waits
   for ADV-11 (the rules suggest for now). The demo stack keeps its data: the new demo plans (probes, the fault) show
   on a fresh demo only.
5. ADV-19 plan status and the finalize queue. Done — b3d87a9, f58a8d1 (SDD), 59cefe6 (API: `derive_plan_status`
   returned as `Plan.derived_status` by every plan route, never stored; `Plan.finalize_warning_h`;
   `Stats.awaiting_finalize`/`finalized`; `POST /plans/{id}/finalize` for a wave or the plan with one approval and an
   audit event per VM; the source watch with `migration.source_running`; demo `POST /demo/vms/{source_id}/power`),
   3063bc7 (`migration.source_stopped`: the watch cleared the flag silently, so the dashboard kept a stale alert —
   jev_decide 1.0), e1f2220 (dashboard: the derived status in every view, Finalize all in place of Start on a
   Completed plan and no Start once a plan runs, the finalize queue by wave with Finalize wave, "Finalize overdue" and
   "Source VM running", Awaiting finalize and Finalized where Completed was, the source alert and the next step on
   the migration page, the demo power control, the reminder in the plan form, the Overview's attention items).
   jev_decide 0.99 (derive, never store), 0.96 (bulk finalize through each VM's finalize). `TODO(ADV-19): confirm` —
   the 72 h reminder (open question 14). Beyond the spec's wording: Pause is hidden on a finished plan too, a
   completed VM whose source runs again gets no finalize advice until it is stopped, and both finalize dialogs warn
   about it.
6. ADV-20 the two kinds of approval. Done — 19858f2, d7bc3f5 (SDD §16), 580ad25 (dashboard only: every approval entry
   names its kind, "Cutover approval" or "Advisor decision · {decision type}"; the Overview's "Pending approvals" lists
   both with a kind filter (eight rows of the chosen kind, the rest behind "Show all"; b40e523) — a cutover approval only at the cutover gate of a plan that requires one with no counted
   approval, an advisor decision while `advisor_hold` is set — and Needs attention no longer lists them; the migration
   page's Approvals adds the newest strategy decision a person gives or gave; the plan settings show "Cutover approval"
   and "Advisor policy" with mode, threshold and a link to `#policy-{id}` on `/advisor/policies`, which lands focus on
   the card). jev_decide 0.87 (compose from the routes the dashboard reads, no approvals route) and 0.99 (an approval
   listed once). Found on the way: Needs attention had counted the requester's own approval (maker-checker).
   Follow-up (live stack, ADV-19), done: Edit plan's reason called completed migrations "in flight … finish, roll back
   or cancel them first"; the refusal now names each VM with what releases it — finalize or roll back a completed one
   (SDD 434fc23, API 1fd2ad2, dashboard d5b188b; jev_decide 0.99).
7. PR-6 ADV-02 evidence card. Done — see the table above. Recommendations recorded before ADV-02 (the live demo's) show
   "No evidence was recorded with this recommendation."; evidence appears with the next validation.
8. Then the original order (after PR-6); ADV-21 goes with ADV-14. Phase 2 still waits for the owner's
   review of Phase 1.

**For the owner's review (deviations from the spec's wording, each with its reason):**

* Option labels (ADV-03): the spec's table is used — "Cold migration", "Needs your input", "Needs investigation",
  `none` as "Other" in Details only — except that "(CBT)" is kept for VMware warm only: the OpenStack warm
  strategy pre-copies snapshots and scans the whole device, so "Warm migration (CBT)" would be untrue for it
  (jev_decide 0.99). `none` reads "no clear option" in a note's lines. `TODO(ADV-03): confirm`.
* The confidence is shown rounded down to a whole percent, so 0.595 reads "59% (threshold 60%)" rather than a
  contradictory "60% (threshold 60%)" next to "below threshold".
* `decision.pending_approval` is an audit event kind too (ADV-05's list omits it, its acceptance criterion needs
  it).
* Policy scopes (ADV-04) are plans and source providers: the product has no plan tags or environments, so the
  spec's "plan tag > provider > environment > global" reads "plan > source provider > global". `TODO(ADV-04): confirm`.
* Approve and Override (ADV-04) need `advisor.approve`, mapped to admin like `advisor.policy.manage`
  (`TODO(ADV-04): confirm`; `approver` is the alternative). Override offers the other eligible options: running
  the recommended one is approving it.

## PR-1 (ADV-01) — acceptance

| Criterion | Status | Evidence |
|---|---|---|
| 0.57 under a 0.60 threshold reads "Recommended: Warm" and "Decision: Cold — below threshold"; nothing says Warm was selected | met | `lib/advisor.test.ts`, `pages/Advisor.test.tsx` (static-cdn-01 in demo mode); on the demo stack web-02's old note now reads "Decision: Cold — below threshold (57% confidence)" |
| The engine reads `decision.final_option`, not the recommendation's option (end-to-end in Demo mode) | met | `test_the_engine_runs_the_decision_not_the_recommendation`; the mock's validation tests; `tests/e2e/smoke-demo.sh` "the migration runs its newest decision" on the demo stack |
| No threshold comparison in UI components | met | `lib/advisor.ts` words the decision from its status and reason code; a test feeds contradictory numbers and gets the decision's wording |
| `decide()` unit tests cover every `ReasonCode` | met | `seamless/tests/test_decision.py` |
| Notes before and after the data migration are the same; it can be rolled back | met (D5) | nothing is migrated: legacy entries are read from the notes (`test_api_advisor.py` one-to-one test) |

Decisions taken with Jev during PR-1: the dashboard mock simulates Jev and takes the decision at validation like
the API (`jev_decide` 1.0 over static fixtures or a rules-only mock). Found and fixed on the way: a re-validation
recorded recommendations for migrations it leaves alone (7b5cbdc, ab29332). The server demo seed records whatever
Jev decides (auto-applied or below threshold); the dashboard mock seeds the three statuses the engine takes today
(`auto_applied`, `below_threshold`, `fallback_rules`) and one legacy note; `pending_approval`, `approved` and
`overridden` arrive with PR-5.
