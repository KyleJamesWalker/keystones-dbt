"""The plugin driving a real keystones gate, which is the only proof that counts."""

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from keystones.cli import main

PYPROJECT = """[tool.keystones]
root = "keystones"
categories = ["default", "finance"]

[[tool.keystones.language]]
builtin = "sql"
extensions = [".sql"]
preprocessor = "keystones_dbt:preprocess"
"""

MODEL = """{{ config(materialized='incremental') }}

-- keystone(finance): net_revenue
create or replace view net_revenue as
select order_id, amount * 0.97 as net from {{ ref('orders') }};
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    for key, value in (("user.name", "T"), ("user.email", "t@e.st")):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    github = tmp_path / ".github"
    github.mkdir()
    (github / "CODEOWNERS").write_text(
        "/keystones/          @org/data\n"
        "/pyproject.toml      @org/data\n"
        "/.github/CODEOWNERS  @org/data\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "revenue.sql").write_text(MODEL)
    return tmp_path


@pytest.fixture
def run(project: Path):
    def _run(*args: str) -> int:
        return main(["--repo-root", str(project), *args])

    return _run


def sidecar(project: Path) -> str:
    return (project / "keystones" / "finance" / "net_revenue.md").read_text()


def model(project: Path) -> Path:
    return project / "models" / "revenue.sql"


def test_a_dbt_model_resolves_to_a_node(project, run):
    """Without the plugin this file does not parse, so there is no node at all."""
    assert run("add", "--id", "net_revenue", "-m", "GAAP rev rec.") == 0
    assert 'target = "models/revenue.sql::net_revenue"' in sidecar(project)


def test_the_sidecar_names_the_plugin(project, run):
    run("add", "--id", "net_revenue", "-m", "GAAP rev rec.")
    text = sidecar(project)
    assert 'hash = "dbt"' in text
    assert "+dbt/1" in text


def test_a_sql_change_gates(project, run):
    run("add", "--id", "net_revenue", "-m", "GAAP rev rec.")
    model(project).write_text(MODEL.replace("0.97", "0.95"))
    assert run("check", "--all", "--no-base") == 1


def test_changing_the_ref_gates(project, run):
    """The lineage is the semantics; a masked span is not a hole in the gate."""
    run("add", "--id", "net_revenue", "-m", "GAAP rev rec.")
    model(project).write_text(MODEL.replace("ref('orders')", "ref('payments')"))
    assert run("check", "--all", "--no-base") == 1


def test_reindenting_the_sql_does_not_gate(project, run):
    run("add", "--id", "net_revenue", "-m", "GAAP rev rec.")
    model(project).write_text(
        MODEL.replace(
            "select order_id, amount * 0.97 as net",
            "select\n    order_id,\n    amount * 0.97 as net",
        )
    )
    assert run("check", "--all", "--no-base") == 0


def test_uppercasing_keywords_does_not_gate(project, run):
    """sqlfluff does this, and it must not read as a change."""
    run("add", "--id", "net_revenue", "-m", "GAAP rev rec.")
    model(project).write_text(
        MODEL.replace("create or replace view", "CREATE OR REPLACE VIEW")
        .replace("select ", "SELECT ")
        .replace(" from ", " FROM ")
    )
    assert run("check", "--all", "--no-base") == 0


def test_repadding_the_jinja_does_not_gate(project, run):
    run("add", "--id", "net_revenue", "-m", "GAAP rev rec.")
    model(project).write_text(MODEL.replace("{{ ref('orders') }}", "{{ref('orders')}}"))
    assert run("check", "--all", "--no-base") == 0


def test_a_control_flow_model_is_refused_with_guidance_when_configured(
    project, run, capsys
):
    (project / "pyproject.toml").write_text(
        PYPROJECT.replace(
            'preprocessor = "keystones_dbt:preprocess"',
            'preprocessor = { plugin = "keystones_dbt:preprocess", '
            'control_flow = "refuse" }',
        )
    )
    model(project).write_text(
        MODEL.replace(
            "from {{ ref('orders') }}",
            "from {% if is_incremental() %}a{% else %}b{% endif %}",
        )
    )
    assert run("check", "--all", "--no-base") == 1
    err = capsys.readouterr().err
    assert "control flow" in err and "hash=text" in err


def test_a_refused_model_still_works_gated_on_text(project, run):
    """The documented way out, end to end."""
    model(project).write_text(
        "{{ config(materialized='table') }}\n"
        "select\n"
        "-- keystone:start(finance, hash=text): rev-rec\n"
        "    amount * 0.97 as net\n"
        "-- keystone:end\n"
        "from {% if x %}a{% else %}b{% endif %}\n"
    )
    assert run("add", "--id", "rev-rec", "-m", "GAAP rev rec.") == 0
    assert run("check", "--all", "--no-base") == 0


# --- the parser plugin, composed with the mask -------------------------------

SNOWFLAKE_PYPROJECT = """[tool.keystones]
root = "keystones"
categories = ["default", "finance"]

[[tool.keystones.language]]
extensions = [".sql"]
parser = { plugin = "keystones_dbt.parsers:sqlglot", dialect = "snowflake" }
preprocessor = "keystones_dbt:preprocess"
"""

SNOWFLAKE_MODEL = """{{ config(materialized='incremental', unique_key='id') }}

with
-- keystone(finance): net
net as (
    select id, amount:cents::int * 0.97 as net
    from {{ ref('orders') }}
    qualify row_number() over (partition by id order by loaded_at desc) = 1
)
select * from net
{% if is_incremental() %}
where loaded_at > (select max(loaded_at) from {{ this }})
{% endif %}
"""


@pytest.fixture
def snowflake_project(project: Path) -> Path:
    pytest.importorskip("sqlglot")
    (project / "pyproject.toml").write_text(SNOWFLAKE_PYPROJECT)
    model(project).write_text(SNOWFLAKE_MODEL)
    return project


def net_sidecar(project: Path) -> Path:
    return project / "keystones" / "finance" / "net.md"


def with_preprocessor_table(text: str) -> str:
    return SNOWFLAKE_PYPROJECT.replace(
        'preprocessor = "keystones_dbt:preprocess"',
        'preprocessor = { plugin = "keystones_dbt:preprocess", ' + text + " }",
    )


def test_a_snowflake_model_resolves_to_a_cte(snowflake_project, run):
    assert run("add", "--id", "net", "-m", "GAAP rev rec.") == 0
    text = net_sidecar(snowflake_project).read_text()
    assert 'target = "models/revenue.sql::net"' in text
    assert 'hash = "dbt"' in text
    assert "keystones-plugin/1+sqlglot@" in text
    assert "/snowflake/" in text and "+dbt/1" in text


def test_a_change_inside_the_cte_gates(snowflake_project, run):
    run("add", "--id", "net", "-m", "x")
    model(snowflake_project).write_text(SNOWFLAKE_MODEL.replace("0.97", "0.95"))
    assert run("check", "--all", "--no-base") == 1


def test_a_change_to_the_incremental_filter_does_not_gate_the_cte(
    snowflake_project, run
):
    run("add", "--id", "net", "-m", "x")
    model(snowflake_project).write_text(
        SNOWFLAKE_MODEL.replace("loaded_at >", "loaded_at >=")
    )
    assert run("check", "--all", "--no-base") == 0


def test_reformatting_and_uppercasing_do_not_gate(snowflake_project, run):
    run("add", "--id", "net", "-m", "x")
    model(snowflake_project).write_text(
        SNOWFLAKE_MODEL.replace(
            "select id, amount", "SELECT\n        id,\n        amount"
        )
    )
    assert run("check", "--all", "--no-base") == 0


def test_c5_catches_a_hand_edited_cte_sidecar(snowflake_project, run, capsys):
    run("add", "--id", "net", "-m", "x")
    path = net_sidecar(snowflake_project)
    path.write_text(path.read_text().replace("0.97", "0.50"))
    assert run("check", "--all", "--no-base") == 1
    assert "[C5]" in capsys.readouterr().err


def test_a_file_keystone_gates_config_and_the_filter(snowflake_project, run):
    whole = SNOWFLAKE_MODEL.replace("-- keystone(finance): net\n", "").replace(
        "{{ config(", "-- keystone(file, finance): whole\n{{ config(", 1
    )
    model(snowflake_project).write_text(whole)
    assert run("add", "--id", "whole", "-m", "x") == 0
    model(snowflake_project).write_text(
        whole.replace("materialized='incremental'", "materialized='table'")
    )
    assert run("check", "--all", "--no-base") == 1


def test_the_drop_policy_is_configured_in_pyproject(snowflake_project, run):
    (snowflake_project / "pyproject.toml").write_text(
        with_preprocessor_table('control_flow = "drop"')
    )
    assert run("add", "--id", "net", "-m", "x") == 0


def test_a_bad_policy_is_a_config_error(snowflake_project, run, capsys):
    (snowflake_project / "pyproject.toml").write_text(
        with_preprocessor_table('control_flow = "guess"')
    )
    assert run("check", "--all", "--no-base") == 2
    assert "control_flow" in capsys.readouterr().err


def test_a_bad_dialect_is_a_config_error(snowflake_project, run, capsys):
    (snowflake_project / "pyproject.toml").write_text(
        SNOWFLAKE_PYPROJECT.replace('"snowflake"', '"not_a_db"')
    )
    assert run("check", "--all", "--no-base") == 2
    assert "dialect" in capsys.readouterr().err
