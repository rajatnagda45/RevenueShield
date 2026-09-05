"""Unit tests for VoiceIntentExtractor speech date parsing and intent detection."""
from datetime import datetime, timezone
import pytest
from zoneinfo import ZoneInfo

from app.services.voice_intent_extractor import VoiceCustomerIntent, VoiceIntentExtractor


@pytest.fixture
def ref_wednesday():
    # Wednesday, Aug 26, 2026, 10:00 AM IST
    tz = ZoneInfo("Asia/Kolkata")
    return datetime(2026, 8, 26, 10, 0, 0, tzinfo=tz)


def test_extract_tomorrow_intent(ref_wednesday):
    res = VoiceIntentExtractor.extract_promise_intent(
        speech_text="I will pay tomorrow by evening",
        reference_datetime=ref_wednesday,
    )
    assert res.intent == VoiceCustomerIntent.PROMISE_TO_PAY
    assert res.promised_date is not None
    # Tomorrow is Thursday, Aug 27, 2026
    local_p = res.promised_date.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local_p.day == 27
    assert local_p.month == 8
    assert "Thursday" in res.promised_date_display


def test_extract_day_after_tomorrow(ref_wednesday):
    res = VoiceIntentExtractor.extract_promise_intent(
        speech_text="I can do it day after tomorrow",
        reference_datetime=ref_wednesday,
    )
    assert res.intent == VoiceCustomerIntent.PROMISE_TO_PAY
    local_p = res.promised_date.astimezone(ZoneInfo("Asia/Kolkata"))
    # Friday, Aug 28, 2026
    assert local_p.day == 28
    assert "Friday" in res.promised_date_display


def test_extract_next_monday(ref_wednesday):
    res = VoiceIntentExtractor.extract_promise_intent(
        speech_text="I will pay next Monday",
        reference_datetime=ref_wednesday,
    )
    assert res.intent == VoiceCustomerIntent.PROMISE_TO_PAY
    local_p = res.promised_date.astimezone(ZoneInfo("Asia/Kolkata"))
    # Next Monday from Wednesday Aug 26 is Monday Aug 31
    assert local_p.day == 31
    assert local_p.month == 8
    assert "Monday" in res.promised_date_display


def test_extract_in_three_days(ref_wednesday):
    res = VoiceIntentExtractor.extract_promise_intent(
        speech_text="I can make the payment in 3 days",
        reference_datetime=ref_wednesday,
    )
    assert res.intent == VoiceCustomerIntent.PROMISE_TO_PAY
    local_p = res.promised_date.astimezone(ZoneInfo("Asia/Kolkata"))
    # Aug 26 + 3 days = Aug 29 (Saturday)
    assert local_p.day == 29
    assert "Saturday" in res.promised_date_display


def test_extract_explicit_date(ref_wednesday):
    res = VoiceIntentExtractor.extract_promise_intent(
        speech_text="I will clear it on 30th August",
        reference_datetime=ref_wednesday,
    )
    assert res.intent == VoiceCustomerIntent.PROMISE_TO_PAY
    local_p = res.promised_date.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local_p.day == 30
    assert local_p.month == 8


def test_refusal_and_negative_intents():
    res1 = VoiceIntentExtractor.extract_promise_intent("I already paid yesterday through UPI")
    assert res1.intent == VoiceCustomerIntent.ALREADY_PAID

    res2 = VoiceIntentExtractor.extract_promise_intent("I cannot pay right now I have no money")
    assert res2.intent == VoiceCustomerIntent.REFUSAL_TO_PAY

    res3 = VoiceIntentExtractor.extract_promise_intent("This is the wrong number who are you")
    assert res3.intent == VoiceCustomerIntent.WRONG_NUMBER

    res4 = VoiceIntentExtractor.extract_promise_intent("I dispute this charge it was fraud")
    assert res4.intent == VoiceCustomerIntent.DISPUTE


def test_empty_or_unclear_speech():
    res1 = VoiceIntentExtractor.extract_promise_intent("")
    assert res1.intent == VoiceCustomerIntent.UNKNOWN

    res2 = VoiceIntentExtractor.extract_promise_intent("hello yes yes")
    assert res2.intent == VoiceCustomerIntent.UNKNOWN
