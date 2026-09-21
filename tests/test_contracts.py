"""Focused tests for structured stage contract semantics."""

import json

import pytest

from hyqs.pipeline.contracts import SchemaValidationError, parse_and_validate


def _wrap(data: dict) -> str:
    return f"<<<RESULT_JSON>>>\n{json.dumps(data)}\n<<<END_RESULT>>>"


def _security_finding(path_marker=...):
    finding = {"severity": "high", "note": "Unsafe command construction."}
    if path_marker is not ...:
        finding["path"] = path_marker
    return {"verdict": "fail", "summary": "finding", "findings": [finding]}


def test_security_contract_preserves_canonical_finding_path():
    data = _security_finding("hyqs/pipeline/contracts.py")

    assert parse_and_validate("security", _wrap(data)) == data


def test_security_contract_preserves_finding_without_path():
    data = _security_finding()

    assert parse_and_validate("security", _wrap(data)) == data


@pytest.mark.parametrize(
    "path",
    [
        None,
        7,
        [],
        "",
        "/etc/passwd",
        "C:/temp/file.py",
        "../file.py",
        "src/../file.py",
        "./file.py",
        "src//file.py",
        "src/",
        "src\\file.py",
        "src/*.py",
        "src/file[0].py",
        "src/{a,b}.py",
        "src/a.py,src/b.py",
        "src/a.py:10-20",
        "src/a file.py",
        "src/file.py (handler)",
        "~user/file.py",
    ],
)
def test_security_contract_rejects_unsafe_finding_path(path):
    with pytest.raises(SchemaValidationError, match="path|schema violation"):
        parse_and_validate("security", _wrap(_security_finding(path)))


def test_plan_contract_retains_existing_path_character_semantics():
    path = "docs/security review.md"
    data = {
        "summary": "Update documentation.",
        "stories": [
            {
                "id": "S1",
                "title": "Document",
                "task": "Update documentation.",
                "acceptance": "Documentation is current.",
                "target_files": [path],
            }
        ],
        "reuses": [],
        "adds": [],
        "file_impact": [
            {
                "path": path,
                "role": "documentation",
                "justification": "Explain the behavior.",
                "status": "existing",
                "provenance": "S1",
            }
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Documentation only.",
        },
        "target_files": [path],
        "activation": {
            "config_change_required": False,
            "activation_location": "N/A",
            "expected_live_effect": "Documentation reflects the new behavior.",
        },
    }

    assert parse_and_validate("plan", _wrap(data))["target_files"] == [path]


def _minimal_plan_payload(path: str = "docs/notes.md") -> dict:
    return {
        "summary": "Update documentation.",
        "stories": [
            {
                "id": "S1",
                "title": "Document",
                "task": "Update documentation.",
                "acceptance": "Documentation is current.",
                "target_files": [path],
            }
        ],
        "reuses": [],
        "adds": [],
        "file_impact": [
            {
                "path": path,
                "role": "documentation",
                "justification": "Explain the behavior.",
                "status": "existing",
                "provenance": "S1",
            }
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Documentation only.",
        },
        "target_files": [path],
    }


def test_plan_contract_rejects_payload_missing_activation():
    data = _minimal_plan_payload()

    with pytest.raises(SchemaValidationError, match="activation|required"):
        parse_and_validate("plan", _wrap(data))


def test_plan_contract_accepts_and_returns_well_formed_activation():
    data = _minimal_plan_payload()
    data["activation"] = {
        "config_change_required": True,
        "activation_location": "hyqs/config.py FEATURE_FLAG",
        "expected_live_effect": "The new endpoint starts returning data once deployed.",
    }

    result = parse_and_validate("plan", _wrap(data))

    assert result["activation"] == data["activation"]


def test_plan_contract_rejects_config_change_required_with_blank_activation_location():
    data = _minimal_plan_payload()
    data["activation"] = {
        "config_change_required": True,
        "activation_location": "   ",
        "expected_live_effect": "The new endpoint starts returning data once deployed.",
    }

    with pytest.raises(SchemaValidationError, match="activation_location"):
        parse_and_validate("plan", _wrap(data))


def test_plan_contract_rejects_config_change_required_with_blank_expected_live_effect():
    data = _minimal_plan_payload()
    data["activation"] = {
        "config_change_required": True,
        "activation_location": "hyqs/config.py FEATURE_FLAG",
        "expected_live_effect": "",
    }

    with pytest.raises(SchemaValidationError, match="expected_live_effect"):
        parse_and_validate("plan", _wrap(data))


def test_plan_contract_accepts_config_change_not_required_with_blank_activation_fields():
    data = _minimal_plan_payload()
    data["activation"] = {
        "config_change_required": False,
        "activation_location": "",
        "expected_live_effect": "   ",
    }

    result = parse_and_validate("plan", _wrap(data))

    assert result["activation"] == data["activation"]
