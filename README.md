# keystones-dbt

Gate a dbt model on the SQL it means, not on the text it is written as.

A [keystones](https://github.com/KyleJamesWalker/keystones) keystone pins a
review gate to an AST node. dbt models are Jinja, which no SQL grammar can
parse, so without a preprocessor they can only be gated on normalised text -
which trips on every reformat.

This package is two plugins. The preprocessor masks the templating so the SQL
underneath parses, and hands the masked content back so it is still gated. The
parser reads any SQL dialect sqlglot knows, for warehouses the tree-sitter
grammar pack has no grammar for.

## Install

```bash
pip install "keystones-dbt[sqlglot]"
```

```toml
[[tool.keystones.language]]
extensions   = [".sql"]
parser       = { plugin = "keystones_dbt.parsers:sqlglot", dialect = "snowflake" }
preprocessor = { plugin = "keystones_dbt:preprocess", control_flow = "first-branch" }
```

`dialect` is any sqlglot knows: `snowflake`, `bigquery`, `redshift`,
`databricks`, `postgres`, `duckdb` and more. Parsing is offline; nothing
connects to a warehouse. Where the grammar pack has a grammar for your dialect
you may use `builtin = "sql_bigquery"` in place of `parser` and skip the extra.

## What the mask does

| Jinja | becomes |
|---|---|
| `{# comment #}` | removed |
| `{{ config(...) }}` alone on a line | removed |
| any other `{{ ... }}` | a placeholder derived from its content |
| `{% set x = 1 %}`, `{% do %}`, `{% import %}`, `{% from %}` | removed |
| `{% if %}`, `{% for %}` | governed by `control_flow`, below |
| `{% macro %}`, block `{% set %}`, `{% call %}`, `{% raw %}`, other block tags | **refused** |

Masked content is hashed verbatim, so changing `ref('orders')` to
`ref('payments')` trips the gate while reformatting the expression does not.

A keystone hashes its own lines, so a node keystone on a CTE does not see the
`config()` block above it or an incremental filter below it. To gate
materialization or the filter, use `keystone(file)`.

## Control flow

| `control_flow` | `{% if a %} x {% else %} y {% endif %}` |
|---|---|
| `first-branch` (default) | tags removed, `x` kept and masked, `y` removed; the whole block is hashed as one span |
| `drop` | the whole block removed and hashed as one span |
| `refuse` | the file is refused; gate it on text with a region |

With `first-branch` the SQL of the first branch still parses and anchors
nodes. The whole block, every branch included, is in the hash, so a change to
`y` still gates and `{% if a %}x{% endif %}y` cannot collide with
`x{% if a %}y{% endif %}`. A block whose first branch is not valid SQL on its
own fails to parse and is reported with the `hash=text` guidance.

A block that opens or closes outside a keystone's target, because a region or
CTE boundary cuts through it, is refused: gate the whole file or a region
that contains the whole block.

Refused models can always be gated on text with a region:

```sql
-- keystone:start(finance, hash=text): revenue-recognition
    amount * 0.97 as net_revenue
-- keystone:end
```

## Macros

A `{% macro %}` in a model is refused. Files under `macros/` are SQL fragments
that no dialect parses as statements; gate them with `keystone(file)` or a
region on `hash=text`.
