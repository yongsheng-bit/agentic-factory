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

Tests (33, stdlib only):

```bash
python -m unittest tests.test_core
```

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
ones. `scripts/local_llm.py` is a ready-made cheap tier that drives a local
OpenAI-compatible endpoint (e.g. a llama.cpp server on a 3090 box) with a
shell-tool loop, so the high-volume cheap work costs electricity instead of
tokens:

```bash
export LLM_BASE=http://192.168.68.73:8080/v1
export LLM_MODEL=your-model-name
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
factory/cli.py       add / list / status / pause / resume / once / worker / watchdog / gate / blast
scripts/local_llm.py cheap-tier agent against a local OpenAI-compatible endpoint
tools/blast_check.py same rules, run in CI
.github/workflows/quality.yml
```
