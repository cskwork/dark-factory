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

Humans review and merge. The retro never merges, deploys, or records human
decisions itself. Preserve the original lesson and append its resolution.
