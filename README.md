# nosqlmigrate — relational → NoSQL schema advisor

Paste a relational schema and the queries your app actually runs. Get a **proposed
denormalized schema for MongoDB, Cassandra, and Neo4j** — plus a trade-off report
that defends every embed-vs-reference call with the rule that fired, the signals
that drove it, what you gain, what you give up, and how to mitigate it.

Anyone can flatten a schema. The product is the **defensible, traceable rationale**.

```
sql/ddl + queries.sql
   │  parsing (sqlglot: DDL → RelationalModel, queries → Workload)
   ▼
analysis (FK graph · cardinality inference · roles · signals)
   │  rule engine (D01–D10, first-match-wins, every decision citable)
   ├──► MongoDB:  collections, $jsonSchema validators, example docs, index plan
   ├──► Cassandra: one table per access pattern, CQL, hotspot warnings
   ├──► Neo4j:     nodes, relationships, constraints, Cypher
   ▼
trade-off report (markdown + JSON): gain/cost/mitigation per decision,
query mapping, consistency risks, unmigratable constructs
```

## 60-second start

```bash
./scripts/bootstrap.sh                # venv + pinned deps (sqlglot is pinned on purpose)
source .venv/bin/activate

# try it on a sample
nosqlmigrate analyze nosqlmigrate/samples/ecommerce/schema.sql \
    -q nosqlmigrate/samples/ecommerce/queries.sql --target all -o out/
cat out/report.md                     # the design review
```

Or the web UI:

```bash
uvicorn nosqlmigrate.api.main:app --reload
# open http://localhost:8000 — load a sample, hit Analyze
```

## What a decision looks like

> #### `edge:orders->order_items` — **EMBED** via `D03 CONTAINED_1N` (confidence: high)
> Signals: `cardinality=1:N` · `co_access=0.67` · `child_standalone=0.33` ·
> `write_ratio_child=0.33` · `fanout=(1,100)` · `unbounded=False`
> **Gain:** 67% of orders reads also need order_items; embedding serves them with a
> single document fetch and makes parent+children writes atomic without transactions.
> **Cost:** order_items can no longer be queried independently without `$unwind`;
> every item edit rewrites the order document; item-level indexes become multikey.
> **Mitigation:** if item-level analytics appear later, add a rollup collection
> rather than un-embedding.

Every decision also lists its **near-misses** — the rules that *almost* fired and by
how much (`D01: fan-out bounded at ~100 — large but finite`). The interesting
schemas live near the thresholds.

## The rule catalog

| | rule | action |
|---|---|---|
| D01 | UNBOUNDED_CHILD | reference (temporal growth, 16 MB ceiling) |
| D02 | SIZE_RISK | reference / embed-subset by estimated size |
| D03 | CONTAINED_1N | embed (bounded, co-accessed, quiet child) |
| D04 | ONE_TO_ONE_COACCESSED | embed (collapse the table) |
| D05 | SMALL_LOOKUP | duplicate display fields, keep the join key |
| D06 | SHARED_MUTABLE_PARENT | reference (N-way fan-out updates) |
| D07 | JUNCTION_SMALL_SIDE | one-way id array on the bounded side |
| D08 | JUNCTION_LARGE | keep the junction collection |
| D09 | CHILD_STANDALONE | reference ($unwind would dominate) |
| D10 | DEEP_NESTING | break embed chains beyond three levels |

Plus structural fallbacks (S01 self-reference, R00 default-reference) and the
column-family/graph rules (C01–C03, G01–G02). `nosqlmigrate rules` prints the
catalog; the web UI renders it with full rationale.

## Estimates are estimates

Cardinality and fan-out are **structural inferences** (unique FK → 1:1; temporal
child column → unbounded), and query weights are user-supplied. Any of them can be
overridden and re-run:

```json
{"fanout": {"orders->order_items": [1000, null]}, "co_access": {"orders->order_items": 0.2}}
```

```bash
nosqlmigrate analyze schema.sql -q queries.sql --overrides overrides.json
```

## What it won't do

Views, triggers, stored procedures, and CHECK constraints are reported as
**unmigratable** with an explicit "handle this in the application layer" list —
not silently translated. The output is a *proposal* that reads like a design
review, not a migration script.

## Repository layout

```
nosqlmigrate/
  core/       model.py · workload.py · graph.py · signals.py · types.py
  parsing/    ddl.py (three passes) · queries.py · dialects.py
  rules/      catalog.py (single source of truth) · engine.py · document_rules.py
  emitters/   document.py · columnar.py · graph.py
  report/     tradeoffs.py · diagrams.py
  api/        main.py · schemas.py        web/ (SPA)        samples/
tests/
  fixtures/   ecommerce · blog · library  (schema.sql + queries.sql + expected/)
```

## Development

```bash
.venv/bin/pytest              # 150+ tests: unit, structural, golden files, API, CLI
.venv/bin/ruff check .
```

Golden files (`tests/fixtures/*/expected/`) pin the model, the signals, and the
decisions for all three fixtures. A rule change that shifts a decision shows up as
an explicit golden diff — regenerate with `scripts/update_golden.py` **and explain
the semantic change in your PR**.

The three fixtures are the spec: e-commerce (embed/reference showcase), blog
(M:N tags, self-referencing comments, heap table), library (1:1 via PK=FK, lookups,
ALTER-added FK, composite FK, quoted mixed-case identifiers).

Design docs: [`PLAN.md`](PLAN.md) (what & why) · [`docs/roadmap.md`](docs/roadmap.md)
(sequencing & gates) · [`docs/phase-1-plan.md`](docs/phase-1-plan.md) (phase-1 detail).
