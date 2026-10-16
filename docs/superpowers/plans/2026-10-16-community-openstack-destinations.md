# Community OpenStack destinations — implementation plan (2026-10-16)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task by task.

**Goal (PRD G7, FR-29–FR-34, NFR-12):** besides RHOSO 18.0, every community OpenStack release series from 2023.1
Antelope to 2026.2 Hibiscus is a destination, by the support tiers of SDD §21: each strategy a destination's
capabilities allow runs as on RHOSO 18.0, and every destination's series, tier, policy profile and missing
capabilities are shown before a plan runs. Release 0.2.0.

**Spec:** SDD §1.3, §21 (binding once each task moves its item into §4, §6, §9.3, §10 or §16), D13–D15; PRD §3, §6;
QASuite §4 (S-DST), §5, §6.7, §7.1 (M7–M12), §7.2 (LAB-D01–LAB-D08); Security.md C4-06, R-29.

**Decisions taken (Jev):** the tiered window with a 2023.1 floor (0.93); the series detected from the version documents
plus an optional declared one, the least favourable tier the range allows when none is declared (0.77).

## Rules for every task

- The SDD amendment that makes the task's item binding comes first, in its own commit (CLAUDE.md §1). The SDD-reading
  tests (`test_catalog_matches_sdd_9_3`, the §12 route, §15.1 environment and §4.3 event tests) stay green: a finding
  code enters §9.3 in the same change as its catalog entry.
- TDD: the named tests fail first for the stated reason, then pass; mutants of the new logic are killed.
- Track ownership as CLAUDE.md §3; a `fix(collection)`/`feat(collection)` commit adds its CHANGELOG.rst bullet.
- The QASuite rows that say "planned (Tn)" lose the word in the commit that lands Tn.

## Tasks

| # | Track | Task | Tests that must fail first, then pass |
|---|---|---|---|
| T1 | B | `seamless_migrate/planning/platforms.py` (pure): the series table of SDD §21.1 (series, codename, upstream phase, SLURP, the date read, compute and block-storage maxima), `release_range(compute_max, block_storage_max)`, `support_tier(range, declared)`; fixtures of recorded version documents per series | `tests/test_platforms.py`: `test_every_series_of_the_window_has_its_phase_tier_and_compute_maximum`, `test_a_compute_maximum_reads_as_the_series_range_it_fits`, `test_a_maximum_above_the_newest_known_reads_as_outside_newer`, `test_without_a_declared_series_the_tier_is_the_least_favourable_of_the_range`, `test_a_declared_series_inside_the_range_decides_the_tier`, `test_a_declared_series_outside_the_range_warns_and_the_range_decides`, `test_the_sdd_support_table_matches_the_code_series_table`, `test_recorded_version_documents_map_to_their_series`, `test_the_sdk_never_asks_more_than_its_caps_of_any_series_of_the_window` |
| T2 | B | `Provider.release: str \| null` (pattern `^20[0-9]{2}\.[12]$`; refused on a `vmware` provider; SDD §4.2, §12 POST/PATCH `/providers`) | `tests/test_models.py`: `test_provider_release_is_optional_and_validated_as_a_series`, `test_a_release_on_a_vmware_provider_is_refused`; `tests/test_api_resources.py`: a provider created and patched with `release` |
| T3 | B | `OpenStackProvider.check()` reports `release_range`, `release`, `support_tier`, `policy_profile`, `tls` (SDD §10, §21.3, §21.5, §21.7) | `tests/test_providers.py`: `test_openstack_check_reports_release_range_tier_profile_and_tls`, `test_a_plain_http_endpoint_is_reported` |
| T4 | B | Pre-flight findings `DST_CAPABILITY_MISSING` (blocker) and `DST_RELEASE_OUTSIDE_SUPPORT` (warning) with SDD §21.4's capability floor per strategy; catalog and §9.3 in one change | `tests/test_preflight.py`: `test_finding_dst_capability_missing`, `test_finding_dst_release_outside_support`, `test_catalog_matches_sdd_9_3` (unchanged, still green) |
| T5 | B | `NET_PORT_NOT_PERMITTED` and the cutover's port refusal judge by the policy profile: `admin` or `service` under secure-RBAC, `admin` or `advsvc` under legacy (SDD §6.6, §21.5); RHOSO 18.0 keeps the legacy rule until D14 is decided | `tests/test_preflight.py`: `test_finding_net_port_not_permitted_counts_service_under_secure_rbac`, `test_finding_net_port_not_permitted_counts_advsvc_only_under_the_legacy_profile`; `tests/test_executor_ansible.py`: `test_the_cutover_port_refusal_follows_the_policy_profile` |
| T6 | B, C | Destination-neutral texts: findings, events and CLI output name the destination provider, not "RHOSO"; the Providers page and dialog copy (SDD §21.8, PRD FR-34) | `tests/test_preflight.py`: `test_no_user_facing_text_assumes_rhoso_for_a_community_destination`; `Providers.test.tsx`: the page names RHOSO 18.0 and community OpenStack as destinations |
| T7 | A | `conversion_host_content`: the RHEL family by `ansible_os_family` (Rocky Linux 9, AlmaLinux 9, CentOS Stream 10), then `ubuntu.yml` for Ubuntu 24.04 LTS (`qemu-utils`, `nbdkit`, `libnbd-bin`, `libguestfs-tools`); SDD §6.5, §21.6, D15 | `tests/unit/test_conversion_host_content.py`: `test_conversion_host_content_supports_the_rhel_family_by_os_family`, `test_conversion_host_content_installs_the_copy_tools_on_ubuntu_24_04` |
| T8 | C | Dashboard: the provider card shows a destination's series range, tier and policy profile; the provider form takes the declared series for an OpenStack destination; mock parity (SDD §16) | `Providers.test.tsx`: "shows a destination's series, tier and policy profile"; `ProviderDialog.test.tsx`: "takes a declared series for a community destination"; `mock.test.ts`: the mock check reports the same keys |
| T9 | B | Demo: a community destination (2026.2 Hibiscus, Kolla-Ansible preset, secure-RBAC profile) beside `rhoso18`; the demo plans keep `rhoso18` (SDD §7.4, §10) | `tests/test_demo.py`: `test_demo_seeds_a_community_destination_beside_rhoso` |
| T10 | D | The lab: QASuite §6.7 (AC-6), §7.1 M7–M12, §7.2 LAB-D01–LAB-D08 on the reference lab; record the environment descriptor (Performance.md §2) and each destination's tier in the release report (RELEASING.md) | the lab cases themselves; their results in QASuite §13 |

Order: T1 → T2 → T3 → T4; T5 after T3 (it reads `policy_profile`); T6, T7 at any time; T8 after T2 and T3; T9 after
T3; T10 last. D14 (RHOSO 18.0's profile) is decided on the lab run before T5 changes RHOSO's rule.

## Review focus

1. A detected range that straddles tiers (2.95, 2.96) must never read as the better tier without a declared series
   (T1).
2. Capabilities block, names never do: a Tier 3 or Outside cloud with every capability runs, with a warning; a Tier 1
   cloud without Cinder manage cannot run `storage_handover` (T4).
3. The policy profile changes only who may set a port's address or MAC on another project's network; a shared network
   and the credential's own project keep their rules (T5).
4. No user-facing text claims RHOSO for a community destination, and none drops "RHOSO 18.0" where the rule is the
   product's (T6).
5. Raising the openstacksdk pin re-runs `test_the_sdk_never_asks_more_than_its_caps_of_any_series_of_the_window` (T1);
   a new upstream series joins Tier 1 only with its lab run (SDD §21.2).

## Done when

Every task's tests pass and its SDD item is binding; the whole control-plane, collection and dashboard suites are
green; the lab matrix passes on every Tier 1 series (QASuite §7.1 M7–M11) and the smoke subset on Tier 2 (M12);
Security.md R-29 is closed.
