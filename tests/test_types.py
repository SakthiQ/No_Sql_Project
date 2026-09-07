import pytest

from nosqlmigrate.core.types import TypeCategory, classify_type, size_hint


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INT", TypeCategory.INTEGER),
        ("BIGINT", TypeCategory.INTEGER),
        ("bigint", TypeCategory.INTEGER),
        ("BIGSERIAL", TypeCategory.INTEGER),
        ("SERIAL", TypeCategory.INTEGER),
        ("DECIMAL(12,2)", TypeCategory.DECIMAL),
        ("NUMERIC", TypeCategory.DECIMAL),
        ("DOUBLE PRECISION", TypeCategory.DECIMAL),
        ("VARCHAR(255)", TypeCategory.TEXT),
        ("TEXT", TypeCategory.TEXT),
        ("CHAR(2)", TypeCategory.TEXT),
        ("DATETIME", TypeCategory.TEMPORAL),
        ("TIMESTAMPTZ", TypeCategory.TEMPORAL),
        ("TIMESTAMP WITH TIME ZONE", TypeCategory.TEMPORAL),
        ("BOOLEAN", TypeCategory.BOOLEAN),
        ("BOOL", TypeCategory.BOOLEAN),
        ("BLOB", TypeCategory.BINARY),
        ("BYTEA", TypeCategory.BINARY),
        ("JSONB", TypeCategory.JSON),
        ("JSON", TypeCategory.JSON),
        ("UUID", TypeCategory.UUID),
        ("ENUM('a','b')", TypeCategory.ENUM),
        ("WEIRDTYPE", TypeCategory.UNKNOWN),
    ],
)
def test_classify(raw, expected):
    assert classify_type(raw) is expected


def test_size_hint_uses_declared_length():
    assert size_hint(TypeCategory.TEXT, char_length=100) == 200
    assert size_hint(TypeCategory.BINARY, char_length=16) == 16


def test_size_hint_defaults():
    assert size_hint(TypeCategory.INTEGER) == 8
    assert size_hint(TypeCategory.UNKNOWN) == 32
