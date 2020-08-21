"""Tests for the hypofuzz library."""

import json

import pytest

import hypothesis.strategies as st
from hypothesis import given


def test_non_property():
    pass


@pytest.mark.parametrize("p", [1, 2, 3])
@given(h=st.booleans())
def est_hypothesis_and_parametrize(h, p):
    # mixing them doesn't work at the moment, but it could; see interface.py
    print(h, p)
    pass


@given(st.integers(), st.integers())
def test_addition(x, y):
    x + y


JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(),
    extend=lambda x: st.lists(x, max_size=3)
    | st.dictionaries(st.text(), x, max_size=3),
)


@given(JSON)
def test_encode_decode(x):
    assert x == json.loads(json.dumps(x))
