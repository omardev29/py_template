# /// script
# requires-python = ">=3.11"
# dependencies = ["cosmic-ray==8.7.0"]
# ///
"""Cosmic Ray's mutation operators for `./pyt selftest --mutation` (runner/mutation.py).

The runner starts it with `uv run --locked --script`, so Cosmic Ray and its dependencies stay out
of the project's environments (the lock is mutation_cr.py.lock, next to this file). It only lists
the mutants of a module and makes one; the runner writes it into a throwaway copy of the project,
runs the tests there and puts the file back. Cosmic Ray's session database, distributors and test
runner are not used (CLAUDE.md, section 15.1).

  mutation_cr.py check   print Cosmic Ray's version (the CI image makes the environment with it)
  mutation_cr.py serve   read one JSON request per line on stdin, write one JSON line per answer:
    {"op": "version"} -> {"version": Cosmic Ray's version}
    {"op": "list", "path": P, "operators": [NAME, ...]}
        -> {"mutants": [[NAME, OCCURRENCE, INDEX, START_LINE, START_COLUMN, END_LINE,
                         END_COLUMN, DEFINITION], ...]}
    {"op": "mutate", "path": P, "operator": NAME, "occurrence": N}
        -> {"code": the whole mutated module, or null when there is no such mutant}
        -> {"code": null, "cannot": MESSAGE} when Cosmic Ray fails to make that mutant (the
           runner skips it; its ExceptionReplacer, which failed on a dotted name in a tuple, is
           never asked: the runner makes those mutants itself)
    a request that fails otherwise -> {"error": MESSAGE}

OCCURRENCE is Cosmic Ray's own: the mutant's rank among those of its operator in the module, in
the order of a pre-order walk of the parso tree, which is also the order MutationVisitor counts
in when it makes the mutant. INDEX tells the mutants of one node apart (NumberReplacer's +1 is 0,
its -1 is 1). Lines count from 1 and columns from 0, in characters, as parso gives them.
DEFINITION is the function or class around the mutant (null at module level). A module is read
as UTF-8 with its line endings kept: the mutated code has the same ones.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path
from typing import Any


def _source(path: str) -> str:
    return Path(path).read_bytes().decode("utf-8")


def _list(path: str, names: list[str]) -> list[list[Any]]:
    from cosmic_ray import plugins
    from cosmic_ray.ast import ast_nodes, get_ast
    from cosmic_ray.ast.ast_query import ASTQuery

    nodes = list(ast_nodes(get_ast(_source(path))))
    found: list[list[Any]] = []
    for name in names:
        operator = plugins.get_operator(name)()
        occurrence = 0
        for node in nodes:
            for index, (start, end) in enumerate(operator.mutation_positions(node)):
                found.append([name, occurrence, index, start[0], start[1], end[0], end[1], ASTQuery(node).get_definition_name()])
                occurrence += 1
    return found


def _mutate(path: str, name: str, occurrence: int) -> dict[str, Any]:
    from cosmic_ray import plugins
    from cosmic_ray.mutating import mutate_code

    source, operator = _source(path), plugins.get_operator(name)()
    try:
        code: str | None = mutate_code(source, operator, occurrence)
    except Exception as e:  # noqa: BLE001 - Cosmic Ray's own failure to make this one mutant
        return {"code": None, "cannot": f"{type(e).__name__}: {e}"}
    return {"code": code}


def _answer(request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    if op == "version":
        import cosmic_ray.plugins  # noqa: F401 - the operators load, as the other requests need them

        return {"version": metadata.version("cosmic-ray")}
    if op == "list":
        return {"mutants": _list(str(request["path"]), [str(n) for n in request["operators"]])}
    if op == "mutate":
        return _mutate(str(request["path"]), str(request["operator"]), int(request["occurrence"]))
    return {"error": f"unknown request {op!r}"}


def serve() -> int:
    # The answers get stdout's file of their own; whatever else prints (a library, a warning)
    # goes to stderr, so it can never be read as an answer.
    answers = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            answer = _answer(json.loads(line))
        except Exception as e:  # noqa: BLE001 - every failure is an answer: the runner reports it
            answer = {"error": f"{type(e).__name__}: {e}"}
        answers.write(json.dumps(answer, ensure_ascii=True) + "\n")
        answers.flush()
    return 0


def main(argv: list[str]) -> int:
    if argv == ["check"]:
        import cosmic_ray.plugins  # noqa: F401 - the operators load, as serve needs them

        print(f"cosmic-ray {metadata.version('cosmic-ray')}")
        return 0
    if argv == ["serve"]:
        return serve()
    print("usage: mutation_cr.py check|serve", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
