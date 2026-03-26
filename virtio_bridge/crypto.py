"""
Encryption layer for virtio-bridge.

Provides AES-256-GCM encryption for all data passing through the bridge directory.
Each file/chunk gets a unique nonce to prevent replay attacks.

Requires: pip install virtio-bridge[crypto]
  (which installs the 'cryptography' package)

File format (encrypted):
    [12-byte nonce][ciphertext][16-byte GCM auth tag]

Key derivation:
    PBKDF2-HMAC-SHA256 with a fixed salt derived from the secret itself.
    The salt is deterministic so both sides derive the same key without
    needing to exchange anything beyond the shared secret.
"""

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger("virtio-bridge.crypto")

NONCE_SIZE = 12  # AES-GCM standard nonce size
TAG_SIZE = 16    # AES-GCM authentication tag size
KEY_SIZE = 32    # AES-256
PBKDF2_ITERATIONS = 100_000


def _derive_key(secret: str) -> bytes:
    """Derive a 256-bit encryption key from a shared secret using PBKDF2.

    Uses a deterministic salt derived from the secret so both sides
    produce the same key without exchanging additional data.
    """
    # Salt is SHA-256 of "virtio-bridge:" + secret
    # Deterministic but unique per secret
    salt = hashlib.sha256(f"virtio-bridge:{secret}".encode()).digest()
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=KEY_SIZE,
    )


class BridgeCrypto:
    """Encrypts and decrypts bridge data using AES-256-GCM.

    Usage:
        crypto = BridgeCrypto("my-shared-secret")
        encrypted = crypto.encrypt(b"hello world")
        decrypted = crypto.decrypt(encrypted)
    """

    def __init__(self, secret: str):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise ImportError(
                "Encryption requires the 'cryptography' package. "
                "Install it with: pip install virtio-bridge[crypto]"
            )
        self._key = _derive_key(secret)
        self._aesgcm = AESGCM(self._key)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data with a random nonce. Returns nonce + ciphertext + tag."""
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ciphertext

    def decrypt(self, data: bytes) -> Optional[bytes]:
        """Decrypt data. Returns None if decryption fails (wrong key, tampered data)."""
        if len(data) < NONCE_SIZE + TAG_SIZE:
            logger.warning("Data too short to decrypt")
            return None
        nonce = data[:NONCE_SIZE]
        ciphertext = data[NONCE_SIZE:]
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as e:
            logger.warning(f"Decryption failed: {e}")
            return None

    def encrypt_text(self, text: str) -> bytes:
        """Encrypt a text string. Returns encrypted bytes."""
        return self.encrypt(text.encode("utf-8"))

    def decrypt_text(self, data: bytes) -> Optional[str]:
        """Decrypt to a text string. Returns None on failure."""
        plaintext = self.decrypt(data)
        if plaintext is None:
            return None
        return plaintext.decode("utf-8")
