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
- medicine_name (string)
- dosage_times (array of strings: "morning", "evening", "night", or "HH:MM")
- total_quantity (number or null)

Example: [{"medicine_name": "Crocin", "dosage_times": ["morning", "night"], "total_quantity": 20}]

Return ONLY the JSON array, no explanation."""


def _extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text.strip())
    return parsed if isinstance(parsed, list) else parsed.get("medications", [])


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
        medications = _extract_json_array(raw)
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
