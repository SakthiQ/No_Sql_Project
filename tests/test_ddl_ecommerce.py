"""Structural assertions for the ecommerce fixture: the relationships the
columns encode, not incidental facts like column counts."""
from nosqlmigrate.core import diagnostics as diag


def test_all_tables_present(model_ecommerce):
    names = set(model_ecommerce.tables)
    assert names == {
        "customers", "addresses", "orders", "products", "categories",
        "product_categories", "order_items", "payments", "order_events",
    }


def test_orders_to_customers_fk(model_ecommerce):
    orders = model_ecommerce.table("orders")
    fks = orders.foreign_keys
    assert len(fks) == 1
    fk = fks[0]
    assert fk.source_columns == ("customer_id",)
    assert fk.target_table == "customers"
    assert fk.target_columns == ("customer_id",)
    assert fk.on_delete is None


def test_junction_composite_pk(model_ecommerce):
    pc = model_ecommerce.table("product_categories")
    assert pc.pk.is_composite
    assert set(pc.pk.columns) == {"product_id", "category_id"}
    targets = {fk.target_table for fk in pc.foreign_keys}
    assert targets == {"products", "categories"}


def test_order_items_composite_pk_and_cascade(model_ecommerce):
    items = model_ecommerce.table("order_items")
    assert set(items.pk.columns) == {"order_id", "line_no"}
    by_target = {fk.target_table: fk for fk in items.foreign_keys}
    assert by_target["orders"].on_delete == "CASCADE"
    assert by_target["products"].on_delete is None


def test_payments_one_to_one_via_unique_fk(model_ecommerce):
    """The cardinality signal: FK column carrying a UNIQUE constraint."""
    payments = model_ecommerce.table("payments")
    assert payments.is_unique(("order_id",))
    assert not payments.is_unique(("payment_id", "order_id"))


def test_addresses_composite_unique(model_ecommerce):
    addresses = model_ecommerce.table("addresses")
    assert addresses.is_unique(("customer_id", "label"))
    assert not addresses.is_unique(("customer_id",))


def test_view_is_reported_unmigratable(model_ecommerce):
    codes = [d.code for d in model_ecommerce.diagnostics]
    assert diag.UNMIGRATABLE in codes
    view_diag = next(d for d in model_ecommerce.diagnostics if d.code == diag.UNMIGRATABLE)
    assert "VIEW" in view_diag.message


def test_no_parse_errors(model_ecommerce):
    errors = [d for d in model_ecommerce.diagnostics if d.code == diag.PARSE_ERROR]
    assert errors == []


def test_mysql_backtick_identifiers_unquoted_canonically(model_ecommerce):
    # backticked `orders` is quoted in MySQL, but canonical folding still applies
    assert "orders" in model_ecommerce.tables


def test_self_consistent_fk_targets(model_ecommerce):
    for fk in model_ecommerce.foreign_keys():
        target = model_ecommerce.table(fk.target_table)
        assert target is not None
        assert set(fk.target_columns) <= set(target.column_names())
