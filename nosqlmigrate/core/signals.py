"""Workload-derived signals: the numbers the rules reason over.

Every signal here is an estimate from structure + a user-supplied workload —
never a measurement from a live system. The report says so, and the CLI
accepts an overrides file to correct any of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from .graph import (
    CARDINALITY_ONE_TO_ONE,
    JunctionInfo,
    Relationship,
    is_cap_column,
    is_temporal_growth_column,
)
from .model import RelationalModel
from .types import TypeCategory
from .workload import Workload

#: Lookup heuristics: never written in the workload, no outgoing FKs,
#: referenced at least once, and small (non-PK width ≤ this many bytes).
LOOKUP_MAX_NON_PK_WIDTH = 512
LOOKUP_MAX_COLUMNS = 6


@dataclass
class TableStats:
    table: str
    role: str
    row_width: int
    read_weight: float
    write_weight: float
    write_ratio: float
    standalone_weight: float
    incoming: list[str]
    outgoing: list[str]
    is_lookup: bool
    is_junction: bool
    has_hierarchy: bool

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "row_width": self.row_width,
            "read_weight": self.read_weight,
            "write_weight": self.write_weight,
            "write_ratio": round(self.write_ratio, 3),
            "standalone_weight": self.standalone_weight,
            "incoming": self.incoming,
            "outgoing": self.outgoing,
            "is_lookup": self.is_lookup,
            "is_junction": self.is_junction,
            "has_hierarchy": self.has_hierarchy,
        }


@dataclass
class RelationshipStats:
    parent: str
    child: str
    cardinality: str
    fk_name: str | None
    on_delete: str | None

    #: weighted fraction of parent-touching queries that also touch the child
    co_access: float
    #: weighted fraction of child-touching queries that do NOT touch the parent
    child_standalone: float
    write_ratio_child: float
    write_ratio_parent: float

    fanout_min: int
    fanout_max: int | None  # None = unbounded
    unbounded: bool
    #: fanout_mid × child row width, in bytes (heuristic)
    est_embedded_size: int

    parent_is_lookup: bool
    parent_shared: bool  # referenced by more than one other table

    @property
    def fanout_mid(self) -> int:
        if self.fanout_max is None:
            return 1000  # conservative stand-in for "no natural bound"
        return max(1, (self.fanout_min + self.fanout_max) // 2)

    def to_dict(self) -> dict:
        return {
            "parent": self.parent,
            "child": self.child,
            "cardinality": self.cardinality,
            "fk_name": self.fk_name,
            "on_delete": self.on_delete,
            "co_access": round(self.co_access, 3),
            "child_standalone": round(self.child_standalone, 3),
            "write_ratio_child": round(self.write_ratio_child, 3),
            "write_ratio_parent": round(self.write_ratio_parent, 3),
            "fanout_min": self.fanout_min,
            "fanout_max": self.fanout_max,
            "unbounded": self.unbounded,
            "est_embedded_size": self.est_embedded_size,
            "parent_is_lookup": self.parent_is_lookup,
            "parent_shared": self.parent_shared,
        }


def classify_roles(
    model: RelationalModel,
    workload: Workload,
    junctions: dict[str, JunctionInfo],
) -> dict[str, TableStats]:
    """Role classification + per-table workload stats."""
    from . import graph as g

    stats: dict[str, TableStats] = {}
    for name, table in model.tables.items():
        read_w = workload.weight_touching(name) - workload.write_weight_touching(name)
        write_w = workload.write_weight_touching(name)
        total_w = read_w + write_w
        standalone = sum(q.weight for q in workload.queries if q.tables == (name,))
        incoming = g.referenced_by(model, name)
        outgoing = g.references_of(model, name)

        is_junction = name in junctions
        non_pk_cols = [c for c in table.columns if table.pk is None or c.canonical not in table.pk.columns]
        non_pk_width = sum(c.width for c in non_pk_cols)
        is_lookup = (
            not is_junction
            and len(outgoing) == 0
            and len(incoming) >= 1
            and write_w == 0
            and non_pk_width <= LOOKUP_MAX_NON_PK_WIDTH
            and len(table.columns) <= LOOKUP_MAX_COLUMNS
        )

        # weak entity: every PK column is an FK source column
        owned = (
            table.pk is not None
            and not is_junction
            and table.pk.columns
            and all(
                any(c in fk.source_columns for fk in table.foreign_keys)
                for c in table.pk.columns
            )
        )
        hierarchy = g.has_self_reference(model, name)

        if is_junction:
            role = "junction"
        elif is_lookup:
            role = "lookup"
        elif owned:
            role = "owned"
        elif incoming and outgoing:
            role = "hub"
        elif outgoing:
            role = "child"
        elif incoming:
            role = "parent"
        else:
            role = "isolated"

        stats[name] = TableStats(
            table=name,
            role=role,
            row_width=table.row_width(),
            read_weight=read_w,
            write_weight=write_w,
            write_ratio=(write_w / total_w) if total_w > 0 else 0.0,
            standalone_weight=standalone,
            incoming=incoming,
            outgoing=outgoing,
            is_lookup=is_lookup,
            is_junction=is_junction,
            has_hierarchy=hierarchy,
        )
    return stats


def _read_weight(workload: Workload, table: str) -> float:
    return sum(q.weight for q in workload.queries if not q.is_write and table in q.tables)


def _read_weight_both(workload: Workload, a: str, b: str) -> float:
    return sum(
        q.weight
        for q in workload.queries
        if not q.is_write and a in q.tables and b in q.tables
    )


def relationship_stats(
    model: RelationalModel,
    workload: Workload,
    rels: list[Relationship],
    cardinalities: dict[tuple[str, str], str],
    table_stats: dict[str, TableStats],
) -> list[RelationshipStats]:
    out: list[RelationshipStats] = []
    for rel in rels:
        card = cardinalities[rel.key]
        child_table = model.table(rel.child)
        parent_stats = table_stats.get(rel.parent)
        child_stats = table_stats.get(rel.child)

        w_parent = _read_weight(workload, rel.parent)
        w_both = _read_weight_both(workload, rel.parent, rel.child)
        w_child = workload.weight_touching(rel.child)
        w_child_solo = workload.weight_touching_without(rel.child, rel.parent)

        # co-access is read-weighted: embedding exists to serve reads, and a
        # parent's INSERTs shouldn't count as "co-access" with the child.
        co_access = (w_both / w_parent) if w_parent > 0 else 0.0
        standalone = (w_child_solo / w_child) if w_child > 0 else 0.0

        # fan-out inference (structural, not measured)
        if card == CARDINALITY_ONE_TO_ONE:
            fanout_min, fanout_max, unbounded = 1, 1, False
        else:
            temporal = child_table is not None and any(
                c.category is TypeCategory.TEMPORAL and is_temporal_growth_column(c.canonical)
                for c in child_table.columns
            )
            # cap detection ignores FK source columns: loans.copy_no identifies
            # the target copy, it does not cap loans-per-copy
            child_fk_cols = {
                col for fk in child_table.foreign_keys for col in fk.source_columns
            } if child_table is not None else set()
            capped = child_table is not None and any(
                is_cap_column(c.canonical)
                for c in child_table.columns
                if c.canonical not in child_fk_cols
            )
            unbounded = temporal and not capped
            if unbounded:
                fanout_min, fanout_max = 50, None
            elif capped:
                fanout_min, fanout_max = 1, 100
            else:
                fanout_min, fanout_max = 1, 20

        fanout_mid = max(1, (fanout_min + (fanout_max if fanout_max is not None else 1000)) // 2)
        child_width = child_stats.row_width if child_stats else 0

        out.append(RelationshipStats(
            parent=rel.parent,
            child=rel.child,
            cardinality=card,
            fk_name=rel.fk.name,
            on_delete=rel.fk.on_delete,
            co_access=co_access,
            child_standalone=standalone,
            write_ratio_child=child_stats.write_ratio if child_stats else 0.0,
            write_ratio_parent=parent_stats.write_ratio if parent_stats else 0.0,
            fanout_min=fanout_min,
            fanout_max=fanout_max,
            unbounded=unbounded,
            est_embedded_size=fanout_mid * child_width,
            parent_is_lookup=parent_stats.is_lookup if parent_stats else False,
            parent_shared=len(parent_stats.incoming) > 1 if parent_stats else False,
        ))
    return out
