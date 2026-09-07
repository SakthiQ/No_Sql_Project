"""Cassandra emitter: query-first tables.

Column-family design is structurally different from document design: there are
no joins, so *each read query becomes its own table*, partitioned by its
equality predicates and clustered by its sort/range columns. The duplication
that results is the cost, paid at write time — the write-amplification warning
quantifies it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..analyze import Analysis
from ..core.types import TypeCategory
from ..rules.engine import DecisionSet

_CQL_TYPES = {
    "int": "INT", "long": "BIGINT", "double": "DOUBLE", "decimal": "DECIMAL",
    "string": "TEXT", "date": "TIMESTAMP", "bool": "BOOLEAN",
    "binData": "BLOB", "object": "TEXT",
}


def _cql_type(column) -> str:
    from .document import bson_type

    return _CQL_TYPES.get(bson_type(column), "TEXT")


@dataclass
class CqlTable:
    name: str
    partition_keys: list[str]
    clustering_keys: list[tuple[str, bool]]  # (column, desc)
    columns: list[tuple[str, str]]  # (name, cql type)
    source_query: str
    base_table: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "partition_keys": self.partition_keys,
            "clustering_keys": [{"column": c, "desc": d} for c, d in self.clustering_keys],
            "columns": [{"name": n, "type": t} for n, t in self.columns],
            "base_table": self.base_table,
            "source_query": self.source_query,
            "warnings": self.warnings,
        }


@dataclass
class ColumnarResult:
    tables: list[CqlTable] = field(default_factory=list)
    write_amplification: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tables": [t.to_dict() for t in self.tables],
            "write_amplification": self.write_amplification,
        }

    def render(self) -> str:
        lines: list[str] = [
            "-- Cassandra schema proposal (query-first tables)",
            "-- One table per read access pattern; partition key from the equality",
            "-- predicates, clustering columns from sorts and ranges.",
            "",
        ]
        for t in self.tables:
            lines.append(f"-- source: {' '.join(t.source_query.split())[:100]}")
            for w in t.warnings:
                lines.append(f"-- WARNING: {w}")
            all_cols = list(t.columns)
            defined = {n for n, _ in all_cols}
            for c in t.clustering_keys:
                if c[0] not in defined:
                    all_cols.append((c[0], _lookup_type(t, c[0])))
                    defined.add(c[0])
            for c in t.partition_keys:
                if c not in defined:
                    all_cols.append((c, _lookup_type(t, c)))
                    defined.add(c)
            body = ",\n    ".join(f"{n} {ty}" for n, ty in all_cols)
            pk = f"({', '.join(t.partition_keys)})"
            if t.clustering_keys:
                pk += f", {', '.join(c for c, _ in t.clustering_keys)}"
            lines.append(f"CREATE TABLE {t.name} (\n    {body},\n    PRIMARY KEY ({pk})\n);")
            lines.append("")
        if self.write_amplification:
            lines.append("-- Write amplification (each base row written to N query tables):")
            for wa in self.write_amplification:
                lines.append(f"--   {wa['table']}: {wa['tables']} tables — "
                             f"{wa['detail']}")
            lines.append("")
        return "\n".join(lines)


def _lookup_type(table: CqlTable, col: str) -> str:
    for n, t in table.columns:
        if n == col:
            return t
    return "TEXT"


# --------------------------------------------------------------------------- emit


def emit_columnar(analysis: Analysis, decisions: DecisionSet) -> ColumnarResult:
    result = ColumnarResult()
    model = analysis.model

    # fields duplicated into each base table (D05) — available to denormalize
    duplicated: dict[str, dict[str, str]] = {}
    for d in decisions.decisions:
        if d.action == "DUPLICATE" and d.child:
            lookup = model.table(d.parent)
            for colname in d.signals.get("duplicated_fields", []):
                col = lookup.column(colname) if lookup is not None else None
                if col is not None:
                    duplicated.setdefault(d.child, {})[colname] = _cql_type(col)

    base_table_counts: dict[str, list[str]] = {}
    seen_shapes: dict[tuple, CqlTable] = {}

    for q in analysis.workload.queries:
        if q.is_write or not q.tables:
            continue
        base = q.tables[0]
        base_table = model.table(base)
        if base_table is None:
            continue

        eq_cols = [p.column for p in q.predicates if p.table == base and p.op in ("=", "IN")]
        range_cols = [p.column for p in q.predicates if p.table == base and p.op in ("<", "<=", ">", ">=", "BETWEEN", "LIKE", "ILIKE")]
        sort_cols = [(s.column, s.desc) for s in q.sorts if s.table == base]

        # partition key: equality predicates; clustering: sorts then ranges
        partition = list(dict.fromkeys(eq_cols))
        clustering: list[tuple[str, bool]] = []
        for col, desc in sort_cols:
            if col not in partition and (col, desc) not in clustering:
                clustering.append((col, desc))
        for col in range_cols:
            if col not in partition and all(c != col for c, _ in clustering):
                clustering.append((col, False))

        projected = {p.column for p in q.projections if p.table == base}
        projected_any = {p.column for p in q.projections}
        joined_others = [t for t in q.tables[1:]]

        shape = (base, tuple(partition), tuple(c for c, _ in clustering))
        if shape in seen_shapes:
            existing = seen_shapes[shape]
            have = {n for n, _ in existing.columns}
            for col in projected:
                if col not in have:
                    column = base_table.column(col)
                    existing.columns.append((col, _cql_type(column) if column else "TEXT"))
                    have.add(col)
            continue

        name = f"{base}_by_{'_'.join(partition)}" if partition else f"{base}_all"
        columns: list[tuple[str, str]] = []
        for col in base_table.columns:
            if col.canonical in projected or col.canonical in partition \
                    or any(c == col.canonical for c, _ in clustering):
                columns.append((col.canonical, _cql_type(col)))
        # denormalized lookup fields (already decided for the document target)
        for extra_name, extra_type in duplicated.get(base, {}).items():
            if extra_name in projected_any:
                columns.append((extra_name, extra_type))

        warnings: list[str] = []
        if not partition:
            warnings.append("no equality predicate on the base table — this table "
                            "requires a full scan or ALLOW FILTERING; consider a "
                            "synthetic partition key")
        # C02: low-cardinality partition keys
        for col_name in partition:
            col = base_table.column(col_name)
            if col is not None and col.category in (TypeCategory.BOOLEAN, TypeCategory.ENUM):
                warnings.append(f"partition key {col_name} is low-cardinality "
                                f"({col.category.value}) — hot partition risk")
        # C03: unbounded partition growth — a temporal clustering column orders
        # rows within the partition but does not bound the partition itself
        for rel in analysis.relationships:
            if rel.child == base and rel.unbounded:
                warnings.append(
                    f"partition grows without bound: {base} is an unbounded child "
                    f"(of {rel.parent}) — consider bucketing the partition key "
                    f"(e.g. by time) to bound partition size")
                break
        if joined_others:
            dup_fields = sorted(set(duplicated.get(base, {})) & projected_any)
            note = f"Cassandra cannot join {', '.join(joined_others)}"
            if dup_fields:
                note += f"; {', '.join(dup_fields)} are denormalized into this table"
            else:
                note += "; serve the other tables with separate queries and join in the app"
            warnings.append(note)

        table = CqlTable(
            name=name,
            partition_keys=partition,
            clustering_keys=clustering,
            columns=columns,
            source_query=q.raw,
            base_table=base,
            warnings=warnings,
        )
        result.tables.append(table)
        seen_shapes[shape] = table
        base_table_counts.setdefault(base, []).append(name)

    for base, tables in sorted(base_table_counts.items()):
        if len(tables) > 1:
            result.write_amplification.append({
                "table": base,
                "tables": tables,
                "detail": f"each {base} write fans out to {len(tables)} query tables",
            })

    result.tables.sort(key=lambda t: t.name)
    return result


def _is_temporal(column) -> bool:
    return column is not None and column.category is TypeCategory.TEMPORAL
