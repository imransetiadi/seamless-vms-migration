# Advisor — codebase map

Maps every term of the *Seamless Migrate — Advisor Improvement Spec* (`docs/Seamless-Migrate-Advisor
Improvement Spec.md`) to the files, stored kinds and functions that exist in this repository. The spec's
data model is a proposal; the right-hand column says where each concept lives today or will live (the
design decisions are in [PLAN.md](PLAN.md)). Binding contract: `docs/SDD.md` (§4 models, §8 orchestrator,
§9.2 selector, §12 API, §13 roles, §14 AI integration, §16 dashboard).

## Components

| Spec term | Today | Notes |
|---|---|---|
| Jev client | `seamless/src/seamless_migrate/ai/jev.py` — `JevClient.decide/classify/verify/screen`, `call()` | Modes `off`/`stdio`/`http` (`SEAMLESS_JEV_MODE`); timeout `SEAMLESS_JEV_TIMEOUT_S` (20 s); circuit breaker `BREAKER_THRESHOLD = 3` failures → `BREAKER_OPEN_S = 300` s; `status()` → `{mode, available, last_error}`. SDD §14.1. |
| Jev deployment (on-prem / remote) | `deploy/compose/compose.yaml` service `jev`: `npx @jkudish/jev-mcp@0.14.1 --http` with `TYPESAFE_API_KEY`; the control plane calls it at `SEAMLESS_JEV_URL=http://jev:8080/mcp` | The sidecar is a local proxy; the model runs at the TypeSafe API (**remote**). Not recorded in settings yet (ADV-06). |
| agentmemory | `ai/memory.py` — `MemoryClient.remember/search/health`; `ai/knowledge.py` — `KnowledgeService.on_failure/on_completed/on_rolled_back` | `SEAMLESS_MEMORY_URL`, `SEAMLESS_MEMORY_SECRET`, `SEAMLESS_MEMORY_PROJECT` (`seamless-migrate`); every failure swallowed (SDD §14.3). |
| Redaction layer | `ai/memory.py` — `redact(text, redact_names=None)` (PEM, URL credentials, Authorization/Bearer, key=value secrets incl. prefixed keys, CLI `--password`, `smg_` tokens, Keystone fernet, JWT) | Used by `JevClient` (errors), `Advisor._redact` (all Jev payload text), `MemoryClient._clean` (all memory writes and queries), `KnowledgeService`, `verification._check`. No single choke point object yet (ADV-06). |
| Advisor (recommendations) | `ai/advisor.py` — `Advisor.recommend_strategy`, `classify_workloads`, `review_verification` | SDD §14.2. |
| Recommendation / decision | `domain/models.py` — `AdvisorNote {kind, source, summary, confidence, data, created_at}` embedded in `Migration.advisor_notes`; strategy notes carry `data.{deterministic, deterministic_reason, scope, candidates, selected, applied, status, escaped, probabilities, contradicted_requirements, warnings}` | Recommendation and decision are mixed in one note (`selected` vs `applied`). New home: document kind `recommendation` (PLAN.md D1). |
| Decision logic | inline in `Advisor.recommend_strategy` (`applied = in_scope and confidence >= jev_min_confidence and not escaped and status not in (escalate, invalid_response)`) | Becomes the pure `decide()` (ADV-01). |
| Engine reading the decision | `orchestrator.py` `_validate_plan`: `select_strategy()` → `advisor.recommend_strategy()` → strategy = `note.data.selected` when `applied`; `_apply_validation` stores `migration.strategy`; every later step reads `migration.strategy` | Spec's `decision.finalOption` = what `_validate_plan` writes to `migration.strategy`. |
| Strategy selector (rules) | `planning/selector.py` — `select_strategy`, `tie_set`, `advisor_scope`, `eligibility` | SDD §9.2. The "rules" fallback is `select_strategy`. |
| Workload tier | `Advisor.classify_workloads` + `planning/waves.py` `heuristic_tier`, called by `orchestrator.plan_waves` | Plan-level; recorded only as event `advisor.classification` (no stored note). |
| Verification review / console screening | `Advisor.review_verification` (jev_screen + jev_verify), called by the orchestrator's verification step (`plan.verification.use_advisor`) | Note appended to `Migration.advisor_notes`, may set `review_required`. |
| Similar incidents | `KnowledgeService.on_failure` → note `kind=similar_incidents, source=memory`; API `POST /api/v1/advisor/similar-incidents` (`api/routes_misc.py`) → `MemoryClient.search` | Hits `{title, content, score}`; no structured incident yet (ADV-12). |
| Lessons | `KnowledgeService._remember` → `memory.lesson_saved` event | Types `bug` (failure), `fact` (completed), `workflow` (rollback). |
| Advisor status card | `GET /api/v1/advisor/status` (`api/routes_misc.py`, schema `AdvisorStatus{jev, memory}`) | Jev `{mode, available, last_error}`, memory `{enabled, available, last_error}`. |
| Threshold ("the bar") | `config.Settings.jev_min_confidence` = `SEAMLESS_JEV_MIN_CONFIDENCE`, default `0.6` | One value for every environment. |
| Policy | — | New document kind `advisor_policy` (ADV-04). |
| Audit trail | `store.py` table `events` (append-only: `append_event`; only `seamless events purge` — CLI, `delete_events_before` — deletes, no API route); model `domain/models.py` `Event`; API `GET /api/v1/events` + SSE (`api/routes_events.py`); export = JSON lines | Reused for `AuditEvent` (PLAN.md D2). |
| Input snapshot | — | New write-once document kind `advisor_snapshot`, id = SHA-256 of the redacted payload (ADV-05). |
| Storage | `store.py` — `documents(kind, id, version, data JSON)`, `KINDS = ("provider", "plan", "migration")`; `events`; `create_schema()` (no migration framework) | A "reversible migration" here is additive: new kinds and optional fields, original data untouched. |
| Roles / permissions | `domain/enums.py` `Role` (`viewer` < `operator` < `approver` < `admin`); `api/deps.py` `require_role`; dashboard `src/lib/roles.ts` | SDD §13. No permission names yet; `advisor.policy.manage` and `advisor.approve` map to a minimum role. |
| Plans page | `dashboard/src/pages/Plans.tsx`, `PlanDetail.tsx` (actions from `lib/planActions.ts`), `components/MigrationsTable.tsx`, `WavesBoard.tsx` | |
| Migration detail | `dashboard/src/pages/MigrationDetail.tsx` (strategy panel, advisor notes via `components/AdvisorNotes.tsx`) | |
| Preflight | `planning/preflight.py` `run_preflight` → `Finding {code, severity, message}` (SDD §10); dashboard `components/FindingsList.tsx` | |
| Inventory | `GET /api/v1/providers/{id}/inventory` (`api/routes_providers.py`); dashboard `pages/Inventory.tsx` | Evidence source for disk size, CBT (`VMRef.cbt_enabled`), guest OS. |
| Advisor page | `dashboard/src/pages/Advisor.tsx` — `ServiceCard` (Jev, agentmemory), similar-incident search (`useSimilarIncidents`), "Recent advisor notes" built in the browser from `useMigrations()` (last 20) | |
| Note rendering | `dashboard/src/components/AdvisorNotes.tsx` — `NoteDetails` prints "Selected: …" and the probability list; labels in `src/lib/status.ts` (`ADVISOR_KIND_LABELS`, `ADVISOR_SOURCE_LABELS`, `STRATEGY_LABELS`) | Source of problems #1–#3 and #8 of the spec. |
| Demo mode (server) | `seamless/src/seamless_migrate/demo.py` `seed_demo` — providers "RHOSP 17.1 Finance (DC1)" (source), "vCenter DC2", "RHOSO 18.0"; plans validated by the real orchestrator; Jev notes appear when the `jev` sidecar runs | |
| Demo mode (dashboard mock) | `dashboard/src/api/mockData.ts` (`notes` per fixture migration), `src/api/mock.ts` (`VITE_SEAMLESS_MOCK=1`) | |
| Metrics | `metrics.py` — `seamless_advisor_calls_total{tool,outcome}` from `JevClient.calls` | |

## Open questions — answers from the code

| # | Question | Answer from the code | Owner still decides? |
|---|---|---|---|
| 1 | Meaning of `none` and "status ok" | `none`, `ask_user`, `investigate` are jev_decide's escape-hatch outcomes: they appear in `recommendation.probabilities` and, when chosen, set `escaped: true`; the advisor never applies them. "status ok" is the advisor's own wording when `recommendation.status` is absent (`status or 'ok'` in `Advisor.recommend_strategy`). | Yes, the UI meaning of `none` — default: "Other", Details panel only (`TODO(ADV-03): confirm`). |
| 2 | How `confidence` is computed | It is Jev's own calibrated `recommendation.confidence` from the TypeSafe response, not `probabilities[selected]`; the advisor compares that number with the threshold. Kept as is. | No. |
| 3 | Where Jev runs, what is sent | Remote: the `jev` sidecar forwards to the TypeSafe API. Sent today (redacted): the decision sentence, the evidence text (vCPU, RAM, disks with size/type/flags, provisioned/used GiB, guest write rate, SLO, link, finding codes, per-strategy estimates), candidate ids and descriptions; for classification VM name/os/tags/disks/flavor; for verification the check results and the screened console excerpt. | Yes — on-prem deployments (`TODO(ADV-06): confirm`); default remote, full redaction, console screening not sent when remote. |
| 4 | agentmemory shared across customers? | One agentmemory per installation (`SEAMLESS_MEMORY_URL`, project `seamless-migrate`); nothing in the code isolates tenants. | Yes — default: assume shared, tenant isolation (ADV-15 part) right after PR-2. |
| 5 | Current threshold, same everywhere? | `SEAMLESS_JEV_MIN_CONFIDENCE`, default `0.6`, one value for every environment. | No. |
| 6 | Default strategy below threshold | Not always cold: the deterministic choice of `select_strategy` (override → plan default → `min_downtime` tie broken by simplicity, or `simplest_meeting_slo`). The note says "kept {deterministic}". | No. |
| 7 | Roles; who approves/overrides | Roles `viewer`, `operator`, `approver`, `admin` (SDD §13); `approver` already approves cutovers. | Yes — default per spec: Admin only (`TODO(ADV-04): confirm`; `approver` is the natural alternative). |
| 8 | Retention requirements | Events are kept until `seamless events purge` is run by hand; nothing purges automatically. | Yes — default: keep forever. |
| 9 | Cutover timestamps for actual downtime | Recorded: `Migration.downtime_started_at`, `downtime_ended_at`, `actual_downtime_s` (SDD §5.2) and events `migration.downtime_started/ended`. | No. |
| 10 | New decision types in scope? | None exist. | Yes — default: enum values only, inactive. |
