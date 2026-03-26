"""
TCP connection protocol over shared filesystem.

Each TCP connection is represented as a directory under .bridge/tcp/:

    .bridge/tcp/
    └── {conn_id}/
        ├── connect.json     # Connection request (client → server)
        ├── established       # Empty file: connection accepted (server → client)
        ├── error.json       # Connection error (server → client)
        ├── upstream.bin     # Data: client → target (append-only)
        ├── downstream.bin   # Data: target → client (append-only)
        ├── close_up         # Client closed its write end
        └── close_down       # Server closed its write end

Lifecycle:
    1. Client creates {conn_id}/ and writes connect.json
    2. Server detects connect.json, opens real TCP connection
    3. Server writes 'established' on success, 'error.json' on failure
    4. Both sides stream data via upstream.bin / downstream.bin (append + tail)
    5. Either side writes close_up/close_down to signal EOF
    6. When both closed, directory can be cleaned up
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Iterator

TCP_DIR = "tcp"
CONNECT_FILE = "connect.json"
ESTABLISHED_FILE = "established"
ERROR_FILE = "error.json"
UPSTREAM_FILE = "upstream.bin"
DOWNSTREAM_FILE = "downstream.bin"
CLOSE_UP_FILE = "close_up"
CLOSE_DOWN_FILE = "close_down"

STREAM_READ_INTERVAL = 0.005  # 5ms - aggressive for interactive use
CONNECT_POLL_INTERVAL = 0.01  # 10ms
CONNECT_TIMEOUT = 10.0  # seconds


@dataclass
class TcpConnectRequest:
    """Request to establish a TCP connection."""
    conn_id: str
    host: str
    port: int
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.conn_id:
            self.conn_id = uuid.uuid4().hex[:12]
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "TcpConnectRequest":
        return cls(**json.loads(data))


class TcpConnection:
    """
    Manages a single TCP connection over the filesystem.
    Used by both client (VM) and server (host) sides.
    """

    def __init__(self, tcp_dir: Path, conn_id: str, crypto=None):
        self.conn_dir = tcp_dir / conn_id
        self.conn_id = conn_id
        self.crypto = crypto  # Optional BridgeCrypto instance
        self._upstream_pos = 0
        self._downstream_pos = 0
        self._upstream_buf = b""
        self._downstream_buf = b""

    @property
    def connect_path(self) -> Path:
        return self.conn_dir / CONNECT_FILE

    @property
    def established_path(self) -> Path:
        return self.conn_dir / ESTABLISHED_FILE

    @property
    def error_path(self) -> Path:
        return self.conn_dir / ERROR_FILE

    @property
    def upstream_path(self) -> Path:
        return self.conn_dir / UPSTREAM_FILE

    @property
    def downstream_path(self) -> Path:
        return self.conn_dir / DOWNSTREAM_FILE

    @property
    def close_up_path(self) -> Path:
        return self.conn_dir / CLOSE_UP_FILE

    @property
    def close_down_path(self) -> Path:
        return self.conn_dir / CLOSE_DOWN_FILE

    # --- Connection setup ---

    def create_connect_request(self, host: str, port: int) -> TcpConnectRequest:
        """Client side: create connection directory and write connect request."""
        self.conn_dir.mkdir(parents=True, exist_ok=True)
        req = TcpConnectRequest(conn_id=self.conn_id, host=host, port=port)
        if self.crypto:
            path = self.connect_path.with_suffix(".enc")
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(self.crypto.encrypt_text(req.to_json()))
            tmp.rename(path)
        else:
            tmp = self.connect_path.with_suffix(".tmp")
            tmp.write_text(req.to_json(), encoding="utf-8")
            tmp.rename(self.connect_path)
        return req

    def read_connect_request(self) -> Optional[TcpConnectRequest]:
        """Server side: read connect request."""
        try:
            if self.crypto:
                data = self.crypto.decrypt_text(
                    self.connect_path.with_suffix(".enc").read_bytes()
                )
                if data is None:
                    return None
            else:
                data = self.connect_path.read_text(encoding="utf-8")
            return TcpConnectRequest.from_json(data)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def signal_established(self) -> None:
        """Server side: signal that connection is established."""
        self.established_path.touch()

    def signal_error(self, message: str) -> None:
        """Server side: signal connection error."""
        data = json.dumps({"error": message, "timestamp": time.time()})
        if self.crypto:
            self.error_path.write_bytes(self.crypto.encrypt_text(data))
        else:
            self.error_path.write_text(data, encoding="utf-8")

    def wait_established(self, timeout: float = CONNECT_TIMEOUT) -> bool:
        """Client side: wait for connection to be established. Returns False on error/timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.established_path.exists():
                return True
            if self.error_path.exists():
                return False
            time.sleep(CONNECT_POLL_INTERVAL)
        return False

    def get_error(self) -> Optional[str]:
        """Read error message if any."""
        try:
            if self.crypto:
                text = self.crypto.decrypt_text(self.error_path.read_bytes())
                if text is None:
                    return None
                data = json.loads(text)
            else:
                data = json.loads(self.error_path.read_text(encoding="utf-8"))
            return data.get("error", "Unknown error")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    # --- Data transfer ---

    def write_upstream(self, data: bytes) -> None:
        """Client side: send data to target."""
        self._write_stream(self.upstream_path, data)

    def write_downstream(self, data: bytes) -> None:
        """Server side: send data to client."""
        self._write_stream(self.downstream_path, data)

    def _write_stream(self, path: Path, data: bytes) -> None:
        """Write data to a stream file, encrypting if crypto is enabled."""
        with open(path, "ab") as f:
            if self.crypto:
                encrypted = self.crypto.encrypt(data)
                f.write(len(encrypted).to_bytes(4, "big"))
                f.write(encrypted)
            else:
                f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def read_upstream(self) -> bytes:
        """Server side: read new upstream data since last read."""
        if self.crypto:
            return self._read_incremental_encrypted(self.upstream_path, "_upstream_pos", "_upstream_buf")
        return self._read_incremental(self.upstream_path, "_upstream_pos")

    def read_downstream(self) -> bytes:
        """Client side: read new downstream data since last read."""
        if self.crypto:
            return self._read_incremental_encrypted(self.downstream_path, "_downstream_pos", "_downstream_buf")
        return self._read_incremental(self.downstream_path, "_downstream_pos")

    def _read_incremental(self, path: Path, pos_attr: str) -> bytes:
        """Read new data from an append-only file since last position."""
        pos = getattr(self, pos_attr)
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
                if data:
                    setattr(self, pos_attr, pos + len(data))
                return data
        except FileNotFoundError:
            return b""

    def _read_incremental_encrypted(self, path: Path, pos_attr: str, buf_attr: str) -> bytes:
        """Read and decrypt new data from an encrypted stream file."""
        pos = getattr(self, pos_attr)
        buf = getattr(self, buf_attr)
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                new_data = f.read()
                if new_data:
                    setattr(self, pos_attr, pos + len(new_data))
                    buf += new_data
        except FileNotFoundError:
            return b""

        # Parse complete chunks
        result = b""
        while len(buf) >= 4:
            chunk_len = int.from_bytes(buf[:4], "big")
            if len(buf) < 4 + chunk_len:
                break
            encrypted = buf[4:4 + chunk_len]
            buf = buf[4 + chunk_len:]
            plaintext = self.crypto.decrypt(encrypted)
            if plaintext is not None:
                result += plaintext

        setattr(self, buf_attr, buf)
        return result

    def iter_downstream(self, timeout: float = 30.0) -> Iterator[bytes]:
        """Client side: iterate over downstream data chunks."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.read_downstream()
            if data:
                yield data
                deadline = time.time() + timeout  # Reset on activity
            elif self.is_down_closed:
                return
            else:
                time.sleep(STREAM_READ_INTERVAL)

    def iter_upstream(self, timeout: float = 30.0) -> Iterator[bytes]:
        """Server side: iterate over upstream data chunks."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.read_upstream()
            if data:
                yield data
                deadline = time.time() + timeout
            elif self.is_up_closed:
                return
            else:
                time.sleep(STREAM_READ_INTERVAL)

    # --- Connection teardown ---

    def close_upstream(self) -> None:
        """Client side: signal write end closed."""
        self.close_up_path.touch()

    def close_downstream(self) -> None:
        """Server side: signal write end closed."""
        self.close_down_path.touch()

    @property
    def is_up_closed(self) -> bool:
        return self.close_up_path.exists()

    @property
    def is_down_closed(self) -> bool:
        return self.close_down_path.exists()

    @property
    def is_fully_closed(self) -> bool:
        return self.is_up_closed and self.is_down_closed

    def cleanup(self) -> None:
        """Remove all files for this connection."""
        if self.conn_dir.exists():
            for f in self.conn_dir.iterdir():
                try:
                    f.unlink()
                except (FileNotFoundError, PermissionError):
                    pass
            try:
                self.conn_dir.rmdir()
            except (OSError, PermissionError):
                pass


class TcpBridgeDirectory:
    """Manages the TCP section of the bridge directory."""

    def __init__(self, bridge_root: str | Path, crypto=None):
        self.root = Path(bridge_root)
        self.tcp_dir = self.root / TCP_DIR
        self.crypto = crypto

    def init(self) -> None:
        self.tcp_dir.mkdir(parents=True, exist_ok=True)

    def new_connection(self, conn_id: str = "") -> TcpConnection:
        if not conn_id:
            conn_id = uuid.uuid4().hex[:12]
        return TcpConnection(self.tcp_dir, conn_id, crypto=self.crypto)

    def list_pending_connections(self) -> list[str]:
        """List connection IDs that have a connect.json but no established/error."""
        pending = []
        try:
            for d in self.tcp_dir.iterdir():
                if not d.is_dir():
                    continue
                # Check for both plaintext and encrypted connect files
                connect = d / CONNECT_FILE
                connect_enc = d / "connect.enc"
                established = d / ESTABLISHED_FILE
                error = d / ERROR_FILE
                has_connect = connect.exists() or connect_enc.exists()
                if has_connect and not established.exists() and not error.exists():
                    pending.append(d.name)
        except OSError:
            pass
        return sorted(pending)

    def cleanup_stale(self, max_age: float = 300.0) -> int:
        """Remove connection directories older than max_age seconds."""
        now = time.time()
        removed = 0
        try:
            for d in self.tcp_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    if now - d.stat().st_mtime > max_age:
                        conn = TcpConnection(self.tcp_dir, d.name)
                        conn.cleanup()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        return removed
