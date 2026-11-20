# Glossary — the factory's shared language

Name things with these words in tickets, commits, PRs, code, and docs.
One concept, one word. If a word here needs broadening, change it here
first — do not fork synonyms in code.

## Pipeline

- **Ticket** — one unit of work, a row in the board DB, usually mirrored
  by a GitHub issue. Its body is the contract; the build agent may not
  renegotiate it.
- **Intake** — turning a labeled issue into a ticket. Snapshots the body
  into the DB; later issue edits do not propagate.
- **Refine gate** — intake-time static check of the body: backticked
  path-like names must exist in the repo, no duplicate tickets. New
  files must be named without backticks.
- **Attempt** — one build try for a ticket (a `runs` row). Each attempt
  gets a fresh worktree and its own cost line.
- **Tier** — which model an attempt used: `cheap` (local small model,
  the default) or `strong` (escalation). `gate` marks gate-side spend.
- **Gate** — a deterministic check an attempt must pass before its PR
  counts: tests, mutation score, no-comments, blast-radius. A green
  gate is a claim; the mutation gate is what makes it evidence.
- **Blast radius** — how much surface a diff may touch; overreach
  bounces the ticket instead of reviewing it away.
- **Worktree** — the isolated checkout an attempt builds in, resolved
  against `origin/<default>` so a stale base cannot hide behind a local
  main.
- **GUI preview** — screenshots of the rendered app posted to the PR
  when a diff touches GUI paths; best-effort, never gates the ticket.
- **Decision ledger** — the `## Decisions` section an attempt ends its
  summary with: one line per non-obvious choice, as
  `choice - why - the alternative it beat`. Lifted into the PR body;
  best-effort, never gates.
- **Review cap** — the ceiling on PRs awaiting human review
  (`review.max_pending_reviews`). At the cap the factory stops claiming
  work: the binding constraint is the operator's judgment, not agent
  throughput.
- **Fail-closed gate** — a required check that is *missing* counts as
  misconfiguration, not as passing. An absent test command pauses the
  queue; it never becomes a green run.
- **Not proven** — the PR-body section stating, at open time, which
  gates did *not* run on the diff (tests, mutation, preview, review).
  A reviewer never guesses whether green meant "passed" or "absent".
- **Escalation** — a ticket the cheap tier could not finish within its
  budget or retry allowance; it waits for a human, not another model.
- **Canary** — a known-good ticket replayed to prove the pipeline
  itself still works when no real ticket would.

## Board states

`queued → in_progress → pr_open → merged`, with side doors
`needs_info` (contract problem — fix the ticket, then `retry`),
`failed`, `escalated`, `closed`. `needs_info` is a ticket-quality
signal, never a model-quality one.

## People

- **Operator** — David: files tickets, reviews GUI previews, merges.
  The factory never merges.
- **Build agent** — the model running an attempt. It proposes; the
  gates dispose.
