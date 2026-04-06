"""
virtio-bridge client (runs on the VM side).

Starts an HTTP proxy server that accepts requests from local applications,
writes them to the shared filesystem, and waits for responses from the
host-side server.

Applications connect to the client as if it were a normal HTTP server.
"""

import json
import logging
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from io import BytesIO

from .protocol import (
    BridgeDirectory,
    BridgeRequest,
    BridgeResponse,
    DEFAULT_TIMEOUT,
)

logger = logging.getLogger("virtio-bridge.client")


class BridgeProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler that relays requests through the filesystem bridge."""

    # Reference to the BridgeDirectory, set by BridgeClient
    bridge: BridgeDirectory
    target_host: str = "localhost"
    target_url: Optional[str] = None  # Per-client target URL for multi-backend routing
    timeout: float = DEFAULT_TIMEOUT

    def do_GET(self):
        self._proxy_request("GET")

    def do_POST(self):
        self._proxy_request("POST")

    def do_PUT(self):
        self._proxy_request("PUT")

    def do_DELETE(self):
        self._proxy_request("DELETE")

    def do_PATCH(self):
        self._proxy_request("PATCH")

    def do_HEAD(self):
        self._proxy_request("HEAD")

    def do_OPTIONS(self):
        self._proxy_request("OPTIONS")

    def _proxy_request(self, method: str) -> None:
        """Relay an HTTP request through the filesystem bridge."""
        # Read request body if present
        content_length = int(self.headers.get("Content-Length", 0))
        body = None
        if content_length > 0:
            body = self.rfile.read(content_length).decode("utf-8", errors="replace")

        # Determine if streaming is requested
        # Check Accept header for streaming indicators
        accept = self.headers.get("Accept", "")
        is_stream = "text/event-stream" in accept

        # Also check if the request body asks for streaming (OpenAI-style)
        if body and not is_stream:
            try:
                body_json = json.loads(body)
                if isinstance(body_json, dict) and body_json.get("stream"):
                    is_stream = True
            except (json.JSONDecodeError, TypeError):
                pass

        # Build headers dict (skip hop-by-hop headers)
        skip_headers = {"host", "connection", "transfer-encoding", "keep-alive",
                       "proxy-authenticate", "proxy-authorization", "te", "trailers",
                       "upgrade"}
        headers = {}
        for key, value in self.headers.items():
            if key.lower() not in skip_headers:
                headers[key] = value

        # Create bridge request
        req = BridgeRequest(
            id="",  # auto-generated
            method=method,
            path=self.path,
            headers=headers,
            body=body,
            stream=is_stream,
            target=self.target_url,
        )

        logger.info(f"→ {method} {self.path} (id={req.id}, stream={is_stream})")

        # Write request to filesystem
        self.bridge.write_request(req)

        if is_stream:
            self._handle_streaming_response(req)
        else:
            self._handle_regular_response(req)

    def _handle_regular_response(self, req: BridgeRequest) -> None:
        """Wait for and return a regular response."""
        resp = self.bridge.wait_response(req.id, timeout=self.timeout)

        if resp is None:
            self.send_error(504, "Gateway Timeout: no response from host")
            return

        if resp.error:
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            error_body = json.dumps({"error": resp.error}).encode("utf-8")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        # Forward response
        self.send_response(resp.status)

        # Forward headers
        body_bytes = resp.body.encode("utf-8") if resp.body else b""
        skip_resp_headers = {"transfer-encoding", "connection", "content-length"}
        for key, value in resp.headers.items():
            if key.lower() not in skip_resp_headers:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()

        if body_bytes:
            self.wfile.write(body_bytes)

        logger.info(f"← {req.id} {resp.status} ({len(body_bytes)} bytes)")

    def _handle_streaming_response(self, req: BridgeRequest) -> None:
        """Stream response chunks as they appear."""
        # First check if there's an error response (non-streaming)
        # Give the server a moment to start
        time.sleep(0.05)

        error_resp = self.bridge.read_response(req.id)
        if error_resp and error_resp.error:
            self.send_response(error_resp.status)
            self.send_header("Content-Type", "application/json")
            error_body = json.dumps({"error": error_resp.error}).encode("utf-8")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            try:
                (self.bridge.responses_dir / f"{req.id}.json").unlink()
            except (FileNotFoundError, PermissionError):
                pass
            return

        # Send streaming response headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        total_bytes = 0
        try:
            for chunk in self.bridge.read_stream(req.id, timeout=self.timeout):
                # Write chunk in HTTP chunked encoding
                chunk_header = f"{len(chunk):x}\r\n".encode()
                self.wfile.write(chunk_header)
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                total_bytes += len(chunk)

            # Send final chunk
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning(f"Client disconnected during stream: {req.id}")
            # Clean up stream files
            self.bridge._cleanup_stream_files(req.id)

        logger.info(f"← {req.id} streamed ({total_bytes} bytes)")

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        logger.debug(f"{self.address_string()} - {format % args}")


class BridgeClient:
    """
    VM-side client that runs an HTTP proxy server and relays requests
    through the filesystem bridge.
    """

    def __init__(
        self,
        bridge_dir: str | Path,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8080,
        timeout: float = DEFAULT_TIMEOUT,
        target: str | None = None,
        crypto=None,
    ):
        self.bridge = BridgeDirectory(bridge_dir, crypto=crypto)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.timeout = timeout
        self.target = target
        self._server: Optional[HTTPServer] = None

    def start(self) -> None:
        """Start the proxy server. Blocks until stopped."""
        self.bridge.init()

        # Configure the handler class
        handler = type(
            "ConfiguredHandler",
            (BridgeProxyHandler,),
            {
                "bridge": self.bridge,
                "timeout": self.timeout,
                "target_url": self.target,
            },
        )

        self._server = HTTPServer(
            (self.listen_host, self.listen_port),
            handler,
        )

        target_info = f" → {self.target}" if self.target else ""
        logger.info(
            f"Client proxy started: http://{self.listen_host}:{self.listen_port}{target_info}"
        )
        logger.info(f"Bridge directory: {self.bridge.root}")
        logger.info(f"Applications can send requests to http://{self.listen_host}:{self.listen_port}")

        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Client interrupted")
        finally:
            self._server.server_close()
            logger.info("Client stopped")

    def stop(self) -> None:
        """Stop the proxy server."""
        if self._server:
            self._server.shutdown()


def run_client(
    bridge_dir: str,
    listen_host: str = "127.0.0.1",
    listen_port: int = 8080,
    timeout: float = DEFAULT_TIMEOUT,
    target: str | None = None,
    crypto=None,
) -> None:
    """Entry point for running the client."""
    client = BridgeClient(
        bridge_dir=bridge_dir,
        listen_host=listen_host,
        listen_port=listen_port,
        timeout=timeout,
        target=target,
        crypto=crypto,
    )

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        client.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    client.start()
