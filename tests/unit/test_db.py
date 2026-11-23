from app.db import vector_literal


def test_vector_literal_serializes_floats():
    assert vector_literal([1, 2.5, -0.25]) == "[1,2.5,-0.25]"


def test_vector_literal_coerces_numeric_strings():
    assert vector_literal(["1.5", "2"]) == "[1.5,2]"
