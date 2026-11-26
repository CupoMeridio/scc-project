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

from saas_app.config import saas_settings
from saas_app.db import SessionLocal
from saas_app.ml_client import close_http_client
from saas_app.queue_client import close_sync_redis
from saas_app.routers import auth, doctor, ingestion, manager, patient

logging.basicConfig(
    level=getattr(logging, saas_settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("saas-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    saas_settings.validate_security_secrets()
    logger.info("[✓] Backend SaaS avviato (SAAS_ENV=%s).", saas_settings.SAAS_ENV)
    yield
    close_http_client()
    close_sync_redis()
    logger.info("Chiusura Backend SaaS completata.")


app = FastAPI(
    title=saas_settings.PROJECT_NAME,
    version=saas_settings.VERSION,
    description="Backend SaaS RBAC (IAM, Paziente, Diabetologo, Center Manager, CGM Ingestion).",
    docs_url=None if saas_settings.SAAS_ENV == "production" else "/api/docs",
    redoc_url=None if saas_settings.SAAS_ENV == "production" else "/api/redoc",
    openapi_url=None if saas_settings.SAAS_ENV == "production" else "/api/openapi.json",
    lifespan=lifespan,
)


@app.middleware("http")
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


@app.get("/api/health", tags=["Monitoring"])
def health() -> dict:
    return {"status": "healthy", "service": "saas-api", "version": saas_settings.VERSION}


@app.get("/api/ready", tags=["Monitoring"])
def ready() -> dict:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ready", "service": "saas-api", "database": "ok"}


app.include_router(auth.router)
app.include_router(patient.router)
app.include_router(doctor.router)
app.include_router(manager.router)
app.include_router(ingestion.router)
