"""API: the web boundary wraps the pipeline, serves samples and the catalog."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nosqlmigrate.api.main import app  # noqa: E402
from tests.conftest import fixture_dir  # noqa: E402

client = TestClient(app)


def test_analyze_ecommerce():
    payload = {
        "ddl": (fixture_dir("ecommerce") / "schema.sql").read_text(),
        "queries": (fixture_dir("ecommerce") / "queries.sql").read_text(),
    }
    res = client.post("/api/analyze", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["report"]["summary"]["tables"] == 9
    assert body["report"]["summary"]["collections"] == 6
    assert "order_items" in body["markdown"]
    assert "CREATE TABLE" in body["artifacts"]["cassandra_cql"]
    assert "CREATE CONSTRAINT" in body["artifacts"]["neo4j_cypher"]
    assert "erDiagram" in body["er_mermaid"]


def test_analyze_with_dialect_and_bad_input():
    res = client.post("/api/analyze", json={"ddl": "CREATE TABLE t (id INT)"})
    assert res.status_code == 200
    assert res.json()["report"]["summary"]["tables"] == 1

    res = client.post("/api/analyze", json={"ddl": ""})
    assert res.status_code == 422  # pydantic: min_length


def test_samples_endpoint():
    res = client.get("/api/samples")
    assert res.status_code == 200
    samples = res.json()
    assert set(samples) == {"blog", "ecommerce", "library"}
    assert "CREATE TABLE" in samples["ecommerce"]["schema"]
    assert "@weight" in samples["ecommerce"]["queries"]


def test_rules_endpoint():
    res = client.get("/api/rules")
    assert res.status_code == 200
    rules = res.json()
    ids = {r["id"] for r in rules}
    assert {"D01", "D03", "D05", "D07", "D10", "C01", "G01"} <= ids


def test_index_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "nosqlmigrate" in res.text
    assert "Analyze" in res.text


def test_static_assets():
    for asset in ("styles.css", "app.js"):
        res = client.get(f"/static/{asset}")
        assert res.status_code == 200
