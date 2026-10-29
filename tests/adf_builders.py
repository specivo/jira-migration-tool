"""Builders for Atlassian Document Format (ADF), the JSON Jira Cloud's editor stores.

Each function returns one node as a plain ``dict``, ready for ``/rest/api/3``.
Strings given where a block is expected become paragraphs, and strings given
where inline content is expected become text nodes, so content reads close to
what a person types::

    doc(heading(2, "Steps"), ordered("Open the cart", "Press Pay"), p("It fails ", bold("every"), " time."))

Ids an ADF node needs (``localId`` of tasks, decisions and status lozenges) are
derived from the node's text, so the same content gives the same document.
Jira refuses empty text nodes, so building one raises ``ValueError``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any, Literal

type Node = dict[str, Any]
type Inline = Node | str
type Block = Node | str

_LOCAL_ID_NAMESPACE = uuid.UUID("5f0c8f1e-3a6b-4c1d-9e2f-7a8b9c0d1e2f")

PanelKind = Literal["info", "note", "warning", "error", "success"]
StatusColour = Literal["neutral", "purple", "blue", "red", "yellow", "green"]

# Marks.
STRONG: Node = {"type": "strong"}
EM: Node = {"type": "em"}
UNDERLINE: Node = {"type": "underline"}
STRIKE: Node = {"type": "strike"}
CODE: Node = {"type": "code"}
SUB: Node = {"type": "subsup", "attrs": {"type": "sub"}}
SUP: Node = {"type": "subsup", "attrs": {"type": "sup"}}


def colour_mark(colour: str) -> Node:
    return {"type": "textColor", "attrs": {"color": colour}}


def link_mark(href: str) -> Node:
    return {"type": "link", "attrs": {"href": href}}


def local_id(*parts: object) -> str:
    """A stable id for a node, from what it says."""
    return str(uuid.uuid5(_LOCAL_ID_NAMESPACE, "\x1f".join(str(part) for part in parts)))


# ----------------------------------------------------------------------
# Inline nodes
# ----------------------------------------------------------------------


def text(value: str, *marks: Node) -> Node:
    if not value:
        raise ValueError("ADF text nodes cannot be empty")
    node: Node = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def bold(value: str) -> Node:
    return text(value, STRONG)


def italic(value: str) -> Node:
    return text(value, EM)


def underline(value: str) -> Node:
    return text(value, UNDERLINE)


def strike(value: str) -> Node:
    return text(value, STRIKE)


def code(value: str) -> Node:
    return text(value, CODE)


def sub(value: str) -> Node:
    return text(value, SUB)


def sup(value: str) -> Node:
    return text(value, SUP)


def coloured(value: str, colour: str) -> Node:
    return text(value, colour_mark(colour))


def link(value: str, href: str, *marks: Node) -> Node:
    return text(value, link_mark(href), *marks)


def hard_break() -> Node:
    return {"type": "hardBreak"}


def emoji(short_name: str, char: str) -> Node:
    """A standard emoji: ``emoji(":rocket:", "🚀")``. Its id is the code points in hex."""
    code_points = "-".join(f"{ord(c):x}" for c in char if ord(c) != 0xFE0F)
    return {"type": "emoji", "attrs": {"shortName": short_name, "id": code_points, "text": char}}


def mention(account_id: str) -> Node:
    """A user mention. Jira fills in the display name itself."""
    return {"type": "mention", "attrs": {"id": account_id, "text": "@user"}}


def date_node(day: date) -> Node:
    """A date lozenge: midnight UTC of *day*, in milliseconds, as a string."""
    moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return {"type": "date", "attrs": {"timestamp": str(int(moment.timestamp()) * 1000)}}


def status(label: str, colour: StatusColour) -> Node:
    return {"type": "status", "attrs": {"text": label, "color": colour, "localId": local_id("status", label, colour)}}


def inline_card(url: str) -> Node:
    """A smart link: Jira shows the linked issue (or page) as a card."""
    return {"type": "inlineCard", "attrs": {"url": url}}


def _inline(items: Iterable[Inline]) -> list[Node]:
    return [text(item) if isinstance(item, str) else item for item in items]


def _blocks(items: Iterable[Block]) -> list[Node]:
    return [p(item) if isinstance(item, str) else item for item in items]


# ----------------------------------------------------------------------
# Blocks
# ----------------------------------------------------------------------


def doc(*blocks: Block) -> Node:
    return {"type": "doc", "version": 1, "content": _blocks(blocks)}


def p(*content: Inline) -> Node:
    node: Node = {"type": "paragraph", "content": _inline(content)}
    return node


def heading(level: int, *content: Inline) -> Node:
    if not 1 <= level <= 6:
        raise ValueError(f"heading level {level} is not 1 to 6")
    return {"type": "heading", "attrs": {"level": level}, "content": _inline(content)}


def item(*content: Block) -> Node:
    """One list item: paragraphs, and lists nested in it."""
    return {"type": "listItem", "content": _blocks(content)}


def _items(entries: Iterable[Node | str]) -> list[Node]:
    return [entry if isinstance(entry, dict) and entry.get("type") == "listItem" else item(entry) for entry in entries]


def bullets(*entries: Node | str) -> Node:
    return {"type": "bulletList", "content": _items(entries)}


def ordered(*entries: Node | str, start: int = 1) -> Node:
    node: Node = {"type": "orderedList", "content": _items(entries)}
    if start != 1:
        node["attrs"] = {"order": start}
    return node


def tasks(*entries: tuple[bool, str | list[Inline]]) -> Node:
    """A task list: ``tasks((True, "Done already"), (False, "Still to do"))``."""
    nodes = []
    for index, (done, content) in enumerate(entries):
        inline = _inline([content] if isinstance(content, str) else content)
        nodes.append(
            {
                "type": "taskItem",
                "attrs": {"localId": local_id("task", index, inline), "state": "DONE" if done else "TODO"},
                "content": inline,
            }
        )
    return {"type": "taskList", "attrs": {"localId": local_id("tasks", nodes)}, "content": nodes}


def decisions(*entries: str) -> Node:
    nodes = [
        {
            "type": "decisionItem",
            "attrs": {"localId": local_id("decision", index, entry), "state": "DECIDED"},
            "content": [text(entry)],
        }
        for index, entry in enumerate(entries)
    ]
    return {"type": "decisionList", "attrs": {"localId": local_id("decisions", entries)}, "content": nodes}


def code_block(source: str, language: str | None = None) -> Node:
    node: Node = {"type": "codeBlock", "content": [text(source)]}
    if language:
        node["attrs"] = {"language": language}
    return node


def quote(*content: Block) -> Node:
    return {"type": "blockquote", "content": _blocks(content)}


def rule() -> Node:
    return {"type": "rule"}


def panel(kind: PanelKind, *content: Block) -> Node:
    return {"type": "panel", "attrs": {"panelType": kind}, "content": _blocks(content)}


def expand(title: str, *content: Block) -> Node:
    return {"type": "expand", "attrs": {"title": title}, "content": _blocks(content)}


def media_single(media_id: str, *, width: int | None = None) -> Node:
    """An image (or other file) attached to the issue, shown in the text, by its media id.

    *width* is a percentage of the text column (Jira refuses a pixel width without ``widthType``).
    """
    if width is not None and not 0 < width <= 100:
        raise ValueError(f"media width {width} is not a percentage")
    media: Node = {"type": "media", "attrs": {"type": "file", "id": media_id, "collection": ""}}
    attrs: Node = {"layout": "center"}
    if width is not None:
        attrs["width"] = width
    return {"type": "mediaSingle", "attrs": attrs, "content": [media]}


def cell(*content: Block, colspan: int = 1, rowspan: int = 1, header: bool = False) -> Node:
    attrs: Node = {}
    if colspan != 1:
        attrs["colspan"] = colspan
    if rowspan != 1:
        attrs["rowspan"] = rowspan
    return {"type": "tableHeader" if header else "tableCell", "attrs": attrs, "content": _blocks(content)}


def th(*content: Block, colspan: int = 1, rowspan: int = 1) -> Node:
    return cell(*content, colspan=colspan, rowspan=rowspan, header=True)


def row(*cells: Node | str) -> Node:
    """A table row; anything that is not already a cell (a string, a paragraph, a list) becomes one."""
    content = [c if isinstance(c, dict) and c["type"] in ("tableCell", "tableHeader") else cell(c) for c in cells]
    return {"type": "tableRow", "content": content}


def table(*rows: Node) -> Node:
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"}, "content": list(rows)}


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def walk(node: Node) -> Iterable[Node]:
    """Every node of a document, depth first, the node itself first."""
    yield node
    for child in node.get("content") or []:
        yield from walk(child)


def node_types(node: Node) -> set[str]:
    return {n["type"] for n in walk(node)}


def mark_types(node: Node) -> set[str]:
    return {m["type"] for n in walk(node) for m in n.get("marks") or []}


def plain_text(node: Node) -> str:
    return "".join(str(n.get("text") or "") for n in walk(node))
