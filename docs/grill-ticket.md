# Grill the ticket (before it enters the queue)

A ticket is a contract with a model that cannot ask you follow-ups
mid-build. Most gate failures this factory has eaten were ticket-quality
failures, not model failures. Before filing, resolve the ticket's
decision tree the way you would grill a feature spec: every branch
settled, nothing silently assumed.

## The protocol

Work the ticket as a decision tree in **rounds**. The frontier is the
set of questions answerable without answers you haven't heard yet. Ask
the whole frontier at once — each question numbered, each with a
recommended answer. Facts (does this file exist, what does the loader
accept) are looked up, never asked. Stop when the frontier is empty.

For routine tickets this whole protocol collapses to the checklist
below; grill fully only for new subsystems or ambiguous UI work.

## Intake rules (each one is a scar)

1. **Name new files without backticks.** The refine gate existence-checks
   backticked path-like tokens; a backticked new-file path bounces the
   ticket as `needs_info`.
2. **The body is snapshotted at intake.** Editing the GitHub issue later
   does not update the board DB. Fix the DB row, then `factory retry`.
3. **Acceptance criteria must be checkable by a stranger.** "Charts look
   good" fails; "each chart has a title element and y-axis labels"
   passes. If a human with eyes is the only judge, say so and route it
   to the GUI preview + operator review instead of a test.
4. **Never pin exact pixels or coordinates in a ticket.** Ask for
   structure (elements exist, ordering, scaling within a tolerance).
   Exact-geometry acceptance criteria teach models to delete tests.
5. **State the data contract explicitly.** Schema, units, empty-input
   behavior, and whether fixtures are synthetic (they always are).
6. **Name the tier honestly.** Geometry-heavy or long-context tickets
   are strong-tier candidates from the start; three cheap-tier failures
   cost more than one strong attempt.
7. **Keep the diff promise.** One ticket, one surface. If the ticket
   touches two subsystems, split it — the blast gate will.
8. **Done criteria are outcomes, never shapes.** Size, line length,
   operator-per-line, layout and naming conventions are never done
   criteria, even inside "Done when" — they belong to the repo's style,
   and a model told to satisfy them will delete tests to do it. State
   what must be true of the result, not what the code must look like.

## Done when

You can state, without opening the repo: what code changes, what test
proves it, what the operator will see in the PR (diff, checks, and for
GUI paths, the screenshot preview), and what the cheap tier can
realistically do in one attempt.
