import uuid
from datetime import datetime, time

from sqlalchemy import (
    UUID,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # IANA name, e.g. "Asia/Kolkata". NULL falls back to the USER_TIMEZONE setting.
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    medications: Mapped[list["Medication"]] = relationship(back_populates="user")
    reminder_events: Mapped[list["ReminderEvent"]] = relationship(back_populates="user")
    prescriptions: Mapped[list["Prescription"]] = relationship(back_populates="user")
    messages: Mapped[list["Message"]] = relationship(back_populates="user")


class Medication(Base):
    __tablename__ = "medications"
    __table_args__ = (
        Index(
            "uq_medications_user_lower_name",
            "user_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    total_quantity: Mapped[float] = mapped_column(nullable=False, default=0)
    remaining_quantity: Mapped[float] = mapped_column(nullable=False, default=0)
    daily_dose: Mapped[float] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")  # active | paused | stopped | completed
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # End of a finite course ("for 5 days"). NULL means ongoing, e.g. a vitamin.
    course_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="medications")
    dosage_times: Mapped[list["DosageTime"]] = relationship(back_populates="medication", cascade="all, delete-orphan")
    reminder_events: Mapped[list["ReminderEvent"]] = relationship(back_populates="medication")


class DosageTime(Base):
    __tablename__ = "dosage_times"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    medication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medications.id"), nullable=False)
    type: Mapped[str] = mapped_column(Enum("slot", "custom", name="dosage_type"), nullable=False)
    slot: Mapped[str | None] = mapped_column(Enum("morning", "evening", "night", name="slot_type"), nullable=True)
    custom_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    medication: Mapped["Medication"] = relationship(back_populates="dosage_times")


class ReminderEvent(Base):
    __tablename__ = "reminder_events"
    __table_args__ = (
        Index("ix_reminder_events_status_trigger_time", "status", "trigger_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    medication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medications.id"), nullable=False)
    trigger_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(Enum("dose", "refill", name="reminder_type"), nullable=False)
    status: Mapped[str] = mapped_column(Enum("pending", "sent", name="reminder_status"), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="reminder_events")
    medication: Mapped["Medication"] = relationship(back_populates="reminder_events")


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    image_url: Mapped[str] = mapped_column(String, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="prescriptions")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_action: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: pending follow-up context
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="messages")


class DoseLog(Base):
    __tablename__ = "dose_logs"
    __table_args__ = (
        UniqueConstraint("reminder_event_id", name="uq_dose_logs_reminder_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    medication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medications.id"), nullable=False)
    reminder_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reminder_events.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # "taken" | "skipped"
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    medication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medications.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")  # active | ordered | removed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
