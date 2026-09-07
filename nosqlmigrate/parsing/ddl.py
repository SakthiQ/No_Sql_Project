"""sqlglot-backed DDL parser: three passes over the statement list.

Pass 1 — classify statements, build tables from CREATE TABLE (FKs unresolved).
Pass 2 — apply deferred ALTER TABLE actions and CREATE UNIQUE INDEX.
Pass 3 — bind FK targets against the registry (defaults omitted target columns
         to the referenced table's PK; unknown targets become diagnostics).

Three passes because real dumps are not written in dependency order:
`orders` references `customers` before `customers` exists, and
`ALTER TABLE ... ADD CONSTRAINT` at the end is the most common way schemas
actually arrive.

Parsing is maximally permissive: a statement we cannot understand records a
Diagnostic and the run continues, producing a partial (but labeled) model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot import parse as sqlglot_parse
from sqlglot.errors import ParseError

from ..core import diagnostics as diag
from ..core.identifiers import Identifier
from ..core.model import (
    Column,
    ForeignKey,
    PrimaryKey,
    RelationalModel,
    Table,
    UniqueConstraint,
)
from ..core.types import TypeCategory, classify_type
from .dialects import resolve_dialect, sqlglot_dialect

# --------------------------------------------------------------------------- helpers


def _ident(node) -> Identifier:
    """exp.Identifier → our Identifier (original spelling + quoting)."""
    if isinstance(node, exp.Identifier):
        return Identifier(node.name, bool(node.quoted))
    return Identifier(str(node), False)


def _col_name(node) -> str:
    """Canonical column name from an Identifier, Column, or Ordered wrapper."""
    if isinstance(node, exp.Ordered):
        node = node.this
    if isinstance(node, exp.Column):
        node = node.this
    return _ident(node).canonical


def _cols(nodes) -> tuple[str, ...]:
    return tuple(_col_name(n) for n in nodes)


def _ref_action(option_text: str, prefix: str) -> str | None:
    """'ON DELETE SET NULL' + 'ON DELETE' → 'SET NULL'."""
    text = " ".join(option_text.upper().split())
    if text.startswith(prefix):
        return text[len(prefix):].strip() or None
    return None


def _reference_options(reference: exp.Reference) -> tuple[str | None, str | None]:
    on_delete = on_update = None
    for opt in reference.args.get("options") or []:
        text = opt.sql() if not isinstance(opt, str) else opt
        if on_delete is None:
            on_delete = _ref_action(text, "ON DELETE")
        if on_update is None:
            on_update = _ref_action(text, "ON UPDATE")
    return on_delete, on_update


def _target_from_reference(reference: exp.Reference) -> tuple[Identifier, tuple[str, ...]]:
    """Reference(this=Schema(this=Table, expressions=[cols])) → (table ident, cols)."""
    schema = reference.this
    table_ident = _ident(schema.this.this)  # Schema.this = Table, Table.this = Identifier
    target_cols = _cols(schema.expressions)
    return table_ident, target_cols


_SERIAL_TYPES = {"SERIAL", "SMALLSERIAL", "BIGSERIAL"}


@dataclass
class _TableBuilder:
    ident: Identifier
    columns: list[Column] = field(default_factory=list)
    pk: PrimaryKey | None = None
    fks: list[tuple[str | None, tuple[str, ...], Identifier, tuple[str, ...], str | None, str | None]] = field(
        default_factory=list
    )  # (name, source_cols, target_ident, target_cols, on_delete, on_update)
    uniques: list[UniqueConstraint] = field(default_factory=list)

    @property
    def canonical(self) -> str:
        return self.ident.canonical

    def freeze(self) -> Table:
        pk_cols = set(self.pk.columns) if self.pk else set()
        columns = tuple(
            Column(
                name=c.name,
                raw_type=c.raw_type,
                category=c.category,
                nullable=False if c.name.canonical in pk_cols else c.nullable,
                autoincrement=c.autoincrement,
                char_length=c.char_length,
                precision=c.precision,
                scale=c.scale,
                default=c.default,
            )
            for c in self.columns
        )
        return Table(
            name=self.ident,
            columns=columns,
            pk=self.pk,
            foreign_keys=tuple(self.fks),
            uniques=tuple(self.uniques),
        )


# --------------------------------------------------------------------------- column extraction


def _default_text(node, dialect: str) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        return f"'{node.this}'" if node.is_string else str(node.this)
    if isinstance(node, exp.CurrentTimestamp):
        return "CURRENT_TIMESTAMP"
    return node.sql(dialect=dialect)


def _type_params(kind: exp.DataType) -> list:
    params = []
    for p in kind.expressions:
        inner = p.this if isinstance(p, exp.DataTypeParam) else p
        if isinstance(inner, exp.Literal):
            value = inner.this
            # numeric literals may arrive as strings depending on dialect path
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value)
            params.append(value)
        elif isinstance(inner, exp.DataType):
            params.append(str(inner.this.value))
        else:
            params.append(None)
    return params


def _column_from_def(coldef: exp.ColumnDef, dialect: str) -> tuple[Column, dict]:
    """Build a Column plus pending inline-constraint signals for the builder."""
    ident = _ident(coldef.this)
    kind = coldef.args.get("kind")
    raw_type = kind.sql(dialect=dialect) if kind is not None else "UNKNOWN"
    base = str(kind.this.value).upper() if kind is not None else ""
    category = classify_type(raw_type)

    params = _type_params(kind) if kind is not None else []
    char_length = precision = scale = None
    if category is TypeCategory.TEXT and params and isinstance(params[0], int):
        char_length = params[0]
    elif category is TypeCategory.DECIMAL and params and isinstance(params[0], int):
        precision = params[0]
        scale = params[1] if len(params) > 1 and isinstance(params[1], int) else None
    elif category is TypeCategory.BINARY and params and isinstance(params[0], int):
        char_length = params[0]

    nullable = True
    autoincrement = base in _SERIAL_TYPES
    default = None
    inline_pk = False
    inline_unique = False
    inline_reference: exp.Reference | None = None

    for constraint in coldef.args.get("constraints") or []:
        ckind = constraint.args.get("kind") or constraint.this
        if isinstance(ckind, exp.NotNullColumnConstraint):
            nullable = False
        elif isinstance(ckind, exp.PrimaryKeyColumnConstraint):
            inline_pk = True
            nullable = False
        elif isinstance(ckind, exp.UniqueColumnConstraint):
            inline_unique = True  # bare inline UNIQUE (no column list)
        elif isinstance(ckind, exp.AutoIncrementColumnConstraint):
            autoincrement = True
        elif isinstance(ckind, exp.DefaultColumnConstraint):
            default = _default_text(ckind.this, dialect)
        elif isinstance(ckind, exp.Reference):
            inline_reference = ckind
        # comments, collate, generated, etc. are carried in raw_type/DDL text only

    column = Column(
        name=ident,
        raw_type=raw_type,
        category=category,
        nullable=nullable,
        autoincrement=autoincrement,
        char_length=char_length,
        precision=precision,
        scale=scale,
        default=default,
    )
    signals = {
        "inline_pk": inline_pk,
        "inline_unique": inline_unique,
        "inline_reference": inline_reference,
    }
    return column, signals


# --------------------------------------------------------------------------- constraint handling


def _fk_tuple(name, source_cols, reference: exp.Reference):
    target_ident, target_cols = _target_from_reference(reference)
    on_delete, on_update = _reference_options(reference)
    return (name, tuple(source_cols), target_ident, target_cols, on_delete, on_update)


def _apply_table_constraint(builder: _TableBuilder, node, dialect: str, out_diagnostics: list) -> None:
    """One path for inline and table-level constraints: both produce identical
    model objects (the phase-1 refactor pass requires exactly this)."""
    if isinstance(node, exp.PrimaryKey):
        if builder.pk is None:
            builder.pk = PrimaryKey(name=None, columns=_cols(node.expressions))
        else:
            out_diagnostics.append(diag.warning(
                diag.UNSUPPORTED_STATEMENT, f"table {builder.canonical}: duplicate PRIMARY KEY ignored",
                node.sql(dialect=dialect)[:80],
            ))
    elif isinstance(node, exp.ForeignKey):
        builder.fks.append(_fk_tuple(None, _cols(node.expressions), node.args["reference"]))
    elif isinstance(node, exp.Constraint):
        # CONSTRAINT name FOREIGN KEY (...) REFERENCES ... (also UNIQUE/PK wrapped)
        name = node.this.name if isinstance(node.this, exp.Identifier) else None
        for sub in node.expressions:
            if isinstance(sub, exp.ForeignKey):
                builder.fks.append(_fk_tuple(name, _cols(sub.expressions), sub.args["reference"]))
            elif isinstance(sub, exp.UniqueColumnConstraint):
                _apply_table_constraint(builder, sub, dialect, out_diagnostics)
            elif isinstance(sub, exp.PrimaryKey):
                _apply_table_constraint(builder, sub, dialect, out_diagnostics)
    elif isinstance(node, exp.UniqueColumnConstraint) and node.this is not None:
        # named UNIQUE (name) / UNIQUE KEY name (cols) / ALTER ADD UNIQUE (cols)
        schema = node.this
        cols = _cols(schema.expressions)
        name = schema.this.name if isinstance(schema.this, exp.Identifier) else None
        if cols:
            builder.uniques.append(UniqueConstraint(name=name, columns=cols))
    elif isinstance(node, exp.CheckColumnConstraint):
        out_diagnostics.append(diag.warning(
            diag.UNMIGRATABLE_CHECK,
            "CHECK constraint cannot be migrated — enforce in the application layer",
            node.sql(dialect=dialect)[:80],
        ))
    elif isinstance(node, exp.IndexColumnConstraint):
        pass  # plain (non-unique) index: not part of the relational model
    else:
        out_diagnostics.append(diag.info(
            diag.UNSUPPORTED_STATEMENT,
            f"unsupported table constraint ignored: {type(node).__name__}",
            node.sql(dialect=dialect)[:80],
        ))


def _build_table(create: exp.Create, dialect: str, out_diagnostics: list) -> _TableBuilder:
    schema = create.this
    table_ident = _ident(schema.this.this)
    builder = _TableBuilder(ident=table_ident)

    for node in schema.expressions:
        if isinstance(node, exp.ColumnDef):
            column, signals = _column_from_def(node, dialect)
            builder.columns.append(column)
            if signals["inline_pk"]:
                if builder.pk is None:
                    builder.pk = PrimaryKey(name=None, columns=(column.canonical,))
                else:
                    builder.pk = PrimaryKey(name=builder.pk.name,
                                            columns=tuple(dict.fromkeys(builder.pk.columns + (column.canonical,))))
            if signals["inline_unique"]:
                builder.uniques.append(UniqueConstraint(name=None, columns=(column.canonical,)))
            if signals["inline_reference"] is not None:
                builder.fks.append(_fk_tuple(None, (column.canonical,), signals["inline_reference"]))
        else:
            _apply_table_constraint(builder, node, dialect, out_diagnostics)

    return builder


_UNMIGRATABLE_KINDS = {"VIEW", "TRIGGER", "PROCEDURE", "FUNCTION", "MATERIALIZED VIEW", "SEQUENCE"}


def _classify(create: exp.Create, out_diagnostics: list, dialect: str) -> str:
    """'table' | 'unique_index' | 'index' | 'unmigratable' | 'other'."""
    kind = (create.args.get("kind") or "").upper()
    if kind == "TABLE":
        return "table"
    if kind == "INDEX":
        return "unique_index" if create.args.get("unique") else "index"
    if kind in _UNMIGRATABLE_KINDS:
        out_diagnostics.append(diag.warning(
            diag.UNMIGRATABLE,
            f"{kind} is not migrated — handle in the application layer",
            create.sql(dialect=dialect)[:80],
        ))
        return "unmigratable"
    out_diagnostics.append(diag.info(
        diag.UNSUPPORTED_STATEMENT, f"unsupported CREATE {kind} ignored",
        create.sql(dialect=dialect)[:80],
    ))
    return "other"


def _safe_parse(sql: str, dialect: str, out_diagnostics: list) -> list:
    """Parse everything we can. On a parse error, split on statement
    boundaries and salvage the statements that do parse, so one bad line never
    costs the whole model."""
    try:
        return [s for s in sqlglot_parse(sql, read=dialect) if s is not None]
    except ParseError as exc:
        out_diagnostics.append(diag.error(diag.PARSE_ERROR, str(exc).split("\n")[0]))
        statements: list = []
        chunks = [c for c in sql.split(";") if c.strip()]
        for chunk in chunks:
            try:
                parsed = sqlglot_parse(chunk, read=dialect)
                statements.extend(s for s in parsed if s is not None)
            except ParseError:
                out_diagnostics.append(diag.error(
                    diag.PARSE_ERROR, "statement could not be parsed and was skipped",
                    " ".join(chunk.split())[:80],
                ))
        return statements


# --------------------------------------------------------------------------- the public entry point


def parse_ddl(sql: str, dialect: str | None = None) -> RelationalModel:
    """Parse DDL text into a RelationalModel. Never raises on bad input."""
    resolved = resolve_dialect(sql, dialect)
    sg_dialect = sqlglot_dialect(resolved)
    model = RelationalModel(dialect=resolved)
    diagnostics = model.diagnostics

    builders: dict[str, _TableBuilder] = {}
    deferred_alters: list[exp.Alter] = []
    deferred_unique_indexes: list[exp.Create] = []

    # ---- pass 1: classify + build tables -------------------------------
    for stmt in _safe_parse(sql, sg_dialect, diagnostics):
        if isinstance(stmt, exp.Create):
            kind = _classify(stmt, diagnostics, sg_dialect)
            if kind == "table":
                builder = _build_table(stmt, sg_dialect, diagnostics)
                if builder.canonical in builders:
                    diagnostics.append(diag.warning(
                        diag.UNSUPPORTED_STATEMENT,
                        f"duplicate CREATE TABLE {builder.ident.original!r} — later definition wins",
                    ))
                builders[builder.canonical] = builder
            elif kind == "unique_index":
                deferred_unique_indexes.append(stmt)
        elif isinstance(stmt, exp.Alter):
            deferred_alters.append(stmt)
        else:
            diagnostics.append(diag.info(
                diag.UNSUPPORTED_STATEMENT,
                f"statement not used by the analyzer: {type(stmt).__name__}",
                stmt.sql(dialect=sg_dialect)[:80],
            ))

    # ---- pass 2: ALTERs + unique indexes --------------------------------
    for create in deferred_unique_indexes:
        index = create.this  # exp.Index
        table_ident = _ident(index.args["table"].this)
        builder = builders.get(table_ident.canonical)
        if builder is None:
            diagnostics.append(diag.warning(
                diag.ALTER_TARGET_MISSING,
                f"unique index on unknown table {table_ident.original!r} ignored",
                create.sql(dialect=sg_dialect)[:80],
            ))
            continue
        cols = _cols([o for o in index.find_all(exp.Ordered)] or list(index.find_all(exp.Column)))
        if cols:
            name = index.this.name if isinstance(index.this, exp.Identifier) else None
            builder.uniques.append(UniqueConstraint(name=name, columns=cols))

    for alter in deferred_alters:
        table_ident = _ident(alter.this.this)
        builder = builders.get(table_ident.canonical)
        if builder is None:
            diagnostics.append(diag.warning(
                diag.ALTER_TARGET_MISSING,
                f"ALTER on unknown table {table_ident.original!r} ignored",
                alter.sql(dialect=sg_dialect)[:80],
            ))
            continue
        for action in alter.args.get("actions") or []:
            if isinstance(action, exp.ColumnDef):  # ADD COLUMN
                column, signals = _column_from_def(action, sg_dialect)
                if builder.pk is None and signals["inline_pk"]:
                    builder.pk = PrimaryKey(name=None, columns=(column.canonical,))
                if signals["inline_unique"]:
                    builder.uniques.append(UniqueConstraint(name=None, columns=(column.canonical,)))
                if signals["inline_reference"] is not None:
                    builder.fks.append(_fk_tuple(None, (column.canonical,), signals["inline_reference"]))
                if not any(c.canonical == column.canonical for c in builder.columns):
                    builder.columns.append(column)
            elif isinstance(action, exp.AddConstraint):
                for sub in action.expressions:
                    _apply_table_constraint(builder, sub, sg_dialect, diagnostics)
            else:
                diagnostics.append(diag.info(
                    diag.UNSUPPORTED_STATEMENT,
                    f"unsupported ALTER action ignored: {type(action).__name__}",
                    action.sql(dialect=sg_dialect)[:80],
                ))

    # ---- pass 3: bind FKs, freeze tables -------------------------------
    def _target_key(target_ident: Identifier) -> str:
        return target_ident.canonical

    for builder in builders.values():
        frozen_fks: list[ForeignKey] = []
        seen: set[tuple] = set()
        for name, source_cols, target_ident, target_cols, on_delete, on_update in builder.fks:
            target_builder = builders.get(_target_key(target_ident))
            if target_builder is None:
                diagnostics.append(diag.error(
                    diag.DANGLING_FK,
                    f"foreign key from {builder.canonical}.{','.join(source_cols)} references "
                    f"unknown table {target_ident.original!r}",
                ))
            resolved_cols = target_cols
            if target_builder is not None and not resolved_cols:
                if target_builder.pk is not None:
                    resolved_cols = target_builder.pk.columns
                else:
                    diagnostics.append(diag.warning(
                        diag.DANGLING_FK,
                        f"foreign key from {builder.canonical} to {target_builder.canonical} has no "
                        "declared target columns and the target has no primary key",
                    ))
            key = (source_cols, _target_key(target_ident), resolved_cols)
            if key in seen:
                continue  # same relationship declared twice (inline + table-level)
            seen.add(key)
            frozen_fks.append(ForeignKey(
                name=name,
                source_table=builder.canonical,
                source_columns=source_cols,
                target_table=_target_key(target_ident),
                target_columns=resolved_cols,
                on_delete=on_delete,
                on_update=on_update,
            ))
        table = Table(
            name=builder.ident,
            columns=tuple(builder.columns),
            pk=builder.pk,
            foreign_keys=tuple(frozen_fks),
            uniques=tuple(builder.uniques),
        )
        if table.pk is None:
            diagnostics.append(diag.info(
                diag.MISSING_PK,
                f"table {table.canonical} has no primary key",
            ))
        model.add_table(table)

    return model
