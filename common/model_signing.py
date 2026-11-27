"""
FILE: common/model_signing.py
RUOLO: Firma e verifica asimmetrica Ed25519 del manifest dei metadati ML (V4).
"""

import base64
import json
from typing import Any, Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_SIGNED_KEYS = (
    "model_name",
    "model_version",
    "created_at",
    "trained_until",
    "model_sha256",
    "artifacts_integrity",
    "scaler",
)


def canonical_manifest_payload(metadata: Dict[str, Any]) -> bytes:
    """Serializza in forma deterministica i campi critici del manifest escludendo la firma."""
    subset = {k: metadata[k] for k in _SIGNED_KEYS if k in metadata}
    return json.dumps(subset, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest_ed25519(metadata: Dict[str, Any], private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("La chiave di firma modello deve essere Ed25519 (PKCS8 PEM).")
    sig = key.sign(canonical_manifest_payload(metadata))
    return base64.b64encode(sig).decode("ascii")


def verify_manifest_ed25519(metadata: Dict[str, Any], public_pem: str) -> bool:
    sig_b64 = metadata.get("manifest_signature")
    if not isinstance(sig_b64, str) or not sig_b64.strip():
        return False
    try:
        pub = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        sig = base64.b64decode(sig_b64.encode("ascii"), validate=True)
        pub.verify(sig, canonical_manifest_payload(metadata))
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False
