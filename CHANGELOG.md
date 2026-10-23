# Changelog

## Unreleased

- Add configurable run-owner liveness, token ownership, lost-run recovery, key blocking, and ended-only agent archival.
- Add a 165-minute sequential drain budget, serialized state patches, pending focus, per-item backoff, and progress-aware deferrals.
- Keep rollback switches for legacy lock timing and single-unit runs; verify with offline command adapters.

## 0.1.0 - 2026-10-23

- Initial portable harness: guarded agent orchestration, intake triage, deterministic verifiers, human decision pages, and configurable adapters.
- Persist guard decisions, pass wake reasons, and suppress unchanged runnable loops with lessons and notifications.
- Add prerequisite-aware, fail-fast preflight and per-tick probe caching; group and deduplicate human-wait notifications.
- Archive finished orchestrators before launch and retry failed intake scans once.
- Verify migrations through read-only, host-checked DB access before approval requests.
