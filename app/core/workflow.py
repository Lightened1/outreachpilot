"""Prospect workflow state machine.

    imported -> validated -> researching -> researched -> generating -> pending_approval
             -> approved -> executing -> completed

with the side paths:

* ``pending_approval -> rejected -> generating`` (regenerate after a rejection)
* ``pending_approval -> generating`` (regenerate before deciding)
* any working state ``-> failed`` (bounded retries exhausted or a non-retryable error)
* research/execution ``-> blocked`` (the site refused access; a human must look at it)
* ``failed``/``blocked`` -> the step that failed, when a user explicitly retries it

Transitions are explicit data, so the API can explain why a request is refused and the
tests can check every rule.
"""

from __future__ import annotations

from app.core.enums import ProspectStatus as S
from app.core.errors import InvalidTransitionError

TRANSITIONS: dict[S, frozenset[S]] = {
    S.IMPORTED: frozenset({S.VALIDATED, S.FAILED}),
    S.VALIDATED: frozenset({S.RESEARCHING}),
    S.RESEARCHING: frozenset({S.RESEARCHED, S.FAILED, S.BLOCKED}),
    S.RESEARCHED: frozenset({S.GENERATING, S.RESEARCHING}),
    S.GENERATING: frozenset({S.PENDING_APPROVAL, S.FAILED}),
    S.PENDING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.GENERATING}),
    S.REJECTED: frozenset({S.GENERATING}),
    S.APPROVED: frozenset({S.EXECUTING, S.GENERATING}),
    S.EXECUTING: frozenset({S.COMPLETED, S.FAILED, S.BLOCKED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset({S.RESEARCHING, S.GENERATING, S.EXECUTING}),
    S.BLOCKED: frozenset({S.RESEARCHING, S.EXECUTING}),
}

# States in which a background task owns the prospect; a second task must not start.
BUSY_STATES = frozenset({S.RESEARCHING, S.GENERATING, S.EXECUTING})
TERMINAL_STATES = frozenset({S.COMPLETED})


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS[current]


def ensure_transition(current: S | str, target: S | str) -> S:
    """Return ``target`` if the move is allowed, otherwise raise ``InvalidTransitionError``."""
    cur, tgt = S(current), S(target)
    if not can_transition(cur, tgt):
        allowed = sorted(s.value for s in TRANSITIONS[cur])
        raise InvalidTransitionError(
            f"Cannot move prospect from '{cur.value}' to '{tgt.value}'.",
            details={"current": cur.value, "requested": tgt.value, "allowed": allowed},
        )
    return tgt


def allowed_next(current: S | str) -> list[str]:
    return sorted(s.value for s in TRANSITIONS[S(current)])
