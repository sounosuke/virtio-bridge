"""
Protocol layer for virtio-bridge.

Defines the file-based request/response format and directory structure
used for communication between the client (VM) and server (host).

Directory layout:
    .bridge/
    ├── requests/       # Client writes, server reads
    │   ├── {uuid}.json
    │   └── ...
    ├── responses/      # Server writes, client reads
    │   ├── {uuid}.json      # Complete responses
    │   ├── {uuid}.stream    # Streaming responses (appended to)
    │   ├── {uuid}.done      # Signals stream completion
    │   └── ...
    └── .lock           # Optional advisory lock
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, Iterator


# Directory names within the bridge root
REQUESTS_DIR = "requests"
RESPONSES_DIR = "responses"

# File extensions
REQUEST_EXT = ".json"
RESPONSE_EXT = ".json"
STREAM_EXT = ".stream"
STREAM_DONE_EXT = ".done"

# Timeouts
DEFAULT_TIMEOUT = 30.0  # seconds
STREAM_POLL_INTERVAL = 0.01  # 10ms for streaming chunks
RESPONSE_POLL_INTERVAL = 0.05  # 50ms for response file appearance


@dataclass
class BridgeRequest:
    """An HTTP request to be relayed through the filesystem."""
    id: str
    method: str
    path: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None
    stream: bool = False
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "BridgeRequest":
        d = json.loads(data)
        return cls(**d)


@dataclass
class BridgeResponse:
    """An HTTP response relayed back through the filesystem."""
    id: str
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None
    error: Optional[str] = None
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "BridgeResponse":
        d = json.loads(data)
        return cls(**d)


class BridgeDirectory:
    """Manages the shared filesystem directory structure."""

    def __init__(self, bridge_dir: str | Path):
        self.root = Path(bridge_dir)
        self.requests_dir = self.root / REQUESTS_DIR
        self.responses_dir = self.root / RESPONSES_DIR

    def init(self) -> None:
        """Create directory structure if it doesn't exist."""
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)

    def cleanup_stale(self, max_age: float = 300.0) -> int:
        """Remove request/response files older than max_age seconds. Returns count removed."""
        now = time.time()
        removed = 0
        for d in (self.requests_dir, self.responses_dir):
            if not d.exists():
                continue
            for f in d.iterdir():
                try:
                    if now - f.stat().st_mtime > max_age:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        return removed

    # --- Request operations (client writes, server reads) ---

    def write_request(self, req: BridgeRequest) -> Path:
        """Write a request file. Returns the file path."""
        path = self.requests_dir / f"{req.id}{REQUEST_EXT}"
        # Write to temp file first, then rename for atomicity
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(req.to_json(), encoding="utf-8")
        tmp_path.rename(path)
        return path

    def read_request(self, req_id: str) -> Optional[BridgeRequest]:
        """Read a request file by ID."""
        path = self.requests_dir / f"{req_id}{REQUEST_EXT}"
        try:
            data = path.read_text(encoding="utf-8")
            return BridgeRequest.from_json(data)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def consume_request(self, req_id: str) -> Optional[BridgeRequest]:
        """Read and delete a request file (atomic consume)."""
        req = self.read_request(req_id)
        if req:
            try:
                (self.requests_dir / f"{req_id}{REQUEST_EXT}").unlink()
            except FileNotFoundError:
                pass
        return req

    def list_request_ids(self) -> list[str]:
        """List all pending request IDs, sorted by modification time."""
        try:
            files = sorted(
                self.requests_dir.glob(f"*{REQUEST_EXT}"),
                key=lambda f: f.stat().st_mtime,
            )
            return [f.stem for f in files]
        except OSError:
            return []

    # --- Response operations (server writes, client reads) ---

    def write_response(self, resp: BridgeResponse) -> Path:
        """Write a complete response file. Returns the file path."""
        path = self.responses_dir / f"{resp.id}{RESPONSE_EXT}"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(resp.to_json(), encoding="utf-8")
        tmp_path.rename(path)
        return path

    def read_response(self, req_id: str) -> Optional[BridgeResponse]:
        """Read a response file by request ID."""
        path = self.responses_dir / f"{req_id}{RESPONSE_EXT}"
        try:
            data = path.read_text(encoding="utf-8")
            return BridgeResponse.from_json(data)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def wait_response(self, req_id: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[BridgeResponse]:
        """Wait for a response file to appear. Returns None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.read_response(req_id)
            if resp is not None:
                # Clean up
                try:
                    (self.responses_dir / f"{req_id}{RESPONSE_EXT}").unlink()
                except FileNotFoundError:
                    pass
                return resp
            time.sleep(RESPONSE_POLL_INTERVAL)
        return None

    # --- Streaming response operations ---

    def append_stream(self, req_id: str, chunk: bytes) -> None:
        """Append a chunk to a streaming response file."""
        path = self.responses_dir / f"{req_id}{STREAM_EXT}"
        with open(path, "ab") as f:
            f.write(chunk)
            f.flush()
            os.fsync(f.fileno())

    def finish_stream(self, req_id: str, status: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        """Signal that streaming is complete by writing a .done file."""
        done_path = self.responses_dir / f"{req_id}{STREAM_DONE_EXT}"
        meta = {"id": req_id, "status": status, "headers": headers or {}, "timestamp": time.time()}
        done_path.write_text(json.dumps(meta), encoding="utf-8")

    def read_stream(self, req_id: str, timeout: float = DEFAULT_TIMEOUT) -> Iterator[bytes]:
        """
        Read a streaming response, yielding chunks as they appear.
        Blocks until the .done file appears or timeout.
        """
        stream_path = self.responses_dir / f"{req_id}{STREAM_EXT}"
        done_path = self.responses_dir / f"{req_id}{STREAM_DONE_EXT}"
        deadline = time.time() + timeout
        pos = 0

        while time.time() < deadline:
            # Read any new data
            if stream_path.exists():
                with open(stream_path, "rb") as f:
                    f.seek(pos)
                    data = f.read()
                    if data:
                        pos += len(data)
                        yield data
                        # Reset deadline on activity
                        deadline = time.time() + timeout

            # Check if stream is complete
            if done_path.exists():
                # Read any remaining data
                if stream_path.exists():
                    with open(stream_path, "rb") as f:
                        f.seek(pos)
                        data = f.read()
                        if data:
                            yield data
                # Cleanup
                self._cleanup_stream_files(req_id)
                return

            time.sleep(STREAM_POLL_INTERVAL)

        # Timeout - clean up
        self._cleanup_stream_files(req_id)

    def _cleanup_stream_files(self, req_id: str) -> None:
        """Remove streaming files for a request."""
        for ext in (STREAM_EXT, STREAM_DONE_EXT):
            try:
                (self.responses_dir / f"{req_id}{ext}").unlink()
            except FileNotFoundError:
                pass
