"""The decision engine: ordered rules → decisions with full provenance.

Every decision carries the rule that fired, the signals that drove it, the
gains/costs/mitigations, and the near-misses ("why not") — so the trade-off
report writes itself and every embed-vs-reference call is defensible.

Processing order:
  1. junction tables (D07/D08) — their FK edges are subsumed
  2. FK edges, best-parent-first (co-access), first-match-wins
  3. D10 post-pass over embed chains that nest too deep
  4. table dispositions (nothing silently vanishes; the property test enforces it)
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..analyze import Analysis
from ..core.signals import RelationshipStats, TableStats
from .catalog import rule_meta
from .document_rules import EdgeContext, edge_rules, junction_decision, threshold_report

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


@dataclass(frozen=True)
class NearMiss:
    rule_id: str
    rule_name: str
    detail: str  # what was missing, and by how much

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "rule_name": self.rule_name, "detail": self.detail}


@dataclass(frozen=True)
class Decision:
    rule_id: str
    rule_name: str
    action: str  # EMBED | EMBED_SUBSET | REFERENCE | DUPLICATE | FOLD (junction) | KEEP
    subject: str  # "edge:<parent>-><child>" | "junction:<table>" | "table:<name>"
    parent: str | None
    child: str | None
    confidence: str
    signals: dict
    gains: str
    costs: str
    mitigations: str
    near_misses: tuple[NearMiss, ...] = ()

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "action": self.action,
            "subject": self.subject,
            "parent": self.parent,
            "child": self.child,
            "confidence": self.confidence,
            "signals": self.signals,
            "gains": self.gains,
            "costs": self.costs,
            "mitigations": self.mitigations,
            "near_misses": [nm.to_dict() for nm in self.near_misses],
        }


@dataclass
class Disposition:
    table: str
    disposition: str  # collection | embedded | duplicated | folded | dropped
    into: str | None
    rule_id: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "disposition": self.disposition,
            "into": self.into,
            "rule_id": self.rule_id,
            "reason": self.reason,
        }


@dataclass
class DecisionSet:
    decisions: list[Decision] = field(default_factory=list)
    dispositions: dict[str, Disposition] = field(default_factory=dict)

    def embedded_children(self) -> dict[str, str]:
        """child → parent for every EMBED decision (subset embeds excluded)."""
        return {
            d.child: d.parent
            for d in self.decisions
            if d.action in ("EMBED", "FOLD") and d.child and d.parent
        }

    def to_dict(self) -> dict:
        return {
            "decisions": [d.to_dict() for d in self.decisions],
            "dispositions": [self.dispositions[k].to_dict() for k in sorted(self.dispositions)],
        }


def decide(analysis: Analysis) -> DecisionSet:
    result = DecisionSet()

    junction_children = set(analysis.junctions)

    # ---- 1. junctions -----------------------------------------------------
    for jname in sorted(analysis.junctions):
        info = analysis.junctions[jname]
        decision = junction_decision(analysis, jname, info)
        if decision is not None:
            result.decisions.append(decision)

    # ---- 2. FK edges, best-parent-first per child ---------------------------
    edges = [r for r in analysis.relationships if r.child not in junction_children]
    by_child: dict[str, list[RelationshipStats]] = {}
    for r in edges:
        by_child.setdefault(r.child, []).append(r)

    embedded_into: dict[str, str] = {}  # child -> parent (from EMBED decisions)
    for child in sorted(by_child):
        # strongest co-access claim on the child wins embed rights first
        for rel in sorted(by_child[child], key=lambda r: (-r.co_access, r.parent)):
            if rel.parent == rel.child:
                continue  # handled with self-reference edges below
            ctx = EdgeContext(
                analysis=analysis, rel=rel,
                already_embedded_into=embedded_into.get(child),
            )
            fired, near_misses = _evaluate_edge(ctx)
            result.decisions.append(replace(fired, near_misses=near_misses))
            if fired.action == "EMBED" and fired.child not in embedded_into:
                embedded_into[fired.child] = fired.parent

    # self-references (processed after, so they don't steal embed rights)
    for rel in analysis.relationships:
        if rel.parent == rel.child and rel.child not in junction_children:
            ctx = EdgeContext(analysis=analysis, rel=rel, already_embedded_into=None)
            fired, near_misses = _evaluate_edge(ctx)
            result.decisions.append(replace(fired, near_misses=near_misses))

    # ---- 3. D10 deep-nesting post-pass --------------------------------------
    result.decisions.extend(_deep_nesting_flips(result, analysis))

    # ---- 4. dispositions ------------------------------------------------------
    result.dispositions = _dispositions(analysis, result)
    result.decisions.sort(key=lambda d: (d.subject, d.rule_id))
    return result


def _evaluate_edge(ctx: EdgeContext) -> tuple[Decision, tuple[NearMiss, ...]]:
    """Run the ordered rules; first match wins, near-misses collected for the
    'why not?' section. A near-miss is a rule that failed by ≤ 25% on a
    threshold — the rules that *almost* fired are often the interesting ones."""
    fired: Decision | None = None
    near: list[NearMiss] = []
    for rule in edge_rules:
        decision = rule(ctx)
        if decision is not None and fired is None:
            fired = decision
        elif decision is None or decision.rule_id != rule.rule_id:
            report = threshold_report(ctx, rule.rule_id)
            if report:
                near.append(NearMiss(rule_id=rule.rule_id, rule_name=rule_meta(rule.rule_id).name, detail=report))
    if fired is None:  # pragma: no cover — R00 always fires
        fired = R00_DECISION(ctx)
    return fired, tuple(near[:3])  # top three near-misses only


def R00_DECISION(ctx: EdgeContext) -> Decision:  # noqa: N802 - reads like the rule it is
    from .document_rules import default_reference

    return default_reference(ctx)


def _deep_nesting_flips(result: DecisionSet, analysis: Analysis) -> list[Decision]:
    """EMBED chains deeper than three levels get their deepest embed flipped
    to REFERENCE (D10). Positional updates through three nested arrays are
    where document databases stop being fun."""
    flips: list[Decision] = []
    children = result.embedded_children()
    depth_cache: dict[str, int] = {}

    def depth(table: str, seen: frozenset[str] = frozenset()) -> int:
        if table in depth_cache:
            return depth_cache[table]
        if table in seen:  # cycle guard
            return 0
        parent = children.get(table)
        if parent is None:
            return 0
        d = 1 + depth(parent, seen | {table})
        depth_cache[table] = d
        return d

    # a chain a→b→c→d means b,c,d embedded; d sits at depth 3 (a.b.c.d is depth 4)
    for child, parent in sorted(children.items()):
        if depth(child) > 3:
            rel = analysis.rel(parent, child)
            signals = rel.to_dict() if rel else {}
            original = next(
                (d for d in result.decisions if d.child == child and d.parent == parent
                 and d.action == "EMBED"),
                None,
            )
            if original is not None:
                result.decisions.remove(original)
            flips.append(Decision(
                rule_id="D10", rule_name=rule_meta("D10").name, action="REFERENCE",
                subject=f"edge:{parent}->{child}", parent=parent, child=child,
                confidence=CONFIDENCE_HIGH,
                signals=signals,
                gains="Nested-array positional updates stay within the three-level "
                      "practical limit; each level remains independently queryable.",
                costs=f"The {child} → {parent} read now requires a second lookup instead of "
                      "a nested projection.",
                mitigations="If the deep chain is mostly written whole and read whole, "
                            "revisit the intermediate embeds instead of this one.",
                near_misses=(NearMiss("D03", rule_meta("D03").name,
                                      f"{original.rule_id if original else 'EMBED'} would have "
                                      f"embedded {child} into {parent}; depth {depth(child)} > 3"),),
            ))
    return flips


def _dispositions(analysis: Analysis, result: DecisionSet) -> dict[str, Disposition]:
    """Every table gets exactly one disposition. Nothing silently vanishes —
    a table leaves the output only as an explicit, reasoned drop."""
    out: dict[str, Disposition] = {}

    def standalone(table: str) -> float:
        stats: TableStats | None = analysis.tables.get(table)
        return stats.standalone_weight if stats else 0.0

    # junctions with a FOLD decision
    for d in result.decisions:
        if d.action == "FOLD" and d.child:
            out[d.child] = Disposition(
                d.child, "folded", d.parent, d.rule_id,
                f"junction folded into {d.parent} (one-way array)",
            )
            bounded_side = d.signals.get("bounded_side")
            if bounded_side and bounded_side not in out:
                if standalone(bounded_side) > 0:
                    out[bounded_side] = Disposition(
                        bounded_side, "collection", None, d.rule_id,
                        f"bounded junction side: array of refs duplicated into "
                        f"{d.parent}; collection kept (standalone reads exist)",
                    )
                else:
                    out[bounded_side] = Disposition(
                        bounded_side, "dropped", d.parent, d.rule_id,
                        f"bounded junction side with no standalone access: its fields "
                        f"live inside {d.parent} documents only",
                    )

    for table in sorted(analysis.tables):
        if table in out:
            continue
        stats = analysis.tables.get(table)
        if stats and stats.is_junction:
            info = analysis.junctions[table]
            out[table] = Disposition(
                table, "collection", None, "D08",
                f"junction kept as its own collection (sides {info.sides[0]}, {info.sides[1]})",
            )
            continue

        embedded = next(
            (d for d in result.decisions if d.action == "EMBED" and d.child == table), None
        )
        dup_decisions = [d for d in result.decisions if d.action == "DUPLICATE" and d.parent == table]

        if embedded is not None:
            out[table] = Disposition(
                table, "embedded", embedded.parent, embedded.rule_id,
                f"embedded into {embedded.parent} documents",
            )
        elif dup_decisions and standalone(table) == 0 and (stats is None or stats.write_weight == 0):
            targets = sorted({d.child or "?" for d in dup_decisions})
            out[table] = Disposition(
                table, "dropped", targets[0], dup_decisions[0].rule_id,
                f"pure lookup: display fields duplicated into {', '.join(targets)}; "
                f"nothing in the workload reads or writes it standalone",
            )
        elif dup_decisions:
            targets = ", ".join(sorted({d.child or "?" for d in dup_decisions}))
            out[table] = Disposition(
                table, "collection", None, dup_decisions[0].rule_id,
                f"lookup collection kept (standalone reads exist); display fields "
                f"duplicated into {targets}",
            )
        else:
            out[table] = Disposition(table, "collection", None, "T00",
                                     "root or standalone entity: its own collection")
    return out
