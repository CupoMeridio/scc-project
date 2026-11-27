"""
FILE: saas_app/backup_crypto.py
RUOLO: Cifratura asimmetrica autenticata dei backup pg_dump (X25519 + HKDF-SHA256 + AES-256-GCM, V6).

Il cluster possiede SOLO la chiave pubblica (`BACKUP_PUBLIC_KEY_B64`): un pod compromesso
non può decifrare i backup esistenti, e qualsiasi manomissione del file cifrato fallisce
l'autenticazione del tag GCM in fase di decifratura (`InvalidTag`).
"""

import argparse
import base64
import os
import struct
import sys
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MAGIC = b"SCCBK01\x00"  # 8 byte magic + version
_NONCE_PREFIX_LEN = 8
_CHUNK_SIZE = 64 * 1024


def generate_backup_keypair() -> Tuple[str, str]:
    """Restituisce (private_b64, public_b64) per backup asimmetrici X25519."""
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(priv_raw).decode("ascii"), base64.b64encode(pub_raw).decode("ascii")


def _derive_aead_key(shared_secret: bytes, eph_pub_raw: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"scc-diabetes-pg-backup-v1" + eph_pub_raw,
    ).derive(shared_secret)


def seal_file(in_path: Path, out_path: Path, public_key_b64: str) -> None:
    pub_raw = base64.b64decode(public_key_b64.strip(), validate=True)
    recipient_pub = X25519PublicKey.from_public_bytes(pub_raw)

    eph_priv = X25519PrivateKey.generate()
    eph_pub_raw = eph_priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    salt = os.urandom(16)
    nonce_prefix = os.urandom(_NONCE_PREFIX_LEN)
    key = _derive_aead_key(eph_priv.exchange(recipient_pub), eph_pub_raw, salt)
    aesgcm = AESGCM(key)

    header = _MAGIC + eph_pub_raw + salt + nonce_prefix  # 8 + 32 + 16 + 8 = 64 byte
    with in_path.open("rb") as src, out_path.open("wb") as dst:
        dst.write(header)
        seq = 0
        while True:
            chunk = src.read(_CHUNK_SIZE)
            if not chunk:
                # Frame finale vuoto autenticato per prevenire troncamento silenzioso
                nonce = nonce_prefix + struct.pack(">I", seq)
                aad = header + struct.pack(">II", seq, 1)
                ct = aesgcm.encrypt(nonce, b"", aad)
                dst.write(struct.pack(">IB", len(ct), 1) + ct)
                break
            nonce = nonce_prefix + struct.pack(">I", seq)
            aad = header + struct.pack(">II", seq, 0)
            ct = aesgcm.encrypt(nonce, chunk, aad)
            dst.write(struct.pack(">IB", len(ct), 0) + ct)
            seq += 1


def open_file(in_path: Path, out_path: Path, private_key_b64: str) -> None:
    priv_raw = base64.b64decode(private_key_b64.strip(), validate=True)
    priv = X25519PrivateKey.from_private_bytes(priv_raw)

    with in_path.open("rb") as src, out_path.open("wb") as dst:
        header = src.read(64)
        if len(header) != 64 or header[:8] != _MAGIC:
            raise ValueError("Intestazione backup cifrato non valida.")
        eph_pub_raw = header[8:40]
        salt = header[40:56]
        nonce_prefix = header[56:64]

        eph_pub = X25519PublicKey.from_public_bytes(eph_pub_raw)
        key = _derive_aead_key(priv.exchange(eph_pub), eph_pub_raw, salt)
        aesgcm = AESGCM(key)

        seq = 0
        saw_eof = False
        while True:
            frame_hdr = src.read(5)
            if not frame_hdr:
                break
            if len(frame_hdr) != 5:
                raise ValueError("Frame backup troncato o corrotto.")
            ct_len, is_final = struct.unpack(">IB", frame_hdr)
            ct = src.read(ct_len)
            if len(ct) != ct_len:
                raise ValueError("Payload frame incompleto.")
            nonce = nonce_prefix + struct.pack(">I", seq)
            aad = header + struct.pack(">II", seq, is_final)
            pt = aesgcm.decrypt(nonce, ct, aad)
            if is_final == 1:
                if pt != b"" or src.read(1) != b"":
                    raise ValueError("Dati residui oltre il terminatore autenticato.")
                saw_eof = True
                break
            dst.write(pt)
            seq += 1

        if not saw_eof:
            raise ValueError("Backup troncato: terminatore autenticato assente.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cifratura autenticata backup PostgreSQL (X25519+AES-256-GCM).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="Genera coppia chiavi (PRIVATE=<b64> PUBLIC=<b64>).")

    p_seal = sub.add_parser("seal", help="Cifra un file dump usando solo la chiave pubblica.")
    p_seal.add_argument("--in", dest="src", required=True, type=Path)
    p_seal.add_argument("--out", dest="dst", required=True, type=Path)
    p_seal.add_argument("--public-key", default=os.getenv("BACKUP_PUBLIC_KEY_B64", ""))

    p_open = sub.add_parser("open", help="Decifra e autentica un backup usando la chiave privata.")
    p_open.add_argument("--in", dest="src", required=True, type=Path)
    p_open.add_argument("--out", dest="dst", required=True, type=Path)
    p_open.add_argument("--private-key", default=os.getenv("BACKUP_PRIVATE_KEY_B64", ""))

    args = parser.parse_args(argv)
    if args.cmd == "keygen":
        priv_b64, pub_b64 = generate_backup_keypair()
        print(f"BACKUP_PRIVATE_KEY_B64={priv_b64}")
        print(f"BACKUP_PUBLIC_KEY_B64={pub_b64}")
        return 0
    if args.cmd == "seal":
        if not args.public_key:
            print("BACKUP_PUBLIC_KEY_B64 mancante.", file=sys.stderr)
            return 2
        seal_file(args.src, args.dst, args.public_key)
        return 0
    if args.cmd == "open":
        if not args.private_key:
            print("BACKUP_PRIVATE_KEY_B64 mancante.", file=sys.stderr)
            return 2
        open_file(args.src, args.dst, args.private_key)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
