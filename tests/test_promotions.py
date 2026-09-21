import pytest

from hyqs.pipeline import promotions
from hyqs.pipeline.models import PromotionKind, PromotionState

_ALL_STATES = list(PromotionState)

_LEGAL_PAIRS = [
    (PromotionState.DISPATCHED, PromotionState.DEPLOYING),
    (PromotionState.DISPATCHED, PromotionState.SUPERSEDED),
    (PromotionState.AVAILABLE, PromotionState.DEPLOYING),
    (PromotionState.AVAILABLE, PromotionState.DECLINED),
    (PromotionState.AVAILABLE, PromotionState.SUPERSEDED),
    (PromotionState.DEPLOYING, PromotionState.DEPLOYED),
    (PromotionState.DEPLOYING, PromotionState.FAILED),
    (PromotionState.DEPLOYING, PromotionState.SUPERSEDED),
]


@pytest.mark.parametrize("current,target", _LEGAL_PAIRS)
def test_validate_transition_allows_legal_transitions(current, target):
    promotions.validate_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    "current,target",
    [
        (current, target)
        for current in _ALL_STATES
        for target in _ALL_STATES
        if (current, target) not in _LEGAL_PAIRS
    ],
)
def test_validate_transition_rejects_illegal_transitions(current, target):
    with pytest.raises(ValueError):
        promotions.validate_transition(current, target)


@pytest.mark.parametrize(
    "state,expected",
    [
        (PromotionState.DISPATCHED, False),
        (PromotionState.AVAILABLE, False),
        (PromotionState.DEPLOYING, False),
        (PromotionState.DEPLOYED, True),
        (PromotionState.DECLINED, True),
        (PromotionState.FAILED, True),
        (PromotionState.SUPERSEDED, True),
    ],
)
def test_is_terminal(state, expected):
    assert promotions.is_terminal(state) is expected


def test_initial_state_for_promote_is_dispatched():
    assert promotions.initial_state(PromotionKind.PROMOTE) == PromotionState.DISPATCHED


def test_initial_state_for_offer_is_available():
    assert promotions.initial_state(PromotionKind.OFFER) == PromotionState.AVAILABLE


def test_is_noop_promotion_true_when_release_matches_current():
    assert promotions.is_noop_promotion(5, 5) is True


def test_is_noop_promotion_false_when_no_current_release():
    assert promotions.is_noop_promotion(5, None) is False


def test_is_noop_promotion_false_when_release_differs():
    assert promotions.is_noop_promotion(5, 6) is False
