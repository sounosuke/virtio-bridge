"""
SOCKS5 proxy server (runs on the VM side).

Accepts SOCKS5 CONNECT requests from local applications,
relays TCP connections through the filesystem bridge.

Applications configure this as their SOCKS5 proxy:
    curl --socks5 127.0.0.1:1080 http://target:port/path
    ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:1080 %h %p' user@host

SOCKS5 protocol (RFC 1928):
    1. Client → Server: version(0x05), nmethods, methods[]
    2. Server → Client: version(0x05), method(0x00 = no auth)
    3. Client → Server: version(0x05), cmd(0x01=CONNECT), rsv, atyp, dst.addr, dst.port
    4. Server → Client: version(0x05), rep(0x00=success), rsv, atyp, bnd.addr, bnd.port
    5. Bidirectional data relay
"""

import logging
import select
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .tcp_protocol import TcpBridgeDirectory, TcpConnection, CONNECT_TIMEOUT

logger = logging.getLogger("virtio-bridge.socks")

# SOCKS5 constants
SOCKS_VERSION = 0x05
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04
REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_CONNECTION_REFUSED = 0x05
REP_NETWORK_UNREACHABLE = 0x03


class SocksHandler:
    """Handles a single SOCKS5 client connection."""

    def __init__(self, client_sock: socket.socket, addr: tuple, tcp_bridge: TcpBridgeDirectory):
        self.client = client_sock
        self.addr = addr
        self.tcp_bridge = tcp_bridge
        self.conn: Optional[TcpConnection] = None

    def handle(self) -> None:
        try:
            self._do_handshake()
        except Exception as e:
            logger.error(f"[{self.addr}] Error: {e}")
        finally:
            self.client.close()
            if self.conn:
                self.conn.close_upstream()

    def _do_handshake(self) -> None:
        # Step 1: Auth negotiation
        header = self._recv(2)
        if not header:
            return
        version, nmethods = struct.unpack("!BB", header)
        if version != SOCKS_VERSION:
            logger.warning(f"[{self.addr}] Unsupported SOCKS version: {version}")
            return

        methods = self._recv(nmethods)
        if not methods:
            return

        # Respond: no authentication required
        self.client.sendall(struct.pack("!BB", SOCKS_VERSION, 0x00))

        # Step 2: Connection request
        req_header = self._recv(4)
        if not req_header:
            return
        version, cmd, _, atyp = struct.unpack("!BBBB", req_header)

        if cmd != CMD_CONNECT:
            self._send_reply(REP_GENERAL_FAILURE)
            logger.warning(f"[{self.addr}] Unsupported command: {cmd}")
            return

        # Parse destination address
        dst_host, dst_port = self._parse_address(atyp)
        if dst_host is None:
            self._send_reply(REP_GENERAL_FAILURE)
            return

        logger.info(f"[{self.addr}] CONNECT {dst_host}:{dst_port}")

        # Step 3: Relay via filesystem bridge
        self.conn = self.tcp_bridge.new_connection()
        self.conn.create_connect_request(dst_host, dst_port)

        # Wait for server side to establish real connection
        if not self.conn.wait_established(timeout=CONNECT_TIMEOUT):
            error = self.conn.get_error() or "Connection timeout"
            logger.error(f"[{self.addr}] Connection failed: {error}")
            self._send_reply(REP_CONNECTION_REFUSED)
            self.conn.cleanup()
            self.conn = None
            return

        # Success
        self._send_reply(REP_SUCCESS)
        logger.info(f"[{self.addr}] Connected to {dst_host}:{dst_port} (conn={self.conn.conn_id})")

        # Step 4: Bidirectional relay
        self._relay()

    def _relay(self) -> None:
        """Bidirectional data relay between SOCKS client and filesystem bridge."""
        conn = self.conn
        client = self.client

        # Thread: read from client socket → write to upstream file
        def upstream_pump():
            try:
                while True:
                    try:
                        data = client.recv(8192)
                    except (socket.error, OSError):
                        break
                    if not data:
                        break
                    conn.write_upstream(data)
            finally:
                conn.close_upstream()

        # Thread: read from downstream file → write to client socket
        def downstream_pump():
            try:
                for chunk in conn.iter_downstream(timeout=60):
                    try:
                        client.sendall(chunk)
                    except (socket.error, OSError):
                        break
            finally:
                try:
                    client.shutdown(socket.SHUT_WR)
                except (socket.error, OSError):
                    pass

        up_thread = threading.Thread(target=upstream_pump, daemon=True)
        down_thread = threading.Thread(target=downstream_pump, daemon=True)
        up_thread.start()
        down_thread.start()

        # Wait for both directions to finish
        up_thread.join(timeout=120)
        down_thread.join(timeout=120)

        logger.info(f"[{self.addr}] Connection closed (conn={conn.conn_id})")

    def _parse_address(self, atyp: int) -> tuple[Optional[str], Optional[int]]:
        """Parse SOCKS5 address based on address type."""
        if atyp == ATYP_IPV4:
            raw = self._recv(4)
            if not raw:
                return None, None
            host = socket.inet_ntoa(raw)
        elif atyp == ATYP_DOMAIN:
            length_byte = self._recv(1)
            if not length_byte:
                return None, None
            length = length_byte[0]
            domain = self._recv(length)
            if not domain:
                return None, None
            host = domain.decode("ascii")
        elif atyp == ATYP_IPV6:
            raw = self._recv(16)
            if not raw:
                return None, None
            host = socket.inet_ntop(socket.AF_INET6, raw)
        else:
            return None, None

        port_data = self._recv(2)
        if not port_data:
            return None, None
        port = struct.unpack("!H", port_data)[0]
        return host, port

    def _send_reply(self, rep: int) -> None:
        """Send SOCKS5 reply."""
        # Reply with 0.0.0.0:0 as bound address
        reply = struct.pack("!BBBB", SOCKS_VERSION, rep, 0x00, ATYP_IPV4)
        reply += socket.inet_aton("0.0.0.0")
        reply += struct.pack("!H", 0)
        try:
            self.client.sendall(reply)
        except (socket.error, OSError):
            pass

    def _recv(self, n: int) -> Optional[bytes]:
        """Receive exactly n bytes from client."""
        data = b""
        while len(data) < n:
            try:
                chunk = self.client.recv(n - len(data))
            except (socket.error, OSError):
                return None
            if not chunk:
                return None
            data += chunk
        return data


class SocksServer:
    """SOCKS5 proxy server that relays connections through filesystem bridge."""

    def __init__(
        self,
        bridge_dir: str | Path,
        listen_host: str = "127.0.0.1",
        listen_port: int = 1080,
    ):
        self.tcp_bridge = TcpBridgeDirectory(bridge_dir)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server_sock: Optional[socket.socket] = None
        self._running = False

    def start(self) -> None:
        """Start the SOCKS5 server. Blocks until stopped."""
        self.tcp_bridge.init()

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind((self.listen_host, self.listen_port))
        self._server_sock.listen(32)
        self._running = True

        if self.listen_host in ("0.0.0.0", "::"):
            logger.warning(
                "WARNING: SOCKS5 proxy is bound to all interfaces! "
                "This exposes the proxy to the network. Use 127.0.0.1 for local-only access."
            )

        logger.info(f"SOCKS5 proxy started: {self.listen_host}:{self.listen_port}")
        logger.info(f"Bridge directory: {self.tcp_bridge.root}")

        try:
            while self._running:
                try:
                    client_sock, addr = self._server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                handler = SocksHandler(client_sock, addr, self.tcp_bridge)
                t = threading.Thread(target=handler.handle, daemon=True)
                t.start()
        except KeyboardInterrupt:
            logger.info("SOCKS server interrupted")
        finally:
            self._server_sock.close()
            logger.info("SOCKS server stopped")

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass


def run_socks(
    bridge_dir: str,
    listen_host: str = "127.0.0.1",
    listen_port: int = 1080,
) -> None:
    """Entry point for running the SOCKS5 proxy."""
    server = SocksServer(
        bridge_dir=bridge_dir,
        listen_host=listen_host,
        listen_port=listen_port,
    )

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    server.start()
