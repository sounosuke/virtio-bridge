#!/usr/bin/env python3
"""
Self-contained integration test for virtio-bridge.

Tests the full pipeline (HTTP relay + SOCKS5 relay) on a single machine
without VirtioFS or any external services.

Usage:
    python3 -m virtio_bridge.cli integration-test
    # or directly:
    python3 tests/test_integration.py
"""

import json
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Allow running directly: python3 tests/test_integration.py
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from virtio_bridge.server import BridgeServer
from virtio_bridge.client import BridgeClient
from virtio_bridge.socks import SocksServer
from virtio_bridge.tcp_relay import TcpRelayServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _EchoHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server that echoes request info back as JSON."""

    def do_GET(self):
        body = json.dumps({"method": "GET", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(length).decode() if length else ""
        body = json.dumps({"method": "POST", "path": self.path, "body": req_body}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress logs


class _TcpEchoServer:
    """Simple TCP server that echoes received data back."""

    def __init__(self, port: int):
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(10)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(5)
        self._running = True

    def serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
                conn.settimeout(5)
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)
                time.sleep(0.1)
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._running = False
        self._sock.close()


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _print(msg: str, ok: bool = True):
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {msg}")


def run_integration_test() -> bool:
    """Run full integration tests. Returns True if all pass."""
    passed = 0
    failed = 0
    tmpdir = tempfile.mkdtemp(prefix="virtio-bridge-test-")
    bridge_dir = str(Path(tmpdir) / ".bridge")

    print(f"\nvirtio-bridge integration test")
    print(f"bridge dir: {tmpdir}\n")

    # ------------------------------------------------------------------
    # 1. Start echo HTTP server
    # ------------------------------------------------------------------
    echo_port = _find_free_port()
    echo_srv = HTTPServer(("127.0.0.1", echo_port), _EchoHandler)
    threading.Thread(target=echo_srv.serve_forever, daemon=True).start()

    # ------------------------------------------------------------------
    # 2. Start bridge server (host side) → echo server
    # ------------------------------------------------------------------
    server = BridgeServer(bridge_dir=bridge_dir, target=f"http://127.0.0.1:{echo_port}")
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    # ------------------------------------------------------------------
    # 3. Start bridge client (VM side)
    # ------------------------------------------------------------------
    client_port = _find_free_port()
    client = BridgeClient(
        bridge_dir=bridge_dir,
        listen_host="127.0.0.1",
        listen_port=client_port,
        timeout=10.0,
    )
    threading.Thread(target=client.start, daemon=True).start()
    time.sleep(0.3)

    # ------------------------------------------------------------------
    # Test A: HTTP GET
    # ------------------------------------------------------------------
    print("HTTP mode:")
    try:
        url = f"http://127.0.0.1:{client_port}/v1/models"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        assert data["method"] == "GET"
        assert data["path"] == "/v1/models"
        _print("GET /v1/models → 200 OK")
        passed += 1
    except Exception as e:
        _print(f"GET /v1/models → {e}", ok=False)
        failed += 1

    # ------------------------------------------------------------------
    # Test B: HTTP POST
    # ------------------------------------------------------------------
    try:
        url = f"http://127.0.0.1:{client_port}/v1/chat/completions"
        payload = json.dumps({"model": "test", "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        assert data["method"] == "POST"
        assert data["path"] == "/v1/chat/completions"
        assert "test" in data["body"]
        _print("POST /v1/chat/completions → 200 OK")
        passed += 1
    except Exception as e:
        _print(f"POST /v1/chat/completions → {e}", ok=False)
        failed += 1

    # Clean up HTTP mode
    client.stop()
    server.stop()
    time.sleep(0.3)

    # ------------------------------------------------------------------
    # 4. SOCKS5 mode: tcp-relay + socks + TCP echo server
    # ------------------------------------------------------------------
    print("\nSOCKS5 mode:")

    tcp_echo_port = _find_free_port()
    tcp_echo = _TcpEchoServer(tcp_echo_port)
    threading.Thread(target=tcp_echo.serve, daemon=True).start()

    relay = TcpRelayServer(bridge_dir=bridge_dir)
    threading.Thread(target=relay.start, daemon=True).start()
    time.sleep(0.3)

    socks_port = _find_free_port()
    socks = SocksServer(bridge_dir=bridge_dir, listen_port=socks_port)
    threading.Thread(target=socks.start, daemon=True).start()
    time.sleep(0.3)

    # ------------------------------------------------------------------
    # Test C: SOCKS5 CONNECT → TCP echo
    # ------------------------------------------------------------------
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(("127.0.0.1", socks_port))

        # Handshake
        sock.sendall(b"\x05\x01\x00")
        assert sock.recv(2) == b"\x05\x00"

        # CONNECT to TCP echo server
        target = b"127.0.0.1"
        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(target)]) + target
            + struct.pack("!H", tcp_echo_port)
        )
        resp = sock.recv(10)
        assert resp[1] == 0x00  # success

        # Send + receive
        sock.sendall(b"hello via socks5")
        time.sleep(0.5)
        data = sock.recv(4096)
        sock.close()
        assert b"hello via socks5" in data
        _print(f"CONNECT 127.0.0.1:{tcp_echo_port} → echo OK")
        passed += 1
    except Exception as e:
        _print(f"CONNECT → {e}", ok=False)
        failed += 1

    # ------------------------------------------------------------------
    # Test D: SOCKS5 CONNECT to closed port → error
    # ------------------------------------------------------------------
    try:
        closed_port = _find_free_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect(("127.0.0.1", socks_port))

        sock.sendall(b"\x05\x01\x00")
        assert sock.recv(2) == b"\x05\x00"

        target = b"127.0.0.1"
        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(target)]) + target
            + struct.pack("!H", closed_port)
        )
        resp = sock.recv(10)
        assert resp[1] != 0x00  # should fail
        sock.close()
        _print(f"CONNECT to closed port → rejected")
        passed += 1
    except Exception as e:
        _print(f"CONNECT to closed port → {e}", ok=False)
        failed += 1

    # Clean up
    socks.stop()
    relay.stop()
    tcp_echo.stop()
    echo_srv.shutdown()
    time.sleep(0.3)

    # ------------------------------------------------------------------
    # 5. Encrypted mode: HTTP with --secret
    # ------------------------------------------------------------------
    has_crypto = False
    try:
        from virtio_bridge.crypto import BridgeCrypto
        has_crypto = True
    except ImportError:
        pass

    if has_crypto:
        print("\nEncrypted HTTP mode:")
        enc_tmpdir = tempfile.mkdtemp(prefix="virtio-bridge-enc-test-")
        enc_bridge_dir = str(Path(enc_tmpdir) / ".bridge")
        test_secret = "integration-test-secret-42"
        crypto = BridgeCrypto(test_secret)

        enc_echo_port = _find_free_port()
        enc_echo_srv = HTTPServer(("127.0.0.1", enc_echo_port), _EchoHandler)
        threading.Thread(target=enc_echo_srv.serve_forever, daemon=True).start()

        enc_server = BridgeServer(
            bridge_dir=enc_bridge_dir,
            target=f"http://127.0.0.1:{enc_echo_port}",
            crypto=crypto,
        )
        threading.Thread(target=enc_server.start, daemon=True).start()
        time.sleep(0.3)

        enc_client_port = _find_free_port()
        enc_client = BridgeClient(
            bridge_dir=enc_bridge_dir,
            listen_host="127.0.0.1",
            listen_port=enc_client_port,
            timeout=10.0,
            crypto=crypto,
        )
        threading.Thread(target=enc_client.start, daemon=True).start()
        time.sleep(0.3)

        # Test E: Encrypted HTTP GET
        try:
            url = f"http://127.0.0.1:{enc_client_port}/v1/models"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            assert data["method"] == "GET"
            assert data["path"] == "/v1/models"
            _print("GET /v1/models (encrypted) → 200 OK")
            passed += 1
        except Exception as e:
            _print(f"GET /v1/models (encrypted) → {e}", ok=False)
            failed += 1

        # Test F: Encrypted HTTP POST
        try:
            url = f"http://127.0.0.1:{enc_client_port}/v1/chat/completions"
            payload = json.dumps({"model": "enc-test", "messages": [{"role": "user", "content": "secret"}]}).encode()
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            assert data["method"] == "POST"
            assert "enc-test" in data["body"]
            _print("POST /v1/chat/completions (encrypted) → 200 OK")
            passed += 1
        except Exception as e:
            _print(f"POST /v1/chat/completions (encrypted) → {e}", ok=False)
            failed += 1

        # Test G: Verify files on disk are actually encrypted (not readable as JSON)
        try:
            from virtio_bridge.protocol import BridgeDirectory, BridgeRequest
            enc_bridge = BridgeDirectory(enc_bridge_dir, crypto=crypto)
            enc_bridge.init()
            test_req = BridgeRequest(id="", method="GET", path="/test-verify")
            written_path = enc_bridge.write_request(test_req)
            # The file should be .enc, not .json
            assert written_path.suffix == ".enc", f"Expected .enc, got {written_path.suffix}"
            # Raw bytes should not contain readable JSON
            raw = written_path.read_bytes()
            assert b'"method"' not in raw, "File content is not encrypted!"
            # But decrypting should work
            decrypted = crypto.decrypt_text(raw)
            assert '"method"' in decrypted
            written_path.unlink()
            _print("Files on disk are encrypted (not plaintext)")
            passed += 1
        except Exception as e:
            _print(f"Encryption verification → {e}", ok=False)
            failed += 1

        enc_client.stop()
        enc_server.stop()
        enc_echo_srv.shutdown()
    else:
        print("\nEncrypted mode: SKIPPED (cryptography package not installed)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total = passed + failed
    print(f"\n{'=' * 40}")
    if failed == 0:
        print(f"\033[32mAll {total} tests passed.\033[0m")
    else:
        print(f"\033[31m{failed}/{total} tests failed.\033[0m")

    return failed == 0


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    success = run_integration_test()
    sys.exit(0 if success else 1)
