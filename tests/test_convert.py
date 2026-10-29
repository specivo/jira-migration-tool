"""Jira wiki markup to Markdown, and references to placeholders."""

from __future__ import annotations

import pytest

from specivo_jira_migration.convert import (
    MEDIA_NOT_IN_BUNDLE,
    AttachmentTarget,
    TextContext,
    candidate_keys,
    link_issue_keys,
    mentioned_ids,
    to_markdown,
)

SITE = "https://example.atlassian.net"
ALICE = "712020:00000000-0000-4000-8000-00000000a11c"
BOT = "557058:00000000-0000-4000-8000-0000000a0700"


@pytest.fixture
def ctx() -> TextContext:
    return TextContext(
        site_url=SITE,
        user_refs={ALICE: ALICE, "bob": "JIRAUSER10101"},
        display_names={ALICE: "Alice Example", BOT: "Automation for Jira"},
        issue_refs={"ACME-1": "10001", "ACME-2": "10002", "OPS-7": "10102", "OTHER-3": "20003"},
        in_bundle=frozenset({"10001", "10002", "10102"}),
    )


def md(text: str, ctx: TextContext) -> str | None:
    return to_markdown(text, ctx)


class TestMarkup:
    def test_headings(self, ctx):
        assert md("h1. Title\n\nh3. Part", ctx) == "# Title\n\n### Part"

    def test_lists(self, ctx):
        assert md("* one\n** nested\n* two", ctx) == "- one\n  - nested\n- two"
        assert md("# first\n# second", ctx) == "1. first\n1. second"

    def test_code_keeps_its_language(self, ctx):
        assert md("{code:python}\nprint(1)\n{code}", ctx) == "```python\nprint(1)\n```"

    def test_code_without_a_language_gets_none(self, ctx):
        assert md("{code}\nmake test\n{code}", ctx) == "```\nmake test\n```"

    def test_noformat(self, ctx):
        assert md("{noformat}\nraw *text*\n{noformat}", ctx) == "```\nraw *text*\n```"

    def test_inline_effects(self, ctx):
        assert md("*bold* _italic_ -struck- {{mono}}", ctx) == "**bold** _italic_ ~~struck~~ `mono`"

    def test_tables(self, ctx):
        assert md("||Browser||Result||\n|Safari|fails|", ctx) == "|Browser|Result|\n|---|---|\n|Safari|fails|"

    def test_links(self, ctx):
        assert md("[the docs|https://docs.example.com/a]", ctx) == "[the docs](https://docs.example.com/a)"
        assert md("[https://docs.example.com/a]", ctx) == "<https://docs.example.com/a>"

    def test_blank_is_none(self, ctx):
        assert md("", ctx) is None
        assert md("  \n ", ctx) is None
        assert to_markdown(None, ctx) is None

    def test_windows_line_endings(self, ctx):
        assert md("h2. A\r\n\r\ntext", ctx) == "## A\n\ntext"


class TestMentions:
    def test_a_carried_user_becomes_a_placeholder(self, ctx):
        assert md(f"Thanks [~accountid:{ALICE}]!", ctx) == f"Thanks {{{{user:{ALICE}}}}}!"

    def test_a_data_center_user_name_maps_to_the_user_key(self, ctx):
        assert md("ask [~bob] first", ctx) == "ask {{user:JIRAUSER10101}} first"

    def test_a_user_the_bundle_leaves_out_gets_their_display_name(self, ctx):
        assert md(f"by [~accountid:{BOT}]", ctx) == "by @Automation for Jira"

    def test_an_unknown_user_keeps_the_id(self, ctx):
        assert md("cc [~accountid:999999:aaaa]", ctx) == "cc @999999:aaaa"

    def test_a_display_name_cannot_form_markup_or_a_placeholder(self, ctx):
        ctx.display_names["999999:bbbb"] = "{{user:1}} <b>*x*</b>"
        assert md("cc [~accountid:999999:bbbb]", ctx) == r"cc @\{\{user:1\}\} \<b>\*x\*\</b>"

    def test_mentions_are_found_in_raw_markup(self):
        assert mentioned_ids(f"[~accountid:{ALICE}] and [Bob|~bob]") == {ALICE, "bob"}


class TestIssueKeys:
    def test_a_key_in_the_bundle(self, ctx):
        assert md("Fixed by ACME-2.", ctx) == "Fixed by {{issue:10002}}."

    def test_an_old_key_of_an_issue_in_the_bundle(self, ctx):
        assert md("Was OPS-7.", ctx) == "Was {{issue:10102}}."

    def test_a_known_key_outside_the_bundle_keeps_its_key_as_label(self, ctx):
        assert md("See OTHER-3", ctx) == "See {{issue:20003|OTHER-3}}"

    def test_unknown_keys_stay_text(self, ctx):
        assert md("UTF-8 and ACME-404 and X-1", ctx) == "UTF-8 and ACME-404 and X-1"

    def test_not_inside_words_or_paths(self, ctx):
        assert md("xACME-1 ACME-1x ACME-12 /ACME-1 ACME-1.5", ctx) == "xACME-1 ACME-1x ACME-12 /ACME-1 ACME-1.5"

    def test_not_inside_code(self, ctx):
        assert md("{{ACME-1}}", ctx) == "`ACME-1`"
        assert md("{code}\nACME-1\n{code}\nACME-1", ctx) == "```\nACME-1\n```\n{{issue:10001}}"

    def test_not_inside_a_link_or_url(self, ctx):
        assert md("[ACME-1 notes|https://docs.example.com/ACME-1]", ctx) == (
            "[ACME-1 notes](https://docs.example.com/ACME-1)"
        )
        assert md("https://docs.example.com/ACME-1", ctx) == "https://docs.example.com/ACME-1"

    def test_a_link_to_the_issue_page(self, ctx):
        assert md(f"[{SITE}/browse/ACME-1]", ctx) == "{{issue:10001}}"
        assert md(f"[the bug|{SITE}/browse/ACME-1]", ctx) == "{{issue:10001|the bug}}"
        assert md(f"[ACME-1|{SITE}/browse/ACME-1]", ctx) == "{{issue:10001}}"
        assert md(f"see {SITE}/browse/OTHER-3.", ctx) == "see {{issue:20003|OTHER-3}}."

    def test_a_link_to_another_site_page_stays(self, ctx):
        text = f"[board|{SITE}/jira/software/projects/ACME/boards/1]"
        assert md(text, ctx) == f"[board]({SITE}/jira/software/projects/ACME/boards/1)"

    def test_a_label_that_cannot_be_one_falls_back(self, ctx):
        assert link_issue_keys(f"[a {{x}} b]({SITE}/browse/ACME-1)", ctx) == "{{issue:10001}}"
        assert link_issue_keys(f"[a {{x}} b]({SITE}/browse/OTHER-3)", ctx) == "{{issue:20003|OTHER-3}}"

    def test_existing_placeholders_are_left_alone(self, ctx):
        assert link_issue_keys("{{user:ACME-1}} ACME-1", ctx) == "{{user:ACME-1}} {{issue:10001}}"

    def test_candidates_include_links_to_issue_pages(self):
        text = f"[the bug|{SITE}/browse/OTHER-3] and {SITE.upper()}/browse/OTHER-4?focusedCommentId=1 and /x/OTHER-5"
        assert candidate_keys(text, SITE) == {"OTHER-3", "OTHER-4"}
        assert candidate_keys(text) == set()

    def test_a_link_to_an_issue_page_with_a_query(self, ctx):
        assert md(f"[comment|{SITE}/browse/OTHER-3?focusedCommentId=7]", ctx) == "{{issue:20003|comment}}"

    def test_candidates_are_a_superset(self):
        assert candidate_keys("ACME-1, {code}OPS-2{code} and utf-8 UTF-8") == {"ACME-1", "OPS-2", "UTF-8"}


class TestAttachmentsByName:
    """Data Center wiki markup shows and links attachments by file name."""

    @pytest.fixture
    def files(self, ctx: TextContext) -> TextContext:
        ctx.files = {
            "quote.png": AttachmentTarget("quote.png", ref="30500", content_type="image/png"),
            "my shot.png": AttachmentTarget("my shot.png", ref="30502", content_type="image/png"),
            "ภาพหน้าจอ.png": AttachmentTarget("ภาพหน้าจอ.png", ref="30503", content_type="image/png"),
            "paper list.txt": AttachmentTarget("paper list.txt", ref="30501", content_type="text/plain"),
            "big.zip": AttachmentTarget("big.zip", reason="not carried over, too large"),
        }
        return ctx

    def test_an_image(self, files):
        assert md("!quote.png!", files) == "![quote.png]({{attachment:30500}})"

    def test_a_thumbnail_and_a_width_are_dropped(self, files):
        assert md("A !quote.png|thumbnail! B", files) == "A ![quote.png]({{attachment:30500}}) B"
        assert md("!my shot.png|width=300,height=200!", files) == "![my shot.png]({{attachment:30502}})"

    def test_a_thai_file_name(self, files):
        # jira2markdown reads ASCII names only; this one is read too.
        assert md("ดูภาพ !ภาพหน้าจอ.png!", files) == "ดูภาพ ![ภาพหน้าจอ.png]({{attachment:30503}})"

    def test_a_file_link(self, files):
        assert md("The list: [^paper list.txt]", files) == "The list: [paper list.txt]({{attachment:30501}})"

    def test_a_file_the_bundle_does_not_hold_is_a_note(self, files):
        assert md("[^big.zip] and !gone.png!", files) == (
            "*(attachment big.zip: not carried over, too large)* and *(attachment: gone.png)*"
        )
        assert files.notes == {MEDIA_NOT_IN_BUNDLE: 2}

    def test_an_external_image_stays_a_url(self, files):
        assert md("!https://img.example.com/a.png!", files) == (
            "![https://img.example.com/a.png](https://img.example.com/a.png)"
        )
        assert not files.notes

    def test_text_between_two_exclamation_marks_is_text(self, files):
        assert md("Done !so good! really", files) == "Done !so good! really"

    def test_nothing_is_replaced_in_code(self, files):
        assert md("{code}\n!quote.png!\n{code}", files) == "```\n!quote.png!\n```"
