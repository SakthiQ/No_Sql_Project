"""Shared fixtures: load fixture schemas/queries and manage golden files."""
from __future__ import annotations

import json
from pathlib import Path

from nosqlmigrate.parsing.ddl import parse_ddl

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_NAMES = ("ecommerce", "blog", "library")


def fixture_dir(name: str) -> Path:
    return FIXTURES / name


@pytest.fixture(params=FIXTURE_NAMES)
def fixture_name(request) -> str:
    return request.param


@pytest.fixture
def schema_sql(fixture_name) -> str:
    return (fixture_dir(fixture_name) / "schema.sql").read_text()


@pytest.fixture
def queries_sql(fixture_name) -> str:
    return (fixture_dir(fixture_name) / "queries.sql").read_text()


def golden_path(fixture_name: str, kind: str) -> Path:
    return fixture_dir(fixture_name) / "expected" / f"{kind}.json"


def load_golden(fixture_name: str, kind: str):
    path = golden_path(fixture_name, kind)
    if not path.exists():
        pytest.fail(f"golden file missing: {path} (run scripts/update_golden.py)")
    return json.loads(path.read_text())


def assert_matches_golden(actual: dict, fixture_name: str, kind: str) -> None:
    expected = load_golden(fixture_name, kind)
    assert actual == expected, (
        f"golden mismatch for {fixture_name}/{kind}. If the change is intentional, "
        "regenerate with scripts/update_golden.py and explain the semantic change in your PR."
    )


# ---------------------------------------------------------------- session-scoped parsed models

@pytest.fixture(scope="session")
def model_ecommerce():
    return parse_ddl((fixture_dir("ecommerce") / "schema.sql").read_text())


@pytest.fixture(scope="session")
def model_blog():
    return parse_ddl((fixture_dir("blog") / "schema.sql").read_text())


@pytest.fixture(scope="session")
def model_library():
    return parse_ddl((fixture_dir("library") / "schema.sql").read_text())
