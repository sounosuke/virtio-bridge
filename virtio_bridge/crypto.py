"""
Encryption layer for virtio-bridge.

Provides AES-256-GCM encryption for all data passing through the bridge directory.
Each file/chunk gets a unique nonce to prevent replay attacks.

Two modes:
  1. Passphrase mode (--secret): PBKDF2 key derivation from shared secret
  2. DH mode (--auto-encrypt): X25519 ECDH key exchange, zero-config

Requires: pip install virtio-bridge[crypto]
  (which installs the 'cryptography' package)

File format (encrypted):
    [12-byte nonce][ciphertext][16-byte GCM auth tag]

Key derivation (passphrase mode):
    PBKDF2-HMAC-SHA256 with a fixed salt derived from the secret itself.

Key derivation (DH mode):
    X25519 ECDH shared secret → HKDF-SHA256 → AES-256 key.
    Public keys exchanged via .keys/ directory in the bridge root.
"""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("virtio-bridge.crypto")

NONCE_SIZE = 12  # AES-GCM standard nonce size
TAG_SIZE = 16    # AES-GCM authentication tag size
KEY_SIZE = 32    # AES-256
PBKDF2_ITERATIONS = 100_000

# DH key exchange constants
KEYS_DIR = ".keys"
HOST_PUBKEY_FILE = "host.pub"
VM_PUBKEY_FILE = "vm.pub"


def _ensure_cryptography():
    """Import and return cryptography modules, raising a clear error if not installed."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError:
        raise ImportError(
            "Encryption requires the 'cryptography' package. "
            "Install it with: pip install virtio-bridge[crypto]"
        )


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


def _derive_key_from_shared_secret(shared_secret: bytes) -> bytes:
    """Derive AES-256 key from ECDH shared secret using HKDF."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=b"virtio-bridge-dh-v1",
        info=b"virtio-bridge-aes-key",
    ).derive(shared_secret)


class BridgeCrypto:
    """Encrypts and decrypts bridge data using AES-256-GCM.

    Usage:
        crypto = BridgeCrypto("my-shared-secret")
        encrypted = crypto.encrypt(b"hello world")
        decrypted = crypto.decrypt(encrypted)
    """

    def __init__(self, secret: str):
        AESGCM = _ensure_cryptography()
        self._key = _derive_key(secret)
        self._aesgcm = AESGCM(self._key)

    @classmethod
    def from_key(cls, key: bytes) -> "BridgeCrypto":
        """Create BridgeCrypto from a raw 32-byte key (used by DH mode)."""
        AESGCM = _ensure_cryptography()
        obj = cls.__new__(cls)
        obj._key = key
        obj._aesgcm = AESGCM(key)
        return obj

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


class DHKeyExchange:
    """X25519 Diffie-Hellman key exchange over the filesystem.

    Each side generates an X25519 keypair, writes its public key to the
    bridge directory, and reads the peer's public key. The shared secret
    is derived via ECDH + HKDF, producing a BridgeCrypto instance.

    Public keys are stored in:
        .bridge/.keys/host.pub   (written by server/tcp-relay)
        .bridge/.keys/vm.pub     (written by client/socks)

    Private keys exist ONLY in process memory.

    Usage:
        # Host side:
        dh = DHKeyExchange(bridge_dir, role="host")
        crypto = dh.negotiate(timeout=30)  # blocks until peer key appears

        # VM side:
        dh = DHKeyExchange(bridge_dir, role="vm")
        crypto = dh.negotiate(timeout=30)
    """

    def __init__(self, bridge_dir: str | Path, role: str):
        """
        Args:
            bridge_dir: Path to the bridge root directory.
            role: "host" (server/tcp-relay) or "vm" (client/socks).
        """
        if role not in ("host", "vm"):
            raise ValueError(f"role must be 'host' or 'vm', got '{role}'")

        _ensure_cryptography()
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        self.bridge_dir = Path(bridge_dir)
        self.role = role
        self.keys_dir = self.bridge_dir / KEYS_DIR

        # Generate keypair
        self._private_key = X25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()

        # Determine which files we write and read
        if role == "host":
            self._my_pubkey_file = HOST_PUBKEY_FILE
            self._peer_pubkey_file = VM_PUBKEY_FILE
        else:
            self._my_pubkey_file = VM_PUBKEY_FILE
            self._peer_pubkey_file = HOST_PUBKEY_FILE

        self._peer_pubkey_bytes: Optional[bytes] = None

    def negotiate(self, timeout: float = 30.0) -> BridgeCrypto:
        """Write our public key and wait for the peer's key.

        Blocks until the peer's public key appears or timeout expires.
        Returns a BridgeCrypto ready for encryption/decryption.

        Raises TimeoutError if the peer key doesn't appear in time.
        """
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        from cryptography.hazmat.primitives import serialization

        # Ensure keys directory exists
        self.keys_dir.mkdir(parents=True, exist_ok=True)

        # Write our public key (atomic: tmp + rename)
        my_pub_bytes = self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        my_pub_path = self.keys_dir / self._my_pubkey_file
        tmp_path = my_pub_path.with_suffix(".tmp")
        tmp_path.write_bytes(my_pub_bytes)
        tmp_path.rename(my_pub_path)

        logger.info(f"DH: wrote {self._my_pubkey_file} ({len(my_pub_bytes)} bytes)")

        # Wait for peer's public key
        peer_pub_path = self.keys_dir / self._peer_pubkey_file
        deadline = time.time() + timeout

        while time.time() < deadline:
            if peer_pub_path.exists():
                try:
                    peer_bytes = peer_pub_path.read_bytes()
                    if len(peer_bytes) == 32:  # X25519 public key is 32 bytes
                        self._peer_pubkey_bytes = peer_bytes
                        break
                except (FileNotFoundError, PermissionError):
                    pass
            time.sleep(0.1)

        if self._peer_pubkey_bytes is None:
            raise TimeoutError(
                f"DH: peer key '{self._peer_pubkey_file}' not found within {timeout}s. "
                f"Is the other side running with --auto-encrypt?"
            )

        logger.info(f"DH: read {self._peer_pubkey_file}")

        # Derive shared secret
        peer_public_key = X25519PublicKey.from_public_bytes(self._peer_pubkey_bytes)
        shared_secret = self._private_key.exchange(peer_public_key)
        aes_key = _derive_key_from_shared_secret(shared_secret)

        logger.info("DH: shared secret derived, encryption ready")

        return BridgeCrypto.from_key(aes_key)

    def check_peer_key_changed(self) -> Optional[BridgeCrypto]:
        """Check if the peer's public key has changed. Returns new BridgeCrypto if so.

        Call this periodically to handle peer restarts. Returns None if
        the key hasn't changed or the peer key doesn't exist.
        """
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

        peer_pub_path = self.keys_dir / self._peer_pubkey_file
        try:
            peer_bytes = peer_pub_path.read_bytes()
        except (FileNotFoundError, PermissionError):
            return None

        if len(peer_bytes) != 32:
            return None

        if peer_bytes == self._peer_pubkey_bytes:
            return None  # No change

        # Key changed — re-derive
        logger.info(f"DH: peer key changed, re-negotiating")
        self._peer_pubkey_bytes = peer_bytes

        # Generate new keypair too (forward secrecy on restart)
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        self._private_key = X25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()

        # Write new public key
        my_pub_bytes = self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        my_pub_path = self.keys_dir / self._my_pubkey_file
        tmp_path = my_pub_path.with_suffix(".tmp")
        tmp_path.write_bytes(my_pub_bytes)
        tmp_path.rename(my_pub_path)

        peer_public_key = X25519PublicKey.from_public_bytes(peer_bytes)
        shared_secret = self._private_key.exchange(peer_public_key)
        aes_key = _derive_key_from_shared_secret(shared_secret)

        logger.info("DH: new shared secret derived")
        return BridgeCrypto.from_key(aes_key)
