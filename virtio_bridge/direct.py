"""
Direct bridge client — no HTTP server, no listener.

Instead of starting a proxy server on localhost, this module provides a
Python API that writes directly to the bridge filesystem and polls for
responses.  Works in environments where long-running listener processes
are unreliable (e.g., Cowork VM).

Usage (Python API):
    from virtio_bridge.direct import DirectClient

    client = DirectClient("/path/to/.bridge", target="http://localhost:11436")
    resp = client.request("POST", "/memory/generate", body='{"type":"fact"}')
    print(resp.status, resp.body)

    # Streaming
    for chunk in client.stream("POST", "/v1/chat/completions", body=data):
        print(chunk.decode(), end="")

Usage (CLI):
    virtio-bridge direct -d .bridge -t http://localhost:11436 \\
        POST /memory/generate -b '{"type":"fact"}'
"""

import json
import logging
from pathlib import Path
from typing import Dict, Iterator, Optional

from .protocol import (
    BridgeDirectory,
    BridgeRequest,
    BridgeResponse,
    ExecRequest,
    ExecResponse,
    DEFAULT_TIMEOUT,
)

logger = logging.getLogger("virtio-bridge.direct")


class DirectClient:
    """
    No-listen bridge client.

    Writes request JSON directly to the bridge directory and polls for
    the response file.  No HTTP server, no port binding, no background
    process.
    """

    def __init__(
        self,
        bridge_dir: str | Path,
        target: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        crypto=None,
    ):
        self.bridge = BridgeDirectory(bridge_dir, crypto=crypto)
        self.target = target
        self.timeout = timeout
        self.bridge.init()

    def request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> BridgeResponse:
        """
        Send a request and wait for the response.

        Returns BridgeResponse on success.
        Raises TimeoutError if no response within *timeout* seconds.
        """
        req = BridgeRequest(
            id="",
            method=method,
            path=path,
            headers=headers or {},
            body=body,
            stream=False,
            target=self.target,
        )

        t = timeout or self.timeout
        logger.info(f"→ {method} {path} (id={req.id}, timeout={t}s)")
        self.bridge.write_request(req)

        resp = self.bridge.wait_response(req.id, timeout=t)
        if resp is None:
            raise TimeoutError(
                f"No response within {t}s for {req.id} ({method} {path})"
            )

        logger.info(f"← {req.id} {resp.status} ({len(resp.body or '')} bytes)")
        return resp

    def stream(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[bytes]:
        """
        Send a streaming request.  Yields chunks as they arrive.

        The bridge server writes chunks to ``{id}.stream`` and a
        ``{id}.done`` marker when finished.
        """
        req = BridgeRequest(
            id="",
            method=method,
            path=path,
            headers=headers or {},
            body=body,
            stream=True,
            target=self.target,
        )

        t = timeout or self.timeout
        logger.info(f"→ {method} {path} [stream] (id={req.id}, timeout={t}s)")
        self.bridge.write_request(req)
        yield from self.bridge.read_stream(req.id, timeout=t)
        logger.info(f"← {req.id} stream complete")

    # ---- convenience helpers ----

    def get(self, path: str, **kw) -> BridgeResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: str, **kw) -> BridgeResponse:
        return self.request("POST", path, body=body, headers={"Content-Type": "application/json"}, **kw)

    def exec(
        self,
        cmd: str,
        args: Optional[list] = None,
        cwd: str = ".",
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> ExecResponse:
        """
        Execute a command on the host (Mac) side via the bridge.

        The command is subject to the host's exec policy:
        - "allow" → executed immediately
        - "confirm" → macOS dialog shown, user must approve
        - "deny" → rejected without execution

        Returns ExecResponse with exit_code, stdout, stderr.
        Raises TimeoutError if no response within timeout seconds.
        """
        t = timeout or self.timeout
        req = ExecRequest(
            id="",
            type="exec",
            cmd=cmd,
            args=args or [],
            cwd=cwd,
            env=env,
            timeout=t,
        )

        logger.info(f"EXEC → {cmd} {' '.join(args or [])} in {cwd} (id={req.id})")
        self.bridge.write_request(req)

        resp = self.bridge.wait_exec_response(req.id, timeout=t + 10)  # Extra grace for confirm dialog
        if resp is None:
            raise TimeoutError(
                f"No exec response within {t + 10}s for {req.id} ({cmd})"
            )

        if resp.error:
            logger.info(f"EXEC ← {req.id} ERROR: {resp.error}")
        else:
            logger.info(f"EXEC ← {req.id} exit={resp.exit_code}")
        return resp


class DirectTcpClient:
    """
    No-listen TCP client.

    Creates TCP connections through the bridge filesystem without
    a SOCKS5 proxy.  Useful when both sides are Python.
    """

    def __init__(
        self,
        bridge_dir: str | Path,
        timeout: float = 10.0,
        crypto=None,
    ):
        from .tcp_protocol import TcpBridgeDirectory
        self.tcp_dir = TcpBridgeDirectory(bridge_dir, crypto=crypto)
        self.timeout = timeout
        self.tcp_dir.init()

    def connect(self, host: str, port: int, timeout: Optional[float] = None):
        """
        Open a TCP connection to *host*:*port* via the bridge.

        Returns a TcpConnection object.  Use its write_upstream / read_downstream /
        iter_downstream / close_upstream methods directly.

        Raises ConnectionError if the host-side relay cannot connect.
        Raises TimeoutError if no response within *timeout* seconds.
        """
        t = timeout or self.timeout
        conn = self.tcp_dir.new_connection()
        conn.create_connect_request(host, port)
        logger.info(f"TCP → {host}:{port} (id={conn.conn_id}, timeout={t}s)")

        ok = conn.wait_established(timeout=t)
        if not ok:
            err = conn.get_error()
            if err:
                raise ConnectionError(f"TCP connect to {host}:{port} failed: {err}")
            raise TimeoutError(f"TCP connect to {host}:{port} timed out after {t}s")

        logger.info(f"TCP ← {conn.conn_id} established")
        return conn


def run_direct(
    bridge_dir: str,
    method: str,
    path: str,
    body: Optional[str] = None,
    target: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    stream: bool = False,
    crypto=None,
) -> None:
    """CLI entry point for the *direct* subcommand."""
    client = DirectClient(
        bridge_dir=bridge_dir,
        target=target,
        timeout=timeout,
        crypto=crypto,
    )

    if stream:
        for chunk in client.stream(method, path, body=body):
            print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
        print()  # trailing newline
    else:
        resp = client.request(method, path, body=body)
        if resp.error:
            print(json.dumps({"error": resp.error}, ensure_ascii=False))
        elif resp.body:
            # Pretty-print JSON, fall back to raw text
            try:
                obj = json.loads(resp.body)
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            except (json.JSONDecodeError, TypeError):
                print(resp.body)


def run_exec(
    bridge_dir: str,
    cmd: str,
    args: list,
    cwd: str = ".",
    timeout: float = 30.0,
    crypto=None,
) -> None:
    """CLI entry point for the *exec* subcommand."""
    import sys

    client = DirectClient(
        bridge_dir=bridge_dir,
        timeout=timeout,
        crypto=crypto,
    )

    resp = client.exec(cmd=cmd, args=args, cwd=cwd, timeout=timeout)

    if resp.error:
        print(f"Error: {resp.error}", file=sys.stderr)
        sys.exit(1)

    if resp.stdout:
        print(resp.stdout, end="")
    if resp.stderr:
        print(resp.stderr, end="", file=sys.stderr)
    sys.exit(resp.exit_code)
