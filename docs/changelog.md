---
description: Version history of specivo-jira-migration and the Specivo versions each release works with.
---

# Changelog

The tool is released from tags on GitHub. See the
[releases page](https://github.com/specivo/jira-migration-tool/releases) for each release's files.

## 0.1.0

First release. Writes import bundle format 1.0; loading it needs Specivo 0.11.0 or later.

Carries over:

- projects, issue types, statuses and priorities;
- issues with their description, people, dates and estimates (original and remaining);
- comments, and change history of status, assignee, priority, summary, issue type, estimate, fix version,
  component, parent, sprint, attachments and custom fields;
- attachments and the images and files shown in text;
- issue links as relations, and parents (sub-tasks, epic children) across projects and bundles;
- worklogs as time entries;
- components as categories, and versions;
- sprints from Jira Software boards;
- custom fields of the common types, with the contexts that say where they apply;
- labels as tags;
- watchers.

Text is converted from Atlassian Document Format (Jira Cloud) or wiki markup (Jira Data Center) to
Markdown. Everything that is left out is counted in the bundle's manifest. See
[What is not carried over](not-carried-over.md).

Works with Jira Cloud and with Jira Data Center 8.14 or later. Requires Python 3.12 or later.

### Compatibility

| Tool | Bundle format | Specivo to load it |
|---|---|---|
| 0.1.x | 1.0 | 0.11.0 or later |

A minor version of the bundle format only adds optional properties; a new major version is refused by
Specivo before anything is read. Each release states which format it writes.
