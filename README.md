# Relational → NoSQL Schema Migration Tool

Takes a normalized SQL schema and a set of sample queries, and proposes a denormalized NoSQL schema —
explaining every trade-off it made along the way.

> **Status: design stage.** The architecture and rule set are specified in [PLAN.md](PLAN.md);
> implementation has not started. Nothing here runs yet.

---

## The idea

Flattening a relational schema is easy. Justifying the result is not.

Given `orders` and `order_items`, should the items be embedded in the order document or kept in
their own collection? The answer depends on things a schema alone doesn't say: how often the two are
read together, whether items are ever queried on their own, how many items an order typically has,
how often they're edited after the fact.

This tool reads those signals out of the query workload and applies an explicit, ordered rule set.
Every decision it makes cites the rule that produced it, the signals that triggered that rule, what
the decision buys you, what it costs you, and how to mitigate the cost.

The proposed schema is the output. The **rationale** is the point.

---

## How it works

```
SQL DDL + sample queries
         │
         ▼
   parse (sqlglot)  ──►  relational model  +  workload of access patterns
         │
         ▼
   analyze          ──►  FK graph · cardinality inference · per-edge signals
         │
         ▼
   decide           ──►  ordered rule engine → EMBED / REFERENCE / DUPLICATE / SPLIT
         │
         ▼
   emit + report    ──►  target schema · indexes · trade-off narrative
```

**Cardinality is inferred structurally**, since there's no live database to sample: a `UNIQUE`
constraint on a foreign key means 1:1, a plain foreign key means 1:N, and a primary key made of
exactly two foreign keys with no payload columns is a junction table.

**Signals** driving each decision include co-access frequency across the weighted workload, whether
the child entity is ever queried standalone, write pressure, estimated fan-out, row width, whether
the child is shared between parents, and unbounded-growth risk.

---

## Targets

| Target | Approach |
|---|---|
| **MongoDB** (document) | Embed-vs-reference decisions over the FK graph; `$jsonSchema` validators, example documents, index plan |
| **Cassandra** (column-family) | Query-first: one table per access pattern, keys derived from predicates, hotspot and unbounded-partition warnings, CQL output |
| **Neo4j** (graph) | Foreign keys become relationships, junction tables with payloads become relationships with properties; Cypher constraints and indexes |

Only the document-target decisions transfer between these — column-family modeling in particular is
driven by the query set rather than the schema graph, and is a substantially different algorithm.

---

## What the output looks like

A decision in the trade-off report reads roughly like this:

> **`orders` → `order_items`: EMBED** — rule `D03 CONTAINED_1N`, confidence high
>
> **Signals:** 1:N · fan-out ≈ 10–50, bounded · co-access 0.86 · standalone access 0.0 · write ratio 0.11
>
> **Gain:** single-document reads for 86% of the workload; atomic order writes without transactions.
>
> **Cost:** line items can't be queried independently without `$unwind`; every item edit rewrites the
> whole order document; per-item indexes become multikey.
>
> **Mitigation:** if item-level analytics show up later, add a rollup collection rather than
> unembedding.

Alongside the per-decision entries, the report summarizes joins eliminated, queries now served by a
single lookup, remaining cross-collection reads, which duplicated fields will need fan-out updates,
and which SQL constructs couldn't be migrated at all.

---

## Planned interface

A FastAPI service with a two-pane web UI — paste DDL and queries on the left, get the ER graph,
proposed schema, and expandable rationale cards on the right — plus a CLI for scripted use:

```
nosqlmigrate analyze schema.sql --queries workload.sql --target document -o out/
```

---

## Roadmap

The build is sequenced in 11 phases in [PLAN.md](PLAN.md). Phases 1–7 produce a complete working
tool for the document target; the column-family and graph targets and the web UI follow.

## Limits

These are design constraints, not temporary gaps:

- Cardinality and fan-out are **inferred from structure, not measured**. Users can override any
  estimate and re-run.
- Query weights are supplied by the user. Bad weights produce bad recommendations.
- Views, triggers, stored procedures, and `CHECK` constraints are reported as unmigratable rather
  than translated.
- The output is a **proposal**, meant to read like a design review — not a migration script to run
  unattended.
