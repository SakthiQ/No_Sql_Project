from nosqlmigrate.core.identifiers import Identifier, canonical_name


def test_unquoted_folds_to_lowercase():
    assert Identifier("Orders", quoted=False).canonical == "orders"


def test_quoted_preserves_case():
    ident = Identifier("Genres", quoted=True)
    assert ident.canonical == "Genres"
    assert ident.original == "Genres"


def test_unquoted_already_lower_is_identity():
    assert Identifier("orders").canonical == "orders"


def test_canonical_name_helper():
    assert canonical_name("Orders") == "orders"
    assert canonical_name("Genres", quoted=True) == "Genres"


def test_to_dict_round_trip_shape():
    d = Identifier("Foo", quoted=True).to_dict()
    assert d == {"original": "Foo", "quoted": True, "canonical": "Foo"}
