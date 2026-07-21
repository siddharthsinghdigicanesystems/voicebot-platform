"""Agent prompt and tool schema tests.

These guard against accidental regressions in the prompt that have outsized
effect on bot behavior (forgotten guardrail, removed tool, etc.).
"""

from __future__ import annotations

from app.agent import TOOL_SCHEMAS, build_initial_user_hint, build_system_prompt
from app.telephony.base import CallDirection


def test_tool_schemas_have_required_fields() -> None:
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "lookup_customer",
        "confirm_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "schedule_appointment",
        "lookup_test_results",
        "transfer_to_human",
        "end_call",
    }
    for tool in TOOL_SCHEMAS:
        assert tool["type"] == "function"
        assert tool["description"]
        params = tool["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert isinstance(params.get("required", []), list)


def test_inbound_prompt_contains_inbound_flow() -> None:
    p = build_system_prompt(CallDirection.INBOUND)
    assert "Greet warmly" in p
    assert "lookup_customer" in p


def test_outbound_prompt_contains_outbound_flow() -> None:
    p = build_system_prompt(CallDirection.OUTBOUND)
    assert "Greet by name" in p
    assert "Is now a good time" in p


def test_outbound_hint_includes_appointment_context() -> None:
    hint = build_initial_user_hint(
        CallDirection.OUTBOUND,
        customer_name="Priya",
        appointment={"service": "consultation", "date": "2026-05-09", "time": "15:00"},
    )
    assert hint is not None
    assert "Priya" in hint
    assert "consultation" in hint
    assert "2026-05-09" in hint


def test_inbound_hint_is_none() -> None:
    assert build_initial_user_hint(CallDirection.INBOUND) is None


def test_prompt_contains_critical_guardrails() -> None:
    """If any of these get accidentally removed, fail loudly in CI."""
    p = build_system_prompt(CallDirection.INBOUND)
    assert "credit card" in p.lower()
    assert "transfer" in p.lower()
    assert "make up" in p.lower() or "never make up" in p.lower()


def test_brand_is_substituted() -> None:
    p = build_system_prompt(CallDirection.INBOUND, brand="Globex")
    assert "Globex" in p
    assert "CityCare Hospital" not in p


def test_default_brand_is_citycare() -> None:
    p = build_system_prompt(CallDirection.INBOUND)
    assert "CityCare Hospital" in p
    assert "lookup_test_results" in p
    assert "confirm_appointment" in p


def test_hinglish_prompt_uses_hinglish_flow() -> None:
    p = build_system_prompt(CallDirection.OUTBOUND, language="hinglish")
    assert "Hinglish" in p
    # Hinglish flow uses Hinglish-specific phrases.
    assert "permission lein" in p.lower() or "naam se greet" in p.lower()
    # Guardrails preserved across languages.
    assert "credit card" in p.lower() or "otp" in p.lower()


def test_hindi_prompt_uses_hindi_flow() -> None:
    p = build_system_prompt(CallDirection.INBOUND, language="hi")
    # Some Devanagari-romanized Hindi keyword unique to the Hindi flow.
    assert "swagat" in p.lower() or "sambhav" in p.lower()


def test_unknown_language_falls_back_to_english() -> None:
    p = build_system_prompt(CallDirection.INBOUND, language="klingon")
    # English flow contains "Greet warmly"; Hinglish/Hindi don't.
    assert "Greet warmly" in p


def test_language_aliases_resolve() -> None:
    # 'hindi' / 'hi-in' / 'hi-en' should all resolve to the right base language.
    assert "swagat" in build_system_prompt(
        CallDirection.INBOUND, language="hindi"
    ).lower() or "sambhav" in build_system_prompt(
        CallDirection.INBOUND, language="hindi"
    ).lower()
    assert "Hinglish" in build_system_prompt(
        CallDirection.INBOUND, language="hi-en"
    )
