"""The is_unique truth table: all five declaration paths converge here, and
subsets of composite keys must NOT count as unique."""
from nosqlmigrate.core.identifiers import Identifier
from nosqlmigrate.core.model import Column, PrimaryKey, RelationalModel, Table, UniqueConstraint, make_column
from nosqlmigrate.core.types import TypeCategory


def _table(pk=None, uniques=(), extra_cols=()):
    cols = [
        make_column("id", "INT"),
        make_column("a", "INT"),
        make_column("b", "INT"),
        make_column("payload", "TEXT"),
        *extra_cols,
    ]
    return Table(
        name=Identifier("t"),
        columns=tuple(cols),
        pk=pk,
        uniques=tuple(uniques),
    )


def test_unique_via_single_column_pk():
    t = _table(pk=PrimaryKey(None, ("id",)))
    assert t.is_unique(("id",))
    assert t.is_unique(("ID",))  # case-insensitive lookup
    assert not t.is_unique(("a",))


def test_unique_via_composite_pk_exact_set_only():
    t = _table(pk=PrimaryKey(None, ("a", "b")))
    assert t.is_unique(("a", "b"))
    assert t.is_unique(("b", "a"))  # set equality, order-insensitive
    assert not t.is_unique(("a",))  # subset of composite PK is NOT unique
    assert not t.is_unique(("a", "b", "id"))  # superset is not


def test_unique_via_inline_unique():
    t = _table(uniques=[UniqueConstraint(None, ("a",))])
    assert t.is_unique(("a",))
    assert not t.is_unique(("a", "b"))


def test_unique_via_table_level_composite_unique():
    t = _table(uniques=[UniqueConstraint("uq_ab", ("a", "b"))])
    assert t.is_unique(("a", "b"))
    assert not t.is_unique(("a",))


def test_unique_via_unique_index_constraint():
    """The library fixture path: CREATE UNIQUE INDEX lands as a UniqueConstraint."""
    t = _table(uniques=[UniqueConstraint("uq_copies_book_no", ("a", "b"))])
    assert t.is_unique(("b", "a"))


def test_empty_and_unknown_columns():
    t = _table(pk=PrimaryKey(None, ("id",)))
    assert not t.is_unique(())
    assert not t.is_unique(("nope",))


def test_pk_columns_are_never_nullable():
    t = _table(pk=PrimaryKey(None, ("id", "a")))
    assert not t.is_nullable("id")
    assert not t.is_nullable("a")
    assert t.is_nullable("b")
    assert t.is_nullable("unknown_column")  # unknown → permissive


def test_row_width_sums_column_widths():
    t = Table(
        name=Identifier("t"),
        columns=(
            Column(Identifier("id"), "INT", TypeCategory.INTEGER),
            Column(Identifier("name"), "VARCHAR(50)", TypeCategory.TEXT, char_length=50),
        ),
    )
    assert t.row_width() == 8 + 100


def test_model_lookup_by_any_spelling():
    quoted = Table(name=Identifier("Genres", quoted=True), columns=(make_column("genre_id", "INT"),))
    plain = Table(name=Identifier("branches"), columns=(make_column("branch_id", "INT"),))
    model = RelationalModel(dialect="postgres")
    model.add_table(quoted)
    model.add_table(plain)
    assert model.table("Genres") is quoted  # exact
    assert model.table("genres") is None or model.table("genres") is not quoted
    assert model.table("branches") is plain
    assert model.table("BRANCHES") is plain  # unquoted fold
