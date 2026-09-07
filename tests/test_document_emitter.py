"""Document emitter: validators, examples, indexes — and the property that
nothing silently vanishes between decisions and collections."""
from __future__ import annotations

from nosqlmigrate.analyze import analyze
from nosqlmigrate.emitters.document import conforms, emit_document
from nosqlmigrate.parsing.queries import parse_queries
from nosqlmigrate.rules.engine import decide
from tests.conftest import fixture_dir

_CACHE: dict[str, tuple] = {}


def build(name: str):
    """(analysis, decisions, DocumentResult) for a fixture — memoized."""
    if name not in _CACHE:
        from nosqlmigrate.parsing.ddl import parse_ddl

        model = parse_ddl((fixture_dir(name) / "schema.sql").read_text())
        workload = parse_queries((fixture_dir(name) / "queries.sql").read_text(), model)
        analysis = analyze(model, workload)
        decisions = decide(analysis)
        _CACHE[name] = (analysis, decisions, emit_document(analysis, decisions))
    return _CACHE[name]


def _coll(result, name):
    return next(c for c in result.collections if c.name == name)


def _field_names(coll):
    return [f.name for f in coll.fields]


# ---- the property test (runs for every fixture) ------------------------------


def test_every_table_accounted_for(fixture_name):
    """Every input table appears as a collection, as embedded/duplicated fields
    in its home collection, or as an explicit drop. Nothing silently vanishes."""
    analysis, decisions, result = build(fixture_name)
    for table, disp in decisions.dispositions.items():
        if disp.disposition == "collection":
            assert any(c.name == table for c in result.collections), \
                f"{table}: disposition says collection but none exists"
            continue
        home = result.home_of[table]
        assert any(c.name == home for c in result.collections), \
            f"{table}: home collection {home} does not exist"
        home_coll = _coll(result, home)
        sources = " ".join(f.source for f in home_coll.fields)
        assert table in sources, f"{table}: no fields with provenance in {home}"


def test_examples_conform_to_their_own_validators(fixture_name):
    _, _, result = build(fixture_name)
    assert result.collections
    for coll in result.collections:
        violations = conforms(coll.example(), coll.validator())
        assert violations == [], f"{coll.name}: {violations}"


# ---- ecommerce -----------------------------------------------------------------


def test_orders_embeds_order_items():
    _, _, result = build("ecommerce")
    orders = _coll(result, "orders")
    items = next(f for f in orders.fields if f.name == "order_items")
    assert items.array_of is not None
    sub_names = [p.name for p in (items.array_of.properties or [])]
    assert "order_id" not in sub_names  # FK back to the parent is dropped
    assert {"line_no", "product_id", "quantity", "unit_price"} <= set(sub_names)


def test_customers_embed_addresses():
    _, _, result = build("ecommerce")
    assert "addresses" in _field_names(_coll(result, "customers"))


def test_orders_validator_marks_required_fields():
    _, _, result = build("ecommerce")
    validator = _coll(result, "orders").validator()["$jsonSchema"]
    assert "order_id" in validator["required"]
    assert validator["properties"]["total"]["bsonType"] == "decimal"


def test_orders_pk_fk_and_workload_indexes():
    _, _, result = build("ecommerce")
    orders = _coll(result, "orders")
    unique_pk = [i for i in orders.indexes if i.unique]
    assert unique_pk and unique_pk[0].keys == [("order_id", 1)]
    fk = [i for i in orders.indexes if i.reason.startswith("foreign key")]
    assert ("customer_id", 1) in fk[0].keys
    # customer order history: WHERE customer_id = ? ORDER BY placed_at DESC
    compound = [i for i in orders.indexes
                if [k[0] for k in i.keys] == ["customer_id", "placed_at"]]
    assert compound and compound[0].keys == [("customer_id", 1), ("placed_at", -1)]


def test_products_junction_multikey_index():
    _, _, result = build("ecommerce")
    products = _coll(result, "products")
    multikey = [i for i in products.indexes if i.multikey]
    assert multikey and multikey[0].keys == [("categories.category_id", 1)]
    assert "categories" in _field_names(products)


def test_ecommerce_home_of():
    _, _, result = build("ecommerce")
    assert result.home_of["order_items"] == "orders"
    assert result.home_of["addresses"] == "customers"
    assert result.home_of["product_categories"] == "products"


# ---- blog -----------------------------------------------------------------------


def test_blog_tags_folded_into_posts():
    _, _, result = build("blog")
    posts = _coll(result, "posts")
    tags = next(f for f in posts.fields if f.name == "tags")
    sub_names = [p.name for p in (tags.array_of.properties or [])]
    assert {"tag_id", "name"} <= set(sub_names)
    assert "tags" not in [c.name for c in result.collections]  # dropped: no standalone reads
    assert "post_tags" not in [c.name for c in result.collections]  # folded


# ---- library ----------------------------------------------------------------------


def test_library_books_shape():
    """books is the showcase: 1:1 collapse + lookup duplication + junction array."""
    _, _, result = build("library")
    books = _coll(result, "books")
    names = _field_names(books)
    assert {"isbn", "synopsis", "page_count"} <= set(names)  # book_details collapsed (D04)
    assert "genre_name" in names                              # Genres duplicated (D05)
    assert "authors" in names                                 # book_authors folded (D07)
    assert "Genres" not in [c.name for c in result.collections]
    assert "book_details" not in [c.name for c in result.collections]


def test_copies_carry_branch_names():
    _, _, result = build("library")
    copies = _coll(result, "book_copies")
    assert {"branch_name", "city"} <= set(_field_names(copies))
    assert "branches" not in [c.name for c in result.collections]
