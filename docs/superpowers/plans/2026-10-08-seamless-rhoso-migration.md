# Seamless Migrate 0.1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Turn the os-migrate 1.0.5 collection into Seamless Migrate 0.1.0 — a minimal-downtime migration platform from RHOSP 17.1 / community OpenStack / VMware to RHOSO 18.0 with a control plane, dashboard, Jev + agentmemory integration, and deployment on OpenShift and Docker Compose (Colima, PostgreSQL).

**Architecture:** The Ansible collection remains the data mover and gains a warm path (snapshot pre-copy + BLAKE2b chunk delta sync). A Python control plane (`seamless/`) owns plans, waves, validation, estimation, strategy selection, an FSM-driven asyncio orchestrator, REST/SSE API and RBAC, and calls the collection through `ansible-playbook`. A React dashboard consumes the API. Jev (MCP) advises within deterministic bounds; agentmemory stores lessons.

**Tech Stack:** Python 3.11+ (FastAPI, Pydantic v2, SQLAlchemy 2 Core, psycopg 3, httpx, mcp 2.x, openstacksdk, pyVmomi), Ansible core 2.16+, stdlib-only Python for conversion-host code, React 18 + Vite 8 + TypeScript 5 + Tailwind 4.3 + TanStack Query 5 + Recharts 2 (started on Vite 5 / Tailwind 3.4; upgraded 2026-10-08), PostgreSQL 16, Docker Compose on Colima, OpenShift (kustomize).

**Spec:** `docs/SDD.md` (binding; section numbers referenced as `SDD §n`) and `docs/PRD.md`.

**Status (2026-10-08):** all tracks and the integration steps are done; evidence per step is recorded in
`docs/QASuite.md` §13.1a (unit suites, PostgreSQL run, live Jev/agentmemory tests, ansible-lint, dashboard
build, Compose demo smoke test 21/21, DEMO-01…06, review fix wave, release scans, push to GitHub `main`) and the
suites run in GitHub Actions (`.github/workflows/ci.yml`, all jobs green). Reference-lab measurements
(Performance.md §6.3, QASuite §13.2 items 4–6) need a real RHOSP/RHOSO environment and are skipped until the
lab is ready (user decision, 2026-10-08).

## Global Constraints

- Never commit secrets: no API keys, passwords, tokens, `clouds.yaml`, `.env`, `tokens.yaml`. The Jev key is only ever read from the environment (`TYPESAFE_API_KEY`).
- `plugins/module_utils/blocksync.py` is Python 3.6+ standard library only (it runs on conversion hosts) and must also run as a script.
- Control plane: `requires-python = ">=3.11"`; dependency floors `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `pydantic>=2.7`, `sqlalchemy>=2.0`, `psycopg[binary]>=3.2`, `httpx>=0.27`, `pyyaml>=6.0`; extras `openstack = openstacksdk>=4.5`, `vmware = pyvmomi>=8.0`, `jev = mcp>=2.3,<3`.
- Wire formats, enums, field names, routes, roles, env vars, event kinds and finding codes are exactly those in SDD §3–§16.
- Jev pinned as `@jkudish/jev-mcp@0.14.1`; agentmemory MCP pinned as `@agentmemory/mcp@0.9.30`.
- Dashboard: Node 22, design tokens exactly SDD §16; status never conveyed by color alone.
- PostgreSQL 16 is the deployment database; SQLite only for tests/quick local runs.
- Follow TDD (superpowers:test-driven-development): failing test first, then code; commit after every task with a conventional-commit message ending in `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Track file ownership (no track edits another track's files): A = `plugins/`, `roles/`, `playbooks/`, `tests/unit/`, `tests/perf/`, `galaxy.yml`, `CHANGELOG.rst`; B = `seamless/`; C = `dashboard/`; D = `deploy/`, `scripts/compose-init.sh`, `Makefile`, `README.md`, `.mcp.json`, `.claude/`, `CLAUDE.md`, `.gitignore`, `docs/MEMORY.md`, `docs/QASuite.md`, `docs/Security.md`, `docs/Performance.md`.

## Review Focus

1. Disk sizes not a multiple of the chunk size, destination volumes larger than the source, zero-filled regions — blocksync must converge to byte-identical source content and verify the manifest digest (Task A1 tests).
2. VM names containing regex metacharacters or duplicated across projects — os-migrate filters by name; the executor must escape names and preflight must block duplicates (Tasks B3, B6 tests).
3. Control-plane restart in the middle of `cutover` — the orchestrator must resume from `checkpoint` without stopping the source twice or creating two destination servers (Task B7 test `test_resume_mid_cutover_is_idempotent`).
4. Jev unavailable, slow, or returning `invalid_response`/escape hatches — the advisor must fall back to deterministic rules and never select an ineligible strategy (Task B5 tests).
5. An operator action racing the driver (rollback requested while a step runs; cutover requested twice) and SSE clients reconnecting with `since` — no lost or duplicated transitions/events (Tasks B7, B8 tests).

---

## Track A — Collection warm path (owner: implementer A)

### Task A1: Block delta-sync engine

**Files:**
- Create: `plugins/module_utils/blocksync.py`
- Test: `tests/unit/test_blocksync.py`

**Interfaces:**
- Produces: `DEFAULT_CHUNK_SIZE = 4194304`; `chunk_digest(data: bytes) -> bytes`; `zero_digest(length: int) -> bytes`; `device_size(path: str) -> int`; `manifest_digest(digests: Iterable[bytes]) -> bytes`; `run_sender(device: str, chunk_size: int, workers: int, inp: BinaryIO, out: BinaryIO) -> SenderStats`; `run_receiver(device: str, chunk_size: int, workers: int, sender_cmd: list[str], assume_zero: bool = False, progress_cb=None) -> ReceiverSummary`; `main(argv: list[str] | None = None) -> int` (CLI per SDD §6.2, exit codes 0/2/3/4).

- [x] **Step 1: Write failing tests** — `test_identical_devices_transfer_nothing` (`chunks_changed == 0`, `bytes_transferred == 0`), `test_changed_chunks_only_are_sent` (modify 3 of 16 chunks → `chunks_changed == 3`, files byte-identical afterwards), `test_zero_chunks_use_zero_frames` (zeroed region → no data bytes counted for it, dest zeroed), `test_last_partial_chunk` (size = 10 MiB + 123 B), `test_dest_larger_than_source_ok` (only source range compared/written), `test_dest_smaller_exits_4`, `test_chunk_size_mismatch_exits_3`, `test_assume_zero_full_copy` (fresh zero file, all non-zero chunks sent), `test_workers_1_and_4_equivalent`, `test_corrupted_frame_detected_exits_3` (sender wrapper flips a byte → manifest mismatch). Tests drive the real CLI through `sys.executable plugins/module_utils/blocksync.py` subprocesses on temp files.
- [x] **Step 2: Run** `pytest tests/unit/test_blocksync.py -v` → FAIL (module missing).
- [x] **Step 3: Implement** protocol v1 exactly as SDD §6.2: receiver uses one thread sending digests and one applying frames; bounded in-order read-ahead with `concurrent.futures.ThreadPoolExecutor(workers)`; `os.pread`/`os.pwrite`; fsync at end; JSON progress on stderr; summary JSON as last stdout line.
- [x] **Step 4: Run** tests → PASS. Also `python3 plugins/module_utils/blocksync.py --help` works.
- [x] **Step 5: Commit** `feat(collection): add blocksync chunk delta-sync engine`.

### Task A2: Warm migration module_utils and modules

**Files:**
- Create: `plugins/module_utils/warm_migration.py`, `plugins/modules/import_workload_warm_snapshot.py`, `plugins/modules/import_workload_warm_sync.py`
- Test: `tests/unit/test_warm_migration.py`

**Interfaces:**
- Consumes: `volume_common.OpenStackVolumeBase`, `RemoteShell`, `use_lock`, `ATTACH_LOCK_FILE_SOURCE/DESTINATION`; `server.Server`, `server_volume.ServerVolume`; blocksync CLI from A1.
- Produces: `WarmState.load(state_dir, server_id) -> WarmState`, `.save()`, fields per SDD §6.3; `build_warm_block_device_mapping(dest_volumes: dict) -> list[dict]` (boot `/dev/vda` index 0, others −1, all `delete_on_termination: False`); `next_pass_kind(state: WarmState, requested: str) -> str` (`auto` → `full` if no dest volumes else `delta`); `parse_blocksync_progress(line: str) -> dict | None`; `blocksync_receive_command(...) -> list[str]` (quotes device paths, uses `/tmp/seamless-blocksync-{transfer_uuid}.py`); classes `OpenstackWarmSnapshot(OpenStackVolumeBase)` with `create()`/`cleanup()` and `OpenstackWarmSync(OpenStackVolumeBase)` with `sync(pass_kind) -> dict` (sync_pass dict).

- [x] **Step 1: Failing tests** — `test_warm_state_roundtrip_atomic` (file mode 0600, temp+rename), `test_next_pass_kind_auto`, `test_build_bdm_marks_boot_and_keeps_volumes`, `test_parse_progress_line`, `test_receive_command_quotes_paths`, `test_snapshot_create_is_idempotent_per_transfer_uuid` (fake conn records calls; second `create()` with same uuid creates nothing), `test_snapshot_cleanup_deletes_tmp_volumes_and_snapshots`, `test_sync_creates_dest_volumes_only_on_first_pass` (fake conn + fake shell), `test_sync_records_pass_in_state`.
- [x] **Step 2: Run** `pytest tests/unit/test_warm_migration.py -v` → FAIL.
- [x] **Step 3: Implement** module_utils (fake-friendly: inject conn and shell factory) and the two modules (argument specs per SDD §6.4, `os_auth.openstack_full_argument_spec`, `no_log=True` on key paths).
- [x] **Step 4: Run** unit tests → PASS; `python -m py_compile` on modules.
- [x] **Step 5: Commit** `feat(collection): add warm snapshot and sync modules`.

### Task A3: Warm role and playbooks

**Files:**
- Create: `roles/import_workloads_warm/{tasks/main.yml,tasks/precopy.yml,tasks/cutover.yml,tasks/rollback.yml,defaults/main.yml,meta/main.yml,README.md}`, `playbooks/import_workloads_precopy.yml`, `playbooks/import_workloads_cutover.yml`, `playbooks/rollback_workloads.yml`

**Interfaces:**
- Consumes: A2 modules; existing `conversion_host` role `conv_hosts_inventory.yml`/`conv_host_details.yml`, `import_workload_create_instance`, `os_migrate.os_migrate.server_action`.
- Produces: playbooks with the variables of SDD §6.5 (`os_migrate_warm_chunk_size`, `os_migrate_warm_workers`, `os_migrate_rollback_delete_dest_volumes`).

- [x] **Step 1:** Write the role/playbooks mirroring `import_workloads` structure (block/rescue cleanup of tmp snapshots on failure).
- [x] **Step 2: Verify** `ansible-playbook --syntax-check -i inventory/localhost.yml playbooks/import_workloads_precopy.yml` (and the other two) inside a venv with `ansible-core` and `ANSIBLE_COLLECTIONS_PATH` pointing at a temp `ansible_collections/os_migrate/os_migrate` symlink → exit 0; `ansible-lint` on the new role/playbooks → no errors.
- [x] **Step 3: Commit** `feat(collection): add warm migration role and playbooks`.

### Task A4: qemu-nbd hardening

**Files:**
- Modify: `roles/import_from_hypervisor/tasks/process_disk.yml`, `roles/import_from_hypervisor/defaults/main.yml`, `roles/import_from_hypervisor/README.md`

- [x] **Step 1:** Add `--read-only` when `os_migrate_nbdkit_readonly`, `--bind {{ os_migrate_nbdkit_bind_address }}` (new default `127.0.0.1`), `--shared=1`, `quote` filters for paths and instance ids; document TCP-mode bind requirement and SSH mode.
- [x] **Step 2: Verify** syntax-check of `playbooks/import_from_hypervisor.yml` → exit 0; ansible-lint clean on the role.
- [x] **Step 3: Commit** `fix(collection): export hypervisor disks read-only and bound`.

### Task A5: blocksync benchmark + collection metadata

**Files:**
- Create: `tests/perf/bench_blocksync.py`, `tests/perf/README.md`
- Modify: `galaxy.yml` (version `1.1.0`, description mentions warm migration to RHOSO), `CHANGELOG.rst`

- [x] **Step 1:** Benchmark script: creates sparse/random temp devices (default 2 GiB, configurable), runs full (`--assume-zero`) and delta (1 %, 5 %, 20 % changed) passes for chunk sizes 1/4/16 MiB and workers 1/4, prints a Markdown table (MiB/s scanned, bytes transferred, wall time) and writes `tests/perf/results-<host>.md`.
- [x] **Step 2: Run** `python3 tests/perf/bench_blocksync.py --size-gib 1` → table printed; commit the results file.
- [x] **Step 3: Commit** `chore(collection): blocksync benchmark and 1.1.0 metadata`.

---

## Track B — Control plane (owner: implementer B)

All paths relative to `seamless/`. Package `src/seamless_migrate/`. Tests under `tests/`, run with `pytest -q` (pytest-asyncio `asyncio_mode = "auto"`).

### Task B1: Scaffold, settings, enums, models, FSM

**Files:** Create `pyproject.toml`, `README.md`, `src/seamless_migrate/{__init__.py,config.py}`, `src/seamless_migrate/domain/{__init__.py,enums.py,models.py,fsm.py}`; Test `tests/test_models.py`, `tests/test_fsm.py`, `tests/test_config.py`.

**Interfaces:** Produces `Settings.from_env(env: Mapping[str,str] | None = None) -> Settings` (every variable in SDD §15.1), all models of SDD §4.2, `fsm.TRANSITIONS: dict[Phase, frozenset[Phase]]`, `fsm.transition(m: Migration, to: Phase, reason: str, actor: str = "system") -> Migration`, `fsm.InvalidTransition`, `fsm.is_terminal(phase)`, `fsm.WAVE_COMPLETE_PHASES`, `fsm.SUCCESS_PHASES`.

- [x] **Step 1: Failing tests** — `test_every_allowed_transition_succeeds` and `test_every_other_transition_raises` (exhaustive over Phase×Phase against SDD §5.1 table), `test_failed_to_cancelled_requires_no_downtime`, `test_retry_increments_attempts`, `test_vmref_used_bytes_fallback_is_60_percent`, `test_models_roundtrip_json` (Plan/Migration `model_dump_json` → `model_validate_json` equal), `test_settings_defaults_match_sdd`, `test_settings_pg_url_from_env`.
- [x] **Step 2–4:** run → FAIL, implement, run → PASS.
- [x] **Step 5: Commit** `feat(seamless): scaffold control plane domain model and FSM`.

### Task B2: Store and event bus

**Files:** Create `src/seamless_migrate/{store.py,events.py}`; Test `tests/test_store.py`, `tests/test_events.py`.

**Interfaces:** `Store(url: str)`, `create_schema()`, `ping() -> bool`, `put/get/list/delete/append_event/events` per SDD §11, `ConflictError`, `NotFound`; `EventBus.publish(event: Event, persist: bool)`, `EventBus.subscribe(since: int = 0) -> AsyncIterator[Event]`, `emit(store, bus, kind, message, *, plan_id=None, migration_id=None, actor="system", data=None, persist=True) -> Event`.

- [x] **Step 1: Failing tests** — `test_put_get_roundtrip`, `test_optimistic_conflict_raises`, `test_list_filters`, `test_events_since_and_filters`, `test_ephemeral_events_not_persisted`, `test_subscribe_replays_since_then_live`; the same store tests parametrized over `[sqlite, postgres]` where postgres runs only if `SEAMLESS_TEST_PG_URL` is set.
- [x] **Step 2–4:** FAIL → implement → PASS (run once with `SEAMLESS_TEST_PG_URL` exported to exercise PostgreSQL).
- [x] **Step 5: Commit** `feat(seamless): add SQLAlchemy store (PostgreSQL/SQLite) and event bus`.

### Task B3: Planning — estimator, selector, preflight, waves

**Files:** Create `src/seamless_migrate/planning/{__init__.py,estimator.py,selector.py,preflight.py,waves.py}`; Test `tests/test_estimator.py`, `tests/test_selector.py`, `tests/test_preflight.py`, `tests/test_waves.py`.

**Interfaces:** `EstimatorParams`, `estimate(vm, strategy, params, slo_s) -> Estimate` (SDD §9.1); `eligibility(vm, source_kind, plan, src_caps, dst_caps, findings) -> dict[Strategy, list[str]]`, `select_strategy(vm, estimates, plan) -> tuple[Strategy, str]`, `tie_set(estimates) -> list[Strategy]` (SDD §9.2); `SourceInventory`, `DestinationInventory` dataclasses, `run_preflight(vm, plan, src_inv, dst_inv, all_vms) -> list[Finding]` (SDD §9.3 catalog); `heuristic_tier(vm) -> str`, `plan_waves(vms, tiers, max_wave_size=10) -> list[Wave]` (SDD §9.4).

- [x] **Step 1: Failing tests** — `test_cold_downtime_formula` (100 GiB used, 125 MiB/s → `60+30+819.2+60+120`), `test_warm_converges_and_counts_passes`, `test_warm_scan_floor_applies`, `test_handover_downtime_independent_of_size`, `test_vmware_warm_uses_exact_delta`, `test_ineligible_strategies_marked`, `test_multiattach_blocks_warm`, `test_handover_requires_backend_map_and_admin`, `test_vmware_warm_requires_cbt`, `test_override_ignored_when_ineligible`, `test_min_downtime_tie_prefers_simpler`, one test per finding code (`test_finding_<code_lower>`), `test_quota_aggregates_across_plan`, `test_duplicate_names_blocked`, `test_pilot_wave_first`, `test_app_tag_kept_together`, `test_manual_review_last_wave`.
- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: Commit** `feat(seamless): add estimator, strategy selector, preflight and wave planner`.

### Task B4: Providers

**Files:** Create `src/seamless_migrate/providers/{__init__.py,base.py,fake.py,openstack.py,vmware.py,registry.py}`; Test `tests/test_providers.py`.

**Interfaces:** protocols and inventories per SDD §10; `FakeSourceProvider(kind: ProviderKind, seed: int)` (24 OpenStack VMs or 12 VMware VMs with the traits in SDD §10), `FakeDestinationProvider(seed)`, `OpenStackProvider(provider: Provider, settings)`, `VMwareProvider(provider, settings)`, `registry.build(provider, settings) -> SourceProvider | DestinationProvider`, `ProviderError`.

- [x] **Step 1: Failing tests** — `test_fake_openstack_inventory_has_required_traits` (multiattach, vGPU flavor, rhel6, windows AD, 3 DBs ≥ 500 GiB), `test_fake_vmware_mixed_cbt`, `test_registry_returns_fake_in_demo`, `test_openstack_provider_maps_server_to_vmref` (stub `openstack.connect` returning fakes with servers/volumes/ports/flavors), `test_openstack_check_reports_admin_and_ovn`, `test_vmware_provider_maps_vm` (stub pyVmomi objects), `test_missing_optional_dependency_raises_provider_error`.
- [x] **Step 2–4:** FAIL → implement (lazy imports; `asyncio.to_thread`) → PASS.
- [x] **Step 5: Commit** `feat(seamless): add OpenStack, VMware and demo providers`.

### Task B5: AI — Jev client, advisor, agentmemory, knowledge

**Files:** Create `src/seamless_migrate/ai/{__init__.py,jev.py,advisor.py,memory.py,knowledge.py}`; Copy recorded fixtures into `tests/fixtures/jev_{decide,classify,verify,screen}_response.json`; Test `tests/test_jev.py`, `tests/test_advisor.py`, `tests/test_memory.py`, `tests/test_knowledge.py`, `tests/test_live_integrations.py` (skipped unless `SEAMLESS_LIVE_JEV=1` / `SEAMLESS_LIVE_MEMORY=1`).

**Interfaces:** `JevClient(settings, session_factory=None)` with async `decide/classify/verify/screen` returning dicts, `JevUnavailable`, circuit breaker (3 failures → open 300 s); `Advisor(jev: JevClient | None, settings)` with `recommend_strategy(vm, estimates, plan) -> AdvisorNote | None`, `classify_workloads(vms) -> tuple[dict[str,str], AdvisorNote]`, `review_verification(vm, result) -> AdvisorNote | None` (SDD §14.2); `redact(text: str, redact_names: list[str] | None = None) -> str`; `MemoryClient(url, secret, project, transport=None)` with `remember/search/health` (SDD §14.3), `MemoryHit`; `KnowledgeService(memory, store, bus)` with `on_failure/on_completed/on_rolled_back`.

- [x] **Step 1: Failing tests** — `test_decide_parses_fixture`, `test_circuit_breaker_opens_after_three_failures`, `test_stdio_env_whitelist` (child env only PATH/HOME/provider vars), `test_recommend_applies_only_within_tie_set_and_threshold`, `test_recommend_ignores_escape_hatch_and_escalate`, `test_recommend_never_returns_ineligible`, `test_classify_review_items_fall_back_to_heuristic`, `test_verification_blocked_console_sets_review_required`, `test_verification_contradiction_sets_review_never_flips_pass`, `test_redact_removes_secrets` (password=, token, Bearer, PEM, `scheme://u:p@h`), `test_memory_remember_payload_and_auth_header` (httpx.MockTransport), `test_memory_search_tolerant_parsing`, `test_memory_failures_swallowed`, `test_knowledge_on_failure_attaches_hits`; live: `test_live_jev_decide` (uses stdio with `TYPESAFE_API_KEY`), `test_live_memory_roundtrip` (uses `SEAMLESS_MEMORY_URL`).
- [x] **Step 2–4:** FAIL → implement → PASS (also run the live tests once with the provided env).
- [x] **Step 5: Commit** `feat(seamless): add Jev advisor and agentmemory knowledge integration`.

### Task B6: Executors

**Files:** Create `src/seamless_migrate/executors/{__init__.py,base.py,simulated.py,ansible.py,handover.py,router.py}`, `src/seamless_migrate/security/secrets.py`; Test `tests/test_executor_simulated.py`, `tests/test_executor_ansible.py` (with a fake `ansible-playbook` script on PATH that records argv/vars and writes os-migrate state/warm files), `tests/test_executor_handover.py`, `tests/test_secrets.py`.

**Interfaces:** SDD §7.1 contract; `SimulatedExecutor(settings)`; `AnsibleExecutor(settings)` with pure helpers `workload_filter(name) -> list[dict]`, `apply_mappings(doc, mappings) -> dict`, `build_vars(step, ctx) -> dict`, `build_inventory(ctx) -> str`, `classify_failure(output: str) -> type[Exception]`; `HandoverExecutor(settings, conn_factory)` with journal file `handover-journal.json`; `ExecutorRouter(settings).for_strategy(strategy) -> Executor`; `secrets.resolve(name, settings) -> dict`, `secrets.load_cloud_auth(cloud, settings) -> dict`.

- [x] **Step 1: Failing tests** — `test_simulated_warm_pass_deltas_shrink`, `test_simulated_cutover_marks_downtime`, `test_simulated_failure_rate_seeded`, `test_workload_filter_escapes_regex`, `test_apply_mappings_rewrites_refs`, `test_secrets_file_0600_and_deleted_after_run`, `test_warm_precopy_runs_export_once_then_precopy`, `test_cutover_cold_sets_stop_before_migration`, `test_vmware_warm_flags` (`cbt_sync`/`cutover`), `test_progress_tail_reports_pct`, `test_transient_vs_permanent_failure`, `test_handover_journal_resume_skips_done_steps`, `test_handover_rollback_reverses_order`, `test_secret_resolution_file_then_env`.
- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: Commit** `feat(seamless): add simulated, Ansible and storage-handover executors`.

### Task B7: Orchestrator and verification

**Files:** Create `src/seamless_migrate/{orchestrator.py,verification.py}`; Test `tests/test_orchestrator.py`, `tests/test_verification.py`.

**Interfaces:** SDD §8 public API; `Verifier(dst_provider, settings).verify(ctx) -> VerificationResult`.

- [x] **Step 1: Failing tests** (SimulatedExecutor with `demo_speed` high and `tick_s` small) — `test_validate_creates_migrations_with_findings_and_estimates`, `test_start_rejects_blocked_plan`, `test_warm_flow_reaches_completed_with_downtime`, `test_cold_flow`, `test_requires_approval_waits`, `test_cutover_window_respected_and_force_window`, `test_keep_warm_pass_runs_after_interval`, `test_wave_dependencies_respected`, `test_max_concurrent_cutovers`, `test_failed_cutover_auto_rolls_back`, `test_rollback_request_during_step_is_serialized`, `test_retry_after_failure`, `test_finalize_requires_confirm_name`, `test_resume_mid_cutover_is_idempotent`, `test_advisor_tie_break_recorded`, `test_similar_incidents_attached_on_failure`, `test_verification_checks` (server/ports/tcp/console), `test_verification_console_unavailable_skips_with_warning`.
- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: Commit** `feat(seamless): add orchestrator and post-cutover verification`.

### Task B8: REST API, auth, SSE, stats, metrics

**Files:** Create `src/seamless_migrate/api/{__init__.py,app.py,deps.py,schemas.py,routes_providers.py,routes_plans.py,routes_migrations.py,routes_events.py,routes_misc.py}`, `src/seamless_migrate/security/{__init__.py,auth.py}`, `src/seamless_migrate/metrics.py`; Test `tests/test_api_*.py`, `tests/test_auth.py`.

**Interfaces:** `create_app(settings, store=None, orchestrator=None) -> FastAPI` (lifespan starts/stops orchestrator), routes exactly SDD §12; `TokenStore.from_file(path)`, `hash_token(token)`, `generate_token() -> str` (`smg_` prefix), `require_role(role)` dependency; SPA static serving.

- [x] **Step 1: Failing tests** — `test_health_public_reports_db`, `test_unauthenticated_401_and_audit_event`, `test_role_matrix` (parametrized over every route × role), `test_error_envelope_shape`, `test_provider_crud`, `test_plan_create_validate_start_flow`, `test_migration_actions_transitions` (approve/cutover/sync/rollback/retry/cancel/finalize/strategy), `test_finalize_confirm_mismatch_400`, `test_events_since`, `test_sse_stream_resume_and_heartbeat`, `test_stats_shape`, `test_metrics_format`, `test_spa_fallback_serves_index`, `test_auth_disabled_only_on_loopback` (CLI guard helper).
- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: Commit** `feat(seamless): add REST/SSE API with RBAC, stats and metrics`.

### Task B9: CLI, demo seeding, container image

**Files:** Create `src/seamless_migrate/{cli.py,demo.py,__main__.py}`, `Containerfile`, `.containerignore` (in `seamless/`); Test `tests/test_cli.py`, `tests/test_demo.py`.

**Interfaces:** `main(argv: list[str] | None = None) -> int` (SDD §15), console script `seamless`; `seed_demo(store, settings) -> None` (3 providers, 2 plans "Finance apps (RHOSP 17.1 → RHOSO)" and "DC2 VMware exit", validated and started so the dashboard shows every phase within minutes).

- [x] **Step 1: Failing tests** — `test_token_create_prints_token_and_yaml_hash`, `test_plan_apply_from_yaml`, `test_estimate_table`, `test_serve_refuses_auth_disabled_on_public_bind`, `test_demo_seed_idempotent`.
- [x] **Step 2–4:** FAIL → implement → PASS; full suite `pytest -q` green; `ruff check` clean.
- [x] **Step 5:** `Containerfile` per SDD §17 (UBI 9 python-311, non-root, installs `.[openstack,vmware,jev]`, ansible-core, the collection from the repo root, Node 22 for `npx`, copies `dashboard/dist` when present via build arg); build context = repo root.
- [x] **Step 6: Commit** `feat(seamless): add CLI, demo mode and container image`.

---

## Track C — Dashboard (owner: implementer C)

### Task C1: Scaffold, design system, shell, API layer

**Files:** Create `dashboard/{package.json,index.html,vite.config.ts,tsconfig.json,tailwind.config.ts,postcss.config.js,.eslintrc.cjs}`, `dashboard/design-system/` (persisted ui-ux-pro-max output), `dashboard/src/{main.tsx,App.tsx,index.css}`, `dashboard/src/api/{types.ts,client.ts,stream.ts,mock.ts,hooks.ts}`, `dashboard/src/components/{Layout.tsx,ThemeToggle.tsx,StatusBadge.tsx,PhaseStepper.tsx,KpiTile.tsx,ConfirmDialog.tsx,EmptyState.tsx,ErrorBanner.tsx}`, `dashboard/src/lib/format.ts`; Test `dashboard/src/**/*.test.ts(x)`.

**Interfaces:** `types.ts` mirrors SDD §4 and §12 exactly; `ApiClient` (`get/post/put/patch/delete`, `ApiError {status, code, message}`), `streamEvents(since, onEvent, signal)` (fetch streaming SSE parser with resume); `phaseMeta(phase) -> {label, tone, icon}`; formatters `formatBytes`, `formatDuration`, `formatPct`, `formatRelative`.

- [x] **Step 1:** Run `python3 <ui-ux-pro-max>/scripts/search.py "devops monitoring admin dashboard" --design-system -p "Seamless Migrate" --density 8 --motion 3 --variance 3 --persist --page dashboard` from `dashboard/` and commit `design-system/`.
- [x] **Step 2: Failing tests** — `format.test.ts` (bytes/duration/pct edge cases), `phase.test.ts` (every Phase has label+tone+icon), `stream.test.ts` (parses multi-line SSE chunks, ignores heartbeats, tracks last id), `client.test.ts` (bearer header, error envelope → ApiError, 401 → onUnauthorized).
- [x] **Step 3–4:** implement → `npm test` PASS, `npm run typecheck` PASS.
- [x] **Step 5: Commit** `feat(dashboard): scaffold design system, shell and API layer`.

### Task C2: Overview, Providers, Inventory

**Files:** Create `dashboard/src/pages/{Overview.tsx,Providers.tsx,Inventory.tsx}` and components `ThroughputChart.tsx`, `DowntimeSloChart.tsx`, `PhaseDistribution.tsx`, `VmTable.tsx`; tests for VmTable filtering and Overview rendering with mock data.

- [x] Steps: failing tests → implement → `npm test` → commit `feat(dashboard): overview, providers and inventory pages`.

### Task C3: Plans

**Files:** Create `dashboard/src/pages/{Plans.tsx,PlanDetail.tsx}`, components `WavesBoard.tsx`, `MigrationsTable.tsx`, `FindingsList.tsx`, `EstimateCell.tsx`, `PlanCreateDialog.tsx`; tests for MigrationsTable sorting/filtering and action buttons enabled by plan status.

- [x] Steps: failing tests → implement → `npm test` → commit `feat(dashboard): plans list and plan detail`.

### Task C4: Migration detail, Events, Advisor, Login

**Files:** Create `dashboard/src/pages/{MigrationDetail.tsx,Events.tsx,Advisor.tsx,Login.tsx}`, components `SyncPassChart.tsx`, `DowntimeClock.tsx`, `Timeline.tsx`, `MigrationActions.tsx`, `AdvisorNotes.tsx`; tests: actions enabled per phase+role (e.g. Finalize only in `completed` for approver, requires typing VM name), downtime clock counts from `downtime_started_at`, events list appends streamed events.

- [x] Steps: failing tests → implement → `npm test`, `npm run typecheck`, `npm run build` → commit `feat(dashboard): migration detail, events, advisor and login`.

### Task C5: Accessibility and polish pass

- [x] Verify the ui-ux-pro-max pre-delivery checklist (no emoji icons, cursor-pointer, 150–300 ms transitions, contrast ≥ 4.5:1 in both themes — add `contrast.test.ts` computing WCAG ratios for token pairs, focus-visible rings, reduced motion, 375/768/1024/1440 layouts) → commit `chore(dashboard): accessibility and responsive polish`.

---

## Track D — Tooling, deployment, documentation (owner: implementer D)

### Task D1: Agent tooling and README

**Files:** Create `.mcp.json` (servers `jev`: `npx -y @jkudish/jev-mcp@0.14.1`, env `TYPESAFE_API_KEY: ${TYPESAFE_API_KEY}`; `agentmemory`: `npx -y @agentmemory/mcp@0.9.30`, env `AGENTMEMORY_URL`, `AGENTMEMORY_SECRET`), `.claude/settings.json` (`extraKnownMarketplaces` for `obra/superpowers-marketplace` and `nextlevelbuilder/ui-ux-pro-max-skill`; `enabledPlugins` `superpowers@superpowers-marketplace`, `ui-ux-pro-max@ui-ux-pro-max-skill`), `CLAUDE.md` (how agents work here: SDD is binding, superpowers workflow, Jev usage rules, agentmemory conventions, never commit secrets); Modify `README.md` (new top section: what Seamless Migrate is, quick start with Colima/Compose, links to docs; keep the os-migrate content below under "Collection reference").

- [x] Validate JSON with `python3 -m json.tool` → commit `chore: add MCP servers, Claude plugins and agent guide`.

### Task D2: Compose on Colima and OpenShift manifests

**Files:** Create `deploy/compose/compose.yaml`, `deploy/compose/README.md`, `scripts/compose-init.sh`, `deploy/openshift/{kustomization.yaml,namespace.yaml,serviceaccount.yaml,configmap.yaml,secret-example.yaml,postgres-statefulset.yaml,postgres-service.yaml,pvc.yaml,deployment.yaml,service.yaml,route.yaml,networkpolicy.yaml}`; Modify `Makefile` (targets of SDD §17.1), `.gitignore` (`deploy/compose/.env`, `deploy/compose/tokens.yaml`).

- [x] Verify `docker compose -f deploy/compose/compose.yaml config` (with a dummy `.env`) succeeds and `kubectl kustomize deploy/openshift` (or `python3 -c yaml.safe_load_all`) parses → commit `feat(deploy): compose stack for Colima and OpenShift manifests`.

### Task D3: Documentation set

**Files:** Create `docs/MEMORY.md`, `docs/QASuite.md`, `docs/Security.md`, `docs/Performance.md`.

- [x] Write each document from SDD/PRD (sections listed in the dispatch brief); every requirement/threat/test references SDD sections and the concrete test names listed in this plan → commit `docs: add memory, QA suite, security and performance documents`.

---

## Integration (controller)

- [x] Merge track branches into `feat/seamless-rhoso-migration`; run collection unit tests, control-plane tests (SQLite + PostgreSQL), dashboard tests/build.
- [x] `make seamless-up` on Colima profile `seamless`; smoke-test `/api/v1/health`, demo flow, dashboard, Jev via HTTP sidecar, agentmemory via host.
- [x] Final whole-branch review (superpowers:requesting-code-review, most capable model) + Jev `jev_gate` on completion claims; one fix wave; reconcile docs with measured numbers.
- [x] Push `main` and `feat/seamless-rhoso-migration` to `https://github.com/imransetiadi/seamless-vms-migration.git` after a secret scan.
