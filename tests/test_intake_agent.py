import re

from hyqs.intake.agent import INTERVIEW_SYS
from hyqs.intake.gate import _REQUIRED


def test_interview_prompt_required_fields_match_completeness_gate():
    match = re.search(
        r"REQUIRED SPEC FIELDS \(authoritative order\):\n([^\n]+)",
        INTERVIEW_SYS,
    )

    assert match is not None
    prompt_required = [field.strip() for field in match.group(1).split("|")]
    assert prompt_required == _REQUIRED
