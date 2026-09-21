"""Focused tests for the priority-boost primitives in models.py."""

from hyqs.pipeline.models import (
    CRITICAL_PATH_BOOST_PER_DEPENDENT,
    CRITICAL_PATH_BOOST_PER_DEPTH,
    MAX_CRITICAL_PATH_BOOST,
    MAX_PRIORITY_AGING_BOOST,
    PRIORITY_AGING_PER_HOUR,
    REMEDIATION_LINK_KEYS,
    REMEDIATION_PRIORITY_BOOST,
    PriorityBoost,
    PriorityBoostReason,
    compute_effective_priority,
)


def test_compute_effective_priority_returns_base_with_no_boosts_when_nothing_applies():
    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=False,
        unresolved_dependent_count=0,
        max_dependent_depth=0,
        age_hours=0.0,
    )

    assert effective == 10
    assert reasons == []


def test_compute_effective_priority_applies_remediation_boost_only():
    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=True,
        unresolved_dependent_count=0,
        max_dependent_depth=0,
        age_hours=0.0,
    )

    assert effective == 10 + REMEDIATION_PRIORITY_BOOST
    assert len(reasons) == 1
    assert reasons[0].reason == PriorityBoostReason.REMEDIATION
    assert reasons[0].amount == REMEDIATION_PRIORITY_BOOST


def test_compute_effective_priority_caps_critical_path_boost_for_large_dependents():
    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=False,
        unresolved_dependent_count=1000,
        max_dependent_depth=1000,
        age_hours=0.0,
    )

    assert effective == 10 + MAX_CRITICAL_PATH_BOOST
    assert len(reasons) == 1
    assert reasons[0].reason == PriorityBoostReason.CRITICAL_PATH
    assert reasons[0].amount == MAX_CRITICAL_PATH_BOOST


def test_compute_effective_priority_uncapped_critical_path_boost_matches_formula():
    unresolved_dependent_count = 2
    max_dependent_depth = 1
    expected = min(
        MAX_CRITICAL_PATH_BOOST,
        unresolved_dependent_count * CRITICAL_PATH_BOOST_PER_DEPENDENT
        + max_dependent_depth * CRITICAL_PATH_BOOST_PER_DEPTH,
    )

    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=False,
        unresolved_dependent_count=unresolved_dependent_count,
        max_dependent_depth=max_dependent_depth,
        age_hours=0.0,
    )

    assert effective == 10 + expected
    assert reasons[0].amount == expected


def test_compute_effective_priority_caps_aging_boost_for_large_age():
    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=False,
        unresolved_dependent_count=0,
        max_dependent_depth=0,
        age_hours=10_000.0,
    )

    assert effective == 10 + MAX_PRIORITY_AGING_BOOST
    assert len(reasons) == 1
    assert reasons[0].reason == PriorityBoostReason.AGING
    assert reasons[0].amount == MAX_PRIORITY_AGING_BOOST


def test_compute_effective_priority_uncapped_aging_boost_matches_formula():
    age_hours = 3.0
    expected = min(MAX_PRIORITY_AGING_BOOST, int(age_hours * PRIORITY_AGING_PER_HOUR))

    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=False,
        unresolved_dependent_count=0,
        max_dependent_depth=0,
        age_hours=age_hours,
    )

    assert effective == 10 + expected
    assert reasons[0].amount == expected


def test_compute_effective_priority_combines_all_three_boosts_in_order():
    effective, reasons = compute_effective_priority(
        base_priority=10,
        is_remediation=True,
        unresolved_dependent_count=2,
        max_dependent_depth=1,
        age_hours=3.0,
    )

    assert [r.reason for r in reasons] == [
        PriorityBoostReason.REMEDIATION,
        PriorityBoostReason.CRITICAL_PATH,
        PriorityBoostReason.AGING,
    ]
    assert effective == 10 + sum(r.amount for r in reasons)
    assert all(r.amount >= 0 for r in reasons)
    assert all(r.amount != 0 for r in reasons)


def test_priority_boost_to_dict_shape():
    boost = PriorityBoost(
        reason=PriorityBoostReason.REMEDIATION,
        amount=20,
        detail="remediation job (+20)",
    )

    assert boost.to_dict() == {
        "reason": "remediation",
        "amount": 20,
        "detail": "remediation job (+20)",
    }


def test_remediation_link_keys_is_a_tuple_of_expected_strings():
    assert REMEDIATION_LINK_KEYS == (
        "ai_fix_for",
        "deploy_fix_for",
        "alembic_merge_fix_for",
        "fix_for",
        "fixes_job_id",
        "parent_job_id",
        "remediation_root_job_id",
    )
