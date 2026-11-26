# Agentic Software Factory

A working implementation of the "agentic software factory" pattern: a queue of
tickets, an agent that implements each one in an isolated git worktree, and a
set of gates that decide whether the result becomes a PR, gets retried, or gets
escalated to a human. **Agent proposes, code disposes** — every ordering,
budget and gate decision is deterministic Python; the model only ever does the
work inside one bounded attempt.

This is not a demo. It runs unattended on a scheduled tick against real
repositories and has merged 30+ tickets. What makes it safe is not the model
(a cheap local one) but the seams around it: fail-closed gates, a mutation gate
that proves the tests constrain the code, a blast-radius router that sends
dangerous diffs to a human, and a PR body that states — at open time — exactly
what was *not* proven.

```
ticket -> refine gate -> review cap -> budget caps -> worktree -> agent
           (body is     (stop         (per-ticket    (isolated  (cheap or
            buildable)   producing)    + daily)       git)      strong tier)
                                                                     |
  tests (fail-closed) <- secret+deps gate <- junk-diff check <- commit
       |                                                           |
  mutation gate (tests must kill mutants)                          |
       |                                                           |
  blast gate --low/med--> PR + AI review + Not proven + Decisions  |
       |                + GUI preview comment <- sync reads merge  |
       +--high--> needs_review (human)       <-retry (max 3)-------+
                                                  -> escalate (human)
```

## Quickstart

```bash
cd agentic-factory
python -m factory.cli -c factory.json init        # creates the sqlite queue

# point config at your repo and your agent command
cp config.example.json factory.json

# queue a ticket and watch one pass through the pipeline
python -m factory.cli -c factory.json add "Fix off-by-one in tank dip rounding" \
    --body "Dips.xlsx rounding drops 0.5 L on half-litre readings" --budget 1.00
python -m factory.cli -c factory.json once --dry   # no agent, shows the worktree
python -m factory.cli -c factory.json once         # real run

python -m factory.cli -c factory.json status
python -m factory.cli -c factory.json worker       # run the loop
python -m factory.cli -c factory.json tick         # one unattended cycle (cron)
```

Tests (271 assertions, stdlib only, no pytest):

```bash
python -m unittest discover -s tests -p "test_*.py"   # unit
python tests/smoke.py                                 # real git, fake agent
```

Live tests against a real model (skip cleanly if the endpoint is down):

```bash
python tests/live_llm.py            # one ticket, end to end
python tests/live_llm.py 5          # hit-rate over 5 tickets
python tests/live_adversarial.py    # phantom bug + harder task
```

## Commands

Every command takes the global `-c/--config` (default `$FACTORY_CONFIG`, else
`factory.json`). Flags below are exactly the ones `factory/cli.py` defines.

| Command | Flags | What it does |
|---|---|---|
| `init` | — | Create the sqlite queue at `db` in the config |
| `add <title>` | `--body`, `--ref`, `--source`, `--budget`, `--retries`, `--blast {low,medium,high}`, `--tier {cheap,strong}`, `--from-file FILE` | Queue a ticket; `--blast` pins the blast rating, `--tier` pins the model tier instead of letting the router decide, `--from-file` bulk imports JSONL (one object per line, `title` required) |
| `list` | `--status` | Ticket table: status, attempts, spend, blast, tier, ref |
| `status` | — | Kill-switch state, today's spend vs the daily cap, status counts, tier mix, merged-without-review count |
| `show <id>` | — | One ticket in full: blast + reason, tier + reason, spend vs budget, PR URL, last failure |
| `runs` | `--ticket`, `--limit` | Run history with the routing decision, so a tier choice can be audited later |
| `retry <id>` | — | Requeue a `failed`/`escalated`/`closed` ticket without resetting its attempt count |
| `tier <id> {cheap,strong,auto}` | — | Pin a ticket's tier, or `auto` to hand it back to the router; prints the resulting route |
| `pause` | — | **Kill switch.** No new tickets start; running agents finish their current step |
| `resume` | — | Release the kill switch |
| `once` | `--dry` | Process a single ticket; `--dry` sets up the worktree without running an agent |
| `worker` | `--dry`, `--idle` | Run the loop, sleeping `--idle` seconds when the queue is empty |
| `tick` | `--max`, `--gh`, `--no-board`, `--json` | **Unattended cycle:** reap stale runs, sync PR state from GitHub, drain up to `--max` tickets, update the project board, print one line. Safe to cron; a busy engine makes it a no-op |
| `board` | `--dry` | Push ticket statuses to the GitHub project board columns |
| `import-issues` | `--gh`, `--label` | Import open GitHub issues with a label as tickets (idempotent) |
| `sync` | `--gh` | **Read PR state back** from GitHub into the queue: merged/closed, who merged, `approved` vs `admin-bypass`. `--gh` overrides the `gh` binary path |
| `babysit` | `--force`, `--gh` | **Answer review comments** on open PRs: one agent round per PR on its own branch, push the fix or the refusal, reply in the thread. Runs inside `tick`; `--force` runs it once even when disabled in config |
| `canary` | `--python`, `--data`, `--truth`, `--repo` | Replay the merged parser against real receipts vs a ground-truth CSV, run under the *customer's* venv so the factory stays stdlib-only. Exit 0 pass / 1 fail / 2 setup |
| `watchdog` | `--older-than`, `--kill` | Reap runs older than `--older-than` seconds and escalate their tickets; `--kill` also tree-kills the agents still running |
| `gate` | `--path` | Run the secret + dependency gates over a repo/worktree; exit 1 on failure |
| `blast` | `--path` | Classify the working diff as low/medium/high; exit 2 on high |

## Controls this implements

| Control | Where | Why it exists |
|---|---|---|
| Kill switch | `cli pause` / `resume`, checked before every step | Stops NEW work within seconds |
| Watchdog | `cli watchdog --kill`, run every tick | Reaps hung runs, tree-kills orphaned agents |
| Refine gate | `refine.py`, before any agent call | A ticket whose body names files that do not exist, or demands a shape instead of an outcome, wastes a whole attempt; grill it at intake instead (`docs/grill-ticket.md`) |
| Review cap | `review.max_pending_reviews` | Backpressure is a number: at the cap the factory stops producing, because the binding constraint is your review attention, not agent throughput |
| Per-ticket budget | `tickets.budget_usd`, checked before start | A retry loop should not turn $5 into $400 |
| Daily budget | `daily_budget_usd`, auto-pauses queue | Caps a bad day at a known number |
| Fail-closed tests | `gates.require_tests` (default on) | An empty `test_command` pauses the queue as **misconfigured**; a missing required check must never become a green run |
| Retry cap | `max_retries` (3) then `escalated` | Matches the diagram's "3 retries + escalate" |
| Secret gate | `gates.py`, **fails closed** | An agent that commits a key is a breach, not a bug |
| Dependency gate | `gates.py` (osv-scanner / npm audit / pip-audit) | Agents install packages; someone should check them |
| Mutation gate | `mutate.py` | Tests passing proves nothing unless they *kill mutants* of the new code; a suite that asserts nothing gets bounced |
| Blast radius | `blast.py`, deterministic path rules | Gates human review; must be the most reliable part |
| Human gate | `needs_review` status + branch protection | High blast never auto-merges |
| Canary | `cli canary` | A regression in a merged parser is caught against ground truth, not vibes |
| Retro digest | `scripts/retro.py` | Weekly: spend, failure classes, and the human-pending queue — the operator's own review backlog made visible |

## What every PR proves (and what it says it did not)

The PR body is evidence, assembled by code, never by the agent:

- **blast radius + tier + cost + gate lines** — what ran, what it cost
- **`## Not proven`** — every gate that did *not* run on this diff, named
  (tests absent, mutation disabled, preview pending/failed, review down).
  The section is never empty: green must never be confused with absent.
- **mutation report** — mutants generated, killed, survived
- **`## Decisions`** — the agent's ledger: one line per non-obvious choice,
  `choice - why - the alternative it beat`. Best-effort, never gates.
- **GUI preview** — for page changes, a real headless-browser screenshot of
  the rendered pages attached after open (best-effort; runs as a subprocess
  of the *customer's* venv, so the factory itself stays stdlib-only)

## Blast radius rules

`high` (never auto-merge): `migrations/**`, `*.sql`, `**/auth/**`, `**/billing/**`,
`.github/workflows/**`, `Dockerfile*`, `*.tf`, `*.pem`, `*.key`, or >= 25 files,
or >= 800 changed lines.

`medium` (PR + CI, human optional): lockfiles, `api/**`, `models/**`, >= 8 files,
>= 250 lines, or diff text matching auth/TLS relaxation, error suppression,
destructive data ops, or hardcoded credentials.

`low`: everything else. Anything the rules cannot decide is escalated, never
defaulted to low.

## Model tiers

`config.agent.strong` runs medium/high tickets; `config.agent.cheap` runs low
ones. `scripts/local_llm.py` is a ready-made cheap tier: a native
**tool-calling** loop (not a text protocol — the model returns real
`tool_calls`, so there is nothing to mis-parse) against any OpenAI-compatible
server.

Measured on the 3090 box (`192.168.68.73:8080`, `qwen3.8-flash-next-unsloth-ud-iq4_xs`,
n_ctx 131072), 2026-11-16:

| Measurement | Result |
|---|---|
| Decode speed | ~99 tok/s, 0.47 s short-call latency |
| Simple bug ticket | 5/5 green, 8–15 s each, 4–7 tool calls |
| Tokens per ticket | ~12k in / ~7.6k out |
| Phantom-bug ticket (no such defect) | refused; escalated `agent produced no changes` |
| Multi-file task + new tests | green, tests verified on the agent's own commit |

The phantom-bug case is the one that makes auto-merge survivable: a green test
suite says nothing about a ticket that was wrong, so the agent must be able to
say "cannot reproduce" instead of writing *something*. `tests/live_adversarial.py`
holds it to that.

The agent's sandbox is what lets a cheap model near a real repo:

- every path is resolved and must land inside the worktree; `.git` is off-limits
- file tools are Python, not shell, so behaviour is identical on Windows/Linux
- `run_command` refuses VCS-mutating, destructive and network commands — the
  orchestrator owns commits and pushes; an agent that can `git push` is an agent
  that can ship
- **`run_command` has no shell.** argv only: no pipes, no `>`, no `&&` (an
  operator outside a quoted argument is refused with an explanation, not a
  mysterious exit code). `.cmd`/`.bat` shims like `npm` are wrapped in
  `cmd /c` with a fixed argv, so nothing user-shaped reaches the parser.
- **the child's environment is an allowlist.** The orchestrator may hold
  `GITHUB_TOKEN` or a cloud key; a child inherits everything by default, so
  `set` used to be a credential read. `LLM_RUN_ENV` adds names a build needs.
  `TEMP`/`TMP` are pinned inside the worktree so scratch stays observable.
- arguments naming a path outside the worktree (`../x`, `C:\Windows\...`) are refused
- tools **return** an error string instead of raising, so the model can recover
  from a bad path rather than killing the run
- step and token budgets bound a confused model before it burns an hour

**Honest limits.** This is a guardrail against a confused model, not a wall
against a determined one: a program *given* a path can still resolve its way
out, and a regex denylist cannot stop `python -c` from doing anything Python can
do. The realistic attack is prompt injection from a file in the target repo —
which is why the actual boundary is `factory/sandbox.py`: with `sandbox.enabled`
the agent, the test suite, the mutants, the preview renderer and the canary all
run in disposable containers (`docker build -t factory-agent:local .`), each
with one worktree mounted, a fresh home, no credentials, and no network except
the local LLM through a one-rule egress gate. The gates protect the diff; only
the container protects the machine. Cmd builtins
(`echo`, `dir`, `set`) do not work, by design.

Because the model is local, `price_per_mtok_*` is set to API-equivalent
**shadow** prices in `config.example.json`. At $0 the per-ticket and daily
budget governors are inert; with shadow prices the ledger keeps meaning
"what this would have cost" and reads as the money the 3090 saved.

```bash
export LLM_BASE=http://192.168.68.73:8080/v1     # no auth: LAN only, never port-forward this
export LLM_MODEL=qwen3.8-flash-next-unsloth-ud-iq4_xs
export LLM_MAX_STEPS=20 LLM_MAX_TOKENS=60000 LLM_TIMEOUT=300
```

Cost is parsed from the agent's JSON output (`total_cost_usd`, or `usage`
tokens x `price_per_mtok_*`). If an agent prints nothing parseable, cost is 0 —
so put a real price in the config or watch `status` rather than trusting it.

## Container sandbox

`factory/sandbox.py` is the only boundary that protects the *machine* (the
gates protect the diff). With `sandbox.enabled` every code-executing stage —
the agent, the test suite, the mutants, the preview renderer, the canary —
runs in a disposable container: one worktree mounted, fresh home, no
credentials, `--read-only` rootfs, pids/memory caps, and no network except
the local LLM through a one-rule egress gate container (internal Docker
network, single TCP forward). Commits, pushes and PRs stay host-side, so an
agent's worst day is a bad diff in a throwaway worktree.

```bash
docker build -t factory-agent:local .
```

```json
"sandbox": {
  "enabled": true,
  "image": "factory-agent:local",
  "network": "factory-llm",
  "egress": {"network": "factory-llm", "gate_name": "factory-egress",
             "listen": 8080, "target": "192.168.68.73:8080",
             "llm_base": "http://factory-egress:8080/v1"},
  "limits": {"memory": "2g", "cpus": 2, "pids": 256}
}
```

`enabled: false` (the default) keeps every legacy path byte-identical.
Never mount the home directory, the docker socket, or the factory sqlite db
into an attempt container; the module only mounts the per-ticket worktree,
the prompt file, `scripts/` (read-only), and explicit output dirs. Live
smoke tests: `FACTORY_SANDBOX_LIVE=1 python -m unittest tests.test_sandbox`.

## Required repo setup (do not skip)

The pipeline only has teeth if GitHub is told to enforce it:

1. **Branch protection on `main`**: require a pull request, require status
   checks (`secrets`, `deps`, `quality`, `e2e`), require branches to be up to date.
2. **CODEOWNERS** listing a human for the high-blast paths:
   ```
   /migrations/  @you
   /**/auth/     @you
   /*.sql        @you
   ```
3. **Environments** for staging/production with "required reviewers" — that is
   the diagram's human gate, and it costs nothing to turn on.
4. Give the agent a **scoped, read-mostly credential**. A `GITHUB_TOKEN` with
   `contents: write` and no `pull-requests: write` is a reasonable start; never
   give it an admin token, and never give it production cloud credentials.

## House rules

`AGENTS.md` (injected into every agent prompt, and CI-enforced where possible)
carries the scar tissue, including: *a green job is not proof its steps ran*,
and *a rule earns its place only where code enforces it* — prose rules get
obeyed even when the model would do better alone, so hard rules live behind
gates, checks, and tests. `GLOSSARY.md` is the shared vocabulary; name things
with its words or the retro cannot find them.

## What is intentionally not here yet

Triage agent (reproduce-the-bug before anyone builds it), QA agent against a
staging deploy, observability loop-back (errors become tickets on their own).
Each is a separate worker over the same queue. Formal verification stays
dropped on purpose: contract tests plus the canary do that job cheaper.

## Files

```
factory/db.py        queue, runs, spend ledger, kill switch, review-cap flag (sqlite, atomic claims)
factory/blast.py     deterministic blast-radius rules
factory/gates.py     secret + dependency gate, test runner
factory/mutate.py    mutation gate: does the test suite constrain the new code?
factory/refine.py    ticket intake gate (buildable body, outcome criteria)
factory/review.py    independent AI reviewer over the diff (advisory)
factory/preview.py   GUI screenshot preview posted to PRs (best-effort, customer venv)
factory/canary.py    ground-truth replay of a merged parser, run in the customer venv
factory/worktree.py  per-ticket git worktree isolation
factory/agent.py     model-tier routing, decision-ledger extraction, wall-clock cap, tree kill, cost parse
factory/worker.py    the loop, the gates' order, and the PR body
factory/tick.py      the unattended cycle (reap, sync, drain, board)
factory/sync.py      PR state read-back (merged/closed, who merged, how)
factory/board.py     GitHub project board column sync
factory/issues.py    GitHub issue import (labelled, idempotent)
factory/notify.py    operator notifications
factory/cli.py       the whole CLI - see Commands above
GLOSSARY.md          shared vocabulary - name things with these words
AGENTS.md            rules for working on the factory itself
docs/grill-ticket.md ticket intake protocol (grill before queueing)
scripts/retro.py     weekly digest: spend, failure classes, human queue
scripts/tick_task.py scheduled-task wrapper for `cli tick`
scripts/local_llm.py cheap-tier agent against a local OpenAI-compatible endpoint
tools/blast_check.py same blast rules, run in CI
tools/no_comments_check.py CI guard: no `#` comments in Python
.github/workflows/quality.yml
tests/               unit tests + fake-agent smoke + live-model probes
```

## Bugs the tests actually caught

Not hypothetical — each of these was a live defect found by running the suite:

1. **The prompt was committed into every PR.** The prompt file was written
   inside the worktree, so `git add -A` shipped it.
2. **Retries died on a leftover branch.** A failed attempt left `agent/ticket-N`
   behind; the next `git worktree add -b` failed with "already exists", so the
   retry cap could never be reached.
3. **The human gate was silently dead.** Blast radius was computed against
   `HEAD` *after* the commit, so the diff looked empty, everything rated `low`,
   and high-blast changes auto-merged. Fixed by capturing the pre-commit SHA.
4. **Build artifacts rode into PRs.** `git add -A` trusted the target repo's
   `.gitignore`, so an agent running the tests committed its own `__pycache__`.
5. **Sandbox guards raised instead of reporting.** A raised exception escaped
   the tool and killed the run; the model never got to correct itself.
6. **`list_files` returned nothing for root-level files.** `fnmatch` has no real
   `**`, so `**/*.py` never matched `app.py`.
7. **The retro window was always empty.** `runs.started_at` is an epoch REAL
   compared against `date('now')` — a number vs a string that silently matched
   nothing. Fixed by computing the cutoff in Python.
8. **A head-only summary clip would have eaten the decision ledger.** The
   ledger lives at the tail of the agent summary; the clip kept the head.
   Caught by a test written while porting the feature, before it shipped.
