"""OCR + LLM prescription extraction service using Gemini."""
import json
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Prescription, User
from app.services import llm

EXTRACT_PROMPT = """You are a prescription parser. Look at this prescription image and extract all medications.
Return ONLY a valid JSON array of objects with these fields:
- medicine_name (string: the brand or generic name, without the strength)
- strength (string or null: e.g. "500mg", "10ml")
- dosage_times (array of strings: "morning", "evening", "night", or "HH:MM")
  "1-0-1" means morning and night; "1-1-1" means morning, evening and night; "0-0-1" means night
- dose_quantity (number or null: units taken AT EACH dose — "2 tablets twice daily" is 2)
- duration_days (integer or null: length of the course — "x 5 days" or "for 1 week" (7); null if not stated)
- total_quantity (number or null: the whole pack, if written)

Example: [{"medicine_name": "Azithromycin", "strength": "500mg", "dosage_times": ["morning", "night"], "dose_quantity": 1, "duration_days": 5, "total_quantity": 10}]

Return ONLY the JSON array, no explanation."""


def _extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text.strip())
    return parsed if isinstance(parsed, list) else parsed.get("medications", [])


def _number(value, cast):
    """Coerce a model-provided value to a number, or None if it is not one."""
    if value in (None, "", "null"):
        return None
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return None


def normalise_extracted(medications: list) -> list[dict]:
    """Coerce the model's output into the shape the confirm flow relies on.

    Gemini returns numbers as strings now and then, omits fields, and sometimes
    repeats the strength inside the name. Everything downstream — dose size,
    course length, refill projection — depends on these being right, so fix
    them here rather than in three places.
    """
    normalised: list[dict] = []
    for item in medications:
        if not isinstance(item, dict):
            continue
        name = str(item.get("medicine_name") or "").strip()
        if not name:
            continue
        strength = str(item.get("strength") or "").strip()
        if strength and strength.lower() not in name.lower():
            name = f"{name} {strength}"
        times = item.get("dosage_times") or []
        normalised.append({
            "medicine_name": name,
            "dosage_times": [str(t) for t in times if t],
            "total_quantity": _number(item.get("total_quantity"), float),
            "dose_quantity": _number(item.get("dose_quantity"), float),
            "duration_days": _number(item.get("duration_days"), int),
        })
    return normalised


def format_preview(medications: list[dict]) -> str:
    """The list the user confirms against — it must show everything we will store."""
    from app.telegram_format import esc

    lines = ["Found these medications in your prescription. Reply 'confirm' to add them all:\n"]
    for m in medications:
        times = ", ".join(m.get("dosage_times") or []) or "no times found"
        detail = f"• <b>{esc(m['medicine_name'])}</b> — {esc(times)}"
        qty = m.get("dose_quantity")
        if qty and qty > 1:
            detail += f", {qty:g} per dose"
        days = m.get("duration_days")
        if days:
            detail += f", for {days} day(s)"
        total = m.get("total_quantity")
        if total:
            detail += f" ({total:g} in total)"
        lines.append(detail)
    return "\n".join(lines)


async def download_and_store(db: AsyncSession, user: User, file_url: str) -> Prescription:
    prescription = Prescription(user_id=user.id, image_url=file_url)
    db.add(prescription)
    await db.commit()
    await db.refresh(prescription)
    return prescription


async def extract_medications(db: AsyncSession, prescription: Prescription) -> list[dict]:
    """Download image, send to Gemini vision, extract medications."""
    async with httpx.AsyncClient() as http:
        resp = await http.get(prescription.image_url, timeout=30)
        resp.raise_for_status()
        image_bytes = resp.content

    raw = await llm.generate_from_media(
        data=image_bytes, mime_type="image/jpeg", prompt=EXTRACT_PROMPT
    )

    try:
        medications = normalise_extracted(_extract_json_array(raw))
    except Exception:
        medications = []

    # Store extracted medications as JSON in extracted_text for confirmation flow
    prescription.extracted_text = json.dumps(medications)
    await db.commit()

    return medications


async def get_pending_prescription(db: AsyncSession, user_id) -> list[dict] | None:
    """Get the most recent unconfirmed prescription's extracted medications."""
    result = await db.execute(
        select(Prescription)
        .where(Prescription.user_id == user_id, Prescription.extracted_text.isnot(None))
        .order_by(Prescription.created_at.desc())
        .limit(1)
    )
    prescription = result.scalar_one_or_none()
    if not prescription or not prescription.extracted_text:
        return None
    try:
        meds = json.loads(prescription.extracted_text)
        return meds if meds else None
    except Exception:
        return None
