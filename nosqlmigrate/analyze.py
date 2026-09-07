"""The analysis: model + workload → the signal set every target reasons over.

This is the phase-3 deliverable. `Analysis.to_dict()` is snapshotted as
tests/fixtures/*/expected/signals.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import graph as g
from .core.model import RelationalModel
from .core.signals import RelationshipStats, TableStats, classify_roles, relationship_stats
from .core.workload import Workload


@dataclass
class Analysis:
    model: RelationalModel
    workload: Workload
    tables: dict[str, TableStats] = field(default_factory=dict)
    relationships: list[RelationshipStats] = field(default_factory=list)
    junctions: dict[str, g.JunctionInfo] = field(default_factory=dict)

    def rel(self, parent: str, child: str) -> RelationshipStats | None:
        for r in self.relationships:
            if r.parent == parent and r.child == child:
                return r
        return None

    def to_dict(self) -> dict:
        return {
            "tables": {name: s.to_dict() for name, s in sorted(self.tables.items())},
            "relationships": [r.to_dict() for r in self.relationships],
            "junctions": {
                name: {"sides": list(j.sides), "has_payload": j.has_payload}
                for name, j in sorted(self.junctions.items())
            },
        }


def analyze(model: RelationalModel, workload: Workload) -> Analysis:
    rels = g.relationships(model)
    cardinalities = {rel.key: g.cardinality(model, rel) for rel in rels}
    junctions = g.detect_junctions(model)
    table_stats = classify_roles(model, workload, junctions)
    rel_stats = relationship_stats(model, workload, rels, cardinalities, table_stats)
    return Analysis(
        model=model,
        workload=workload,
        tables=table_stats,
        relationships=rel_stats,
        junctions=junctions,
    )
