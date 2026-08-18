# Relational → NoSQL Schema Migration Tool — Build Plan

**Stack:** Python 3.11+, FastAPI web UI, rule-based decision engine
**Targets:** MongoDB (document), Cassandra (column-family), Neo4j (graph)

---

## 1. What the tool actually does

**Input:** a normalized SQL schema (`CREATE TABLE` DDL) + a set of sample queries, each with an
optional weight (relative frequency) and a read/write tag.

**Output, per target:**
- A proposed denormalized schema (collections / column families / node-and-edge model)
- Concrete artifacts: `$jsonSchema` validators + example documents, CQL `CREATE TABLE`, Cypher constraints
- Index / key recommendations derived from the query workload
- A **trade-off report**: for every denormalization decision — which rule fired, which signals drove
  it, what you gain, what you give up, and how to mitigate it

The trade-off report is the product. Anyone can flatten a schema; the value is in a defensible,
traceable rationale for every embed-vs-reference call.

---

## 2. Architecture

```
sql/ddl + queries
      │
      ▼
┌─────────────────┐   sqlglot
│  parser         │──► RelationalModel (tables, columns, PK/FK/unique, nullability)
│                 │──► Workload (access patterns: joins, filters, projections, sorts, aggregates)
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  analyzer       │  FK graph (networkx) · cardinality inference · table role classification
│                 │  co-access matrix · fan-out & growth estimates · write-pressure scoring
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  decision engine│  ordered rule set → Decision(edge, action, rule_id, signals, confidence)
│  (rule-based)   │  actions: EMBED · EMBED_SUBSET · REFERENCE · DUPLICATE · SPLIT
└─────────────────┘
      │
      ├──► emitters/document.py   → collections, validators, example docs, index plan
      ├──► emitters/columnar.py   → one table per query, partition/clustering keys, CQL
      ├──► emitters/graph.py      → labels, rel types, property placement, Cypher
      │
      ▼
┌─────────────────┐
│  reporter       │  trade-off narrative + machine-readable JSON
└─────────────────┘
      │
      ▼
   FastAPI  ──►  /api/analyze  ·  static SPA (paste DDL → see schema, ER graph, report)
```

**Layout**

```
nosqlmigrate/
  core/        model.py  workload.py  graph.py  signals.py
  parsing/     ddl.py  queries.py  dialects.py
  rules/       engine.py  document_rules.py  columnar_rules.py  graph_rules.py  catalog.py
  emitters/    document.py  columnar.py  graph.py  base.py
  report/      tradeoffs.py  templates/
  api/         main.py  schemas.py
  web/         index.html  app.js  styles.css
  cli.py
tests/fixtures/  ecommerce/  blog/  library/   (schema.sql + queries.sql + expected/*.json)
```

---

## 3. The analysis layer (where the real work is)

**Cardinality inference** — no live database, so infer from structure:

- FK column carries a UNIQUE constraint → **1:1**
- FK column non-unique → **1:N**
- Table whose PK is exactly two FKs and has no non-key columns → **junction table → M:N**
  (with extra payload columns → M:N with association attributes; different emit path)
- Self-referencing FK → hierarchy; flag for materialized-path vs adjacency-list treatment
- Nullable FK → optional relationship; affects whether embedding wastes space

**Signals computed per table and per FK edge:**

| Signal | Derived from | Feeds |
|---|---|---|
| `co_access` | % of weighted queries joining both tables | embed vs reference |
| `child_standalone` | queries selecting child without parent | reference / duplicate |
| `write_ratio` | weighted INSERT/UPDATE/DELETE touching table | embed penalty |
| `fanout_estimate` | cardinality class + heuristic from column semantics | unbounded-array risk |
| `row_width` | sum of declared column widths | 16 MB doc-limit risk |
| `is_lookup` | few columns, referenced by many, never updated in workload | inline duplication |
| `is_junction` | PK = two FKs | M:N handling |
| `unbounded` | child has time-ish column and no natural cap | forces REFERENCE |
| `shared_child` | referenced by >1 parent | duplication vs reference |

**Table role classification:** aggregate root · owned/dependent entity · lookup/reference ·
junction · standalone.

---

## 4. Decision rules (document target — sketch)

Ordered, first-match-wins, each with an id so the report can cite it:

- `D01 UNBOUNDED_CHILD` — child grows without bound (orders→events, user→logs) → **REFERENCE**.
  Rationale: embedding invites the 16 MB ceiling and rewrites the whole doc on every append.
- `D02 SIZE_RISK` — `fanout × row_width` over threshold → **REFERENCE**, or `EMBED_SUBSET`:
  embed the N most recent, keep the full set in its own collection.
- `D03 CONTAINED_1N` — 1:N, bounded fan-out, high `co_access`, child never queried standalone,
  low write pressure → **EMBED**. The classic order→line_items case.
- `D04 ONE_TO_ONE_COACCESSED` — 1:1 and co-accessed → **EMBED** (collapse the table entirely).
- `D05 SMALL_LOOKUP` — lookup table, read-only in workload → **DUPLICATE** the display fields into
  the referencing doc, keep the code as the join key. Extended-reference pattern.
- `D06 SHARED_MUTABLE_CHILD` — referenced by multiple parents *and* updated in the workload →
  **REFERENCE**. Duplication here means N-way fan-out updates.
- `D07 JUNCTION_SMALL_SIDE` — M:N with one bounded side → embed an array of ids on the bounded
  side only (one-way; avoid dual arrays).
- `D08 JUNCTION_LARGE` — M:N, both sides unbounded → keep the junction as its own collection.
- `D09 CHILD_STANDALONE` — child heavily queried on its own → **REFERENCE** even if co-accessed;
  optionally `DUPLICATE` a summary subset if reads dominate writes.
- `D10 DEEP_NESTING` — embedding would exceed 3 levels → break at the deepest level, citing
  positional-update limits on nested arrays.

**Column-family rules** are query-first and structurally different: one table per access pattern;
partition key from the equality predicates; clustering columns from range predicates and ORDER BY;
flag low-cardinality partition keys (hotspots) and unbounded partitions; report the write
amplification the resulting duplication causes.

**Graph rules:** FK → relationship; junction table with payload → relationship with properties;
lookup table → node label or property depending on whether it is ever traversed.

Each `Decision` carries `gains`, `costs`, and `mitigations` strings so the report writes itself.

---

## 5. Trade-off report

Per decision:

> **orders → order_items: EMBED** (rule `D03 CONTAINED_1N`, confidence high)
> Signals: 1:N, fan-out ≈ 10–50 bounded, co-access 0.86, standalone access 0.0, write ratio 0.11
> **Gain:** single-document reads for 86% of the workload; atomic order writes without transactions.
> **Cost:** line items cannot be queried independently without `$unwind`; every item edit rewrites
> the order document; per-item indexes become multikey.
> **Mitigation:** if item-level analytics appear later, add a rollup collection rather than
> unembedding.

Plus a schema-level summary: joins eliminated, queries now served by a single lookup, remaining
cross-collection reads, consistency risks introduced (which duplicated fields need fan-out updates
and where), and unmigratable constructs (triggers, CHECK constraints, views, stored procedures) as
an explicit "handle this in the application layer" list.

---

## 6. Build phases

| Phase | Deliverable | Done when |
|---|---|---|
| **1. Core model + parser** | `RelationalModel` from DDL via sqlglot; MySQL/Postgres dialects; inline + table-level FK; composite keys | 3 fixture schemas parse into correct models, unit-tested |
| **2. Query analysis** | `Workload` from SELECT/INSERT/UPDATE/DELETE; join edges, predicates, projections, sorts, weights | Access patterns extracted for all fixture queries |
| **3. Graph + signals** | FK graph, cardinality inference, role classification, full signal set | Signals snapshot-tested per fixture |
| **4. Rule engine + document rules** | Pluggable engine, `D01`–`D10`, decisions with full provenance | Golden-file tests: expected decisions per fixture |
| **5. Document emitter** | Collections, `$jsonSchema`, example docs, index plan | Emitted JSON validates; example docs conform to their own validators |
| **6. Trade-off reporter** | Markdown + JSON report | Every decision cites a rule and lists gain/cost/mitigation |
| **7. CLI** | `nosqlmigrate analyze schema.sql --queries q.sql --target document -o out/` | End-to-end on all fixtures |
| **8. Column-family target** | Query-first table generation, key selection, CQL, hotspot/unbounded warnings | Cassandra fixture produces valid CQL; warnings fire correctly |
| **9. Graph target** | Cypher constraints + indexes, node/rel model | Neo4j fixture output |
| **10. FastAPI + web UI** | `POST /api/analyze`, two-pane editor, ER graph (Cytoscape or Mermaid), target tabs, expandable rationale cards, artifact download | Paste-to-result under a second at fixture size |
| **11. Polish** | Rule-catalog page, "why not?" for rules that nearly fired, sample-schema loader, README | Demoable start to finish |

Phases 1–7 are the working tool; 8–11 are breadth and presentation. If time runs short, ship 1–7
plus 10 for a single target rather than three shallow targets.

---

## 7. Testing

- **Fixtures:** e-commerce (the embed/reference showcase), blog (M:N tags, self-referencing
  comments), library (lookup tables, 1:1 detail split). Each with `schema.sql`, `queries.sql`, and
  golden `expected/decisions.json` + `expected/document.json`.
- **Golden-file tests** on decisions — a rule change that shifts a decision must show as an explicit diff.
- **Property test:** every input table appears in the output as a collection, an embedded document,
  or an explicitly-dropped entry with a reason. Nothing silently vanishes.
- **Round-trip sanity:** every input query maps to a documented retrieval path in the output.
- **Validation:** emitted `$jsonSchema` checked against generated example docs; CQL parsed for
  syntax; Cypher optionally run against a Neo4j test container.

---

## 8. Known limits (state these up front)

- No live statistics — cardinality and fan-out are structural inferences, not measurements. The tool
  should let a user override any estimate and re-run.
- Query weights are user-supplied; garbage in, garbage out.
- Views, triggers, stored procedures, and CHECK constraints are reported as unmigratable, not translated.
- The output is a *proposal*. It should read like a design review, not a migration script.
