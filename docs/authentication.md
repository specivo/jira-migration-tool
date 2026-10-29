---
description: Give specivo-jira-migration credentials for Jira Cloud (email and API token) or Jira Data Center (personal access token), choose the site name, and use environment variables so secrets stay out of your shell history.
---

# Authentication and configuration

The tool needs three things: the address of your Jira site, credentials, and the keys of the projects to
export. Each can be an option or an environment variable.

## Credentials

Pass credentials in the environment. That keeps them out of the process list and your shell history. The
tool never writes them anywhere and never prints them.

=== "Jira Cloud"

    Use the email address of an account and an
    [API token](https://id.atlassian.com/manage-profile/security/api-tokens) created for it.

    ```bash
    export JIRA_URL=https://your-site.example
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=your_api_token_here
    ```

=== "Jira Data Center"

    Use a personal access token (Data Center 8.14 or later).

    ```bash
    export JIRA_URL=https://jira.example.com
    export JIRA_PAT=your_personal_access_token_here
    ```

The tool asks the site (`/rest/api/2/serverInfo`) which kind it is and refuses credentials of the other
kind. Setting the email and token variables together with `JIRA_PAT` is refused as ambiguous: unset the
ones for the other kind of site.

!!! tip "Use a dedicated account"
    Create an account for the migration and give it read access to the projects you are moving. You can
    delete the token when you are done. The account does not need to change anything in Jira.

The permissions the account needs are listed on the [overview page](index.md#permissions-the-jira-account-needs).

## Settings

| Setting | Option | Environment variable | Notes |
|---|---|---|---|
| Site address | `--url` | `JIRA_URL` | The base address of the site. |
| Cloud email | `--email` | `JIRA_EMAIL` | Cloud only. |
| Cloud API token | `--api-token` | `JIRA_API_TOKEN` | Cloud only. Prefer the variable. |
| Data Center token | `--pat` | `JIRA_PAT` | Data Center only. Prefer the variable. |
| Projects | `--project KEY` | `JIRA_PROJECTS` | Repeatable, or comma-separated. |
| Site name in the bundle | `--instance NAME` | none | See below. |

Options override the environment. The tool has no configuration file of its own: if you keep these
values in a file, load it into your shell's environment before you run the tool, and keep that file
readable by you only.

A run needs `--url` (or `JIRA_URL`), credentials, at least one project, and `--out`. A missing one stops
the tool with exit status 2 and a message saying what to give.

## The site name (`--instance`)

Every bundle names the system and the installation it came from. For this tool the system is `jira`, and
the installation is the **site name**: by default the host name of `--url` (plus its path, for a site
served under one).

Specivo remembers which Jira record became which Specivo row under that name. That is what lets you:

- load the same export twice without duplicates;
- export the site again later and add only what is new;
- export project by project and have the parts link to each other.

So use **the same site name on every export of the same Jira site**. The default already does that, as
long as you always give the same `--url`. Set `--instance` yourself when the address changes between
exports (a new host name for the same Jira), or when you migrate two different Jira sites into one
Specivo and need them told apart.

## Where output goes

`--out PATH` is where the bundle is written: a new or empty directory, or a path ending in `.zip`
(or add `--zip`). The tool refuses to write over an existing bundle or into a folder that is not empty.
