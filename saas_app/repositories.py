"""
FILE: saas_app/repositories.py
RUOLO: Layer di accesso ai dati (Repository pattern) per isolare le query SQLAlchemy dai router.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from saas_app.config import saas_settings
from saas_app.models import (
    CgmReading,
    Device,
    DoctorProfile,
    Intervention,
    MealPrediction,
    Patient,
    RevokedToken,
    User,
    ensure_utc,
)


def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.execute(select(User).where(User.username == username)).scalar_one_or_none()


def get_patient_code_by_user_id(db: Session, user_id: int) -> Optional[str]:
    return db.execute(select(Patient.patient_code).where(Patient.user_id == user_id)).scalar_one_or_none()


def get_doctor_profile(db: Session, user_id: int) -> Optional[DoctorProfile]:
    return db.execute(select(DoctorProfile).where(DoctorProfile.user_id == user_id)).scalar_one_or_none()


def get_patient_by_user_id(db: Session, user_id: int) -> Optional[Patient]:
    return db.execute(select(Patient).where(Patient.user_id == user_id)).scalar_one_or_none()


def get_patient_by_id(db: Session, patient_id: int) -> Optional[Patient]:
    return db.execute(select(Patient).where(Patient.id == patient_id)).scalar_one_or_none()


def get_patient_by_code(db: Session, patient_code: str) -> Optional[Patient]:
    return db.execute(select(Patient).where(Patient.patient_code == patient_code)).scalar_one_or_none()


def get_latest_cgm_reading(db: Session, patient_id: int) -> Optional[CgmReading]:
    return db.execute(
        select(CgmReading)
        .where(CgmReading.patient_id == patient_id)
        .order_by(desc(CgmReading.recorded_at))
        .limit(1)
    ).scalar_one_or_none()


def get_recent_cgm_readings(db: Session, patient_id: int, limit: int = 13) -> List[CgmReading]:
    rows = db.execute(
        select(CgmReading)
        .where(CgmReading.patient_id == patient_id)
        .order_by(desc(CgmReading.recorded_at))
        .limit(limit)
    ).scalars().all()
    return list(reversed(rows))


def get_cgm_readings_since(db: Session, patient_id: int, cutoff: datetime) -> Sequence[CgmReading]:
    return db.execute(
        select(CgmReading)
        .where(CgmReading.patient_id == patient_id, CgmReading.recorded_at >= cutoff)
        .order_by(CgmReading.recorded_at.asc())
    ).scalars().all()


def get_meals_since(db: Session, patient_id: int, cutoff: datetime) -> Sequence[MealPrediction]:
    return db.execute(
        select(MealPrediction)
        .where(MealPrediction.patient_id == patient_id, MealPrediction.declared_at >= cutoff)
        .order_by(desc(MealPrediction.declared_at))
    ).scalars().all()


def count_patient_meals_and_flags(db: Session, patient_id: int) -> Tuple[int, int]:
    total = db.execute(
        select(func.count(MealPrediction.id)).where(MealPrediction.patient_id == patient_id)
    ).scalar_one()
    flagged = db.execute(
        select(func.count(MealPrediction.id)).where(
            MealPrediction.patient_id == patient_id,
            MealPrediction.doctor_flag.is_(True),
        )
    ).scalar_one()
    return int(total or 0), int(flagged or 0)


def list_patients_for_doctor(db: Session, doctor_id: int) -> Sequence[Patient]:
    return db.execute(
        select(Patient).where(Patient.doctor_id == doctor_id).order_by(Patient.patient_code.asc())
    ).scalars().all()


def get_latest_cgm_readings_for_patients(db: Session, patient_ids: Sequence[int]) -> Dict[int, CgmReading]:
    """Ultima lettura CGM di più pazienti in una sola query (max recorded_at per paziente)."""
    ids = list(patient_ids)
    if not ids:
        return {}
    latest = (
        select(CgmReading.patient_id.label("patient_id"), func.max(CgmReading.recorded_at).label("max_ts"))
        .where(CgmReading.patient_id.in_(ids))
        .group_by(CgmReading.patient_id)
        .subquery()
    )
    rows = db.execute(
        select(CgmReading).join(
            latest,
            (CgmReading.patient_id == latest.c.patient_id) & (CgmReading.recorded_at == latest.c.max_ts),
        )
    ).scalars().all()
    return {r.patient_id: r for r in rows}


def get_latest_meals_for_patients(db: Session, patient_ids: Sequence[int]) -> Dict[int, MealPrediction]:
    """Ultimo pasto di più pazienti in una sola query (max declared_at per paziente)."""
    ids = list(patient_ids)
    if not ids:
        return {}
    latest = (
        select(MealPrediction.patient_id.label("patient_id"), func.max(MealPrediction.declared_at).label("max_ts"))
        .where(MealPrediction.patient_id.in_(ids))
        .group_by(MealPrediction.patient_id)
        .subquery()
    )
    rows = db.execute(
        select(MealPrediction).join(
            latest,
            (MealPrediction.patient_id == latest.c.patient_id) & (MealPrediction.declared_at == latest.c.max_ts),
        )
    ).scalars().all()
    return {m.patient_id: m for m in rows}


def count_flags_by_patient(db: Session, patient_ids: Sequence[int]) -> Dict[int, int]:
    """Numero di pasti segnalati dal medico, per paziente, in una sola query."""
    ids = list(patient_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(MealPrediction.patient_id, func.count(MealPrediction.id))
        .where(MealPrediction.patient_id.in_(ids), MealPrediction.doctor_flag.is_(True))
        .group_by(MealPrediction.patient_id)
    ).all()
    return {int(pid): int(cnt) for pid, cnt in rows}


def list_interventions_for_patient(db: Session, patient_id: int) -> Sequence[Intervention]:
    return db.execute(
        select(Intervention)
        .where(Intervention.patient_id == patient_id)
        .order_by(desc(Intervention.created_at))
    ).scalars().all()


def get_device_by_key_hash(db: Session, key_hash: str) -> Optional[Device]:
    return db.execute(
        select(Device).where(Device.key_hash == key_hash, Device.revoked_at.is_(None))
    ).scalar_one_or_none()


def get_device_by_patient_id(db: Session, patient_id: int) -> Optional[Device]:
    return db.execute(
        select(Device).where(Device.patient_id == patient_id).order_by(desc(Device.id))
    ).scalars().first()


def set_device_key_hash(db: Session, device: Device, key_hash: str) -> None:
    """Ruota la chiave di un dispositivo esistente (device_cli create)."""
    device.key_hash = key_hash
    device.revoked_at = None


def revoke_device(db: Session, device: Device, when: datetime) -> None:
    device.revoked_at = when


def insert_cgm_reading(
    db: Session,
    patient_id: int,
    recorded_at: datetime,
    glucose_mmol: float,
    insulin_u: float,
    cho_rate: float,
) -> bool:
    """
    INSERT immutabile: ON CONFLICT DO NOTHING.
    Le ritrasmissioni restano idipotenti (same key = no-op), ma i dati
    già scritti non possono essere sovrascritti.
    Ritorna True se inserita, False se già esistente (conflitto).
    """
    dialect = db.get_bind().dialect.name
    inserter = pg_insert if dialect == "postgresql" else sqlite_insert
    conflict = (
        {"constraint": "uq_cgm_patient_time"}
        if dialect == "postgresql"
        else {"index_elements": ["patient_id", "recorded_at"]}
    )
    values = {
        "patient_id": patient_id,
        "recorded_at": recorded_at,
        "glucose_mmol": glucose_mmol,
        "insulin_u": insulin_u,
        "cho_rate": cho_rate,
    }
    stmt = inserter(CgmReading).values(**values).on_conflict_do_nothing(**conflict)
    result = db.execute(stmt)
    return result.rowcount > 0


def backfill_meal_ground_truth(
    db: Session,
    patient_id: int,
    reading_time: datetime,
    glucose_mmol: float,
) -> int:
    """
    Verità a terra (P9): dopo il salvataggio di una lettura CGM assegna la glicemia
    misurata ai pasti dichiarati il cui istante target (declared_at + 30 min) coincide
    con la lettura entro mezzo passo CGM (griglia a 5 min => scarto massimo 2.5 min,
    comunque entro i +-5 min richiesti) e che non hanno ancora actual_glucose_30m_mmol.

    I pasti senza dati del sensore (NO_RECENT_DATA) non hanno una previsione reale e
    restano esclusi. Ritorna il numero di pasti aggiornati.
    """
    if reading_time.tzinfo is None:
        reading_time = reading_time.replace(tzinfo=timezone.utc)
    tolerance = timedelta(minutes=saas_settings.CGM_STEP_MINUTES / 2.0)
    window_lo = reading_time - timedelta(minutes=30) - tolerance
    window_hi = reading_time - timedelta(minutes=30) + tolerance
    candidates = db.execute(
        select(MealPrediction).where(
            MealPrediction.patient_id == patient_id,
            MealPrediction.actual_glucose_30m_mmol.is_(None),
            MealPrediction.outcome_code != "NO_RECENT_DATA",
            MealPrediction.declared_at >= window_lo,
            MealPrediction.declared_at <= window_hi,
        )
    ).scalars().all()

    updated = 0
    for meal in candidates:
        target = ensure_utc(meal.declared_at) + timedelta(minutes=30)
        if abs(reading_time - target) <= tolerance:
            meal.actual_glucose_30m_mmol = glucose_mmol
            updated += 1
    return updated


def revoke_jti(db: Session, jti: str, expires_at: datetime) -> None:
    if db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti)).first() is None:
        db.add(RevokedToken(jti=jti, expires_at=expires_at))


def delete_expired_revoked_tokens(db: Session, now: datetime) -> int:
    """Elimina i token revocati ormai scaduti: la tabella non cresce all'infinito (P12)."""
    result = db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    return int(result.rowcount or 0)
