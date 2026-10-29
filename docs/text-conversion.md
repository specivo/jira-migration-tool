---
description: How specivo-jira-migration turns Jira descriptions and comments into Markdown for Specivo - Atlassian Document Format on Jira Cloud, wiki markup on Data Center, plus mentions, issue keys, images and files.
---

# How text is converted

Specivo stores issue descriptions, comments and custom field text as **Markdown**. Jira stores them in
its own formats, so the tool converts every text on the way. The conversion is different on Jira Cloud and
on Jira Data Center.

## Jira Cloud: Atlassian Document Format

On Jira Cloud the tool reads issues and comments through REST API v3, where text is **Atlassian Document
Format** (ADF), the editor's own JSON. The tool converts ADF to Markdown itself, node by node, so nothing
passes through wiki markup on the way. (Cloud's older v2 API does that conversion on the server and
loses smart links, panels, tasks, merged table cells and more.)

The Markdown is written for what Specivo renders:

| In Jira | In Specivo |
|---|---|
| Headings, bold, italic, code, links, quotes, rules | The same |
| Strikethrough, underline, subscript, superscript | `<del>`, `<u>`, `<sub>`, `<sup>` |
| Text and background colour | Dropped, the text kept |
| Bullet, numbered and task lists, nested in any mix | Markdown lists; tasks as `- [ ]` and `- [x]` |
| Decisions | `- **Decision:** ...` |
| Panels (info, note, warning, ...) and expands | A block quote under a bold label (the panel type, or the expand's title) |
| Code blocks | Fenced code with its language (an indented block inside a list or quote) |
| Status lozenges, dates | The status as `code`, the date as `2026-11-19` |
| Emoji | The emoji character |
| Tables | Markdown tables. A merged cell is written unmerged, with its text in the first cell. Lists and code in a cell are folded into one line. A table inside a cell is written after its table. |
| Smart links (inline, block, embed) | A reference to the issue when they point to one on the site, otherwise a link |
| Images shown in the text | `![name](attachment)`, pointing at the issue's attachment. The width is dropped. |
| Files shown in the text, and non-image files | `[name](attachment)`, a link to it |
| An image or file whose attachment is not in the bundle | A short note naming it, and why when known |

Element kinds the tool does not know keep their text and are counted, per kind, in the manifest's
`not_carried_over`, like merged cells, tables inside cells, and images and files whose attachment is not in
the bundle. Raw JSON is never written into a text.

### How images are tied to attachments

An image in Jira text names its file by a *media id*, not by the attachment's id. Jira gives the media id of
each attachment in the redirect of its download. The tool reads it once per attachment and keeps it in its
working state, which is how images in descriptions and comments find their attachment.

The width Jira keeps for an image is dropped: Specivo points only Markdown images at an attachment, so an
image written with a width would show no picture.

## Jira Data Center: wiki markup

On Data Center the tool reads text through REST API v2 as Jira wiki markup and converts it to Markdown with
the open-source [jira2markdown](https://github.com/catcombo/jira2markdown) library: headings, lists, code,
tables, links and text effects.

Images and file links written by name (`!screenshot.png!`, `!screenshot.png|thumbnail!`, `[^log.txt]`, in
any script) become the issue's attachment of that name (the newest, when several share it). Size options
are dropped, and a name the issue has no attachment for becomes a note.

## On both

- **Mentions.** A mention of a carried user becomes a placeholder that Specivo turns into `@login`. A
  mention of an app account reads `@Display Name`.
- **Issue keys.** An issue key (`ACME-12`), or a link to the site's `/browse/ACME-12` page, becomes a
  reference to the issue, which Specivo writes with the issue's **new** key.
  - The key of an issue outside the bundle (another project, or past `--limit`) is looked up in Jira. If it
    exists, Specivo links it when that issue is imported, now or in a later bundle from the same site.
    Otherwise it is written as it was.
  - A word that only looks like a key (`UTF-8`) and that Jira does not know stays text.
  - Nothing inside code, a link or a URL is changed.
  - `--no-resolve-keys` skips the look-ups, so only keys of issues in the bundle become references.
- **Markup characters** in plain text are escaped, so text can never turn into a link by accident.
- **Conversion failures.** Text that cannot be converted is kept as written, and the tool prints how many
  texts that was.

Comments are plain text in Specivo's time entries, so a worklog comment on Cloud is flattened: one line per
paragraph, `@Display Name` for a mention, and a smart link's address.
