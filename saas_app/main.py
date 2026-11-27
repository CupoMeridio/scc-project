"""
FILE: saas_app/main.py
RUOLO: Applicazione FastAPI SaaS con validazione fail-fast dei segreti e header OWASP/CSP/HSTS.
"""

import logging
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from saas_app.auth import _get_revocation_redis, reset_revocation_redis
from saas_app.config import saas_settings
from saas_app.db import SessionLocal
from saas_app.iam_client import close_iam_client
from saas_app.ml_client import close_http_client
from saas_app.queue_client import close_sync_redis
from saas_app.routers import auth, doctor, ingestion, manager, patient

logging.basicConfig(
    level=getattr(logging, saas_settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("saas-api")


_ROUTERS_BY_MODE = {
    "all": (auth.router, patient.router, doctor.router, manager.router, ingestion.router),
    "iam": (auth.router,),
    "clinical": (patient.router, doctor.router),
    "manager": (manager.router,),
    "ingest": (ingestion.router,),
}


def create_app(service_mode: str | None = None) -> FastAPI:
    """Costruisce l'app FastAPI abilitando solo i router della zona applicativa scelta."""
    active_mode = (service_mode or saas_settings.SERVICE_MODE or "all").strip().lower()
    if active_mode not in _ROUTERS_BY_MODE:
        raise ValueError(f"SERVICE_MODE non valido: {active_mode}")

    @asynccontextmanager
    async def _mode_lifespan(application: FastAPI):
        require_jwt = active_mode != "ingest"
        saas_settings.validate_security_secrets(require_jwt=require_jwt)
        logger.info(
            "[✓] Backend SaaS avviato (SAAS_ENV=%s, SERVICE_MODE=%s).",
            saas_settings.SAAS_ENV,
            active_mode,
        )
        yield
        close_http_client()
        close_iam_client()
        close_sync_redis()
        reset_revocation_redis()
        logger.info("Chiusura Backend SaaS completata.")

    application = FastAPI(
        title=saas_settings.PROJECT_NAME,
        version=saas_settings.VERSION,
        description="Backend SaaS RBAC (IAM, Paziente, Diabetologo, Center Manager, CGM Ingestion).",
        docs_url=None if saas_settings.SAAS_ENV == "production" else "/api/docs",
        redoc_url=None if saas_settings.SAAS_ENV == "production" else "/api/redoc",
        openapi_url=None if saas_settings.SAAS_ENV == "production" else "/api/openapi.json",
        lifespan=_mode_lifespan,
    )
    application.state.service_mode = active_mode

    @application.middleware("http")
    async def security_tracing_middleware(request: Request, call_next) -> Response:
        raw_req_id = request.headers.get("X-Request-ID", "")
        if raw_req_id and re.match(r"^[A-Za-z0-9_\-]{1,64}$", raw_req_id):
            request_id = raw_req_id
        else:
            request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none';"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @application.get("/api/health", tags=["Monitoring"])
    def health() -> dict:
        return {
            "status": "healthy",
            "service": "saas-api",
            "service_mode": active_mode,
            "version": saas_settings.VERSION,
        }

    @application.get("/api/ready", tags=["Monitoring"])
    def ready() -> dict:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        rev_redis = _get_revocation_redis(readonly=True)
        if rev_redis is not None and active_mode in ("clinical", "manager"):
            rev_redis.ping()
        return {
            "status": "ready",
            "service": "saas-api",
            "service_mode": active_mode,
            "database": "ok",
        }

    for router in _ROUTERS_BY_MODE[active_mode]:
        application.include_router(router)
    return application


app = create_app()
