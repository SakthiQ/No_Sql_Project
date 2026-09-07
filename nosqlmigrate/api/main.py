"""The web API and static SPA.

    uvicorn nosqlmigrate.api.main:app

Endpoints:
    POST /api/analyze   — run the full pipeline, return report + artifacts
    GET  /api/samples   — sample schemas for the loader
    GET  /api/rules     — the rule catalog (single source of truth)
    GET  /              — the SPA
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import pipeline
from ..emitters.columnar import emit_columnar
from ..emitters.graph import emit_graph
from ..report.diagrams import mermaid_er
from ..rules.catalog import CATALOG
from .schemas import AnalyzeRequest

app = FastAPI(
    title="nosqlmigrate",
    version="0.1.0",
    description="Relational → NoSQL schema migration advisor",
)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    try:
        result = pipeline.run(req.ddl, req.queries, dialect=req.dialect, overrides=req.overrides)
    except Exception as exc:  # the pipeline is permissive, but never 500 blindly
        raise HTTPException(status_code=422, detail=f"analysis failed: {exc}") from exc

    cassandra = emit_columnar(result.analysis, result.decisions)
    neo4j = emit_graph(result.analysis, result.decisions)
    return {
        "report": result.report,
        "markdown": result.markdown,
        "er_mermaid": mermaid_er(result.analysis),
        "artifacts": {
            "cassandra_cql": cassandra.render(),
            "cassandra_warnings": [w for t in cassandra.tables for w in t.warnings],
            "write_amplification": cassandra.write_amplification,
            "neo4j_cypher": neo4j.render(),
        },
    }


@app.get("/api/samples")
def samples() -> dict:
    out = {}
    for sample_dir in sorted(SAMPLES_DIR.iterdir()):
        if not sample_dir.is_dir():
            continue
        schema = sample_dir / "schema.sql"
        if schema.exists():
            out[sample_dir.name] = {
                "schema": schema.read_text(),
                "queries": (sample_dir / "queries.sql").read_text()
                if (sample_dir / "queries.sql").exists() else "",
            }
    return out


@app.get("/api/rules")
def rules() -> list[dict]:
    return [
        {
            "id": m.id, "name": m.name, "action": m.action, "target": m.target,
            "summary": m.summary, "rationale": m.rationale,
        }
        for m in CATALOG.values()
    ]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
