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
| PR-1 | ADV-01 | `recommendation` kind and model, pure `decide()` with every `ReasonCode`, strategy recommendations written at validation, `migration.strategy` from `decision.final_option`, legacy adapter in the listing, feed from the new route, notes show "Recommended … / Decision …" (no "Selected"). Labels keep the SDD's strategy names ("Warm", not "Warm migration (CBT)": OpenStack warm uses snapshot deltas, only VMware warm uses CBT) — `TODO(ADV-03): confirm` | in progress |
| PR-2 | ADV-06 core | one redaction gateway every Jev call and memory write passes; fixture test (password, bearer, PEM, clouds.yaml `auth`) | planned |
| PR-3 | ADV-05 | audit event kinds, snapshots with SHA-256, Audit tab, audit page with filters, CSV/JSON export | planned |
| PR-4 | ADV-03 | one confidence ("57% (threshold 60%)"), threshold from settings in `decision.threshold_used`, Details panel with option labels, tooltip | planned |
| PR-5 | ADV-04 | `advisor_policy` kind, resolution (plan tag > provider > environment > global; ties → strictest), CRUD (admin), approve/override (admin, reason ≥ 10 chars when required), pending approval holds the migration, Demo policy "Finance — approval required" | planned |
| PR-6 | ADV-02 | deterministic evidence builder with sources, evidence card (3–6 + Show all, Supports/Opposes with icons), similar incidents (≤ 3), "Limited evidence" → `pending_approval` | planned |
| PR-7 | ADV-06 UI | deployment badge (`SEAMLESS_JEV_DEPLOYMENT`, endpoint host), "Data sent to advisor" panel from the serializers, console screening `send_to_remote` setting | planned |

Phases 2–4 (PR-8 … PR-16) follow the spec's order after the review.
