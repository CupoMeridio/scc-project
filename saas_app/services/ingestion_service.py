"""
FILE: saas_app/services/ingestion_service.py
RUOLO: Ingestione asincrona via coda Redis + persistenza immutabile su PostgreSQL.
"""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_app import repositories as repo
from saas_app.config import saas_settings
from saas_app.models import Patient
from saas_app.queue_client import enqueue_cgm_sample
from saas_app.schemas import CgmIngestRequest, CgmIngestResponse


def _validate_timestamp(ts: datetime) -> datetime:
    """Valida che il timestamp sia entro la finestra consentita."""
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    future_limit = now + timedelta(minutes=saas_settings.CGM_TIMESTAMP_FUTURE_TOLERANCE_MINUTES)
    past_limit = now - timedelta(hours=saas_settings.CGM_TIMESTAMP_MAX_AGE_HOURS)
    if ts > future_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Timestamp nel futuro non consentito.",
        )
    if ts < past_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Timestamp troppo vecchio.",
        )
    return ts


def enqueue_or_ingest_sample(
    db: Session, payload: CgmIngestRequest, patient: Patient
) -> CgmIngestResponse:
    ts = payload.recorded_at or datetime.now(timezone.utc)
    ts = _validate_timestamp(ts)

    if saas_settings.REDIS_URL:
        try:
            enqueue_cgm_sample(
                {
                    "patient_code": patient.patient_code,
                    "recorded_at": ts.isoformat(),
                    "glucose_mmol": payload.glucose_mmol,
                    "insulin_u": payload.insulin_u,
                    "cho_rate": payload.cho_rate,
                }
            )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Coda di ingestione CGM temporaneamente non disponibile.",
            )
        return CgmIngestResponse(status="queued", patient_code=patient.patient_code, recorded_at=ts)

    return persist_sample(db, payload, patient, ts)


def persist_sample(
    db: Session, payload: CgmIngestRequest, patient: Patient, ts: datetime | None = None
) -> CgmIngestResponse:
    effective_ts = ts or payload.recorded_at or datetime.now(timezone.utc)
    effective_ts = _validate_timestamp(effective_ts)
    inserted = repo.insert_cgm_reading(
        db=db,
        patient_id=patient.id,
        recorded_at=effective_ts,
        glucose_mmol=payload.glucose_mmol,
        insulin_u=payload.insulin_u,
        cho_rate=payload.cho_rate,
    )
    if inserted:
        # Verità a terra (P9): la lettura appena salvata chiude i pasti il cui +30 min
        # è ora raggiunto, in modo che previsione e misurato siano confrontabili.
        repo.backfill_meal_ground_truth(db, patient.id, effective_ts, payload.glucose_mmol)
    db.commit()
    status_str = "ingested" if inserted else "duplicate"
    return CgmIngestResponse(status=status_str, patient_code=patient.patient_code, recorded_at=effective_ts)
