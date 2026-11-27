"""
FILE: saas_app/models.py
RUOLO: Modelli ORM relazionali, time-series, revoca token e Audit Monitor.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saas_app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class User(Base):
    """Account autenticati (IAM): solo identità di accesso, nessuna PII clinica."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    provision_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    patient_profile: Mapped[Optional["Patient"]] = relationship(
        "Patient",
        back_populates="user",
        foreign_keys="Patient.user_id",
        uselist=False,
    )


class DoctorProfile(Base):
    """Profilo anagrafico minimo dei diabetologi nella Application Zone clinica."""

    __tablename__ = "doctor_profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)


class Patient(Base):
    """Profilo clinico e anagrafico del paziente, dieta prescritta e medico assegnato."""

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    patient_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    diet_carb_limit_g: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    target_min_mmol: Mapped[float] = mapped_column(Float, default=3.9, nullable=False)
    target_max_mmol: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped["User"] = relationship("User", back_populates="patient_profile", foreign_keys=[user_id])
    doctor: Mapped["User"] = relationship("User", foreign_keys=[doctor_id])


class Device(Base):
    """Dispositivo CGM associato a un paziente con chiave hashed."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])


class CgmReading(Base):
    """Serie temporale idempotente delle misurazioni CGM ogni 5 minuti (RF04, RNF04)."""

    __tablename__ = "cgm_readings"
    __table_args__ = (
        UniqueConstraint("patient_id", "recorded_at", name="uq_cgm_patient_time"),
        Index("ix_cgm_patient_recorded", "patient_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    glucose_mmol: Mapped[float] = mapped_column(Float, nullable=False)
    insulin_u: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cho_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class MealPrediction(Base):
    """Dichiarazione pasto del paziente, predizione a +30 min e flag clinico (RF03-RF07)."""

    __tablename__ = "meals_predictions"
    __table_args__ = (
        Index("ix_meals_patient_declared", "patient_id", "declared_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False)
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    carbs_g: Mapped[float] = mapped_column(Float, nullable=False)
    insulin_u: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_glucose_mmol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forecasted_glucose_mmol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual_glucose_30m_mmol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # risk_class è nullo quando l'esito è NO_RECENT_DATA: non esiste una classe di rischio
    # calcolabile senza dati del sensore (N5), quindi non si inventa un valore 0.
    risk_class: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    outcome_code: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome_message: Mapped[str] = mapped_column(Text, nullable=False)
    doctor_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), default="1.0.0", nullable=False)


class Intervention(Base):
    """Interventi clinici registrati dal diabetologo (RF14, RF15)."""

    __tablename__ = "interventions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True, nullable=False)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    intervention_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cause: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    new_diet_carb_limit_g: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class RevokedToken(Base):
    """Blacklist JTI per revoca server-side al logout."""

    __tablename__ = "revoked_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    """Audit Monitor append-only scritto atomicamente nella stessa transazione clinica."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_patient_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
