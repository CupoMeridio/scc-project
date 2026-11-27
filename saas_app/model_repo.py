"""
FILE: saas_app/model_repo.py
RUOLO: Servizio Data Zone "Model Repository" (attach/arch.pdf).

Espone in sola lettura ai pod di predizione gli artefatti ONNX e i metadati SHA-256
della versione Champion attiva, e accetta aggiornamenti autenticati (PUT /models/publish)
solo dal CronJob della Training Zone (protetto da NetworkPolicy e token segreto).
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common.model_signing import verify_manifest_ed25519

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("saas-model-repo")

MODEL_REPO_DIR = Path(os.getenv("MODEL_REPO_DIR", "/var/lib/model-repo"))
BASELINE_MODEL_DIR = Path(os.getenv("BASELINE_MODEL_DIR", "/app/baseline_model"))
MODEL_REPO_UPLOAD_TOKEN = os.getenv("MODEL_REPO_UPLOAD_TOKEN", "")
MODEL_SIGNING_PUBLIC_KEY_PEM = os.getenv("MODEL_SIGNING_PUBLIC_KEY_PEM", "").replace("\\n", "\n")
SAAS_ENV = os.getenv("SAAS_ENV", "production").lower()

ALLOWED_FILES = frozenset({"model.onnx", "model.onnx.data", "model_metadata.json"})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _current_dir() -> Path:
    return MODEL_REPO_DIR / "current"


def _verify_dir_integrity(model_dir: Path) -> Dict[str, object]:
    meta_path = model_dir / "model_metadata.json"
    onnx_path = model_dir / "model.onnx"
    if not meta_path.is_file() or not onnx_path.is_file():
        raise ValueError("Artefatti minimi (model.onnx / model_metadata.json) mancanti.")

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    integrity = metadata.get("artifacts_integrity", {})
    expected_onnx = integrity.get("model.onnx", metadata.get("model_sha256", ""))
    if not expected_onnx or _sha256_file(onnx_path).lower() != str(expected_onnx).lower():
        raise ValueError("Hash SHA-256 non valido per model.onnx.")

    data_path = model_dir / "model.onnx.data"
    if data_path.is_file():
        expected_data = integrity.get("model.onnx.data", "")
        if not expected_data or _sha256_file(data_path).lower() != str(expected_data).lower():
            raise ValueError("Hash SHA-256 non valido per model.onnx.data.")
    return metadata


def bootstrap_repository() -> None:
    MODEL_REPO_DIR.mkdir(parents=True, exist_ok=True)
    cur = _current_dir()
    if (cur / "model.onnx").is_file() and (cur / "model_metadata.json").is_file():
        _verify_dir_integrity(cur)
        return

    if not BASELINE_MODEL_DIR.is_dir():
        raise RuntimeError(f"Directory baseline non trovata: {BASELINE_MODEL_DIR}")

    staging = MODEL_REPO_DIR / ".bootstrap_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for fname in ALLOWED_FILES:
        src = BASELINE_MODEL_DIR / fname
        if src.is_file():
            shutil.copy2(src, staging / fname)
    _verify_dir_integrity(staging)

    shutil.rmtree(cur, ignore_errors=True)
    staging.rename(cur)
    logger.info("[✓] Model Repository inizializzato con il modello baseline (%s).", cur)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if SAAS_ENV != "test" and len(MODEL_REPO_UPLOAD_TOKEN) < 32:
        raise RuntimeError("[FAIL-CLOSED] MODEL_REPO_UPLOAD_TOKEN mancante o inferiore a 32 caratteri.")
    bootstrap_repository()
    yield


app = FastAPI(
    title="Diabetes Model Repository",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "healthy", "service": "model-repository"}


@app.get("/ready")
def ready() -> dict:
    try:
        meta = _verify_dir_integrity(_current_dir())
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Repository non pronto: {exc}",
        ) from exc
    return {
        "status": "ready",
        "model_version": meta.get("model_version", "unknown"),
    }


@app.get("/models/current/manifest")
def get_manifest() -> dict:
    cur = _current_dir()
    try:
        meta = _verify_dir_integrity(cur)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Artefatti del repository non validi: {exc}",
        ) from exc
    files: List[str] = [f for f in ("model_metadata.json", "model.onnx", "model.onnx.data") if (cur / f).is_file()]
    return {
        "model_version": meta.get("model_version", "unknown"),
        "artifacts_integrity": meta.get("artifacts_integrity", {}),
        "files": files,
    }


@app.get("/models/current/{filename}")
def download_artifact(filename: str):
    if filename not in ALLOWED_FILES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artefatto non trovato.")
    target = _current_dir() / filename
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artefatto non presente.")
    media_type = "application/json" if filename.endswith(".json") else "application/octet-stream"
    return FileResponse(path=str(target), media_type=media_type, filename=filename)


class PublishBundle(BaseModel):
    metadata_b64: str
    onnx_b64: str
    data_b64: Optional[str] = None


@app.put("/models/publish")
def publish_model(
    bundle: PublishBundle,
    x_repo_token: Optional[str] = Header(default=None),
) -> dict:
    expected_token = MODEL_REPO_UPLOAD_TOKEN or os.getenv("MODEL_REPO_UPLOAD_TOKEN", "")
    if not expected_token or not x_repo_token or not secrets.compare_digest(x_repo_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token upload non valido.")

    try:
        meta_bytes = base64.b64decode(bundle.metadata_b64, validate=True)
        onnx_bytes = base64.b64decode(bundle.onnx_b64, validate=True)
        data_bytes = base64.b64decode(bundle.data_b64, validate=True) if bundle.data_b64 else None
        meta = json.loads(meta_bytes.decode("utf-8"))
        integrity = meta.get("artifacts_integrity", {})
        if _sha256_bytes(onnx_bytes).lower() != str(integrity.get("model.onnx", "")).lower():
            raise ValueError("Checksum model.onnx non corrispondente.")
        if data_bytes is not None:
            if _sha256_bytes(data_bytes).lower() != str(integrity.get("model.onnx.data", "")).lower():
                raise ValueError("Checksum model.onnx.data non corrispondente.")
        pub_pem = MODEL_SIGNING_PUBLIC_KEY_PEM or os.getenv("MODEL_SIGNING_PUBLIC_KEY_PEM", "").replace("\\n", "\n")
        if pub_pem and not verify_manifest_ed25519(meta, pub_pem):
            raise ValueError("Firma Ed25519 del manifest non valida o assente.")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    MODEL_REPO_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".publish_", dir=str(MODEL_REPO_DIR)))
    try:
        (tmp_dir / "model_metadata.json").write_bytes(meta_bytes)
        (tmp_dir / "model.onnx").write_bytes(onnx_bytes)
        if data_bytes is not None:
            (tmp_dir / "model.onnx.data").write_bytes(data_bytes)
        _verify_dir_integrity(tmp_dir)

        cur = _current_dir()
        old = MODEL_REPO_DIR / ".previous"
        shutil.rmtree(old, ignore_errors=True)
        if cur.exists():
            cur.rename(old)
        tmp_dir.rename(cur)
        shutil.rmtree(old, ignore_errors=True)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    logger.info("[✓] Pubblicata nuova versione modello nel repository: %s", meta.get("model_version"))
    return {"status": "published", "model_version": meta.get("model_version", "unknown")}
