# Jira migration tool for Specivo

Migrate projects, issues, comments and history from Jira Cloud or Jira Data Center to
[Specivo](https://specivo.io), the open-source, self-hosted Jira alternative. The tool reads Jira
through its REST API and writes a Specivo import bundle, which Specivo validates and loads.

This tool is not affiliated with or endorsed by Atlassian. "Jira" is a trademark of Atlassian and
is used here only to say which system the tool reads.

- Bundle format 1.0. Requires Specivo 0.11.0 or later to load.
- SDK: [`specivo-migration-sdk`](https://github.com/specivo/migration-sdk) `v0.2.0`.
- Jira Cloud, and Jira Data Center (or Server) 8.14 or later. Python 3.12 or later.
- Uses only Jira's public REST API, with your own credentials.

Full documentation: <https://specivo.io/docs/jira-migration-tool/> (the Markdown sources are in [`docs/`](docs/)).

## Install

From a release (there is no PyPI package):

```bash
uv tool install git+https://github.com/specivo/jira-migration-tool@vX.Y.Z
# or: pipx install git+https://github.com/specivo/jira-migration-tool@vX.Y.Z
```

## Use

Credentials are best passed in the environment, so they stay out of the process list and your
shell history. The tool never writes them anywhere and never prints them.

**Jira Cloud**: an email address and an [API token](https://id.atlassian.com/manage-profile/security/api-tokens):

```bash
export JIRA_EMAIL=admin@example.com
export JIRA_API_TOKEN=...
specivo-jira-migration --url https://example.atlassian.net --project ACME --project OPS \
    --out jira-export --validate
```

**Jira Data Center**: a personal access token:

```bash
export JIRA_PAT=...
specivo-jira-migration --url https://jira.example.com --project SUP --out jira-export --validate
```

The tool asks Jira (`/rest/api/2/serverInfo`) which kind of site it is and refuses credentials of
the other kind. The account needs to be able to browse the projects (and "Administer Jira" on
Cloud to read where custom fields apply; see below); it never changes anything in
Jira.

This writes a **directory**: `manifest.json`, `lookups.json`, one `.jsonl` file per kind of
record and `files/`, the attachments' bytes. `--zip` (or an `--out` ending in `.zip`) writes a single archive instead. Then, on the
Specivo server:

```bash
python -m specivo.cli.import_data --source bundle --path jira-export --validate
python -m specivo.cli.import_data --source bundle --path jira-export --dry-run
python -m specivo.cli.import_data --source bundle --path jira-export
```

Start with a trial run: `--limit 20` exports the first 20 issues of each project.

### One bundle or one per project

Several projects can go into one bundle or into a bundle each; Specivo links them the same way.
Links and parents between projects of different bundles are written into each bundle that holds one
of their issues, naming the other issue by its Jira id, and Specivo makes them once both issues are
imported, whichever bundle is loaded first. For that, export every part with the same
`--instance` (the default, the site's host name, already is) and load the parts one after the
other into the same Specivo, with `--continue-import` from the second one on, and the same
`--links-file` (by default it is created next to the bundle, so keep the bundles in one directory
or name the file). What waits for a later part is kept in that file, and it is deleted once nothing
waits. `specivo-bundle validate` and `--validate` report such references as `external_ref`
warnings, which is expected for a part.

### Options

| Option | Meaning |
|---|---|
| `--url URL` | The Jira site (default: `$JIRA_URL`). |
| `--email`, `--api-token` | Jira Cloud credentials (default: `$JIRA_EMAIL`, `$JIRA_API_TOKEN`). |
| `--pat TOKEN` | Jira Data Center personal access token (default: `$JIRA_PAT`). |
| `--project KEY` | A project to export. Repeatable, or comma-separated (default: `$JIRA_PROJECTS`). |
| `--instance NAME` | The site's name in the bundle (default: its host name, e.g. `example.atlassian.net`). |
| `--out PATH` | A new or empty directory, or a `.zip` path. |
| `--zip` | Write a `.zip` instead of a directory. |
| `--status-category-map NAME=CATEGORY,...` | Place statuses in Specivo's backlog/active/done/closed grouping. By default Jira's status category decides: To Do → backlog, In Progress → active, Done → closed. |
| `--max-attachment-size SIZE` | Leave out attachments larger than this, e.g. `500M` or `2G`; `0` for no limit (default: `100M`). They are counted in the manifest. |
| `--no-resolve-keys` | Do not ask Jira about issue keys that are not in the bundle, in text and in the Epic Link field (see below). |
| `--limit N` | At most N issues per project, oldest key first, for a trial run. |
| `--page-size N` | Items per page when listing (default: 100). |
| `--keep-state` | Keep the state directory after a successful run. |
| `--restart` | Throw away the state of an earlier, unfinished run and start again. |
| `--validate` | Check the bundle once written, as Specivo will. |
| `--quiet` | Print only errors. Progress goes to stderr, the bundle's path to stdout. |

Exit status: 0 on success, 1 when the export failed or `--validate` found problems, 2 for a usage
error (including an unknown project).

### If a run stops

Everything read from Jira is kept, as it arrives, in a state directory next to the output
(`jira-export.jira-state/`). If a run stops (a network failure, a rate limit it gave up on,
Ctrl-C), **run the same command again**: projects already listed are not listed again, issues
already read are not read again, watchers already listed are not asked for again, and attachments
already downloaded (and still hashing to what was
recorded) are not downloaded again. A download in progress is written under a temporary name and
discarded if the run stops. Running again with a higher `--max-attachment-size` downloads what an
earlier run left out for its size. A state made with other options (another site, other projects,
another `--limit`) is refused; `--restart` discards it. So is a state written by another version of
this tool, whose format may differ: delete it (or pass `--restart`) and run again.

The bundle is written from the state alone, in a fixed order, so the same state always gives the
same bundle, byte for byte. The state holds what Jira returned, email addresses included: it is
created readable by you only, and removed once the bundle is written (unless `--keep-state`).

### Rate limits

Jira Cloud limits how fast a client may call it and answers `429 Too Many Requests` (sometimes
`503`) when it goes too fast, not always with a `Retry-After`. The tool retries such a request up to
six times, waiting at least what `Retry-After` asks for (capped at ten minutes), or else backing off
exponentially with jitter, and logs each wait. If it still fails, the run stops and can be
continued as above.

## What it carries

- **Projects**: key, name, description. Company-managed and team-managed projects alike.
- **Issue types** as trackers, **statuses** (with their category) and **priorities**, as the
  bundle's lookups: those the exported issues use, and statuses, types and priorities that only
  their change history names. A team-managed project's own issue types and statuses are separate
  lookups, as they are in Jira. Each tracker's default status is the one most of its issues
  started in.
- **Issues**: summary, description, type, status, priority, reporter, assignee, created, updated,
  resolution date (as `closed_at`), due date. The Jira key and the resolution name are kept in
  the issue's `extra`, since the bundle format has no place for them. Issues are
  written in key order, so Specivo numbers them in the same order (keys of deleted issues leave
  no gaps in Specivo).
- **Users** who appear in the exported issues: reporters, assignees, comment and change authors,
  and users mentioned in text. Inactive users are carried too, as locked accounts. App accounts
  (automation rules, integrations) are not people and are left out; what they wrote is attributed
  to Specivo's import account.
- **Comments** with their author and time. A comment restricted to a role or group becomes a
  private note.
- **Change history** of status, assignee, priority, summary and issue type, as journal entries with
  their author, time and old and new values (by ref, with the name Jira shows for each, which
  Specivo shows when the record did not come across), and attachments
  added and removed (the attachment's id and file name), whether or not the file is still there.
  Changes of the original estimate are `estimated_hours` changes, in hours. Changes of fix
  versions and components are changes of the target version and the category: the list before
  each change is rebuilt by walking the history back from the issue's list now, the same rule as
  below picks one value, and a change that leaves that value alone writes nothing. What Jira writes
  into the history when work is logged (time spent, remaining estimate, worklog ids) follows from
  the worklogs, which are carried, so it is left out without being counted; Specivo keeps the
  remaining estimate as it is now, without a history.
- **Estimates**, in hours rounded to hundredths: the original estimate as the issue's estimated
  hours, and the remaining estimate as its remaining estimate (the seconds are in
  `extra.jira_original_estimate_seconds` and `extra.jira_remaining_estimate_seconds` when rounding
  changed them).
- **Attachments** of each issue, with their author, time, file name and MIME type. The bytes go to
  the bundle's `files/`, each distinct content once, named by its SHA-256 (never by the name it
  has in Jira). When one issue holds several files of the same name, the later ones are numbered
  (`image (2).png`), since Specivo links a file name to the newest attachment of that name; the
  name Jira gave is kept in `extra.jira_filename`. Files over `--max-attachment-size`, files Jira
  refuses to send (403) and files it no longer has (404) are counted by reason in the manifest.

- **Issue links** as relations, each link once (Jira lists it on both of its issues): Blocks as
  `blocks`, Duplicate as `duplicates`, Cloners as `copied_to` (the issue cloned from is copied to
  its clone) and Relates as `relates`, in the direction Jira reads them. A link of any other type
  (Problem/Incident, a type of the site's own) becomes `relates`, with Jira's names for it in the
  relation's `extra`, and is counted per type. A link to an issue that is not in the bundle (in
  a project exported to another bundle, or past `--limit`) is a relation all the same, naming the
  other issue by its id, with the same type and direction: each bundle that holds one of the two
  issues carries it, under the same link id, and Specivo makes it once, when both issues are in
  (see [One bundle or one per project](#one-bundle-or-one-per-project)). A link to an issue Jira
  no longer has (deleted while the export ran) is counted and kept in the issue's
  `extra.jira_links` (the link type, its direction and the other issue's key and id).

- **Parents**: a sub-task's parent, and an epic's children (Jira Cloud gives both as `parent`, in
  company-managed and team-managed projects alike), become the child's parent issue, across
  projects too. On Data Center (and an older Cloud site) an epic's children name it in the Epic
  Link field instead, which is read the same way. A parent that is not in the bundle is written
  all the same, by its Jira id, and Specivo sets it once both issues are in, as for links. The
  Epic Link field names the epic by key only, so a key of an epic outside the bundle is looked up
  in Jira with the keys in text (not with `--no-resolve-keys`); one Jira does not find, and a
  parent Jira no longer has, are counted and kept in the issue's `extra.jira_parent` (its key, and
  its id when known). Specivo loads the projects of a bundle in the order it lists them, so the
  tool lists a project holding parents of another project's issues in the same bundle before that
  project; between bundles the order does not matter. Within a project Specivo creates parents
  before their children, so a child whose key is lower than its parent's is numbered after it.

- **Worklogs** as time entries, on their issue and its project (which gets time tracking), with
  their author and times, under one activity, "Development", since Jira's worklogs have none. The
  day the time was spent is the date of the worklog's start in its author's time zone (the day
  Jira shows them), or in the offset Jira gave when the author has none. The hours are the time
  spent rounded to hundredths of an hour, which is what Specivo keeps (the seconds are in `extra`
  when rounding changed them); a worklog of less than 18 seconds rounds to nothing and is counted.
  A comment is plain text in Specivo, so on Cloud its ADF is flattened: a line per paragraph,
  `@Display Name` for a mention, a smart link's URL; Data Center's wiki markup is kept as written.

- **Components** as the project's issue categories, with the default assignee when the component
  assigns issues to its lead (and the lead is in the bundle), and **versions** as its versions,
  with their release date and description: a released version is closed, an archived one locked
  (nothing new may target it), any other open. The start date and Jira's released and archived
  flags are kept in `extra`. Specivo has one category and one target version per issue, so an
  issue's first component is its category and, of its fix versions, the one released last (or,
  when none is released, the last listed) is its target version; issues with more than one
  component and the fix versions beyond the one carried are counted, and all of them are named in
  the issue's `extra` (`components`, `fix_versions`). Affects versions have no field in Specivo:
  they are counted and named in `extra.affects_versions`.

- **Watchers**, as Jira lists them (`/issue/{id}/watchers`, asked only for issues whose watch count
  is above zero): the reporter and assignee only when Jira lists them, though Specivo makes them
  watchers of their issues anyway. Listing them takes Jira's "View voters and watchers" permission:
  when Jira refuses (403), the issue's watch count is counted and the export carries on, and so are
  watchers it counts but does not list, and app accounts.

- **Labels** as the issue's **tags**. Specivo's tags belong to a project, as Jira's labels belong to
  the issues that use them, and are what Specivo shows and filters by, so a label is a tag of each
  project whose issues carry it (a labels-type *custom* field is a custom field instead, so its
  values stay apart). Specivo keeps tag names to 64 characters and treats names differing only in
  case as one tag; a longer label is counted and kept in `extra.labels`. Specivo keeps no history
  of tags, so label changes in the history are counted.

- **Sprints** of Jira Software, read from each project's boards (`/rest/agile/1.0/board`, and every
  scrum board's sprints) and from the issues' Sprint field: name, goal, start and end dates (the
  days Jira shows the account that exports, in its time zone), when a closed sprint was completed,
  and its state (future → planned, active → active, closed → completed). A project gets the
  sprints of the boards located in it, also those no issue is in yet, and each sprint of another
  board its issues are in: a Jira board can span projects, and a Specivo sprint holds issues of its
  own project only, so a sprint shared by projects becomes a sprint in each (counted), with the
  same name and dates. Jira lists every sprint an issue has been in; Specivo has one, so an issue
  is in its open sprint (active or future), or else in the one completed last, and the others are
  named in `extra.sprints` and counted. Changes of the Sprint field in the history are changes of
  that one sprint (by ref, with the sprint's name as text), written when the rule picks another.
  Specivo has one active sprint per project and imports a second one (parallel sprints) as
  planned. Without Jira Software, or when the account may not read a project's boards (counted),
  the sprints are the ones the issues name.

- **Custom fields** the exported issues use, with their values and history:

  | Jira | Specivo custom field |
  |---|---|
  | Text field, read-only text, group and project pickers | text (`string`); groups by name, projects by key |
  | Paragraph (text area) | long text (`text`), as Markdown: ADF on Cloud, wiki markup on Data Center |
  | Number, story points | `float` |
  | Date picker, date time picker | `date`, `datetime` |
  | Select list, radio buttons | `list`, its options listed |
  | Multi-select, checkboxes | `list`, several values |
  | Select list (cascading) | `list`, a value read `Parent / Child` |
  | User picker (single and multiple) | `user`, by the user's ref |
  | Version picker (single and multiple) | `version`, by the version's ref |
  | URL | `link` |
  | Labels | `string`, several values |

  A value is stored as it is, without links: a mention in a paragraph reads `@Name`, an issue key
  stays text. A value naming an app account, or a version that is not the project's, is left out
  and counted. Specivo shows a field by its Jira name, in any script (Thai included), on the issue
  page, in history, filters, the REST API and MCP, and a user or version value by the person's or
  version's name to a reader who may see them. Its key, where values are stored, is the name in
  snake case, or the field's id for a name with nothing ASCII can spell; two fields whose names
  give one key both add their number. On Cloud the field's contexts say where it applies: a global context makes it a
  field of every project, otherwise of the projects and issue types its contexts name, together
  with the projects whose issues hold a value. Reading contexts takes "Administer Jira"; when Jira
  refuses (counted), and on Data Center, a field applies to the projects whose issues use it. A
  team-managed project's own field applies to that project. The options of an option field are
  its contexts' options, disabled ones included, and the values the issues and history hold.
  Changes in the history are custom field changes: a user or version field's by ref, with the
  name Jira shows as text, a detail per value added or removed for a field of several values; any
  other field's as the text Jira shows. Fields Jira keeps for its own screens or that the bundle
  carries otherwise are left out and their values counted: Rank, Sprint and Epic Link (the
  issue's sprint and parent), epic name, colour and status, Team, Development, Parent Link and
  computed fields, and fields of an app's own type.

Comments, worklogs and change history are read with their own paging: the lists Jira embeds in an
issue are truncated.

### Attachments and credentials

On Cloud a file is read from `/rest/api/3/attachment/content/{id}`, which redirects to Atlassian's
media host with a short-lived token in the URL. The tool follows redirects itself, one hop at a
time, and sends credentials only to the site given in `--url` (same scheme, host and port): a hop
to any other host goes through a separate HTTP client that has no credentials and no cookies, and a
redirect from `https` to `http` is refused. On Data Center the attachment's own `content` URL is
used when it is on the site, and `/rest/api/2/attachment/content/{id}` otherwise. Files are
streamed to disk and hashed as they arrive; the size limit is checked against what Jira says and
again against the bytes. Downloads run one at a time.

### Identifiers

Each record's `source_ref` is Jira's own id, which never changes: the issue **id** (`10042`), not
its key, because a key changes when an issue moves or its project is renamed; the project id; the
Cloud `accountId` or the Data Center user key; `comment:<id>` and `history:<id>` for journal
entries. Export the same site again later with the same `--instance` (the default is stable), load
the new bundle with `--continue-import`, and Specivo adds only what is new.

### Text

On **Jira Cloud**, issues and comments are read through REST v3, where descriptions and comments
are Atlassian Document Format (ADF), the editor's own JSON. The tool converts ADF to Markdown
itself, node by node, so nothing passes through wiki markup on the way (Cloud's REST v2 does that
conversion and loses smart links, panels, tasks, merged table cells and more). The Markdown is
written for what Specivo renders:

| In Jira | In Specivo |
|---|---|
| Headings, bold, italic, code, links, quotes, rules | the same |
| Strikethrough, underline, sub- and superscript | `<del>`, `<u>`, `<sub>`, `<sup>` |
| Text and background colour | dropped, the text kept |
| Bullet, numbered and task lists, nested in any mix | Markdown lists; tasks as `- [ ]` and `- [x]` |
| Decisions | `- **Decision:** …` |
| Panels (info, note, warning, …) and expands | a block quote under a bold label (the type, or the expand's title) |
| Code blocks | fenced code with its language (an indented block inside a list or quote) |
| Status lozenges, dates | the status as `code`, the date as `2026-11-19` |
| Emoji | the emoji character |
| Tables | Markdown tables; a merged cell is written unmerged, its text in the first cell; lists and code in a cell are folded into one line; a table inside a cell is written after its table |
| Smart links (inline, block, embed) | a reference to the issue when they point to one on the site, otherwise a link |
| Images shown in the text (`mediaSingle`) | `![name](attachment)`, the issue's attachment; the width is dropped |
| Files shown in the text (`mediaGroup`, `mediaInline`, a non-image in `mediaSingle`) | `[name](attachment)`, a link to it |
| An image or file whose attachment is not in the bundle | a short note naming it, and why when known |

An image in the text names its file by a media id, not by the attachment's id. Jira gives the
media id of each attachment in the redirect of its download (`/file/<media id>/binary`), which is
how the tool ties the two together, for descriptions and comments alike; the id is read once per
attachment and kept in the state. (The attachment's metadata does not hold the media id, and the
`renderedFields` HTML names it for some images only.) The width Jira keeps for an image is dropped:
Specivo points only Markdown images at the attachment, not an HTML `<img>`, so an image with a
width would show no picture. A file of another issue, or one deleted or left out, is a note.

Element kinds the tool does not know keep their text and are counted, per kind, in the manifest's
`not_carried_over`, like merged cells, tables in cells, and images and files whose attachment is
not in the bundle.

On **Jira Data Center**, text is read through REST v2 as Jira wiki markup and converted to Markdown
with [jira2markdown](https://github.com/catcombo/jira2markdown): headings, lists, code, tables,
links, text effects. Images and file links by name (`!screenshot.png!`,
`!screenshot.png|thumbnail!`, `[^log.txt]`, any script in the name) become the issue's attachment
of that name (the newest, when several share it); size options are dropped, and a name the issue
has no attachment for is a note.

On both, on top of that:

- a **mention** of a carried user becomes a placeholder Specivo turns into `@login`; a mention of
  an app account reads `@Display Name`;
- an **issue key** (`ACME-12`), or a link to the site's `/browse/ACME-12` page, becomes a reference
  to the issue, which Specivo writes with the issue's new key. A key of an issue outside the
  bundle (another project, or past `--limit`), including one only linked to by its `/browse/` page,
  is looked up in Jira: if it exists, Specivo links it
  when that issue is imported, by this bundle's site, now or in a later bundle, and otherwise
  writes the key as it was. A key Jira does not know (`UTF-8`) stays text. Nothing inside code, a
  link or a URL is changed. `--no-resolve-keys` skips the look-ups, so only keys of issues in the
  bundle become references.

### Users without a visible email address

Jira Cloud shows an account's email address only if its owner allows it (or to an organisation
admin with the right access). Such a user is written without one; Specivo then gives the account a
placeholder address (`login@invalid`) that never receives mail, and lists it in the import report,
so an administrator can fill in the real one. The tool says how many there are. A login is taken
from the email address, or else from the display name.

## What it does not carry

Everything else Jira returns for an issue is counted in the manifest's `not_carried_over`, so
nothing disappears silently: links to issues Jira no longer has, parents Jira no longer has or
that an Epic Link names by a key Jira did not find, remote links (web
and Confluence links, counted from the change history; they are not read), components and fix
versions beyond the one an issue can have, affects versions, the sprints an issue was in before
its current one, custom fields of Jira's own or an app's type and values naming app accounts,
labels too long for a tag, watchers Jira would not list, votes, the change history of other fields, app accounts, change
history Jira did not return, and attachments that were not downloaded (by reason). Confluence
pages, boards, filters, dashboards, workflows, schemes and add-on data are not read at all.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
```

The SDK is a path dependency on `../migration-sdk` while the two are developed side by side; a
release pins it to an SDK tag.

### Fixtures

Unit tests never touch the network. `tests/fixtures/cloud.json` and `tests/fixtures/datacenter.json`
are **hand-written** cassettes, shaped after Atlassian's public REST API reference, that a fake site
(`tests/fake_jira.py`, built on [respx](https://lundberg.github.io/respx/)) plays back. They cover
both project styles, a search whose `isLast` never turns true, a 429 with and without
`Retry-After` and a 503, users with hidden email addresses, an inactive user, an app account,
truncated embedded comments, worklogs and change history, an issue deleted between search and read,
issue links of every built-in type and an unknown one, inside and across projects (a third
project, OPS, holds the far end of a link and a parent, so the projects can be exported one per
bundle), sub-tasks, an epic child in another project and Data Center Epic Links, one of an epic
of another project found by its key,
components and versions read page by page (released, archived, both and neither), issues with
several of each, watchers (an issue without any, a 403, fewer listed than counted, an app account),
attachments (inline images, a file shown in a comment, content shared by two attachments, two files
of one name on one issue, a Thai file name, a file over the size limit, a 403 and a 404, the media
host behind Cloud's redirect, attachments added and removed in the change history), boards and
sprints (closed, active and future, a kanban board, a sprint of one project's board holding another
project's issue, a sprint of a board deleted since, boards Jira refuses to list), a custom field of
every type the tool carries and of some it leaves out, with values and history, contexts for one
project and issue type, a field whose contexts Jira refuses, team-managed fields, two fields of
one name and a field with a Thai name, labels (one too long for a tag), and a Data Center site
with user keys and user names, attachments shown by name, and sprints, custom fields and their
history in Data Center's shapes. All names, emails, account
ids and file contents in them are invented.

`python -m tests.write_fixture_bundle OUT [KEYS]` writes a bundle from the Cloud cassette (projects
`ACME,SCRUM` unless *KEYS* names others); Specivo's own test suite uses it to load the bundle into a
database end to end, as one bundle and one per project.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
