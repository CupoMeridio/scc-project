"""
FILE: saas_app/routers/ingestion.py
RUOLO: Router HTTP per ingestione CGM con autenticazione per dispositivo.
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from saas_app.auth import hash_device_key
from saas_app.db import get_db
from saas_app.schemas import CgmIngestRequest, CgmIngestResponse
from saas_app.services import ingestion_service

router = APIRouter(prefix="/api/cgm", tags=["CGM Ingestion"])

_DEVICE_RATE_LIMIT = 60  # max 60 letture/minuto per dispositivo
_DEVICE_RATE_WINDOW = 60
_device_hits: dict[int, list[float]] = {}


def _prune_device_hits(now: float) -> None:
    """Rimuove le voci scadute: senza pulizia il dizionario in memoria cresce senza limite (N1)."""
    for key in list(_device_hits.keys()):
        recent = [t for t in _device_hits[key] if now - t < _DEVICE_RATE_WINDOW]
        if recent:
            _device_hits[key] = recent
        else:
            del _device_hits[key]


def _check_device_rate_limit(device_id: int) -> None:
    now = time.monotonic()
    _prune_device_hits(now)
    hits = _device_hits.get(device_id, [])
    if len(hits) >= _DEVICE_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit per dispositivo superato.",
        )
    hits.append(now)
    _device_hits[device_id] = hits


@router.post("/ingest", response_model=CgmIngestResponse)
def ingest_cgm_sample(
    payload: CgmIngestRequest,
    response: Response,
    x_device_key: Optional[str] = Header(default=None),
    x_forwarded_proto: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> CgmIngestResponse:
    # L'endpoint è interno: rifiuta richieste provenienti dall'Ingress (D2)
    if x_forwarded_proto:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Endpoint non accessibile dall'esterno.")

    if not x_device_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chiave dispositivo mancante.")

    key_hash = hash_device_key(x_device_key)
    from saas_app import repositories as repo

    device = repo.get_device_by_key_hash(db, key_hash)
    if device is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Dispositivo non registrato o revocato.")

    # Il paziente si ricava dal dispositivo, non dal payload
    if payload.patient_code and payload.patient_code != device.patient.patient_code:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="patient_code non corrisponde al dispositivo.",
        )

    _check_device_rate_limit(device.id)

    result = ingestion_service.enqueue_or_ingest_sample(db, payload, device.patient)
    response.status_code = (
        status.HTTP_202_ACCEPTED if result.status == "queued" else status.HTTP_201_CREATED
    )
    return result
