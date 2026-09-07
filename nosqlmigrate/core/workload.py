"""The workload: what the application actually does with the schema.

Each query is reduced to an access pattern — tables touched, join edges,
predicates, projections, sorts, aggregates — plus a user-supplied weight
(relative frequency) and a read/write tag. Signals (phase 3) are derived from
these patterns; the patterns themselves stay raw and inspectable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class QueryKind(StrEnum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

    @property
    def is_write(self) -> bool:
        return self is not QueryKind.SELECT


@dataclass(frozen=True)
class Predicate:
    """One filter condition: column <op> literal/parameter."""

    table: str  # canonical table name ("" when unresolvable)
    column: str
    op: str  # =, !=, <, <=, >, >=, LIKE, ILIKE, IN, BETWEEN, IS NULL, IS NOT NULL

    def to_dict(self) -> dict:
        return {"table": self.table, "column": self.column, "op": self.op}


@dataclass(frozen=True)
class Projection:
    table: str
    column: str

    def to_dict(self) -> dict:
        return {"table": self.table, "column": self.column}


@dataclass(frozen=True)
class Sort:
    table: str
    column: str
    desc: bool

    def to_dict(self) -> dict:
        return {"table": self.table, "column": self.column, "desc": self.desc}


@dataclass(frozen=True)
class Query:
    index: int
    raw: str
    kind: QueryKind
    weight: float = 1.0
    tag: str = ""  # "read" | "write" (derived from kind unless overridden)
    tables: tuple[str, ...] = ()  # canonical, first-appearance order
    joins: tuple[tuple[str, str], ...] = ()  # canonical table pairs, sorted within pair
    predicates: tuple[Predicate, ...] = ()
    projections: tuple[Projection, ...] = ()
    set_columns: tuple[str, ...] = ()  # INSERT target columns / UPDATE SET columns
    sorts: tuple[Sort, ...] = ()
    aggregates: tuple[str, ...] = ()  # COUNT, SUM, AVG, MIN, MAX

    @property
    def is_write(self) -> bool:
        return self.kind.is_write if not self.tag else self.tag == "write"

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "kind": self.kind.value,
            "weight": self.weight,
            "tag": self.tag or ("write" if self.kind.is_write else "read"),
            "tables": list(self.tables),
            "joins": [list(j) for j in self.joins],
            "predicates": [p.to_dict() for p in self.predicates],
            "projections": [p.to_dict() for p in self.projections],
            "set_columns": list(self.set_columns),
            "sorts": [s.to_dict() for s in self.sorts],
            "aggregates": list(self.aggregates),
            "raw": " ".join(self.raw.split()),
        }


@dataclass
class Workload:
    queries: list[Query] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"queries": [q.to_dict() for q in self.queries]}

    # -- aggregations used by the signal layer ----------------------------

    def total_weight(self) -> float:
        return sum(q.weight for q in self.queries)

    def weight_touching(self, table: str) -> float:
        return sum(q.weight for q in self.queries if table in q.tables)

    def write_weight_touching(self, table: str) -> float:
        return sum(q.weight for q in self.queries if q.is_write and table in q.tables)

    def weight_touching_both(self, a: str, b: str) -> float:
        return sum(q.weight for q in self.queries if a in q.tables and b in q.tables)

    def weight_touching_without(self, table: str, other: str) -> float:
        """Weight of queries touching `table` but not `other`."""
        return sum(
            q.weight for q in self.queries if table in q.tables and other not in q.tables
        )
