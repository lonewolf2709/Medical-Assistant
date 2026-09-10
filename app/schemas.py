from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


# --- Intent schemas ---

class IntentType(str, Enum):
    CONVERSATION="CONVERSATION"
    ADD_MEDICATION = "ADD_MEDICATION"
    SET_REMINDER = "SET_REMINDER"
    REFILL = "REFILL"
    QNA = "QNA"
    LIST_MEDICATIONS = "LIST_MEDICATIONS"
    STOP_MEDICATION = "STOP_MEDICATION"
    PAUSE_MEDICATION = "PAUSE_MEDICATION"
    RESUME_MEDICATION = "RESUME_MEDICATION"
    ADHERENCE = "ADHERENCE"
    ADD_TO_CART = "ADD_TO_CART"
    VIEW_CART = "VIEW_CART"
    REMOVE_FROM_CART = "REMOVE_FROM_CART"
    CHECKOUT = "CHECKOUT"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    SET_TIMEZONE = "SET_TIMEZONE"
    UNKNOWN = "UNKNOWN"


class AddMedicationIntent(BaseModel):
    intent: IntentType = IntentType.ADD_MEDICATION
    medicine_name: str
    dosage_times: list[str]  # e.g. ["morning", "night"] or ["17:30"]
    total_quantity: float | None = None
    duration_days: int | None = None  # finite course; None means ongoing


class SetReminderIntent(BaseModel):
    intent: IntentType = IntentType.SET_REMINDER
    medicine_name: str
    times: list[str]

class ConvesationIntent(BaseModel):
    intent: IntentType = IntentType.CONVERSATION
    message: str
class RefillIntent(BaseModel):
    intent: IntentType = IntentType.REFILL
    medicine_name: str
    quantity: float
    unit: str  # "tablet" | "strip"


class QnaIntent(BaseModel):
    intent: IntentType = IntentType.QNA
    question: str


class SetTimezoneIntent(BaseModel):
    intent: IntentType = IntentType.SET_TIMEZONE
    timezone: str  # IANA name, e.g. "Asia/Karachi"


class UnknownIntent(BaseModel):
    intent: IntentType = IntentType.UNKNOWN
    raw_text: str


class ListMedicationsIntent(BaseModel):
    intent: IntentType = IntentType.LIST_MEDICATIONS


class StopMedicationIntent(BaseModel):
    intent: IntentType = IntentType.STOP_MEDICATION
    medicine_name: str


class PauseMedicationIntent(BaseModel):
    intent: IntentType = IntentType.PAUSE_MEDICATION
    medicine_name: str
    days: int | None = None  # None means indefinite pause


class ResumeMedicationIntent(BaseModel):
    intent: IntentType = IntentType.RESUME_MEDICATION
    medicine_name: str


class AdherenceIntent(BaseModel):
    intent: IntentType = IntentType.ADHERENCE


class AddToCartIntent(BaseModel):
    intent: IntentType = IntentType.ADD_TO_CART
    medicine_name: str
    quantity: int = 1


class ViewCartIntent(BaseModel):
    intent: IntentType = IntentType.VIEW_CART


class RemoveFromCartIntent(BaseModel):
    intent: IntentType = IntentType.REMOVE_FROM_CART
    medicine_name: str


class CheckoutIntent(BaseModel):
    intent: IntentType = IntentType.CHECKOUT


class OrderConfirmedIntent(BaseModel):
    intent: IntentType = IntentType.ORDER_CONFIRMED
    medicine_name: str
    quantity: int = 1


ParsedIntent = (AddMedicationIntent | SetReminderIntent | RefillIntent | QnaIntent |
                ListMedicationsIntent | ConvesationIntent | StopMedicationIntent |
                PauseMedicationIntent | ResumeMedicationIntent | AdherenceIntent |
                AddToCartIntent | ViewCartIntent | RemoveFromCartIntent |
                CheckoutIntent | OrderConfirmedIntent | SetTimezoneIntent |
                UnknownIntent)


# --- Telegram webhook schemas ---

class TelegramUser(BaseModel):
    id: int
    first_name: str | None = None
    username: str | None = None


class TelegramChat(BaseModel):
    id: int


class TelegramVoice(BaseModel):
    file_id: str
    duration: int
    mime_type: str | None = None
    file_size: int | None = None


class TelegramMessage(BaseModel):
    message_id: int
    from_: TelegramUser | None = Field(None, alias="from")
    chat: TelegramChat
    text: str | None = None
    photo: list[Any] | None = None
    voice: TelegramVoice | None = None

    model_config = {"populate_by_name": True}


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None


# --- API response schemas ---

class HealthResponse(BaseModel):
    status: str = "ok"
