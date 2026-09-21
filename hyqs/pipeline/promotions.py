"""Pure state-machine helpers for promotions — no store/DB access."""

from __future__ import annotations

from .models import PromotionKind, PromotionState

_NON_TERMINAL = {
    PromotionState.DISPATCHED,
    PromotionState.AVAILABLE,
    PromotionState.DEPLOYING,
}
_TERMINAL = {
    PromotionState.DEPLOYED,
    PromotionState.DECLINED,
    PromotionState.FAILED,
    PromotionState.SUPERSEDED,
}

_LEGAL_TRANSITIONS: dict[PromotionState, set[PromotionState]] = {
    PromotionState.DISPATCHED: {PromotionState.DEPLOYING, PromotionState.SUPERSEDED},
    PromotionState.AVAILABLE: {
        PromotionState.DEPLOYING,
        PromotionState.DECLINED,
        PromotionState.SUPERSEDED,
    },
    PromotionState.DEPLOYING: {
        PromotionState.DEPLOYED,
        PromotionState.FAILED,
        PromotionState.SUPERSEDED,
    },
    PromotionState.DEPLOYED: set(),
    PromotionState.DECLINED: set(),
    PromotionState.FAILED: set(),
    PromotionState.SUPERSEDED: set(),
}


def is_terminal(state: PromotionState) -> bool:
    return state in _TERMINAL


def validate_transition(current: PromotionState, target: PromotionState) -> None:
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"illegal promotion transition: {current.value} -> {target.value}"
        )


def initial_state(kind: PromotionKind) -> PromotionState:
    return (
        PromotionState.DISPATCHED
        if kind == PromotionKind.PROMOTE
        else PromotionState.AVAILABLE
    )


def is_noop_promotion(release_id: int, current_release_id: int | None) -> bool:
    return release_id == current_release_id
