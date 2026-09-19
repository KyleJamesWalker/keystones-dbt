"""Masking dbt's Jinja so the SQL underneath can be parsed and hashed."""

import pytest
from keystones.preprocess import Refused

from keystones_dbt import (
    KEYSTONES_PREPROCESSOR_NAME,
    KEYSTONES_PREPROCESSOR_VERSION,
    preprocess,
)

MODEL = """{{ config(materialized='incremental') }}

with orders as (
    select order_id, amount from {{ ref('orders') }}
)
select order_id, amount * 0.97 as net from orders
"""


def masked(src: str) -> str:
    return preprocess(src)[0]


def spans(src: str) -> str:
    return preprocess(src)[1]


# --- identity ----------------------------------------------------------------


def test_it_declares_the_identity_keystones_requires():
    assert KEYSTONES_PREPROCESSOR_NAME == "dbt"
    assert KEYSTONES_PREPROCESSOR_VERSION


# --- the contract ------------------------------------------------------------


def test_the_mask_preserves_the_line_count():
    assert masked(MODEL).count("\n") == MODEL.count("\n")


def test_a_multi_line_expression_still_preserves_lines():
    src = "select a from {{ ref(\n    'orders'\n) }}\nwhere x = 1\n"
    assert masked(src).count("\n") == src.count("\n")


def test_a_placeholder_is_derived_from_content_not_position():
    """C5 re-hashes a slice on its own, so context must not change the mask."""
    one = masked("select * from {{ ref('orders') }}\n")
    two = masked("select 1;\nselect * from {{ ref('orders') }}\n")
    assert one.strip() in two


def test_the_same_expression_masks_the_same_way_twice():
    out = masked("select {{ a() }}, {{ a() }} from t\n")
    tokens = [w for w in out.replace(",", " ").split() if w.startswith("__ks_")]
    assert len(tokens) == 2 and tokens[0] == tokens[1]


def test_padding_inside_the_braces_is_not_a_change():
    assert preprocess("select * from {{ref('o')}}\n") == preprocess(
        "select * from {{   ref('o')   }}\n"
    )


def test_spacing_between_tokens_is_still_a_change():
    """Deliberately conservative: normalising further needs a Jinja parser.

    Collapsing all whitespace would equate var('my key') with var('mykey'), and
    a gate that misses a change is worse than one that trips on a reformat.
    """
    assert preprocess("select {{ ref('o') }}\n") != preprocess(
        "select {{ ref( 'o' ) }}\n"
    )


# --- what gets masked --------------------------------------------------------


def test_a_directive_on_its_own_line_is_removed_not_named():
    """`{{ config() }}` sits where a statement belongs; a name there is invalid."""
    assert "__ks_" not in masked(MODEL).splitlines()[0]
    assert masked(MODEL).splitlines()[0].strip() == ""


def test_a_multi_line_directive_is_removed_too():
    src = "{{ config(\n    materialized='table'\n) }}\nselect 1\n"
    out = masked(src)
    assert "__ks_" not in out and out.count("\n") == src.count("\n")


def test_an_inline_expression_becomes_an_identifier():
    out = masked("select * from {{ ref('orders') }}\n")
    assert "__ks_" in out and "{{" not in out


def test_a_jinja_comment_is_removed():
    assert "never" not in masked("{# never mind #}\nselect 1\n")


def test_a_non_control_tag_is_removed():
    assert "{%" not in masked("{% set x = 1 %}\nselect {{ x }}\n")


def test_masked_content_is_handed_back_for_hashing():
    assert "ref('orders')" in spans(MODEL)
    assert "config(materialized='incremental')" in spans(MODEL)


def test_changing_a_ref_changes_the_spans():
    assert spans(MODEL) != spans(MODEL.replace("'orders'", "'payments'"))


# --- refusal -----------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "select {% if x %}a{% else %}b{% endif %} from t\n",
        "select {% for c in cols %}{{ c }},{% endfor %} 1 from t\n",
        "select a from t\n{% if inc %}where 1=1{% endif %}\n",
    ],
)
def test_control_flow_is_refused(src):
    """Branches concatenate into nonsense that still parses. Refuse instead."""
    with pytest.raises(Refused, match="control flow"):
        preprocess(src)


def test_a_brace_inside_a_string_is_refused():
    """`{{ "}}" }}` mis-splits, and the result parses while meaning something else."""
    with pytest.raises(Refused, match="quote"):
        preprocess("select '{{ \"}}\" }}' as x from t\n")


def test_an_ordinary_quoted_argument_is_not_refused():
    assert masked("select * from {{ ref('orders') }}\n")


# --- the residue is real SQL -------------------------------------------------


def test_the_residue_parses_as_sql():
    parser = pytest.importorskip("tree_sitter_language_pack").get_parser("sql")
    assert not parser.parse(masked(MODEL).encode()).root_node.has_error
