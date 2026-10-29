"""Jira Software sprints, as the bundle carries them.

Sprints come from two places: the Agile REST API (``/rest/agile/1.0/board``
and each scrum board's ``/sprint`` list, read by :mod:`.fetch`), which lists a
board's sprints whether or not they hold issues, and the Sprint field of each
issue, which names the sprints the issue has been in. Jira Cloud gives that
field's values as objects; Data Center often as a string,
``com.atlassian.greenhopper.service.sprint.Sprint@1a2b[id=1,state=CLOSED,...]``,
which :func:`parse_sprint` reads too.

**Projects.** A Jira sprint belongs to the board it was created on (its
``originBoardId``), and a board's filter can span projects, so one sprint can
hold issues of several projects. A Specivo sprint holds issues of its own
project only. So each project gets a sprint of its own for every sprint its
issues are in: the sprint of the project the board is located in keeps Jira's
id as its ``source_ref``, and every other project's is ``<id>@<project id>``.
The refs follow from Jira alone, so a project exported on its own gets the
same ones as in a bundle with the others.

**One sprint per issue.** Jira lists every sprint an issue has been in;
Specivo has one. :func:`pick` takes the sprint the issue is in now: the open
one (active, or else future: Jira lets an issue be in one open sprint at a
time), or else the closed one completed last. The others are named in the
issue's ``extra``.

**States.** future is planned, active is active, closed is completed. The
dates are the days the sprint starts and ends as Jira shows them to the
account that exports, in that account's time zone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from specivo_migration_sdk import timestamp

type JSON = dict[str, Any]

STATES = {"future": "planned", "active": "active", "closed": "completed"}
# The fields of Data Center's string form, in the order it writes them.
_LEGACY_RE = re.compile(r"^[\w.$]*Sprint@[0-9a-f]*\[(?P<body>.*)\]$", re.DOTALL)
_LEGACY_FIELD_RE = re.compile(r",(?=[A-Za-z]+=)")
_LEGACY_KEYS = {
    "id": "id",
    "rapidViewId": "originBoardId",
    "state": "state",
    "name": "name",
    "goal": "goal",
    "startDate": "startDate",
    "endDate": "endDate",
    "completeDate": "completeDate",
}


@dataclass
class SprintInfo:
    """One Jira sprint, as far as it is known."""

    id: str
    name: str
    state: str = "closed"
    goal: str | None = None
    start: str | None = None
    end: str | None = None
    completed: str | None = None
    created: str | None = None
    board_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in ("active", "future")

    def merge(self, other: SprintInfo) -> None:
        """Fill what this one lacks from *other* (an issue's copy of the same sprint)."""
        for name in ("goal", "start", "end", "completed", "created", "board_id"):
            if getattr(self, name) is None and getattr(other, name) is not None:
                setattr(self, name, getattr(other, name))


def parse_sprint(value: object) -> SprintInfo | None:
    """A sprint from the Agile API, or from an issue's Sprint field (an object, or Data Center's string)."""
    data: Mapping[str, Any]
    if isinstance(value, str):
        match = _LEGACY_RE.match(value.strip())
        if match is None:
            return None
        found: dict[str, Any] = {}
        for part in _LEGACY_FIELD_RE.split(match.group("body")):
            name, _, text = part.partition("=")
            if name in _LEGACY_KEYS and text not in ("", "<null>"):
                found[_LEGACY_KEYS[name]] = text
        data = found
    elif isinstance(value, dict):
        data = value
    else:
        return None
    sprint_id = str(data.get("id") or "").strip()
    if not sprint_id.isdigit():
        return None
    state = str(data.get("state") or "closed").strip().lower()
    goal = str(data.get("goal") or "").strip() or None
    board = data.get("originBoardId") or data.get("boardId")
    return SprintInfo(
        id=sprint_id,
        name=str(data.get("name") or "").strip() or f"Sprint {sprint_id}",
        state=state if state in STATES else "closed",
        goal=goal,
        start=_text(data.get("startDate")),
        end=_text(data.get("endDate")),
        completed=_text(data.get("completeDate")),
        created=_text(data.get("createdDate")),
        board_id=str(board) if board else None,
    )


def issue_sprints(value: object) -> list[SprintInfo]:
    """The sprints an issue's Sprint field names, in Jira's order."""
    if not isinstance(value, list):
        return []
    found = [parse_sprint(item) for item in value]
    return [sprint for sprint in found if sprint is not None]


def pick(ids: Iterable[str], known: Mapping[str, SprintInfo]) -> str | None:
    """The sprint an issue in sprints *ids* is in: the open one, or else the closed one completed last.

    Of several open ones (Jira allows one at a time, but history may say otherwise) the active one, then
    the last listed. A sprint not in *known* (deleted since) counts as closed, by its id.
    """
    listed = [sprint_id for sprint_id in dict.fromkeys(ids) if sprint_id]
    if not listed:
        return None
    rank = {sprint_id: position for position, sprint_id in enumerate(listed)}

    def key(sprint_id: str) -> tuple[int, float, int, int]:
        sprint = known.get(sprint_id)
        if sprint is None:
            return (0, 0.0, _number(sprint_id), rank[sprint_id])
        if sprint.state == "active":
            return (3, 0.0, 0, rank[sprint_id])
        if sprint.state == "future":
            return (2, 0.0, 0, rank[sprint_id])
        return (1, _epoch(sprint.completed or sprint.end), _number(sprint_id), rank[sprint_id])

    return max(listed, key=key)


def sprint_date(value: str | None, zone: str | None) -> date | None:
    """The day a sprint timestamp falls on, in *zone* when it is a time zone Python knows."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(timestamp(value))
    except ValueError:
        return None
    if zone:
        try:
            moment = moment.astimezone(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return moment.date()


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp(value)).timestamp()
    except ValueError:
        return 0.0


def _number(sprint_id: str) -> int:
    return int(sprint_id) if sprint_id.isdigit() else 0
