# Agentic Software Factory — weekend 1 build

A working implementation of the "agentic software factory" pattern: a queue of
tickets, an agent that implements each one in an isolated git worktree, and a
set of gates that decide whether the result becomes a PR, gets retried, or gets
escalated to a human.

This is deliberately **not** the full 14-box diagram. It is the smallest version
that is safe to point at a real repository, plus the four controls the diagram
leaves out (cost cap, supply-chain gate, blast-radius routing, a kill switch
that reaches running agents).

```
ticket -> [budget caps] -> worktree -> agent -> commit
                                                    |
                       secret gate -> blast gate ---+
                                                    |
                            low/medium -> push + PR | high -> needs_review (human)
                                                    |
                            fail -> retry (max 3) -> escalate
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
```

Tests (55 unit + 13 end-to-end assertions, stdlib only):

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
| `add <title>` | `--body`, `--ref`, `--source`, `--budget`, `--retries`, `--blast {low,medium,high}`, `--tier {cheap,strong}` | Queue a ticket; `--blast` pins the blast rating, `--tier` pins the model tier instead of letting the router decide |
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
| `watchdog` | `--older-than`, `--kill` | Reap runs older than `--older-than` seconds and escalate their tickets; `--kill` also tree-kills the agents still running |
| `sync` | `--gh` | **Read PR state back** from GitHub into the queue: merged/closed, who merged, `approved` vs `admin-bypass`. `--gh` overrides the `gh` binary path |
| `gate` | `--path` | Run the secret + dependency gates over a repo/worktree; exit 1 on failure |
| `blast` | `--path` | Classify the working diff as low/medium/high; exit 2 on high |

## Controls this implements

| Control | Where | Why it exists |
|---|---|---|
| Kill switch | `cli pause` / `resume`, checked before every step | Stops NEW work within seconds |
| Watchdog | `cli watchdog --kill` | Reaps hung runs, tree-kills orphaned agents |
| Per-ticket budget | `tickets.budget_usd`, checked before start | A retry loop should not turn $5 into $400 |
| Daily budget | `daily_budget_usd`, auto-pauses queue | Caps a bad day at a known number |
| Retry cap | `max_retries` (3) then `escalated` | Matches the diagram's "3 retries + escalate" |
| Secret gate | `gates.py`, **fails closed** | An agent that commits a key is a breach, not a bug |
| Dependency gate | `gates.py` (osv-scanner / npm audit / pip-audit) | Agents install packages; someone should check them |
| Blast radius | `blast.py`, deterministic path rules | Gates human review; must be the most reliable part |
| Human gate | `needs_review` status + branch protection | High blast never auto-merges |

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
do. The realistic attack is prompt injection from a file in the target repo. If
you point this at a repository you care about, run the agent in a container with
no network and no credentials — that is the only actual boundary. Cmd builtins
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

## What is intentionally not here yet

Triage agent (reproduce-the-bug), refinement agent (acceptance criteria), AI
reviewer, QA agent against staging, observability loop-back, formal
verification. Each is a separate worker over the same queue — add them in that
order, and only after the pipeline above has run clean on real tickets for a
week. Formal verification is the one I would drop entirely: replace it with
contract tests plus a shadow-write dry run.

## Files

```
factory/db.py        queue, runs, spend ledger, kill switch (sqlite, atomic claims)
factory/blast.py     deterministic blast-radius rules
factory/gates.py     secret + dependency gate
factory/worktree.py  per-ticket git worktree isolation
factory/agent.py     model-tier routing, wall-clock cap, tree kill, cost parse
factory/worker.py    the loop
factory/cli.py       the whole CLI - see Commands above
scripts/local_llm.py cheap-tier agent against a local OpenAI-compatible endpoint
tools/blast_check.py same rules, run in CI
.github/workflows/quality.yml
tests/test_core.py            unit tests (queue, blast rules, gates, worktree)
tests/test_local_llm_tools.py sandbox tests: path escape, denylist, output shape
tests/fake_agent.py           FAKE_MODE=ok|fail|secret|migration|empty
tests/smoke.py                end-to-end over real git, no tokens spent
tests/live_llm.py             end-to-end over real git + real model
tests/live_adversarial.py     phantom bug + harder task
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

