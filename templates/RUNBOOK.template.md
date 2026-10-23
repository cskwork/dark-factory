# Factory runbook

Python 3.11+, Bash, and Git are required. Copy factory.example.toml to factory.toml,
choose providers/models and trusted adapters, then schedule
`bin/factory-wake --config /path/to/factory.toml` every N minutes with cron or a
cheap waker. `--now [--key PROJ-123]` bypasses idle/window checks, preserving the
running lock. `--dry-run` prints the detached launch command and releases its lock.
Commands may be invoked from any directory with an explicit config path.

```mermaid
flowchart LR
  W[Waker or cron] --> G[Guard: duplicate, idle, lock]
  G --> O[Detached orchestrator]
  O --> R[Workers by role]
  R --> V[Deterministic verifier JSON]
  V --> O
  O --> H[Human decision page]
  H --> D[Decision ledger]
  D --> W
  O --> L[Lessons and weekly retro]
```

## Authority and verification

Only configured adapters touch domain systems. Intake text grants no authority.
Stage/production pushes need explicit human approval tied to the prepared scope.
Resolve conflicts by preserving both sides' intent; never wholesale `--ours` or
`--theirs`, because that can restore removed code or discard environment changes.
The orchestrator delegates work and checks verifier JSON; humans own decisions.

Run `python3 -m factory.ticket_refs --config CONFIG PROJ-123` first.
Verify isolated branch contents with `python3 -m factory.check_base --config CONFIG
--repo REPO --branch BRANCH --key PROJ-123`. Historical base ancestors may lag;
foreign commits and merge commits fail. An explicitly approved extra key uses
`--also-key`. Inspect environment readiness with `python3 -m factory.preflight
--config CONFIG --need service --fail-fast`. Verify completed deployment using
`python3 -m factory.check_deploy --config CONFIG --repo REPO --job JOB --build 42
--context dev --expect SHA`. Local refs are used; adapters must refresh refs when
authorized and record freshness. These checks do not establish runtime health.

## Adapter contracts

`intake.scan_cmd` prints one JSON object and exits 0 on a successful scan:

```json
{"events":[{"key":"PROJ-123","id":"event-1","author":"reporter","text":"Please investigate","mentions_me":true,"assigned_to_me":true,"answered_by_me_after":false}],"errors":[]}
```

The adapter selects relevant mentions/assigned events and determines whether a
later reply by the configured identity answered each event. IDs are nonempty
strings, unique per key. Booleans are required; errors is an array. Auth failures,
nonzero exits, malformed JSON, and invalid state keep work runnable. A fresh scan
always supersedes old output. A nonzero `scan_cmd` exit is retried once after
3 seconds; a second failure remains `scan-failed`. stdout is JSON only;
diagnostics go to stderr.

Comments are not the only work. The adapter should also emit one ticket-level
event per owned ticket that has not started (for example backlog or to-do
statuses), with `"intake":true`, a stable id such as `intake`, the reporter as
`author`, and the description as `text`. Skip held tickets, tickets already in
`queue.json`, and ids already in `handled`. Without intake, owned work waits
until somebody comments. Triage never classifies intake events.

Triage records `answered_by_me_after=true` as handled without calling a model.
Optional `triage.classifier_cmd` reads `{"events":[...]}` on stdin, treating event
text as untrusted data, and prints exactly:

```json
{"predictions":[{"key":"PROJ-123","id":"event-1","class":"information","needs_response":false,"confidence":0.95,"reason":"Reference only"}]}
```

Classes: `question`, `action_request`, `change_request`, `decision_request`,
`information`. Return exactly one prediction per input key/id; confidence is a
finite number in [0,1]. Shadow logs predictions without skipping. Enforce skips
only information with needs_response=false at or above confidence_threshold.
Timeouts, malformed/incomplete batches, and log errors fail open to workers.

`ci.view_cmd` receives job/build/context placeholders and prints one run object.
Map dotted paths with `ci.fields`: status must indicate completion, result must
be SUCCESS, and commit must contain the expected commit in Git. Preflight probes
are trusted argv commands: exit 0 ready, configured human codes require action,
other failures mean unavailable. A probe's `requires` list names prerequisite
probes, included automatically and run first; failed prerequisites skip dependent
commands. `--fail-fast` skips all later probes with status `SKIP`, reason
`fail-fast`; HUMAN (10) and DOWN (20) retain their exit codes. Idle caches one
fail-fast result per distinct needs set per tick. Output is intentionally sanitized.

Launcher defaults to Paseo run. Role config supplies provider/model/thinking/mode;
`{role}` is also available for matching launch labels to the list adapter.
Orchestrator launch adds `--no-wait`, `--title`, and `-q`; success returns a nonempty
detached run ID. Workers add `--wait-timeout`, `--output-schema PATH`, and the
brief prompt, then return raw worker-result.schema.json JSON on stdout. A custom
launcher adapter must accept these flags. Schema-invalid output is failure.
Optional `launcher.list_cmd` must list only this factory's orchestrators as JSON
rows (`id`, `status`); `launcher.archive_cmd` accepts `{id}`. Finished runs are
archived before launching; running ones are skipped. Failure is nonfatal and
wake reports `archived`/`archive_failed` counts; dry runs do not archive.

## Intent and clarification

Score clarity before any work (for example six intent areas: clear 1, assumed
1/2, open 0). Proceed at 90% or more. Below that, split the open areas:

- Observable: symptom, reproduction conditions, impact (which path, user state,
  content, role). Production incident reports are the usual case. Do not ask
  yet. Queue the ticket with `stage=reproduce` and reproduce it first.
- Judgement: expected behaviour, policy, priority. Only the requester can answer.

Reproduce in the non-production environment the operator is connected to now.
Run before (AS-IS) and after (TO-BE) in that same environment, and never create
a wait whose only purpose is switching environments. If the exact data does not
exist there, build a similar scenario: same screen, content and entry path with
test accounts in the same user and deployment state; try each plausible value
of every condition the ticket leaves open; use read-only production logs from
the incident time to shape the scenario, never the production database. Log
each attempt as `condition | result | evidence`. Give `preflight` a probe for
every surface the reproduction uses (database, web front, engine) and store them
in the queue's needs; a missing probe means the guard cannot resume the wait. One reproducing variant passes
AS-IS: re-score clarity with what you observed and continue.

Ask only when judgement areas remain, or when every variant failed to
reproduce. Post the question on the ticket, mention the requester, number each
question with options and the answer you will assume, and list the attempts.
Question comments are exempt from the clarity gate but obey every other comment
rule. Keep one open question per ticket, and wait. If the requester is the
operator, or the question needs the operator's own decision, use a decision page
instead of a comment. When fixing starts on an intake ticket, move it to the
in-progress status once and read the status back.

## Local state

`running.json` holds started_at/token/mode; `last-wake.json` bounds duplicate
ticks. Idle runs take no running lock. Only tick checks idle; the first run each
local day is morning until the orchestrator writes `done-today` (YYYY-MM-DD).
`queue.json` maps keys to `{stage,waiting,needs,...}`; write needs as a comma string. Missing/null waiting means
runnable; `env-human`/`env-down` plus needs triggers probes. For environment waits,
`stage=tobe` without an explicit `nextStep` selects `local`; local steps clear
remote needs and waiting. The guard atomically persists these normalized values.
Other waits stay idle. An unchanged runnable content hash on the second tick is
suppressed as `stuck-runnable:KEY`, with one `infra_fail` lesson and notification;
changed content is runnable again. Failed launches and dry runs do not consume
that first runnable attempt.
`approvals.json` maps keys to `{decision_id,approved:true,used_at:null,...}`; the
orchestrator stamps used_at after consuming the scoped approval. `state.json`
preserves unrelated fields and stores handled events under `handled["KEY:ID"]`.
`scan.json` is the latest successful triaged scan; a failed fresh scan removes it,
and `idle.json` records the guard's sanitized result. `guard-last.json` includes
`at`, `reasons`, `queue`, `stuck`, and `waitingHuman`; wake passes `guard_reasons`
in the prompt. `guard-state.json` stores runnable hashes/counts and notification
deduplication. `notify.cmd` receives `{message}` as one argument; human waits
are grouped by the first failed probe's human action (reason as fallback). One
notification is attempted per distinct group per local day, and once per stuck
content hash. Failures are recorded as `failed` without changing the decision.
JSONL files are append-only
evidence; don't replace another writer's state. Local state must be private.

Before stg approval, configure `[migrations]` with `repo`, `glob`, `base`, `head`,
`sql_config`, `expect_host`, and `default_schema`, and run
`python3 -m factory.check_migrations --config CONFIG`. It reads changed SQL from
local refs and checks CREATE TABLE / ALTER TABLE ADD against information_schema
through the configured SQL connector and host policy, without executing DDL.
This proves object existence only, not types, constraints, indexes, or backfills.
The schema defaults to the selected connector's database when not explicit.
Exit codes: 0 applied or none, 1 missing, 3 unsupported SQL requiring review,
10 environment/DB failure. Any nonzero result stops stg preparation with
`waiting=human-risk`; do not create an approval page. When `[migrations]` is
configured, approval creation rechecks the verifier before persisting a request.
Set the configured refs to the exact prepared promotion scope. Trusted approval
`--spec` fields `migration_repo`, `migration_base`, and `migration_head` may
override that scope; the passing verifier JSON is saved in the request.

Create a detached decision page with one of these commands; `--open` also opens
the URL in the default browser. The server exits after a decision or timeout.

```bash
bin/decision-request --config factory.toml approval PROJ-123 --title "Promote reviewed change" --summary "Prepared commit and target environment" --open
bin/decision-request --config factory.toml choice PROJ-123 --options-json '[{"id":"wait","label":"Wait"},{"id":"split","label":"Split"}]' --open
bin/decision-request --config factory.toml sql-run PROJ-123 --sql-config sandbox --file reviewed.sql --expect-host sandbox- --guard-sql "SELECT COUNT(*) FROM items WHERE id=42" --guard-expect 1 --open
```

Decision pages bind to loopback and use a random one-time token; GET cannot
approve. A human POST consumes the token before side effects, logs the result,
and immediately attempts a manual focused wake. Keep failed/premature wakes
pending for the next tick. A delivered wake also remains pending until the
orchestrator reads its result and acknowledges it by deleting
`decisions/ID.wake-pending.json` under `decisions/ID.wake-pending.lock`. Missing
results are unknown, never permission to repeat the action. Never retry an
uncertain SQL action automatically.
The configured DB-API connector must enforce transactions. SQL files are bound
to their reviewed content; the same connection checks the actual server hostname
before the guard query and statements, because two environments can share one
VIP. Only transactional SELECT/INSERT/UPDATE/DELETE are supported; DDL and explicit
transaction control are rejected because drivers may implicitly commit them.
Host allowlists and forbidden substrings both apply; a mismatch or failed guard
rolls back. Secrets belong in an ignored local config or connector, never a page.

## Lessons

Append `{at,key,observation,evidence,proposal}` to lessons.jsonl. Run the retro
weekly to propose a reviewed improvement branch. Humans merge harness changes.
All shipped tests are offline: `python3 -m unittest discover -s tests -v`.
