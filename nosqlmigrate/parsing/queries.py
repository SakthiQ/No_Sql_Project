"""Query-workload parser.

Statement format in queries.sql:

    -- @weight 30 @tag read
    SELECT ... ;

`@weight` is a relative frequency (default 1). `@tag` overrides the
read/write classification derived from the statement kind. Directive comment
lines accumulate onto the next statement; other comments are ignored.

Column qualifiers are resolved through table aliases (FROM/JOIN `AS`), and
bare column names attach to the FROM table when only one table is present.
Table names resolve through the model so quoted/mixed-case spellings
("Genres") map to the same canonical key the DDL parser produced.
"""
from __future__ import annotations

import re

from sqlglot import exp
from sqlglot import parse as sqlglot_parse
from sqlglot.errors import ParseError

from ..core.model import RelationalModel
from ..core.workload import Predicate, Projection, Query, QueryKind, Sort, Workload
from .dialects import sqlglot_dialect

_DIRECTIVE = re.compile(r"@(weight|tag)\s+([\w.]+)")


def split_statements(sql: str) -> list[tuple[dict, str]]:
    """Split on top-level semicolons, keeping @-directives with their statement."""
    chunks: list[tuple[dict, str]] = []
    directives: dict = {}
    buffer: list[str] = []

    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            for m in _DIRECTIVE.finditer(stripped):
                directives[m.group(1)] = m.group(2)
            continue
        if stripped.startswith("/*"):
            continue  # block comment
        if stripped:
            buffer.append(line)
        if stripped.endswith(";"):
            text = "\n".join(buffer).strip().rstrip(";").strip()
            if text:
                chunks.append((directives, text))
            directives, buffer = {}, []

    if buffer:
        text = "\n".join(buffer).strip().rstrip(";").strip()
        if text:
            chunks.append((directives, text))
    return chunks


_OP_NAMES = {
    exp.EQ: "=", exp.NEQ: "!=", exp.LT: "<", exp.LTE: "<=",
    exp.GT: ">", exp.GTE: ">=",
}


def _canonical_table(name: str, model: RelationalModel) -> str:
    t = model.table(name)
    return t.canonical if t is not None else name.lower()


def _qualified_column(col: exp.Column, aliases: dict[str, str], default_table: str | None) -> tuple[str, str] | None:
    """(canonical_table, column) for a Column node, or None when unresolvable."""
    table_ref = col.table  # alias or table name as written
    if table_ref:
        table = aliases.get(table_ref.lower()) or aliases.get(table_ref)
        if table is None:
            return None
        return table, col.name
    if default_table:
        return default_table, col.name
    return None


class _Extractor:
    def __init__(self, model: RelationalModel):
        self.model = model

    def extract(self, index: int, raw: str, directives: dict, dialect: str) -> Query:
        try:
            stmt = sqlglot_parse(raw, read=dialect)[0]
        except (ParseError, IndexError):
            stmt = None

        if stmt is None:
            return Query(index=index, raw=raw, kind=QueryKind.SELECT, weight=1.0)

        if isinstance(stmt, exp.Select):
            return self._select(index, raw, directives, stmt)
        if isinstance(stmt, exp.Insert):
            return self._insert(index, raw, directives, stmt)
        if isinstance(stmt, exp.Update):
            return self._update(index, raw, directives, stmt)
        if isinstance(stmt, exp.Delete):
            return self._delete(index, raw, directives, stmt)
        if isinstance(stmt, exp.Union):
            # analyze the first leg; set operations are rare in workloads
            inner = stmt.this if isinstance(stmt.this, exp.Select) else None
            if inner is not None:
                q = self._select(index, raw, directives, inner)
                return q
        return Query(index=index, raw=raw, kind=QueryKind.SELECT, weight=1.0)

    # -- statement kinds --------------------------------------------------

    def _select(self, index, raw, directives, stmt: exp.Select) -> Query:
        alias_map, tables = self._from_and_joins(stmt)
        default_table = tables[0] if len(tables) == 1 else None

        projections = []
        for node in stmt.expressions:
            for col in node.find_all(exp.Column):
                resolved = _qualified_column(col, alias_map, default_table)
                if resolved:
                    projections.append(Projection(*resolved))

        predicates = []
        where = stmt.args.get("where") or stmt.args.get("where_")
        if where is not None:
            predicates = self._predicates(where.this, alias_map, default_table)

        sorts = []
        order = stmt.args.get("order")
        if order is not None:
            for oe in order.expressions:
                if not isinstance(oe, exp.Ordered):
                    continue
                col = oe.this if isinstance(oe.this, exp.Column) else next(oe.find_all(exp.Column), None)
                if col is not None:
                    resolved = _qualified_column(col, alias_map, default_table)
                    if resolved:
                        sorts.append(Sort(resolved[0], resolved[1], bool(oe.args.get("desc"))))

        aggregates = []
        for node in stmt.find_all(exp.AggFunc):
            aggregates.append(type(node).__name__.upper())

        return Query(
            index=index, raw=raw, kind=QueryKind.SELECT,
            weight=float(directives.get("weight", 1.0)),
            tag=directives.get("tag", ""),
            tables=tuple(tables),
            joins=tuple(self._join_edges(stmt, alias_map)),
            predicates=tuple(predicates),
            projections=tuple(projections),
            sorts=tuple(sorts),
            aggregates=tuple(dict.fromkeys(aggregates)),
        )

    def _insert(self, index, raw, directives, stmt: exp.Insert) -> Query:
        schema = stmt.this
        table_node = schema.this if isinstance(schema, exp.Schema) else schema
        table = _canonical_table(table_node.name, self.model)
        cols = tuple(c.name for c in schema.expressions) if isinstance(schema, exp.Schema) else ()
        return Query(
            index=index, raw=raw, kind=QueryKind.INSERT,
            weight=float(directives.get("weight", 1.0)), tag=directives.get("tag", ""),
            tables=(table,), set_columns=cols,
        )

    def _update(self, index, raw, directives, stmt: exp.Update) -> Query:
        table = _canonical_table(stmt.this.name, self.model)
        alias_map = {}
        if stmt.this.alias:
            alias_map[stmt.this.alias.lower()] = table
        set_cols = tuple(
            s.this.name for s in stmt.expressions if isinstance(s.this, exp.Column)
        )
        predicates = []
        where = stmt.args.get("where")
        if where is not None:
            predicates = self._predicates(where.this, alias_map, table)
        return Query(
            index=index, raw=raw, kind=QueryKind.UPDATE,
            weight=float(directives.get("weight", 1.0)), tag=directives.get("tag", ""),
            tables=(table,), set_columns=set_cols, predicates=tuple(predicates),
        )

    def _delete(self, index, raw, directives, stmt: exp.Delete) -> Query:
        table = _canonical_table(stmt.this.name, self.model)
        alias_map = {}
        if stmt.this.alias:
            alias_map[stmt.this.alias.lower()] = table
        predicates = []
        where = stmt.args.get("where")
        if where is not None:
            predicates = self._predicates(where.this, alias_map, table)
        return Query(
            index=index, raw=raw, kind=QueryKind.DELETE,
            weight=float(directives.get("weight", 1.0)), tag=directives.get("tag", ""),
            tables=(table,), predicates=tuple(predicates),
        )

    # -- shared walkers ----------------------------------------------------

    def _from_and_joins(self, stmt: exp.Select) -> tuple[dict, list[str]]:
        """Alias→canonical-table map and ordered table list from FROM + JOINs."""
        alias_map: dict[str, str] = {}
        tables: list[str] = []

        def register(table_node: exp.Table) -> None:
            canonical = _canonical_table(table_node.name, self.model)
            if canonical not in tables:
                tables.append(canonical)
            alias = table_node.alias or table_node.name
            if alias:
                alias_map[alias.lower()] = canonical

        from_clause = stmt.args.get("from_") or stmt.args.get("from")
        if from_clause is not None and isinstance(from_clause.this, exp.Table):
            register(from_clause.this)
        elif from_clause is not None:
            # nested subquery in FROM: register its inner tables
            for t in from_clause.find_all(exp.Table):
                register(t)

        for join in stmt.args.get("joins") or []:
            for t in join.find_all(exp.Table):
                register(t)
        return alias_map, tables

    def _join_edges(self, stmt: exp.Select, alias_map: dict) -> list[tuple[str, str]]:
        """Join pairs from JOIN ... ON column=column, plus the joined table
        paired with the FROM table as a fallback when ON is unresolvable."""
        edges: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        from_clause = stmt.args.get("from_") or stmt.args.get("from")
        from_table = None
        if from_clause is not None and isinstance(from_clause.this, exp.Table):
            from_table = _canonical_table(from_clause.this.name, self.model)

        def add(a: str, b: str) -> None:
            if a and b and a != b:
                key = tuple(sorted((a, b)))
                if key not in seen:
                    seen.add(key)
                    edges.append((key[0], key[1]))  # type: ignore[index]

        for join in stmt.args.get("joins") or []:
            joined_tables = [
                _canonical_table(t.name, self.model) for t in join.find_all(exp.Table)
            ]
            on = join.args.get("on")
            resolved_any = False
            if on is not None:
                for cond in self._conditions(on):
                    cols = list(cond.find_all(exp.Column))
                    if len(cols) == 2:
                        left = _qualified_column(cols[0], alias_map, None)
                        right = _qualified_column(cols[1], alias_map, None)
                        if left and right:
                            add(left[0], right[0])
                            resolved_any = True
            if not resolved_any:
                # no resolvable ON condition: pair the joined table with FROM
                for jt in joined_tables:
                    if from_table:
                        add(from_table, jt)
        return edges

    def _conditions(self, node: exp.Expression) -> list[exp.Expression]:
        """Flatten AND/OR trees into leaf conditions."""
        if isinstance(node, exp.Connector):
            out: list[exp.Expression] = []
            for side in node.flatten():
                out.extend(self._conditions(side))
            return out
        return [node]

    def _predicates(self, where: exp.Expression, alias_map: dict, default_table: str | None) -> list[Predicate]:
        preds: list[Predicate] = []
        for cond in self._conditions(where):
            # column-to-column equality across tables is a join condition, not a filter
            if isinstance(cond, exp.EQ):
                cols = list(cond.find_all(exp.Column))
                non_cols = [c for c in (cond.this, cond.expression) if not isinstance(c, exp.Column)]
                if len(cols) >= 2 and not non_cols:
                    continue
            col = next(cond.find_all(exp.Column), None)
            if col is None:
                continue
            resolved = _qualified_column(col, alias_map, default_table)
            if resolved is None:
                continue
            preds.append(Predicate(resolved[0], resolved[1], _op_of(cond)))
        return preds


def _op_of(cond: exp.Expression) -> str:
    for node_type, name in _OP_NAMES.items():
        if isinstance(cond, node_type):
            return name
    if isinstance(cond, (exp.Like, exp.ILike)):
        return "ILIKE" if isinstance(cond, exp.ILike) else "LIKE"
    if isinstance(cond, exp.In):
        return "IN"
    if isinstance(cond, exp.Between):
        return "BETWEEN"
    if isinstance(cond, exp.Is):
        return "IS NOT NULL" if cond.args.get("negate") else "IS NULL"
    return type(cond).__name__.upper()


def parse_queries(sql: str, model: RelationalModel) -> Workload:
    """Parse a queries.sql file into a Workload bound to a model."""
    dialect = sqlglot_dialect(model.dialect)
    extractor = _Extractor(model)
    queries = [
        extractor.extract(i, text, directives, dialect)
        for i, (directives, text) in enumerate(split_statements(sql))
    ]
    return Workload(queries=queries)
