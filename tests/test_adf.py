"""Atlassian Document Format to Markdown: one class per kind of node, mark or fallback."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from specivo_migration_sdk.placeholders import find_placeholders

from specivo_jira_migration import adf
from specivo_jira_migration.convert import MEDIA_NOT_IN_BUNDLE, AttachmentTarget, TextContext
from tests import adf_builders as b

SITE = "https://example.atlassian.net"
ALICE = "712020:00000000-0000-4000-8000-00000000a11c"
BOT = "557058:00000000-0000-4000-8000-0000000a0700"
NOBODY = "712020:00000000-0000-4000-8000-0000000000ff"
THAI = "ตะกร้าสินค้าของคุณว่างเปล่า เลือกสินค้าที่ชอบแล้วกดชำระเงินได้เลย"
RTL_ARABIC = "سلة التسوق فارغة"
RTL_HEBREW = "עגלת הקניות ריקה"
MARKUP_CHARS = "Literal markup characters: { } | [ ] * _ ~ ^ + - ?? -- {{issue:1}} {code} [~user] !image.png! h1. #"
LONG_LINE = "This line is deliberately long and has no break in it: " + " ".join(
    f"segment-{n:03d} keeps going without a newline" for n in range(1, 41)
)


@pytest.fixture
def ctx() -> TextContext:
    return TextContext(
        site_url=SITE,
        user_refs={ALICE: ALICE},
        display_names={ALICE: "Alice Example", BOT: "Automation for Jira"},
        issue_refs={"ACME-1": "10001", "ACME-2": "10002", "OPS-7": "10102", "OTHER-3": "20003"},
        in_bundle=frozenset({"10001", "10002", "10102"}),
    )


def md(ctx: TextContext, *blocks: Any) -> str | None:
    return adf.to_markdown(b.doc(*blocks), ctx)


def cell(*content: Any, **attrs: Any) -> dict[str, Any]:
    return b.cell(*content, **attrs)


class TestDocuments:
    def test_paragraphs(self, ctx):
        assert md(ctx, "One.", "Two.") == "One.\n\nTwo."

    def test_empty_and_missing(self, ctx):
        assert adf.to_markdown(None, ctx) is None
        assert adf.to_markdown({"type": "doc", "version": 1, "content": []}, ctx) is None
        assert md(ctx, b.p(" ")) is None

    def test_a_string_is_plain_text(self, ctx):
        assert adf.to_markdown("Plain *text* from ACME-1", ctx) == "Plain \\*text\\* from {{issue:10001}}"

    def test_something_else_is_a_failure(self, ctx):
        assert adf.to_markdown(["not", "a", "document"], ctx) is None
        assert ctx.failures == 1

    def test_headings(self, ctx):
        assert md(ctx, b.heading(1, "Title"), b.heading(3, "Part"), b.heading(6, "Small")) == (
            "# Title\n\n### Part\n\n###### Small"
        )

    def test_a_heading_ending_in_a_hash_keeps_it(self, ctx):
        assert md(ctx, b.heading(2, "Use C#")) == "## Use C\\#"

    def test_a_hard_break(self, ctx):
        assert md(ctx, b.p("one", b.hard_break(), "two")) == "one  \ntwo"

    def test_rule_and_quote(self, ctx):
        assert md(ctx, "Above", b.rule(), b.quote("Quoted", "Second")) == "Above\n\n---\n\n> Quoted\n>\n> Second"

    def test_page_layouts_keep_their_columns_in_order(self, ctx):
        layout = {
            "type": "layoutSection",
            "content": [
                {"type": "layoutColumn", "attrs": {"width": 50}, "content": [b.p("Left")]},
                {"type": "layoutColumn", "attrs": {"width": 50}, "content": [b.p("Right")]},
            ],
        }
        assert md(ctx, layout) == "Left\n\nRight"
        assert not ctx.notes


class TestMarks:
    def test_basic(self, ctx):
        text = md(ctx, b.p(b.bold("b"), " ", b.italic("i"), " ", b.code("c")))
        assert text == "**b** *i* `c`"

    def test_strike_underline_sub_sup_are_html(self, ctx):
        text = md(ctx, b.p(b.strike("old"), " ", b.underline("key"), " H", b.sub("2"), "O x", b.sup("2")))
        assert text == "<del>old</del> <u>key</u> H<sub>2</sub>O x<sup>2</sup>"

    def test_two_marks_on_one_text(self, ctx):
        assert md(ctx, b.p(b.text("both", b.STRONG, b.EM))) == "***both***"

    def test_a_run_of_texts_with_one_mark_is_wrapped_once(self, ctx):
        assert md(ctx, b.p(b.bold("one "), b.text("two", b.STRONG, b.EM))) == "**one *two***"

    def test_spaces_stay_outside_the_markers(self, ctx):
        assert md(ctx, b.p("a", b.bold(" bold "), "b")) == "a **bold** b"

    def test_colours_are_dropped(self, ctx):
        background = {"type": "backgroundColor", "attrs": {"color": "#fedec8"}}
        assert md(ctx, b.p(b.coloured("red", "#bf2600"), " ", b.text("marked", background))) == "red marked"
        assert not ctx.notes

    def test_an_unknown_mark_keeps_the_text_and_is_counted(self, ctx):
        assert md(ctx, b.p(b.text("shiny", {"type": "sparkle"}))) == "shiny"
        assert ctx.notes == {adf.UNKNOWN_MARK.format("sparkle"): 1}

    def test_code_keeps_backticks_and_markup_literal(self, ctx):
        assert md(ctx, b.p(b.code("a `b` *c* ACME-1"))) == "``a `b` *c* ACME-1``"
        assert md(ctx, b.p(b.code("`start"))) == "`` `start ``"

    def test_a_link(self, ctx):
        assert (
            md(ctx, b.p(b.link("the docs", "https://docs.example.com/a"))) == "[the docs](https://docs.example.com/a)"
        )

    def test_a_link_with_formatting(self, ctx):
        text = md(ctx, b.p(b.link("brief", "https://design.example.com/x", b.STRONG)))
        assert text == "[**brief**](https://design.example.com/x)"

    def test_a_link_whose_text_is_its_url(self, ctx):
        url = "https://docs.example.com/a_b"
        assert md(ctx, b.p(b.link(url, url))) == f"<{url}>"

    def test_a_link_target_with_spaces_and_parentheses(self, ctx):
        text = md(ctx, b.p(b.link("x", "https://example.com/a b(c)")))
        assert text == "[x](https://example.com/a%20b%28c%29)"


class TestLists:
    def test_bullets(self, ctx):
        assert md(ctx, b.bullets("one", "two")) == "- one\n- two"

    def test_ordered_keeps_its_numbers(self, ctx):
        assert md(ctx, b.ordered("a", "b", start=3)) == "3. a\n4. b"

    def test_mixed_nesting_indents_by_four(self, ctx):
        nested = b.ordered(
            b.item("Contact", b.bullets(b.item("E-mail", b.ordered("On blur", "Typos")), "Phone")),
            "Payment",
        )
        assert md(ctx, nested) == (
            "1. Contact\n    - E-mail\n        1. On blur\n        2. Typos\n    - Phone\n2. Payment"
        )

    def test_two_lists_in_a_row_stay_two(self, ctx):
        assert md(ctx, b.bullets("a"), b.ordered("b")) == "- a\n\n<!-- -->\n\n1. b"

    def test_an_item_with_two_paragraphs(self, ctx):
        assert md(ctx, b.bullets(b.item("first", "second"))) == "- first\n\n    second"

    def test_code_in_an_item_is_indented(self, ctx):
        text = md(ctx, b.bullets(b.item("Run:", b.code_block("make test", "bash"))))
        assert text == "- Run:\n\n        :::bash\n        make test"

    def test_an_item_that_starts_with_code(self, ctx):
        item = {"type": "listItem", "content": [b.code_block("x = 1")]}
        assert md(ctx, {"type": "bulletList", "content": [item]}) == "- <span></span>\n\n        x = 1"

    def test_a_list_item_text_that_looks_like_markup(self, ctx):
        assert md(ctx, b.bullets("# not a heading", "1. not a number")) == "- \\# not a heading\n- 1\\. not a number"


class TestTasksAndDecisions:
    def test_tasks_keep_their_state(self, ctx):
        assert md(ctx, b.tasks((True, "Draft"), (False, "Review"))) == "- [x] Draft\n- [ ] Review"

    def test_a_nested_task_list(self, ctx):
        tasks = b.tasks((True, "Parent"), (False, "Next"))
        tasks["content"].insert(1, b.tasks((False, "Child")))
        assert md(ctx, tasks) == "- [x] Parent\n    - [ ] Child\n- [ ] Next"

    def test_decisions(self, ctx):
        decisions = b.decisions("Ship it.", "Wait.")
        decisions["content"][1]["attrs"]["state"] = "UNDECIDED"
        assert md(ctx, decisions) == "- **Decision:** Ship it.\n- **Decision (open):** Wait."


class TestPanelsAndExpands:
    @pytest.mark.parametrize(
        ("kind", "label"),
        [("info", "Info"), ("note", "Note"), ("warning", "Warning"), ("error", "Error"), ("success", "Success")],
    )
    def test_a_panel_is_a_quote_with_its_type(self, ctx, kind, label):
        assert md(ctx, b.panel(kind, "Careful.")) == f"> **{label}:** Careful."

    def test_a_custom_panel_reads_note(self, ctx):
        assert md(ctx, b.panel("custom", "Text")) == "> **Note:** Text"  # type: ignore[arg-type]

    def test_a_panel_that_starts_with_a_list(self, ctx):
        assert md(ctx, b.panel("info", b.bullets("a", "b"))) == "> **Info**\n>\n> - a\n> - b"

    def test_an_expand_is_a_quote_under_its_title(self, ctx):
        expand = b.expand("Details here", "Hidden text", b.code_block("x", "python"))
        assert md(ctx, expand) == "> **Details here**\n>\n> Hidden text\n>\n>     :::python\n>     x"

    def test_a_nested_expand_and_one_without_a_title(self, ctx):
        nested = {"type": "nestedExpand", "attrs": {"title": ""}, "content": [b.p("Inside")]}
        assert md(ctx, nested) == "> **Details**\n>\n> Inside"


class TestCodeBlocks:
    def test_fenced_with_its_language(self, ctx):
        assert md(ctx, b.code_block("print(1)", "python")) == "```python\nprint(1)\n```"

    def test_without_a_language(self, ctx):
        assert md(ctx, b.code_block("make test")) == "```\nmake test\n```"

    def test_backticks_inside_get_a_longer_fence(self, ctx):
        assert md(ctx, b.code_block("```\nx\n```")) == "````\n```\nx\n```\n````"

    def test_nothing_inside_is_converted(self, ctx):
        assert md(ctx, b.code_block("ACME-1 *x* {x}")) == "```\nACME-1 *x* {x}\n```"

    def test_a_placeholder_in_fenced_code_is_neutralised(self, ctx):
        assert md(ctx, b.code_block("{{issue:1}} {{note}}")) == "```\n{\u200b{issue:1}} {{note}}\n```"

    def test_an_odd_language_is_dropped(self, ctx):
        assert md(ctx, b.code_block("x", "c++ <script>")) == "```\nx\n```"

    def test_indented_code_cannot_hold_a_placeholder(self, ctx):
        text = md(ctx, b.quote(b.code_block("{{issue:1}}")))
        assert text is not None
        assert list(find_placeholders(text)) == []
        assert "{\u200b{issue:1}}" in text

    def test_indented_code_whose_first_line_looks_like_a_language(self, ctx):
        assert md(ctx, b.quote(b.code_block("#!/bin/sh\necho"))) == ">     :::text\n>     #!/bin/sh\n>     echo"


class TestPlaceholderInjection:
    """Text a person wrote never becomes a placeholder, wherever it sits: code, lozenges, URLs."""

    HOSTILE = "{{issue:10001}}{{user:" + ALICE + "}}{{attachment:a1}}{{wiki:w1|x}}"

    def test_a_hostile_document(self, ctx):
        h = self.HOSTILE
        fence_breaker = f"```\n{h}\n````\n{h}\n~~~ {h}"
        external = {"type": "media", "attrs": {"type": "external", "url": f"https://img.example.com/{h}.png"}}
        doc = b.doc(
            b.p(b.code(h), " ", b.status(h, "red"), " ", b.link("click", f"https://example.com/?q={h}")),
            b.p(
                b.link(f"https://example.com/{h}", f"https://example.com/{h}"), " ", b.inline_card(f"https://e.com/{h}")
            ),
            b.code_block(fence_breaker),
            b.bullets(b.item("in a list", b.code_block(h))),
            b.table(b.row(b.th("code")), b.row(cell(b.code_block(h), b.p(b.code(h))))),
            {"type": "mediaSingle", "attrs": {"layout": "center"}, "content": [external]},
            b.p("Real ones: ", b.mention(ALICE), " ACME-1"),
        )
        text = adf.to_markdown(doc, ctx)
        assert text is not None
        # Searched over the whole text: fenced code is not trusted to be skipped.
        found = re.findall(r"\{\{(?:issue|user|attachment|wiki):[^{}\n]*\}\}", text)
        assert found == [f"{{{{user:{ALICE}}}}}", "{{issue:10001}}"]

    def test_urls_with_braces_are_never_autolinks(self, ctx):
        text = md(ctx, b.p(b.inline_card("https://e.com/{{issue:1}}")))
        assert text == "[https://e.com/\\{\\{issue:1}}](https://e.com/%7B%7Bissue:1%7D%7D)"


class TestInlineNodes:
    def test_status_lozenge(self, ctx):
        assert md(ctx, b.p("Backend ", b.status("IN PROGRESS", "blue"))) == "Backend `IN PROGRESS`"

    def test_date_as_iso(self, ctx):
        from datetime import date

        assert md(ctx, b.p("by ", b.date_node(date(2026, 11, 19)))) == "by 2026-11-19"

    def test_emoji(self, ctx):
        no_text = {"type": "emoji", "attrs": {"shortName": ":custom_party:", "id": "x"}}
        assert md(ctx, b.p(b.emoji(":rocket:", "🚀"), " ", no_text)) == "🚀 :custom_party:"

    def test_a_mention_of_a_carried_user(self, ctx):
        assert md(ctx, b.p("Thanks ", b.mention(ALICE), "!")) == f"Thanks {{{{user:{ALICE}}}}}!"

    def test_a_mention_of_a_user_left_out(self, ctx):
        assert md(ctx, b.p("by ", b.mention(BOT))) == "by @Automation for Jira"

    def test_a_mention_of_an_unknown_user_uses_the_text_jira_wrote(self, ctx):
        mention = {"type": "mention", "attrs": {"id": NOBODY, "text": "@Former Staff"}}
        assert md(ctx, b.p("cc ", mention)) == "cc @Former Staff"
        assert md(ctx, b.p("cc ", {"type": "mention", "attrs": {"id": "abc"}})) == "cc @abc"

    def test_an_editor_placeholder_is_dropped(self, ctx):
        assert md(ctx, b.p("a", {"type": "placeholder", "attrs": {"text": "Type here"}}, "b")) == "ab"


class TestSmartLinks:
    def test_an_issue_of_the_bundle(self, ctx):
        assert md(ctx, b.p("See ", b.inline_card(f"{SITE}/browse/ACME-2"), ".")) == "See {{issue:10002}}."

    def test_an_issue_outside_the_bundle(self, ctx):
        assert md(ctx, b.p(b.inline_card(f"{SITE}/browse/OTHER-3"))) == "{{issue:20003|OTHER-3}}"

    def test_a_link_to_a_comment_of_an_issue(self, ctx):
        card = b.inline_card(f"{SITE}/browse/ACME-1?focusedCommentId=10100")
        assert md(ctx, b.p(card)) == "{{issue:10001}}"

    def test_an_issue_jira_does_not_know(self, ctx):
        url = f"{SITE}/browse/ACME-404"
        assert md(ctx, b.p(b.inline_card(url))) == f"<{url}>"

    def test_any_other_page(self, ctx):
        assert md(ctx, b.p(b.inline_card("https://docs.example.com/page"))) == "<https://docs.example.com/page>"

    def test_block_and_embed_cards(self, ctx):
        block = {"type": "blockCard", "attrs": {"url": f"{SITE}/browse/ACME-1"}}
        embed = {"type": "embedCard", "attrs": {"url": "https://video.example.com/v/1", "layout": "center"}}
        assert md(ctx, block, embed) == "{{issue:10001}}\n\n<https://video.example.com/v/1>"

    def test_a_card_given_by_its_data(self, ctx):
        card = {"type": "inlineCard", "attrs": {"data": {"@type": "Document", "url": "https://docs.example.com/d"}}}
        assert md(ctx, b.p(card)) == "<https://docs.example.com/d>"

    def test_never_the_wiki_markup_form(self, ctx):
        text = md(ctx, b.p(b.inline_card("https://docs.example.com/page")))
        assert text is not None
        assert "smart-link" not in text and "|" not in text


class TestIssueKeys:
    def test_keys_in_text(self, ctx):
        assert md(ctx, "Fixed by ACME-2; was OPS-7; see OTHER-3.") == (
            "Fixed by {{issue:10002}}; was {{issue:10102}}; see {{issue:20003|OTHER-3}}."
        )

    def test_unknown_keys_stay_text(self, ctx):
        assert md(ctx, "UTF-8 and ACME-404") == "UTF-8 and ACME-404"

    def test_not_in_code_or_link_text(self, ctx):
        link = b.link("ACME-1 notes", "https://docs.example.com/ACME-1")
        text = md(ctx, b.p(b.code("ACME-1"), " ", link), b.code_block("ACME-2"))
        assert text == "`ACME-1` [ACME-1 notes](https://docs.example.com/ACME-1)\n\n```\nACME-2\n```"

    def test_a_link_to_an_issue_page(self, ctx):
        assert md(ctx, b.p(b.link("the bug", f"{SITE}/browse/ACME-1"))) == "{{issue:10001|the bug}}"
        assert md(ctx, b.p(b.link("ACME-1", f"{SITE}/browse/ACME-1"))) == "{{issue:10001}}"
        assert md(ctx, b.p(b.link("other", "/browse/OTHER-3"))) == "{{issue:20003|other}}"

    def test_a_plain_url_to_an_issue_page(self, ctx):
        assert md(ctx, f"see {SITE}/browse/OTHER-3.") == "see {{issue:20003|OTHER-3}}."

    def test_a_url_to_another_page_stays(self, ctx):
        assert md(ctx, "see https://docs.example.com/ACME-1") == "see https://docs.example.com/ACME-1"


class TestEscaping:
    def test_markup_characters_stay_literal(self, ctx):
        text = md(ctx, MARKUP_CHARS)
        assert text == (
            "Literal markup characters: \\{ } | \\[ \\] \\* \\_ ~ ^ + - ?? -- \\{\\{issue:1}} \\{code} "
            "\\[~user\\] !image.png! h1. #"
        )

    def test_nothing_that_looks_like_a_placeholder_is_one(self, ctx):
        text = md(ctx, "A literal {{issue:1}} and {{user:42}} and {{wiki:Page|x}}")
        assert text is not None
        assert list(find_placeholders(text)) == []

    def test_a_placeholder_split_over_two_texts(self, ctx):
        text = md(ctx, b.p("{", b.bold("{issue:1}}")))
        assert text is not None
        assert list(find_placeholders(text)) == []

    def test_backslashes(self, ctx):
        assert md(ctx, "C:\\temp\\*.log") == "C:\\\\temp\\\\\\*.log"

    def test_html_is_text(self, ctx):
        assert md(ctx, "<b>not bold</b> &lt; a < b & c") == "&lt;b>not bold&lt;/b> &amp;lt; a < b & c"

    def test_underscores_inside_words_are_left(self, ctx):
        assert md(ctx, "snake_case and _emphasis_") == "snake_case and \\_emphasis\\_"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("# not a heading", "\\# not a heading"),
            ("- not a list", "\\- not a list"),
            ("+ not a list", "\\+ not a list"),
            ("12. not a number", "12\\. not a number"),
            ("> not a quote", "\\> not a quote"),
            ("===", "&#61;=="),
            ("---", "\\---"),
            ("    not code", "not code"),
        ],
    )
    def test_what_would_start_a_block(self, ctx, text, expected):
        assert md(ctx, text) == expected

    def test_after_a_hard_break_too(self, ctx):
        assert md(ctx, b.p("Title", b.hard_break(), "---")) == "Title  \n\\---"


class TestTables:
    def test_a_simple_table(self, ctx):
        table = b.table(b.row(b.th("Step"), b.th("Thai")), b.row("Button", "ชำระเงิน"))
        assert md(ctx, table) == "| Step | Thai |\n| --- | --- |\n| Button | ชำระเงิน |"

    def test_a_table_without_a_header_row(self, ctx):
        assert md(ctx, b.table(b.row("a", "b"))) == "|  |  |\n| --- | --- |\n| a | b |"

    def test_a_header_column(self, ctx):
        table = b.table(b.row(b.th("Week"), b.th("Orders")), b.row(b.th("W41"), "1 388"))
        assert md(ctx, table) == "| Week | Orders |\n| --- | --- |\n| **W41** | 1 388 |"

    def test_pipes_and_breaks_in_cells(self, ctx):
        table = b.table(b.row(b.th("A")), b.row(b.cell(b.p("x | y", b.hard_break(), b.code("a|b")))))
        assert md(ctx, table) == "| A |\n| --- |\n| x \\| y<br>`a|b` |"

    def test_a_merged_cell_is_written_unmerged(self, ctx):
        table = b.table(
            b.row(b.th("Week"), b.th("Sessions"), b.th("Orders")),
            b.row(b.th("W42"), cell("both", colspan=2)),
            b.row(cell("tall", rowspan=2), "1", "2"),
            b.row("3", "4"),
        )
        assert md(ctx, table) == (
            "| Week | Sessions | Orders |\n| --- | --- | --- |\n| **W42** | both |  |\n| tall | 1 | 2 |\n|  | 3 | 4 |"
        )
        assert ctx.notes == {adf.MERGED_CELLS: 2}

    def test_lists_and_code_in_a_cell_fold_into_one_line(self, ctx):
        table = b.table(
            b.row(b.th("Notes")),
            b.row(cell(b.bullets("Payment", b.item("Rain", b.ordered("one"))), b.code_block("SELECT 1;", "sql"))),
        )
        assert md(ctx, table) == (
            "| Notes |\n| --- |\n| • Payment<br>• Rain<br>&nbsp;&nbsp;&nbsp;&nbsp;1. one<br>`SELECT 1;` |"
        )

    def test_tasks_panels_and_references_in_a_cell(self, ctx):
        table = b.table(
            b.row(b.th("What")),
            b.row(cell(b.tasks((True, "Done")), b.panel("warning", "Careful"), b.p(b.mention(ALICE), " ACME-1"))),
        )
        assert md(ctx, table) == (
            f"| What |\n| --- |\n| [x] Done<br>**Warning:**<br>Careful<br>{{{{user:{ALICE}}}}} {{{{issue:10001}}}} |"
        )

    def test_a_table_in_a_cell_follows_the_table(self, ctx):
        inner = b.table(b.row(b.th("x")), b.row("1"))
        outer = b.table(b.row(b.th("A"), b.th("B")), b.row("a", cell(b.p("see"), inner)))
        assert md(ctx, outer) == (
            "| A | B |\n| --- | --- |\n| a | see<br>*(table 1 below)* |\n\n"
            "*Table in row 2, column 2:*\n\n| x |\n| --- |\n| 1 |"
        )
        assert ctx.notes == {adf.NESTED_TABLES: 1}

    def test_a_table_in_a_quote(self, ctx):
        assert md(ctx, b.expand("More", b.table(b.row(b.th("a")), b.row("1")))) == (
            "> **More**\n>\n> | a |\n> | --- |\n> | 1 |"
        )


SHOT = "5e1f0000-0000-4000-8000-000000010201"
LOG = "5e1f0000-0000-4000-8000-000000010200"
PDF = "5e1f0000-0000-4000-8000-000000010203"
BIG = "5e1f0000-0000-4000-8000-000000010202"
ODD = "5e1f0000-0000-4000-8000-000000010209"


def _media(media_id: str, **attrs: Any) -> dict[str, Any]:
    return {"type": "media", "attrs": {"type": "file", "id": media_id, "collection": "", **attrs}}


@pytest.fixture
def media_ctx(ctx: TextContext) -> TextContext:
    ctx.media = {
        SHOT: AttachmentTarget("screenshot.png", ref="10201", content_type="image/png"),
        LOG: AttachmentTarget("console.log", ref="10200", content_type="text/plain"),
        PDF: AttachmentTarget("invoice.pdf", ref="10203", content_type="application/pdf"),
        BIG: AttachmentTarget("recording.mp4", content_type="video/mp4", reason="not carried over, too large"),
        ODD: AttachmentTarget("odd [1] {{user:x}}.png", ref="10209", content_type="image/png"),
    }
    return ctx


class TestMedia:
    def test_an_image_in_the_text_is_its_attachment(self, media_ctx):
        assert md(media_ctx, "Before:", b.media_single(SHOT, width=50)) == (
            "Before:\n\n![screenshot.png]({{attachment:10201}})"
        )
        assert not media_ctx.notes

    def test_the_width_is_dropped(self, media_ctx):
        # Specivo resolves attachments in Markdown images only, never in an <img src>.
        text = md(media_ctx, b.media_single(SHOT, width=30))
        assert text == "![screenshot.png]({{attachment:10201}})"
        assert "<img" not in text and "30" not in text

    def test_a_file_in_a_media_single_that_is_no_image_is_a_link(self, media_ctx):
        assert md(media_ctx, b.media_single(PDF)) == "[invoice.pdf]({{attachment:10203}})"

    def test_a_media_group_holds_links(self, media_ctx):
        group = {"type": "mediaGroup", "content": [_media(LOG), _media(SHOT)]}
        assert md(media_ctx, group) == "[console.log]({{attachment:10200}}) [screenshot.png]({{attachment:10201}})"

    def test_an_inline_file_is_a_link(self, media_ctx):
        inline = {"type": "mediaInline", "attrs": {"type": "file", "id": LOG, "collection": ""}}
        assert md(media_ctx, b.p("See ", inline, " for details.")) == (
            "See [console.log]({{attachment:10200}}) for details."
        )

    def test_the_alt_text_is_the_label(self, media_ctx):
        node = {"type": "mediaSingle", "attrs": {"layout": "center"}, "content": [_media(SHOT, alt="Login page")]}
        assert md(media_ctx, node) == "![Login page]({{attachment:10201}})"

    def test_a_name_cannot_close_the_label_or_make_a_placeholder(self, media_ctx):
        text = md(media_ctx, b.media_single(ODD))
        assert text == "![odd \\[1\\] \\{\\{user:x\\}\\}.png]({{attachment:10209}})"
        assert [p.ref for p in find_placeholders(text)] == ["10209"]

    def test_an_image_in_a_table_cell(self, media_ctx):
        table = b.table(b.row(b.th("Shot")), b.row(cell(b.media_single(SHOT))))
        assert md(media_ctx, table) == "| Shot |\n| --- |\n| ![screenshot.png]({{attachment:10201}}) |"

    def test_a_file_the_bundle_does_not_hold_is_a_note_saying_why(self, media_ctx):
        group = {"type": "mediaGroup", "content": [_media(BIG)]}
        assert md(media_ctx, group) == "*(attachment recording.mp4: not carried over, too large)*"
        assert media_ctx.notes == {MEDIA_NOT_IN_BUNDLE: 1}

    def test_a_file_of_another_issue_is_a_note(self, media_ctx):
        assert md(media_ctx, b.media_single("0a1b2c3d")) == "*(an attachment is shown here in Jira)*"
        media = b.media_single("0a1b2c3d")
        media["content"][0]["attrs"]["alt"] = "screen [1].png"
        assert md(media_ctx, media) == "*(attachment: screen \\[1\\].png)*"
        assert media_ctx.notes == {MEDIA_NOT_IN_BUNDLE: 2}

    def test_without_attachments_every_file_is_a_note(self, ctx):
        media = {"type": "media", "attrs": {"type": "file", "id": "1", "collection": ""}}
        inline = {"type": "mediaInline", "attrs": {"type": "file", "id": "2", "collection": ""}}
        assert md(ctx, {"type": "mediaGroup", "content": [media, media]}, b.p("file ", inline)) == (
            "*(an attachment is shown here in Jira)* *(an attachment is shown here in Jira)*\n\n"
            "file *(an attachment is shown here in Jira)*"
        )
        assert ctx.notes == {MEDIA_NOT_IN_BUNDLE: 3}

    def test_an_external_image(self, ctx):
        media = {"type": "media", "attrs": {"type": "external", "url": "https://img.example.com/a.png", "alt": "a"}}
        assert md(ctx, {"type": "mediaSingle", "attrs": {"layout": "center"}, "content": [media]}) == (
            "![a](https://img.example.com/a.png)"
        )
        assert not ctx.notes


class TestUnknownNodes:
    def test_an_unknown_block_keeps_its_text(self, ctx):
        node = {"type": "bodiedExtension", "attrs": {"extensionKey": "x"}, "content": [b.p("Kept ", b.bold("text"))]}
        assert md(ctx, node) == "Kept **text**"
        assert ctx.notes == {adf.UNKNOWN_NODE.format("bodiedExtension"): 1}

    def test_an_unknown_inline_node_keeps_its_text(self, ctx):
        node = {"type": "inlineThing", "attrs": {"text": "ACME-1 here"}}
        assert md(ctx, b.p("a ", node)) == "a {{issue:10001}} here"
        assert ctx.notes == {adf.UNKNOWN_NODE.format("inlineThing"): 1}

    def test_an_extension_without_text_is_counted(self, ctx):
        node = {"type": "extension", "attrs": {"extensionKey": "chart", "parameters": {"a": 1}}}
        assert md(ctx, "Before", node, "After") == "Before\n\nAfter"
        assert ctx.notes == {adf.UNKNOWN_NODE.format("extension"): 1}

    def test_counts_per_type(self, ctx):
        first, second = {"type": "x1", "content": [b.p("a")]}, {"type": "x1", "content": [b.p("b")]}
        md(ctx, first, second, {"type": "x2", "text": "c"})
        assert ctx.notes == {adf.UNKNOWN_NODE.format("x1"): 2, adf.UNKNOWN_NODE.format("x2"): 1}

    @pytest.mark.parametrize(
        "doc",
        [
            {"type": "doc", "content": "not a list"},
            {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text"}]}]},
            {"type": "doc", "content": [{"type": "table", "content": [{"type": "tableRow", "content": None}]}]},
            {
                "type": "doc",
                "content": [{"type": "heading", "attrs": "bad", "content": [{"type": "text", "text": "x"}]}],
            },
            {"type": "doc", "content": [{"type": "orderedList", "attrs": {"order": "x"}, "content": [1, None]}]},
            {"type": "doc", "content": [{"type": "text", "text": "loose", "marks": ["bad", {"type": None}]}]},
        ],
    )
    def test_malformed_documents_never_fail_or_leak_json(self, ctx, doc):
        text = adf.to_markdown(doc, ctx) or ""
        assert "{" not in text.replace("\\{", "") and '"type"' not in text
        assert ctx.failures == 0


class TestTextThatAlreadyWorked:
    def test_thai_right_to_left_and_emoji(self, ctx):
        line = f"{THAI} / {RTL_ARABIC} / {RTL_HEBREW} 🇹🇭 👍🏽"
        assert md(ctx, line) == line

    def test_a_long_line_is_not_wrapped(self, ctx):
        assert md(ctx, LONG_LINE) == LONG_LINE


class TestLookups:
    def test_candidate_keys(self):
        doc = b.doc(
            b.p("ACME-1 and ", b.code("CODE-1"), " ", b.link("LINKED-1", f"{SITE}/browse/LINK-2")),
            b.p(b.inline_card(f"{SITE}/browse/CARD-3"), f" {SITE}/browse/URL-4 https://docs.example.com/DOC-5"),
            b.code_block("BLOCK-6"),
        )
        assert adf.candidate_keys(doc, SITE) == {"ACME-1", "LINK-2", "CARD-3", "URL-4"}
        assert adf.candidate_keys(doc) == {"ACME-1"}

    def test_mentioned_ids(self):
        doc = b.doc(b.p(b.mention(ALICE), " and ", b.mention(NOBODY)), b.bullets(b.item(b.p(b.mention(BOT)))))
        assert adf.mentioned_ids(doc) == {ALICE, NOBODY, BOT}


class TestPlainText:
    """ADF flattened to text, for a field Specivo does not render as Markdown (a worklog's comment)."""

    def test_blocks_become_lines_and_marks_are_dropped(self):
        body = b.doc(
            b.heading(2, "Summary"),
            b.p(b.bold("Fixed"), " the ", b.code("Set-Cookie"), " header,", b.hard_break(), "then retested."),
            b.bullets("Safari", "Firefox"),
            b.code_block("curl -I https://shop.example.com"),
        )
        assert adf.to_plain_text(body) == (
            "Summary\nFixed the Set-Cookie header,\nthen retested.\nSafari\nFirefox\ncurl -I https://shop.example.com"
        )

    def test_inline_nodes(self):
        body = b.doc(
            b.p(
                b.mention(ALICE), " and ", b.mention(NOBODY), " on ", b.date_node(date(2026, 11, 19)), ": ",
                b.status("IN REVIEW", "blue"), " ", b.emoji(":rocket:", "🚀"), " ", b.inline_card("https://example.com/x"),
            )
        )  # fmt: skip
        text = adf.to_plain_text(body, {ALICE: "Alice Example"})
        assert text == "@Alice Example and @user on 2026-11-19: IN REVIEW 🚀 https://example.com/x"

    def test_media_leaves_nothing_and_empty_is_none(self):
        assert adf.to_plain_text(b.doc(b.media_single("5e1f0000-0000-4000-8000-000000010200"))) is None
        assert adf.to_plain_text(b.doc(b.p("  "))) is None
        assert adf.to_plain_text(None) is None
        assert adf.to_plain_text("  plain  ") == "plain"
        assert adf.to_plain_text(b.doc(b.p(THAI))) == THAI
