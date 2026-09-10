import json
import logging
import re

from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from app.services import llm
from app.schemas import (
    AddMedicationIntent, AdherenceIntent, AddToCartIntent, ViewCartIntent,
    RemoveFromCartIntent, CheckoutIntent, OrderConfirmedIntent,
    ConvesationIntent, IntentType, ListMedicationsIntent, ParsedIntent,
    QnaIntent, RefillIntent, SetReminderIntent, SetTimezoneIntent, StopMedicationIntent,
    PauseMedicationIntent, ResumeMedicationIntent, UnknownIntent,
)

logger = logging.getLogger(__name__)

_gemini_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

SYSTEM_PROMPT = """You are a medication assistant parser. Extract ALL intents from the user's message and return ONLY a valid JSON array.

A single message may contain multiple intents. Extract all of them.

Supported intents:
- CONVERSATIONAL:{"intent":"CONVERSATION", "message":str}
- ADD_MEDICATION: { "intent": "ADD_MEDICATION", "medicine_name": str, "dosage_times": [str], "total_quantity": float|null, "duration_days": int|null }
  dosage_times values: "morning", "evening", "night", or "HH:MM" for custom times
  duration_days: number of days for a finite course, null if ongoing
  e.g. "Take azithromycin for 5 days" → duration_days: 5
  e.g. "Antibiotic twice a day for a week" → duration_days: 7
  e.g. "Take vitamin D every morning" → duration_days: null
- SET_REMINDER: { "intent": "SET_REMINDER", "medicine_name": str, "times": [str] }
- REFILL: { "intent": "REFILL", "medicine_name": str, "quantity": float, "unit": str }
  unit values: "tablet" or "strip"
- QNA: { "intent": "QNA", "question": str }
  use for medicine questions AND symptom statements
  e.g. "what are side effects of paracetamol" → QNA
  e.g. "I am feeling feverish" → QNA with question: "I am feeling feverish"
  e.g. "I have a headache" → QNA with question: "I have a headache"
  e.g. "my stomach hurts" → QNA with question: "my stomach hurts"
  Any health complaint or symptom description should be QNA
- LIST_MEDICATIONS: { "intent": "LIST_MEDICATIONS" }
  use when user asks to see/list/show their medications or medicines
- STOP_MEDICATION: { "intent": "STOP_MEDICATION", "medicine_name": str }
  use when user wants to permanently stop/delete/remove a medication
  e.g. "Stop Paracetamol", "Remove Crocin", "Delete Dolo"
- PAUSE_MEDICATION: { "intent": "PAUSE_MEDICATION", "medicine_name": str, "days": int|null }
  use when user wants to temporarily pause a medication
  e.g. "Pause Crocin for 3 days" → days: 3
  e.g. "Pause Vitamin D" → days: null (indefinite)
- RESUME_MEDICATION: { "intent": "RESUME_MEDICATION", "medicine_name": str }
  use when user wants to resume a paused medication
  e.g. "Resume Crocin", "Start taking Vitamin D again"
- ADHERENCE: { "intent": "ADHERENCE" }
  use when user asks about their medication adherence, compliance, or how they're doing
  e.g. "How am I doing?", "Show my adherence", "Did I take my meds?"
- ADD_TO_CART: { "intent": "ADD_TO_CART", "medicine_name": str, "quantity": int }
  e.g. "Add Crocin to cart", "Put Paracetamol in cart", "I need to order Vitamin D"
- VIEW_CART: { "intent": "VIEW_CART" }
  e.g. "Show my cart", "What do I need to order?", "List cart items"
- REMOVE_FROM_CART: { "intent": "REMOVE_FROM_CART", "medicine_name": str }
  e.g. "Remove Crocin from cart", "Delete Paracetamol from cart"
- CHECKOUT: { "intent": "CHECKOUT" }
  e.g. "Checkout", "Order these items", "Buy everything in cart", "Show buy links"
- ORDER_CONFIRMED: { "intent": "ORDER_CONFIRMED", "medicine_name": str, "quantity": int }
  e.g. "Ordered 2 strips Crocin", "I bought Paracetamol", "Placed order for Vitamin D"
- CONVERSATION: { "intent": "CONVERSATION", "message": str }
  use for greetings, small talk, how are you, thanks, etc
- SET_TIMEZONE: { "intent": "SET_TIMEZONE", "timezone": str }
  use when the user states where they are or which timezone they want
  timezone must be an IANA name
  e.g. "I'm in Karachi" → timezone: "Asia/Karachi"
  e.g. "set my timezone to IST" → timezone: "Asia/Kolkata"
  e.g. "I moved to London" → timezone: "Europe/London"
- UNKNOWN: { "intent": "UNKNOWN", "raw_text": str }
  ONLY use UNKNOWN if the message has absolutely no recognisable intent. Never mix UNKNOWN with other intents in the same array.

Examples:
- "Take Crocin morning and night" → [{"intent": "ADD_MEDICATION", "medicine_name": "Crocin", "dosage_times": ["morning", "night"], "total_quantity": null, "duration_days": null}]
- "Azithromycin 500mg once daily for 5 days" → [{"intent": "ADD_MEDICATION", "medicine_name": "Azithromycin 500mg", "dosage_times": ["morning"], "total_quantity": 5, "duration_days": 5}]
- "Add Crocin 30 tablets and set reminder to 4pm" → [{"intent": "ADD_MEDICATION", "medicine_name": "Crocin", "dosage_times": ["16:00"], "total_quantity": 30}, {"intent": "SET_REMINDER", "medicine_name": "Crocin", "times": ["16:00"]}]

Return ONLY the JSON array, no explanation, no markdown."""


def _extract_json(text: str) -> list | dict:
    """Strip markdown code fences if present and parse JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def _parse_single(data: dict, original_text: str) -> ParsedIntent:
    intent_type = data.get("intent", "UNKNOWN")
    match intent_type:
        case IntentType.CONVERSATION:
            return ConvesationIntent(**data)
        case IntentType.ADD_MEDICATION:
            return AddMedicationIntent(**data)
        case IntentType.SET_REMINDER:
            return SetReminderIntent(**data)
        case IntentType.REFILL:
            return RefillIntent(**data)
        case IntentType.QNA:
            return QnaIntent(**data)
        case IntentType.LIST_MEDICATIONS:
            return ListMedicationsIntent()
        case IntentType.STOP_MEDICATION:
            return StopMedicationIntent(**data)
        case IntentType.PAUSE_MEDICATION:
            return PauseMedicationIntent(**data)
        case IntentType.RESUME_MEDICATION:
            return ResumeMedicationIntent(**data)
        case IntentType.ADHERENCE:
            return AdherenceIntent()
        case IntentType.ADD_TO_CART:
            return AddToCartIntent(**data)
        case IntentType.VIEW_CART:
            return ViewCartIntent()
        case IntentType.REMOVE_FROM_CART:
            return RemoveFromCartIntent(**data)
        case IntentType.CHECKOUT:
            return CheckoutIntent()
        case IntentType.ORDER_CONFIRMED:
            return OrderConfirmedIntent(**data)
        case IntentType.SET_TIMEZONE:
            return SetTimezoneIntent(**data)
        case _:
            return UnknownIntent(raw_text=original_text)


async def parse_intent(text: str) -> list[ParsedIntent]:
    if not text or not text.strip():
        return [UnknownIntent(raw_text=text)]
    try:
        @_gemini_retry
        async def _call() -> str:
            return await llm.generate_text(f"{SYSTEM_PROMPT}\n\nUser message: {text}")

        parsed = _extract_json(await _call())

        # Normalise: always work with a list
        if isinstance(parsed, dict):
            parsed = [parsed]

        results = [_parse_single(item, text) for item in parsed]
        # If we have valid intents alongside UNKNOWN, drop the UNKNOWN ones
        non_unknown = [r for r in results if not isinstance(r, UnknownIntent)]
        return non_unknown if non_unknown else results or [UnknownIntent(raw_text=text)]
    except Exception as e:
        logger.info(e)
        return [UnknownIntent(raw_text=text)]


def format_intent(intent: ParsedIntent) -> str:
    match intent:
        case AddMedicationIntent():
            times = ", ".join(intent.dosage_times)
            qty = f", quantity: {intent.total_quantity}" if intent.total_quantity else ""
            return f"Add medication: {intent.medicine_name} at {times}{qty}"
        case SetReminderIntent():
            return f"Set reminder for {intent.medicine_name} at {', '.join(intent.times)}"
        case RefillIntent():
            return f"Refill {intent.medicine_name}: {intent.quantity} {intent.unit}(s)"
        case QnaIntent():
            return f"Question: {intent.question}"
        case ListMedicationsIntent():
            return "List medications"
        case UnknownIntent():
            return f"Unknown: {intent.raw_text}"
        case _:
            return str(intent)
