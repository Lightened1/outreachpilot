"""The prospect state machine: every allowed and forbidden move."""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.core.enums import ProspectStatus as S
from app.core.errors import InvalidTransitionError
from app.core.workflow import BUSY_STATES, TRANSITIONS, allowed_next, can_transition, ensure_transition

HAPPY_PATH = [
    S.IMPORTED,
    S.VALIDATED,
    S.RESEARCHING,
    S.RESEARCHED,
    S.GENERATING,
    S.PENDING_APPROVAL,
    S.APPROVED,
    S.EXECUTING,
    S.COMPLETED,
]


def test_every_status_has_rules() -> None:
    assert set(TRANSITIONS) == set(S)


def test_happy_path_is_allowed() -> None:
    for current, nxt in pairwise(HAPPY_PATH):
        assert ensure_transition(current, nxt) is nxt


def test_messages_cannot_be_sent_without_approval() -> None:
    for state in (S.VALIDATED, S.RESEARCHED, S.GENERATING, S.PENDING_APPROVAL, S.REJECTED):
        assert not can_transition(state, S.EXECUTING)
    assert can_transition(S.APPROVED, S.EXECUTING)


def test_cannot_skip_research() -> None:
    assert not can_transition(S.VALIDATED, S.GENERATING)


def test_rejection_leads_to_regeneration_only() -> None:
    assert allowed_next(S.REJECTED) == [S.GENERATING.value]


def test_completed_is_terminal() -> None:
    assert allowed_next(S.COMPLETED) == []
    with pytest.raises(InvalidTransitionError):
        ensure_transition(S.COMPLETED, S.EXECUTING)


@pytest.mark.parametrize("busy", sorted(BUSY_STATES, key=lambda s: s.value))
def test_busy_states_can_fail(busy: S) -> None:
    assert can_transition(busy, S.FAILED)


def test_only_browser_steps_can_be_blocked() -> None:
    blockable = {s for s, targets in TRANSITIONS.items() if S.BLOCKED in targets}
    assert blockable == {S.RESEARCHING, S.EXECUTING}


def test_failed_and_blocked_can_be_retried_explicitly() -> None:
    assert can_transition(S.FAILED, S.RESEARCHING)
    assert can_transition(S.FAILED, S.EXECUTING)
    assert can_transition(S.BLOCKED, S.RESEARCHING)
    assert not can_transition(S.BLOCKED, S.COMPLETED)


def test_invalid_transition_explains_allowed_moves() -> None:
    with pytest.raises(InvalidTransitionError) as exc:
        ensure_transition("validated", "approved")
    assert exc.value.details == {"current": "validated", "requested": "approved", "allowed": ["researching"]}
    assert exc.value.status_code == 409
