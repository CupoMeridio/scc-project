"""
FILE: saas_app/ml_client.py
RUOLO: Client HTTP sincrono con connection pooling e retry verso il Prediction Service.

Usato solo dagli endpoint `def`, che FastAPI esegue nel thread pool: nessuna
chiamata bloccante finisce nell'event loop.
"""

import logging
import socket
import time
from typing import Any, Dict, List, Optional

import httpx

from saas_app.clinical import MlPredictResponse
from saas_app.config import saas_settings

logger = logging.getLogger("saas-api.ml_client")

_shared_client: Optional[httpx.Client] = None


def get_http_client() -> httpx.Client:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.Client(
            timeout=httpx.Timeout(connect=1.5, read=3.0, write=1.5, pool=2.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_client


def close_http_client() -> None:
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        _shared_client.close()
    _shared_client = None


class MlServiceUnavailableError(Exception):
    """Errore interno sicuro: non espone URL o dettagli infrastrutturali ai client."""


def call_ml_predict(
    patient_code: str,
    glucose_history: List[float],
    exogenous_inputs: List[float],
) -> MlPredictResponse:
    """Invoca il Prediction Service con retry su errori transitori e valida il contratto JSON."""
    url = saas_settings.ML_SERVICE_URL.rstrip("/") + "/predict"
    payload = {
        "patient_id": f"patient_{patient_code}",
        "glucose_history": glucose_history,
        "exogenous_inputs": exogenous_inputs,
    }
    client = get_http_client()

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return MlPredictResponse.model_validate(resp.json())
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.05)
        except Exception as exc:
            logger.error("Risposta ML non valida o errore HTTP: %s", exc)
            raise MlServiceUnavailableError("Servizio di predizione ML non disponibile.") from exc

    logger.error("Servizio ML irraggiungibile dopo retry: %s", last_exc)
    raise MlServiceUnavailableError("Servizio di predizione ML temporaneamente non raggiungibile.")


def _resolve_ml_metric_urls() -> List[str]:
    targets: List[str] = []
    try:
        infos = socket.getaddrinfo(
            saas_settings.ML_HEADLESS_HOST,
            saas_settings.ML_METRICS_PORT,
            proto=socket.IPPROTO_TCP,
        )
        seen_ips = set()
        for info in infos:
            ip = info[4][0]
            if ip not in seen_ips:
                seen_ips.add(ip)
                targets.append(f"http://{ip}:{saas_settings.ML_METRICS_PORT}/metrics")
    except socket.gaierror:
        targets.append(saas_settings.ML_SERVICE_URL.rstrip("/") + "/metrics")
    return targets


def collect_ml_pod_metrics() -> List[Dict[str, Any]]:
    """Raccoglie /metrics dai pod ML. Chiamata da un endpoint `def`, quindi in thread pool."""
    targets = _resolve_ml_metric_urls()
    client = get_http_client()
    results: List[Dict[str, Any]] = []
    for url in targets:
        try:
            resp = client.get(url, timeout=1.5)
            if resp.status_code == 200:
                results.append(resp.json())
        except Exception as exc:
            logger.debug("Metriche ML non raggiungibili su %s: %s", url, exc)
    return results
