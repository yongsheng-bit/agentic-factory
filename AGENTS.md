# Working on the factory itself

The factory is a stdlib-only product. These rules are what its own CI
enforces; they apply to every change in this repository.

1. **Stdlib only.** No pip installs, no probing the environment. The
   build agent's world is `python -m unittest` and the standard
   library; playwright-class dependencies run only as subprocesses of
   a customer venv, never imported here.
2. **Tests are unittest.** `PYTHONPATH=. python -m unittest discover
   -s tests`. A test must constrain the code: if you can delete the
   implementation and the suite stays green, the test is decoration.
3. **No `#` comments in Python.** Docstrings carry intent; `tools/
   no_comments_check.py` enforces this in CI.
4. **A green job is not proof its steps ran.** When changing CI or
   gates, verify a step actually executed (fail it on purpose once).
5. **Ticket authoring:** read `docs/grill-ticket.md` before filing.
   Most gate failures are ticket-quality failures.
6. **Vocabulary:** use `GLOSSARY.md` words in code, tickets, and PRs.
7. **Never merge a customer PR.** Opening PRs is the factory's limit;
   merging belongs to the operator.
