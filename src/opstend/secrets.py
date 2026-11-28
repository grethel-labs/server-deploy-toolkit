"""Encrypted secrets in the replicated state.

Workload passwords, API keys for AI providers and similar values are stored as
AES-256-GCM ciphertext under kind ``secret``. The data key exists only on cluster
nodes (``<data_dir>/cluster/data.key``, 0600). A joining node receives it encrypted
to its own key pair (ECDH P-256 + HKDF + AES-GCM), so a party that relays the
join exchange learns nothing.

Plain values never enter jobs, logs, the audit trail, Zorec context or API
responses; jobs refer to secrets by id.
"""

from __future__ import annotations

import base64
import os
import secrets as pysecrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO = b"opstend data key v1"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


@dataclass
class DataKey:
    key: bytes

    @classmethod
    def generate(cls) -> DataKey:
        return cls(AESGCM.generate_key(bit_length=256))

    @classmethod
    def load_or_create(cls, path: Path) -> DataKey:
        if path.exists():
            return cls(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = cls.generate()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key.key)
        return key

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.key)

    def encrypt(self, value: str, context: str) -> dict[str, str]:
        nonce = pysecrets.token_bytes(12)
        ct = AESGCM(self.key).encrypt(nonce, value.encode(), context.encode())
        return {"nonce": _b64(nonce), "ct": _b64(ct)}

    def decrypt(self, sealed: dict[str, str], context: str) -> str:
        return (
            AESGCM(self.key)
            .decrypt(_unb64(sealed["nonce"]), _unb64(sealed["ct"]), context.encode())
            .decode()
        )


# --- key transport for joining nodes ------------------------------------------------


def _derive(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(shared)


def seal_for(public_key_pem: bytes, data: bytes) -> dict[str, str]:
    """Encrypt `data` so only the holder of the matching private key can read it."""
    recipient = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(recipient, ec.EllipticCurvePublicKey):
        raise ValueError("an EC public key is required")
    ephemeral = ec.generate_private_key(recipient.curve)
    key = _derive(ephemeral.exchange(ec.ECDH(), recipient))
    nonce = pysecrets.token_bytes(12)
    epk = ephemeral.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "epk": epk.decode(),
        "nonce": _b64(nonce),
        "ct": _b64(AESGCM(key).encrypt(nonce, data, INFO)),
    }


def open_sealed(private_key: ec.EllipticCurvePrivateKey, sealed: dict[str, str]) -> bytes:
    epk = serialization.load_pem_public_key(sealed["epk"].encode())
    if not isinstance(epk, ec.EllipticCurvePublicKey):
        raise ValueError("invalid sealed data")
    key = _derive(private_key.exchange(ec.ECDH(), epk))
    return AESGCM(key).decrypt(_unb64(sealed["nonce"]), _unb64(sealed["ct"]), INFO)


# --- the store ------------------------------------------------------------------------


class Vault:
    """Secrets by id. Values are encrypted with the data key and bound to their id."""

    def __init__(self, store: Any, key: DataKey) -> None:
        self.store = store
        self.key = key

    def seal(self, secret_id: str, value: str, label: str, owner: str) -> dict[str, Any]:
        """Build the replicated document; the caller writes it through Raft."""
        if not value:
            raise ValueError("empty secret")
        return {
            "id": secret_id,
            "label": label,
            "owner": owner,
            "updated_at": datetime.now(UTC).isoformat(),
            "sealed": self.key.encrypt(value, f"secret:{secret_id}"),
        }

    def reveal(self, secret_id: str) -> str:
        doc = self.store.get("secret", secret_id)
        if doc is None:
            raise KeyError(f"secret {secret_id} not found")
        return self.key.decrypt(doc["sealed"], f"secret:{secret_id}")

    def describe(self) -> list[dict[str, Any]]:
        """Metadata only, never values."""
        return [{k: v for k, v in d.items() if k != "sealed"} for d in self.store.list("secret")]
