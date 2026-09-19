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
}
_END = {"variable_end", "block_end", "comment_end"}


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
        elif ttype in _END:
            # `-%}` carries the whitespace it strips; the delimiter is the token.
            value = value.rstrip()
            pos = src.index(value, cursor)
            end = pos + len(value)
            out.append(_block(kind, src, start, end, inner_start, pos))
            cursor = end
            kind = None
        elif ttype in ("raw_begin", "raw_end"):
            # The whole `{% raw %}` tag arrives as one token.
            start = src.index(value, cursor)
            end = start + len(value)
            out.append(_block("statement", src, start, end, start + 2, end - 2))
            cursor = end
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
