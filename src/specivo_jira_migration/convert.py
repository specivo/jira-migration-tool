"""Jira wiki markup to Markdown, with references turned into bundle placeholders.

Jira Data Center returns text as wiki markup (REST v2). Jira Cloud returns
Atlassian Document Format through REST v3, which :mod:`.adf` converts; the
reference rules and :class:`TextContext` here serve both.

The markup itself is converted by ``jira2markdown``. Two things are done on top:

- **Mentions** (``[~accountid:...]`` on Cloud, ``[~username]`` on Data
  Center) of a user the bundle carries become ``{{user:REF}}``, which Specivo
  turns into ``@login``. A user the bundle leaves out (an app account, say) is
  written as ``@Display Name`` from the account id to display name map, and an
  unknown one as ``@`` and the id Jira wrote.
- **Issue keys** (``WEB-12``) become ``{{issue:REF}}``, where REF is the
  issue's Jira id (the bundle's ``source_ref`` for issues), so the reference
  survives a project key rename in Jira and Specivo's own new key is filled
  in on load. A key of an issue that is not in this bundle but that Jira
  knows becomes ``{{issue:REF|WEB-12}}``: Specivo links it if an earlier
  bundle of the same site brought that issue, and otherwise writes the key as
  it was. A link to the site's ``/browse/KEY`` page is treated the same way,
  and is looked up like a bare key when its issue is not in the bundle.
  A key Jira does not know (``UTF-8``) stays text. Nothing is replaced inside
  code (fenced or inline), inside a link's text or target, or in a URL.
- **Attachments** shown or linked by file name (``!name.png!``,
  ``!name.png|thumbnail!``, ``[^name.txt]``) become
  ``![name.png]({{attachment:REF}})`` and ``[name.txt]({{attachment:REF}})``
  when the issue has an attachment of that name in the bundle (the newest,
  when several share it, as Jira shows). Size and other image options are
  dropped: Specivo takes no width from Markdown. A file the bundle does not
  carry is a note naming it. The bundle format also resolves a bare file name
  as a link target to the issue's attachment of that name, but a placeholder
  names the record itself and survives the file being renamed on load.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

import jira2markdown.parser  # noqa: F401 - sets pyparsing's whitespace default the grammar needs
from jira2markdown.elements import MarkupElements
from jira2markdown.markup.advanced import Code
from jira2markdown.markup.images import Image
from jira2markdown.markup.links import Attachment, Mention
from pyparsing import (
    Combine,
    Forward,
    Literal,
    Optional,
    ParserElement,
    PrecededBy,
    Regex,
    SkipTo,
    StringStart,
    Word,
    ZeroOrMore,
    alphanums,
    printables,
)
from specivo_migration_sdk import placeholders

# An issue key as Jira writes one: project key, a dash, the number.
ISSUE_KEY_RE = re.compile(r"(?<![\w/.\-])([A-Z][A-Z0-9_]{0,254}-[1-9][0-9]{0,17})(?![\w\-]|\.\w)")
# A mention in wiki markup, with or without a label; the id as jira2markdown reads it.
MENTION_RE = re.compile(r"\[(?:[^\]|]*\|)?~(?:accountid:)?([A-Za-z0-9:\-]+)\]", re.IGNORECASE)

# One Markdown construct that must not be touched, or that is a link to look at.
_INLINE_RE = re.compile(
    r"(?P<placeholder>\{\{(?:issue|user|attachment|wiki):[^{}\n]*\}\})"
    r"|(?P<code>(?P<ticks>`+).+?(?P=ticks))"
    r"|(?P<image>!\[[^\]\n]*\]\([^)\n]*\))"
    r"|(?P<link>\[(?P<text>[^\]\n]*)\]\((?P<href>[^)\s]*)\))"
    r"|(?P<autolink><(?P<auto>[a-z][a-z0-9+.\-]*:[^>\s]*)>)"
    r"|(?P<url>\b[a-z][a-z0-9+.\-]*://[^\s<>()\[\]]+)",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
# What would end a Markdown link text or form a placeholder in it.
_LABEL_ESCAPE_RE = re.compile(r"([\\\[\]{}`*_<|])")
# What would end a link target early, or make a placeholder of it.
_TARGET_ESCAPES = str.maketrans(
    {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "{": "%7B", "}": "%7D", "|": "%7C"}
)

# A file name's extension, which tells an image of a missing file from text between two "!".
_FILE_NAME_RE = re.compile(r"\S\.[A-Za-z0-9]{1,10}$")

# The manifest's word for images and files in text whose attachment the bundle does not hold.
MEDIA_NOT_IN_BUNDLE = "Images and files shown in text whose attachment is not in the bundle (a note stands in for each)"


@dataclass(frozen=True)
class AttachmentTarget:
    """One attachment of the issue whose text is being converted, as the text may refer to it.

    ``ref`` is its ``source_ref`` when the bundle carries it; otherwise
    ``reason`` says why it does not (and the text gets a note).
    """

    filename: str
    ref: str | None = None
    content_type: str | None = None
    reason: str | None = None

    @property
    def image(self) -> bool | None:
        """Whether the file is an image, by its MIME type; None when Jira gave none."""
        if not self.content_type:
            return None
        return self.content_type.lower().startswith("image/")


@dataclass
class TextContext:
    """What the converter needs to know about the bundle being written.

    ``user_refs`` maps what a mention names (a Cloud account id, a Data Center
    user name) to the user's ``source_ref``; ``display_names`` maps it to a
    name for users the bundle does not carry. ``issue_refs`` maps an issue key
    to its ``source_ref`` and ``in_bundle`` holds the refs the bundle carries.

    ``media`` (Cloud: by media file id) and ``files`` (Data Center: by the
    file name Jira shows) are the attachments of the issue being converted;
    the caller sets them before each issue's texts.

    ``failures`` counts texts that could not be converted and ``notes`` what
    a conversion had to leave out or write differently, by the label the
    manifest's ``not_carried_over`` gives it.
    """

    site_url: str = ""
    user_refs: Mapping[str, str] = field(default_factory=dict)
    display_names: Mapping[str, str] = field(default_factory=dict)
    issue_refs: Mapping[str, str] = field(default_factory=dict)
    in_bundle: frozenset[str] = frozenset()
    media: Mapping[str, AttachmentTarget] = field(default_factory=dict)
    files: Mapping[str, AttachmentTarget] = field(default_factory=dict)
    failures: int = 0
    notes: Counter[str] = field(default_factory=Counter)
    _grammar: Forward | None = field(default=None, init=False, repr=False)

    def grammar(self) -> Forward:
        """jira2markdown's grammar with our mention and code elements, built once per context.

        This is what ``jira2markdown.convert`` builds on every call; building it
        once saves most of the time on a large export.
        """
        if self._grammar is None:
            elements = MarkupElements()
            elements.replace(Mention, _mention_element(self))
            elements.replace(Code, _Code)
            elements.replace(Image, _image_element(self))
            elements.replace(Attachment, _attachment_element(self))
            inline_markup = Forward()
            markup = Forward()
            inline = [element for element in elements if element.is_inline_element]
            inline_markup <<= elements.expr(inline_markup, markup, {}, inline)
            markup <<= elements.expr(inline_markup, markup, {}, elements)
            self._grammar = markup
        return self._grammar


def candidate_keys(text: str | None, site_url: str = "") -> set[str]:
    """Every string in raw wiki markup that looks like an issue key (a superset of what is replaced).

    Keys in links to *site_url*'s ``/browse/KEY`` pages are included: the key
    regex alone skips them, as it skips every key that follows a ``/``.
    """
    keys = set(ISSUE_KEY_RE.findall(text or ""))
    if site_url and text:
        prefix = re.escape(site_url.rstrip("/") + "/browse/")
        keys |= set(re.findall(f"(?i:{prefix})" + r"([A-Z][A-Z0-9_]{0,254}-[1-9][0-9]{0,17})(?![\w\-])", text))
    return keys


def mentioned_ids(text: str | None) -> set[str]:
    """Every account id or user name mentioned in raw wiki markup."""
    return set(MENTION_RE.findall(text or ""))


def _mention_element(ctx: TextContext) -> type[Mention]:
    class PlaceholderMention(Mention):  # type: ignore[misc]
        """A mention of a carried user becomes a user placeholder; any other one ``@name``."""

        def action(self, tokens: object) -> str:
            name = str(getattr(tokens, "accountid", ""))
            ref = ctx.user_refs.get(name)
            if ref is not None:
                return placeholders.user(ref)
            # A display name is set by its user: escaped, so it cannot form markup or a placeholder.
            return f"@{escape_label(ctx.display_names.get(name, name))}"

    return PlaceholderMention


def attachment_markdown(target: AttachmentTarget | None, name: str, ctx: TextContext, *, image: bool) -> str:
    """An attached file in text: an image or a link to it by placeholder, or a note when the bundle lacks it.

    *name* is what the text shows: the file name, or the text's own label.
    """
    if target is not None and target.ref is not None:
        label = escape_label(name or target.filename)
        return f"{'!' if image else ''}[{label}]({placeholders.attachment(target.ref)})"
    ctx.notes[MEDIA_NOT_IN_BUNDLE] += 1
    shown = escape_label(name or (target.filename if target else ""))
    if target is not None and target.reason:
        return f"*(attachment {shown}: {target.reason})*" if shown else f"*(an attachment: {target.reason})*"
    return f"*(attachment: {shown})*" if shown else "*(an attachment is shown here in Jira)*"


def escape_label(text: str) -> str:
    """Text for a Markdown link or image label: brackets, emphasis and the start of a placeholder escaped."""
    return _LABEL_ESCAPE_RE.sub(r"\\\1", " ".join(text.split()))


def link_target(url: str) -> str:
    """A URL as a Markdown link target, percent-encoded where it would end the target or form a placeholder."""
    return url.translate(_TARGET_ESCAPES)


def _image_element(ctx: TextContext) -> type[Image]:
    class AttachedImage(Image):  # type: ignore[misc]
        """``!name.png!`` and ``!name.png|options!``: an attachment by placeholder, or an external image.

        The name may hold any character but ``|``, ``!`` and line breaks
        (jira2markdown reads ASCII names only, and leaves ``!ไฟล์.png!`` as
        written). Options (``thumbnail``, ``width=300``) are dropped.
        """

        def action(self, tokens: object) -> str:
            name = str(getattr(tokens, "url", "")).strip()
            if re.match(r"[a-z][a-z0-9+.\-]*://", name, re.IGNORECASE):
                return f"![{escape_label(name)}]({link_target(name)})"
            target = ctx.files.get(name)
            if target is None and not _FILE_NAME_RE.search(name):
                # "!so good!" is text, not an image of a file that is missing.
                return str(getattr(tokens, "original", None) or f"!{name}!")
            return attachment_markdown(target, name, ctx, image=True)

        @property
        def expr(self) -> ParserElement:
            option = (
                Word(alphanums + " ")
                + Optional(Literal("=").suppress() + Word(printables + " ", exclude_chars=",!"))
                + Optional(",").suppress()
            )
            return (StringStart() | PrecededBy(Regex(r"\W", flags=re.UNICODE), retreat=1)) + Combine(
                "!"
                + Regex(r"[^|!\r\n\t]{3,}").set_results_name("url")
                + Optional("|" + ZeroOrMore(option.set_results_name("attrs", list_all_matches=True), stop_on="!"))
                + "!",
            ).set_parse_action(self.action)

    return AttachedImage


def _attachment_element(ctx: TextContext) -> type[Attachment]:
    class AttachedFile(Attachment):  # type: ignore[misc]
        """``[^name.txt]``: a link to the attachment by placeholder."""

        def action(self, tokens: object) -> str:
            name = str(getattr(tokens, "filename", "")).strip()
            return attachment_markdown(ctx.files.get(name), name, ctx, image=False)

        @property
        def expr(self) -> ParserElement:
            return Combine("[^" + SkipTo("]", fail_on="\n").set_results_name("filename") + "]").set_parse_action(
                self.action
            )

    return AttachedFile


class _Code(Code):  # type: ignore[misc]
    """A code block without a language stays without one (jira2markdown would say Java)."""

    def action(self, tokens: object) -> str:
        lang = str(getattr(tokens, "lang", "") or "").lower()
        text = str(getattr(tokens, "text", "")).strip("\n")
        return f"```{lang}\n{text}\n```"


def to_markdown(text: str | None, ctx: TextContext) -> str | None:
    """Convert one wiki markup body to Markdown with placeholders. ``None`` and blank give ``None``."""
    if text is None or not text.strip():
        return None
    try:
        markdown = str(ctx.grammar().transform_string(text.replace("\r\n", "\n")))
    except Exception:  # noqa: BLE001 - a parser failure keeps the text as Jira wrote it
        ctx.failures += 1
        markdown = text
    return link_issue_keys(markdown, ctx).strip("\n") or None


def link_issue_keys(markdown: str, ctx: TextContext) -> str:
    """Replace issue keys and ``/browse/KEY`` links in *markdown* with issue placeholders."""
    if not ctx.issue_refs:
        return markdown
    out: list[str] = []
    fence: str | None = None
    for line in markdown.split("\n"):
        match = _FENCE_RE.match(line)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        if match:
            fence = match.group(1)
            out.append(line)
            continue
        out.append(_link_line(line, ctx))
    return "\n".join(out)


def _link_line(line: str, ctx: TextContext) -> str:
    parts: list[str] = []
    position = 0
    for match in _INLINE_RE.finditer(line):
        parts.append(_replace_keys(line[position : match.start()], ctx))
        parts.append(_inline(match, ctx))
        position = match.end()
    parts.append(_replace_keys(line[position:], ctx))
    return "".join(parts)


def _inline(match: re.Match[str], ctx: TextContext) -> str:
    whole = match.group(0)
    if match.group("link") is not None:
        key = browse_key(match.group("href"), ctx)
        if key is not None:
            text = match.group("text")
            return issue_placeholder(key, ctx, label=None if text == key else text) or whole
    elif match.group("autolink") is not None:
        key = browse_key(match.group("auto"), ctx)
        if key is not None:
            return issue_placeholder(key, ctx) or whole
    elif match.group("url") is not None:
        url = match.group("url")
        # A sentence's full stop or comma is not part of the URL.
        trimmed = url.rstrip(".,;:!?")
        key = browse_key(trimmed, ctx)
        if key is not None:
            replaced = issue_placeholder(key, ctx)
            if replaced is not None:
                return replaced + url[len(trimmed) :]
    return whole


def browse_key(href: str, ctx: TextContext) -> str | None:
    """The issue key of a link to the site's ``/browse/KEY`` page (a query or fragment after it allowed), or None."""
    if not ctx.site_url:
        return None
    prefix = ctx.site_url.rstrip("/") + "/browse/"
    if href.lower().startswith(prefix.lower()):
        rest = href[len(prefix) :]
    elif href.startswith("/browse/"):
        # A link relative to the site, as Jira writes some itself.
        rest = href[len("/browse/") :]
    else:
        return None
    key = re.split(r"[?#]", rest, maxsplit=1)[0].rstrip("/")
    return key if ISSUE_KEY_RE.fullmatch(key) else None


def _replace_keys(text: str, ctx: TextContext) -> str:
    if not text:
        return text
    return ISSUE_KEY_RE.sub(lambda m: issue_placeholder(m.group(1), ctx) or m.group(0), text)


def issue_placeholder(key: str, ctx: TextContext, *, label: str | None = None) -> str | None:
    """The placeholder for issue *key* (with *label* as its text, when usable), or None for a key Jira does not know."""
    ref = ctx.issue_refs.get(key)
    if ref is None:
        return None
    if label is None and ref not in ctx.in_bundle:
        label = key
    if label is not None and not _usable_label(label):
        label = key if ref not in ctx.in_bundle else None
    return placeholders.issue(ref, label)


def _usable_label(label: str) -> bool:
    return bool(label) and not any(char in label for char in "{}\n") and "]]" not in label
