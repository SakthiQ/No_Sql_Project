"""Type normalization: raw SQL types → a small set of categories.

Both the raw type and the category are kept: phase 3 estimates row width from
declared widths, phase 5 emits meaningful BSON/CQL/Cypher types from the raw
spelling, and the rules reason over the category.
"""
from __future__ import annotations

from enum import Enum


class TypeCategory(str, Enum):
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    TEXT = "TEXT"
    TEMPORAL = "TEMPORAL"
    BOOLEAN = "BOOLEAN"
    BINARY = "BINARY"
    JSON = "JSON"
    UUID = "UUID"
    ENUM = "ENUM"
    UNKNOWN = "UNKNOWN"


# Base type name (uppercased, parameters stripped) → category.
_TYPE_MAP: dict[str, TypeCategory] = {
    t: TypeCategory.INTEGER
    for t in (
        "INT", "INTEGER", "INT2", "INT4", "INT8", "TINYINT", "SMALLINT", "MEDIUMINT",
        "BIGINT", "SERIAL", "BIGSERIAL", "SMALLSERIAL", "BIT", "IDENTITY",
    )
} | {
    t: TypeCategory.DECIMAL
    for t in ("DECIMAL", "NUMERIC", "DEC", "FIXED", "FLOAT", "FLOAT4", "FLOAT8",
              "DOUBLE", "DOUBLE PRECISION", "REAL", "MONEY")
} | {
    t: TypeCategory.TEXT
    for t in ("CHAR", "VARCHAR", "VARCHAR2", "CHARACTER", "CHARACTER VARYING",
              "NCHAR", "NVARCHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", "CLOB", "STRING")
} | {
    t: TypeCategory.TEMPORAL
    for t in ("DATE", "DATETIME", "TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE",
              "TIME", "TIMETZ", "TIME WITH TIME ZONE", "YEAR", "INTERVAL")
} | {
    t: TypeCategory.BOOLEAN for t in ("BOOL", "BOOLEAN")
} | {
    t: TypeCategory.BINARY
    for t in ("BINARY", "VARBINARY", "BLOB", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB", "BYTEA")
} | {
    t: TypeCategory.JSON for t in ("JSON", "JSONB")
} | {
    "UUID": TypeCategory.UUID,
} | {
    t: TypeCategory.ENUM for t in ("ENUM", "SET")
}

# MySQL's BIT is binary in PG's hands; treat BIT(1) as boolean later if it matters.
_TYPE_MAP.pop("BIT", None)

#: Default width (bytes) per category when no declared width exists. These feed
#: the row-width estimate; they are heuristics, and the report says so.
_DEFAULT_SIZE: dict[TypeCategory, int] = {
    TypeCategory.INTEGER: 8,
    TypeCategory.DECIMAL: 16,
    TypeCategory.TEXT: 64,
    TypeCategory.TEMPORAL: 8,
    TypeCategory.BOOLEAN: 1,
    TypeCategory.BINARY: 64,
    TypeCategory.JSON: 256,
    TypeCategory.UUID: 16,
    TypeCategory.ENUM: 16,
    TypeCategory.UNKNOWN: 32,
}


def classify_type(raw_type: str) -> TypeCategory:
    """Map a raw type spelling (possibly with parameters) to a category."""
    base = raw_type.strip().upper()
    # strip parameters: DECIMAL(12,2) → DECIMAL; TIMESTAMP(3) → TIMESTAMP
    base = base.split("(", 1)[0].strip()
    # normalize whitespace runs (DOUBLE PRECISION)
    base = " ".join(base.split())
    return _TYPE_MAP.get(base, TypeCategory.UNKNOWN)


def size_hint(category: TypeCategory, char_length: int | None = None,
              precision: int | None = None) -> int:
    """Estimated stored width in bytes for one value of this type."""
    if category is TypeCategory.TEXT and char_length:
        return max(8, char_length * 2)  # utf-8 safety factor
    if category is TypeCategory.BINARY and char_length:
        return char_length
    if category is TypeCategory.DECIMAL and precision:
        return max(8, precision * 2)
    return _DEFAULT_SIZE.get(category, 32)
