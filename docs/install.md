---
description: Install the specivo-jira-migration command from a GitHub release with uv or pipx, or from a source checkout, and check that it runs.
---

# Installation

The tool is a Python command, `specivo-jira-migration`. It needs **Python 3.12 or later**. There is no
package on PyPI: install it from a tagged release on GitHub.

## From a release

Pick the release you want from the
[releases page](https://github.com/specivo/jira-migration-tool/releases) and install it with
[uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/):

```bash
uv tool install git+https://github.com/specivo/jira-migration-tool@vX.Y.Z
# or
pipx install git+https://github.com/specivo/jira-migration-tool@vX.Y.Z
```

Replace `vX.Y.Z` with the release tag. Both commands put `specivo-jira-migration` on your path in an
isolated environment.

## From a source checkout

```bash
git clone https://github.com/specivo/jira-migration-tool.git
cd jira-migration-tool
uv sync
uv run specivo-jira-migration --help
```

A checkout installs the Specivo migration SDK from a path next to it while the two are developed
together. A release pins the SDK to a tag. Use a release unless you are changing the tool.

## Check the install

```bash
specivo-jira-migration --version
specivo-jira-migration --help
```

`--help` lists every option; the [export page](exporting.md#options) describes them.

## Where to run it

Run the tool on a machine that can reach Jira and has room for the bundle. For a site with many large
attachments, that can be a lot of disk: each distinct file is stored once in the bundle's `files/` folder,
and the tool also keeps a working state next to the output while it runs (see
[If a run stops](exporting.md#if-a-run-stops)). Then copy the finished bundle to the machine that runs
Specivo, or mount it into the Specivo container.
