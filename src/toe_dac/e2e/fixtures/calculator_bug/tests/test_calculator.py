import inspect

import pytest

from calculator import add, divide


def test_add():
    assert add(2, 3) == 5


def test_divide():
    assert divide(6, 2) == 3


def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)


def test_divide_signature_is_stable():
    assert list(inspect.signature(divide).parameters) == ["a", "b"]
