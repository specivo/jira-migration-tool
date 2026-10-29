---
description: Move projects, issues, comments, history and attachments from Jira Cloud or Jira Data Center into Specivo with the specivo-jira-migration tool. What it does, what you need, and where to start.
---

# Jira migration tool

The Jira migration tool copies projects, issues, comments, change history, attachments and more from
Jira Cloud or Jira Data Center into [Specivo](https://specivo.io). It reads Jira through Jira's public
REST API with your own credentials, and writes a Specivo **import bundle**: a folder (or a `.zip`) of
plain files. Specivo then checks the bundle and loads it.

```text
Jira  --REST API-->  specivo-jira-migration  -->  import bundle  -->  Specivo
                     (this tool, on your machine)   (files)           (validate, dry run, import)
```

The tool never changes anything in Jira. Credentials stay on your machine: they are not written to the
bundle, to a log or to the tool's working files.

!!! note "Not affiliated with Atlassian"
    This tool is not affiliated with or endorsed by Atlassian. "Jira" is a trademark of Atlassian and is
    used here only to say which system the tool reads.

## What you need

| | Requirement |
|---|---|
| **Specivo** | 0.11.0 or later, to load the bundle (bundle format 1.0). |
| **The machine that runs the tool** | Python 3.12 or later, and enough disk space for the attachments you want to carry. It needs network access to Jira only. |
| **Jira Cloud** | The REST API v3. An account with an API token. |
| **Jira Data Center** (or Server) | Version 8.14 or later, which has personal access tokens. The tool uses REST API v2. |
| **Permissions of the Jira account** | See the table below. |

### Permissions the Jira account needs

The account is used read-only. It sees only what Jira lets it see, so use an account that can see every
project you want to migrate.

| Permission in Jira | Needed for | If it is missing |
|---|---|---|
| **Browse projects** (and issue-level view rights) on each project | Projects, issues, comments, history, attachments | The project is not found, and the tool stops with a usage error. |
| **Administer Jira** (Jira Cloud) | Reading where each custom field applies (its contexts) | The tool counts the refusal and applies the field to the projects whose issues use it. |
| **View voters and watchers** | Listing the watchers of an issue | Watchers are counted, not carried. The export goes on. |
| Access to the project's boards (Jira Software) | Reading sprints from boards | The sprints are the ones the issues name. The tool counts each board it could not read. |

## How a migration goes

1. [Install the tool](install.md).
2. [Give it credentials](authentication.md) for your Jira site.
3. [Export](exporting.md) a trial of a few issues, then the whole site or project by project.
4. [Check, rehearse and load the bundle](loading.md) in Specivo.
5. Read the report. Anything the tool could not carry is counted in the bundle's manifest, and Specivo
   prints it in the import report. See [What is not carried over](not-carried-over.md).
6. Export again just before you switch over, and load the new bundle to add what changed. The
   [loading page](loading.md#catching-up-before-the-switch) explains how.

## In these docs

- [Installation](install.md)
- [Authentication and configuration](authentication.md)
- [Exporting from Jira](exporting.md)
- [Validating and loading the bundle](loading.md)
- [What is carried over](carried-over.md)
- [How text is converted](text-conversion.md)
- [What is not carried over](not-carried-over.md)
- [Troubleshooting](troubleshooting.md)
- [Jira Data Center](data-center.md)
- [Changelog](changelog.md)

For the Specivo side of an import, see [Importing from another tracker](https://specivo.io/docs/specivo/install/importing/)
in the Specivo user guide. The bundle format is specified in
[`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md) in the
Specivo repository.
