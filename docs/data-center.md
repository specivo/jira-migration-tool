---
description: Using specivo-jira-migration with Jira Data Center, how it differs from Jira Cloud, and a note on Data Center's announced end of life.
---

# Jira Data Center

The tool works with Jira Data Center (and Jira Server) **8.14 or later**, the first versions with personal
access tokens.

## How it differs from Jira Cloud

| | Jira Cloud | Jira Data Center |
|---|---|---|
| Credentials | Email address and API token | Personal access token (`JIRA_PAT`) |
| REST API | v3 | v2 |
| Text format | Atlassian Document Format, converted by the tool | Wiki markup, converted with jira2markdown |
| Issue search | `POST /rest/api/3/search/jql`, paged by token | `/rest/api/2/search`, paged by offset |
| Epic children | The `parent` field | The **Epic Link** field, read the same way |
| People | Identified by account id | Identified by user key |
| Custom field contexts | Read, with **Administer Jira** | Not read. A field applies to the projects whose issues use it. |
| Images in text | Tied to attachments by media id | Written by name (`!file.png!`, `[^file.txt]`) |

Everything else works the same way. [What is carried over](carried-over.md) describes both.

## Example

```bash
export JIRA_URL=https://jira.example.com
export JIRA_PAT=your_personal_access_token_here
specivo-jira-migration --project SUP --out jira-export --validate
```

## End of life

Atlassian has announced that Data Center is being retired: new customers can no longer buy it, existing
customers can renew for a limited time, and the product reaches its end of life after that. The dates
Atlassian published at the time of writing are:

| Milestone | Date |
|---|---|
| End of sale to new customers | 30 March 2026 |
| Existing customers can renew up to | 28 March 2029 |
| End of life | 28 March 2029 |

Atlassian may change these dates; check
[Atlassian's Data Center end-of-life page](https://www.atlassian.com/licensing/data-center-end-of-life)
for the current ones. If you run
Data Center and are planning to leave it, start a trial export early. You can export and load as often as
you like, and a [later export](loading.md#catching-up-before-the-switch) adds only what is new.
