"""The FK graph: relationships, cardinality inference, junction detection.

Pure structure — no workload inputs here. Everything in this module is
derivable from the DDL alone.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import ForeignKey, RelationalModel, Table

CARDINALITY_ONE_TO_ONE = "1:1"
CARDINALITY_ONE_TO_MANY = "1:N"


@dataclass(frozen=True)
class Relationship:
    """One FK edge, oriented parent (referenced) → child (referencing)."""

    parent: str
    child: str
    fk: ForeignKey

    @property
    def key(self) -> tuple[str, str]:
        return (self.parent, self.child)


@dataclass(frozen=True)
class JunctionInfo:
    """A junction table: PK is exactly two FK column sets."""

    table: str
    sides: tuple[str, str]  # the two referenced tables
    has_payload: bool  # association attributes beyond the two FK columns


def relationships(model: RelationalModel) -> list[Relationship]:
    out: list[Relationship] = []
    for fk in model.foreign_keys():
        if model.table(fk.target_table) is None:
            continue  # dangling FK — diagnostic already recorded by the parser
        out.append(Relationship(parent=fk.target_table, child=fk.source_table, fk=fk))
    return out


def cardinality(model: RelationalModel, rel: Relationship) -> str:
    """Children per parent. The FK column(s) carrying a uniqueness guarantee
    on the child table → at most one child per parent → 1:1. This is the
    single most consequential inference in the tool: it is exactly one call
    to Table.is_unique(), which converges all five declaration paths."""
    child = model.table(rel.child)
    if child is not None and child.is_unique(rel.fk.source_columns):
        return CARDINALITY_ONE_TO_ONE
    return CARDINALITY_ONE_TO_MANY


def junction_info(model: RelationalModel, table: Table) -> JunctionInfo | None:
    """Junction iff the PK is exactly the union of two FKs' source columns
    and nothing else is a key column. Payload columns → M:N with association
    attributes (different emit path)."""
    if table.pk is None or not table.pk.is_composite or len(table.pk.columns) != 2:
        return None
    fks = [fk for fk in table.foreign_keys if not fk.is_self_reference and not fk.is_composite]
    if len(fks) < 2:
        return None
    pk_set = set(table.pk.columns)
    for i, fk_a in enumerate(fks):
        for fk_b in fks[i + 1:]:
            if set(fk_a.source_columns) | set(fk_b.source_columns) == pk_set:
                if fk_a.target_table == fk_b.target_table:
                    continue
                payload = any(
                    c.canonical not in pk_set
                    and c.canonical not in set(fk_a.source_columns) | set(fk_b.source_columns)
                    for c in table.columns
                )
                sides = (fk_a.target_table, fk_b.target_table)
                return JunctionInfo(
                    table=table.canonical,
                    sides=tuple(sorted(sides)),  # type: ignore[arg-type]
                    has_payload=payload,
                )
    return None


def detect_junctions(model: RelationalModel) -> dict[str, JunctionInfo]:
    return {
        t.canonical: info
        for t in model.tables.values()
        if (info := junction_info(model, t)) is not None
    }


def referenced_by(model: RelationalModel, table_name: str) -> list[str]:
    """Distinct source tables holding an FK into this table (self-refs excluded)."""
    target = model.table(table_name)
    if target is None:
        return []
    return sorted({
        fk.source_table
        for fk in model.referencing(target.canonical)
        if fk.source_table != target.canonical
    })


def references_of(model: RelationalModel, table_name: str) -> list[str]:
    """Distinct tables this table points at (self-refs excluded)."""
    t = model.table(table_name)
    if t is None:
        return []
    return sorted({fk.target_table for fk in t.foreign_keys if not fk.is_self_reference})


def has_self_reference(model: RelationalModel, table_name: str) -> bool:
    t = model.table(table_name)
    return t is not None and any(fk.is_self_reference for fk in t.foreign_keys)


# Column names that cap the number of children per parent: a trailing
# line/copy/sequence word (line_no, copy_no, page_num, sort_order) argues the
# child set is enumerated and bounded. Bare "order"/"id" columns do NOT count —
# order_id is a key, and a post's "order" is one-per-post, not a cap.
_CAP_SUFFIXES = {
    "no", "num", "seq", "line", "position", "pos", "level", "depth",
    "rank", "version", "copy", "count", "index",
}
_CAP_EXACT = {"position", "sequence", "line_number", "sort_order", "ordinal"}


def is_cap_column(name: str) -> bool:
    lowered = name.lower()
    parts = lowered.split("_")
    if lowered in _CAP_EXACT:
        return True
    return len(parts) >= 2 and parts[-1] in _CAP_SUFFIXES


def is_temporal_growth_column(name: str) -> bool:
    """A temporal column whose name suggests append-style growth."""
    lowered = name.lower()
    return lowered.endswith(("_at", "_time", "_date", "_ts")) or lowered in (
        "timestamp", "created", "logged", "viewed"
    )
