# Network cut-over — implementation plan (2026-10-21)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task by task.

**Goal (release 0.4.0; PRD FR-28 and the release table's "Octavia/Heat migration helpers"):**
- At cutover, a VM's network identity follows it where Seamless can move it: a floating IP that keeps its address, a
  DNS record swapped, and BGP announcements that follow the floating IP.
- An admin's hooks run at fixed points of each migration for what Seamless cannot move.
- The pre-flight reports what is not moved: Heat stacks, autoscaling groups, Octavia memberships, an address change,
  `allowed_address_pairs`, split prefixes, and DNS integration.
- Octavia memberships move with a helper, as a Technology Preview.
- Everything is validated in simulation until the LAB-NET cases run on two clouds (SDD §23.10).

**Spec:** SDD §23 (each item binding once its task moves it into the section §23.12 names), D18, §1 and D3 (the gap
0.1.0 left); PRD goal G9, FR-28 and FR-42–FR-47 (N1).

**Decisions taken:** the owner approved SDD §23.11's eleven defaults on 2026-10-21 (D18). The research behind them was
done the same day from the platforms' documentation; the pull request that proposed this plan (#75) lists its sources.

## Rules for every task

- The SDD amendment that makes the task's item binding comes first, in its own commit, by the controller (CLAUDE.md
  §1, §3). It moves the item from §23 into the section §23.12 names. The SDD-reading tests stay green: a finding code
  enters §9.3 with its catalog entry (N2), and a route enters §12 with its handler (N3).
- TDD: the named tests fail first for the stated reason, then pass, and mutants of the new logic are killed. A test
  named here keeps its name.
- Track ownership as in CLAUDE.md §3. Where a shared fixture holds two tracks to one contract, the task's pull request
  carries one commit per track.
- Each task's pull request adds its QASuite rows (track D) naming the tests it landed.
- Fixtures are synthetic: made-up addresses from the documentation ranges (192.0.2.0/24, 198.51.100.0/24,
  203.0.113.0/24, 2001:db8::/32), made-up zones under `example.com`, and no customer host.
- A hook is code execution. No test, fixture or demo hook reaches a network outside the test process, and no hook text
  goes to Jev or agentmemory.
- Technology Preview: no text, test name, doc or commit claims the network cut-over was validated on two clouds until
  N14's cases pass (SDD §23.10).

## Tasks

| # | Track | Task | Tests that must fail first, then pass |
|---|---|---|---|
| N1 | controller | The PRD amendment (done with D18: goal G9, FR-28, FR-42–FR-47, journey 8, the release table and §3's non-goals as delivered), in its own commit; then each task's SDD commit | after each SDD commit, the SDD-reading tests: `test_catalog_matches_sdd_9_3`, `test_event_kinds_match_sdd_4_3_and_the_dashboard`, `test_every_route_is_documented_in_sdd_12`, `test_settings_read_exactly_the_variables_sdd_15_1_documents` |
| N2 | B, C, D | In two parts: N2a (done: the Heat and Octavia findings, the source's reads, `Plan.acknowledged_findings`, the mock's) and N2b (done: the network findings, `Nic`'s address pairs, floating IPs and DNS names, the destination's floating IPs and DNS, `Provider.announces_tenant_networks`, the mock's). The findings of §23.8 in §9.3, the catalog, the API's pre-flight and the mock. The source providers read a VM's Heat stack (the stack resources by `physical_resource_id`, nested depth) and its Octavia memberships (members by address), and the inventory reads `allowed_address_pairs`; each read is optional and its failure is a warning, never a blocker | `tests/test_preflight.py`: `test_finding_src_vm_heat_stack`, `test_finding_src_vm_autoscaled_blocks_until_acknowledged`, `test_finding_src_vm_lb_member`, `test_finding_map_fip_address_change`, `test_finding_src_port_address_pairs`, `test_finding_net_prefix_split`, `test_finding_dst_dns_integration_missing`; `tests/test_providers.py`: `test_a_source_without_heat_or_octavia_reads_no_stack_and_no_member`; the shared finding cases (`eligibility_cases.json`) |
| N3 | B | Done (with the dashboard's types and the mock's refusal; the CLI's `plan apply` checks hooks as POST does). The domain: a hook catalog (admin) with `playbook` and `webhook` kinds, a plan's network cut-over settings (hooks per point, the floating IP opt-in, DNS targets, Octavia pool mappings), and their routes in §12 | `tests/test_api_hooks.py`: `test_only_an_admin_registers_a_hook`, `test_a_plan_references_hooks_by_name_per_point`, `test_a_webhook_hook_needs_https`, `test_a_hook_in_use_cannot_be_deleted`; `tests/test_models.py`: `test_a_plans_network_cutover_settings_round_trip` |
| N4 | B | Done (`seamless_migrate.hooks`; the hook points that call it are N5's). The hook runner: the context document, the idempotency key, timeouts, secret files, `redact()` on output, and `migration.action` events | `tests/test_hooks.py`: `test_a_playbook_hook_gets_the_context_and_no_credential`, `test_a_webhook_hook_posts_the_context_with_its_bearer_secret`, `test_a_hook_that_exceeds_its_timeout_fails`, `test_a_hooks_output_is_redacted`, `test_a_rerun_hook_gets_the_same_idempotency_key` |
| N5 | B | The hook points in the cutover, verification, rollback and finalize of every strategy, resumed after a restart: a `pre_stop` failure keeps the source running, and a failure after the stop follows `auto_rollback` | `tests/test_orchestrator.py`: `test_a_pre_stop_hook_failure_keeps_the_source_running`, `test_a_post_create_hook_failure_rolls_back_under_auto_rollback`, `test_a_hooks_revert_runs_on_rollback`, `test_a_restart_resumes_a_hook_point_with_the_same_key`, `test_post_verify_hooks_run_only_after_the_verification_passed` |
| N6 | B, A | The floating IP keep-address move (§23.4): pre-flight blockers, the reservation in pre-staging, the journaled source disassociate, the destination associate, rollback and finalize, on every OpenStack strategy. A: os-migrate's `auto` reports whether it reused or created an address | `tests/test_fip_move.py`: `test_the_preflight_blocks_an_address_outside_the_destinations_external_subnets`, `test_prestaging_reserves_the_address_detached`, `test_the_cutover_moves_the_address_after_the_stop`, `test_a_rollback_gives_the_address_back_to_the_source_before_it_starts`, `test_finalize_releases_the_sources_allocation`, `test_a_handover_moves_the_address_too`; A: `tests/unit/test_server_floating_ip.py`: `test_auto_reports_a_new_address` |
| N7 | B | The DNS swap (§23.5): Designate and RFC 2136 back ends, compare-and-swap, polling, revert, and the TTL warning | `tests/test_dns_swap.py`: `test_a_designate_swap_waits_for_the_recordset_to_be_active`, `test_an_rfc2136_swap_is_a_compare_and_swap`, `test_a_swap_that_finds_another_address_changes_nothing`, `test_a_rollback_swaps_the_record_back`, `test_a_ttl_longer_than_the_slo_warns`; the TSIG key is never logged: `test_no_tsig_key_reaches_a_log_line` |
| N8 | B | The reachability wait for a moved floating IP in the verification (§23.6), counted in the downtime | `tests/test_verification.py`: `test_a_moved_floating_ip_must_be_reachable_within_the_bound`, `test_the_reachability_wait_counts_in_the_downtime` |
| N9 | B | The Octavia member helper (§23.7): drain, join, revert; the provider-compatibility finding | `tests/test_octavia_helper.py`: `test_the_source_member_is_drained_before_the_stop`, `test_the_vm_joins_the_destination_pool_after_the_verification`, `test_a_rollback_removes_the_member_and_re_enables_the_source`, `test_an_ovn_pool_refuses_a_listener_it_cannot_serve`, `test_tls_containers_are_mapped_never_read` |
| N10 | B | The estimator: the hooks' time inside the downtime, and the DNS TTL tail shown apart (§9.1) | `tests/test_estimator.py`: `test_hooks_inside_the_downtime_add_their_timeouts`, `test_the_ttl_tail_is_reported_apart_from_the_downtime` |
| N11 | B | The demo: fake floating IPs, a DNS zone, a load balancer and a Heat stack with an autoscaling group, and a seeded plan that shows each rule | `tests/test_demo.py`: `test_the_demo_moves_a_floating_ip_with_its_address`, `test_the_demo_swaps_a_dns_record`, `test_the_demo_shows_the_heat_and_octavia_findings` |
| N12 | C | The dashboard: the plan form's "Network cut-over" section, the hook catalog page (admin), the hook timeline on the migration page, and the findings, with the Technology Preview badges (§23.9) | `Plans.test.tsx`: "maps a plan's network cut-over: hooks per point, the floating IP move, DNS targets and Octavia pools"; `Hooks.test.tsx`: "lets only an admin register a hook"; `MigrationDetail.test.tsx`: "shows each hook's run on the timeline"; `a11y.test.tsx`: the new controls pass |
| N13 | D | QASuite rows and LAB-NET cases; Security.md (hook execution, webhook secrets, TSIG keys); Operations.md runbooks (Heat stacks, Octavia load balancers, Designate zones, Manila shares) | `test_every_test_qasuite_names_exists` (unchanged, green) |
| N14 | D (lab) | LAB-NET01…: two clouds on one external VLAN, a BGP fabric with the OVN BGP agent, Designate and BIND with TSIG, Octavia amphora and OVN providers, a Heat autoscaling group; the results in QASuite §13 with the versions | the lab cases themselves |

**Order:** N1, then N2 (useful on its own), N3, N4 and N5. Then N6 and N7 in parallel, N8 after N6, and N9. N10 and N11
come after N5–N9, and N12 after N11. N13 goes with each task, and N14 last.
