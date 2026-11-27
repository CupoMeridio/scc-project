"""
FILE: saas_app/iam_client.py
RUOLO: Modello in-memory dell'identità verificata dal JWT EdDSA e client di provisioning
       pazienti in due passi (SOLO clinical -> iam con token dedicato IAM_PROVISION_TOKEN).

Nessun servizio verifica i token chiamando IAM via HTTP ad ogni richiesta, e `manager`
non possiede alcuna credenziale né percorso di rete verso la Identity Zone (V1 / Q1).
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx
from fastapi import HTTPException, status

from saas_app.config import saas_settings

logger = logging.getLogger("saas-api.iam_client")

_iam_http_client: Optional[httpx.Client] = None


@dataclass
class AuthenticatedUser:
    """Identità estratta dal JWT EdDSA verificato localmente (nessuna chiamata IAM)."""

    id: int
    username: str
    role: str
    is_active: bool = True
    must_change_password: bool = False


def get_iam_client() -> httpx.Client:
    global _iam_http_client
    if _iam_http_client is None or _iam_http_client.is_closed:
        verify_opt: bool | str = saas_settings.IAM_CA_CERT_PATH or True
        _iam_http_client = httpx.Client(
            verify=verify_opt,
            timeout=httpx.Timeout(connect=1.5, read=3.0, write=1.5, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _iam_http_client


def close_iam_client() -> None:
    global _iam_http_client
    if _iam_http_client is not None and not _iam_http_client.is_closed:
        _iam_http_client.close()
    _iam_http_client = None


def _provision_headers() -> dict[str, str]:
    if not saas_settings.IAM_PROVISION_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credenziale di provisioning IAM non configurata per questa zona.",
        )
    return {"X-Provision-Token": saas_settings.IAM_PROVISION_TOKEN}


def _raise_for_iam_error(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        detail = resp.json().get("detail", "Errore IAM.")
    except Exception:
        detail = "Errore IAM."
    raise HTTPException(status_code=resp.status_code, detail=detail)


def new_provision_key() -> str:
    return uuid.uuid4().hex


def provision_patient_account_via_iam(username: str, idempotency_key: str) -> Tuple[int, str]:
    """Passo 1 (pending): crea credenziali in stato `pending` senza nome/email clinici."""
    url = saas_settings.IAM_INTERNAL_URL.rstrip("/") + "/api/auth/internal/patients/provision"
    try:
        resp = get_iam_client().post(
            url,
            json={"username": username, "idempotency_key": idempotency_key},
            headers=_provision_headers(),
        )
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        logger.error("Servizio IAM irraggiungibile durante provision paziente: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servizio di autenticazione temporaneamente non disponibile.",
        ) from exc

    _raise_for_iam_error(resp)
    data = resp.json()
    return int(data["user_id"]), str(data["temporary_password"])


def activate_patient_account_via_iam(user_id: int, idempotency_key: str) -> None:
    """Passo 2 (active): attiva l'account solo dopo il commit del profilo clinico."""
    url = saas_settings.IAM_INTERNAL_URL.rstrip("/") + f"/api/auth/internal/patients/{user_id}/activate"
    try:
        resp = get_iam_client().post(
            url,
            json={"idempotency_key": idempotency_key},
            headers=_provision_headers(),
        )
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        logger.error("Servizio IAM irraggiungibile durante attivazione paziente %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account creato in stato pending: attivazione IAM temporaneamente non disponibile.",
        ) from exc

    _raise_for_iam_error(resp)
