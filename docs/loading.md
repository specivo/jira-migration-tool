---
description: Check, rehearse and load a bundle written by specivo-jira-migration into Specivo with the core import command, including --continue-import, --links-file and --merge-duplicate-statuses.
---

# Validating and loading the bundle

Loading is done by Specivo, not by this tool. It needs **Specivo 0.11.0 or later**. The import command is
the same for every migration tool; this page shows what matters for a Jira bundle. The full guide is
[Importing from another tracker](https://specivo.io/docs/specivo/install/importing/) in the Specivo user
guide.

!!! warning "Back up first"
    Back up your Specivo database and data directory before a real import.

Copy the bundle to the Specivo server, or mount it into the `api` container, in a folder that is writable
by the app (an import keeps a small links file next to the bundle). Then run, in this order:

```bash
# 1. Check the bundle. Reads only the bundle: no database is touched.
python -m specivo.cli.import_data --source bundle --path jira-export --validate

# 2. Rehearse. The whole import runs in a transaction that is always rolled back.
python -m specivo.cli.import_data --source bundle --path jira-export --dry-run

# 3. Import.
python -m specivo.cli.import_data --source bundle --path jira-export
```

With Docker, prefix each command with `docker compose exec api` and use the path inside the container.

The dry run is the one to read carefully. Its report shows the project keys Specivo chose, statuses and
trackers that already exist, accounts that need an email address, and everything that could not be
carried across.

## Options you will use

| Option | Use it to |
|---|---|
| `--merge-duplicate-statuses` | Reuse an existing status, tracker or priority of the same name instead of creating a second one. A project that Jira manages itself (a team-managed project) has its own issue types and statuses, so names like "Task" or "Done" can appear twice. This merges them. |
| `--project-key-map IDENTIFIER=KEY,...` | Choose the Specivo project key for a project. The identifier the tool writes is the Jira project key in lower case, so `ops=OPS2` gives Jira's `OPS` the Specivo key `OPS2`. The dry run report shows the key chosen for each project. |
| `--project IDS` | Load only some projects of the bundle, by **Jira project id** (the numeric id, comma-separated), not by key. |
| `--continue-import` | Add to an earlier import of the same Jira site. Required from the second load on. |
| `--links-file PATH` | Keep the links file at this path (see below). |
| `--files-path DIR` | Read the attachment files from this folder instead of the bundle's own `files/`. |
| `--resume RUN_ID` | Continue a load that stopped. The run id is printed at the end of a run. |
| `--import-admins` | Make the Jira administrators administrators in Specivo too. By default they arrive as ordinary accounts and are listed in the report. |
| `--report-json PATH` | Also write the report to a file. |

Jira accounts arrive **without passwords**; everyone sets a new one (or signs in with
[single sign-on](https://specivo.io/docs/specivo/install/single-sign-on/)).

## Loading the parts

When you export project by project, load the parts one after another:

```bash
python -m specivo.cli.import_data --source bundle --path exports/acme \
    --links-file /import/jira-links.sqlite
python -m specivo.cli.import_data --source bundle --path exports/ops \
    --links-file /import/jira-links.sqlite --continue-import
```

The links file is a small SQLite file. It remembers the parts of a bundle that point at something not
imported yet: an issue mention in a text, a relation, a parent. Each later load completes what it can, in
any order, and the file is deleted once nothing waits. Give every load of the same site the same file.
By default it is created next to the bundle as `specivo-links.jira.<site name>.sqlite`.

Texts that someone edited in Specivo after the import are left as they are.

Check what is still waiting with:

```bash
python -m specivo.cli.admin import-map pending --links-file /import/jira-links.sqlite
```

## Catching up before the switch

Importing early and catching up later is the safest way to move a busy team.

1. Export and load everything a week or two ahead. People can look around in Specivo.
2. Just before the switch, export the **same site again** with the same `--instance`.
3. Load the new bundle with `--continue-import`. Specivo adds only the records created since.

Records already imported are recognised and skipped, not updated. A change made to an issue in Jira after
it was imported is therefore not copied; plan the final export for when work in Jira has stopped.

When you have switched over and will not import from that site again, an administrator can free the
bookkeeping with `python -m specivo.cli.admin import-map purge --system jira --instance SITE --yes`
(see the Specivo guide for what that means), and delete the links file.

## Issue numbers

Specivo numbers a project's issues in the order they load (`ACME-1`, `ACME-2`, ...). The tool writes issues
in Jira key order, but keys of deleted or moved issues leave no gaps, so Specivo's numbers can differ from
Jira's. References between issues use Jira's ids, so links stay correct. The Jira key is kept with the
issue in the bundle's `extra` data.

## After the load

- Open a few projects and compare issue counts with Jira.
- Fill in the real email address for accounts Specivo gave a placeholder address. This happens when a Jira
  Cloud account hides its email address from the exporting account. The tool tells you how many there are.
- Check each project's members and roles.
