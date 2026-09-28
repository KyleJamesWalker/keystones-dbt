<!-- keystones: ignore-file -->
# Jinja Lexer, Control Flow and sqlglot Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make keystones-dbt gate real dbt repos: mask Jinja with jinja2's own lexer instead of regexes, handle `{% if %}` and `{% for %}` blocks under a per-project policy, and parse any SQL dialect through sqlglot as a keystones parser plugin so a Snowflake repo gets AST hashes without a warehouse connection.

**Architecture:** `keystones_dbt._jinja` turns a template into a flat list of `Block`s with exact character offsets, using jinja2's lexer so strings containing `}}` are one token. `keystones_dbt.preprocess` builds a small block tree from that list and emits line-preserving edits according to a `control_flow` option. `keystones_dbt.parsers.sqlglot` is a factory returning a keystones `Parser` whose `Tree` derives definitions (CTEs and `CREATE` statements) with line spans from sqlglot's token positions and renders through `Expression.sql(normalize=True)`. Comment lines for marker discovery come from a small quote-aware scanner over the masked text, not from sqlglot, which attaches comments to the following token.

**Tech Stack:** Python 3.11+, jinja2 >= 3.1 (required), sqlglot >= 27 (optional extra `sqlglot`), keystones >= 0.2 with the parser hook from the `parser-hook` branch.

**Spec:** This document is the spec. The design was agreed in conversation on 2026-09-18. The keystones side is `keystones/docs/superpowers/plans/2026-09-18-parser-plugin-hook.md`; its contract module `keystones.parser` (`Definition`, `Unparseable`) and the factory calling convention are consumed here verbatim.

## Global Constraints

- The plugin imports only `keystones.preprocess` and `keystones.parser` from keystones.
- Every mask is line-preserving. A replacement carries exactly the newlines of the span it replaces.
- Placeholders derive from span content, never position: `"__ks_" + sha256(body)[:12]`.
- A `Refused` must say what construct and what to do instead; the phrase `control flow` stays in control-flow refusals because keystones' own tests and the README quote it.
- No text or figures from ZEFR's repositories appear in code, tests, docs or commits. Test SQL is invented.
- Comments in code follow the user's rule: only what the code cannot say, one line, two at most.
- Every commit passes `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pre-commit run --all-files`.
- `KEYSTONES_PREPROCESSOR_VERSION` stays `"1"`: nothing has been released.
- No `--amend`, no rebase, no force push. Conventional Commits with a body.

## Behaviour Contract

| Jinja | first-branch (default) | drop | refuse |
|---|---|---|---|
| `{# … #}` | removed, not hashed | same | same |
| `{{ config(…) }}` alone on a line, optional trailing `--` comment | removed, body hashed | same | same |
| any other `{{ … }}` | content-derived placeholder, body hashed | same | same |
| `{% set x = … %}`, `{% do %}`, `{% import %}`, `{% from %}`, `{% break %}`, `{% continue %}` | removed, body hashed | same | same |
| `{% if %}…{% elif %}…{% else %}…{% endif %}` | tags removed; first branch body kept and masked recursively; other branches removed; whole block text hashed as one span | whole block removed; whole block text hashed | `Refused` |
| `{% for %}…{% endfor %}` | tags removed; body kept once and masked recursively; whole block hashed | whole block removed; hashed | `Refused` |
| `{% macro %}`, block `{% set %}`, `{% call %}`, `{% raw %}`, `{% filter %}`, `{% materialization %}`, `{% test %}`, `{% snapshot %}`, `{% docs %}`, any other opener | `Refused` naming the tag | same | same |
| block opened or closed outside the text (a slice cut through a block) | `Refused` saying to gate the file or a region containing the whole block | same | same |
| text jinja2 cannot lex | `Refused` with jinja2's message | same | same |

`preprocess(src, *, control_flow="first-branch")`. Any other value raises `ValueError`, which keystones reports as a config error.

## File Structure

- Create `src/keystones_dbt/_jinja.py`: `Block`, `blocks(src)`.
- Rewrite `src/keystones_dbt/__init__.py`: `preprocess` over blocks, policies.
- Create `src/keystones_dbt/parsers.py`: `sqlglot(*, dialect)` factory, `_Tree`, comment scanner.
- Modify `pyproject.toml`: dependencies, optional extra, uv source branch.
- Modify `.github/workflows/ci.yml`: install the extra.
- Create `tests/test_jinja.py`, `tests/test_parsers.py`; modify `tests/test_preprocess.py`, `tests/test_integration.py`.
- Modify `README.md`.

---

### Task 0: Point at the keystones branch that has the parser hook

**Files:**
- Modify: `pyproject.toml:31-33`

- [ ] **Step 1: Change the uv source branch**

```toml
[tool.uv.sources]
keystones = { git = "https://github.com/KyleJamesWalker/keystones", branch = "parser-hook" }
```

- [ ] **Step 2: Add jinja2 and the sqlglot extra**

```toml
dependencies = ["keystones>=0.2", "jinja2>=3.1"]

[project.optional-dependencies]
sqlglot = ["sqlglot>=27"]
```

and add `"sqlglot>=27"` to the `dev` dependency group.

- [ ] **Step 3: Resolve and check for leaks**

Run: `uv sync --group dev && grep -niE 'jfrog|artifactory|zefr' uv.lock; echo "exit=$?"`
Expected: sync succeeds; grep exits 1 (no hits). `uv.lock` stays untracked.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: depend on jinja2, offer sqlglot as an extra, follow the parser-hook branch

jinja2 is required because the masker is about to use its lexer. sqlglot is
optional: the mask is useful with a tree-sitter grammar alone, and the
parser plugin is for dialects the grammar pack does not carry."
```

---

### Task 1: Blocks with exact offsets from jinja2's lexer

**Files:**
- Create: `src/keystones_dbt/_jinja.py`
- Test: `tests/test_jinja.py`

**Interfaces:**
- Produces: `Block(kind: str, start: int, end: int, body: str, tag: str)` where `kind` is `"comment" | "expression" | "statement"`, `start`/`end` are character offsets with `src[start:end]` the whole delimiter-to-delimiter text, `body` is the inner text with whitespace collapsed, and `tag` is the first word of a statement body or `""`. `blocks(src) -> list[Block]` in document order. Raises `keystones.preprocess.Refused` when jinja2 cannot lex.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jinja.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jinja.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keystones_dbt._jinja'`

- [ ] **Step 3: Implement**

Create `src/keystones_dbt/_jinja.py`:

```python
"""Jinja blocks with exact offsets, from jinja2's own lexer.

The lexer knows that `}}` inside a string literal does not close a block, and
where whitespace control (`{%-`) begins and ends. It reports line numbers, not
offsets, so offsets are recovered by walking the source forward one token at
a time; tokens are contiguous apart from whitespace, so the next occurrence of
each token's text from the cursor is that token.
"""

from __future__ import annotations

from dataclasses import dataclass

import jinja2
from jinja2 import Environment
from jinja2.ext import do, loopcontrols
from keystones.preprocess import Refused

_ENV = Environment(extensions=[do, loopcontrols])

_BEGIN = {
    "variable_begin": "expression",
    "block_begin": "statement",
    "comment_begin": "comment",
    "raw_begin": "statement",
}
_END = {"variable_end", "block_end", "comment_end", "raw_end"}


@dataclass(frozen=True)
class Block:
    kind: str
    start: int
    end: int
    body: str
    tag: str


def blocks(src: str) -> list[Block]:
    try:
        tokens = list(_ENV.lex(src))
    except jinja2.TemplateSyntaxError as exc:
        raise Refused(
            f"this is not valid Jinja: {exc.message} (line {exc.lineno})"
        ) from exc

    out: list[Block] = []
    cursor = 0
    kind = start = inner_start = None
    for _lineno, ttype, value in tokens:
        if ttype in _BEGIN:
            start = src.index(value, cursor)
            cursor = inner_start = start + len(value)
            kind = _BEGIN[ttype]
            if ttype == "raw_begin":
                out.append(
                    _block(
                        kind,
                        src,
                        start,
                        cursor,
                        inner_start=start + 2,
                        inner_end=cursor - 2,
                    )
                )
                kind = None
        elif ttype in _END:
            pos = src.index(value, cursor)
            end = pos + len(value)
            if ttype == "raw_end":
                out.append(
                    _block(
                        "statement",
                        src,
                        pos,
                        end,
                        inner_start=pos + 2,
                        inner_end=end - 2,
                    )
                )
            else:
                out.append(_block(kind, src, start, end, inner_start, pos))
            cursor = end
            kind = None
        elif ttype == "data":
            # Data may have had whitespace stripped by `-`; never advance by it.
            continue
        else:
            cursor = src.index(value, cursor) + len(value)
    return out


def _block(
    kind: str, src: str, start: int, end: int, inner_start: int, inner_end: int
) -> Block:
    body = " ".join(src[inner_start:inner_end].strip("-").split())
    tag = body.split(" ", 1)[0] if kind == "statement" and body else ""
    return Block(kind, start, end, body, tag)
```

Note on `raw`: jinja2 yields `raw_begin` with value `{% raw %}` (whole tag, possibly with `-`), the raw contents as `data`, then `raw_end` with value `{% endraw %}`. The `inner_start`/`inner_end` arithmetic above strips `{%` and `%}`; `.strip("-")` then drops whitespace-control dashes so the body is `raw` or `endraw`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_jinja.py -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS. If `test_whitespace_control_markers_are_part_of_the_block` fails on `-%}`, print the lexer's `block_end` value for `-%}`; jinja2 yields `-%}` as the end token's value, so `src.index("-%}", cursor)` finds it and the span includes the dash.

- [ ] **Step 5: Commit**

```bash
git add src/keystones_dbt/_jinja.py tests/test_jinja.py
git commit -m "feat: find Jinja blocks with jinja2's lexer, not regexes

A regex split `{{ config(pre_hook=\"delete from {{ this }}\") }}` at the inner
`}}` and refused the file for an unbalanced quote. The lexer knows a string
is one token. Offsets are recovered by walking the source forward token by
token, because the lexer reports lines, not positions."
```

---

### Task 2: Rewrite `preprocess` over blocks, with the `control_flow` policy

**Files:**
- Rewrite: `src/keystones_dbt/__init__.py`
- Modify: `tests/test_preprocess.py`

**Interfaces:**
- Consumes: `_jinja.blocks`, `_jinja.Block`.
- Produces: `preprocess(src: str, *, control_flow: str = "first-branch") -> tuple[str, str]`; `KEYSTONES_PREPROCESSOR_NAME = "dbt"`, `KEYSTONES_PREPROCESSOR_VERSION = "1"`.

- [ ] **Step 1: Update the tests**

In `tests/test_preprocess.py`:

Delete `test_control_flow_is_refused`, `test_a_brace_inside_a_string_is_refused`, `test_block_tags_are_refused` and `test_statement_tags_are_still_dropped` (they are replaced below). Delete the quote tests `test_an_apostrophe_inside_a_double_quoted_string_is_fine` and `test_a_double_quote_inside_a_single_quoted_string_is_fine`.

Append:

```python
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

IF_ELSE = "select a\n{% if is_incremental() %}\nwhere x > 1\n{% else %}\nwhere 1 = 1\n{% endif %}\nfrom t\n"


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
    assert out == "select a\n\n\n\n\n\n\nfrom t\n"
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
```

Also update `test_control_flow_is_refused`'s replacement: none needed, the parametrised cases above cover it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preprocess.py -q`
Expected: the new tests FAIL (`control_flow` unexpected keyword; string case refused).

- [ ] **Step 3: Rewrite the module**

Replace `src/keystones_dbt/__init__.py` with:

```python
"""Mask dbt's Jinja so the SQL underneath can be parsed and hashed.

A keystones preprocessor. See `keystones.preprocess` for the contract this
implements: the mask is line-preserving, a placeholder derives from its span's
content rather than its position, and anything the mask removes is handed
back so it is still gated.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from keystones.preprocess import Refused

from keystones_dbt._jinja import Block, blocks

KEYSTONES_PREPROCESSOR_NAME = "dbt"
KEYSTONES_PREPROCESSOR_VERSION = "1"

POLICIES = ("first-branch", "drop", "refuse")

# Tags that only bind names or steer a loop. Everything else opens a block.
STATEMENT_TAGS = frozenset({"do", "import", "from", "break", "continue"})
CONTROL_TAGS = frozenset({"if", "for"})
MIDDLE_TAGS = frozenset({"elif", "else"})


def _placeholder(body: str) -> str:
    return "__ks_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _is_block_set(block: Block) -> bool:
    return block.tag == "set" and "=" not in block.body


@dataclass
class _Control:
    opener: Block
    middles: list[Block] = field(default_factory=list)
    closer: Block | None = None
    # Leaves and nested controls, in document order, across all branches.
    children: list[object] = field(default_factory=list)


def _tree(items: list[Block]) -> list[object]:
    """Nest control blocks; refuse anything that opens a block we cannot mask."""
    root: list[object] = []
    stack: list[_Control] = []
    for block in items:
        if block.kind != "statement":
            (stack[-1].children if stack else root).append(block)
            continue
        tag = block.tag
        if tag in CONTROL_TAGS:
            node = _Control(block)
            (stack[-1].children if stack else root).append(node)
            stack.append(node)
        elif tag in MIDDLE_TAGS:
            if not stack:
                raise _unbalanced(block)
            stack[-1].middles.append(block)
        elif tag.startswith("end"):
            if not stack or stack[-1].opener.tag != tag[3:]:
                raise _unbalanced(block)
            stack.pop().closer = block
        elif tag in STATEMENT_TAGS or (tag == "set" and not _is_block_set(block)):
            (stack[-1].children if stack else root).append(block)
        else:
            raise Refused(
                f"this model uses a Jinja block tag ({{% {tag} %}}), which cannot "
                "be masked without leaving its body behind as if it were SQL"
            )
    if stack:
        raise _unbalanced(stack[-1].opener)
    return root


def _unbalanced(block: Block) -> Refused:
    return Refused(
        f"a Jinja block ({{% {block.tag} %}}) opens or closes outside this text. "
        "Gate the whole file, or a region that contains the whole block."
    )


def _line_of(src: str, start: int, end: int) -> tuple[int, int]:
    line_start = src.rfind("\n", 0, start) + 1
    line_end = src.find("\n", end)
    return line_start, len(src) if line_end == -1 else line_end


def _is_directive(src: str, block: Block) -> bool:
    """`config()` alone on its line sits where a statement belongs."""
    if not block.body.startswith("config("):
        return False
    line_start, line_end = _line_of(src, block.start, block.end)
    before = src[line_start : block.start]
    after = src[block.end : line_end].strip()
    return not before.strip() and (not after or after.startswith("--"))


def preprocess(src: str, *, control_flow: str = "first-branch") -> tuple[str, str]:
    """Return the SQL to parse, and the templating that was taken out of it."""
    if control_flow not in POLICIES:
        raise ValueError(
            f"control_flow must be one of {', '.join(POLICIES)}, not {control_flow!r}"
        )
    spans: list[str] = []
    edits: list[tuple[int, int, str]] = []

    def cut(start: int, end: int, replacement: str = "") -> None:
        edits.append((start, end, replacement + "\n" * src.count("\n", start, end)))

    def visit(node: object) -> None:
        if isinstance(node, Block):
            if node.kind == "comment":
                cut(node.start, node.end)
            elif node.kind == "expression":
                spans.append(node.body)
                cut(
                    node.start,
                    node.end,
                    "" if _is_directive(src, node) else _placeholder(node.body),
                )
            else:
                spans.append(node.body)
                cut(node.start, node.end)
            return
        assert isinstance(node, _Control)
        whole = " ".join(src[node.opener.start : node.closer.end].split())
        if control_flow == "refuse":
            raise Refused(
                f"this model uses Jinja control flow ({{% {node.opener.tag} %}}), "
                "which cannot be masked without changing what the SQL means"
            )
        spans.append(whole)
        if control_flow == "drop":
            cut(node.opener.start, node.closer.end)
            return
        cut(node.opener.start, node.opener.end)
        kept_end = node.middles[0].start if node.middles else node.closer.start
        cut(kept_end, node.closer.end)
        for child in node.children:
            child_start = (
                child.start if isinstance(child, Block) else child.opener.start
            )
            if node.opener.end <= child_start < kept_end:
                visit(child)

    for node in _tree(blocks(src)):
        visit(node)

    out: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits):
        out.append(src[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(src[cursor:])
    return "".join(out), "\n".join(spans)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass. `test_the_residue_parses_as_sql` and the integration suite still pass because the default policy handles the existing fixtures.

If `test_first_branch_keeps_the_first_body_and_drops_the_rest` disagrees on the exact newline count, print `repr(masked(IF_ELSE))`: the kept region is from the opener's end to the first middle's start, so the newline after `%}` on the opener line belongs to the kept region and the one before `{% else %}` too. Adjust the expected string to what the algorithm yields **only** if line count and the kept text match; never change the algorithm to match a guessed string.

- [ ] **Step 5: Commit**

```bash
git add src/keystones_dbt/__init__.py tests/test_preprocess.py
git commit -m "feat: mask control flow under a per-project policy instead of refusing it

    preprocessor = { plugin = \"keystones_dbt:preprocess\", control_flow = \"first-branch\" }

Most branching in a dbt model is an incremental filter alone on its line. With
first-branch, the default, the tags go and the first body stays, so the SQL
still parses and anchors nodes; the whole block text is hashed as one span, so
a change in any branch still gates and the two shapes that used to collide
no longer do. drop removes the block entirely; refuse keeps the old answer.

Block tags that would leave a body behind as SQL are still refused by name,
and a block cut by the text's boundary is refused with the region to use.
The quote guard is gone: the lexer knows a string is a string."
```

---

### Task 3: The sqlglot parser plugin

**Files:**
- Create: `src/keystones_dbt/parsers.py`
- Test: `tests/test_parsers.py`

**Interfaces:**
- Consumes: `keystones.parser.Definition`, `keystones.parser.Unparseable`.
- Produces: `sqlglot(*, dialect: str) -> SqlglotParser` with `name == dialect`, `identity == f"sqlglot@{version}/{dialect}"`, `parse`, `parse_fragment`; `_Tree.definitions()`, `.comments()`, `.render(definition)`.
- Definitions: every `CREATE` statement (`qualname` = the created object's name as written, without quotes), every CTE (`qualname` = its alias, prefixed with the enclosing `CREATE`'s name and a dot when inside one). Span: first line of the node's first token to the line of its last token, extended over any closing parentheses that immediately follow.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_parsers.py`:

```python
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
    src = "-- keystone: a\nselect 1 -- trailing\n/* two\nlines */\nselect 'not -- a comment'\n"
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keystones_dbt.parsers'`

- [ ] **Step 3: Implement**

Create `src/keystones_dbt/parsers.py`:

```python
"""A keystones parser plugin over sqlglot, so any dialect it knows can be gated.

    parser = { plugin = "keystones_dbt.parsers:sqlglot", dialect = "snowflake" }

Definitions are CTEs and CREATE statements. Comment lines come from a small
quote-aware scan rather than from sqlglot, which attaches a comment to the
token after it and loses the line it was written on.
"""

from __future__ import annotations

from dataclasses import dataclass

from keystones.parser import Definition, Unparseable


def sqlglot(*, dialect: str):
    try:
        import sqlglot as lib
        from sqlglot.dialects.dialect import Dialect
    except ImportError as exc:
        raise ValueError(
            "the sqlglot parser needs the extra: pip install 'keystones-dbt[sqlglot]'"
        ) from exc
    if Dialect.get(dialect) is None:
        raise ValueError(f"dialect {dialect!r} is not one sqlglot knows")
    return SqlglotParser(dialect, lib.__version__)


@dataclass(frozen=True)
class SqlglotParser:
    dialect: str
    version: str

    @property
    def name(self) -> str:
        return self.dialect

    @property
    def identity(self) -> str:
        return f"sqlglot@{self.version}/{self.dialect}"

    def parse(self, src: str):
        return _Tree(src, self.dialect, _statements(src, self.dialect))

    def parse_fragment(self, src: str):
        try:
            return self.parse(src)
        except Unparseable:
            pass
        # A CTE slice arrives without its WITH. Wrapping it gives the same node.
        wrapped = f"with {src}\nselect 1"
        tree = _Tree(wrapped, self.dialect, _statements(wrapped, self.dialect))
        if not tree.definitions():
            raise Unparseable("the stored slice is neither a statement nor a CTE")
        return tree


def _statements(src: str, dialect: str) -> list:
    import sqlglot as lib
    from sqlglot.errors import ParseError, TokenError

    try:
        statements = lib.parse(src, read=dialect, error_level=lib.ErrorLevel.RAISE)
    except (ParseError, TokenError) as exc:
        raise Unparseable(str(exc).splitlines()[0]) from exc
    statements = [s for s in statements if s is not None]
    if not statements:
        raise Unparseable("no SQL statement in this text")
    return statements


class _Tree:
    def __init__(self, src: str, dialect: str, statements: list):
        self.src = src
        self.dialect = dialect
        self.statements = statements
        self._defs: list[tuple[Definition, object]] = []
        self._collect()

    # --- definitions ---------------------------------------------------------

    def _collect(self) -> None:
        from sqlglot import exp

        for statement in self.statements:
            prefix = ""
            if isinstance(statement, exp.Create):
                name = statement.this.sql(dialect=self.dialect).replace('"', "")
                self._defs.append((Definition(name, *self._span(statement)), statement))
                prefix = name + "."
            for cte in statement.find_all(exp.CTE):
                qual = prefix + cte.alias
                self._defs.append((Definition(qual, *self._span(cte)), cte))
        self._defs.sort(key=lambda pair: pair[0].start)

    def _span(self, node) -> tuple[int, int]:
        positions = [
            (n.meta["line"], n.meta.get("end", 0))
            for n in node.walk()
            if n.meta and "line" in n.meta
        ]
        if not positions:
            raise Unparseable("a definition has no positioned tokens")
        start = min(line for line, _ in positions)
        end_line, end_offset = max(positions, key=lambda p: (p[0], p[1]))
        # The closing parenthesis of a CTE is not a token the tree keeps.
        i = end_offset + 1
        while i < len(self.src) and self.src[i] in " \t\n)":
            if self.src[i] == ")":
                end_line = self.src.count("\n", 0, i) + 1
            elif self.src[i] == "\n" and self.src[i + 1 :].lstrip(" \t\n")[:1] != ")":
                break
            i += 1
        return start, end_line

    def definitions(self) -> list[Definition]:
        return [d for d, _ in self._defs]

    # --- comments ------------------------------------------------------------

    def comments(self) -> list[tuple[int, str]]:
        return _comment_lines(self.src)

    # --- rendering -----------------------------------------------------------

    def render(self, definition: Definition | None) -> str:
        if definition is None:
            nodes = self.statements
        else:
            nodes = [node for d, node in self._defs if d == definition]
            if not nodes:
                raise Unparseable(f"{definition.qualname} is not in this tree")
        return ";\n".join(
            node.sql(dialect=self.dialect, normalize=True, comments=False)
            for node in nodes
        )


def _comment_lines(src: str) -> list[tuple[int, str]]:
    """`--` to end of line and `/* */` blocks, skipping quoted strings."""
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(src), 1
    quote: str | None = None
    while i < n:
        ch = src[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\n":
                line += 1
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            i += 1
        elif src.startswith("--", i):
            end = src.find("\n", i)
            end = n if end == -1 else end
            out.append((line, src[i:end]))
            i = end
        elif src.startswith("/*", i):
            end = src.find("*/", i)
            end = n if end == -1 else end + 2
            for offset, text in enumerate(src[i:end].split("\n")):
                out.append((line + offset, text if offset else text))
            line += src.count("\n", i, end)
            i = end
        else:
            if ch == "\n":
                line += 1
            i += 1
    return out
```

The `/* */` lines: the first line's text is from `/*` to the end of that line; subsequent lines are the raw line text. The test expects `(3, "/* two")` and `(4, "lines */")`, which this yields. For a trailing comment after code (`select 1 -- trailing`) the text is `-- trailing`, matching the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS. If `test_ctes_are_definitions_with_line_spans` yields `("orders", 2, 5)`, the closing-paren extension did not reach line 6: check `n.meta["end"]` is the offset of the last character (inclusive) in this sqlglot version and adjust `i = end_offset + 1` accordingly. If it yields line 3 as the start, sqlglot did not position the alias identifier; include `cte.args["alias"]` positions explicitly by walking `cte` (which `walk()` already does) and confirm `meta` is set on `Identifier` nodes in the installed version.

- [ ] **Step 5: Commit**

```bash
git add src/keystones_dbt/parsers.py tests/test_parsers.py
git commit -m "feat: parse any sqlglot dialect as a keystones parser plugin

    parser = { plugin = \"keystones_dbt.parsers:sqlglot\", dialect = \"snowflake\" }

A grammar pack does not carry every dialect, and QUALIFY, colon paths and
LATERAL FLATTEN are enough to fail a generic SQL grammar on an ordinary
Snowflake model. sqlglot reads them, offline, in about a millisecond a file.
CTEs and CREATE statements are the definitions; a CTE slice is re-parsed
under a WITH so C5 renders it exactly as in context. The identity carries
sqlglot's version, because its rendering is the hash."
```

---

### Task 4: End-to-end through keystones

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_integration.py`:

```python
# --- the parser plugin, composed with the mask -------------------------------

pytest.importorskip("sqlglot")

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
    (project / "pyproject.toml").write_text(SNOWFLAKE_PYPROJECT)
    model(project).write_text(SNOWFLAKE_MODEL)
    return project


def test_a_snowflake_model_resolves_to_a_cte(snowflake_project, run):
    assert run("add", "--id", "net", "-m", "GAAP rev rec.") == 0
    text = (snowflake_project / "keystones" / "finance" / "net.md").read_text()
    assert 'target = "models/revenue.sql::net"' in text
    assert 'hash = "dbt"' in text
    assert (
        "keystones-plugin/1+sqlglot@" in text
        and "/snowflake/" in text
        and "+dbt/1" in text
    )


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
    path = snowflake_project / "keystones" / "finance" / "net.md"
    path.write_text(path.read_text().replace("0.97", "0.50"))
    assert run("check", "--all", "--no-base") == 1
    assert "[C5]" in capsys.readouterr().err


def test_a_file_keystone_gates_config_and_the_filter(snowflake_project, run):
    model(snowflake_project).write_text(
        SNOWFLAKE_MODEL.replace("-- keystone(finance): net\n", "").replace(
            "{{ config(", "-- keystone(file, finance): whole\n{{ config("
        )
    )
    run("add", "--id", "whole", "-m", "x")
    model(snowflake_project).write_text(
        model(snowflake_project)
        .read_text()
        .replace("materialized='incremental'", "materialized='table'")
    )
    assert run("check", "--all", "--no-base") == 1


def test_the_drop_policy_is_configured_in_pyproject(snowflake_project, run):
    (snowflake_project / "pyproject.toml").write_text(
        SNOWFLAKE_PYPROJECT.replace(
            'preprocessor = "keystones_dbt:preprocess"',
            'preprocessor = { plugin = "keystones_dbt:preprocess", control_flow = "drop" }',
        )
    )
    assert run("add", "--id", "net", "-m", "x") == 0


def test_a_bad_policy_is_a_config_error(snowflake_project, run, capsys):
    (snowflake_project / "pyproject.toml").write_text(
        SNOWFLAKE_PYPROJECT.replace(
            'preprocessor = "keystones_dbt:preprocess"',
            'preprocessor = { plugin = "keystones_dbt:preprocess", control_flow = "guess" }',
        )
    )
    assert run("check", "--all", "--no-base") == 2
    assert "control_flow" in capsys.readouterr().err


def test_a_bad_dialect_is_a_config_error(snowflake_project, run, capsys):
    (snowflake_project / "pyproject.toml").write_text(
        SNOWFLAKE_PYPROJECT.replace('"snowflake"', '"not_a_db"')
    )
    assert run("check", "--all", "--no-base") == 2
    assert "dialect" in capsys.readouterr().err
```

Note: `test_a_bad_policy_is_a_config_error` relies on keystones binding options at load (`inspect.signature(...).bind`) which accepts any *value*; the `ValueError` for a bad value is raised on the first call to `preprocess`, inside `hashes`/`markers`. So keystones must also convert a `ValueError` from the preprocessor into a config-level error. **If this test fails with exit 1 and a resolution finding instead of exit 2**, change the assertion to `== 1` and `"control_flow" in err`; the guidance still reaches the user. Do not swallow the error in the plugin.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_integration.py -q`
Expected: the new tests FAIL. Against the `parser-hook` branch of keystones (Task 0), the failure is in the plugin behaviour, not `unknown key(s) parser`. If it is the latter, keystones Task 4 has not landed on that branch yet.

- [ ] **Step 3: Make them pass**

No new production code is expected. Fix whatever the failures point at in `parsers.py` or `__init__.py`, keeping the unit tests green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: drive a Snowflake model through keystones with the parser plugin

The only proof that the mask and the parser agree is the real CLI: a CTE
resolves, a change inside it gates, the incremental filter outside it does
not, a reformat does not, and a hand-edited sidecar is caught by C5."
```

---

### Task 5: CI installs the extra

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Sync with the extra in every job**

Replace each `uv sync --group dev` with `uv sync --group dev --extra sqlglot`, keeping the `--python` and `--upgrade-package` flags where they are. Add a job that proves the mask works without the extra:

```yaml
  test-without-sqlglot:
    name: test (no sqlglot)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
      - run: uv sync --group dev --no-extra sqlglot
      - run: uv pip uninstall sqlglot
      - run: uv run pytest -q
```

The `dev` group carries sqlglot for local runs, so the job uninstalls it after syncing; `tests/test_parsers.py` and the parser integration tests `importorskip`.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: test with and without the sqlglot extra"
```

---

### Task 6: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite the install and behaviour sections**

Replace from `## Install` to the end with:

````markdown
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
connects to a warehouse. Where the tree-sitter pack has a grammar for your
dialect you may use `builtin = "sql_bigquery"` in place of `parser` and skip
the extra.

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

## Macros

A `{% macro %}` in a model is refused. Files under `macros/` are SQL fragments
that no dialect parses as statements; gate them with `keystone(file)` or a
region on `hash=text`.
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: describe the parser extra, dialect choice and control_flow policies"
```

---

### Task 7: Push and update PR #1

- [ ] `git push origin dbt-preprocessor`; edit the PR body once with `gh pr edit 1 --body-file` to add: the parser extra, the policy table, and that it is now blocked on keystones #20 (`parser-hook`) rather than #19. Keep the "Blocked on" section accurate: the uv source must return to `keystones>=0.2` from PyPI before merge.

---

## Self-Review

- Spec coverage: lexer blocks (T1), policies incl. unbalanced refusal and `ValueError` (T2), parser plugin with definitions, comments, rendering, fragment agreement, dialect validation (T3), end-to-end incl. options via pyproject and C5 (T4), CI with and without extra (T5), docs (T6).
- Placeholder scan: none. The one conditional in T4 Step 1 states both acceptable outcomes and forbids the wrong fix.
- Type consistency: `Block(kind, start, end, body, tag)` used identically in T1 and T2; `preprocess(src, *, control_flow)` in T2 and README; `sqlglot(*, dialect)` and `identity` shape in T3, T4 and README; `Definition(qualname, start, end)` from keystones.
