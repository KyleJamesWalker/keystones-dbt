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


def test_a_config_line_may_carry_a_trailing_sql_comment():
    out = masked("{{ config(materialized='table') }} -- why\nselect 1\n")
    assert "__ks_" not in out
    assert "select 1" in out


def test_only_config_is_dropped_when_alone_on_a_line():
    """A macro emitting the last select column is a value, not a directive."""
    out = masked("select\n    id,\n    {{ cents('amount') }}\nfrom t\n")
    assert out.count("__ks_") == 1
    assert ",\n\nfrom" not in out


def test_two_expressions_on_one_line_are_two_placeholders():
    out = masked("select\n{{ a() }}, {{ b() }}\nfrom t\n")
    assert out.count("__ks_") == 2
    assert "select" in out and "from t" in out


def test_an_inline_expression_becomes_an_identifier():
    out = masked("select * from {{ ref('orders') }}\n")
    assert "__ks_" in out and "{{" not in out


def test_a_jinja_comment_is_removed():
    assert "never" not in masked("{# never mind #}\nselect 1\n")


def test_masked_content_is_handed_back_for_hashing():
    assert "ref('orders')" in spans(MODEL)
    assert "config(materialized='incremental')" in spans(MODEL)


def test_changing_a_ref_changes_the_spans():
    assert spans(MODEL) != spans(MODEL.replace("'orders'", "'payments'"))


# --- strings are strings -----------------------------------------------------


def test_a_closing_brace_inside_a_string_is_masked_correctly():
    out = masked('{{ config(pre_hook="delete from {{ this }}") }}\nselect 1\n')
    assert out == "\nselect 1\n"


def test_an_apostrophe_inside_a_double_quoted_string_is_fine():
    assert "__ks_" in masked('select {{ var("don\'t") }} from t\n')


# --- statement tags ----------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "{% set x = 1 %}\nselect {{ x }}\n",
        "{%- set x = 1 -%}\nselect {{ x }}\n",
        "{% do log('x') %}\nselect 1\n",
        "{% import 'm.sql' as m %}\nselect 1\n",
        "{% from 'm.sql' import x %}\nselect 1\n",
    ],
)
def test_statement_tags_are_dropped(src):
    assert "{%" not in masked(src)


@pytest.mark.parametrize(
    "src, tag",
    [
        ("{% macro m() %}\nselect 1\n{% endmacro %}\n", "macro"),
        ("{% set q %}\nselect 1\n{% endset %}\n", "set"),
        ("{% call statement('x') %}select 1{% endcall %}\n", "call"),
        ("{% raw %}{{ not jinja }}{% endraw %}\nselect 1\n", "raw"),
        (
            "{% materialization x, default %}select 1{% endmaterialization %}\n",
            "materialization",
        ),
    ],
)
def test_block_tags_are_refused(src, tag):
    """Dropping the tags would leave the block body behind as if it were SQL."""
    with pytest.raises(Refused, match=tag):
        preprocess(src)


# --- control flow: first-branch (default) ------------------------------------

IF_ELSE = (
    "select a\n"
    "{% if is_incremental() %}\n"
    "where x > 1\n"
    "{% else %}\n"
    "where 1 = 1\n"
    "{% endif %}\n"
    "from t\n"
)


def test_first_branch_keeps_the_first_body_and_drops_the_rest():
    assert masked(IF_ELSE) == "select a\n\nwhere x > 1\n\n\n\nfrom t\n"


def test_first_branch_hashes_the_whole_block_as_one_span():
    assert (
        "if is_incremental() %} where x > 1 {% else %} where 1 = 1 {% endif"
        in spans(IF_ELSE)
    )


def test_a_change_in_the_dropped_branch_still_changes_the_spans():
    assert spans(IF_ELSE) != spans(IF_ELSE.replace("where 1 = 1", "where 2 = 2"))


def test_elif_branches_are_dropped_too():
    src = "{% if a %}\nx\n{% elif b %}\ny\n{% else %}\nz\n{% endif %}\n"
    assert masked(src) == "\nx\n\n\n\n\n\n"


def test_expressions_inside_the_kept_branch_are_masked():
    src = "{% if a %}\nfrom {{ ref('t') }}\n{% endif %}\n"
    out = masked(src)
    assert out.startswith("\nfrom __ks_") and "{{" not in out


def test_nested_blocks_inside_the_kept_branch_follow_the_policy():
    src = "{% if a %}\n{% if b %}\nx\n{% else %}\ny\n{% endif %}\n{% endif %}\n"
    assert masked(src) == "\n\nx\n\n\n\n\n"


def test_a_for_body_is_kept_once():
    src = "select\n{% for c in cols %}\n{{ c }},\n{% endfor %}\n1\n"
    out = masked(src)
    assert out.count("__ks_") == 1 and "{%" not in out


def test_the_block_shape_is_part_of_the_hash():
    """`{% if x %}a{% endif %}b` and `a{% if x %}b{% endif %}` must not collide."""
    one = preprocess("{% if x %}a{% endif %}b\n")
    two = preprocess("a{% if x %}b{% endif %}\n")
    assert one != two


# --- control flow: other policies --------------------------------------------


def test_drop_removes_the_whole_block():
    out, extra = preprocess(IF_ELSE, control_flow="drop")
    assert out == "select a\n\n\n\n\n\nfrom t\n"
    assert "where x > 1" in extra


def test_refuse_refuses_control_flow():
    with pytest.raises(Refused, match="control flow"):
        preprocess(IF_ELSE, control_flow="refuse")


def test_an_unknown_policy_is_a_value_error():
    with pytest.raises(ValueError, match="control_flow"):
        preprocess("select 1\n", control_flow="guess")


# --- a block cut by the text's boundary --------------------------------------


@pytest.mark.parametrize("src", ["{% if a %}\nx\n", "x\n{% endif %}\n", "{% else %}\n"])
def test_an_unbalanced_block_is_refused_with_guidance(src):
    with pytest.raises(Refused, match="whole block"):
        preprocess(src)


# --- the residue is real SQL -------------------------------------------------


def test_the_residue_parses_as_sql():
    parser = pytest.importorskip("tree_sitter_language_pack").get_parser("sql")
    assert not parser.parse(masked(MODEL).encode()).root_node.has_error
