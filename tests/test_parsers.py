"""A keystones parser over sqlglot, one dialect per table."""

import pytest
from keystones.parser import Unparseable

pytest.importorskip("sqlglot")

from keystones_dbt.parsers import sqlglot

MODEL = """-- keystone(finance): orders
with orders as (
    select id, amount:cents::int as cents
    from __ks_a1b2c3d4e5f6
    qualify row_number() over (partition by id order by loaded_at desc) = 1
),
totals as (
    select sum(cents) as cents from orders
)
select * from totals
"""


@pytest.fixture
def snowflake():
    return sqlglot(dialect="snowflake")


def test_identity_names_the_library_version_and_dialect(snowflake):
    import sqlglot as lib

    assert snowflake.name == "snowflake"
    assert snowflake.identity == f"sqlglot@{lib.__version__}/snowflake"


def test_an_unknown_dialect_is_a_value_error():
    with pytest.raises(ValueError, match="dialect"):
        sqlglot(dialect="not_a_db")


def test_ctes_are_definitions_with_line_spans(snowflake):
    defs = snowflake.parse(MODEL).definitions()
    assert [(d.qualname, d.start, d.end) for d in defs] == [
        ("orders", 2, 6),
        ("totals", 7, 9),
    ]


def test_a_create_statement_is_a_definition(snowflake):
    src = "create or replace view analytics.v as\nselect 1\n"
    [d] = snowflake.parse(src).definitions()
    assert (d.qualname, d.start, d.end) == ("analytics.v", 1, 2)


def test_a_cte_inside_a_create_is_qualified_by_it(snowflake):
    src = "create view v as\nwith c as (\n  select 1\n)\nselect * from c\n"
    assert [d.qualname for d in snowflake.parse(src).definitions()] == ["v", "v.c"]


def test_comment_lines_come_with_their_line_numbers(snowflake):
    src = (
        "-- keystone: a\n"
        "select 1; -- trailing\n"
        "/* two\n"
        "lines */\n"
        "select 'not -- a comment'\n"
    )
    assert snowflake.parse(src).comments() == [
        (1, "-- keystone: a"),
        (2, "-- trailing"),
        (3, "/* two"),
        (4, "lines */"),
    ]


def test_render_ignores_case_and_whitespace(snowflake):
    a = snowflake.parse("select   ID from T\nwhere x=1\n")
    b = snowflake.parse("SELECT id\nFROM t WHERE x = 1\n")
    assert a.render(None) == b.render(None)


def test_render_sees_a_meaning_change(snowflake):
    a = snowflake.parse("select id from t where x = 1\n")
    b = snowflake.parse("select id from t where x = 2\n")
    assert a.render(None) != b.render(None)


def test_render_excludes_comments(snowflake):
    a = snowflake.parse("select id -- note\nfrom t\n")
    b = snowflake.parse("select id from t\n")
    assert a.render(None) == b.render(None)


def test_a_cte_fragment_renders_like_the_cte_in_context(snowflake):
    """C5 parses the stored slice alone; the answer must not depend on context."""
    tree = snowflake.parse(MODEL)
    orders = tree.definitions()[0]
    fragment = "\n".join(MODEL.splitlines()[orders.start - 1 : orders.end])
    alone = snowflake.parse_fragment(fragment)
    assert alone.render(alone.definitions()[0]) == tree.render(orders)


def test_a_statement_fragment_renders_like_itself(snowflake):
    src = "create view v as select 1\n"
    assert snowflake.parse_fragment(src).render(None) == snowflake.parse(src).render(
        None
    )


def test_text_that_is_not_sql_is_unparseable(snowflake):
    with pytest.raises(Unparseable):
        snowflake.parse("select from where (\n")


def test_an_empty_file_is_unparseable(snowflake):
    with pytest.raises(Unparseable):
        snowflake.parse("\n\n")


def test_snowflake_syntax_the_generic_grammar_lacks_parses(snowflake):
    src = (
        "select v:a.b::text as ab, f.value:name::string as n\n"
        "from t, lateral flatten(input => t.arr) as f\n"
        "qualify row_number() over (partition by ab order by n) = 1\n"
    )
    assert snowflake.parse(src).render(None)
