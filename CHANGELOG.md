# Changelog

## Unreleased

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
