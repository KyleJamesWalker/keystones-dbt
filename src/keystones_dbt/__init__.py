"""Mask dbt's Jinja so the SQL underneath can be parsed and hashed.

A keystones preprocessor. See `keystones.preprocess` for the contract this
implements; the rules that matter here are that the mask is line-preserving,
that a placeholder derives from its span's content rather than its position,
and that anything the mask removes is handed back so it is still gated.
"""

from __future__ import annotations

import hashlib
import re

from keystones.preprocess import Refused

KEYSTONES_PREPROCESSOR_NAME = "dbt"
KEYSTONES_PREPROCESSOR_VERSION = "1"

COMMENT = re.compile(r"\{#.*?#\}", re.S)
# `config()` alone on its line sits where a statement belongs, so a placeholder
# there is not valid SQL. The body is tempered so the match ends at the first
# `}}`: a lazy dot anchored to `$` would backtrack across other expressions.
DIRECTIVE = re.compile(
    r"(?m)^[ \t]*\{\{(\s*config\s*\((?:(?!\}\}).)*)\}\}[ \t]*(?:--[^\n]*)?$", re.S
)
EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.S)
TAG = re.compile(r"\{%-?(.*?)-?%\}", re.S)
STRING = re.compile(r"'[^']*'|\"[^\"]*\"")

# Tags that only bind names. Anything else opens a block or a branch: branches
# concatenate into something that parses and means something else, and
# `{% if x %}a{% endif %}b` masks identically to `a{% if x %}b{% endif %}`, so
# a real change between them would not move the hash. A block's body would be
# left behind as if it were SQL.
STATEMENT_TAGS = frozenset({"do", "import", "from"})


def _normalise(body: str) -> str:
    return " ".join(body.split())


def _placeholder(body: str) -> str:
    """Content-derived, so a slice of a file masks the same as the whole file."""
    return "__ks_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _guard(body: str) -> None:
    # `{{ "}}" }}` closes the span early and the residue still parses, so the
    # mis-split would pass silently.
    if "'" in STRING.sub("", body) or '"' in STRING.sub("", body):
        raise Refused(
            "a Jinja expression here has an unbalanced quote, so where it ends "
            "cannot be determined by masking alone"
        )


def _guard_tag(body: str) -> None:
    word = body.split(" ", 1)[0]
    if word in STATEMENT_TAGS or (word == "set" and "=" in body):
        return
    raise Refused(
        f"this model uses Jinja control flow or a block tag ({{% {word} %}}), "
        "which cannot be masked without changing what the SQL means"
    )


def preprocess(src: str) -> tuple[str, str]:
    """Return the SQL to parse, and the templating that was taken out of it."""
    spans: list[str] = []

    def replace(with_placeholder: bool, tag: bool = False):
        def apply(match: re.Match[str]) -> str:
            body = _normalise(match.group(1))
            _guard(body)
            if tag:
                _guard_tag(body)
            spans.append(body)
            # The mask has to hold the lines it consumed, or every target below
            # it moves.
            padding = "\n" * match.group(0).count("\n")
            return (_placeholder(body) if with_placeholder else "") + padding

        return apply

    out = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), src)
    out = DIRECTIVE.sub(replace(with_placeholder=False), out)
    out = EXPRESSION.sub(replace(with_placeholder=True), out)
    out = TAG.sub(replace(with_placeholder=False, tag=True), out)
    return out, "\n".join(spans)
