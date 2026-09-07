"""Document-model rules: embed vs reference, with the reasoning attached.

Each rule is a function (EdgeContext) → Decision | None. The engine runs them
in order, first match wins. `threshold_report` powers the "why not?" section:
for a rule that didn't fire, it says what was missing and by how much.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..analyze import Analysis
from ..core.graph import JunctionInfo
from ..core.signals import RelationshipStats

# ---- thresholds (documented in the report; overridable via CLI) -----------
THRESHOLDS = {
    "D02_soft_bytes": 64 * 1024,   # above: EMBED_SUBSET instead of plain EMBED
    "D02_hard_bytes": 512 * 1024,  # above: REFERENCE outright
    "D03_co_access": 0.60,
    "D03_max_standalone": 0.40,
    "D03_max_write_ratio": 0.40,
    "D04_co_access": 0.50,
    "D09_standalone": 0.50,
    "near_miss_margin": 0.25,      # within 25% of a threshold counts as "almost"
}

HIGH = "high"
MEDIUM = "medium"


def rule(rule_id: str):
    """Attach the catalog id to a rule function."""
    def deco(fn):
        fn.rule_id = rule_id
        return fn
    return deco


@dataclass
class EdgeContext:
    analysis: Analysis
    rel: RelationshipStats
    already_embedded_into: str | None = None


def _edge_decision(ctx: EdgeContext, rule_id: str, action: str, confidence: str,
                   gains: str, costs: str, mitigations: str,
                   extra_signals: dict | None = None) -> Decision:  # noqa: F821
    from .engine import Decision

    signals = ctx.rel.to_dict()
    if extra_signals:
        signals.update(extra_signals)
    return Decision(
        rule_id=rule_id,
        rule_name=_name(rule_id),
        action=action,
        subject=f"edge:{ctx.rel.parent}->{ctx.rel.child}",
        parent=ctx.rel.parent,
        child=ctx.rel.child,
        confidence=confidence,
        signals=signals,
        gains=gains,
        costs=costs,
        mitigations=mitigations,
    )


def _name(rule_id: str) -> str:
    from .catalog import rule_meta

    return rule_meta(rule_id).name


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# --------------------------------------------------------------------------- rules


@rule("S01")
def rule_s01_self_reference(ctx: EdgeContext):
    if ctx.rel.parent != ctx.rel.child:
        return None
    return _edge_decision(
        ctx, "S01", "REFERENCE", HIGH,
        gains="Adjacency lists are index-friendly, handle arbitrary depth, and match "
              "how comment threads and org charts are actually queried.",
        costs="Recursive traversal (whole subtrees) needs recursive CTEs in SQL and "
              "$graphLookup in MongoDB — neither is free.",
        mitigations="If whole-subtree reads dominate, maintain a materialized path field "
                    "(path '/a/b/c') as a derived, repairable index.",
        extra_signals={"self_reference": True},
    )


@rule("R00")
def rule_already_embedded(ctx: EdgeContext):
    """The child already lives inside another parent's documents; this edge
    keeps its FK as a plain reference field inside that embedded doc."""
    if ctx.already_embedded_into is None or ctx.already_embedded_into == ctx.rel.parent:
        return None
    return _edge_decision(
        ctx, "R00", "REFERENCE", HIGH,
        gains=f"{ctx.rel.child} is already embedded under '{ctx.already_embedded_into}'; "
              "the foreign-key field rides along inside the embedded documents with "
              "no extra lookup.",
        costs="The referenced parent cannot be reached by a covered query on the "
              "embedded child alone.",
        mitigations="None needed — this is the standard shape for embedded sub-documents "
                    "that reference other collections.",
        extra_signals={"embedded_under": ctx.already_embedded_into},
    )


@rule("D01")
def rule_d01_unbounded(ctx: EdgeContext):
    rel = ctx.rel
    if not rel.unbounded:
        return None
    confidence = HIGH if (rel.write_ratio_child > 0.3 or rel.child_standalone > 0.5) else MEDIUM
    return _edge_decision(
        ctx, "D01", "REFERENCE", confidence,
        gains=f"{rel.child} grows without bound (temporal column, no natural cap); "
              f"keeping it separate avoids the 16 MB document ceiling and whole-document "
              f"rewrites on every append.",
        costs="Reading a parent with its children needs a second query (or $lookup) "
              "instead of one document fetch.",
        mitigations="If recent-children reads matter, add a capped 'recent' array on the "
                    "parent and treat it as a cache, not the system of record.",
    )


@rule("D02")
def rule_d02_size_risk(ctx: EdgeContext):
    rel = ctx.rel
    if rel.unbounded or rel.cardinality == "1:1":
        return None
    size = rel.est_embedded_size
    if size <= THRESHOLDS["D02_soft_bytes"]:
        return None
    if size > THRESHOLDS["D02_hard_bytes"]:
        return _edge_decision(
            ctx, "D02", "REFERENCE", MEDIUM,
            gains=f"Estimated embedded content {_fmt_bytes(size)} per parent document "
                  f"(fan-out ≈ {rel.fanout_mid} × child row width) would dominate "
                  f"document size.",
            costs="Two-collection reads for co-accessed data.",
            mitigations="Revisit if the workload's real fan-out is much lower than the "
                        "structural estimate; override the estimate and re-run.",
        )
    # soft zone: embed the most recent N, keep the rest in its own collection
    return _edge_decision(
        ctx, "D02", "EMBED_SUBSET", MEDIUM,
        gains="Hot reads get the most recent children in-document; the full history "
              "stays in a dedicated collection.",
        costs="The split is an application-level invariant: every append must write "
              "both the child collection and (when recent) the parent's capped array.",
        mitigations="Cap the array (e.g. last 20) and rebuild it from the child "
                    "collection if it drifts.",
        extra_signals={"est_embedded_size": size, "soft_limit": THRESHOLDS["D02_soft_bytes"]},
    )


@rule("D03")
def rule_d03_contained_1n(ctx: EdgeContext):
    rel = ctx.rel
    if rel.cardinality != "1:N" or rel.unbounded:
        return None
    if not (rel.co_access >= THRESHOLDS["D03_co_access"]
            and rel.child_standalone <= THRESHOLDS["D03_max_standalone"]
            and rel.write_ratio_child <= THRESHOLDS["D03_max_write_ratio"]):
        return None
    comfortable = (
        rel.co_access >= 0.65
        and rel.child_standalone <= 0.2
        and rel.write_ratio_child <= 0.2
    )
    return _edge_decision(
        ctx, "D03", "EMBED", HIGH if comfortable else MEDIUM,
        gains=f"{int(rel.co_access * 100)}% of {rel.parent} reads also need "
              f"{rel.child}; embedding serves them with a single document fetch and "
              f"makes parent+children writes atomic without transactions.",
        costs=f"{rel.child} can no longer be queried independently without $unwind; "
              f"every child edit rewrites the parent document; child-level indexes "
              f"become multikey.",
        mitigations="If child-level analytics appear later, add a rollup collection "
                    "rather than un-embedding.",
    )


@rule("D04")
def rule_d04_one_to_one(ctx: EdgeContext):
    rel = ctx.rel
    if rel.cardinality != "1:1":
        return None
    if rel.co_access < THRESHOLDS["D04_co_access"]:
        return None
    return _edge_decision(
        ctx, "D04", "EMBED", HIGH if rel.co_access >= 0.55 else MEDIUM,
        gains=f"1:1 via a unique foreign key and {int(rel.co_access * 100)}% co-access: "
              f"two tables that are always read together become one document.",
        costs=f"{rel.child} loses its own collection; anything that queried it "
              f"standalone (currently {rel.child_standalone:.0%}) must go through "
              f"{rel.parent}.",
        mitigations="Keep the child's unique key as an indexed field inside the parent "
                    "document.",
    )


@rule("D05")
def rule_d05_small_lookup(ctx: EdgeContext):
    rel = ctx.rel
    if not rel.parent_is_lookup:
        return None
    parent_table = ctx.analysis.model.table(rel.parent)
    display_fields = [c.canonical for c in parent_table.columns
                      if parent_table.pk is None or c.canonical not in parent_table.pk.columns] \
        if parent_table is not None else []
    standalone = ctx.analysis.tables.get(rel.parent).standalone_weight \
        if rel.parent in ctx.analysis.tables else 0.0
    return _edge_decision(
        ctx, "D05", "DUPLICATE", HIGH,
        gains=f"{rel.parent} is a small, read-only lookup: duplicating "
              f"({', '.join(display_fields) or 'display fields'}) into {rel.child} "
              f"documents removes the join from hot reads while the code field "
              f"remains the join key for repairs.",
        costs=f"Renaming a {rel.parent} value means a fan-out update across every "
              f"{rel.child} document that carries it.",
        mitigations="Lookups like this rarely change; when they do, batch-repair by "
                    "the join key. The lookup collection "
                    + ("is kept — something reads it standalone." if standalone > 0
                       else "can be dropped: nothing in the workload reads it alone."),
        extra_signals={
            "duplicated_fields": display_fields,
            "parent_standalone_weight": standalone,
            "keep_parent_collection": standalone > 0,
        },
    )


@rule("D09")
def rule_d09_child_standalone(ctx: EdgeContext):
    rel = ctx.rel
    if rel.child_standalone < THRESHOLDS["D09_standalone"]:
        return None
    return _edge_decision(
        ctx, "D09", "REFERENCE", HIGH if rel.child_standalone >= 0.7 else MEDIUM,
        gains=f"{int(rel.child_standalone * 100)}% of {rel.child} access happens "
              f"without {rel.parent} — an embedded copy would force $unwind on more "
              f"than half the workload.",
        costs=f"The {int(rel.co_access * 100)}% of reads that want both need a second "
              f"lookup or $lookup stage.",
        mitigations="If those joined reads are latency-critical, duplicate a small "
                    "summary of the parent into the child (reads must dominate writes).",
    )


@rule("D06")
def rule_d06_shared_mutable(ctx: EdgeContext):
    rel = ctx.rel
    if not (rel.parent_shared and rel.write_ratio_parent > 0):
        return None
    return _edge_decision(
        ctx, "D06", "REFERENCE", MEDIUM,
        gains=f"{rel.parent} is referenced by {len(ctx.analysis.tables[rel.parent].incoming) if rel.parent in ctx.analysis.tables else 'multiple'} "
              f"tables and is updated by the workload; referencing it by id keeps "
              f"one canonical copy and avoids N-way fan-out updates.",
        costs="Every consumer pays a join/$lookup to render the parent's fields.",
        mitigations="If some consumers can tolerate staleness, cache the parent's "
                    "display fields with a TTL rather than duplicating them.",
    )


@rule("R00")
def rule_r00_default(ctx: EdgeContext):
    return _edge_decision(
        ctx, "R00", "REFERENCE", MEDIUM,
        gains="References are always correct: no size ceiling, no rewrite blast "
              "radius, children stay independently queryable.",
        costs="Co-accessed reads pay a second lookup; parent+child writes are not "
              "atomic without transactions.",
        mitigations="See the near-misses — this is the fallback, and the interesting "
                    "question is which rule *almost* fired.",
    )


edge_rules = [
    rule_s01_self_reference,
    rule_already_embedded,
    rule_d05_small_lookup,      # parent-side: tiny immutable lookup → duplicate, always safe
    rule_d01_unbounded,
    rule_d02_size_risk,
    rule_d03_contained_1n,
    rule_d04_one_to_one,
    rule_d09_child_standalone,
    rule_d06_shared_mutable,    # parent-side: shared + mutable → reference, explained
    rule_r00_default,
]


# --------------------------------------------------------------------------- junctions


def junction_decision(analysis: Analysis, name: str, info: JunctionInfo):
    from .engine import Decision

    side_stats = {s: analysis.tables.get(s) for s in info.sides}
    bounded = [s for s in info.sides if side_stats.get(s) and side_stats[s].is_lookup]

    if info.has_payload and not bounded:
        return Decision(
            rule_id="D08", rule_name=_name("D08"), action="KEEP",
            subject=f"junction:{name}", parent=None, child=name, confidence=HIGH,
            signals={"sides": list(info.sides), "has_payload": True},
            gains="The junction carries association attributes; keeping it as its own "
                  "collection preserves them without polluting either side.",
            costs="M:N traversals need a two-hop lookup through the junction collection.",
            mitigations="Denormalize counts onto the sides if traversal speed matters.",
        )

    if not bounded:
        return Decision(
            rule_id="D08", rule_name=_name("D08"), action="KEEP",
            subject=f"junction:{name}", parent=None, child=name, confidence=HIGH,
            signals={"sides": list(info.sides), "has_payload": info.has_payload},
            gains=f"Both sides ({', '.join(info.sides)}) are unbounded entities; neither "
                  f"can hold the other's ids without unbounded array growth.",
            costs="M:N traversals need a two-hop lookup through the junction collection.",
            mitigations="If one side later proves bounded in practice, override its "
                        "fan-out estimate and re-run.",
        )

    # D07: exactly one bounded side (both bounded → smaller row width wins)
    bounded_side = min(bounded, key=lambda s: side_stats[s].row_width if side_stats[s] else 0)
    host = next(s for s in info.sides if s != bounded_side)
    bounded_table = analysis.model.table(bounded_side)
    display = [c.canonical for c in bounded_table.columns
               if bounded_table.pk is None or c.canonical not in bounded_table.pk.columns] \
        if bounded_table is not None else []
    standalone = side_stats[bounded_side].standalone_weight if side_stats[bounded_side] else 0.0

    return Decision(
        rule_id="D07", rule_name=_name("D07"), action="FOLD",
        subject=f"junction:{name}", parent=host, child=name, confidence=HIGH,
        signals={
            "sides": list(info.sides),
            "bounded_side": bounded_side,
            "host": host,
            "duplicated_fields": display,
            "keep_bounded_collection": standalone > 0,
        },
        gains=f"{bounded_side} is a bounded vocabulary: an array of "
              f"{'ids + ' + ', '.join(display) + ' ' if display else 'ids'}on each "
              f"{host} document answers the co-accessed reads in one fetch, one-way "
              f"(no dual arrays to keep consistent).",
        costs=f"Removing a {bounded_side} from the vocabulary means updating every "
              f"host document that carries it; the array index is multikey.",
        mitigations="Vocabulary changes are rare; repair by scanning the array field "
                    "when they happen.",
    )


# --------------------------------------------------------------------------- near-miss reports


def threshold_report(ctx: EdgeContext, rule_id: str) -> str | None:
    """For a rule that did NOT fire: a one-line 'what was missing', when it was
    close (within the near-miss margin). Distant rules stay silent."""
    rel = ctx.rel
    m = THRESHOLDS["near_miss_margin"]
    if rule_id == "D01":
        if rel.fanout_max is not None and rel.fanout_max >= 100:
            return f"fan-out bounded at ~{rel.fanout_max} (capped column) — large but finite"
        return None
    if rule_id == "D02":
        soft = THRESHOLDS["D02_soft_bytes"]
        if soft * (1 - m) < rel.est_embedded_size <= soft:
            return f"estimated embedded size {_fmt_bytes(rel.est_embedded_size)} within " \
                   f"25% of the {_fmt_bytes(soft)} soft limit"
        return None
    if rule_id == "D03":
        misses = []
        if rel.cardinality != "1:N":
            return None
        if rel.unbounded:
            return None
        if THRESHOLDS["D03_co_access"] * (1 - m) <= rel.co_access < THRESHOLDS["D03_co_access"]:
            misses.append(f"co-access {rel.co_access:.2f} just under "
                          f"{THRESHOLDS['D03_co_access']:.2f}")
        if THRESHOLDS["D03_max_standalone"] < rel.child_standalone <= THRESHOLDS["D03_max_standalone"] * (1 + m):
            misses.append(f"child standalone {rel.child_standalone:.2f} just over "
                          f"{THRESHOLDS['D03_max_standalone']:.2f}")
        if THRESHOLDS["D03_max_write_ratio"] < rel.write_ratio_child <= THRESHOLDS["D03_max_write_ratio"] * (1 + m):
            misses.append(f"child write ratio {rel.write_ratio_child:.2f} just over "
                          f"{THRESHOLDS['D03_max_write_ratio']:.2f}")
        return "; ".join(misses) or None
    if rule_id == "D04":
        if rel.cardinality == "1:1" and THRESHOLDS["D04_co_access"] * (1 - m) <= rel.co_access < THRESHOLDS["D04_co_access"]:
            return f"co-access {rel.co_access:.2f} just under {THRESHOLDS['D04_co_access']:.2f}"
        return None
    if rule_id == "D09":
        if THRESHOLDS["D09_standalone"] * (1 - m) <= rel.child_standalone < THRESHOLDS["D09_standalone"]:
            return f"child standalone {rel.child_standalone:.2f} just under " \
                   f"{THRESHOLDS['D09_standalone']:.2f}"
        return None
    return None


def default_reference(ctx: EdgeContext):
    return rule_r00_default(ctx)
