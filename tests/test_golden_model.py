"""Golden model snapshots: pin the model's shape.

A diff here after a code change means the model's serialized shape changed —
that must be a deliberate act with a written justification, because every
later golden file (decisions, emitters) builds on this one."""
from __future__ import annotations

from tests.conftest import assert_matches_golden, FIXTURE_NAMES


def test_golden_model_snapshots(fixture_name, request):
    model = request.getfixturevalue(f"model_{fixture_name}")
    assert_matches_golden(model.to_dict(), fixture_name, "model")
