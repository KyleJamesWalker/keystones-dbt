"""Finding Jinja blocks with jinja2's lexer, so strings are one token."""

import pytest
from keystones.preprocess import Refused

from keystones_dbt._jinja import blocks


def spans(src):
    return [(b.kind, src[b.start : b.end]) for b in blocks(src)]


def test_each_block_is_found_with_exact_offsets():
    src = "{{ config(x=1) }}\nselect {{ ref('a') }} {# note #}\n{% set y = 2 %}\n"
    assert spans(src) == [
        ("expression", "{{ config(x=1) }}"),
        ("expression", "{{ ref('a') }}"),
        ("comment", "{# note #}"),
        ("statement", "{% set y = 2 %}"),
    ]


def test_a_closing_brace_inside_a_string_does_not_end_the_block():
    block = '{{ config(pre_hook="delete from {{ this }}") }}'
    assert spans(f"select {block}\n") == [("expression", block)]


def test_whitespace_control_markers_are_part_of_the_block():
    src = "a\n{%- if x -%}\nb\n{%- endif %}\n"
    assert spans(src) == [("statement", "{%- if x -%}"), ("statement", "{%- endif %}")]


def test_the_body_is_normalised_and_the_tag_is_the_first_word():
    [block] = blocks("{%   if\n   is_incremental()   %}")
    assert block.body == "if is_incremental()"
    assert block.tag == "if"


def test_an_expression_has_no_tag():
    [block] = blocks("{{ ref( 'a' ) }}")
    assert block.body == "ref( 'a' )"
    assert block.tag == ""


def test_multi_line_blocks_keep_their_offsets():
    src = "{{ config(\n    materialized='table'\n) }}\nselect 1\n"
    [block] = blocks(src)
    assert src[block.start : block.end] == "{{ config(\n    materialized='table'\n) }}"


def test_raw_blocks_are_statements_named_raw():
    src = "{% raw %}{{ not jinja }}{% endraw %}\n"
    assert [(b.kind, b.tag) for b in blocks(src)] == [
        ("statement", "raw"),
        ("statement", "endraw"),
    ]


def test_unlexable_text_is_refused():
    with pytest.raises(Refused, match="Jinja"):
        blocks("select {{ ref('a' }}\n")


def test_plain_sql_has_no_blocks():
    assert blocks("select 1 from t\n") == []
