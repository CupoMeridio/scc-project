"""
FILE: saas_app/queue_client.py
RUOLO: Client Redis per la coda asincrona CGM (DMZ Queue -> Ingestion Worker).
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple, Union

import redis

from saas_app.config import saas_settings

logger = logging.getLogger("saas-api.queue")

_sync_redis: Optional[redis.Redis] = None

# La coda usa un envelope con contatore tentativi: il messaggio resta in una
# lista "in-flight" finché il worker non fa ack, così un crash non lo perde.
INFLIGHT_QUEUE_NAME = f"{saas_settings.REDIS_QUEUE_NAME}:inflight"
DLQ_QUEUE_NAME = f"{saas_settings.REDIS_QUEUE_NAME}:dlq"
MAX_PROCESS_ATTEMPTS = 3

Event = Union[Dict[str, Any], str]


def encode_event(event: Event, attempt: int = 0) -> str:
    return json.dumps({"attempt": attempt, "event": event}, separators=(",", ":"))


def decode_envelope(raw: str) -> Tuple[Event, int]:
    """Decodifica l'envelope {"attempt": n, "event": ...} usato dalla coda."""
    obj = json.loads(raw)
    if not isinstance(obj, dict) or "event" not in obj:
        raise ValueError("Envelope di coda non valido.")
    try:
        attempt = int(obj.get("attempt", 0))
    except (TypeError, ValueError):
        attempt = 0
    return obj["event"], attempt


def _redis_conn_kwargs(connect_timeout: float, socket_timeout: float) -> dict:
    kwargs: dict = {
        "decode_responses": True,
        "socket_connect_timeout": connect_timeout,
        "socket_timeout": socket_timeout,
        "retry_on_timeout": True,
        "health_check_interval": 30,
    }
    if saas_settings.REDIS_CA_CERT_PATH:
        kwargs["ssl_ca_certs"] = saas_settings.REDIS_CA_CERT_PATH
        kwargs["ssl_cert_reqs"] = "required"
    return kwargs


def get_sync_redis() -> Optional[redis.Redis]:
    """Client condiviso per l'API: usato da endpoint `def`, quindi in thread pool."""
    global _sync_redis
    if not saas_settings.REDIS_URL:
        return None
    if _sync_redis is None:
        _sync_redis = redis.from_url(
            saas_settings.REDIS_URL,
            **_redis_conn_kwargs(1.5, 2.0),
        )
    return _sync_redis


def close_sync_redis() -> None:
    global _sync_redis
    if _sync_redis is not None:
        _sync_redis.close()
    _sync_redis = None


def enqueue_cgm_sample(event: Dict[str, Any]) -> None:
    client = get_sync_redis()
    if client is None:
        raise RuntimeError("REDIS_URL non configurato.")
    client.rpush(saas_settings.REDIS_QUEUE_NAME, encode_event(event))


def build_sync_redis() -> redis.Redis:
    if not saas_settings.REDIS_URL:
        raise RuntimeError("REDIS_URL obbligatorio per l'Ingestion Worker.")
    return redis.from_url(
        saas_settings.REDIS_URL,
        **_redis_conn_kwargs(3.0, 8.0),
    )


def claim_for_processing(client: redis.Redis, timeout: int = 5) -> Optional[str]:
    """Sposta atomicamente un evento dalla coda principale a quella in-flight."""
    return client.brpoplpush(
        saas_settings.REDIS_QUEUE_NAME, INFLIGHT_QUEUE_NAME, timeout
    )


def ack_processed(client: redis.Redis, raw_payload: str) -> None:
    """Conferma il messaggio: lo rimuove dalla lista in-flight."""
    client.lrem(INFLIGHT_QUEUE_NAME, 1, raw_payload)


def requeue_failed(client: redis.Redis, raw_payload: str) -> bool:
    """
    Rimuove il messaggio dalla lista in-flight e lo riporta in coda con un
    tentativo in più. Dopo MAX_PROCESS_ATTEMPTS lo sposta nella DLQ.
    Ritorna True se il messaggio torna in coda, False se finisce nella DLQ.
    """
    event, attempt = decode_envelope(raw_payload)
    next_attempt = attempt + 1
    target = (
        DLQ_QUEUE_NAME if next_attempt >= MAX_PROCESS_ATTEMPTS else saas_settings.REDIS_QUEUE_NAME
    )
    pipe = client.pipeline(transaction=True)
    pipe.lrem(INFLIGHT_QUEUE_NAME, 1, raw_payload)
    pipe.rpush(target, encode_event(event, next_attempt))
    pipe.execute()
    return target == saas_settings.REDIS_QUEUE_NAME


def recover_inflight(client: redis.Redis) -> int:
    """
    Recupera all'avvio i messaggi rimasti in-flight dopo un crash dei worker.
    Il ripristino è idempotente lato DB grazie all'upsert atomico.
    """
    pending = client.lrange(INFLIGHT_QUEUE_NAME, 0, -1)
    for raw in pending:
        pipe = client.pipeline(transaction=True)
        pipe.lrem(INFLIGHT_QUEUE_NAME, 1, raw)
        pipe.rpush(saas_settings.REDIS_QUEUE_NAME, raw)
        pipe.execute()
    if pending:
        logger.warning("[✓] Recuperati %d eventi in-flight da un precedente crash.", len(pending))
    return len(pending)
