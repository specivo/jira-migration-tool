---
description: Fix common problems with specivo-jira-migration and the Specivo import - 401 and 403 responses, missing projects, size limits, rate limits, stopped runs, warnings, and re-running safely.
---

# Troubleshooting

## The tool will not start the export

| Message | What it means |
|---|---|
| `give --url, or set JIRA_URL` | No site address. |
| `give --email and --api-token ... or --pat` | No credentials, or credentials of both kinds. Give one kind. |
| `... are all set: unset the ones for the other kind of site` | The Cloud variables and `JIRA_PAT` are both set. Unset one set. |
| `name at least one project key` | Give `--project KEY` or set `JIRA_PROJECTS`. |
| `... already holds a bundle` / `is not empty` | `--out` points at an existing bundle or a folder with files in it. Use a new path. |
| `put credentials in --email/--api-token or --pat, never in the URL` | The `--url` has a user name or password in it. Remove it. |
| An unknown project (exit status 2) | The key does not exist, or the account cannot browse it. Check the key and the account's access. |
| A state of another export | A previous unfinished run used other options. Pass `--restart`, or use the old options. |

The tool also refuses credentials of the wrong kind for the site (for example a personal access token for
a Cloud site). Use the credentials described in [Authentication](authentication.md).

## 401 and 403 responses

- **401** means the credentials were not accepted. Check the email address and token for Cloud, or the
  personal access token for Data Center. Tokens can expire or be revoked.
- **403 on a whole project** means the account cannot browse it. Give the account access in Jira.
- **403 on one kind of data** does not stop the export. The tool counts it and carries on:
  - custom field contexts (**Administer Jira** on Cloud): the field then applies to the projects whose
    issues use it;
  - watchers (**View voters and watchers**): watchers are counted, not carried;
  - a project's boards: the sprints are the ones the issues name;
  - an attachment: counted under "refused by Jira".

Read the manifest's `not_carried_over` to see which of these happened and how many times.

## Attachments

- **Attachments are missing.** Files larger than `--max-attachment-size` (default 100 MB) are left out and
  counted. Run again with a higher limit (for example `--max-attachment-size 2G`, or `0` for no limit):
  the tool downloads only what an earlier run left out.
- **Not found (404).** Jira lists the attachment but no longer has the file. It is counted.
- **Disk space.** A large site needs room for its attachments in the bundle and for the working state.

## Rate limits and slow exports

Jira Cloud answers `429` or `503` when the tool calls it too fast. The tool waits and retries up to six
times for each request (see [Rate limits](exporting.md#rate-limits)). If it still fails, it stops and says
so. Wait a while, then **run the same command again**; nothing already read is read twice.

To go gentler, lower `--page-size`. Exporting one project at a time spreads the load.

## A run stopped half way

Run the same command again. See [If a run stops](exporting.md#if-a-run-stops). If it refuses with a message
about the state, either the options changed (pass `--restart` to start over) or the state was written by
another version of the tool (delete it, or pass `--restart`).

## Problems when loading into Specivo

| Symptom | What to do |
|---|---|
| `--validate` reports `unresolved_ref` or `count_mismatch` | The bundle is damaged or incomplete. Export again. Do not edit the files. |
| `external_ref` warnings on a part | Expected when you export project by project. See [Loading the parts](loading.md#loading-the-parts). |
| The load is refused because the source was imported before | Add `--continue-import`. It is required from the second load of the same site. |
| A duplicate status, tracker or priority | Load with `--merge-duplicate-statuses`. |
| A second active sprint became planned | Specivo allows one active sprint per project. This is reported, and intended. |
| People have a `@invalid` address | Jira hid their email address. Fill in the real addresses after the import. |
| Mentions of issues from another part stay plain text | The other part is not loaded yet. Load it with the same `--links-file`. `import-map pending` shows what waits. |
| A text did not turn into a link | Someone edited it in Specivo after the import. Specivo never overwrites an edited text. |
| The load stopped part way | Run it again with `--resume RUN_ID`. Each phase commits by itself. |

## Running it again

Every part of the process can be repeated safely:

- Running the **export** again continues a stopped run, or starts a new one.
- **Loading the same bundle** again creates nothing new (with `--continue-import`).
- **Loading a newer export** of the same site adds only the records created since. It does not update
  records already imported.

If you need to start over completely, restore your Specivo backup from before the import, and load again.

## Getting help

When you report a problem, include the tool's version (`specivo-jira-migration --version`), the command
you ran **without credentials**, whether the site is Cloud or Data Center, and the message. Do not send
the bundle or the state directory: they hold your data and people's email addresses.
