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
    children: list[object] = field(default_factory=list)


def _tree(items: list[Block]) -> list[object]:
    """Nest control blocks; refuse anything that opens a block we cannot mask."""
    root: list[object] = []
    stack: list[_Control] = []

    def parent() -> list[object]:
        return stack[-1].children if stack else root

    for block in items:
        if block.kind != "statement":
            parent().append(block)
            continue
        tag = block.tag
        if tag in CONTROL_TAGS:
            node = _Control(block)
            parent().append(node)
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
            parent().append(block)
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


def _is_directive(src: str, block: Block) -> bool:
    """`config()` alone on its line sits where a statement belongs."""
    if not block.body.startswith("config("):
        return False
    line_start = src.rfind("\n", 0, block.start) + 1
    line_end = src.find("\n", block.end)
    line_end = len(src) if line_end == -1 else line_end
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
                return
            spans.append(node.body)
            if node.kind == "expression" and not _is_directive(src, node):
                cut(node.start, node.end, _placeholder(node.body))
            else:
                cut(node.start, node.end)
            return
        assert isinstance(node, _Control)
        if control_flow == "refuse":
            raise Refused(
                f"this model uses Jinja control flow ({{% {node.opener.tag} %}}), "
                "which cannot be masked without changing what the SQL means"
            )
        spans.append(" ".join(src[node.opener.start : node.closer.end].split()))
        if control_flow == "drop":
            cut(node.opener.start, node.closer.end)
            return
        cut(node.opener.start, node.opener.end)
        kept_end = node.middles[0].start if node.middles else node.closer.start
        cut(kept_end, node.closer.end)
        for child in node.children:
            first = child.start if isinstance(child, Block) else child.opener.start
            if node.opener.end <= first < kept_end:
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
