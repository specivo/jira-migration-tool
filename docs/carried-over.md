---
description: What specivo-jira-migration carries from Jira into Specivo, area by area - issues, text, attachments, comments, history, links, parents, worklogs, components, versions, sprints, custom fields, labels, watchers and estimates.
---

# What is carried over

This page lists what the tool reads from Jira through its REST API and how each part becomes a Specivo
record. Anything not listed here, or only partly carried, is described in
[What is not carried over](not-carried-over.md) and counted in the bundle's manifest.

## Projects, issue types, statuses and priorities

| Jira | Specivo |
|---|---|
| Project key, name, description | Project with the same key, name and description. Company-managed and team-managed projects alike. |
| Issue types | Trackers. Each tracker's default status is the one most of its issues started in. |
| Statuses | Statuses, with a category: To Do becomes backlog, In Progress becomes active, Done becomes closed (change it with `--status-category-map`). |
| Priorities | Priorities. |

The tool carries the types, statuses and priorities that the exported issues use, and those that only
their change history names. A team-managed project has its own issue types and statuses, so they are
separate records, as they are in Jira. Load with `--merge-duplicate-statuses` to merge ones that share a
name with an existing record.

## Issues

| Jira | Specivo |
|---|---|
| Summary | Subject |
| Description | Description, converted to Markdown (see [How text is converted](text-conversion.md)) |
| Issue type, status, priority | Tracker, status, priority |
| Reporter | Author |
| Assignee | Assignee |
| Created, updated | Created and updated dates, kept |
| Resolution date | Closed date |
| Due date | Due date |
| Security level or issue restriction | Private issue. The details are kept in the issue's `extra` data. |
| Issue key, resolution name | Kept in the issue's `extra` data. The bundle has no property for them. |

Issues are written in key order, so Specivo numbers them in the same order. Keys of deleted or moved issues
leave no gaps in Specivo, so numbers can differ after a gap.

## People

Users who appear in the exported issues are carried: reporters, assignees, comment and change authors,
watchers, and people mentioned in text. Inactive users are carried too, as locked accounts. **App
accounts** (automation rules, integrations) are not people and are left out; what they wrote is attributed
to Specivo's import account.

A Jira Cloud account whose owner hides their email address from the exporting account is written without
one. Specivo gives it a placeholder address that never receives mail and lists it in the import report, so
an administrator can fill in the real one. The login is taken from the email address, or else from the
display name.

## Comments

Comments are carried with their author and time. A comment restricted to a role or group becomes a
**private note**. The full list is read through its own paging endpoint, since the list Jira embeds in an
issue is truncated. Edited and deleted comments are handled.

## Change history

History becomes journal entries with the original author and time.

| Change | How it is carried |
|---|---|
| Status, assignee, priority, summary, issue type | A change with old and new values. A value that names a record (a status, an assignee) is linked to the record Specivo created, and shows by name. If the record did not come across, Specivo shows the name Jira showed. |
| Attachments added and removed | The attachment's id and file name, whether or not the file still exists. |
| Original estimate | A change of the estimate, in hours. |
| Fix versions, components | A change of the target version and the category, using the one-value rules described below. The list before each change is rebuilt by walking the history back from the issue's list now. A change that leaves the chosen value alone writes nothing. |
| Parent, epic link | A change of parent. |
| Sprint | A change of the issue's sprint, written when the one-sprint rule below picks another sprint. |
| Custom fields | A change of the field, under the field's name. |
| Time spent, remaining estimate, worklog ids | Not written: they follow from the worklogs, which are carried. They are skipped without being counted. |
| Labels, and other fields | Counted in the manifest. |

The full change history of an issue is read through the issue's own paging endpoint.

## Attachments and inline images

Attachments of each issue are carried with their author, time, file name and MIME type. The bytes go to the
bundle's `files/` folder, each distinct content once, named by its SHA-256 (never by the name it has in
Jira).

- When one issue holds several files of the same name, the later ones are numbered (`image (2).png`),
  because Specivo links a file name to the newest attachment of that name. The name Jira gave is kept in
  the attachment's `extra` data.
- Files over `--max-attachment-size`, files Jira refuses to send (403) and files it no longer has (404) are
  counted, by reason, in the manifest.
- Images and files shown inside descriptions and comments point at the issue's attachment. See
  [How text is converted](text-conversion.md).

## Issue links

Each Jira link becomes one relation (Jira lists a link on both of its issues; the tool writes it once).

| Jira link type | Specivo relation |
|---|---|
| Blocks | `blocks` |
| Duplicate | `duplicates` |
| Cloners | `copied_to` (the issue cloned from is copied to its clone) |
| Relates | `relates` |
| Any other type | `relates`, with Jira's names for the type kept in the relation's `extra` data, and counted per type |

The direction is the one Jira reads. A link to an issue in a project exported to another bundle (or past
`--limit`) is still a relation, naming the other issue by its id; each bundle that holds one of the two
issues carries it under the same link id, and Specivo makes it once, when both issues are imported. A link
to an issue Jira no longer has is counted and kept in the issue's `extra` data.

## Parent issues

A sub-task's parent, and an epic's children, become the child's **parent issue**, also across projects. On
Jira Cloud both are the `parent` field, in company-managed and team-managed projects. On Data Center and
older Cloud sites the children of an epic name it in the **Epic Link** field, which is read the same way.

A parent that is not in the bundle is written by its Jira id, and Specivo sets it when both issues are
imported. An Epic Link names the epic by key only, so the key of an epic outside the bundle is looked up in
Jira (not with `--no-resolve-keys`). A parent that Jira does not find is counted and kept in the issue's
`extra` data.

## Worklogs

Worklogs become **time entries** on their issue and project (which gets time tracking), with their author
and times, under one activity, **Development**, since Jira's worklogs have none.

- The day the time was spent is the date of the worklog's start in its author's time zone (the day Jira
  shows), or in the offset Jira gave when the author has none.
- The hours are the time spent rounded to hundredths of an hour, which is what Specivo keeps. The seconds
  are kept in `extra` when rounding changed them. A worklog of less than 18 seconds rounds to nothing and
  is counted.
- A worklog's comment is plain text.
- A worklog restricted to a group or role keeps its time, but not its comment: Specivo shows a time
  entry to everyone who may see the project's time. The restriction is kept in `extra`, and the comments
  left out are counted in the report.
- A worklog on a restricted issue keeps its time but not its comment either: Specivo imports the issue as
  private, but shows its time entries to everyone who may see the project's time.

## Components and versions

| Jira | Specivo |
|---|---|
| Component | The project's **category**, with the default assignee when the component assigns issues to its lead and the lead is in the bundle |
| Version | A **version** with its release date and description. A released version becomes closed, an archived one locked (nothing new may target it), any other open. The start date and the released and archived flags are kept in `extra`. |

Specivo has **one category and one target version per issue**. An issue's first component becomes its
category. Of its fix versions, the one released last becomes its target version (or, when none is released,
the last listed). Issues with more than one component, and fix versions beyond the one carried, are counted
and all of them are named in the issue's `extra` data. **Affects versions** have no field in Specivo; they
are counted and named in `extra` too.

## Sprints

Sprints of Jira Software are read from each project's boards and from the issues' Sprint field.

| Jira | Specivo |
|---|---|
| Sprint name, goal | Name, goal |
| Start and end dates | Start and end dates (the days Jira shows the exporting account, in its time zone) |
| Completion time of a closed sprint | Kept with the sprint's velocity |
| State: future, active, closed | Planned, active, completed |

- A project gets the sprints of the boards located in it, also those no issue is in yet, and each sprint of
  another board that its issues are in.
- A Jira board can span projects, but a Specivo sprint holds issues of its own project only. A sprint shared
  by projects becomes a sprint in each, with the same name and dates. This is counted.
- **One sprint per issue.** Jira lists every sprint an issue has been in; Specivo has one. An issue goes
  into its open sprint (active or future), or else the one completed last. The others are named in the
  issue's `extra` data and counted.
- **One active sprint per project.** A second active sprint (parallel sprints) is imported as planned.
  Specivo says so in the import report.
- A completed sprint's velocity is recorded from its issues when Specivo loads the bundle.
- Without Jira Software, or when the account may not read a project's boards, the sprints are the ones the
  issues name. Boards that could not be read are counted.

## Custom fields

Custom fields that the exported issues use are carried with their values and history. Specivo stores them
as issue metadata.

| Jira field type | Specivo custom field |
|---|---|
| Text field, read-only text, group and project pickers | Text. Groups by name, projects by key. |
| Paragraph (text area) | Long text, as Markdown |
| Number, story points | Number (`float`) |
| Date picker, date time picker | `date`, `datetime` |
| Select list, radio buttons | `list`, with its options listed |
| Multi-select, checkboxes | `list`, several values |
| Select list (cascading) | `list`, a value read `Parent / Child` |
| User picker (single and multiple) | `user`, by the user's ref |
| Version picker (single and multiple) | `version`, by the version's ref |
| URL | `link` |
| Labels field | Text, several values |

Things to know:

- Specivo shows a field by its **Jira name**, in any script, on the issue page, in history, in filters, in
  the REST API and over MCP. A user or version value shows by the person's or version's name to a reader
  who may see them.
- The key under which values are stored is the name in snake case, or the field's id for a name that
  ASCII cannot spell. When two fields' names give one key, both get their number added.
- A value is stored as it is, without links: a mention in a paragraph reads `@Name`, an issue key stays
  text. A value that names an app account, or a version that is not the project's, is left out and counted.
- **Where a field applies.** On Jira Cloud, the field's contexts say so: a global context makes it a field
  of every project, otherwise of the projects and issue types its contexts name, together with the
  projects whose issues hold a value. Reading contexts takes **Administer Jira**. When Jira refuses
  (counted), and on Data Center, a field applies to the projects whose issues use it. A team-managed
  project's own field applies to that project.
- The options of an option field are its contexts' options, disabled ones included, plus the values the
  issues and history hold.
- Fields Jira keeps for its own screens, or that the bundle carries another way, are left out and their
  values counted: Rank, Sprint and Epic Link (carried as the issue's sprint and parent), epic name, colour
  and status, Team, Development, Parent Link, computed fields, and fields of an app's own type.

## Labels

Jira labels become **tags** of the issue. Specivo's tags belong to a project, as Jira's labels belong to the
issues that use them, so a label becomes a tag of each project whose issues carry it. Names that differ
only in case are one tag, spelled as first named. A label longer than 64 characters (Specivo's limit) is
counted and kept in the issue's `extra` data. A field of the labels type is a custom field instead, so its
values stay apart from tags.

Specivo keeps no history of tags, so label changes in the history are counted, not carried.

## Watchers

Watchers are listed as Jira lists them (only for issues whose watch count is above zero). Listing them
takes Jira's **View voters and watchers** permission. When Jira refuses, the issue's watch count is counted
and the export carries on; so are watchers that Jira counts but does not list, and app accounts. Specivo
makes an issue's reporter and assignee watchers anyway.

## Estimates

| Jira | Specivo |
|---|---|
| Original estimate | The issue's **estimated hours**, in hours rounded to hundredths. A zero estimate stays zero. |
| Remaining estimate | The issue's **remaining estimate**, with the same rounding. An issue can have one without an original estimate. |

When rounding changed a value, the seconds are in the issue's `extra` data
(`jira_original_estimate_seconds`, `jira_remaining_estimate_seconds`). Specivo stores the remaining
estimate as given. It does not derive it from time entries, and it keeps no history for it, since Jira
rewrites it on every worklog.

## Identifiers

Each record's source reference is Jira's own id, which never changes: the issue **id** (`10042`) and not
its key, because a key changes when an issue moves or its project is renamed; the project id; the Cloud
account id or the Data Center user key; and `comment:<id>` and `history:<id>` for journal entries. This
is what lets a later export of the same site add only what is new.
