# keystones-dbt

Gate a dbt model on the SQL it means, not on the text it is written as.

A [keystones](https://github.com/KyleJamesWalker/keystones) keystone pins a
review gate to an AST node. dbt models are Jinja, which no SQL grammar can
parse, so without a preprocessor they can only be gated on normalised text -
which trips on every reformat.

This is the preprocessor. It masks the templating so the SQL underneath parses,
and hands the masked content back so it is still gated.

## Install

```bash
pip install keystones-dbt
```

```toml
[[tool.keystones.language]]
builtin = "sql"          # or sql_bigquery
extensions = [".sql"]
preprocessor = "keystones_dbt:preprocess"
```

The dialect is yours to pick; this plugin never sees it.

## What it does

| Jinja | becomes |
|---|---|
| `{# comment #}` | removed |
| `{{ config(...) }}` alone on a line | removed |
| `{{ ref('orders') }}` inline | a placeholder derived from its content |
| `{% set x = 1 %}` | removed |
| `{% if %}`, `{% for %}` | **refused** |

Masked content is hashed verbatim, so changing `ref('orders')` to
`ref('payments')` trips the gate while reformatting the expression does not.

## Why control flow is refused

`{% if x %}a{% else %}b{% endif %}` masks to `ab` - a single nonsense
identifier that parses cleanly. Worse, `{% if x %}a{% endif %}b` and
`a{% if x %}b{% endif %}` mask identically, so a real change between them would
not move the hash. A review gate that misses a change is worse than one that
refuses to guess.

Gate those models on text instead, with a region:

```sql
-- keystone:start(finance, hash=text): revenue-recognition
    amount * 0.97 as net_revenue
-- keystone:end
```

Across a 740-model dbt project, 80.3% of models contain only `{{ }}`
expressions and are handled here; 19.6% use control flow and need the region.
