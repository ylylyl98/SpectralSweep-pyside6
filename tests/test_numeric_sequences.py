import pytest

from utils.numeric_sequences import parse_numeric_sequence


@pytest.mark.parametrize("expression, expected", [
    ("4", [4]),
    ("-4,0,4", [-4, 0, 4]),
    ("[-4, 0, 4]", [-4, 0, 4]),
    ("1; 2; 3", [1, 2, 3]),
    ("1e-2,-2e-2", [0.01, -0.02]),
    ("linspace(-4,4,5)", [-4, -2, 0, 2, 4]),
    ("(-4,4,5)", [-4, -2, 0, 2, 4]),
    ("LINSPACE (4,-4,5)", [4, 2, 0, -2, -4]),
    ("linspace(2,2,3)", [2, 2, 2]),
    ("-4:2:4", [-4, -2, 0, 2, 4]),
    ("4:-2:-4", [4, 2, 0, -2, -4]),
    ("0:2:5", [0, 2, 4]),
    ("3:-1:3", [3]),
    ("[linspace(-4,0,3),3,5:2:9]", [-4, -2, 0, 3, 5, 7, 9]),
    ("0:0.1:0.3", [0, 0.1, 0.2, 0.3]),
])
def test_supported_sequences(expression, expected):
    assert parse_numeric_sequence(expression) == pytest.approx(expected)


@pytest.mark.parametrize("expression, error", [
    ("", "Enter"), ("[]", "empty"), ("1,,2", "empty item"),
    ("1,2,", "empty item"), ("[1,2", "brackets"),
    ("linspace(0,1,5", "parentheses"), ("0,1,5)", "parentheses"),
    ("(0,1)", "linspace"), ("linspace(0,1,2,3)", "linspace"),
    ("(0,1,1)", "point count"), ("(0,1,0)", "point count"),
    ("(0,1,-2)", "point count"), ("(0,1,2.5)", "point count"),
    ("0:0:1", "zero step"), ("0:-1:2", "reverse the step sign"),
    ("0:1", "start:step:stop"), ("0::1", "start:step:stop"),
    ("nan", "finite"), ("1,inf", "finite"), ("linspace(0,inf,5)", "finite"),
    ("(0,1,nan)", "finite"), ("0:inf:5", "finite"),
    ("linspace(-1e308,1e308,3)", "finite"),
    ("__import__('os')", "not a number"),
])
def test_invalid_sequences_are_rejected(expression, error):
    with pytest.raises(ValueError, match=error):
        parse_numeric_sequence(expression)


@pytest.mark.parametrize("expression", [
    "linspace(0,1,1000000000000)", "(0,1,1e300)", "0:1:1e300",
    "-1e308:1:1e308", "1,2,3,4,5,6", "0,linspace(1,5,5)",
])
def test_size_limit_is_enforced_before_expansion(expression):
    with pytest.raises(ValueError, match="At most 5"):
        parse_numeric_sequence(expression, max_values=5)


def test_exact_size_limit():
    assert parse_numeric_sequence("0,1:1:4", max_values=5) == [0, 1, 2, 3, 4]
