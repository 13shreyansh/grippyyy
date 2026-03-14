import pytest

from agent.engine.executor import _extract_confirmation
from agent.engine.field_mapper import map_fields


@pytest.mark.asyncio
async def test_case_specific_mappings_and_transforms() -> None:
    user_data = {
        "phone": "+6591234567",
        "nric": "S1234567A",
        "company": "Shopee Singapore",
        "vendor_block": "1",
        "vendor_street": "Fusionopolis Place",
        "vendor_postal_code": "138522",
        "complaint_summary": "Shopee Singapore sold me a defective laptop and they are refusing a refund.",
        "amount": "1299",
    }
    genome = {
        "steps": [{
            "step_number": 1,
            "fields": [
                {"name": "NRIC number *", "role": "textbox", "selector_css": "#Nric"},
                {"name": "Phone number (mobile) *", "role": "textbox", "selector_css": "#Mobilephone"},
                {"name": "Vendor block/house number *", "role": "textbox", "selector_css": "#Blockhousenumber_primary"},
                {
                    "name": "Natureofcomplaints",
                    "role": "combobox",
                    "selector_css": "#Natureofcomplaints",
                    "options": ["Refund issue", "Defective or Non-Conforming Goods"],
                },
                {
                    "name": "Industry",
                    "role": "combobox",
                    "selector_css": "#Industry",
                    "options": ["Computers", "Miscellaneous"],
                },
            ],
            "nav_buttons": [],
            "submit_buttons": [],
        }]
    }

    step = (await map_fields(
        user_data, genome, species="generic_personal_info",
        use_llm_fallback=False,
    ))[0]
    mapped = {entry["field_name"]: entry["value"] for entry in step["mappings"]}

    assert mapped["NRIC number *"] == "567A"
    assert mapped["Phone number (mobile) *"] == "91234567"
    assert mapped["Vendor block/house number *"] == "1"
    assert mapped["Natureofcomplaints"] == "Refund issue"
    assert mapped["Industry"] == "Computers"


def test_extract_case_confirmation_number() -> None:
    body = (
        "Success Your Complaint has been submitted successfully. "
        "Reference Number is: T2026034558 OK"
    )

    assert _extract_confirmation(body) == "T2026034558"
