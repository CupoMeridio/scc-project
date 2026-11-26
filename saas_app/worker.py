"""
FILE: saas_app/worker.py
RUOLO: Ingestion Worker dedicato che consuma la coda Redis e scrive in PostgreSQL (arch.pdf).
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone

from saas_app import repositories as repo
from saas_app.config import saas_settings
from saas_app.db import SessionLocal
from saas_app.queue_client import (
    ack_processed,
    build_sync_redis,
    claim_for_processing,
    decode_envelope,
    recover_inflight,
    requeue_failed,
    MAX_PROCESS_ATTEMPTS,
)
from saas_app.schemas import CgmIngestRequest
from saas_app.services.ingestion_service import persist_sample

logging.basicConfig(
    level=getattr(logging, saas_settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cgm-ingestion-worker")

# File di heartbeat per le probe Kubernetes. /tmp è intenzionale: è l'unica directory
# scrivibile nel container read-only (readOnlyRootFilesystem) e il file è effimero,
# non contiene dati sensibili e viene ricreato a ogni ciclo (bandit B108: accettato).
HEARTBEAT_PATH = os.getenv("WORKER_HEARTBEAT_PATH", "/tmp/cgm-worker-alive")
_running = True

# Cadenza della manutenzione periodica (pulizia dei token revocati scaduti).
_MAINTENANCE_INTERVAL_SECONDS = 3600


def _heartbeat() -> None:
    """Aggiorna il file controllato dalle probe di Kubernetes."""
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError as exc:  # pragma: no cover - diagnostico
        logger.warning("Impossibile aggiornare l'heartbeat del worker: %s", exc)


def _handle_stop(signum, _frame):
    global _running
    logger.info("Ricevuto segnale %s: arresto controllato del worker...", signum)
    _running = False


def _cleanup_revoked_tokens() -> None:
    """Rimuove i token revocati scaduti: la tabella revoked_tokens non cresce senza limite (P12)."""
    try:
        with SessionLocal() as db:
            removed = repo.delete_expired_revoked_tokens(db, datetime.now(timezone.utc))
            db.commit()
        if removed:
            logger.info("[✓] Rimossi %d token revocati scaduti.", removed)
    except Exception as exc:  # pragma: no cover - solo diagnostico
        logger.error("Pulizia dei token revocati fallita: %s", exc)


def process_raw_message(raw_payload: str) -> None:
    data, _attempt = decode_envelope(raw_payload)
    req = CgmIngestRequest.model_validate(data)
    with SessionLocal() as db:
        patient = repo.get_patient_by_code(db, req.patient_code)
        if patient is None:
            raise ValueError(f"Paziente '{req.patient_code}' non trovato per l'evento in coda.")
        # persist_sample salva la lettura e, nella stessa transazione, assegna la verità a
        # terra (actual_glucose_30m_mmol) ai pasti il cui +30 min è ora raggiunto (P9).
        persist_sample(db, req, patient)


def main() -> None:
    # Il worker non firma né verifica token: valida solo i segreti che usa davvero (P12).
    saas_settings.validate_security_secrets(require_jwt=False)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    client = build_sync_redis()
    recover_inflight(client)
    _cleanup_revoked_tokens()
    last_maintenance = time.monotonic()
    _heartbeat()
    logger.info("[✓] Ingestion Worker avviato in ascolto su coda '%s'.", saas_settings.REDIS_QUEUE_NAME)

    while _running:
        _heartbeat()
        if time.monotonic() - last_maintenance >= _MAINTENANCE_INTERVAL_SECONDS:
            _cleanup_revoked_tokens()
            last_maintenance = time.monotonic()
        try:
            raw_payload = claim_for_processing(client, timeout=5)
        except Exception as exc:
            logger.error("Errore durante la lettura della coda CGM: %s", exc)
            time.sleep(1.0)
            continue
        if raw_payload is None:
            continue

        try:
            process_raw_message(raw_payload)
        except Exception as exc:
            logger.error("Errore durante l'elaborazione campione CGM in coda: %s", exc)
            try:
                if requeue_failed(client, raw_payload):
                    logger.warning("Evento riportato in coda per un nuovo tentativo.")
                else:
                    logger.critical(
                        "Evento spostato nella DLQ '%s' dopo %d tentativi.",
                        saas_settings.REDIS_QUEUE_NAME + ":dlq",
                        MAX_PROCESS_ATTEMPTS,
                    )
            except Exception as redis_exc:
                # Il messaggio resta in-flight: verrà recuperato al riavvio del worker.
                logger.error("Impossibile aggiornare lo stato Redis dell'evento: %s", redis_exc)
            time.sleep(1.0)
            continue

        try:
            ack_processed(client, raw_payload)
        except Exception as exc:
            logger.error("Ack Redis fallito (evento recuperabile al riavvio): %s", exc)

    client.close()
    logger.info("Ingestion Worker terminato.")


if __name__ == "__main__":
    main()
