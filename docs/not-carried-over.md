---
description: What specivo-jira-migration does not carry from Jira to Specivo, and how it reports each gap through the not_carried_over list in the bundle manifest and the extra data on records.
---

# What is not carried over

Nothing Jira returns for an issue should disappear silently. The tool records what it left out in two
places:

- **`not_carried_over`** in the bundle's `manifest.json`: one entry for each kind of thing, with a
  description and a count. Specivo prints these entries in its import report.
- **`extra`** on a record: source data that has no place in Specivo, kept with the record it belongs to,
  for example the Jira key of an issue, or the components an issue had beyond the one Specivo can hold.
  Specivo does not import `extra`, but the report counts the records that have some.

When the tool finishes, it prints how many kinds of data are listed in the manifest. To read them yourself,
open `manifest.json` and look at `not_carried_over`; each entry has a `what` (a sentence saying what was
left out and, often, where it is kept) and a `count`.

## What is counted

| Left out | Why, and where to find it |
|---|---|
| Links to issues Jira no longer has | Counted. Kept in the issue's `extra` (link type, direction, the other issue's key and id). |
| Parents Jira no longer has, or an Epic Link naming a key Jira did not find | Counted. Kept in the issue's `extra`. |
| Remote links (web and Confluence links) | Counted from the change history. They are not read. |
| Components and fix versions beyond the one an issue can have | Specivo has one category and one target version per issue. Counted, and all are named in `extra`. |
| Affects versions, and their history | No field in Specivo. Counted and named in `extra`. |
| Sprints an issue was in before its current one | Specivo has one sprint per issue. Counted and named in `extra`. |
| Sprints shared by several projects | Become one sprint per project. Counted. |
| Boards the account may not read | Counted. The sprints are then the ones the issues name. |
| Custom fields of Jira's own or of an app's type | Counted with their values. See [Custom fields](carried-over.md#custom-fields). |
| Custom field values naming app accounts | Counted. |
| Labels too long for a tag | Counted. Kept in `extra`. |
| Label changes in the history | Specivo keeps no tag history. Counted. |
| Watchers Jira would not list | Counted. Needs the **View voters and watchers** permission. |
| Votes | Not carried. |
| History of fields other than those listed in [Change history](carried-over.md#change-history) | Counted. |
| History Jira did not return | Counted. |
| Edits of an issue's description | Not carried. |
| App accounts | Their comments and changes are attributed to Specivo's import account. |
| Attachments that were not downloaded | Counted, by reason: over `--max-attachment-size`, refused by Jira (403), or no longer there (404). |
| Merged table cells, tables in cells, and images or files whose attachment is not in the bundle | Counted per kind. The text is kept. |
| Unknown rich-text elements | Counted per kind. The text is kept. |

## What is not read at all

The tool reads projects and the issues in them. It does not read:

- Confluence pages and spaces;
- boards, filters and dashboards (boards are read only to find sprints);
- workflows, schemes, screens and permissions;
- automation rules;
- data stored by add-ons and apps.

Roles and project membership are not copied: add the people you want to each Specivo project after the
import, or map them with single sign-on groups.

## Differences you will notice

- **Issue numbers.** Specivo numbers issues in the order they load, so keys can differ from Jira's after
  deleted or moved issues. See [Issue numbers](loading.md#issue-numbers).
- **One value where Jira has several.** One category, one target version and one sprint per issue (see
  above). The rest are in `extra`.
- **Estimates in hours.** Jira keeps seconds; Specivo keeps hours to hundredths.
