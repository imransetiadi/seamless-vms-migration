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
| PR-4 | ADV-03 | one confidence ("57% (threshold 60%)"), threshold from settings in `decision.threshold_used`, Details panel with option labels, tooltip | done — 791c259 and the feat commit after it (confidence once, rounded down; reason without numbers; `Term` tooltip; Details tab; old-notes replay test) |
| PR-5 | ADV-04 | `advisor_policy` kind, resolution (plan tag > provider > environment > global; ties → strictest), CRUD (admin), approve/override (admin, reason ≥ 10 chars when required), pending approval holds the migration, Demo policy "Finance — approval required" | planned |
| PR-6 | ADV-02 | deterministic evidence builder with sources, evidence card (3–6 + Show all, Supports/Opposes with icons), similar incidents (≤ 3), "Limited evidence" → `pending_approval` | planned |
| PR-7 | ADV-06 UI | deployment badge (`SEAMLESS_JEV_DEPLOYMENT`, endpoint host), "Data sent to advisor" panel from the serializers, console screening `send_to_remote` setting | planned |

Phases 2–4 (PR-8 … PR-16) follow the spec's order after the review.

**For the owner's review (deviations from the spec's wording, each with its reason):**

* Option labels (ADV-03): the spec's table is used — "Cold migration", "Needs your input", "Needs investigation",
  `none` as "Other" in Details only — except that "(CBT)" is kept for VMware warm only: the OpenStack warm
  strategy pre-copies snapshots and scans the whole device, so "Warm migration (CBT)" would be untrue for it
  (jev_decide 0.99). `none` reads "no clear option" in a note's lines. `TODO(ADV-03): confirm`.
* The confidence is shown rounded down to a whole percent, so 0.595 reads "59% (threshold 60%)" rather than a
  contradictory "60% (threshold 60%)" next to "below threshold".
* `decision.pending_approval` is an audit event kind too (ADV-05's list omits it, its acceptance criterion needs
  it).

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
