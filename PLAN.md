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

These are the **shared** signals, computed once and consumed by all three targets. Each target then
derives its own on top of them — partition cardinality and write amplification for column-family
(§4.2), traversal depth and node degree for graph (§4.3). The analysis layer is the only part of the
pipeline the three targets genuinely have in common.

---

## 4. Decision rules

Each target gets its own rule set, and they are **not variations on one algorithm**. The document
rules walk the FK graph edge by edge deciding embed-vs-reference. The column-family rules barely look
at the FK graph at all — they iterate over the *query workload* and build a table per access pattern.
The graph rules mostly preserve the relational structure and spend their effort on what to reify and
where to seed traversals. Three genuinely different algorithms sharing one analysis layer.

All rules are ordered, first-match-wins, and carry an id so the report can cite them. Every
`Decision` carries `gains`, `costs`, and `mitigations` strings so the trade-off report writes itself
from the decision data rather than being composed separately.

### 4.1 Document target (MongoDB) — `D01`–`D10`

Edge-driven: for each FK edge in the graph, decide how the child relates to the parent.

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

### 4.2 Column-family target (Cassandra) — `C01`–`C10`

**Query-driven, not entity-driven.** This is the part that most resembles a different tool wearing
the same skin. Cassandra has no joins and no ad-hoc filtering: a query is only servable if its table
was designed for it. So the algorithm iterates the *workload*, not the schema graph, and the output
table count tracks the number of distinct access patterns rather than the number of entities. A
9-table relational schema with 14 access patterns produces roughly 14 column-family tables, most of
them holding overlapping copies of the same data.

- `C01 QUERY_TABLE` — every distinct (filter set, sort order) combination in the workload becomes its
  own table. Two queries with identical filters but different `ORDER BY` are two tables.
- `C02 PARTITION_FROM_EQUALITY` — partition key is built from the equality predicates. Every query
  against the table must supply the complete partition key, so a query filtering only on a range has
  no valid table and is reported as **unservable** rather than silently given a bad one.
- `C03 CLUSTERING_FROM_RANGE_SORT` — clustering columns come from range predicates first, then
  `ORDER BY` columns, in that order, with direction taken from the sort. The last clustering column
  must make the row unique or rows collide and overwrite.
- `C04 LOW_CARDINALITY_PARTITION` — partition key with few distinct values (status flags, booleans,
  country codes) concentrates the whole workload on a handful of nodes → **hotspot**. Recommend a
  compound partition key or explicit bucketing.
- `C05 UNBOUNDED_PARTITION` — a partition that grows forever (every event for a user, every reading
  from a sensor) → add a time bucket to the partition key, e.g. `((user_id, month), event_time)`.
  This is the single most common Cassandra design failure and the tool should be loud about it.
- `C06 STATIC_COLUMNS` — parent attributes that would otherwise repeat on every row of a partition →
  emit as `STATIC` columns instead of duplicating per row.
- `C07 JOIN_TO_DUPLICATION` — every join in the workload becomes physically duplicated data. The rule
  computes a **write amplification factor**: how many tables a single logical write must touch, and
  therefore how much application-side dual-write logic this design requires.
- `C08 COLLECTION_TYPE` — a bounded child set never queried independently → a frozen collection or
  UDT on the parent row rather than its own table. Bounded meaning low hundreds; beyond that the
  whole collection must be read and rewritten on every change.
- `C09 SECONDARY_INDEX_REFUSAL` — deliberately **declines** to recommend secondary indexes on
  high-cardinality or frequently-updated columns, and says why: they scatter reads across every node.
  Recommends an additional query table instead, and prices the extra writes.
- `C10 TOMBSTONE_RISK` — workload deletes rows or relies on TTL within a partition → warn about
  tombstone accumulation and recommend time-windowed partitions that can be dropped wholesale.

**Columnar-specific signals:** partition cardinality estimate, partition size estimate
(`rows × row_width`), write amplification factor, and **query coverage** — the fraction of the
workload that has a servable table, which is the primary quality metric for this target.

### 4.3 Graph target (Neo4j) — `G01`–`G09`

**Structure-preserving.** Graph is the closest mapping to the relational source, so the rules spend
their effort on the three places the mapping is genuinely ambiguous: what becomes a node versus a
property, what gets reified, and where traversals enter the graph.

- `G01 ENTITY_NODE` — entity table → node label; its columns → node properties.
- `G02 FK_RELATIONSHIP` — FK → directed relationship, named from the semantics of the FK rather than
  the column name (`orders.customer_id` → `(:Order)-[:PLACED_BY]->(:Customer)`).
- `G03 JUNCTION_DISSOLVES` — pure junction table (PK of exactly two FKs, no payload) → a relationship,
  and **the table disappears entirely**. This is graph's headline win over the relational source and
  should be reported as such.
- `G04 JUNCTION_WITH_PAYLOAD` — junction carrying extra columns → relationship with properties —
  *unless* the payload is filtered or aggregated on in the workload, in which case reify it as an
  intermediate node, because relationship properties can't be indexed as flexibly.
- `G05 LOOKUP_NODE_OR_PROPERTY` — the real judgment call for lookup tables. If the workload ever
  traverses *from* the lookup value ("all products in this category") → node. If the value is only
  ever displayed alongside its parent → flatten to a property and drop the table.
- `G06 REIFY_HYPEREDGE` — a relationship logically connecting three or more entities cannot be a
  binary edge and must become a node. Detected where a table's PK spans three or more FKs.
- `G07 PROPERTY_PLACEMENT` — attributes describing the *association* go on the relationship;
  attributes describing the entity go on the node. Getting this backwards is the most common
  graph-modeling error and is mechanically detectable from which table the column came from.
- `G08 TRAVERSAL_ENTRY_INDEX` — traversals don't need indexes, but the node a traversal *starts* from
  does. Index and constraint recommendations come from the anchor predicates of each query, not from
  the join structure. Easy to overlook precisely because graph databases are advertised as
  join-free.
- `G09 DENSE_NODE` — a node with very high expected degree (a `Country` node joined to millions of
  addresses) makes traversals through it expensive → warn, and recommend relationship-type
  splitting or intermediate grouping nodes.

**Graph-specific signals:** traversal depth per query (longest join chain), estimated node degree per
label, and **traversal ratio** — the share of the workload that actually performs multi-hop joins.

### 4.4 Target fit assessment

Because the three algorithms are so different, the tool should also report **whether the target suits
the workload at all**, rather than dutifully emitting a schema for whatever was requested:

- Graph fit is weak when `traversal_ratio` is low — a workload of single-table filtered lookups gains
  nothing from a graph database, and the report should say so plainly.
- Column-family fit is weak when the workload contains ad-hoc or analytical queries, since every
  access pattern must be known in advance.
- Document fit is weak when the workload is dominated by many-to-many traversal or by aggregations
  spanning most entities.

This assessment costs little — the signals already exist — and it is the difference between a tool
that gives advice and one that only follows orders.

---

## 5. Trade-off report

One entry per decision, in the same shape for all three targets — rule id, signals, gain, cost,
mitigation — so the report format is target-independent even though the reasoning is not.

**Document:**

> **orders → order_items: EMBED** (rule `D03 CONTAINED_1N`, confidence high)
> Signals: 1:N, fan-out ≈ 10–50 bounded, co-access 0.86, standalone access 0.0, write ratio 0.11
> **Gain:** single-document reads for 86% of the workload; atomic order writes without transactions.
> **Cost:** line items cannot be queried independently without `$unwind`; every item edit rewrites
> the order document; per-item indexes become multikey.
> **Mitigation:** if item-level analytics appear later, add a rollup collection rather than
> unembedding.

**Column-family** — note the entry is about a *query*, not a table pair:

> **`orders_by_customer_and_date`: NEW TABLE + BUCKETED PARTITION**
> (rules `C01 QUERY_TABLE`, `C05 UNBOUNDED_PARTITION`, confidence high)
> Serves: `Q4` (weight 0.22) — orders for a customer, most recent first.
> Partition key `(customer_id, order_month)`, clustering `(order_date DESC, order_id)`.
> **Gain:** single-partition read for 22% of the workload; no scatter-gather; predictable latency
> as the table grows.
> **Cost:** order data now lives in 3 tables — a write touches all of them with no cross-table
> atomicity, and the month bucket must be computed by the application on every read.
> Storage ≈ 2.8× the relational footprint.
> **Mitigation:** wrap the triple write in a single repository method; queries spanning a month
> boundary must fan out to two partitions.

**Graph** — note the entry is about a table *disappearing*:

> **`post_tags`: DISSOLVED INTO RELATIONSHIP** (rule `G03 JUNCTION_DISSOLVES`, confidence high)
> `(:Post)-[:TAGGED_AS]->(:Tag)` — the junction table has no counterpart in the target schema.
> **Gain:** the 2-hop join in `Q7` ("posts sharing a tag with post X") becomes a 2-hop traversal
> with no join cost; related-content queries stay flat as the dataset grows.
> **Cost:** nothing to hang association attributes on if the join table later gains columns;
> bulk tag reassignment is a relationship rewrite rather than a single `UPDATE`.
> **Mitigation:** if `post_tags` gains a payload, `G04` reifies it back into a node — plan for
> that migration rather than assuming the edge stays bare.

Plus a schema-level summary, with the target-appropriate headline metric: joins eliminated and
remaining cross-collection reads for **document**; query coverage, table count, write amplification
and storage multiplier for **column-family**; tables dissolved, traversal depth and dense-node
warnings for **graph**. All three also report the consistency risks introduced (which duplicated
fields need fan-out updates and where), the target fit assessment from §4.4, and unmigratable
constructs (triggers, CHECK constraints, views, stored procedures) as an explicit "handle this in
the application layer" list.

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
| **8. Column-family rules + emitter** | `C01`–`C10`, workload-driven table generation, partition/clustering key selection, write-amplification and query-coverage metrics, CQL emitter | Golden decisions per fixture; valid CQL; hotspot, unbounded-partition and unservable-query warnings all fire on purpose-built cases |
| **9. Graph rules + emitter** | `G01`–`G09`, node/relationship model, reification and lookup-placement decisions, traversal-entry indexes, Cypher emitter | Golden decisions per fixture; junction dissolution verified; dense-node and hyperedge cases detected |
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
