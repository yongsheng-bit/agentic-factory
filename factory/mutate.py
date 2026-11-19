"""Mutation gate: prove the tests can actually fail.

The agent writes the tests its own code is judged by, so a green suite is
evidence of consistency, not of constraint. This gate attacks exactly that
gap: mechanically break the code the PR added (swap comparisons, flip
booleans, bump constants, trade and for or), re-run the suite per mutant,
and count how many breakages the tests catch. A suite that stays green
while the code gets wrong is not verification - measured on the first
petro PRs, three shipped suites that never passed at all, and a parser
test could equally pass on a parser that ignores every line.

Only lines the diff ADDS are mutated: pre-existing code is the repo's
problem, not this PR's, and scanning whole files turns every untouched
module into gate noise. A mutant that survives is not proof of a bad
test (equivalent mutants exist), so the gate fails on a low kill RATE
with enough mutants, never on a single survivor.
"""

import ast
import subprocess

from .gates import added_lines

OP_SWAPS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


class _Collector(ast.NodeVisitor):
    """Collect (node, kind, payload) mutation candidates on added lines."""

    def __init__(self, wanted_lines):
        self.wanted = wanted_lines
        self.candidates = []

    def visit_Compare(self, node):
        if node.lineno in self.wanted:
            for i, op in enumerate(node.ops):
                swap = OP_SWAPS.get(type(op))
                if swap is not None:
                    self.candidates.append((node, "op", i))
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        if node.lineno in self.wanted:
            self.candidates.append((node, "boolop", None))
        self.generic_visit(node)

    def visit_Constant(self, node):
        if node.lineno in self.wanted:
            if node.value is True or node.value is False:
                self.candidates.append((node, "const", None))
            elif isinstance(node.value, (int, float)):
                self.candidates.append((node, "num", None))
        self.generic_visit(node)


def _apply(tree, candidate):
    node, kind, payload = candidate
    if kind == "op":
        node.ops[payload] = OP_SWAPS[type(node.ops[payload])]()
    elif kind == "boolop":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "const":
        node.value = not node.value
    elif kind == "num":
        node.value = node.value + 1


def candidates(source, wanted_lines):
    """Mutation candidates in `source` restricted to `wanted_lines`.

    Returns [] for files that do not parse - a syntax error is the test
    gate's problem, not this gate's.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    col = _Collector(set(wanted_lines))
    col.visit(tree)
    return col.candidates


def _is_test_file(path):
    """True for test code, which the mutation gate must never mutate.

    Mutants inside a test file cannot be killed by that same test - a
    test-only ticket (e.g. "add a browser test") scored an impossible
    0/12, and the failure message goaded the next attempt into rewriting
    product code it was told to leave alone. Tests are the weapon in
    this gate, never the target.
    """
    norm = path.replace("\\", "/")
    return norm.startswith("tests/") or norm.rsplit("/", 1)[-1].startswith(
        "test_")


def plan(diff, read_file, max_mutants=12):
    """Deterministic mutant plan: [(path, source, wanted, index), ...].

    Candidates are referenced by index into a freshly collected list rather
    than by AST node: holding node references across mutants mutates the
    shared source tree in place and mutant N silently contains mutants
    1..N-1 as well - measured, it made a strong suite score 4/5 on code it
    never actually saw. Indexing into a re-collected list keeps the plan
    stable (same diff mutates the same spots) while each mutant applies to
    a pristine parse.
    """
    per_file = {}
    for path, line_no, _ in added_lines(diff):
        if path.endswith(".py") and not _is_test_file(path):
            per_file.setdefault(path, set()).add(line_no)
    plan_list = []
    for path in sorted(per_file):
        source = read_file(path)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        col = _Collector(per_file[path])
        col.visit(tree)
        ordered = sorted(col.candidates,
                         key=lambda c: (c[0].lineno, c[0].col_offset))
        for idx in range(len(ordered)):
            plan_list.append((path, source, frozenset(per_file[path]), idx))
            if len(plan_list) >= max_mutants:
                return plan_list
    return plan_list


def run_mutations(workdir, diff, command, env=None, max_mutants=12,
                  min_score=0.5, timeout_s=120):
    """Mutate the diff's added lines and score how many the suite kills.

    Returns (ok, report). ok is False only with enough mutants to be
    confident (>= 3) and a kill rate below `min_score`; anything else -
    no candidates, unreadable files, a suite that cannot start - passes
    with a note, because this gate sharpens the test gate and must never
    become a new way to block a good PR on tooling noise.
    """
    def read_file(rel):
        try:
            with open(f"{workdir}/{rel}", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    mutants = plan(diff, read_file, max_mutants)
    if not mutants:
        return True, "mutation: no candidates in added lines (skipped)"
    killed, survived = 0, []
    for path, source, wanted, idx in mutants:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        col = _Collector(wanted)
        col.visit(tree)
        ordered = sorted(col.candidates,
                         key=lambda c: (c[0].lineno, c[0].col_offset))
        if idx >= len(ordered):
            continue
        cand = ordered[idx]
        try:
            _apply(tree, cand)
            mutated = ast.unparse(tree)
        except (ValueError, RecursionError):
            continue
        try:
            with open(f"{workdir}/{path}", "w", encoding="utf-8") as fh:
                fh.write(mutated)
        except OSError:
            continue
        try:
            proc = subprocess.run(command, shell=True, cwd=workdir, env=env,
                                  capture_output=True, text=True,
                                  timeout=timeout_s)
            died = proc.returncode != 0
        except subprocess.TimeoutExpired:
            died = True
        finally:
            with open(f"{workdir}/{path}", "w", encoding="utf-8") as fh:
                fh.write(source)
        if died:
            killed += 1
        else:
            survived.append(f"{path}:{cand[0].lineno}")
    total = killed + len(survived)
    if total == 0:
        return True, "mutation: no mutant could be applied (skipped)"
    score = killed / total
    report = (f"mutation: {killed}/{total} killed"
              + (f" (survived: {', '.join(survived[:6])})" if survived else ""))
    if total >= 3 and score < min_score:
        return False, (f"tests do not constrain the new code - "
                       f"{report}, threshold {min_score:g}")
    return True, report
