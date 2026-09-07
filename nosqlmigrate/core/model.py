"""The relational model every later phase reads from.

Design decisions (argued in docs/phase-1-plan.md §3):

- **Frozen dataclasses, not Pydantic.** The core never imports the web layer.
- **Foreign keys hold table *names*, not object references** — no cycles, so
  the model serializes deterministically for golden files. Resolution helpers
  give the convenience back.
- **Every key is a tuple**, even single-column keys: junction detection is
  defined as "primary key is exactly two foreign key columns", unreachable if
  composites get flattened to scalars.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .diagnostics import Diagnostic
from .identifiers import Identifier, canonical_name
from .types import TypeCategory, classify_type, size_hint


@dataclass(frozen=True)
class Column:
    name: Identifier
    raw_type: str
    category: TypeCategory
    nullable: bool = True
    autoincrement: bool = False
    char_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    default: str | None = None

    @property
    def canonical(self) -> str:
        return self.name.canonical

    @property
    def width(self) -> int:
        return size_hint(self.category, self.char_length, self.precision)

    def to_dict(self) -> dict:
        return {
            "name": self.name.original,
            "type": self.raw_type,
            "category": self.category.value,
            "nullable": self.nullable,
            "autoincrement": self.autoincrement,
            "char_length": self.char_length,
            "precision": self.precision,
            "scale": self.scale,
            "default": self.default,
        }


@dataclass(frozen=True)
class PrimaryKey:
    name: str | None
    columns: tuple[str, ...]

    @property
    def is_composite(self) -> bool:
        return len(self.columns) > 1

    def to_dict(self) -> dict:
        return {"name": self.name, "columns": list(self.columns)}


@dataclass(frozen=True)
class ForeignKey:
    name: str | None
    source_table: str  # canonical table name
    source_columns: tuple[str, ...]
    target_table: str  # canonical table name
    target_columns: tuple[str, ...]  # may be empty until pass 3 resolves them
    on_delete: str | None = None
    on_update: str | None = None

    @property
    def is_composite(self) -> bool:
        return len(self.source_columns) > 1

    @property
    def is_self_reference(self) -> bool:
        return self.source_table == self.target_table

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source_table": self.source_table,
            "source_columns": list(self.source_columns),
            "target_table": self.target_table,
            "target_columns": list(self.target_columns),
            "on_delete": self.on_delete,
            "on_update": self.on_update,
        }


@dataclass(frozen=True)
class UniqueConstraint:
    name: str | None
    columns: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"name": self.name, "columns": list(self.columns)}


@dataclass(frozen=True)
class Table:
    name: Identifier
    columns: tuple[Column, ...]
    pk: PrimaryKey | None = None
    foreign_keys: tuple[ForeignKey, ...] = ()
    uniques: tuple[UniqueConstraint, ...] = ()

    # -- lookups ---------------------------------------------------------

    @property
    def canonical(self) -> str:
        return self.name.canonical

    def column(self, name: str) -> Column | None:
        key = name.lower()
        for col in self.columns:
            if col.canonical == key or col.name.original == name:
                return col
        return None

    def column_names(self) -> list[str]:
        return [c.canonical for c in self.columns]

    def is_nullable(self, col_name: str) -> bool:
        col = self.column(col_name)
        if col is None:
            return True
        if self.pk is not None and col.canonical in self.pk.columns:
            return False  # primary keys are never null
        return col.nullable

    def is_unique(self, cols) -> bool:
        """True if this exact column set is guaranteed unique — via the PK,
        an inline UNIQUE, a table-level UNIQUE, or a unique index added by
        ALTER/CREATE UNIQUE INDEX. All declaration paths converge here, and
        phase 3's 1:1-vs-1:N call is a single invocation of this method.

        Note: a *subset* of a composite unique key is not unique; comparison
        is exact set equality.
        """
        wanted = {self._canon(c) for c in cols}
        if not wanted:
            return False
        if self.pk is not None and set(self.pk.columns) == wanted:
            return True
        return any(set(u.columns) == wanted for u in self.uniques)

    def row_width(self) -> int:
        return sum(c.width for c in self.columns)

    def _canon(self, col_name: str) -> str:
        col = self.column(col_name)
        return col.canonical if col else col_name.lower()

    def to_dict(self) -> dict:
        return {
            "name": self.name.original,
            "quoted": self.name.quoted,
            "columns": [c.to_dict() for c in self.columns],
            "primary_key": self.pk.to_dict() if self.pk else None,
            "foreign_keys": [fk.to_dict() for fk in sorted(
                self.foreign_keys, key=lambda f: (f.source_columns, f.target_table))],
            "uniques": [u.to_dict() for u in sorted(self.uniques, key=lambda u: u.columns)],
        }


@dataclass
class RelationalModel:
    dialect: str
    tables: dict[str, Table] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add_table(self, table: Table) -> None:
        self.tables[table.canonical] = table

    def table(self, name: str) -> Table | None:
        """Look up a table by any spelling: exact (quoted/mixed-case) first,
        then the unquoted lowercase fold."""
        return self.tables.get(name) or self.tables.get(canonical_name(name))

    def foreign_keys(self) -> list[ForeignKey]:
        out = []
        for t in self.tables.values():
            out.extend(t.foreign_keys)
        return out

    def referencing(self, table_name: str) -> list[ForeignKey]:
        """All FKs that point *at* this table."""
        target = self.table(table_name)
        if target is None:
            return []
        return [fk for fk in self.foreign_keys() if fk.target_table == target.canonical]

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "tables": [self.tables[k].to_dict() for k in sorted(self.tables)],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def make_column(name: str, raw_type: str, **kwargs) -> Column:
    """Convenience constructor used in tests and synthetic schemas."""
    quoted = kwargs.pop("quoted", False)
    return Column(
        name=Identifier(name, quoted),
        raw_type=raw_type,
        category=classify_type(raw_type),
        **kwargs,
    )
