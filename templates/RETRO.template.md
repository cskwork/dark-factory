# Weekly factory retrospective

Read lessons.jsonl, runs.jsonl, triage-shadow.jsonl, decision outcomes, and verifier
artifacts for the week. Separate observed failures from proposals and unknowns.

1. Count idle skips, launched runs, failed verifiers, repeated waits, and human
   decisions. Cite evidence for each recurring pattern; never infer model quality
   from a successful process exit.
2. Propose the smallest change that addresses a demonstrated cause. Show the
   expected effect, safety boundary, and offline regression scenario. Keep domain
   adapter changes separate from portable harness changes.
3. Implement approved proposals on a dedicated branch from the configured base,
   run the offline suite, and submit a concise review with evidence. Never weaken
   a verifier to obtain PASS or broaden triage enforcement without approval.
4. Open every proposal needing a user decision through the existing decision
   path, as well as including it in the report. Write a choice spec with `key`,
   `title`, `question`, `summary` (counts, dates and 1-3 evidence references),
   `options` (`id`, `label`, `detail`), `recommended` (an option id) and
   `recommendReason`. Use `bin/decision-request --config CONFIG choice PROJ-123
   --spec DECISION.json --open`. For a factory-wide proposal use a key matching
   the configured key pattern, such as `FACTORY-0`. Cite an existing open
   recurring-lesson decision instead of creating a duplicate. Record the
   decision id and spec path in the report; record retro completion only after
   the decision surface opens successfully.

Humans review and merge. The retro opens requests but never answers them,
merges, or deploys. Preserve the original lesson and append its resolution
through `python3 -m factory.state --config CONFIG lesson --json LESSON_JSON`.
