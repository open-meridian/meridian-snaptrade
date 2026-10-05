"""The plan-code link, read from the plugin's settings (contract v14: W2.10's
notes, W4.8, W6.11; meridian-design plans/the-custodians-activity-explains-
a-break, question 4, and the product owner's option A of 2026-10-05).

A plan's own fund code -- Fidelity's `OQKR` for the plan's VIGIX -- names no
security SnapTrade resolves, so an admin of the plugin links it, once, per
external account, to the instrument it is. The links are the table setting
`plan_code_links`, declared in settings.py: an external account this plugin
reported, the plan's code as SnapTrade names it, and a deployment instrument
record's ID, entered in the dashboard's Settings form as an editable typed
table, which checks every cell. The plugin only reads it, as any setting
arrives, and never sets it: no plugin storage holds a person's choice, and
the deployment's configuration records each change and who made it.

Each row arrives with `changed_by` and `changed_at`, which the conductor
stamps when a row is added or changed; an activity resolved through a link
names them as its provenance (requirement 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from meridian.bounds import PROVENANCE_PERSON_LENGTH

#: The setting's columns, by name.
ACCOUNT = "account"
CODE = "code"
INSTRUMENT = "instrument"


@dataclass(frozen=True)
class PlanCodeLink:
    """A plan's own fund code on one external account, linked by a named
    person to the deployment's instrument record it is (requirement 3)."""

    external_account_id: str
    code: str
    instrument_id: str
    #: Who added or last changed the row, and when, as the conductor stamped.
    changed_by: str = ""
    changed_at: str = ""

    @property
    def person(self) -> str:
        """Who linked it and when, as the activity's provenance names them,
        within its bound."""
        said = f"{self.changed_by}, {self.changed_at}" if self.changed_at else self.changed_by
        return said[: PROVENANCE_PERSON_LENGTH.most]


def links_of(rows: object) -> tuple[PlanCodeLink, ...]:
    """The links a delivered `plan_code_links` holds: each row naming an
    account, a code and an instrument; any other row is not a link, and
    nothing is guessed from it."""
    if not isinstance(rows, list):
        return ()
    return tuple(
        PlanCodeLink(
            external_account_id=row[ACCOUNT],
            code=row[CODE],
            instrument_id=row[INSTRUMENT],
            changed_by=row.get("changed_by", ""),
            changed_at=row.get("changed_at", ""),
        )
        for row in rows
        if isinstance(row, Mapping)
        and all(
            isinstance(row.get(name), str) and row.get(name)
            for name in (ACCOUNT, CODE, INSTRUMENT)
        )
    )


def link_for(
    links: Iterable[PlanCodeLink], external_account_id: str, code: str
) -> PlanCodeLink | None:
    """The person's link for a code on this account, or None."""
    return next(
        (
            link
            for link in links
            if link.external_account_id == external_account_id and link.code == code
        ),
        None,
    )
