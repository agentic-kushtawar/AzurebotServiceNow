from core.voice.sop_validation import limit_sop_steps, validate_steps


def _build_long_sop():
    return {
        "sop_id": "SOP_20251225T124006",
        "title": "Uploaded SOP",
        "steps": [
            {"step_id": 1, "action": "Standard Operating Procedure - Handling of", "parameters": {}},
            {"step_id": 2, "action": "Out-of-Specification (OOS) Results", "parameters": {}},
            {"step_id": 3, "action": "SOP ID: QA-LAB-OOS-001 | Version: 3.1 | Effective Date: 15-Aug-2024", "parameters": {}},
            {"step_id": 4, "action": "Controlled Document - Uncontrolled When Printed", "parameters": {}},
            {"step_id": 5, "action": "Purpose", "parameters": {}},
            {"step_id": 6, "action": "This SOP defines the procedure for identification, investigation, documentation, and closure of", "parameters": {}},
            {"step_id": 7, "action": "Out-of-Specification (OOS) laboratory test results to ensure compliance with EU GMP and FDA 21", "parameters": {}},
            {"step_id": 8, "action": "CFR Part 211.", "parameters": {}},
            {"step_id": 9, "action": "Scope", "parameters": {}},
            {"step_id": 10, "action": "This procedure applies to all analytical testing of raw materials, intermediates, and finished", "parameters": {}},
        ],
    }


def test_sop_validation_first_six_steps_compliant():
    sop = limit_sop_steps(_build_long_sop(), 6)
    steps = [
        {"action": "Standard Operating Procedure - Handling of", "parameters": {}},
        {"action": "Out-of-Specification (OOS) Results", "parameters": {}},
        {"action": "SOP ID: QA-LAB-OOS-001 | Version: 3.1 | Effective Date: 15-Aug-2024", "parameters": {}},
        {"action": "Controlled Document - Uncontrolled When Printed", "parameters": {}},
        {"action": "Purpose", "parameters": {}},
        {"action": "This SOP defines the procedure for identification, investigation, documentation, and closure of", "parameters": {}},
    ]

    result = validate_steps(sop, steps)

    assert result.status == "COMPLIANT"
    assert result.issues == []


def test_sop_validation_first_six_steps_partially_compliant():
    sop = limit_sop_steps(_build_long_sop(), 6)
    steps = [
        {"action": "Standard Operating Procedure - Handling of", "parameters": {}},
        {"action": "Out-of-Specification (OOS) Results", "parameters": {}},
        {"action": "SOP ID: QA-LAB-OOS-001 | Version: 3.1 | Effective Date: 15-Aug-2024", "parameters": {}},
        {"action": "Controlled Document - Uncontrolled When Printed", "parameters": {}},
        {"action": "Purpose", "parameters": {}},
        # Missing step 6
    ]

    result = validate_steps(sop, steps)

    assert result.status == "PARTIALLY_COMPLIANT"
    assert len(result.issues) >= 1
