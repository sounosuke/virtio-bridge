"""
virtio-bridge server (runs on the host / Mac side).

Watches the shared directory for incoming request files,
forwards them to the target HTTP server, and writes responses back.

Supports both regular and streaming responses.
"""

import json
import logging
import signal
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from http.client import HTTPResponse

from .protocol import (
    BridgeDirectory,
    BridgeRequest,
    BridgeResponse,
)
from .security import LOCAL_HOSTS, parse_allow_hosts, validate_target_url
from .watcher import FileWatcher

logger = logging.getLogger("virtio-bridge.server")


def _is_safe_path(path: str) -> bool:
    """Validate that a request path is safe to forward."""
    if not path or not path.startswith("/"):
        return False
    # Block path traversal
    if ".." in path:
        return False
    # Block null bytes
    if "\x00" in path:
        return False
    return True


class BridgeServer:
    """
    Host-side server that watches for request files and forwards them
    to a target HTTP server.

    Supports per-request target routing: if a request includes a ``target``
    field, that URL is used instead of the server's default.  This allows a
    single server process to relay to multiple backends (e.g. LLM on :11434
    and embedding on :11435).
    """

    def __init__(
        self,
        bridge_dir: str | Path,
        target: str | None = None,
        workers: int = 4,
        allow_hosts: frozenset[str] | None = None,
        crypto=None,
    ):
        self.bridge = BridgeDirectory(bridge_dir, crypto=crypto)
        self.target = target.rstrip("/") if target else None
        self.workers = workers
        self.allow_hosts = allow_hosts or LOCAL_HOSTS
        self._running = False
        self._watcher: Optional[FileWatcher] = None

        # Validate default target against allow list at startup (if given)
        if self.target:
            validate_target_url(self.target, self.allow_hosts)

    def _resolve_target(self, req: BridgeRequest) -> str:
        """Resolve the target URL for a request.

        Priority: req.target > self.target.
        Raises ValueError if no target is available or the host is not allowed.
        """
        target = req.target or self.target
        if not target:
            raise ValueError(
                "No target URL: request has no 'target' field and server has no --target default"
            )
        target = target.rstrip("/")
        validate_target_url(target, self.allow_hosts)
        return target

    def start(self) -> None:
        """Start the server. Blocks until stopped."""
        self.bridge.init()

        # Clean up stale files from previous runs
        removed = self.bridge.cleanup_stale(max_age=300)
        if removed:
            logger.info(f"Cleaned up {removed} stale files")

        # Process any existing requests first
        self._process_existing_requests()

        self._running = True
        watch_pattern = "*.enc" if self.bridge.crypto else "*.json"
        self._watcher = FileWatcher.create(self.bridge.requests_dir, pattern=watch_pattern)

        if self.target:
            logger.info(f"Server started: {self.bridge.root} → {self.target} (default)")
        else:
            logger.info(f"Server started: {self.bridge.root} (no default target, per-request routing)")
        logger.info(f"Watching for requests in: {self.bridge.requests_dir}")

        try:
            self._watcher.watch(self._on_request_file)
        except KeyboardInterrupt:
            logger.info("Server interrupted")
        finally:
            self._running = False
            logger.info("Server stopped")

    def stop(self) -> None:
        """Stop the server."""
        self._running = False
        if self._watcher:
            self._watcher.stop()

    def _process_existing_requests(self) -> None:
        """Process any request files that were written before the server started."""
        req_ids = self.bridge.list_request_ids()
        if req_ids:
            logger.info(f"Processing {len(req_ids)} existing requests")
            for req_id in req_ids:
                filepath = self.bridge.requests_dir / f"{req_id}.json"
                self._on_request_file(filepath)

    def _on_request_file(self, filepath: Path) -> None:
        """Called when a new request file is detected."""
        req_id = filepath.stem
        if req_id.endswith(".tmp") or filepath.suffix == ".tmp":
            return

        # Use threading for concurrent request handling
        t = threading.Thread(
            target=self._handle_request,
            args=(req_id,),
            daemon=True,
        )
        t.start()

    def _handle_request(self, req_id: str) -> None:
        """Handle a single request: read, forward, write response."""
        req = self.bridge.consume_request(req_id)
        if req is None:
            logger.warning(f"Request {req_id} disappeared before processing")
            return

        # Validate request path
        if not _is_safe_path(req.path):
            logger.warning(f"Rejected unsafe path: {req.path} (id={req_id})")
            error_resp = BridgeResponse(
                id=req_id,
                status=400,
                error="Invalid request path",
            )
            self.bridge.write_response(error_resp)
            return

        # Resolve target URL (per-request override or default)
        try:
            target = self._resolve_target(req)
        except ValueError as e:
            logger.warning(f"Rejected request {req_id}: {e}")
            error_resp = BridgeResponse(
                id=req_id,
                status=400,
                error=str(e),
            )
            self.bridge.write_response(error_resp)
            return

        logger.info(f"→ {req.method} {target}{req.path} (id={req_id}, stream={req.stream})")
        start = time.time()

        try:
            if req.stream:
                self._handle_streaming_request(req, target)
            else:
                self._handle_regular_request(req, target)
        except Exception as e:
            logger.error(f"Error handling {req_id}: {e}")
            error_resp = BridgeResponse(
                id=req_id,
                status=502,
                error=str(e),
            )
            self.bridge.write_response(error_resp)

        elapsed = time.time() - start
        logger.info(f"← {req_id} ({elapsed:.2f}s)")

    def _handle_regular_request(self, req: BridgeRequest, target: str) -> None:
        """Forward a regular (non-streaming) HTTP request."""
        url = f"{target}{req.path}"
        body = req.body.encode("utf-8") if req.body else None

        http_req = urllib.request.Request(
            url=url,
            data=body,
            headers=req.headers,
            method=req.method,
        )

        try:
            with urllib.request.urlopen(http_req, timeout=60) as http_resp:
                resp_body = http_resp.read().decode("utf-8", errors="replace")
                resp_headers = dict(http_resp.getheaders())

                resp = BridgeResponse(
                    id=req.id,
                    status=http_resp.status,
                    headers=resp_headers,
                    body=resp_body,
                )
        except urllib.error.HTTPError as e:
            resp_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            resp = BridgeResponse(
                id=req.id,
                status=e.code,
                headers=dict(e.headers.items()) if e.headers else {},
                body=resp_body,
            )
        except urllib.error.URLError as e:
            resp = BridgeResponse(
                id=req.id,
                status=502,
                error=f"Connection failed: {e.reason}",
            )

        self.bridge.write_response(resp)

    def _handle_streaming_request(self, req: BridgeRequest, target: str) -> None:
        """Forward a streaming HTTP request, relaying chunks via filesystem."""
        url = f"{target}{req.path}"
        body = req.body.encode("utf-8") if req.body else None

        http_req = urllib.request.Request(
            url=url,
            data=body,
            headers=req.headers,
            method=req.method,
        )

        try:
            http_resp: HTTPResponse = urllib.request.urlopen(http_req, timeout=120)
            resp_headers = dict(http_resp.getheaders())

            # Stream chunks to filesystem
            while True:
                chunk = http_resp.read(4096)
                if not chunk:
                    break
                self.bridge.append_stream(req.id, chunk)

            self.bridge.finish_stream(
                req.id,
                status=http_resp.status,
                headers=resp_headers,
            )
        except urllib.error.HTTPError as e:
            # For errors, write any body then finish
            if e.fp:
                error_body = e.read()
                if error_body:
                    self.bridge.append_stream(req.id, error_body)
            self.bridge.finish_stream(req.id, status=e.code)
        except urllib.error.URLError as e:
            # Connection error - write error as a regular response
            error_resp = BridgeResponse(
                id=req.id,
                status=502,
                error=f"Connection failed: {e.reason}",
            )
            self.bridge.write_response(error_resp)
        except Exception as e:
            error_resp = BridgeResponse(
                id=req.id,
                status=502,
                error=str(e),
            )
            self.bridge.write_response(error_resp)


def run_server(
    bridge_dir: str,
    target: str | None = None,
    allow_hosts: frozenset[str] | None = None,
    crypto=None,
) -> None:
    """Entry point for running the server."""
    server = BridgeServer(bridge_dir=bridge_dir, target=target, allow_hosts=allow_hosts, crypto=crypto)

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    server.start()
