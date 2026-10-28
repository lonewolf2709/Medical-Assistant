"""A photographed prescription must capture the same detail as a typed one."""
import json
from datetime import datetime, timezone

import pytest

from app.models import Prescription
from app.schemas import TelegramMessage, TelegramUpdate, TelegramUser
from app.services import medication_service, prescription_service


# ── pure normalisation of what Gemini returns ────────────────────────────────

def test_strength_is_folded_into_the_medicine_name():
    out = prescription_service.normalise_extracted([
        {"medicine_name": "Azithromycin", "strength": "500mg", "dosage_times": ["morning"]}
    ])
    assert out[0]["medicine_name"] == "Azithromycin 500mg"


def test_strength_is_not_duplicated_when_already_in_the_name():
    out = prescription_service.normalise_extracted([
        {"medicine_name": "Azithromycin 500mg", "strength": "500mg", "dosage_times": ["morning"]}
    ])
    assert out[0]["medicine_name"] == "Azithromycin 500mg"


def test_dose_quantity_and_duration_are_coerced_to_numbers():
    out = prescription_service.normalise_extracted([
        {"medicine_name": "Crocin", "dosage_times": ["morning"],
         "dose_quantity": "2", "duration_days": "5", "total_quantity": "10"}
    ])
    assert out[0]["dose_quantity"] == 2.0
    assert out[0]["duration_days"] == 5
    assert out[0]["total_quantity"] == 10.0


def test_missing_fields_become_none_not_errors():
    out = prescription_service.normalise_extracted([{"medicine_name": "Crocin"}])
    assert out[0] == {
        "medicine_name": "Crocin", "dosage_times": [], "total_quantity": None,
        "dose_quantity": None, "duration_days": None,
    }


def test_entries_without_a_name_are_dropped():
    out = prescription_service.normalise_extracted([{"dosage_times": ["morning"]}, {"medicine_name": ""}])
    assert out == []


# ── extraction stores the richer shape ──────────────────────────────────────

async def test_extract_medications_keeps_dose_and_duration(db, user, monkeypatch):
    class FakeResp:
        content = b"jpegbytes"
        def raise_for_status(self): pass

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, timeout=None): return FakeResp()

    monkeypatch.setattr(prescription_service.httpx, "AsyncClient", FakeClient)

    async def fake_vision(*, data, mime_type, prompt):
        assert data == b"jpegbytes"
        return json.dumps([{
            "medicine_name": "Azithromycin", "strength": "500mg",
            "dosage_times": ["morning", "night"], "total_quantity": 10,
            "dose_quantity": 1, "duration_days": 5,
        }])
    monkeypatch.setattr(prescription_service.llm, "generate_from_media", fake_vision)

    prescription = await prescription_service.download_and_store(db, user, "https://x/img.jpg")
    meds = await prescription_service.extract_medications(db, prescription)

    assert meds[0]["medicine_name"] == "Azithromycin 500mg"
    assert meds[0]["duration_days"] == 5
    stored = json.loads(prescription.extracted_text)
    assert stored[0]["dose_quantity"] == 1.0


# ── the preview the user confirms against ───────────────────────────────────

def test_preview_shows_dose_quantity_and_course_length():
    text = prescription_service.format_preview([
        {"medicine_name": "Azithromycin 500mg", "dosage_times": ["morning", "night"],
         "total_quantity": 10, "dose_quantity": 2, "duration_days": 5},
    ])
    assert "Azithromycin 500mg" in text
    assert "2" in text and "5 day" in text, text


def test_preview_escapes_html_in_names():
    text = prescription_service.format_preview([
        {"medicine_name": "Vitamin B<3", "dosage_times": ["morning"],
         "total_quantity": None, "dose_quantity": None, "duration_days": None},
    ])
    assert "B&lt;3" in text and "B<3" not in text


# ── confirming creates the same medication a typed message would ─────────────

def _confirm_update():
    return TelegramUpdate(update_id=1, message=TelegramMessage(
        message_id=1, chat={"id": 42}, text="confirm", **{"from": TelegramUser(id=42)}))


async def test_confirming_creates_a_finite_course_with_the_right_dose(db, user, sent):
    from app.routers.webhook import _process_message_inner

    db.add(Prescription(user_id=user.id, image_url="https://x/img.jpg", extracted_text=json.dumps([{
        "medicine_name": "Azithromycin 500mg", "dosage_times": ["morning", "night"],
        "total_quantity": 20, "dose_quantity": 2, "duration_days": 5,
    }])))
    await db.commit()

    await _process_message_inner(_confirm_update())

    med = await medication_service.get_medication(db, user.id, "Azithromycin 500mg")
    assert med is not None
    assert med.daily_dose == 4.0, "2 units × 2 times a day"
    assert med.course_end is not None and med.course_end > datetime.now(timezone.utc)


async def test_confirming_a_minimal_prescription_still_works(db, user, sent):
    from app.routers.webhook import _process_message_inner

    db.add(Prescription(user_id=user.id, image_url="https://x/img.jpg",
                        extracted_text=json.dumps([{"medicine_name": "Crocin", "dosage_times": ["morning"]}])))
    await db.commit()

    await _process_message_inner(_confirm_update())

    med = await medication_service.get_medication(db, user.id, "Crocin")
    assert med is not None and med.daily_dose == 1.0 and med.course_end is None
