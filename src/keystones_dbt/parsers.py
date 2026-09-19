"""A keystones parser plugin over sqlglot, so any dialect it knows can be gated.

    parser = { plugin = "keystones_dbt.parsers:sqlglot", dialect = "snowflake" }

Definitions are CTEs and CREATE statements. Comment lines come from a small
quote-aware scan rather than from sqlglot, which attaches a comment to the
token after it and loses the line it was written on.
"""

from __future__ import annotations

import re
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
        # A CTE slice arrives without a query to belong to, and the first one
        # arrives with the WITH. Wrapping it gives the same node either way.
        body = re.sub(r"^\s*with\s+", "", src, count=1, flags=re.IGNORECASE)
        body = re.sub(r",\s*$", "", body)
        wrapped = f"with {body}\nselect 1"
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

    def _collect(self) -> None:
        from sqlglot import exp

        for statement in self.statements:
            prefix = ""
            if isinstance(statement, exp.Create):
                name = statement.this.sql(dialect=self.dialect).replace('"', "")
                self._defs.append((Definition(name, *self._span(statement)), statement))
                prefix = name + "."
            for cte in statement.find_all(exp.CTE):
                self._defs.append(
                    (Definition(prefix + cte.alias, *self._span(cte)), cte)
                )
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
            i += 1
        return start, end_line

    def definitions(self) -> list[Definition]:
        return [d for d, _ in self._defs]

    def comments(self) -> list[tuple[int, str]]:
        return _comment_lines(self.src)

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
                out.append((line + offset, text))
            line += src.count("\n", i, end)
            i = end
        else:
            if ch == "\n":
                line += 1
            i += 1
    return out
