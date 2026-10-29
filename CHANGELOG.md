# Changelog

## 0.6.0 - 2026-10-29

- Count and alert on failed intake monitoring instead of only cooling it down: a malformed
  payload records `scan-malformed` (any other error `scan-error`) without a cooldown, so it
  stays a wake-worthy failure; the whole payload contract including events is now checked
  inside the scan, so a breached event is counted like unparseable JSON; and three consecutive
  failures of any kind append one `infra_fail` lesson and send one deduplicated notification,
  both cleared by a healthy scan.
- Retrospective improvements: optional per-source intake health separates unavailable monitoring
  from idle, whole-command scan failure cools the adapter down (`--force-refresh` bypasses it),
  worker attempts keep a unique checkpoint and `invocation.json` even on failure, caller
  `--requirements` manifests gate a declared stage against saved verifier JSON, and
  `summary-record --invocation/--observations` records measured cost and quality without estimates.
- Bound intake scan wall time and count a scan that never finished: `intake.scan_budget_seconds`
  covers one scan including its retry, each attempt stays under `command_timeout_seconds`, the
  retry runs only while the budget covers it, and a tick killed inside its scan leaves
  `attemptStartedAt` so the next scan records `scan-interrupted` and applies the cooldown ladder.
  Previously a hang cost two command timeouts and recorded nothing, so the same hang repeated on
  every tick with failureCount 0.
- Every result comment attaches the developer verification query as a `.sql` file card (run on non-production first); the result read-back uses `--min-file 1`. Questions and intake replies are exempt.

## 0.5.0 - 2026-10-28

- Treat launcher `error` status as finished for owner liveness, lost-run recovery, release, and ended-only archival; keys remain blocked until reconciliation verifies the actual outcome.
- Keep pending focus for blocked keys report-only instead of repeatedly waking the orchestrator; include the blocked pending keys in guard evidence and morning/status reports.
- Reject empty or filesystem-root `state_dir` configuration before CLI state access, including paths that resolve to `/`, with explicit errors.
- Port the applicable 2026-10-28 retrospective fixes and cover recovery, pending focus, and directory validation with offline regression tests.

## 0.4.0 - 2026-10-27

- Read-only verification queries a developer can run are attached to the ticket comment as a `.sql` file card instead of being pasted into the body; write/rollback SQL stays in the developer note. `factory.comment_check` counts file cards (mediaGroup) separately from inline images and adds `readback --min-file`.

## 0.3.0 - 2026-10-27

- Add planner-readable ticket comments: a runbook section for the planner/QA comment shape (request → result, before/after captures, requester mention, developer detail in a separate note) and `factory.comment_check`, which lints a comment before posting and verifies the read-back (developer terms, code names, commits, builds, branches, code formatting, unconverted markup, length, media and mention minimums). `[comments]` adds locale terms, allowed words and patterns.

## 0.2.0 - 2026-10-26

- Add post-release follow-up to the runbook: a reported problem on an already released ticket opens a new linked to-do ticket with a planner-readable description instead of reopening the closed one, with resume-safe creation and duplicate guards.
- Add configurable run-owner liveness, token ownership, lost-run recovery, key blocking, and ended-only agent archival.
- Add a 165-minute sequential drain budget, serialized state patches, pending focus, per-item backoff, and progress-aware deferrals.
- Keep rollback switches for legacy lock timing and single-unit runs; verify with offline command adapters.

## 0.1.0 - 2026-10-23

- Initial portable harness: guarded agent orchestration, intake triage, deterministic verifiers, human decision pages, and configurable adapters.
- Persist guard decisions, pass wake reasons, and suppress unchanged runnable loops with lessons and notifications.
- Add prerequisite-aware, fail-fast preflight and per-tick probe caching; group and deduplicate human-wait notifications.
- Archive finished orchestrators before launch and retry failed intake scans once.
- Verify migrations through read-only, host-checked DB access before approval requests.
