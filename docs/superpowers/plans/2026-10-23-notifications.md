# Notifications — implementation plan (2026-10-23)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task by task.

**Goal.** The release is 0.5.0 (owner decision 1). The plan answers PRD §9's 0.2.0 row,
"notifications (email/Slack webhooks)", and the Application owner's "notifications via events" (PRD §4):
- An admin keeps a catalog of channels:
  - email through the installation's SMTP relay;
  - Slack incoming webhooks;
  - HTTPS webhooks signed as Standard Webhooks specify.
- An operator subscribes a channel to a plan, and an admin subscribes one to every plan. A subscription chooses its
  topics: approvals, downtime, problems, outcomes and progress.
- A message names the VM, the plan, the migration, the phase and the downtime. It never holds a credential, an
  address or an error's text.
- Delivery is asynchronous, at least once with a dedupe key, and ordered per channel. Retries and a message's age are
  bounded. Delivery never delays or fails a migration, and each message's outcome is an audit event.
- Fakes test everything. A live smoke against a real workspace, relay and receiver runs before the release (SDD
  §24.10).

**Spec:** SDD §24 and D19. Each item becomes binding when its task moves it into the section §24.12 names. The PRD's §4 and §9 change with NT1.

**Decisions:** the owner took the twelve defaults of SDD §24.11 as written on 2026-10-23 (D19). The research behind the
design was done on 2026-10-23 from the standards' and the vendors' documentation.
The pull request that proposes this plan lists the sources.

## Rules for every task

- **The SDD comes first.** The amendment that makes a task's item binding is its own commit, by the controller
  (CLAUDE.md §1, §3), and it moves the item from §24 into the section §24.12 names. The SDD-reading tests stay green:
  - an event kind enters §4.3, `events.py` and the dashboard's `PERSISTED_EVENT_KINDS` in one task, since
    `test_event_kinds_match_sdd_4_3_and_the_dashboard` reads all three;
  - a route enters §12 with its handler and its row in the role matrix;
  - a variable enters §15.1 with `Settings`;
  - a metric enters §18 with `/metrics`.
- **TDD.** The named tests fail first, for the stated reason, then pass. Mutants of the new logic are killed. A test
  named here keeps its name.
- **Tracks.** Track ownership is as in CLAUDE.md §3. Where a shared contract binds two tracks (the event kinds, the
  API types), the task's pull request carries one commit per track.
- **QASuite.** Each task's pull request adds its QASuite rows (track D), naming the tests it landed.
- **Synthetic fixtures:**
  - addresses under `example.com` and `example.org`;
  - made-up Slack webhook paths, too short for a secret scanner's pattern;
  - signing keys generated in the test;
  - no customer host and no real workspace.
- **No message leaves the test process.** No test, fixture or demo sends one; only NT12's `live` tests do, and only
  when enabled. Nothing of a notification goes to Jev or agentmemory.
- **Secrets stay hidden.** A Slack URL, a signing key and a relay's password never reach an event, a log line, an API
  answer or a test's failure message, and each sender's tests assert it.
- **The notifier stays out of a migration's path.** A test that needs the orchestrator to wait for a send means the
  design is wrong.

## Tasks

| # | Track | Task | Tests that must fail first, then pass |
|---|---|---|---|
| NT1 | controller | Once the owner has decided §24.11, the PRD amendment, in its own commit: a goal, FRs from FR-48 (channels, subscriptions, messages, delivery), the Application owner's surface, a journey, the release table's 0.2.0 row as delivered and a 0.5.0 row. Then each task's SDD commit | after each SDD commit, the SDD-reading tests: `test_catalog_matches_sdd_9_3`, `test_event_kinds_match_sdd_4_3_and_the_dashboard`, `test_every_route_is_documented_in_sdd_12`, `test_the_role_matrix_covers_every_route_sdd_12_lists`, `test_settings_read_exactly_the_variables_sdd_15_1_documents`, `test_every_setting_is_documented_in_sdd_15_1`, `test_metrics_exposes_every_family_sdd_18_names` |
| NT2 | B | The domain (§24.3): `NotificationChannel` (each kind's fields, the rules for recipients and URLs, `redact_names`, `enabled`), `NotificationSubscription` (the default topics), `NotifyTopic` and the trigger names, `NotificationCursor`; the document kinds `notification_channel`, `notification_subscription` and `notification_cursor` | `tests/test_models.py`: `test_an_email_channel_needs_recipients_and_takes_no_url`, `test_a_webhook_channels_url_is_https_without_user_information`, `test_a_slack_channel_holds_no_url_in_its_document`, `test_a_channel_name_follows_the_hook_name_rule`, `test_a_subscription_defaults_to_approval_downtime_and_problem`; `tests/test_store.py`: `test_notification_channels_subscriptions_and_cursors_round_trip` |
| NT3 | B, C | The routes of §24.3 (except the test) and §24.4, with their roles and refusals. The write-only secret route under `notify-{name}`, with a Slack secret checked to be an incoming webhook's URL. The `notify-` prefix refused for a provider's, a hook's and a DNS target's secret. `channel.*` and `subscription.*` in `events.py`, §4.3 and the dashboard's kind list. C: the API types | `tests/test_api_notifications.py`: `test_only_an_admin_changes_the_channel_catalog`, `test_an_operator_subscribes_a_channel_to_a_running_plan_and_its_status_stays`, `test_only_an_admin_subscribes_a_channel_to_every_plan`, `test_a_subscribed_channel_cannot_be_deleted`, `test_a_channels_secret_is_write_only_and_named_after_it`, `test_a_slack_secret_must_be_a_slack_incoming_webhook_url`, `test_recipients_and_webhook_urls_are_shown_in_full_to_an_admin_alone`, `test_the_catalogs_events_carry_no_url_recipient_or_secret`, `test_a_notify_secret_name_is_refused_for_a_provider_a_hook_and_a_dns_target`; green with the new kinds and routes: `test_event_kinds_match_sdd_4_3_and_the_dashboard`, `test_every_route_is_documented_in_sdd_12`, `test_the_role_matrix_covers_every_route_sdd_12_lists` |
| NT4 | B | Topics, triggers and messages (§24.5) as pure functions: the trigger table, including the approval rule of §16's Pending approvals; the titles and lines; the three formats (the email message, the Slack payload, the Standard Webhooks document); the escaping; `redact_names`; links on `SEAMLESS_PUBLIC_URL`. The `cause` of `migration.downtime_ended` and the `source_stopped` of a failure | `tests/test_notify_messages.py`: `test_each_trigger_names_the_vm_the_plan_and_their_ids`, `test_a_downtime_message_says_where_the_vm_runs_and_how_long_it_was_down`, `test_the_approval_trigger_follows_the_pending_approvals_rule`, `test_a_waves_single_shot_cutovers_make_one_approval_message`, `test_slack_values_are_escaped_so_no_name_mentions_a_channel`, `test_an_email_subject_holds_no_control_character`, `test_no_message_carries_an_error_an_address_a_comment_or_an_actor`, `test_a_channel_that_redacts_names_sends_ids`, `test_a_link_carries_ids_alone_and_none_without_a_public_url`, `test_the_webhook_document_is_the_standard_webhooks_payload`; `tests/test_orchestrator.py`: `test_downtime_ended_names_its_cause`, `test_a_failure_records_whether_the_source_is_stopped`; the §15.1 tests green with `SEAMLESS_PUBLIC_URL` |
| NT5 | B, C | The notifier, `seamless_migrate.notify` (§24.6): per-channel cursors and order; routing through the subscriptions, once per channel; the retry classes, the backoff, `Retry-After`, pacing and expiry; the dedupe key, the cursor written with its outcome's event, and the restart; the server's lifespan; a sender interface that the fakes implement. Also the test route, `notification.*` (C: the dashboard's kind list) and `seamless_notifications_total` | `tests/test_notify.py`: `test_a_channel_sends_in_event_order`, `test_a_failing_channel_holds_back_no_other_channel`, `test_a_temporary_failure_is_retried_with_backoff_and_retry_after`, `test_a_final_answer_is_not_retried`, `test_a_message_whose_event_is_an_hour_old_expires`, `test_a_run_of_expired_messages_is_one_event`, `test_a_restart_resends_an_unrecorded_message_with_its_key`, `test_a_channel_two_subscriptions_match_sends_once`, `test_a_new_channel_starts_at_its_creation`, `test_a_disabled_channel_queues_nothing`, `test_notification_events_are_never_notified`, `test_the_clis_events_are_notified_by_the_server`, `test_a_channel_sends_at_most_one_message_a_second`, `test_the_notifier_starts_again_after_an_unexpected_error`; `tests/test_orchestrator.py`: `test_a_channel_that_never_answers_delays_no_migration`; `tests/test_api_notifications.py`: `test_a_channels_test_answers_its_outcome_and_is_recorded`; `test_metrics_exposes_every_family_sdd_18_names` green with the new metric |
| NT6 | B, D | The email sender (§24.3, §24.5): `smtplib` in the notifier's threads; STARTTLS or implicit TLS, never plain text; the certificate check; AUTH after TLS; the reply classes; the headers. The `SEAMLESS_SMTP_*` variables. `aiosmtpd` and a certificate maker in the dev extra. D: the NetworkPolicy's egress to 587 and 465 | `tests/test_notify_email.py`: `test_email_goes_over_starttls_and_authenticates_after_it`, `test_email_goes_over_implicit_tls`, `test_a_relay_without_starttls_gets_nothing_in_clear_text`, `test_a_relay_whose_certificate_fails_verification_gets_nothing`, `test_an_smtp_4yz_reply_is_retried_and_a_5yz_reply_is_final`, `test_the_message_id_is_the_key_and_auto_submitted_is_set`, `test_a_partly_refused_message_counts_its_refused_recipients`, `test_an_email_channel_without_a_relay_fails_with_no_relay`; the §15.1 tests green with the new variables; `tests/test_deploy_manifests.py`: `test_external_egress_reaches_the_smtp_submission_ports` |
| NT7 | B | The Slack sender (§24.5, §24.6): the payload, the wait on a 429's `Retry-After`, Slack's final answers. `redact()` learns Slack incoming webhook URLs | `tests/test_notify_slack.py`: `test_slack_gets_text_and_blocks_and_nothing_unfurls`, `test_a_slack_429_waits_its_retry_after`, `test_a_revoked_slack_webhook_fails_without_a_retry`, `test_the_slack_url_reaches_no_event_no_log_line_and_no_answer`; `tests/test_redaction.py`: `test_a_slack_incoming_webhook_url_is_redacted`, with the existing redaction tests green |
| NT8 | B | The webhook sender (§24.3): the Standard Webhooks headers and signature, a new signature and timestamp for each try with the same id, no redirect, the final answers | `tests/test_notify_webhook.py`: `test_a_webhook_is_signed_as_standard_webhooks_specify`, `test_each_try_is_signed_anew_with_the_same_webhook_id`, `test_a_webhook_redirect_is_not_followed`, `test_a_410_from_a_webhook_is_final`, `test_the_signing_key_never_leaves_the_control_plane` |
| NT9 | C | The dashboard (§24.9): the Notifications page in the Activity group (the catalog, the channels' state, an admin's channel forms, the secret dialog, the test, the every-plan subscriptions), the plan page's Notifications panel, the mock | `Notifications.test.tsx`: "lets only an admin change a channel", "never shows a Slack URL or a signing key", "shows recipients to an admin and their number to everyone else", "shows a channel's test outcome"; `PlanDetail.test.tsx`: "lets an operator subscribe a channel to the plan with the default topics"; `App.test.tsx`: "lists Notifications in the Activity group"; `mock.test.ts`: "keeps channels and subscriptions as the API does"; `a11y.test.tsx`: the new controls pass |
| NT10 | B | The demo (§24.9): recording senders in demo mode, and one seeded channel of each kind subscribed to the seeded plans | `tests/test_demo.py`: `test_the_demo_records_notifications_and_sends_none`, `test_the_demos_cutovers_notify_their_downtime` |
| NT11 | D, B | QASuite (the requirement rows and the notifications suite); Security.md (the channel secrets and the recipients as assets; the flows to the relay, Slack and receivers; the threats: header injection, mrkdwn injection, a leaked webhook URL, a flood; the data that leaves, in §10.4's style); Operations.md (configuring the relay, a Slack webhook and a webhook receiver; the egress; rotating a channel's secret); MEMORY.md's erasure bullet (the recipients). B: the erasure test learns the recipients | `test_every_test_qasuite_names_exists` (unchanged, green); `test_the_erasure_bullet_names_every_l2_field_that_records_a_user` (extended with the recipients) |
| NT12 | B, D (live) | The live smoke (§24.10), marked `live`, as `test_live_integrations.py`'s tests are: a Slack workspace's webhook; a relay with STARTTLS and one with implicit TLS (a test mailbox); a receiver that verifies with a Standard Webhooks library. The secrets come from the environment, never from the repository. The results, with the versions, go in QASuite §13 | `tests/test_notify_live.py`, skipped unless `SEAMLESS_LIVE_SLACK=1`, `SEAMLESS_LIVE_SMTP=1` or `SEAMLESS_LIVE_WEBHOOK=1`: `test_live_slack_accepts_a_message`, `test_live_smtp_delivers_over_starttls`, `test_live_smtp_delivers_over_implicit_tls`, `test_live_webhook_receiver_verifies_the_signature` |

**Order:** NT1, then NT2. Then NT3 and NT4 in parallel, and NT5 after both. Then NT6, NT7 and NT8 in parallel. NT9
comes after NT3 and NT5, and NT10 after NT6–NT8. NT11 goes with each task. NT12 comes last, before the release:
owner decision 12 makes it a release gate, not a Technology Preview.
