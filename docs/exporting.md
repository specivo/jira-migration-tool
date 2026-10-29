---
description: Export a Jira site with specivo-jira-migration - a trial run, the whole site or project by project, every option, resuming a stopped run, and how rate limits are handled.
---

# Exporting from Jira

An export reads the projects you name and writes a bundle. Start small, check the result, then run the
real export.

## A trial run

`--limit 20` exports the first 20 issues of each project (oldest key first). It is the quickest way to
check credentials, permissions and how your text and fields come across.

```bash
specivo-jira-migration --project ACME --out trial-export --limit 20 --validate
```

`--validate` checks the bundle once it is written, with the same checks Specivo makes. The tool prints the
bundle's path and a one-line summary of what it wrote, and exits 0 when all went well.

## The whole site in one bundle

Name every project you want:

```bash
specivo-jira-migration --project ACME --project OPS --project SUP \
    --out jira-export --validate
```

(`--project ACME,OPS,SUP` is the same.) The result is a **directory** holding `manifest.json`,
`lookups.json`, one `.jsonl` file for each kind of record, and `files/` with the attachments' bytes. Use
`--zip`, or an `--out` that ends in `.zip`, to write a single archive instead.

## Project by project

A large site is easier to move in parts, and the parts can be exported at different times. Run the tool
once for each project (or group of projects), with the **same site name** each time:

```bash
specivo-jira-migration --project ACME --out exports/acme --validate
specivo-jira-migration --project OPS  --out exports/ops  --validate
```

Links, parent issues and issue mentions between projects of different bundles are written into every
bundle that holds one end of them, naming the other issue by its Jira id. Specivo makes them when both
issues have been imported, whichever bundle you load first. For that to work:

- export every part with the same `--instance` (the default, the site's host name, already is the same);
- load the parts one after another into the same Specivo, with `--continue-import` from the second one on;
- give every load the same `--links-file`, or keep the bundles in one directory so the default location
  is shared. See [Validating and loading](loading.md#loading-the-parts).

`--validate` reports such cross-bundle references as `external_ref` warnings. That is expected for a part.

## Options

| Option | Meaning |
|---|---|
| `--url URL` | The Jira site (default: `$JIRA_URL`). |
| `--email`, `--api-token` | Jira Cloud credentials (default: `$JIRA_EMAIL`, `$JIRA_API_TOKEN`). |
| `--pat TOKEN` | Jira Data Center personal access token (default: `$JIRA_PAT`). |
| `--project KEY` | A project to export. Repeatable, or comma-separated (default: `$JIRA_PROJECTS`). |
| `--instance NAME` | The site's name in the bundle (default: its host name). |
| `--out PATH` | A new or empty directory, or a `.zip` path. Required. |
| `--zip` | Write a `.zip` instead of a directory. |
| `--status-category-map NAME=CATEGORY,...` | Place statuses in Specivo's `backlog` / `active` / `done` / `closed` grouping. By default Jira's status category decides: To Do becomes backlog, In Progress becomes active, Done becomes closed. |
| `--max-attachment-size SIZE` | Leave out attachments larger than this, for example `500M` or `2G`; `0` means no limit. Default `100M`. Left-out files are counted in the manifest. |
| `--no-resolve-keys` | Do not ask Jira about issue keys that are not in the bundle (in text, and in the Epic Link field). They stay plain text. |
| `--limit N` | At most N issues per project, oldest key first. For a trial run. |
| `--page-size N` | Items per page when listing (default 100). |
| `--keep-state` | Keep the state directory after a successful run. |
| `--restart` | Throw away the state of an earlier, unfinished run and start again. |
| `--validate` | Check the bundle once it is written, as Specivo will. |
| `--quiet` | Print only errors. Progress goes to stderr, the bundle's path to stdout. |
| `--version` | Print the tool's version. |

Exit status: **0** on success, **1** when the export failed or `--validate` found problems, **2** for a
usage error (including an unknown project).

### Mapping statuses

Specivo groups every status into one of four categories (`backlog`, `active`, `done`, `closed`), which
drive the boards and reports. The tool takes the category from Jira's own status category. Override single
statuses by name:

```bash
specivo-jira-migration --project ACME --out jira-export \
    --status-category-map "In Review=active,Won't Do=closed"
```

## If a run stops

Everything the tool reads from Jira is kept, as it arrives, in a **state directory** next to the output
(`jira-export.jira-state/`). If a run stops (a network failure, a rate limit it gave up on, Ctrl-C),
**run the same command again**:

- projects already listed are not listed again, and issues already read are not read again;
- watchers already listed are not asked for again;
- attachments already downloaded, and still hashing to what was recorded, are not downloaded again. A
  download in progress is written under a temporary name and discarded if the run stops;
- running again with a higher `--max-attachment-size` downloads what an earlier run left out for its size.

A state made with other options (another site, other projects, another `--limit`) is refused; pass
`--restart` to discard it. So is a state written by another version of this tool.

The bundle is written from the state alone, in a fixed order, so the same state always gives the same
bundle, byte for byte.

!!! warning "The state holds what Jira returned"
    That includes people's email addresses. The directory is created readable by you only, and removed
    once the bundle is written, unless you pass `--keep-state`. Delete it yourself when you are done with
    it.

## Rate limits

Jira Cloud limits how fast a client may call it, and answers `429 Too Many Requests` (sometimes `503`) when
the tool goes too fast, not always with a `Retry-After` header. The tool retries such a request up to six
times. It waits at least what `Retry-After` asks for (capped at ten minutes), or else backs off
exponentially with some jitter, and logs each wait. If a request still fails after that, the run stops and
can be continued as described above.

Downloads of attachments run one at a time.

## Attachments and credentials

On Jira Cloud a file is read from the attachment content endpoint, which redirects to a media host with a
short-lived token in the address. The tool follows redirects itself, one hop at a time, and sends your
credentials only to the site given in `--url` (same scheme, host and port). A hop to any other host uses a
separate client with no credentials and no cookies, and a redirect from `https` to `http` is refused. Media
tokens are never logged. On Data Center the attachment's own address is used when it is on the site.

Each file is streamed to disk and hashed as it arrives. The size limit is checked against what Jira says
and again against the bytes received.
