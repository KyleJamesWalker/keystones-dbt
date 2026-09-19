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


def test_a_control_flow_model_is_refused_with_guidance(project, run, capsys):
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
