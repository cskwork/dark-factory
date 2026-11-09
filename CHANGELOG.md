# Changelog

## 0.9.0 - 2026-11-09

- Add an optional judge (`factory.judge`, `[judge]`): an independent model answers decisions
  that do not need a person. It writes the strongest argument against the recommendation
  first, follows the user's past decisions on the key (more verification over less), and
  decides only at risk=low, confidence at least `min_confidence` (0.8) and an existing
  option; a model failure escalates. Without the section it is off, so an unconfigured
  factory never calls a model or posts a comment. `state_dir/judge` (`off|shadow|on`)
  overrides `[judge] mode`; `shadow` logs verdicts to `judge.jsonl` without applying them.
- Choices are judged detached while the request holds its decision id: decided is recorded
  `by=judge` and wakes the line like a human decision; escalated opens the human item or
  page under the same id. New optional choice field `humanReason`; values in `human_only`
  (`security-plan`, `product-scope`, `prod-data`) and every `sql-run` stay human.
- The guard judges `human-risk` waits that carry the new optional `question` and `suggest`
  fields: accepted items resume with a `[<time> judge]` note. Each question is judged once,
  at most twice per key per day; leased keys and concurrently changed items are left alone.
- Promotion approval from the tracker (`[judge.promotion]`, `facts_cmd`/`comment_cmd`
  adapters): a QA-done status set by someone other than the operator after the operator's
  latest result comment, or later comments confirming QA, approve `by=judge`. Otherwise one
  comment per result asks the reporter to move the ticket and `decision-request` exits 11
  (`waiting=reporter`); the guard resumes that wait when the tracker shows the status.
  Self transitions, self-reported tickets and missing result comments stay human.
- On a to-do surface the decision watcher keeps one pinned `[factory] judge: on|off` setting
  item (sdlc-kit 2.23.0 `pin`; older kits keep it unpinned); a reply switches the mode.
- An unused approval no longer wakes the guard while its key waits on an environment with
  needs; the passing preflight wakes it, so a blocked environment does not start an
  orchestrator every tick.
- Ended runs with no agent left to archive get `runs/TOKEN/archived` and are not listed on
  every later wake.

## 0.8.0 - 2026-11-06

- Accept caller-declared event completion when every comment has same-key handled
  evidence, even while the ticket waits. Listed queue evidence may supply its
  stage; non-event units and all verifier checks stay strict, preventing completed
  replies from repeatedly failing the completion gate.
- Route every lesson through one serialized writer and state CLI. Normalize changing
  identifiers and open a recommended fix-now / accept / ignore decision after three
  occurrences in fourteen days, with open-request deduplication and a seven-day
  post-answer cooldown. Configurable thresholds turn recurring failures into a
  concrete user choice instead of more log lines.
- Require weekly-retro user choices to open on the decision surface with a recommended
  option and evidence, so proposals do not remain unanswered in a report.
- Preserve exception type and up to 300 message characters in guard diagnostics,
  including state-write failures, so internal errors can be diagnosed.
- Classify model-at-capacity responses as rate limits, so infrastructure shortages
  use the existing item backoff instead of a model retry.
- Add configurable worker environment strings (empty by default) and exact-agent
  cancellation with stopped-state confirmation after invalid or missing results.
  Explicit cancel failures and unavailable adapters prevent silent overlapping agents.
- Add optional full-line regex validation to existing preflight command probes,
  requiring a nonempty listing so an empty or malformed successful command cannot
  establish readiness. Existing exit-only probes retain their behavior.

## 0.7.0 - 2026-11-03

- Add `[decisions] surface = "app" | "tui" | "web" | "html"` (default `html`, validated). `html`
  keeps the local decision page with one-time-token buttons unchanged. The other three are views of
  the sdlc-kit to-do list (`~/.sdlc/todo.md`, `SDLC_TODO`, `SDLC_KIT`): `decision-request` starts no
  page or server and prints `D<n> <to-do id>`; the decision waits as a `[decision D<n>] KEY · title`
  item and the human's reply on it is the decision for every kind. `approve` (also `1`, `승인`) runs an
  approval or SQL run, a number (`2`, `2번`, `D12 2`), option id or label picks a choice, `hold`
  (`보류`) records nothing and closes the item, and anything else is asked again on the same item.
- New `factory.decision_inbox`: a watcher (`watch`) polls every minute while a decision item is
  open and records replies through `decision_page.decide` with `by="todo"`, the reply text as note
  and a per-reply `messageId`, so the single-use claim, audit line, action and focused wake match a
  click. The decision is recorded before the item is closed; a crash in between is finished on the
  next cycle without a second record. `decision-request` and every `factory-wake` tick start it.
- With a to-do surface, alerts sent through `notify.cmd` also update one `[factory alert] <project>`
  item whose waiting text is the latest alert; a to-do failure never changes the notify status.

## 0.6.2 - 2026-11-02

- Reconcile the queue with the tracker every tick. The intake scan may report `queueStatuses` for
  queued keys (a failed lookup goes to `queueStatusError` and never wakes the line); with
  `[reconcile]`, items whose status maps to `done` or whose stage is a `done_stages` entry leave the
  queue, and `human-risk` items whose status maps to `awaiting-reporter` become `reporter`. Keys owned
  by another run, unknown ownership and concurrent edits are left alone; results are recorded in
  `guard-last.json` `queueReconcile` and `runs.jsonl`. Previously finished and QA-waiting tickets kept
  every queue item waiting, so nothing ran.
- When nothing is runnable, no other run owns a key and `human-risk` items exist, the guard sends one
  summary a day (`No runnable work: human check N, awaiting reply M`) instead of staying silent.
- `check-result` classifies a provider's content-filter refusal configured in `[drain.model_refusal]`
  as `failureType=model-refusal` with `retryAllowed=true` and `retryWorker`, even when the requirements
  gate failed; the orchestrator retries the unit once with that role and a failed retry is human-risk.
- Workers no longer clean up: they list `cleanup:<path>` in artifacts and the orchestrator runs the new
  `python3 -m factory.cleanup --result RESULT --execute`, which removes only clean, pushed worktrees
  under `[cleanup].roots` and merge branches whose tip is in an `env_branches` ref. Workers stalled on
  cleanup prompts after finishing their work. Workers also commit with a subject and a short body.
- The runbook moves intake tickets to in progress one workflow transition at a time with a read-back,
  instead of "once", which stalled when the workflow had no direct transition.
- Add a regression test that a closed owner whose liveness alert was already sent is reclaimed at
  once without a second alert.

## 0.6.1 - 2026-10-30

- A failed `check-result --requirements` gate now returns `queueWaiting`. A worker that stopped on an
  environment wait (env-human/env-down, e.g. wrong VPN for a DB check) keeps that wait instead of
  being overwritten with human-risk, so the guard probes its needs, notifies the VPN action and resumes
  when the environment is ready. Identity mismatches stay human-risk.

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
