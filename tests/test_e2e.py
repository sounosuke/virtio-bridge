"""End-to-end tests for HTTP relay and SOCKS5 TCP relay."""

import json
import socket
import struct
import threading
import time
import urllib.request

import pytest

from virtio_bridge.client import BridgeClient
from virtio_bridge.protocol import BridgeDirectory, BridgeResponse
from virtio_bridge.socks import SocksServer
from virtio_bridge.tcp_relay import TcpRelayServer


@pytest.fixture
def bridge_dir(tmp_path):
    return str(tmp_path / ".bridge")


class TestHttpRelay:
    """E2E tests for the HTTP proxy relay."""

    def test_regular_request(self, bridge_dir):
        """HTTP GET → filesystem → simulated server → response."""
        client = BridgeClient(
            bridge_dir=bridge_dir,
            listen_host="127.0.0.1",
            listen_port=0,  # Will pick a port after binding
            timeout=5.0,
        )
        # We need a fixed port for testing
        client_port = _find_free_port()
        client = BridgeClient(
            bridge_dir=bridge_dir,
            listen_host="127.0.0.1",
            listen_port=client_port,
            timeout=5.0,
        )
        bridge = BridgeDirectory(bridge_dir)

        # Start client in background
        client_thread = threading.Thread(target=client.start, daemon=True)
        client_thread.start()
        time.sleep(0.3)

        # Simulate server: watch for requests and write responses
        def fake_server():
            deadline = time.time() + 5
            while time.time() < deadline:
                ids = bridge.list_request_ids()
                for req_id in ids:
                    req = bridge.consume_request(req_id)
                    if req:
                        resp = BridgeResponse(
                            id=req.id,
                            status=200,
                            headers={"Content-Type": "application/json"},
                            body=json.dumps({"path": req.path, "method": req.method}),
                        )
                        bridge.write_response(resp)
                        return
                time.sleep(0.05)

        server_thread = threading.Thread(target=fake_server, daemon=True)
        server_thread.start()
        time.sleep(0.1)

        # Send request
        url = f"http://127.0.0.1:{client_port}/v1/models"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())

        assert body["path"] == "/v1/models"
        assert body["method"] == "GET"
        client.stop()

    def test_streaming_request(self, bridge_dir):
        """Streaming POST → filesystem → simulated server → SSE chunks."""
        client_port = _find_free_port()
        client = BridgeClient(
            bridge_dir=bridge_dir,
            listen_host="127.0.0.1",
            listen_port=client_port,
            timeout=10.0,
        )
        bridge = BridgeDirectory(bridge_dir)

        client_thread = threading.Thread(target=client.start, daemon=True)
        client_thread.start()
        time.sleep(0.3)

        def fake_streaming_server():
            deadline = time.time() + 5
            while time.time() < deadline:
                ids = bridge.list_request_ids()
                for req_id in ids:
                    req = bridge.consume_request(req_id)
                    if req and req.stream:
                        for i in range(3):
                            obj = {"choices": [{"delta": {"content": f"token{i}"}}]}
                            chunk = f"data: {json.dumps(obj)}\n\n"
                            bridge.append_stream(req.id, chunk.encode())
                            time.sleep(0.05)
                        bridge.append_stream(req.id, b"data: [DONE]\n\n")
                        bridge.finish_stream(req.id, status=200)
                        return
                time.sleep(0.05)

        server_thread = threading.Thread(target=fake_streaming_server, daemon=True)
        server_thread.start()
        time.sleep(0.1)

        body_data = json.dumps({
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }).encode()

        req = urllib.request.Request(
            f"http://127.0.0.1:{client_port}/v1/chat/completions",
            data=body_data,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode()

        assert "token0" in data
        assert "token1" in data
        assert "token2" in data
        assert "[DONE]" in data
        client.stop()


class TestSocksRelay:
    """E2E tests for the SOCKS5 TCP relay."""

    def test_socks5_connect(self, bridge_dir):
        """SOCKS5 CONNECT → filesystem → TCP relay → echo server."""
        # 1. Start echo server
        echo_port = _find_free_port()
        echo_thread = threading.Thread(
            target=_echo_server, args=(echo_port,), daemon=True
        )
        echo_thread.start()
        time.sleep(0.2)

        # 2. Start TCP relay (host side)
        relay = TcpRelayServer(bridge_dir=bridge_dir)
        relay_thread = threading.Thread(target=relay.start, daemon=True)
        relay_thread.start()
        time.sleep(0.2)

        # 3. Start SOCKS5 proxy (VM side)
        socks_port = _find_free_port()
        socks = SocksServer(bridge_dir=bridge_dir, listen_port=socks_port)
        socks_thread = threading.Thread(target=socks.start, daemon=True)
        socks_thread.start()
        time.sleep(0.2)

        # 4. Connect through SOCKS5
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(("127.0.0.1", socks_port))

        # SOCKS5 handshake
        sock.sendall(b"\x05\x01\x00")
        resp = sock.recv(2)
        assert resp == b"\x05\x00"

        # CONNECT
        target_host = b"127.0.0.1"
        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(target_host)]) + target_host
            + struct.pack("!H", echo_port)
        )
        resp = sock.recv(10)
        assert resp[1] == 0x00  # Success

        # Send data
        sock.sendall(b"hello from socks")
        time.sleep(0.5)
        data = sock.recv(4096)
        sock.close()

        assert b"hello from socks" in data

        relay.stop()
        socks.stop()

    def test_socks5_connect_refused(self, bridge_dir):
        """SOCKS5 CONNECT to a closed port should return error."""
        relay = TcpRelayServer(bridge_dir=bridge_dir)
        relay_thread = threading.Thread(target=relay.start, daemon=True)
        relay_thread.start()
        time.sleep(0.2)

        socks_port = _find_free_port()
        socks = SocksServer(bridge_dir=bridge_dir, listen_port=socks_port)
        socks_thread = threading.Thread(target=socks.start, daemon=True)
        socks_thread.start()
        time.sleep(0.2)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect(("127.0.0.1", socks_port))

        sock.sendall(b"\x05\x01\x00")
        resp = sock.recv(2)
        assert resp == b"\x05\x00"

        # Connect to a port that nothing is listening on
        closed_port = _find_free_port()
        target_host = b"127.0.0.1"
        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(target_host)]) + target_host
            + struct.pack("!H", closed_port)
        )
        resp = sock.recv(10)
        assert resp[1] != 0x00  # Should not be success
        sock.close()

        relay.stop()
        socks.stop()


# --- Helpers ---

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _echo_server(port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(10)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    try:
        conn, _ = srv.accept()
        data = conn.recv(4096)
        response = f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
        conn.sendall(response)
        time.sleep(0.2)
        conn.close()
    finally:
        srv.close()
