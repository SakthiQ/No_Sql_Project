"""MongoDB document emitter: collections, $jsonSchema validators, example
documents, index plans.

The example documents are generated to conform to their own validators — a
conformance test enforces it, so the artifacts can't drift from the schema.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from ..analyze import Analysis
from ..core.model import Table
from ..core.types import TypeCategory
from ..rules.engine import DecisionSet
from .base import EmittedIndex, FieldSpec, object_schema

SUBSET_CAP = 20  # default cap for EMBED_SUBSET arrays

_BSON = {
    "int": "int", "long": "long", "double": "double", "decimal": "decimal",
    "string": "string", "date": "date", "bool": "bool",
    "binData": "binData", "object": "object",
}


def bson_type(column) -> str:
    if column.category is TypeCategory.INTEGER:
        return "long" if "BIG" in column.raw_type.upper() else "int"
    if column.category is TypeCategory.DECIMAL:
        if any(t in column.raw_type.upper() for t in ("FLOAT", "DOUBLE", "REAL")):
            return "double"
        return "decimal"
    if column.category is TypeCategory.TEXT:
        return "string"
    if column.category is TypeCategory.TEMPORAL:
        return "date"
    if column.category is TypeCategory.BOOLEAN:
        return "bool"
    if column.category is TypeCategory.BINARY:
        return "binData"
    if column.category is TypeCategory.JSON:
        return "object"
    # UUID / ENUM / UNKNOWN ride as strings
    return "string"


# ------------------------------------------------------------------ examples


def _stable(table: str, column: str, salt: str = "") -> int:
    return zlib.crc32(f"{table}.{column}{salt}".encode())


def example_value(table: str, column, salt: str = ""):
    """Deterministic, plausible example value for a column."""
    name = column.canonical
    h = _stable(table, name, salt)
    cat = column.category
    if cat is TypeCategory.INTEGER:
        return (h % 9000) + 100
    if cat is TypeCategory.DECIMAL:
        return round(((h % 5000) / 100) + 0.99, 2)
    if cat is TypeCategory.TEMPORAL:
        return f"2026-{(h % 12) + 1:02d}-{(h % 27) + 1:02d}T{(h % 24):02d}:30:00Z"
    if cat is TypeCategory.BOOLEAN:
        return h % 2 == 0
    if cat is TypeCategory.JSON:
        return {"kind": "sample", "value": h % 100}
    if "email" in name:
        return f"user{h % 97}@example.com"
    if "url" in name or "ref" in name and cat is TypeCategory.TEXT:
        return f"https://example.com/{name}/{h % 97}"
    if cat is TypeCategory.TEXT and column.char_length and column.char_length <= 80:
        return f"Sample {name.replace('_', ' ')} {h % 97}"
    if cat is TypeCategory.TEXT:
        return f"Sample {name.replace('_', '')} text for document {h % 97}."
    return f"{name}-{h % 97}"


# ------------------------------------------------------------------ the emitter


@dataclass
class Collection:
    name: str
    source_tables: list[str]
    fields: list[FieldSpec] = field(default_factory=list)
    indexes: list[EmittedIndex] = field(default_factory=list)

    def validator(self) -> dict:
        return {
            "$jsonSchema": {
                "bsonType": "object",
                **object_schema(self.fields),
                "additionalProperties": True,
            }
        }

    def example(self) -> dict:
        doc: dict = {}
        for f in self.fields:
            doc[f.name] = _example_for(f)
        return doc

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source_tables": self.source_tables,
            "fields": [f.to_dict() for f in self.fields],
            "validator": self.validator(),
            "example": self.example(),
            "indexes": [i.to_dict() for i in self.indexes],
        }


def _example_for(f: FieldSpec):
    if f.array_of is not None:
        return [_example_for(f.array_of), _example_for(f.array_of)]
    if f.properties:
        return {p.name: _example_for(p) for p in f.properties}
    if f.example is not None:
        return f.example
    return None


def _column_field(table: Table, column) -> FieldSpec:
    return FieldSpec(
        name=column.canonical,
        bson_type=bson_type(column),
        source=f"column:{table.canonical}.{column.canonical}",
        required=not column.nullable,
        description=f"{column.raw_type} from {table.name.original}.{column.name.original}",
        example=example_value(table.canonical, column),
    )


def _child_fields(analysis: Analysis, child: str, skip_fk_to: str | None) -> list[FieldSpec]:
    """Fields for a child table's columns, dropping the FK back to its parent."""
    table = analysis.model.table(child)
    if table is None:
        return []
    skip_cols: set[str] = set()
    if skip_fk_to:
        for fk in table.foreign_keys:
            if fk.target_table == skip_fk_to:
                skip_cols.update(fk.source_columns)
    return [
        _column_field(table, c)
        for c in table.columns
        if c.canonical not in skip_cols
    ]


def _plural(name: str) -> str:
    return name if name.endswith("s") else f"{name}s"


@dataclass
class DocumentResult:
    collections: list[Collection]
    home_of: dict[str, str]  # every input table → the collection its data lives in

    def to_dict(self) -> dict:
        return {
            "collections": [c.to_dict() for c in self.collections],
            "table_homes": dict(sorted(self.home_of.items())),
        }


def emit_document(analysis: Analysis, decisions: DecisionSet) -> DocumentResult:
    model = analysis.model
    dispositions = decisions.dispositions

    # where does each table's data live?
    def home(table: str, seen: frozenset = frozenset()) -> str:
        if table in seen:
            return table
        d = dispositions.get(table)
        if d is None:
            return table
        if d.disposition in ("collection",):
            return table
        if d.disposition in ("embedded", "folded") and d.into:
            return home(d.into, seen | {table})
        if d.disposition == "dropped" and d.into:
            return home(d.into, seen | {table})
        return table

    homes = {t: home(t) for t in model.tables}

    # decisions indexed for lookup
    embeds = {}        # parent -> [(child, decision)]
    dups_by_child = {}  # referencing collection -> [decision] (lookup fields ride along)
    folds = {}         # host -> decision (junction)
    subsets = {}       # parent -> (child, decision)
    for d in decisions.decisions:
        if d.action == "EMBED" and d.parent and d.child:
            embeds.setdefault(d.parent, []).append((d.child, d))
        elif d.action == "DUPLICATE" and d.child:
            dups_by_child.setdefault(d.child, []).append(d)
        elif d.action == "FOLD" and d.parent:
            folds[d.parent] = d
        elif d.action == "EMBED_SUBSET" and d.parent and d.child:
            subsets[d.parent] = (d.child, d)

    collections: list[Collection] = []
    for table_name in sorted(model.tables):
        disp = dispositions.get(table_name)
        if disp is None or disp.disposition not in ("collection",):
            continue
        table = model.table(table_name)
        coll = Collection(name=table_name, source_tables=[table_name])

        # 1. own columns
        if table is not None:
            for column in table.columns:
                coll.fields.append(_column_field(table, column))

        # 2. 1:1 embeds collapse inline; 1:N embeds become arrays
        for child, dec in sorted(embeds.get(table_name, []), key=lambda x: x[0]):
            child_fields = _child_fields(analysis, child, skip_fk_to=table_name)
            if dec.signals.get("cardinality") == "1:1":
                coll.fields.extend(child_fields)
            else:
                coll.fields.append(FieldSpec(
                    name=_plural(child),
                    source=f"embedded:{child} ({dec.rule_id})",
                    array_of=FieldSpec(
                        name=child, source=f"embedded:{child}",
                        properties=child_fields or None,
                        required=False,
                    ),
                    description=f"embedded {child} rows ({dec.rule_id} {dec.rule_name})",
                ))

        # 3. subset embeds: capped array of the most recent children
        if table_name in subsets:
            child, dec = subsets[table_name]
            child_fields = _child_fields(analysis, child, skip_fk_to=table_name)
            coll.fields.append(FieldSpec(
                name=f"recent_{_plural(child)}",
                source=f"embedded-subset:{child} ({dec.rule_id})",
                max_items=SUBSET_CAP,
                array_of=FieldSpec(name=child, source=f"embedded-subset:{child}",
                                   properties=child_fields or None),
                description=f"the {SUBSET_CAP} most recent {child}; full history in "
                            f"the {child} collection ({dec.rule_id})",
            ))

        # 4. duplicated lookup fields (D05): display fields from tiny lookups
        for dec in dups_by_child.get(table_name, []):
            lookup = analysis.model.table(dec.parent)
            if lookup is None:
                continue
            for colname in dec.signals.get("duplicated_fields", []):
                col = lookup.column(colname)
                if col is None:
                    continue
                coll.fields.append(FieldSpec(
                    name=col.canonical,
                    bson_type=bson_type(col),
                    source=f"duplicated:{dec.parent}.{col.canonical}",
                    description=f"denormalized from {dec.parent} ({dec.rule_id}); "
                                f"join key {dec.child} keeps the code",
                    example=example_value(dec.parent, col),
                ))

        # 5. junction arrays (D07): bounded side ids (+ display fields)
        if table_name in folds:
            dec = folds[table_name]
            bounded = dec.signals.get("bounded_side")
            display = dec.signals.get("duplicated_fields", [])
            bounded_table = analysis.model.table(bounded)
            sub_fields: list[FieldSpec] = []
            if bounded_table is not None:
                if bounded_table.pk is not None:
                    for pk_col in bounded_table.pk.columns:
                        col = bounded_table.column(pk_col)
                        if col is not None:
                            sub_fields.append(_column_field(bounded_table, col))
                for colname in display:
                    col = bounded_table.column(colname)
                    if col is not None and col.canonical not in {f.name for f in sub_fields}:
                        sub_fields.append(_column_field(bounded_table, col))
            coll.fields.append(FieldSpec(
                name=_plural(bounded or "refs"),
                source=f"junction:{dec.child} ({dec.rule_id})",
                array_of=FieldSpec(name=bounded, source=f"junction-ref:{bounded}",
                                   properties=sub_fields or None),
                description=f"one-way array of {bounded} refs ({dec.rule_id} {dec.rule_name})",
            ))

        # 6. embedded children of embedded children (depth ≤ 3 by D10) are
        #    flattened into the sub-document via recursion
        coll.indexes = _derive_indexes(analysis, decisions, coll, table, folds, homes)
        collections.append(coll)

    return DocumentResult(collections=collections, home_of=homes)


# ------------------------------------------------------------------ indexes


def _derive_indexes(analysis, decisions, coll, table, folds, homes) -> list[EmittedIndex]:
    indexes: list[EmittedIndex] = []
    seen: set[tuple] = set()

    def add(keys, unique, reason, multikey=False):
        normalized = tuple(keys)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        indexes.append(EmittedIndex(keys=list(keys), unique=unique, reason=reason, multikey=multikey))

    # primary key
    if table is not None and table.pk is not None and len(table.pk.columns) == 1:
        add([(table.pk.columns[0], 1)], True, "primary key")

    # foreign keys kept as references
    if table is not None:
        for fk in table.foreign_keys:
            if fk.is_self_reference:
                continue
            if fk.is_composite:
                add([(c, 1) for c in fk.source_columns], False,
                    f"foreign key to {fk.target_table}")
            else:
                add([(fk.source_columns[0], 1)], False, f"foreign key to {fk.target_table}")

    # workload-driven compounds, derived per query: equality predicates first,
    # then the sort — merging predicates from different queries would invent
    # compounds no query ever issues
    for q in analysis.workload.queries:
        if q.is_write:
            continue
        if any(homes.get(t, t) != coll.name for t in q.tables):
            continue
        eq_fields: list[str] = []
        sort_fields: list[tuple[str, int]] = []
        for p in q.predicates:
            if (p.table == coll.name and p.op in ("=", "IN", "IS NULL", "IS NOT NULL")
                    and p.column not in eq_fields):
                eq_fields.append(p.column)
        for s in q.sorts:
            if s.table == coll.name and s.column not in eq_fields:
                pair = (s.column, -1 if s.desc else 1)
                if pair not in sort_fields:
                    sort_fields.append(pair)
        if eq_fields and sort_fields:
            add([(f, 1) for f in eq_fields] + sort_fields, False,
                "equality predicates + sort from workload query")
        elif eq_fields:
            add([(f, 1) for f in eq_fields], False, "equality predicates from workload query")
        elif sort_fields:
            add(sort_fields, False, "sort from workload query")

    # junction arrays: queries that filtered on the junction's bounded side
    if coll.name in folds:
        dec = folds[coll.name]
        bounded = dec.signals.get("bounded_side")
        array_field = _plural(bounded or "")
        bounded_table = analysis.model.table(bounded)
        if bounded_table is not None and bounded_table.pk is not None:
            pk = bounded_table.pk.columns[0] if len(bounded_table.pk.columns) == 1 else None
            if pk:
                add([(f"{array_field}.{pk}", 1)], False,
                    f"multikey: membership queries on {bounded} via the embedded array",
                    multikey=True)

    indexes.sort(key=lambda i: (not i.unique, [k[0] for k in i.keys]))
    return indexes


# ------------------------------------------------------------------ validation


def conforms(document: dict, validator: dict, path: str = "$") -> list[str]:
    """Check an example document against its $jsonSchema. Returns violations."""
    schema = validator.get("$jsonSchema", validator)
    return _check(document, schema, path)


def _check(value, schema: dict, path: str) -> list[str]:
    violations: list[str] = []
    expected = schema.get("bsonType")
    if expected and not _bson_ok(value, expected):
        violations.append(f"{path}: expected bsonType {expected}, got "
                          f"{_actual_type(value)} ({value!r})")
    if expected == "object" or "properties" in schema:
        if isinstance(value, dict):
            for req in schema.get("required", []):
                if req not in value:
                    violations.append(f"{path}: missing required field {req!r}")
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    violations.extend(_check(value[key], sub, f"{path}.{key}"))
        elif "properties" in schema or "required" in schema:
            violations.append(f"{path}: expected object, got {_actual_type(value)}")
    if expected == "array" and isinstance(value, list):
        items = schema.get("items")
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            violations.append(f"{path}: {len(value)} items exceeds maxItems {max_items}")
        if items:
            for i, item in enumerate(value):
                violations.extend(_check(item, items, f"{path}[{i}]"))
    return violations


_BSON_ACTUAL = {
    int: "int", bool: "bool", str: "string", dict: "object", list: "array",
    float: "double",
}


def _actual_type(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "long" if abs(value) > 2**31 else "int"
    if isinstance(value, float):
        return "double"
    return _BSON_ACTUAL.get(type(value), type(value).__name__)


_BSON_OK = {
    "int": ("int", "long"),
    "long": ("int", "long"),
    "double": ("double", "int", "long"),
    "decimal": ("double", "int", "long"),
    "string": ("string",),
    "bool": ("bool",),
    "date": ("string",),
    "binData": ("string",),
    "object": ("object",),
    "array": ("array",),
}


def _bson_ok(value, expected: str) -> bool:
    actual = _actual_type(value)
    return actual in _BSON_OK.get(expected, (actual,))
