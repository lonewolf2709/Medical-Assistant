"""Debug-only endpoints for testing bot logic without Telegram.

Mounted only when DEBUG is on: these accept an arbitrary telegram_id and so can
act as any user.
"""
from pydantic import BaseModel
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.parser import parse_intent, format_intent
from app.services.qa_service import answer_question
from app.services.user_service import get_or_create_user
from app.services.medication_service import add_or_update_medication, apply_refill, get_medication
from app.services.reminder_service import regenerate_dose_events, regenerate_refill_event
from app.routers.webhook import _route_intent

router = APIRouter(prefix="/dev", tags=["dev"])


class ParseRequest(BaseModel):
    text: str


class ChatRequest(BaseModel):
    telegram_id: str
    text: str


class QARequest(BaseModel):
    question: str
    telegram_id: str


class MedicationRequest(BaseModel):
    telegram_id: str
    medicine_name: str
    dosage_times: list[str]
    total_quantity: float | None = None


class RefillRequest(BaseModel):
    telegram_id: str
    medicine_name: str
    quantity: float
    unit: str  # "tablet" or "strip"


@router.post("/parse")
async def parse(req: ParseRequest):
    """Parse raw text into structured intents."""
    intents = await parse_intent(req.text)
    return {
        "intents": [i.model_dump() for i in intents],
        "formatted": [format_intent(i) for i in intents],
    }


@router.post("/chat")
async def chat(req: ChatRequest, db: AsyncSession = Depends(get_db)):
    """Send a message as a user and get the bot reply — full pipeline without Telegram."""
    user = await get_or_create_user(db, req.telegram_id)
    intents = await parse_intent(req.text)
    replies = [
        await _route_intent(db, user, intent, req.telegram_id) for intent in intents
    ]
    return {
        "intents": [i.model_dump() for i in intents],
        "reply": "\n\n".join(r for r in replies if r),
    }


@router.post("/qa")
async def qa(req: QARequest):
    """Ask a medicine question directly."""
    answer = await answer_question(req.question, chat_id=req.telegram_id)
    return {"answer": answer}


@router.post("/medication/add")
async def add_medication(req: MedicationRequest, db: AsyncSession = Depends(get_db)):
    """Add or update a medication for a user."""
    user = await get_or_create_user(db, req.telegram_id)
    med = await add_or_update_medication(db, user.id, req.medicine_name, req.dosage_times, req.total_quantity)
    await regenerate_dose_events(db, med)
    await regenerate_refill_event(db, med)
    return {"medication": {"id": str(med.id), "name": med.name, "remaining_quantity": med.remaining_quantity}}


@router.post("/medication/refill")
async def refill(req: RefillRequest, db: AsyncSession = Depends(get_db)):
    """Record a refill for a medication."""
    user = await get_or_create_user(db, req.telegram_id)
    med = await apply_refill(db, user.id, req.medicine_name, req.quantity, req.unit)
    if med is None:
        return {"error": f"Medication '{req.medicine_name}' not found for user {req.telegram_id}"}
    await regenerate_refill_event(db, med)
    return {"medication": {"id": str(med.id), "name": med.name, "remaining_quantity": med.remaining_quantity}}
