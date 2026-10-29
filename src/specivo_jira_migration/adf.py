"""Atlassian Document Format (ADF) to Markdown, for text read from Jira Cloud.

Jira Cloud's editor stores text as ADF, a JSON tree, and REST v3 returns it
as it is. Converting the tree here keeps what REST v2 loses when it turns ADF
into wiki markup first (smart links, panels, tasks, decisions, status
lozenges, dates, code languages, merged table cells).

The Markdown written is what Specivo renders: Python-Markdown with tables and
fenced code, and raw inline HTML through an allowlist. That decides several
choices:

- Nested lists are indented by four spaces. Two lists in a row at the top
  level are kept apart by an HTML comment, or they would merge into one.
- Fenced code works only at the top level. Inside a list item or a quote,
  code is an indented block, its language given on a ``:::lang`` first line.
- Strike, underline and sub/superscript are ``<del>``, ``<u>``, ``<sub>`` and
  ``<sup>``. Text and background colours are dropped, the text kept.
- Task items are ``- [ ]`` and ``- [x]``, decisions ``- **Decision:**``.
  Specivo shows the brackets as written; it has no checkboxes.
- Panels and expands are block quotes under a bold label: Specivo has no
  collapsible block (``<details>`` is not allowed through).
- Tables are Markdown tables. A merged cell is written unmerged, its text in
  the first of the cells it covered; block content in a cell is folded into
  one line with ``<br>``; a table inside a cell follows the table that held
  it. An HTML table would keep the merge, but Specivo does not render Markdown
  inside raw HTML, so links, mentions and issue keys in its cells would show
  as source.

Images and files shown in the text (``media`` nodes) are tied to the
issue's attachments by media file id (see :func:`media_markdown`) and
written as ``![name]({{attachment:REF}})`` or ``[name]({{attachment:REF}})``.

References follow the same rules as wiki markup (see :mod:`.convert`):
mentions become user placeholders, issue keys and links to an issue's
``/browse/KEY`` page (smart links included) become issue placeholders, never
inside code or a link's text. Plain text is escaped, so what a person typed
(``{code}``, ``[~user]``, ``--``, a literal ``{{issue:1}}``) stays literal.

A node or mark this converter does not know keeps its text and is counted per
type in the manifest. Nothing is ever written as JSON.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from specivo_migration_sdk import placeholders

from specivo_jira_migration.convert import (
    ISSUE_KEY_RE,
    TextContext,
    attachment_markdown,
    browse_key,
    issue_placeholder,
)

type Node = Mapping[str, Any]

# What the manifest's not_carried_over says about each fallback.
UNKNOWN_NODE = "Rich text elements of a kind this tool does not convert ({}): their text is kept"
UNKNOWN_MARK = "Text formatting of a kind this tool does not convert ({}): the text is kept unformatted"
MERGED_CELLS = "Merged table cells (written as separate cells, the text in the first)"
NESTED_TABLES = "Tables inside table cells (written after the table that held them)"

PANEL_LABELS = {
    "info": "Info",
    "note": "Note",
    "tip": "Tip",
    "success": "Success",
    "warning": "Warning",
    "error": "Error",
}
LIST_TYPES = frozenset({"bulletList", "orderedList", "taskList", "decisionList"})
INLINE_TYPES = frozenset(
    {"text", "hardBreak", "mention", "emoji", "inlineCard", "date", "status", "mediaInline", "placeholder"}
)
# Blocks that only group others (page layouts): their content is written in order.
GROUP_TYPES = frozenset({"layoutSection", "layoutColumn", "doc"})

# Marks written as HTML tags Specivo lets through.
HTML_MARKS = {"strike": "del", "underline": "u"}
# Marks whose only effect is how the text looks: the text is kept as it is.
DROPPED_MARKS = frozenset(
    {"textColor", "backgroundColor", "annotation", "border", "alignment", "indentation", "breakout", "fragment"}
    | {"dataConsumer"}
)
KNOWN_MARKS = frozenset({"link", "strong", "em", "code", "subsup"} | set(HTML_MARKS))

# A URL in plain text; a sentence's full stop or comma after it is not part of it.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s<>()\[\]]+", re.IGNORECASE)
# What Python-Markdown would read as markup anywhere in a line.
_ESCAPE_RE = re.compile(r"[\\`*\[\]{]|_|<(?=[A-Za-z/!?])|&(?=#?\w+;)")
# What it would read as the start of a block at the start of a line.
_LINE_START_RE = re.compile(r"(?:[#>]|-(?=[-\s]|$)|\+(?=\s|$)|(?P<number>\d+)(?=\.(?:\s|$)))")
_SETEXT_RE = re.compile(r"=+\s*")
_LANGUAGE_RE = re.compile(r"[A-Za-z0-9_+#.\-]{1,40}")
_PLACEHOLDER_START_RE = re.compile(r"\{\{(?=(?:issue|user|attachment|wiki):)")
# Marks a hard line break in running text, a table cell, a heading.
_BREAK = "  \n"
_CELL_BREAK = "<br>"
# Keeps two lists in a row apart: Python-Markdown would merge them.
_LIST_SEPARATOR = "<!-- -->"


def to_markdown(body: object, ctx: TextContext) -> str | None:
    """Convert one ADF document to Markdown with placeholders. ``None``, empty and blank give ``None``.

    A body that is not a document (a string an app wrote through REST v2, say)
    is written as plain text. If the converter itself fails, the document's
    text is kept, escaped, and the failure counted.
    """
    if body is None:
        return None
    if isinstance(body, str):
        text = _Renderer(ctx).plain(body)
        return text.strip() or None
    if not isinstance(body, Mapping):
        ctx.failures += 1
        return None
    try:
        markdown = _Renderer(ctx).document(body)
    except Exception:  # noqa: BLE001 - malformed input keeps its text rather than failing the export
        ctx.failures += 1
        markdown = _escape(" ".join(plain_text(body).split()))
    return markdown.strip("\n") or None


def plain_text(node: object) -> str:
    """Every text a document holds, run together; for fallbacks and lookups."""
    return "".join(_texts(node))


def to_plain_text(body: object, display_names: Mapping[str, str] | None = None) -> str | None:
    """One ADF document as plain text, for fields that are not Markdown (a worklog's comment, say).

    Each block (paragraph, heading, list item, cell, ...) is a line; marks are
    dropped; a mention reads ``@Display Name``, a smart link its URL, a date
    ``2026-11-19``, a status its text, an emoji its character. Images and
    files leave nothing. ``None``, empty and blank give ``None``; a string is
    kept as it is, stripped.
    """
    if isinstance(body, str):
        return body.strip() or None
    if not isinstance(body, Mapping):
        return None
    names = display_names or {}
    out: list[str] = []

    def visit(node: Node) -> None:
        kind = node.get("type")
        attrs = _attrs(node)
        if kind == "text":
            out.append(str(node.get("text") or ""))
        elif kind == "hardBreak":
            out.append("\n")
        elif kind == "mention":
            account = str(attrs.get("id") or "")
            out.append("@" + (names.get(account) or str(attrs.get("text") or "").lstrip("@").strip() or account))
        elif kind == "emoji":
            out.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            out.append(_card_url(node))
        elif kind == "date":
            out.append(_date(attrs.get("timestamp")))
        elif kind == "status":
            out.append(str(attrs.get("text") or ""))
        else:
            block = not _is_inline(node)
            if block:
                out.append("\n")
            for child in _content(node):
                visit(child)
            if block:
                out.append("\n")

    visit(body)
    lines = [" ".join(line.split()) for line in "".join(out).split("\n")]
    return "\n".join(line for line in lines if line) or None


def candidate_keys(body: object, site_url: str = "") -> set[str]:
    """Every issue key a document could refer to: in text outside code, and in links and smart links to the site."""
    keys: set[str] = set()
    for node in walk(body, skip=frozenset({"codeBlock"})):
        kind = node.get("type")
        if kind == "text":
            marks = _mark_types(node)
            if "code" not in marks and "link" not in marks:
                text = str(node.get("text") or "")
                keys |= set(ISSUE_KEY_RE.findall(text))
                keys |= {key for url in _URL_RE.findall(text) if (key := _site_key(url, site_url))}
            for mark in node.get("marks") or []:
                if isinstance(mark, Mapping) and mark.get("type") == "link":
                    key = _site_key(str(_attrs(mark).get("href") or ""), site_url)
                    if key:
                        keys.add(key)
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            key = _site_key(_card_url(node), site_url)
            if key:
                keys.add(key)
    return keys


def mentioned_ids(body: object) -> set[str]:
    """Every account id a document mentions."""
    return {str(_attrs(node)["id"]) for node in walk(body) if node.get("type") == "mention" and _attrs(node).get("id")}


def walk(node: object, *, skip: frozenset[str] = frozenset()) -> Iterator[Node]:
    """Every node of a document, depth first, the node itself first. Anything that is not a node is skipped.

    Nodes of a type in *skip* are left out, with everything inside them.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, Mapping) or current.get("type") in skip:
            continue
        yield current
        content = current.get("content")
        if isinstance(content, list):
            stack.extend(reversed(content))


def media_markdown(media: Node, ctx: TextContext, *, single: bool = False) -> str:
    """What stands for one ``media`` or ``mediaInline`` node in the text.

    This is the one place where an image or file shown in the text is
    written. The node names its file by media file id (``attrs.id``), and
    ``ctx.media`` maps the media ids of the issue's attachments to them: the
    id is read from the ``Location`` of ``GET
    /rest/api/3/attachment/content/{id}`` with redirects not followed (see
    :mod:`.fetch`). A media node of an issue refers to that issue's
    attachments, in its description and its comments alike.

    - An image is ``![name]({{attachment:REF}})``: a ``media`` node in a
      ``mediaSingle`` (*single*), unless the attachment's MIME type says it
      is not an image.
    - Anything else is a link, ``[name]({{attachment:REF}})``: a file in a
      ``mediaGroup``, a ``mediaInline``, a non-image in a ``mediaSingle``.
    - The name is the node's ``alt`` when it has one, else the file name.
    - A file the bundle does not hold (deleted, over the size limit, refused
      by Jira, or another issue's) is a note naming it when its name is
      known, counted in the manifest.
    - An external image (``attrs.type == "external"``) is a URL and is
      written as a Markdown image.

    ``mediaSingle``'s ``width`` (a percentage of the text column) is dropped:
    Specivo's importer expands ``{{attachment:REF}}`` to a Markdown link
    target and its renderer points only Markdown images at the attachment,
    not the ``src`` of an inline ``<img>``, so an ``<img width>`` would show
    no picture.
    """
    attrs = _attrs(media)
    alt = str(attrs.get("alt") or attrs.get("name") or "").strip()
    url = str(attrs.get("url") or "")
    if attrs.get("type") == "external" and url:
        return f"![{_escape(alt)}]({_link_target(url)})"
    target = ctx.media.get(str(attrs.get("id") or ""))
    image = single and media.get("type") == "media" and (target is None or target.image is not False)
    return attachment_markdown(target, alt, ctx, image=image)


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


@dataclass
class _Table:
    """A table's rows as cell texts, and the tables found in its cells, written after it."""

    rows: list[list[str]] = field(default_factory=list)
    header: bool = False
    after: list[str] = field(default_factory=list)


class _Renderer:
    def __init__(self, ctx: TextContext) -> None:
        self.ctx = ctx

    def document(self, doc: Node) -> str:
        return self.blocks(_content(doc), depth=0)

    def plain(self, text: str) -> str:
        """Plain text, escaped, with references, as a paragraph."""
        return self._paragraph_text(self.text(text, break_with=_BREAK))

    # Blocks ----------------------------------------------------------

    def blocks(self, nodes: Sequence[Node], *, depth: int) -> str:
        """Render block nodes, one after the other. Inline nodes among them are gathered into paragraphs."""
        rendered: list[tuple[str, str]] = []
        loose: list[Node] = []

        def flush() -> None:
            if loose:
                text = self._paragraph_text(self.inline(loose))
                if text.strip():
                    rendered.append(("paragraph", text))
                loose.clear()

        for node in nodes:
            kind = str(node.get("type") or "")
            if _is_inline(node):
                loose.append(node)
                continue
            flush()
            if kind in GROUP_TYPES:
                text = self.blocks(_content(node), depth=depth)
            else:
                text = self.block(node, depth=depth)
            if text.strip():
                rendered.append((kind, text))
        flush()

        out: list[str] = []
        previous = ""
        for kind, text in rendered:
            if out:
                if previous in LIST_TYPES and kind in LIST_TYPES and depth == 0:
                    out.append(_LIST_SEPARATOR)
            out.append(text)
            previous = kind
        return "\n\n".join(out)

    def block(self, node: Node, *, depth: int) -> str:
        kind = str(node.get("type") or "")
        handler = BLOCK_HANDLERS.get(kind)
        if handler is not None:
            return handler(self, node, depth)
        return self._unknown_block(node, depth)

    def _unknown_block(self, node: Node, depth: int) -> str:
        self.ctx.notes[UNKNOWN_NODE.format(node.get("type") or "unnamed")] += 1
        content = _content(node)
        if content:
            return self.blocks(content, depth=depth)
        text = str(_attrs(node).get("text") or node.get("text") or "")
        return self.plain(text) if text.strip() else ""

    def paragraph(self, node: Node, depth: int) -> str:
        return self._paragraph_text(self.inline(_content(node)))

    def heading(self, node: Node, depth: int) -> str:
        level = _attrs(node).get("level")
        level = level if isinstance(level, int) and 1 <= level <= 6 else 1
        text = " ".join(self.inline(_content(node), break_with=" ").split())
        if not text:
            return ""
        if text.endswith("#"):
            # Python-Markdown would take a closing # for the heading's own.
            text = text[:-1] + "\\#"
        return f"{'#' * level} {text}"

    def bullet_list(self, node: Node, depth: int) -> str:
        return self._list(node, depth, lambda _: "-")

    def ordered_list(self, node: Node, depth: int) -> str:
        start = _attrs(node).get("order")
        first = start if isinstance(start, int) and start >= 0 else 1
        # Specivo numbers every ordered list from 1; the numbers are written as Jira showed them.
        return self._list(node, depth, lambda index: f"{first + index}.")

    def task_list(self, node: Node, depth: int) -> str:
        return self._list(node, depth, lambda _: "-")

    def decision_list(self, node: Node, depth: int) -> str:
        return self._list(node, depth, lambda _: "-")

    def _list(self, node: Node, depth: int, marker: Callable[[int], str]) -> str:
        entries: list[str] = []
        index = 0
        for child in _content(node):
            kind = child.get("type")
            if kind in LIST_TYPES and entries:
                # A task list nests by holding another list next to its items.
                entries[-1] += "\n" + _indent(self.block(child, depth=depth + 1))
                continue
            if kind == "listItem":
                body = self._item(_content(child), depth + 1)
            elif kind == "taskItem":
                done = str(_attrs(child).get("state") or "").upper() == "DONE"
                body = ("[x] " if done else "[ ] ") + self._paragraph_text(self.inline(_content(child)))
            elif kind == "decisionItem":
                decided = str(_attrs(child).get("state") or "DECIDED").upper() == "DECIDED"
                label = "**Decision:**" if decided else "**Decision (open):**"
                body = f"{label} {self._paragraph_text(self.inline(_content(child)))}".rstrip()
            else:
                body = self.blocks([child], depth=depth + 1)
            if not body.strip():
                body = "<span></span>"
            entries.append(_list_entry(marker(index), body))
            index += 1
        return "\n".join(entries)

    def _item(self, nodes: Sequence[Node], depth: int) -> str:
        parts: list[str] = []
        previous = ""
        for node in nodes:
            kind = str(node.get("type") or "")
            text = self.blocks([node], depth=depth)
            if not text.strip():
                continue
            if not parts and kind not in ("paragraph", "mediaSingle", "mediaGroup") and kind not in INLINE_TYPES:
                # A list item's first line must be text, or the marker stands alone.
                parts.append("<span></span>")
                previous = "paragraph"
            if parts:
                # A list right under the item's text needs no blank line; anything else does.
                parts.append("\n" if kind in LIST_TYPES and previous == "paragraph" else "\n\n")
            parts.append(text)
            previous = kind
        return "".join(parts)

    def code_block(self, node: Node, depth: int) -> str:
        code = "".join(_texts(node)).rstrip("\n")
        language = str(_attrs(node).get("language") or "")
        language = language if _LANGUAGE_RE.fullmatch(language) else ""
        if depth == 0:
            longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
            fence = "`" * max(3, longest + 1)
            return f"{fence}{language}\n{_neutralise(code)}\n{fence}"
        # Fenced code is not read inside a list or a quote: an indented block is.
        lines = code.split("\n")
        if language:
            lines.insert(0, f":::{language}")
        elif lines and (lines[0].startswith(":::") or lines[0].startswith("#!")):
            # The highlighter would read the first line as a language.
            lines.insert(0, ":::text")
        lines = [_neutralise(line) for line in lines]
        return "\n".join(("    " + line) if line else "" for line in lines)

    def blockquote(self, node: Node, depth: int) -> str:
        return _quote(self.blocks(_content(node), depth=depth + 1))

    def panel(self, node: Node, depth: int) -> str:
        kind = str(_attrs(node).get("panelType") or "info").lower()
        return self._labelled(PANEL_LABELS.get(kind, "Note"), _content(node), depth, inline_label=True)

    def expand(self, node: Node, depth: int) -> str:
        title = str(_attrs(node).get("title") or "").strip() or "Details"
        return self._labelled(title, _content(node), depth, inline_label=False)

    def _labelled(self, label: str, content: Sequence[Node], depth: int, *, inline_label: bool) -> str:
        if inline_label and content and content[0].get("type") == "paragraph":
            first = self._paragraph_text(self.inline(_content(content[0])))
            head = f"**{_escape(label)}:** {first}".rstrip()
            content = content[1:]
        else:
            head = f"**{_escape(label)}**"
        rest = self.blocks(content, depth=depth + 1)
        return _quote(head + ("\n\n" + rest if rest else ""))

    def rule(self, node: Node, depth: int) -> str:
        return "---"

    def media_single(self, node: Node, depth: int) -> str:
        parts = [
            media_markdown(child, self.ctx, single=True) if child.get("type") == "media" else self.inline([child])
            for child in _content(node)
        ]
        return " ".join(part for part in parts if part)

    def media_group(self, node: Node, depth: int) -> str:
        return " ".join(media_markdown(child, self.ctx) for child in _content(node) if child.get("type") == "media")

    def card_block(self, node: Node, depth: int) -> str:
        return self.card(node)

    def table(self, node: Node, depth: int) -> str:
        table = self._table(node)
        if not table.rows:
            return ""
        width = max(len(row) for row in table.rows)
        rows = [row + [""] * (width - len(row)) for row in table.rows]
        header = rows.pop(0) if table.header else [""] * width
        lines = [_table_row(header), "| " + " | ".join("---" for _ in range(width)) + " |"]
        lines += [_table_row(row) for row in rows]
        return "\n\n".join(["\n".join(lines), *table.after])

    def _table(self, node: Node) -> _Table:
        result = _Table()
        rows = [row for row in _content(node) if row.get("type") == "tableRow"]
        taken: set[tuple[int, int]] = set()
        grid: dict[tuple[int, int], str] = {}
        result.header = bool(rows) and all(cell.get("type") == "tableHeader" for cell in _content(rows[0]))
        for r, row in enumerate(rows):
            c = 0
            for cell in _content(row):
                while (r, c) in taken:
                    c += 1
                colspan = _span(_attrs(cell).get("colspan"))
                rowspan = min(_span(_attrs(cell).get("rowspan")), len(rows) - r)
                if colspan > 1 or rowspan > 1:
                    self.ctx.notes[MERGED_CELLS] += 1
                text = self._cell(cell, result, (r + 1, c + 1))
                if cell.get("type") == "tableHeader" and r > 0 and text and "<br>" not in text:
                    text = f"**{text}**"
                for dr in range(rowspan):
                    for dc in range(colspan):
                        taken.add((r + dr, c + dc))
                        grid[(r + dr, c + dc)] = text if dr == dc == 0 else ""
                c += colspan
        width = max((col for _, col in grid), default=-1) + 1
        result.rows = [[grid.get((r, c), "") for c in range(width)] for r in range(len(rows))]
        return result

    def _cell(self, cell: Node, table: _Table, position: tuple[int, int]) -> str:
        """One cell's content on one line: paragraphs and list items joined with ``<br>``."""
        lines: list[str] = []
        for node in _content(cell):
            lines += self._cell_lines(node, table, position, level=0)
        return _CELL_BREAK.join(line for line in lines if line.strip())

    def _cell_lines(self, node: Node, table: _Table, position: tuple[int, int], *, level: int) -> list[str]:
        kind = node.get("type")
        if kind in ("paragraph", "heading"):
            text = self.inline(_content(node), break_with=_CELL_BREAK, cell=True)
            return [f"**{text.strip()}**" if kind == "heading" and text.strip() else text]
        if kind in LIST_TYPES:
            return self._cell_list(node, table, position, level=level)
        if kind == "codeBlock":
            return [_code_span(line) for line in "".join(_texts(node)).split("\n") if line.strip()]
        if kind in ("blockquote", "panel", "expand", "nestedExpand"):
            label = ""
            if kind == "panel":
                label = PANEL_LABELS.get(str(_attrs(node).get("panelType") or "").lower(), "Note")
            elif kind != "blockquote":
                label = str(_attrs(node).get("title") or "").strip() or "Details"
            inner = [line for child in _content(node) for line in self._cell_lines(child, table, position, level=level)]
            if label:
                inner.insert(0, f"**{_escape(label, cell=True)}:**")
            return ["> " + line if kind == "blockquote" else line for line in inner]
        if kind == "table":
            self.ctx.notes[NESTED_TABLES] += 1
            row, column = position
            table.after.append(f"*Table in row {row}, column {column}:*\n\n{self.table(node, 1)}")
            return [f"*(table {len(table.after)} below)*"]
        if kind == "mediaSingle":
            return [self.media_single(node, 1)]
        if kind == "mediaGroup":
            return [self.media_group(node, 1)]
        if kind == "rule":
            return []
        if kind in ("blockCard", "embedCard"):
            return [self.card(node, cell=True)]
        if _is_inline(node):
            return [self.inline([node], break_with=_CELL_BREAK, cell=True)]
        if kind not in GROUP_TYPES:
            self.ctx.notes[UNKNOWN_NODE.format(kind or "unnamed")] += 1
        return [line for child in _content(node) for line in self._cell_lines(child, table, position, level=level)]

    def _cell_list(self, node: Node, table: _Table, position: tuple[int, int], *, level: int) -> list[str]:
        """A list in a cell: one line per item, nested items indented with non-breaking spaces."""
        lines: list[str] = []
        number = _attrs(node).get("order")
        number = number if isinstance(number, int) and number >= 0 else 1
        indent = "&nbsp;" * 4 * level
        for child in _content(node):
            kind = child.get("type")
            if kind in LIST_TYPES:
                lines += self._cell_list(child, table, position, level=level + 1)
                continue
            if node.get("type") == "orderedList":
                marker = f"{number}."
                number += 1
            elif kind == "taskItem":
                marker = "[x]" if str(_attrs(child).get("state") or "").upper() == "DONE" else "[ ]"
            elif kind == "decisionItem":
                marker = "**Decision:**"
            else:
                marker = "•"
            blocks = _content(child)
            if kind != "listItem":
                head, rest = self.inline(blocks, break_with=" ", cell=True), []
            elif blocks and blocks[0].get("type") == "paragraph":
                head, rest = self.inline(_content(blocks[0]), break_with=" ", cell=True), blocks[1:]
            else:
                head, rest = "", blocks
            lines.append(f"{indent}{marker} {head.strip()}".rstrip())
            for block in rest:
                inner = self._cell_lines(block, table, position, level=level + 1)
                if block.get("type") not in LIST_TYPES:
                    inner = ["&nbsp;" * 4 * (level + 1) + line for line in inner]
                lines += inner
        return lines

    def _paragraph_text(self, text: str) -> str:
        """Escape what would start a block at the start of each line of a paragraph."""
        return "\n".join(_escape_line_start(line) for line in text.split("\n")).strip("\n")

    # Inline ------------------------------------------------------------

    def inline(self, nodes: Sequence[Node], *, break_with: str = _BREAK, cell: bool = False) -> str:
        items = [(self._marks(node), node) for node in nodes]
        return self._runs(items, break_with=break_with, cell=cell, in_link=False)

    def _marks(self, node: Node) -> tuple[Node, ...]:
        kept: list[Node] = []
        for mark in node.get("marks") or []:
            if not isinstance(mark, Mapping):
                continue
            kind = str(mark.get("type") or "")
            if kind in KNOWN_MARKS:
                kept.append(mark)
            elif kind not in DROPPED_MARKS:
                self.ctx.notes[UNKNOWN_MARK.format(kind or "unnamed")] += 1
        return tuple(kept)

    def _runs(self, items: list[tuple[tuple[Node, ...], Node]], *, break_with: str, cell: bool, in_link: bool) -> str:
        """Render inline nodes, wrapping each run of nodes that share a mark in that mark once."""
        out: list[str] = []
        index = 0
        while index < len(items):
            marks, node = items[index]
            if not marks:
                out.append(self._inline_node(node, break_with=break_with, cell=cell, in_link=in_link))
                index += 1
                continue
            mark = self._outer_mark(items, index)
            end = index + 1
            while end < len(items) and any(_same_mark(mark, other) for other in items[end][0]):
                end += 1
            run = [(tuple(m for m in item_marks if not _same_mark(m, mark)), n) for item_marks, n in items[index:end]]
            out.append(self._wrap(mark, run, break_with=break_with, cell=cell, in_link=in_link))
            index = end
        return "".join(out)

    def _outer_mark(self, items: list[tuple[tuple[Node, ...], Node]], index: int) -> Node:
        """The mark to open first: a link, else the one shared by the longest run; code only when alone."""
        marks = items[index][0]
        for mark in marks:
            if mark.get("type") == "link":
                return mark
        choices = [mark for mark in marks if mark.get("type") != "code"] or list(marks)

        def reach(mark: Node) -> int:
            end = index + 1
            while end < len(items) and any(_same_mark(mark, other) for other in items[end][0]):
                end += 1
            return end

        return max(choices, key=reach)

    def _wrap(
        self, mark: Node, run: list[tuple[tuple[Node, ...], Node]], *, break_with: str, cell: bool, in_link: bool
    ) -> str:
        kind = mark.get("type")
        if kind == "code":
            text = "".join(str(node.get("text") or "") if node.get("type") == "text" else " " for _, node in run)
            return _code_span(text)
        if kind == "link":
            href = str(_attrs(mark).get("href") or "")
            label = "".join(text for _, node in run for text in _texts(node)).strip()
            key = browse_key(href, self.ctx)
            if key is not None and not in_link:
                replaced = issue_placeholder(key, self.ctx, label=None if label in ("", key, href) else label)
                if replaced is not None:
                    return replaced
            inner = self._runs(run, break_with=" ", cell=cell, in_link=True)
            if not href:
                return inner
            if not inner.strip() or label == href:
                return f"<{href}>" if _autolinkable(href) else f"[{_escape(href, cell=cell)}]({_link_target(href)})"
            return f"[{inner}]({_link_target(href)})"
        inner = self._runs(run, break_with=break_with, cell=cell, in_link=in_link)
        stripped = inner.strip()
        if not stripped:
            return inner
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()) :]
        if kind == "strong":
            wrapped = f"**{stripped}**"
        elif kind == "em":
            wrapped = f"*{stripped}*"
        elif kind == "subsup":
            tag = "sub" if _attrs(mark).get("type") == "sub" else "sup"
            wrapped = f"<{tag}>{stripped}</{tag}>"
        else:
            tag = HTML_MARKS[str(kind)]
            wrapped = f"<{tag}>{stripped}</{tag}>"
        return lead + wrapped + trail

    def _inline_node(self, node: Node, *, break_with: str, cell: bool, in_link: bool) -> str:
        kind = node.get("type")
        attrs = _attrs(node)
        if kind == "text":
            return self.text(str(node.get("text") or ""), break_with=break_with, cell=cell, in_link=in_link)
        if kind == "hardBreak":
            return break_with
        if kind == "mention":
            return self.mention(attrs, cell=cell)
        if kind == "emoji":
            return _escape(str(attrs.get("text") or attrs.get("shortName") or ""), cell=cell)
        if kind == "inlineCard":
            return self.card(node, cell=cell, in_link=in_link)
        if kind == "date":
            return _escape(_date(attrs.get("timestamp")), cell=cell)
        if kind == "status":
            label = str(attrs.get("text") or "").strip()
            return _code_span(label) if label else ""
        if kind == "mediaInline":
            return media_markdown(node, self.ctx)
        if kind == "placeholder":
            # The editor's prompt text ("Type here"), not something anyone wrote.
            return ""
        self.ctx.notes[UNKNOWN_NODE.format(kind or "unnamed")] += 1
        content = _content(node)
        if content:
            return self._runs([(self._marks(n), n) for n in content], break_with=break_with, cell=cell, in_link=in_link)
        return self.text(str(attrs.get("text") or node.get("text") or ""), break_with=break_with, cell=cell)

    def mention(self, attrs: Mapping[str, Any], *, cell: bool = False) -> str:
        account = str(attrs.get("id") or "")
        ref = self.ctx.user_refs.get(account)
        if ref is not None:
            return placeholders.user(ref)
        name = self.ctx.display_names.get(account) or str(attrs.get("text") or "").lstrip("@").strip() or account
        return "@" + _escape(name, cell=cell)

    def card(self, node: Node, *, cell: bool = False, in_link: bool = False) -> str:
        url = _card_url(node)
        if not url:
            return ""
        key = browse_key(url, self.ctx)
        if key is not None and not in_link:
            replaced = issue_placeholder(key, self.ctx)
            if replaced is not None:
                return replaced
        if in_link:
            return _escape(url, cell=cell)
        return f"<{url}>" if _autolinkable(url) else f"[{_escape(url, cell=cell)}]({_link_target(url)})"

    def text(self, text: str, *, break_with: str = _BREAK, cell: bool = False, in_link: bool = False) -> str:
        """Plain text, escaped; outside a link, issue keys and links to issue pages become placeholders."""
        text = text.replace("\r\n", "\n")
        if "\n" in text:
            return break_with.join(
                self.text(part, break_with=break_with, cell=cell, in_link=in_link) for part in text.split("\n")
            )
        if in_link or not self.ctx.issue_refs:
            return _escape(text, cell=cell)
        out: list[str] = []
        position = 0
        for match in _URL_RE.finditer(text):
            out.append(self._keys(text[position : match.start()], cell=cell))
            url = match.group(0)
            trimmed = url.rstrip(".,;:!?")
            key = browse_key(trimmed, self.ctx)
            replaced = issue_placeholder(key, self.ctx) if key is not None else None
            out.append((replaced + _escape(url[len(trimmed) :])) if replaced else _escape(url, cell=cell))
            position = match.end()
        out.append(self._keys(text[position:], cell=cell))
        return "".join(out)

    def _keys(self, text: str, *, cell: bool) -> str:
        out: list[str] = []
        position = 0
        for match in ISSUE_KEY_RE.finditer(text):
            replaced = issue_placeholder(match.group(1), self.ctx)
            if replaced is None:
                continue
            out.append(_escape(text[position : match.start()], cell=cell))
            out.append(replaced)
            position = match.end()
        out.append(_escape(text[position:], cell=cell))
        return "".join(out)


BLOCK_HANDLERS: dict[str, Callable[[_Renderer, Node, int], str]] = {
    "paragraph": _Renderer.paragraph,
    "heading": _Renderer.heading,
    "bulletList": _Renderer.bullet_list,
    "orderedList": _Renderer.ordered_list,
    "taskList": _Renderer.task_list,
    "decisionList": _Renderer.decision_list,
    "codeBlock": _Renderer.code_block,
    "blockquote": _Renderer.blockquote,
    "panel": _Renderer.panel,
    "expand": _Renderer.expand,
    "nestedExpand": _Renderer.expand,
    "rule": _Renderer.rule,
    "mediaSingle": _Renderer.media_single,
    "mediaGroup": _Renderer.media_group,
    "blockCard": _Renderer.card_block,
    "embedCard": _Renderer.card_block,
    "table": _Renderer.table,
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _attrs(node: Node) -> Mapping[str, Any]:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, Mapping) else {}


def _content(node: Node) -> list[Node]:
    content = node.get("content")
    if not isinstance(content, list):
        return []
    return [child for child in content if isinstance(child, Mapping)]


def _texts(node: object) -> Iterator[str]:
    for current in walk(node):
        if current.get("type") == "text":
            yield str(current.get("text") or "")
        elif current.get("type") == "hardBreak":
            yield "\n"


def _mark_types(node: Node) -> set[str]:
    return {str(mark.get("type")) for mark in node.get("marks") or [] if isinstance(mark, Mapping)}


def _same_mark(one: Node, other: Node) -> bool:
    return one.get("type") == other.get("type") and _attrs(one) == _attrs(other)


def _is_inline(node: Node) -> bool:
    """A node of a kind not known as a block: inline when it holds only text or inline nodes."""
    kind = node.get("type")
    if kind in INLINE_TYPES:
        return True
    if kind in BLOCK_HANDLERS or kind in GROUP_TYPES:
        return False
    content = _content(node)
    if content:
        return all(_is_inline(child) for child in content)
    return "text" in node


def _card_url(node: Node) -> str:
    attrs = _attrs(node)
    url = attrs.get("url")
    if not url and isinstance(attrs.get("data"), Mapping):
        url = attrs["data"].get("url")
    return str(url or "").strip()


def _site_key(url: str, site_url: str) -> str | None:
    if not url or not site_url:
        return None
    return browse_key(url, TextContext(site_url=site_url))


def _span(value: object) -> int:
    return value if isinstance(value, int) and 1 <= value <= 1000 else 1


def _date(value: object) -> str:
    """A date lozenge's timestamp (milliseconds, as text) as an ISO date, in UTC as Jira shows it."""
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value or "")


def _escape(text: str, *, cell: bool = False) -> str:
    """Escape what Python-Markdown would read as markup, and the start of a placeholder."""

    def replace(match: re.Match[str]) -> str:
        char = match.group(0)
        if char == "_":
            at = match.start()
            before = text[at - 1] if at else ""
            after = text[at + 1] if at + 1 < len(text) else ""
            # Inside a word an underscore is never emphasis.
            return "_" if before.isalnum() and after.isalnum() else "\\_"
        if char == "<":
            return "&lt;"
        if char == "&":
            return "&amp;"
        return "\\" + char

    escaped = _ESCAPE_RE.sub(replace, text)
    return escaped.replace("|", "\\|") if cell else escaped


def _escape_line_start(line: str) -> str:
    stripped = line.lstrip(" \t")
    if _SETEXT_RE.fullmatch(stripped):
        return "&#61;" + stripped[1:]
    match = _LINE_START_RE.match(stripped)
    if match is None:
        return stripped
    number = match.group("number")
    if number is not None:
        return number + "\\" + stripped[len(number) :]
    return "\\" + stripped


def _neutralise(code: str) -> str:
    """Code with a zero-width space in each ``{{`` that would start a placeholder.

    Specivo skips fenced code when it expands placeholders, but not inline
    code or indented blocks, and it pairs fences by their first three
    characters only, so no code is trusted to be skipped: what looks like a
    placeholder in code must not be one.
    """
    return _PLACEHOLDER_START_RE.sub("{\u200b{", code)


def _code_span(text: str) -> str:
    text = _neutralise(" ".join(text.split("\n")))
    if not text:
        return ""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`"):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def _autolinkable(url: str) -> bool:
    """Whether *url* can be written as ``<url>``: no spaces or angle brackets, and nothing a placeholder is made of."""
    return bool(re.fullmatch(r"[a-z][a-z0-9+.\-]*:[^\s<>{}|]+", url, re.IGNORECASE))


# What would end a link target early, or make a placeholder of it.
_TARGET_ESCAPES = str.maketrans(
    {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "{": "%7B", "}": "%7D", "|": "%7C"}
)


def _link_target(url: str) -> str:
    """A URL as a Markdown link target, percent-encoded where it would end the target or form a placeholder."""
    return url.translate(_TARGET_ESCAPES)


def _indent(text: str) -> str:
    return "\n".join(("    " + line) if line else "" for line in text.split("\n"))


def _list_entry(marker: str, body: str) -> str:
    first, _, rest = body.partition("\n")
    return f"{marker} {first}" + ("\n" + _indent(rest) if rest else "")


def _quote(text: str) -> str:
    return "\n".join(("> " + line) if line else ">" for line in text.split("\n"))


def _table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"
