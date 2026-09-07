"""Structural assertions for the library fixture (Postgres, mixed case,
ALTER-added constraints, composite FK, unique index)."""


def test_quoted_mixed_case_table_preserved(model_library):
    assert "Genres" in model_library.tables  # exact case — quoted identifier
    assert model_library.table("Genres").column("genre_name") is not None


def test_fk_to_quoted_table(model_library):
    books = model_library.table("books")
    fk = next(fk for fk in books.foreign_keys if fk.target_table == "Genres")
    assert fk.source_columns == ("genre_id",)
    assert fk.target_columns == ("genre_id",)


def test_one_to_one_pk_is_fk(model_library):
    """book_details.book_id is both the PK and the FK — the D04 trigger."""
    bd = model_library.table("book_details")
    assert bd.pk is not None and bd.pk.columns == ("book_id",)
    fk = next(fk for fk in bd.foreign_keys if fk.target_table == "books")
    assert fk.source_columns == ("book_id",)
    # PK=FK means the child side is unique: the 1:1 signal
    assert bd.is_unique(("book_id",))


def test_unique_index_becomes_uniqueness(model_library):
    bc = model_library.table("book_copies")
    assert bc.is_unique(("book_id", "copy_no"))
    assert not bc.is_unique(("copy_id", "book_id", "copy_no"))  # superset no


def test_composite_fk(model_library):
    loans = model_library.table("loans")
    composite = [fk for fk in loans.foreign_keys if fk.is_composite]
    assert len(composite) == 1
    fk = composite[0]
    assert fk.source_columns == ("book_id", "copy_no")
    assert fk.target_table == "book_copies"
    assert fk.target_columns == ("book_id", "copy_no")


def test_alter_table_fk_bound(model_library):
    """The FK on loans→members arrives via ALTER TABLE ADD CONSTRAINT."""
    loans = model_library.table("loans")
    fk = next(fk for fk in loans.foreign_keys if fk.target_table == "members")
    assert fk.source_columns == ("member_id",)
    assert fk.name == "fk_loans_member"


def test_junction_with_bounded_side(model_library):
    ba = model_library.table("book_authors")
    assert set(ba.pk.columns) == {"book_id", "author_id"}
    assert {fk.target_table for fk in ba.foreign_keys} == {"books", "authors"}


def test_lookup_tables_shape(model_library):
    genres = model_library.table("Genres")
    assert len(genres.columns) == 2
    branches = model_library.table("branches")
    assert len(branches.columns) == 3
    assert model_library.referencing("branches") and model_library.referencing("branches")[0].source_table == "book_copies"


def test_no_diagnostics_for_library(model_library):
    # library is the clean fixture: no unmigratable constructs, all PKs present
    assert [d for d in model_library.diagnostics if d.code in ("PARSE_ERROR", "UNMIGRATABLE", "UNMIGRATABLE_CHECK")] == []
