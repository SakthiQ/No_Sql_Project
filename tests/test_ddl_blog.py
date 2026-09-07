"""Structural assertions for the blog fixture (Postgres dialect)."""
from nosqlmigrate.core import diagnostics as diag


def test_self_referencing_fk(model_blog):
    comments = model_blog.table("comments")
    self_fks = [fk for fk in comments.foreign_keys if fk.is_self_reference]
    assert len(self_fks) == 1
    assert self_fks[0].source_columns == ("parent_id",)
    assert self_fks[0].on_delete == "CASCADE"


def test_forward_reference_resolves(model_blog):
    """posts references categories before categories is defined (pass-3 binding)."""
    posts = model_blog.table("posts")
    fk = next(fk for fk in posts.foreign_keys if fk.target_table == "categories")
    assert fk.source_columns == ("category_id",)
    assert fk.target_columns == ("category_id",)
    assert fk.on_delete == "SET NULL"


def test_nullable_vs_required_fk(model_blog):
    posts = model_blog.table("posts")
    assert posts.is_nullable("category_id")  # ON DELETE SET NULL requires nullability
    assert not posts.is_nullable("author_id")


def test_reserved_word_quoted_column(model_blog):
    posts = model_blog.table("posts")
    col = posts.column("order")
    assert col is not None
    assert col.name.original == "order"
    assert col.default == "0"


def test_heap_table_without_pk(model_blog):
    pv = model_blog.table("page_views")
    assert pv.pk is None
    codes = [(d.code, d.message) for d in model_blog.diagnostics if d.code == diag.MISSING_PK]
    assert codes and "page_views" in codes[0][1]


def test_check_constraint_diagnostic(model_blog):
    checks = [d for d in model_blog.diagnostics if d.code == diag.UNMIGRATABLE_CHECK]
    assert len(checks) == 1
    assert "CHECK" in checks[0].message


def test_pure_junction(model_blog):
    pt = model_blog.table("post_tags")
    assert set(pt.pk.columns) == {"post_id", "tag_id"}
    assert len(pt.columns) == 2  # no payload columns
    assert {fk.target_table for fk in pt.foreign_keys} == {"posts", "tags"}


def test_serial_types_are_integer_and_autoincrement(model_blog):
    posts = model_blog.table("posts")
    pk_col = posts.column("post_id")
    from nosqlmigrate.core.types import TypeCategory
    assert pk_col.category is TypeCategory.INTEGER
    assert pk_col.autoincrement


def test_inline_unique_slug(model_blog):
    assert model_blog.table("posts").is_unique(("slug",))


def test_dialect_detected(model_blog):
    assert model_blog.dialect == "postgres"
