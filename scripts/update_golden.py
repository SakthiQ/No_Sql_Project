#!/usr/bin/env python
"""Regenerate golden files: tests/fixtures/*/expected/*.json.

Run after an INTENTIONAL semantic change, and explain the change in your PR.
Usage:  .venv/bin/python scripts/update_golden.py [model] [signals] [decisions]
With no arguments, regenerates everything the codebase can produce.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nosqlmigrate.parsing.ddl import parse_ddl  # noqa: E402
from tests.conftest import FIXTURE_NAMES, FIXTURES  # noqa: E402


def dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n"


def main() -> None:
    wanted = set(sys.argv[1:]) or {"model", "signals", "decisions"}
    for name in FIXTURE_NAMES:
        fdir = FIXTURES / name
        expected = fdir / "expected"
        expected.mkdir(exist_ok=True)

        model = parse_ddl((fdir / "schema.sql").read_text())
        if "model" in wanted:
            (expected / "model.json").write_text(dump(model.to_dict()))
            print(f"{name}: model.json")

        if wanted & {"signals", "decisions"}:
            from nosqlmigrate.analyze import analyze
            from nosqlmigrate.parsing.queries import parse_queries

            workload = parse_queries((fdir / "queries.sql").read_text(), model)
            analysis = analyze(model, workload)
            if "signals" in wanted:
                (expected / "signals.json").write_text(dump(analysis.to_dict()))
                print(f"{name}: signals.json")
            if "decisions" in wanted:
                from nosqlmigrate.rules.engine import decide

                result = decide(analysis)
                (expected / "decisions.json").write_text(dump(result.to_dict()))
                print(f"{name}: decisions.json")


if __name__ == "__main__":
    main()
