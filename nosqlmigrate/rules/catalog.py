"""The rule catalog — single source of truth.

The web UI's rule-catalog page, the trade-off report's citations, and the
engine's rule metadata all read from here. Nothing hand-maintains a second
copy anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuleMeta:
    id: str
    name: str
    action: str
    target: str  # "document" | "columnar" | "graph"
    summary: str
    rationale: str


CATALOG: dict[str, RuleMeta] = {
    meta.id: meta
    for meta in [
        # ---- document-model rules (edges) --------------------------------
        RuleMeta(
            id="D01", name="UNBOUNDED_CHILD", action="REFERENCE", target="document",
            summary="Child grows without bound (temporal column, no natural cap).",
            rationale="Embedding invites the 16 MB document ceiling and rewrites the whole "
                      "parent document on every append. Keep the child in its own collection "
                      "and let the parent's id be the access key.",
        ),
        RuleMeta(
            id="D02", name="SIZE_RISK", action="REFERENCE / EMBED_SUBSET", target="document",
            summary="Estimated embedded size (fan-out × row width) exceeds safe thresholds.",
            rationale="Above ~512 KB embedded content per document, reads and writes degrade "
                      "and the 16 MB ceiling comes into play. Between 64 KB and 512 KB, embed "
                      "only the most recent N children and keep the full history in its own "
                      "collection (the extended pattern / split).",
        ),
        RuleMeta(
            id="D03", name="CONTAINED_1N", action="EMBED", target="document",
            summary="1:N, bounded fan-out, high co-access, child never read standalone, low write pressure.",
            rationale="The classic embed case: the parent's document is the unit of work, and "
                      "embedding gives single-document reads and atomic writes without "
                      "transactions.",
        ),
        RuleMeta(
            id="D04", name="ONE_TO_ONE_COACCESSED", action="EMBED", target="document",
            summary="1:1 relationship (unique FK) with meaningful co-access.",
            rationale="Two documents that are always read together are one document. Collapse "
                      "the child table entirely.",
        ),
        RuleMeta(
            id="D05", name="SMALL_LOOKUP", action="DUPLICATE", target="document",
            summary="Small, read-only lookup table referenced via FK.",
            rationale="Extended-reference pattern: duplicate the display fields into the "
                      "referencing document and keep the code as the join key. The lookup's "
                      "own collection is kept only if anything reads it standalone.",
        ),
        RuleMeta(
            id="D06", name="SHARED_MUTABLE_PARENT", action="REFERENCE", target="document",
            summary="Parent entity is referenced by several tables and is updated in the workload.",
            rationale="Duplicating shared, changing data into referencing documents means "
                      "N-way fan-out updates on every change. Keep one canonical copy and "
                      "reference it by id.",
        ),
        RuleMeta(
            id="D07", name="JUNCTION_SMALL_SIDE", action="EMBED (ids)", target="document",
            summary="M:N junction with one bounded (lookup-like) side.",
            rationale="Embed an array of the bounded side's keys — plus display fields when "
                      "that side is a tiny lookup — into the unbounded side's documents. "
                      "One-way only: dual arrays invite consistency drift.",
        ),
        RuleMeta(
            id="D08", name="JUNCTION_LARGE", action="COLLECTION", target="document",
            summary="M:N junction where both sides are unbounded (or the junction carries payload).",
            rationale="Keep the junction as its own collection (or relationship in graph "
                      "targets). Neither side can hold the array without unbounded growth.",
        ),
        RuleMeta(
            id="D09", name="CHILD_STANDALONE", action="REFERENCE", target="document",
            summary="Child is heavily accessed without its parent.",
            rationale="An embedded child is only reachable through $unwind. If half the "
                      "workload reaches the child directly, give it its own collection and "
                      "keep the parent id as the access key.",
        ),
        RuleMeta(
            id="D10", name="DEEP_NESTING", action="REFERENCE", target="document",
            summary="Embedding chain would exceed three levels.",
            rationale="Nested arrays beyond three levels hit positional-update limits — "
                      "updating a.b.c.d requires matching through three array levels. Break "
                      "the chain at the deepest embed.",
        ),
        # ---- structural fallbacks -----------------------------------------
        RuleMeta(
            id="S01", name="SELF_REFERENCE", action="REFERENCE", target="document",
            summary="Self-referencing foreign key (adjacency list).",
            rationale="Store parent_id and keep the hierarchy as an adjacency list; "
                      "materialized paths or closure tables are an application-layer choice "
                      "the report flags rather than makes.",
        ),
        RuleMeta(
            id="R00", name="DEFAULT_REFERENCE", action="REFERENCE", target="document",
            summary="No embed/duplicate signal strong enough; or child already embedded elsewhere.",
            rationale="When in doubt, reference. References are always correct and rarely "
                      "optimal; embedding is occasionally optimal and sometimes catastrophic. "
                      "The near-miss list shows which rule almost fired and by what margin.",
        ),
        # ---- column-family rules -------------------------------------------
        RuleMeta(
            id="C01", name="QUERY_FIRST_TABLE", action="CQL TABLE", target="columnar",
            summary="One Cassandra table per access pattern, keyed by its predicates.",
            rationale="Cassandra has no joins: each read query becomes its own table, "
                      "partitioned by the equality predicates and clustered by the sort/range "
                      "columns. Denormalization is the cost, paid at write time.",
        ),
        RuleMeta(
            id="C02", name="HOT_PARTITION", action="WARNING", target="columnar",
            summary="Low-cardinality partition key concentrates all traffic on few nodes.",
            rationale="Partition keys like status or boolean flags create hotspots; prefer a "
                      "high-cardinality key or bucket the low-cardinality one.",
        ),
        RuleMeta(
            id="C03", name="UNBOUNDED_PARTITION", action="WARNING", target="columnar",
            summary="Partition grows forever (e.g. time-series rows under one partition key).",
            rationale="Unbounded partitions eventually exceed practical limits; introduce a "
                      "time bucket into the partition key.",
        ),
        # ---- graph rules ----------------------------------------------------
        RuleMeta(
            id="G01", name="FK_TO_RELATIONSHIP", action="RELATIONSHIP", target="graph",
            summary="Foreign key → relationship; junction → relationship with properties.",
            rationale="The relational FK graph maps directly: tables become nodes (or "
                      "properties for never-traversed lookups), FKs become relationships.",
        ),
        RuleMeta(
            id="G02", name="LOOKUP_PLACEMENT", action="NODE / PROPERTY", target="graph",
            summary="Lookup table becomes a node label when traversed, an inline property when not.",
            rationale="Labels earn their keep when queries traverse them; otherwise the "
                      "lookup's display value rides along as a property on the referencing "
                      "node.",
        ),
    ]
}


def rule_meta(rule_id: str) -> RuleMeta:
    return CATALOG[rule_id]
